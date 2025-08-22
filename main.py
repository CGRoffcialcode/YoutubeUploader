"""
Retro Shorts Re-Uploader

A GUI-based application for managing YouTube video uploads.
Features include re-uploading existing Shorts, uploading local video files,
a flexible scheduling system with presets, and email alerts for critical errors.
"""

# --- Standard Library Imports ---
import os  # Used for interacting with the operating system, like checking file paths.
import pickle  # Used for serializing and de-serializing Python objects (for the auth token).
import subprocess  # Used to run external commands, specifically for yt-dlp.
import datetime  # Used for handling dates and times for scheduling.
import json  # Used for reading and writing the presets configuration file.
import sys  # Used to redirect standard output to the GUI's log console.
import queue  # A thread-safe queue used for communication between the GUI and worker threads.
import threading  # Used to run long tasks (API calls, downloads) without freezing the GUI.
import smtplib  # Used for sending emails via SMTP for error notifications.
from email.message import EmailMessage  # Used to construct the email for error notifications.
import traceback  # Used to get detailed error information for logging and email alerts.
import requests # Used to send messages to Discord webhooks.

# --- Third-Party Library Imports ---

# Google API Client Libraries
from google_auth_oauthlib.flow import InstalledAppFlow  # Handles the OAuth 2.0 authorization flow.
from google.auth.transport.requests import Request  # Handles HTTP requests for Google authentication.
from googleapiclient.discovery import build  # Builds a service object for interacting with a Google API.
from googleapiclient.errors import HttpError  # Custom exception class for Google API errors.
from googleapiclient.http import MediaFileUpload  # Handles the upload of large media files.

# GUI Libraries
import tkinter as tk  # The standard Python interface to the Tk GUI toolkit.
from tkinter import ttk, font, messagebox, filedialog  # Themed widgets, font control, dialog boxes, and file dialogs.
from tkcalendar import DateEntry  # A third-party calendar widget for date selection.

# Other Libraries
import isodate  # Used to parse ISO 8601 duration strings from the YouTube API.


# --- CONFIGURATION ---
# This section contains global constants that configure the application's behavior.

# YouTube API settings
CLIENT_SECRETS_FILE = "client_secrets.json"
API_NAME = 'youtube'
API_VERSION = 'v3'
# Scopes allow the script to manage your YouTube account.
SCOPES = ['https://www.googleapis.com/auth/youtube.upload',
          'https://www.googleapis.com/auth/youtube.readonly']
YOUTUBE_VIDEO_CATEGORY_ID = '22'  # '22' is 'People & Blogs'

# Email Alert Configuration
# IMPORTANT: For this to work with Gmail, you MUST use an "App Password".
# 1. Go to your Google Account settings: https://myaccount.google.com/
# 2. Go to "Security".
# 3. Enable 2-Step Verification if it's not already on.
# 4. Go to "App Passwords".
# 5. Create a new app password for this script and copy the 16-character password.
# 6. Paste that password into SENDER_APP_PASSWORD below.
# DO NOT use your regular Google password here.
ENABLE_EMAIL_ALERTS = True  # Set to False to disable email notifications
SENDER_EMAIL = "your_email@gmail.com"  # The email address you're sending from
SENDER_APP_PASSWORD = "your_16_character_app_password"  # The App Password you generated
RECIPIENT_EMAIL = "crgroblooxfortniteyt@gmail.com" # The email address to send alerts to

# Discord Webhook Configuration
# 1. In your Discord server, go to Server Settings > Integrations > Webhooks.
# 2. Click "New Webhook", give it a name (e.g., "YT Uploader Bot"), and choose a channel.
# 3. Click "Copy Webhook URL" and paste it below.
ENABLE_DISCORD_NOTIFICATIONS = True # Set to False to disable
DISCORD_WEBHOOK_URL = "your_discord_webhook_url_here"


# ==============================================================================
# --- CORE APPLICATION LOGIC (NON-GUI) ---
# ==============================================================================

def get_authenticated_service():
    """
    Handles user authentication via OAuth 2.0 and returns a YouTube API service object.
    It stores and reuses credentials in a 'token.pickle' file.
    """
    credentials = None
    # The file token.pickle stores the user's access and refresh tokens.
    # It's created automatically when the authorization flow completes for the first time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            credentials = pickle.load(token)
    try:
        # If there are no (valid) credentials, or they are expired, let the user log in or refresh.
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                # If credentials exist but are expired, refresh them.
                credentials.refresh(Request())
            else:
                # If no credentials exist, start the OAuth flow.
                flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
                credentials = flow.run_local_server(port=0)
            # Save the new or refreshed credentials for the next run.
            with open('token.pickle', 'wb') as token:
                pickle.dump(credentials, token)

        # Build the service object which will be used to make API calls.
        service = build(API_NAME, API_VERSION, credentials=credentials)
        
        # Fetch the channel's title to use in the uploader signature.
        channels_response = service.channels().list(mine=True, part='snippet').execute()
        channel_title = channels_response['items'][0]['snippet']['title']

        return service, channel_title

    except Exception as e:
        # If any part of authentication fails, send an email alert and stop the app.
        error_body = f"An unrecoverable error occurred during the authentication process.\n\n"
        error_body += f"Error: {e}\n\n"
        error_body += "Traceback:\n"
        error_body += traceback.format_exc()
        # Also print the error to the console for easier debugging,
        # especially if email alerts are not configured.
        print("\n" + "="*80)
        print("!!! AUTHENTICATION ERROR !!!")
        print(error_body)
        print("="*80 + "\n")
        send_error_email("Authentication Failure", error_body)
        # Return None to stop the app gracefully
        return None, None


