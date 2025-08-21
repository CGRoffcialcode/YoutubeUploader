import os
import pickle
import isodate

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from pytube import YouTube

# --- CONFIGURATION ---
CLIENT_SECRETS_FILE = "client_secrets.json"
API_NAME = 'youtube'
API_VERSION = 'v3'
# Scopes allow the script to manage your YouTube account.
# youtube.upload is for uploading, youtube.readonly is for reading video details.
SCOPES = ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly']


def get_authenticated_service():
    """Handles user authentication and returns a YouTube API service object."""
    credentials = None
    # The file token.pickle stores the user's access and refresh tokens.
    # It's created automatically when the authorization flow completes for the first time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            credentials = pickle.load(token)

    # If there are no (valid) credentials available, let the user log in.
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            credentials = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.pickle', 'wb') as token:
            pickle.dump(credentials, token)

    return build(API_NAME, API_VERSION, credentials=credentials)


def get_channel_shorts(youtube):
    """Fetches all videos from the user's channel and filters for Shorts."""
    print("Fetching videos from your channel to find Shorts...")
    shorts = []
    try:
        # Get the 'uploads' playlist ID for the authenticated user's channel.
        channels_response = youtube.channels().list(mine=True, part='contentDetails').execute()
        if not channels_response.get('items'):
            print("Could not find your channel. Make sure you are authenticated with the correct account.")
            return []
        
        playlist_id = channels_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']

        next_page_token = None
        while True:
            playlist_items_response = youtube.playlistItems().list(
                playlistId=playlist_id,
                part='contentDetails',
                maxResults=50,
                pageToken=next_page_token
            ).execute()

            video_ids = [item['contentDetails']['videoId'] for item in playlist_items_response['items']]
            
            if not video_ids:
                break

            # Get video details, including duration
            videos_response = youtube.videos().list(
                id=','.join(video_ids),
                part='snippet,contentDetails'
            ).execute()

            for video in videos_response['items']:
                duration_iso = video['contentDetails']['duration']
                duration_seconds = isodate.parse_duration(duration_iso).total_seconds()
                # A common way to identify a Short is by its duration (< 61 seconds)
                if duration_seconds < 61:
                    shorts.append({
                        'id': video['id'],
                        'title': video['snippet']['title']
                    })

            next_page_token = playlist_items_response.get('nextPageToken')
            if not next_page_token:
                break

    except HttpError as e:
        print(f"An HTTP error {e.resp.status} occurred: {e.content}")
        return []

    return shorts


def download_video(video_id, path='.'):
    """Downloads a YouTube video by its ID using pytube."""
    try:
        yt = YouTube(f'https://www.youtube.com/watch?v={video_id}')
        # Filter for progressive mp4 streams and get the highest resolution
        stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
        if not stream:
            print("No suitable mp4 stream found for this video.")
            return None
        
        print(f"Downloading '{yt.title}'...")
        filepath = stream.download(output_path=path)
        return filepath
    except Exception as e:
        print(f"An error occurred during download: {e}")
        return None


def upload_video(youtube, file_path, title, description, tags, privacy_status="private"):
    """Uploads a video file to YouTube."""
    try:
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags,
                'categoryId': '22'  # '22' is 'People & Blogs'. Change if needed.
            },
            'status': {
                'privacyStatus': privacy_status,
                'selfDeclaredMadeForKids': False
            }
        }

        media = MediaFileUpload(file_path, chunksize=-1, resumable=True)

        print(f"Uploading '{title}'...")
        request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        response = request.execute()
        print(f"Upload successful! Video ID: {response['id']}")
        return response['id']

    except HttpError as e:
        print(f"An HTTP error {e.resp.status} occurred during upload: {e.content}")
        return None


def main():
    youtube = get_authenticated_service()
    if not youtube:
        print("Could not authenticate. Exiting.")
        return

    shorts = get_channel_shorts(youtube)
    if not shorts:
        print("No Shorts found on your channel or an error occurred.")
        return

    print("\n--- Found the following Shorts on your channel ---")
    for i, short in enumerate(shorts):
        print(f"  {i + 1}: {short['title']} (ID: {short['id']})")
    print("--------------------------------------------------\n")

    try:
        choice = int(input("Enter the number of the Short to download and re-upload: ")) - 1
        if not 0 <= choice < len(shorts):
            raise ValueError("Choice out of range.")
        
        num_uploads = int(input("How many times do you want to upload this video? (Use with caution!): "))
        if num_uploads <= 0:
            raise ValueError("Number of uploads must be positive.")

    except ValueError as e:
        print(f"Invalid input. Please enter a valid number. Details: {e}")
        return

    selected_short = shorts[choice]
    video_path = download_video(selected_short['id'])

    if not video_path:
        print("Failed to download the video. Aborting.")
        return

    for i in range(num_uploads):
        print(f"\n--- Starting Upload #{i + 1} of {num_uploads} ---")
        # It's a good practice to slightly change the title to avoid being flagged as spam
        new_title = f"{selected_short['title']} (Re-upload)"
        new_description = f"This is a re-upload of my Short titled '{selected_short['title']}'."
        upload_video(youtube, video_path, new_title, new_description, ["shorts", "your-custom-tag"])

    # Clean up the downloaded file
    os.remove(video_path)
    print(f"\nProcess complete. Cleaned up downloaded file: {video_path}")


if __name__ == '__main__':
    main()