def get_channel_shorts(youtube):
    """
    Fetches all videos from the user's channel by paginating through the 'uploads' playlist,
    then filters them to identify Shorts based on their duration (< 61 seconds).
    """
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
        # Loop through all pages of the playlist results.
        while True:
            playlist_items_response = youtube.playlistItems().list(
                playlistId=playlist_id,
                part='contentDetails',
                maxResults=50,
                pageToken=next_page_token
            ).execute()

            # Get a list of video IDs from the current page of results.
            video_ids = [item['contentDetails']['videoId'] for item in playlist_items_response['items']]
            
            if not video_ids:
                break

            # Make a single API call to get details for all videos on this page.
            videos_response = youtube.videos().list(
                id=','.join(video_ids),
                part='snippet,contentDetails'
            ).execute()

            for video in videos_response['items']:
                duration_iso = video['contentDetails']['duration']
                # Convert ISO 8601 duration format (e.g., "PT1M3S") to seconds.
                duration_seconds = isodate.parse_duration(duration_iso).total_seconds()
                if duration_seconds < 61:
                    # If the video is a Short, add its data to our list.
                    shorts.append({
                        'id': video['id'],
                        'title': video['snippet']['title'],
                        'description': video['snippet']['description'],
                        'published': video['snippet']['publishedAt']
                    })

            # Get the token for the next page, or break the loop if this is the last page.
            next_page_token = playlist_items_response.get('nextPageToken')
            if not next_page_token:
                break

    except HttpError as e:
        # If the API returns an error, log it and send an email alert.
        print(f"An HTTP error {e.resp.status} occurred: {e.content}")
        error_body = f"An HTTP error occurred while fetching channel shorts.\n\n"
        error_body += f"Status: {e.resp.status}\n"
        error_body += f"Content: {e.content.decode('utf-8')}\n\n"
        error_body += "Traceback:\n"
        error_body += traceback.format_exc()
        send_error_email("Failed to Fetch Shorts", error_body)
        return []

    return shorts


def download_video(video_id, path='.'):
    """
    Downloads a YouTube video by its ID using the external 'yt-dlp' command-line tool.
    This is more robust than using a library like pytube.
    """
    video_url = f'https://www.youtube.com/watch?v={video_id}'
    print(f"\nAttempting to download video in highest quality (up to 4K) using yt-dlp: {video_url}")

    # Define a clean, predictable filename. We expect an mp4 file.
    # This avoids parsing yt-dlp output and potential filesystem character issues.
    expected_filename = f"{video_id}.mp4"
    filepath = os.path.join(path, expected_filename)

    command = [
        'yt-dlp',
        # A robust format selector that prioritizes compatibility.
        # 1. Try to get the best separate MP4 video and M4A audio streams.
        # 2. If that fails, get the best pre-merged MP4 file.
        # 3. If that fails, get the absolute best of any format (and rely on re-encoding).
        '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        # After downloading and merging, re-encode the video into a standard MP4.
        # This is more robust than --merge-output-format and fixes codec issues.
        # It ensures maximum compatibility with YouTube's uploader.
        '--recode-video', 'mp4',
        # Specify the exact output file path, including the .mp4 extension.
        '-o', filepath,
        video_url
    ]

    # --- Attempt 1: High-Quality (4K) Download ---
    try:
        print(f"Executing command (4K attempt): {' '.join(command)}")
        subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')

    except subprocess.CalledProcessError as e:
        # This block runs if the high-quality download fails.
        print("\nWarning: Highest quality (4K) download failed. This can happen with certain video codecs.")
        print("Attempting a fallback download in a more compatible format (max 1080p)...")
        # Print the specific error message from yt-dlp for better debugging.
        last_error_line = e.stderr.strip().splitlines()[-1]
        print(f"Original error: {last_error_line}")

        # --- Attempt 2: Safe Fallback (Max 1080p) Command ---
        safe_command = [
            'yt-dlp',
            # Get the best pre-merged MP4 file. This is usually 1080p or 720p and highly compatible.
            '-f', 'best[ext=mp4]/best',
            '-o', filepath,
            video_url
        ]
        try:
            print(f"Executing command (safe mode): {' '.join(safe_command)}")
            subprocess.run(safe_command, check=True, capture_output=True, text=True, encoding='utf-8')
        except Exception as final_e:
            # If the safe mode also fails, then we report the final error.
            print(f"\nAn error occurred during download with yt-dlp (safe mode also failed): {final_e}")
            error_body = f"An error occurred while downloading a video with yt-dlp (both 4K and safe mode failed).\n\n"
            error_body += f"Video URL: {video_url}\n"
            error_body += f"Error: {final_e}\n\n"
            if isinstance(final_e, subprocess.CalledProcessError):
                print(f"Error Output:\n{final_e.stderr}")
                error_body += f"yt-dlp stderr:\n{final_e.stderr}\n\n"
            error_body += "Traceback:\n"
            error_body += traceback.format_exc()
            send_error_email("Video Download Failure", error_body)
            return None

    # After either the 4K or safe mode download succeeds, verify the file exists.
    if not os.path.exists(filepath):
         print(f"\nError: yt-dlp reported success, but the file was not found at '{filepath}'")
         return None

    print(f"\nSuccessfully downloaded to: {filepath}")
    return filepath


def upload_video(youtube, file_path, title, description, tags, channel_name, privacy_status="private", publish_at=None):
    """
    Uploads a video file to YouTube using the provided metadata.
    Handles scheduling and adds a custom signature to the description.
    """
    try:
        # Ensure 'tags' is a mutable list and add the custom YTUPLOADER tag.
        # The YouTube API expects a list of strings for tags (without the '#').
        final_tags = list(tags) if tags else []
        if "YTUPLOADER" not in final_tags:
            final_tags.append("YTUPLOADER")

        # Construct the 'body' of the API request with the video's metadata.
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': final_tags,
                'categoryId': YOUTUBE_VIDEO_CATEGORY_ID
            },
            'status': {
                'privacyStatus': privacy_status,
                'selfDeclaredMadeForKids': False
            }
        }

        # Append the custom signature to the end of the video description.
        uploader_tag = f"\n\n---\n@CGRofficalcode @{channel_name} used YTUPLOADER"
        body['snippet']['description'] += uploader_tag

        # If a publish time is provided, set the video to private and add the schedule time.
        # YouTube requires scheduled videos to be private initially for scheduling to work.
        if publish_at:
            body['status']['privacyStatus'] = 'private'
            body['status']['publishAt'] = publish_at

        # Create a MediaFileUpload object to handle the resumable upload of the video file.
        media = MediaFileUpload(file_path, chunksize=-1, resumable=True)

        # Create and execute the API request to insert the video.
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
        # If the upload fails, log the error and send an email alert.
        error_message = e.content.decode('utf-8')
        print(f"An HTTP error {e.resp.status} occurred during upload: {error_message}")
        error_body = f"An HTTP error occurred while uploading a video.\n\n"
        error_body += f"Video Title: {title}\n"
        error_body += f"File Path: {file_path}\n"
        error_body += f"Status: {e.resp.status}\n"
        error_body += f"Content: {error_message}\n\n"
        error_body += "Traceback:\n"
        error_body += traceback.format_exc()
        send_error_email("Video Upload Failure", error_body)
        return None


def send_error_email(subject, body):
    """
    Sends an email notification if an error occurs, using the configured SMTP settings.
    Checks if email alerts are enabled and properly configured before sending.
    """
    # Do not proceed if alerts are disabled or if the config still has default placeholder values.
    if not ENABLE_EMAIL_ALERTS or "your_email@gmail.com" in SENDER_EMAIL or "your_16_character_app_password" in SENDER_APP_PASSWORD:
        print("Email alerts are not configured. Skipping.")
        return

    # Construct the email message.
    msg = EmailMessage()
    msg.set_content(body)
    msg['Subject'] = f"[YT Uploader ERROR] {subject}"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECIPIENT_EMAIL

    try:
        # Connect to Gmail's SMTP server over SSL, log in, and send the message.
        print(f"Sending error email to {RECIPIENT_EMAIL}...")
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
            smtp.send_message(msg)
        print("Error email sent successfully.")
    except Exception as e:
        print(f"CRITICAL: Failed to send error email. Error: {e}")


def send_discord_notification(message):
    """
    Sends a notification message to the configured Discord webhook URL.
    """
    if not ENABLE_DISCORD_NOTIFICATIONS or "your_discord_webhook_url_here" in DISCORD_WEBHOOK_URL:
        print("Discord notifications are not configured. Skipping.")
        return

    # Discord webhooks expect a JSON payload with a 'content' key.
    payload = {
        "content": message
    }

    try:
        print(f"Sending notification to Discord...")
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        print("Discord notification sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"CRITICAL: Failed to send Discord notification. Error: {e}")


# ==============================================================================
# --- GUI HELPER CLASSES & DIALOGS ---
# ==============================================================================

class ReUploadDialog(tk.Toplevel):
    """A dialog window for editing the metadata of videos selected for re-upload."""
    def __init__(self, parent, shorts_to_edit):
        """
        Initializes the dialog.
        'parent' is the main GUI window, 'shorts_to_edit' is a list of short data dictionaries.
        """
        super().__init__(parent)
        self.transient(parent)
        self.title("Edit Re-Uploads")
        self.app = parent
        self.result = None
        # Convert list of dicts to a dict keyed by a unique identifier (ID + index)
        self.video_metadata = {
            f"{s['id']}_{i}": {"title": s['title'], "description": s['description'], "source_id": s['id']}
            for i, s in enumerate(shorts_to_edit)}
        self.current_key = None

        self.configure(bg=self.app.C_BG)
        self.geometry("800x500")
        self.grab_set()

        self._create_widgets()
        self._populate_listbox()

        # This makes the dialog modal, blocking interaction with the main window until it's closed.
        self.wait_window(self)

    def _create_widgets(self):
        """Creates and arranges all the widgets (listbox, entry fields, buttons) in the dialog."""
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill="both", expand=True)

        # Left side: List of videos
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(list_frame, text="Videos to Re-upload").pack(anchor="w")
        self.video_listbox = tk.Listbox(
            list_frame, bg=self.app.C_LIST_BG, fg=self.app.C_TEXT,
            selectbackground=self.app.C_ACCENT_RED, relief="flat", exportselection=False)
        self.video_listbox.pack(fill="y", expand=True)
        self.video_listbox.bind("<<ListboxSelect>>", self._on_video_select)

        # Right side: Metadata editor
        editor_frame = ttk.Frame(main_frame)
        editor_frame.pack(side="left", fill="both", expand=True)
        
        ttk.Label(editor_frame, text="Title:").pack(anchor="w")
        self.title_entry = ttk.Entry(editor_frame, font=self.app.FONT_UI)
        self.title_entry.pack(fill="x", pady=(0, 10))

        ttk.Label(editor_frame, text="Description:").pack(anchor="w")
        self.desc_text = tk.Text(
            editor_frame, bg=self.app.C_LIST_BG, fg=self.app.C_TEXT, relief="flat", insertbackground=self.app.C_TEXT)
        self.desc_text.pack(fill="both", expand=True)

        # Bottom buttons
        button_frame = ttk.Frame(self, padding="10")
        button_frame.pack(fill="x")
        ttk.Button(button_frame, text="OK", command=self.on_ok).pack(side="right", padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.destroy).pack(side="right")

    def _populate_listbox(self):
        """Fills the listbox with the titles of the videos to be edited."""
        for key, data in self.video_metadata.items():
            self.video_listbox.insert(tk.END, data['title'])
        # Automatically select the first item in the list to show its details.
        if self.video_metadata:
            self.video_listbox.selection_set(0)
            self._on_video_select()

    def _on_video_select(self, event=None):
        """
        Callback function for when a user clicks on an item in the listbox.
        It saves the data from the currently displayed item and loads the data for the new selection.
        """
        selected_indices = self.video_listbox.curselection()
        if not selected_indices:
            return

        # Save metadata of previously selected item before switching
        if self.current_key:
            self.video_metadata[self.current_key]['title'] = self.title_entry.get()
            self.video_metadata[self.current_key]['description'] = self.desc_text.get("1.0", tk.END).strip()
            # Update the title in the listbox in case it was changed
            all_keys = list(self.video_metadata.keys())
            try:
                idx = all_keys.index(self.current_key)
                self.video_listbox.delete(idx)
                self.video_listbox.insert(idx, self.video_metadata[self.current_key]['title'])
                self.video_listbox.selection_set(idx) # Re-select it
            except ValueError:
                pass # Should not happen

        # Load metadata of newly selected item
        selected_index = self.video_listbox.curselection()[0]
        self.current_key = list(self.video_metadata.keys())[selected_index]
        
        if self.current_key:
            metadata = self.video_metadata[self.current_key]
            self.title_entry.delete(0, tk.END)
            self.title_entry.insert(0, metadata['title'])
            self.desc_text.delete("1.0", tk.END)
            self.desc_text.insert("1.0", metadata['description'])

    def on_ok(self):
        """
        Callback for the 'OK' button. Saves the currently edited item's data,
        sets the dialog's result to the complete list of edited metadata, and closes the window.
        """
        self._on_video_select() # Save the currently open file's metadata
        self.result = list(self.video_metadata.values())
        self.destroy()

class LocalUploadDialog(tk.Toplevel):
    """A dialog window for editing the metadata of local video files selected for upload."""
    def __init__(self, parent, file_paths):
        """Initializes the dialog with a list of local file paths."""
        super().__init__(parent)
        self.transient(parent)
        self.title("Edit Local Uploads")
        self.app = parent
        self.result = None
        self.file_metadata = {
            path: {"title": os.path.basename(path).rsplit('.', 1)[0], "description": ""} for path in file_paths}

        self.configure(bg=self.app.C_BG)
        self.geometry("800x500")
        self.grab_set()

        self._create_widgets()
        self._populate_listbox()

    def _create_widgets(self):
        """Creates and arranges all the widgets in the dialog."""
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill="both", expand=True)

        # Left side: List of files
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(list_frame, text="Video Files").pack(anchor="w")
        self.file_listbox = tk.Listbox(
            list_frame, bg=self.app.C_LIST_BG, fg=self.app.C_TEXT,
            selectbackground=self.app.C_ACCENT_RED, relief="flat", exportselection=False)
        self.file_listbox.pack(fill="y", expand=True)
        self.file_listbox.bind("<<ListboxSelect>>", self._on_file_select)

        # Right side: Metadata editor
        editor_frame = ttk.Frame(main_frame)
        editor_frame.pack(side="left", fill="both", expand=True)
        
        ttk.Label(editor_frame, text="Title:").pack(anchor="w")
        self.title_entry = ttk.Entry(editor_frame, font=self.app.FONT_UI)
        self.title_entry.pack(fill="x", pady=(0, 10))

        ttk.Label(editor_frame, text="Description:").pack(anchor="w")
        self.desc_text = tk.Text(
            editor_frame, bg=self.app.C_LIST_BG, fg=self.app.C_TEXT, relief="flat", insertbackground=self.app.C_TEXT)
        self.desc_text.pack(fill="both", expand=True)

        # Bottom buttons
        button_frame = ttk.Frame(self, padding="10")
        button_frame.pack(fill="x")
        ttk.Button(button_frame, text="OK", command=self.on_ok).pack(side="right", padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.destroy).pack(side="right")

    def _populate_listbox(self):
        """Fills the listbox with the basenames of the selected video files."""
        for path in self.file_metadata.keys():
            self.file_listbox.insert(tk.END, os.path.basename(path))
        if self.file_metadata:
            self.file_listbox.selection_set(0)
            self._on_file_select()

    def _on_file_select(self, event=None):
        """
        Callback for when a user clicks on a file in the listbox.
        Saves the data for the previous item and loads the data for the new selection.
        """
        selected_indices = self.file_listbox.curselection()
        if not selected_indices:
            return

        # Save metadata of previously selected item before switching
        if hasattr(self, 'current_path') and self.current_path:
            self.file_metadata[self.current_path]['title'] = self.title_entry.get()
            self.file_metadata[self.current_path]['description'] = self.desc_text.get("1.0", tk.END).strip()

        # Load metadata of newly selected item
        selected_filename = self.file_listbox.get(selected_indices[0])
        # Find the full path from the filename
        self.current_path = next((path for path in self.file_metadata if os.path.basename(path) == selected_filename), None)
        
        if self.current_path:
            metadata = self.file_metadata[self.current_path]
            self.title_entry.delete(0, tk.END)
            self.title_entry.insert(0, metadata['title'])
            self.desc_text.delete("1.0", tk.END)
            self.desc_text.insert("1.0", metadata['description'])

    def on_ok(self):
        """Callback for the 'OK' button. Saves final data and closes the window."""
        self._on_file_select() # Save the currently open file's metadata
        self.result = list(self.file_metadata.values())
        self.destroy()

# --- Preset Management ---

class PresetManager:
    """
    A helper class that handles loading and saving of scheduling presets from a JSON file.
    This abstracts the file I/O away from the GUI logic.
    """
    def __init__(self, filename='scheduling_presets.json'):
        self.filename = filename
        self.presets = self.load()

    def load(self):
        """Loads presets from the JSON file. Returns default if not found."""
        try:
            with open(self.filename, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # Return a default preset if the file doesn't exist or is empty/corrupt
            return {
                "Weekly Sunday @ 9am": {
                    "start_day": "Sunday",
                    "hour": 9,
                    "minute": 0,
                    "interval_days": 7
                }
            }

    def save(self):
        """Saves the current presets to the JSON file."""
        with open(self.filename, 'w') as f:
            json.dump(self.presets, f, indent=4)

    def add_or_update(self, name, data):
        """Adds a new preset or updates an existing one."""
        self.presets[name] = data
        self.save()

    def delete(self, name):
        """Deletes a preset by name."""
        if name in self.presets:
            del self.presets[name]
            self.save()

    def get_preset_names(self):
        """Returns a list of preset names."""
        return list(self.presets.keys())


# --- Preset Management Dialog ---

class PresetManagementDialog(tk.Toplevel):
    """A dialog window that allows the user to add, update, and delete scheduling presets."""
    def __init__(self, parent, preset_manager):
        super().__init__(parent)
        self.transient(parent)
        self.title("Manage Scheduling Presets")
        # The parent of this dialog is SchedulingDialog.
        # The parent of SchedulingDialog is the main app, which holds the theme.
        self.app = parent.parent
        self.preset_manager = preset_manager

        self.configure(bg=self.app.C_BG)
        self.grab_set()

        self._create_widgets()
        self._populate_listbox()

    def _create_widgets(self):
        """Creates and arranges all the widgets for the preset editor."""
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill="both", expand=True)

        # Left side: List of presets
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(list_frame, text="Presets").pack(anchor="w")
        self.preset_listbox = tk.Listbox(
            list_frame, bg=self.app.C_LIST_BG, fg=self.app.C_TEXT,
            selectbackground=self.app.C_ACCENT_RED, relief="flat", exportselection=False)
        self.preset_listbox.pack(fill="y", expand=True)
        self.preset_listbox.bind("<<ListboxSelect>>", self._populate_fields_from_selection)

        # Right side: Editor for a preset
        editor_frame = ttk.Frame(main_frame)
        editor_frame.pack(side="left", fill="both", expand=True)
        
        ttk.Label(editor_frame, text="Preset Name:").grid(row=0, column=0, sticky="w", pady=2)
        self.name_entry = ttk.Entry(editor_frame)
        self.name_entry.grid(row=0, column=1, sticky="ew", pady=2)

        ttk.Label(editor_frame, text="Start Day:").grid(row=1, column=0, sticky="w", pady=2)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        self.day_combo = ttk.Combobox(editor_frame, values=days, state="readonly")
        self.day_combo.grid(row=1, column=1, sticky="ew", pady=2)
        self.day_combo.set("Sunday")

        ttk.Label(editor_frame, text="Time (24h):").grid(row=2, column=0, sticky="w", pady=2)
        time_frame = ttk.Frame(editor_frame)
        time_frame.grid(row=2, column=1, sticky="w")
        self.hour_spinbox = ttk.Spinbox(time_frame, from_=0, to=23, width=3, format="%02.0f")
        self.hour_spinbox.pack(side="left")
        self.hour_spinbox.set("09")
        ttk.Label(time_frame, text=":").pack(side="left", padx=2)
        self.minute_spinbox = ttk.Spinbox(time_frame, from_=0, to=59, width=3, format="%02.0f")
        self.minute_spinbox.pack(side="left")
        self.minute_spinbox.set("00")

        ttk.Label(editor_frame, text="Interval (days):").grid(row=3, column=0, sticky="w", pady=2)
        self.interval_spinbox = ttk.Spinbox(editor_frame, from_=0, to=365, width=4)
        self.interval_spinbox.grid(row=3, column=1, sticky="w", pady=2)
        self.interval_spinbox.set("7")

        # Buttons
        button_frame = ttk.Frame(editor_frame)
        button_frame.grid(row=4, column=0, columnspan=2, pady=10)
        ttk.Button(button_frame, text="Add / Update", command=self._on_add_update).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Delete", command=self._on_delete).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Close", command=self.destroy).pack(side="right", padx=5)

    def _populate_listbox(self):
        """Clears and re-populates the listbox with current preset names."""
        self.preset_listbox.delete(0, tk.END)
        for name in self.preset_manager.get_preset_names():
            self.preset_listbox.insert(tk.END, name)

    def _populate_fields_from_selection(self, event=None):
        """When a preset is selected in the list, its details are shown in the editor fields."""
        selected_indices = self.preset_listbox.curselection()
        if not selected_indices:
            return
        
        selected_name = self.preset_listbox.get(selected_indices[0])
        preset_data = self.preset_manager.presets.get(selected_name)

        if preset_data:
            self.name_entry.delete(0, tk.END)
            self.name_entry.insert(0, selected_name)
            self.day_combo.set(preset_data.get("start_day", "Sunday"))
            self.hour_spinbox.set(f"{preset_data.get('hour', 9):02}")
            self.minute_spinbox.set(f"{preset_data.get('minute', 0):02}")
            self.interval_spinbox.set(str(preset_data.get("interval_days", 7)))

    def _on_add_update(self):
        """
        Callback for the 'Add / Update' button.
        Saves the preset currently defined in the editor fields to the JSON file.
        """
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showerror("Invalid Input", "Preset name cannot be empty.", parent=self)
            return

        data = {
            "start_day": self.day_combo.get(),
            "hour": int(self.hour_spinbox.get()),
            "minute": int(self.minute_spinbox.get()),
            "interval_days": int(self.interval_spinbox.get())
        }
        self.preset_manager.add_or_update(name, data)
        
        # Refresh the list and re-select the item
        self._populate_listbox()
        try:
            new_index = self.preset_listbox.get(0, "end").index(name)
            self.preset_listbox.selection_set(new_index)
            self.preset_listbox.see(new_index)
        except ValueError:
            pass # Item might have been renamed

    def _on_delete(self):
        """
        Callback for the 'Delete' button.
        Removes the currently selected preset from the list and the JSON file.
        """
        selected_indices = self.preset_listbox.curselection()
        if not selected_indices:
            print("No preset selected to delete.")
            return
        
        selected_name = self.preset_listbox.get(selected_indices[0])
        self.preset_manager.delete(selected_name)
        
        # Clear fields and refresh list
        self.name_entry.delete(0, tk.END)
        self._populate_listbox()

# --- Scheduling Dialog ---

class SchedulingDialog(tk.Toplevel):
    """A dialog window with tabs for choosing scheduling options (Manual or Preset)."""
    def __init__(self, parent, preset_manager):
        super().__init__(parent)
        self.transient(parent)
        self.title("Set Upload Schedule")
        self.parent = parent
        self.preset_manager = preset_manager
        self.result = None

        # Inherit theme from parent
        self.configure(bg=parent.C_BG)
        self.grab_set()

        # Use a Notebook widget to create a tabbed interface.
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(pady=10, padx=10, fill="both", expand=True)

        # Manual Scheduling Tab
        manual_frame = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(manual_frame, text="Manual Schedule")
        self._create_manual_tab(manual_frame)

        # Preset Scheduling Tab
        preset_frame = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(preset_frame, text="Use Preset")
        self._create_preset_tab(preset_frame)

        # OK/Cancel Buttons
        button_frame = ttk.Frame(self, padding="10")
        button_frame.pack(fill="x")
        ttk.Button(button_frame, text="OK", command=self.on_ok).pack(side="right", padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.destroy).pack(side="right")

        self.wait_window(self)

    def _create_manual_tab(self, parent):
        """Creates the widgets for the 'Manual Schedule' tab."""
        ttk.Label(parent, text="First Upload Date:").grid(row=0, column=0, sticky="w", pady=2)
        self.date_entry = DateEntry(parent, width=12, background=self.parent.C_ACCENT_RED, foreground='white', borderwidth=2)
        self.date_entry.grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(parent, text="First Upload Time (24h):").grid(row=1, column=0, sticky="w", pady=2)
        time_frame = ttk.Frame(parent)
        time_frame.grid(row=1, column=1, sticky="w")
        self.hour_spinbox = ttk.Spinbox(time_frame, from_=0, to=23, width=3, format="%02.0f")
        self.hour_spinbox.pack(side="left")
        self.hour_spinbox.set(f"{datetime.datetime.now().hour:02}")
        ttk.Label(time_frame, text=":").pack(side="left", padx=2)
        self.minute_spinbox = ttk.Spinbox(time_frame, from_=0, to=59, width=3, format="%02.0f")
        self.minute_spinbox.pack(side="left")
        self.minute_spinbox.set("00")

        ttk.Label(parent, text="Interval Between Uploads:").grid(row=2, column=0, sticky="w", pady=2)
        interval_frame = ttk.Frame(parent)
        interval_frame.grid(row=2, column=1, sticky="w")
        self.interval_days = ttk.Spinbox(interval_frame, from_=0, to=365, width=4)
        self.interval_days.pack(side="left")
        self.interval_days.set("7")
        ttk.Label(interval_frame, text="days").pack(side="left", padx=2)

    def _create_preset_tab(self, parent):
        """Creates the widgets for the 'Use Preset' tab."""
        ttk.Label(parent, text="Select a Preset:").pack(anchor="w", pady=2)
        self.preset_combo = ttk.Combobox(parent, values=self.preset_manager.get_preset_names(), state="readonly")
        self.preset_combo.pack(fill="x", pady=2)
        if self.preset_combo['values']:
            self.preset_combo.current(0)

        ttk.Button(parent, text="Manage Presets...", command=self._open_preset_manager).pack(pady=10)

    def _open_preset_manager(self):
        """Opens the preset management dialog and refreshes the combobox on close."""
        manager_dialog = PresetManagementDialog(self, self.preset_manager)
        self.wait_window(manager_dialog)
        # Refresh the combobox with any new or changed presets
        self.preset_combo['values'] = self.preset_manager.get_preset_names()
        if self.preset_combo['values']:
            self.preset_combo.current(0)

    def _calculate_start_datetime_from_preset(self, preset_name):
        """
        Calculates the first upload datetime based on a preset's rules.
        For example, it finds the date of the "next Sunday".
        """
        preset = self.preset_manager.presets[preset_name]
        days_map = {day: i for i, day in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}
        target_weekday = days_map[preset['start_day']]

        now = datetime.datetime.now()
        days_until_target = (target_weekday - now.weekday() + 7) % 7
        
        first_schedule_date = now + datetime.timedelta(days=days_until_target)
        
        # If the target day is today but the time has already passed, schedule for next week
        if days_until_target == 0 and now.time() > datetime.time(preset['hour'], preset['minute']):
            first_schedule_date += datetime.timedelta(days=7)
            
        return first_schedule_date.replace(hour=preset['hour'], minute=preset['minute'], second=0, microsecond=0)

    def on_ok(self):
        """
        Callback for the 'OK' button. Processes the selected schedule from the active tab,
        sets the dialog's result, and closes the window.
        """
        selected_tab_index = self.notebook.index(self.notebook.select())

        # Manual Tab
        if selected_tab_index == 0:
            start_date = self.date_entry.get_date()
            hour = int(self.hour_spinbox.get())
            minute = int(self.minute_spinbox.get())
            start_datetime = datetime.datetime.combine(start_date, datetime.time(hour, minute))
            interval = datetime.timedelta(days=int(self.interval_days.get()))

        # Preset Tab
        elif selected_tab_index == 1:
            preset_name = self.preset_combo.get()
            if not preset_name:
                print("No preset selected.")
                return
            
            start_datetime = self._calculate_start_datetime_from_preset(preset_name)
            interval = datetime.timedelta(days=self.preset_manager.presets[preset_name]['interval_days'])

        self.result = {'start_datetime': start_datetime, 'interval': interval}
        self.destroy()


# ==============================================================================
# --- MAIN GUI APPLICATION CLASS ---
# ==============================================================================

class RedirectStdout:
    """
    A helper class to redirect the standard output (e.g., `print()` statements)
    to a tkinter Text widget. This allows the user to see console output in the GUI.
    """
    def __init__(self, widget):
        self.widget = widget

    def write(self, text):
        self.widget.insert(tk.END, text)
        self.widget.see(tk.END)  # Auto-scroll

    def flush(self):
        pass  # Required for stdout redirection

class YouTubeUploaderGUI(tk.Tk):
    """The main application class, which inherits from tkinter's root window."""
    def __init__(self):
        super().__init__()
        self.title("Retro Shorts Re-Uploader")
        self.geometry("900x700")

        self.youtube_service = None
        self.channel_name = "YouTube User"
        self.preset_manager = PresetManager()
        self.shorts_data = []
        self.task_queue = queue.Queue()

        # --- Theme Constants ---
        self.C_BG = "#1c1c1c"
        self.C_LIST_BG = "#2a2a2a"
        self.C_HEADER_BG = "#3c3c3c"
        self.C_TEXT = "#e0e0e0"
        self.C_ACCENT_RED = "#e53935"
        self.C_ACCENT_RED_ACTIVE = "#f44336"
        self.C_DISABLED = "#555555"
        self.FONT_UI = ("Segoe UI", 10)
        self.FONT_UI_BOLD = ("Segoe UI", 11, "bold")
        self.FONT_LOG = ("Consolas", 9)

        self.configure(bg=self.C_BG)

        # --- State Variables ---
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)

        self._setup_styles()
        self._create_widgets()
        self.process_queue()

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure(".", background=self.C_BG, foreground=self.C_TEXT,
                        fieldbackground=self.C_LIST_BG, borderwidth=0,
                        lightcolor=self.C_BG, darkcolor=self.C_BG)
        
        style.configure("TButton", background=self.C_ACCENT_RED, foreground="white",
                        padding=8, borderwidth=0, font=self.FONT_UI_BOLD)
        style.map("TButton", background=[('active', self.C_ACCENT_RED_ACTIVE), ('disabled', self.C_DISABLED)])
        style.configure("Small.TButton", padding=4, font=self.FONT_UI)

        style.configure("Treeview", background=self.C_LIST_BG, foreground=self.C_TEXT,
                        rowheight=25, fieldbackground=self.C_LIST_BG)
        style.configure("Treeview.Heading", background=self.C_HEADER_BG,
                        foreground=self.C_TEXT, font=self.FONT_UI_BOLD, padding=5)
        style.map("Treeview.Heading", background=[('active', '#4c4c4c')])
        style.map("Treeview", background=[('selected', self.C_ACCENT_RED)], foreground=[('selected', 'white')])

        style.configure("Vertical.TScrollbar", background=self.C_HEADER_BG,
                        troughcolor=self.C_BG, bordercolor=self.C_BG, arrowcolor=self.C_TEXT)
        style.map("Vertical.TScrollbar", background=[('active', self.C_ACCENT_RED)])

        style.configure("TProgressbar", troughcolor=self.C_LIST_BG,
                        background=self.C_ACCENT_RED, thickness=10, borderwidth=0)
        style.configure("Status.TLabel", foreground=self.C_TEXT, font=self.FONT_UI)
        style.configure("TPanedwindow", background=self.C_BG)

    def _create_widgets(self):
        """Creates and arranges all the GUI elements."""
        self._create_top_bar()
        self._create_main_paned_window()
        self._create_status_bar()
        
        # Redirect stdout to the log widget
        sys.stdout = RedirectStdout(self.log_text)

    def _create_top_bar(self):
        """Creates the top bar with main action buttons."""
        top_frame = ttk.Frame(self, padding="10 5")
        top_frame.pack(fill=tk.X)
        
        self.fetch_button = ttk.Button(top_frame, text="FETCH SHORTS", command=self.start_fetch_thread)
        self.fetch_button.pack(side=tk.LEFT, padx=(0, 5))

        self.select_all_button = ttk.Button(top_frame, text="SELECT ALL", command=self.select_all, state=tk.DISABLED)
        self.select_all_button.pack(side=tk.LEFT, padx=5)

        self.deselect_all_button = ttk.Button(top_frame, text="DESELECT ALL", command=self.deselect_all, state=tk.DISABLED)
        self.deselect_all_button.pack(side=tk.LEFT, padx=5)

        self.local_upload_button = ttk.Button(top_frame, text="UPLOAD FROM PC", command=self.start_local_upload_flow)
        self.local_upload_button.pack(side=tk.LEFT, padx=20)

        selection_help_label = ttk.Label(
            top_frame, text="Use Ctrl+Click or Shift+Click to select multiple items.", style="Status.TLabel")
        selection_help_label.pack(side=tk.LEFT, padx=20)

        self.upload_button = ttk.Button(
            top_frame, text="SCHEDULE RE-UPLOADS", command=self.start_upload_thread, state=tk.DISABLED)
        self.upload_button.pack(side=tk.RIGHT)

    def _create_main_paned_window(self):
        """Creates the main resizable area with the video list and log console."""
        main_pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        list_frame = self._create_list_frame(main_pane)
        log_frame = self._create_log_frame(main_pane)

        main_pane.add(list_frame, weight=3)
        main_pane.add(log_frame, weight=1)

    def _create_list_frame(self, parent):
        """Creates the frame containing the video list Treeview."""
        list_container = ttk.Frame(parent)

        columns = ('#', 'title', 'published', 'id')
        self.tree = ttk.Treeview(list_container, columns=columns, show='headings', selectmode='extended')
        self.tree.heading('#', text='#', anchor=tk.W)
        self.tree.heading('title', text='Title')
        self.tree.heading('published', text='Published', anchor=tk.W)
        self.tree.heading('id', text='Video ID', anchor=tk.W)
        self.tree.column('#', width=40, stretch=tk.NO, anchor=tk.CENTER)
        self.tree.column('title', width=500)
        self.tree.column('published', width=150, stretch=tk.NO, anchor=tk.W)
        self.tree.column('id', width=120, stretch=tk.NO, anchor=tk.W)
        
        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return list_container

    def _create_log_frame(self, parent):
        """Creates the frame for the log console."""
        log_container = ttk.Frame(parent)
        log_container.columnconfigure(0, weight=1)
        log_container.rowconfigure(1, weight=1)

        log_header = ttk.Frame(log_container)
        log_header.grid(row=0, column=0, sticky="ew")

        log_label = ttk.Label(log_header, text="LOG CONSOLE", font=self.FONT_UI_BOLD, foreground=self.C_TEXT)
        log_label.pack(side=tk.LEFT, pady=(5,0))

        clear_log_button = ttk.Button(log_header, text="CLEAR LOG", command=self.clear_log, style="Small.TButton")
        clear_log_button.pack(side=tk.RIGHT)

        self.log_text = tk.Text(
            log_container, height=10, bg="#111111", fg=self.C_TEXT, relief="flat",
            font=self.FONT_LOG, insertbackground=self.C_TEXT, selectbackground=self.C_ACCENT_RED)
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(5,0))
        return log_container

    def _create_status_bar(self):
        """Creates the bottom status bar with status text and a progress bar."""
        status_frame = ttk.Frame(self, padding="10 5")
        status_frame.pack(fill=tk.X)

        status_label = ttk.Label(status_frame, textvariable=self.status_var, style="Status.TLabel")
        status_label.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(
            status_frame, orient='horizontal', mode='determinate', variable=self.progress_var)
        self.progress_bar.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=10)

    def set_controls_state(self, state):
        """Enable or disable all major control buttons."""
        self.fetch_button.config(state=state)
        self.upload_button.config(state=state)
        self.select_all_button.config(state=state)
        self.deselect_all_button.config(state=state)

    def start_fetch_thread(self):
        self.set_controls_state(tk.DISABLED)
        self.tree.delete(*self.tree.get_children()) # Clear list
        self.status_var.set("Authenticating and fetching videos...")
        threading.Thread(target=self.worker_fetch_shorts, daemon=True).start()

    def worker_fetch_shorts(self):
        print("--- Starting Authentication and Fetch Process ---")
        self.youtube_service, self.channel_name = get_authenticated_service()
        if self.youtube_service:
            self.task_queue.put(('STATUS_UPDATE', 'Fetching video list from your channel...'))
            self.shorts_data = get_channel_shorts(self.youtube_service)
            self.task_queue.put(('FETCH_COMPLETE', self.shorts_data))
        else:
            print("Authentication failed. Please check console.")
            self.task_queue.put(('FETCH_FAILED', None))

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def deselect_all(self):
        self.tree.selection_remove(self.tree.get_children())

    def clear_log(self):
        self.log_text.delete('1.0', tk.END)

    def start_local_upload_flow(self):
        """Initiates the workflow for uploading local video files."""
        file_paths = filedialog.askopenfilenames(
            title="Select Video Files to Upload",
            filetypes=[("Video Files", "*.mp4 *.mov *.avi *.mkv"), ("All files", "*.*")]
        )
        if not file_paths:
            print("No files selected.")
            return

        # Dialog to edit metadata
        meta_dialog = LocalUploadDialog(self, file_paths)
        metadata_list = meta_dialog.result
        if not metadata_list:
            print("Local upload cancelled.")
            return

        # Dialog to set schedule
        schedule_dialog = SchedulingDialog(self, self.preset_manager)
        schedule_plan = schedule_dialog.result
        if not schedule_plan:
            print("Scheduling cancelled by user.")
            return

        # Create upload jobs
        upload_jobs = []
        for i, path in enumerate(file_paths):
            upload_jobs.append({'type': 'local', 'source_path': path, **metadata_list[i]})

        self.set_controls_state(tk.DISABLED)
        threading.Thread(target=self.worker_upload_videos, args=(upload_jobs, schedule_plan), daemon=True).start()

    def start_upload_thread(self):
        selected_items = self.tree.selection()
        if not selected_items:
            print("No videos selected for upload.")
            return
        
        selected_shorts_to_upload = []
        for item_iid in selected_items:
            item_values = self.tree.item(item_iid, 'values')
            video_id = item_values[3] # ID is now the 4th column
            short_obj = next((s for s in self.shorts_data if s['id'] == video_id), None)
            if short_obj:
                selected_shorts_to_upload.append(short_obj)

        # Dialog to edit metadata for re-uploads
        reupload_dialog = ReUploadDialog(self, selected_shorts_to_upload)
        edited_metadata = reupload_dialog.result
        if not edited_metadata:
            print("Re-upload editing cancelled.")
            return

        # Dialog to set schedule
        schedule_dialog = SchedulingDialog(self, self.preset_manager)
        schedule_plan = schedule_dialog.result
        if not schedule_plan:
            print("Scheduling cancelled by user.")
            return

        # Create upload jobs from the edited metadata
        upload_jobs = []
        for metadata in edited_metadata:
            upload_jobs.append({
                'type': 're-upload',
                'source_id': metadata['source_id'],
                'title': metadata['title'],
                'description': metadata['description']
            })

        self.set_controls_state(tk.DISABLED)
        threading.Thread(target=self.worker_upload_videos, args=(upload_jobs, schedule_plan), daemon=True).start()

    def _process_reupload_job(self, job, status_update_callback):
        """Handles the download part of a re-upload job."""
        status_update_callback(f'Downloading: {job["title"][:30]}...')
        return download_video(job['source_id'])

    def _process_local_upload_job(self, job, status_update_callback):
        """Handles the source path for a local upload job."""
        status_update_callback(f'Preparing: {job["title"][:30]}...')
        return job['source_path']

    def worker_upload_videos(self, upload_jobs, schedule_plan):
        """Processes a list of upload jobs (re-uploads or local files) in a worker thread."""
        successful_uploads = []
        total_videos = len(upload_jobs)
        self.task_queue.put(('STATUS_UPDATE', f'Starting upload of {total_videos} videos...'))
        self.task_queue.put(('PROGRESS_UPDATE', 0))

        print(f"\n--- Preparing to process {len(upload_jobs)} video(s). ---")
        for i, job in enumerate(upload_jobs):
            job_title = job['title']
            print(f"\n--- Processing Video {i + 1}/{total_videos}: '{job_title}' ---")
            video_path = None  # Initialize to ensure it exists for the 'finally' block

            try:
                status_callback = lambda msg: self.task_queue.put(('STATUS_UPDATE', msg))

                if job['type'] == 're-upload':
                    video_path = self._process_reupload_job(job, status_callback)
                else:  # local upload
                    video_path = self._process_local_upload_job(job, status_callback)

                if not video_path:
                    print(f"Could not find or download video source for '{job_title}'. Skipping.")
                    continue

                status_callback(f'Scheduling video {i+1}/{total_videos}...')
                schedule_datetime = schedule_plan['start_datetime'] + (i * schedule_plan['interval'])
                schedule_iso_string = schedule_datetime.isoformat() + "Z"
                print(f"This video will be scheduled for: {schedule_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

                status_callback(f'Uploading video {i+1}/{total_videos}...')
                upload_id = upload_video(
                    self.youtube_service, video_path, job_title, job['description'],
                    tags=["shorts", "your-custom-tag"], channel_name=self.channel_name,
                    publish_at=schedule_iso_string
                )

                if upload_id:
                    successful_uploads.append({'title': job_title, 'id': upload_id})

            except Exception as e:
                print(f"An unexpected error occurred while processing '{job_title}': {e}")
                traceback.print_exc()  # Print full error to the log for debugging

            finally:
                # This block is guaranteed to run, ensuring cleanup happens.
                # We only delete files that were downloaded for a 're-upload' job.
                if job['type'] == 're-upload' and video_path and os.path.exists(video_path):
                    try:
                        os.remove(video_path)
                        print(f"Cleaned up downloaded file: {video_path}")
                    except OSError as e:
                        print(f"Error: Failed to clean up file {video_path}. Reason: {e}")

            # Update progress
            progress = ((i + 1) / total_videos) * 100
            self.task_queue.put(('PROGRESS_UPDATE', progress))
        
        # After all jobs are processed, send a summary notification to Discord
        if successful_uploads:
            message_lines = [
                "✅ **Upload Batch Complete!**",
                "\nSuccessfully scheduled the following videos:\n"
            ]
            for video in successful_uploads:
                message_lines.append(f"- [{video['title']}](https://www.youtube.com/watch?v={video['id']})")
            send_discord_notification("\n".join(message_lines))

        self.task_queue.put(('UPLOAD_COMPLETE', None))

    def process_queue(self):
        """Checks the task queue for messages from worker threads and updates the GUI."""
        try:
            message_type, data = self.task_queue.get_nowait()

            if message_type == 'FETCH_COMPLETE':
                self.set_controls_state(tk.NORMAL)
                if data:
                    for i, short in enumerate(data):
                        # Format the datetime string for display
                        published_dt = datetime.datetime.fromisoformat(short['published'].replace('Z', '+00:00'))
                        published_str = published_dt.strftime('%Y-%m-%d %H:%M')
                        self.tree.insert('', tk.END, values=(i + 1, short['title'], published_str, short['id']))
                    self.status_var.set(f"Fetch complete. Found {len(data)} Shorts. Ready.")
                    print("\nFetch complete. Select videos from the list and click 'Re-upload Selected'.")
                else:
                    self.status_var.set("No Shorts found or an error occurred.")
                    print("\nNo Shorts found on your channel or an error occurred.")

            elif message_type == 'FETCH_FAILED':
                self.set_controls_state(tk.NORMAL)
                self.status_var.set("Authentication failed. Check log.")

            elif message_type == 'UPLOAD_COMPLETE':
                print("\n--- All selected videos have been processed. ---")
                self.set_controls_state(tk.NORMAL)
                self.status_var.set("All tasks complete. Ready.")

            elif message_type == 'STATUS_UPDATE':
                self.status_var.set(data)

            elif message_type == 'PROGRESS_UPDATE':
                self.progress_var.set(data)

        except queue.Empty:
            pass
        finally:
            self.after(100, self.process_queue)


if __name__ == '__main__':
    app = YouTubeUploaderGUI()
    app.mainloop()
