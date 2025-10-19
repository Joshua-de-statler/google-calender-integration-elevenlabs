# app.py

import os
import json
import base64
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from functools import wraps  # <-- 1. IMPORT WRAPS

import google.oauth2.service_account
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, EmailStr, Field, ValidationError
from supabase import Client, create_client
from dateutil.parser import parse

# --- Configuration ---
load_dotenv()

class Config:
    """Application configuration."""
    GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
    GOOGLE_CREDENTIALS_STR = os.getenv("GOOGLE_CREDENTIALS_JSON")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    API_KEY = os.getenv("API_KEY")  # <-- 2. ADD API_KEY TO CONFIG
    
    TIMEZONE = ZoneInfo('Africa/Johannesburg')
    BUSINESS_HOURS_START = 8
    BUSINESS_HOURS_END = 16
    APPOINTMENT_DURATION_MINUTES = 60
    SEARCH_WINDOW_DAYS = 14
    SUGGESTION_COUNT = 5

    @staticmethod
    def setup_logging():
        """Sets up basic logging for the application."""
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s %(levelname)s: %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

    @staticmethod
    def get_google_credentials():
        """Decodes and returns Google credentials."""
        if not Config.GOOGLE_CREDENTIALS_STR:
            raise ValueError("GOOGLE_CREDENTIALS_JSON environment variable not set.")
        try:
            if Config.GOOGLE_CREDENTIALS_STR.startswith('{'):
                return json.loads(Config.GOOGLE_CREDENTIALS_STR)
            decoded_creds = base64.b64decode(Config.GOOGLE_CREDENTIALS_STR).decode('utf-8')
            return json.loads(decoded_creds)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise ValueError(f"Failed to decode GOOGLE_CREDENTIALS_JSON. Error: {e}")

# --- Pydantic Models for Input Validation ---
class AvailabilityRequest(BaseModel):
    start_time: Optional[str] = None # Corrected for Python 3.9

class BookingRequest(BaseModel):
    name: str
    email: EmailStr
    start_time: str
    goal: str = "Not provided"
    monthly_budget: int = 0
    company_name: str = "Not provided"

# --- Service Abstractions ---
# (GoogleCalendarService and SupabaseService classes remain unchanged)
# ... [No changes to GoogleCalendarService or SupabaseService] ...
class GoogleCalendarService:
    """Handles interactions with the Google Calendar API."""
    SCOPES = ['https://www.googleapis.com/auth/calendar']

    def __init__(self, credentials_info, calendar_id):
        self.credentials = google.oauth2.service_account.Credentials.from_service_account_info(
            credentials_info, scopes=self.SCOPES
        )
        self.service = build('calendar', 'v3', credentials=self.credentials)
        self.calendar_id = calendar_id

    def get_events(self, time_min, time_max):
        """Fetches events from the calendar within a given time range."""
        try:
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            return events_result.get('items', [])
        except HttpError as e:
            logging.error(f"Google Calendar API error: {e}")
            raise

    def create_event(self, summary, description, start_time, end_time):
        """Creates a new event on the calendar."""
        event = {
            'summary': summary,
            'location': 'Video Call - Link to follow',
            'description': description,
            'start': {'dateTime': start_time.isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': end_time.isoformat(), 'timeZone': 'UTC'},
            'reminders': {'useDefault': True},
        }
        try:
            created_event = self.service.events().insert(
                calendarId=self.calendar_id, body=event
            ).execute()
            return created_event
        except HttpError as e:
            logging.error(f"Failed to create Google Calendar event: {e}")
            raise

class SupabaseService:
    """Handles interactions with the Supabase database."""
    def __init__(self, url, key):
        self.supabase: Client = create_client(url, key)

    def save_meeting(self, meeting_data):
        """Saves meeting details to the 'meetings' table."""
        try:
            self.supabase.table("meetings").insert(meeting_data).execute()
            logging.info("Successfully saved lead to Supabase.")
        except Exception as e:
            logging.error(f"Error saving lead to Supabase: {e}")
            # We don't re-raise here as failing to save to Supabase
            # shouldn't prevent the user from getting a success message.


# --- Flask App Initialization ---
app = Flask(__name__)
Config.setup_logging()

# Check for API_KEY on startup
if not Config.API_KEY:
    logging.critical("API_KEY environment variable not set. Application will not run.")
    # You could raise an error here to stop the app from starting
    # raise RuntimeError("API_KEY environment variable not set.")

try:
    google_creds = Config.get_google_credentials()
    calendar_service = GoogleCalendarService(google_creds, Config.GOOGLE_CALENDAR_ID)
    supabase_service = SupabaseService(Config.SUPABASE_URL, Config.SUPABASE_KEY)
except (RuntimeError, ValueError) as e:
    logging.critical(f"Startup configuration error: {e}")

# --- 3. API KEY DECORATOR ---
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check header 'X-API-Key'
        api_key = request.headers.get('X-API-Key')
        if not api_key:
            # Fallback to checking 'Authorization' header (e.g., "Bearer YOUR_KEY")
            auth_header = request.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                api_key = auth_header.split(' ')[1]

        if api_key and api_key == Config.API_KEY:
            return f(*args, **kwargs)
        else:
            logging.warning("Unauthorized API access attempt.")
            return jsonify({"error": "Unauthorized"}), 401
    return decorated_function

# --- API Endpoints ---
@app.route('/', methods=['GET'])
def health_check():
    # This endpoint is not protected, so you can ping it
    # to check if the service is alive.
    return jsonify({"status": "healthy"}), 200

@app.route('/get-availability', methods=['POST'])
@require_api_key  # <-- 4. APPLY THE DECORATOR
def get_availability():
    try:
        data = AvailabilityRequest.model_validate(request.json or {})
    except ValidationError as e:
        return jsonify({"error": "Invalid input.", "details": e.errors()}), 400

    now_sast = datetime.now(Config.TIMEZONE)

    if data.start_time:
        try:
            naive_dt = parse(data.start_time)
            requested_start_sast = Config.TIMEZONE.localize(naive_dt)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid date format."}), 400

        if requested_start_sast < now_sast:
            return jsonify({"status": "unavailable", "message": "Sorry, that time is in the past."})

        if not (0 <= requested_start_sast.weekday() <= 4 and
                Config.BUSINESS_HOURS_START <= requested_start_sast.hour < Config.BUSINESS_HOURS_END):
            return jsonify({"status": "unavailable", "message": "Apologies, that's outside our business hours of Monday to Friday, 8 AM to 4 PM."})

        requested_start_utc = requested_start_sast.astimezone(ZoneInfo("UTC"))
        requested_end_utc = requested_start_utc + timedelta(minutes=Config.APPOINTMENT_DURATION_MINUTES)
        
        events = calendar_service.get_events(requested_start_utc, requested_end_utc)
        if not events:
            return jsonify({"status": "available", "iso_8601": requested_start_utc.isoformat()})

    # --- Find next available slots if requested time is unavailable or not provided ---
    now_utc = now_sast.astimezone(ZoneInfo("UTC"))
    search_start_time = now_utc + timedelta(minutes=15)
    end_of_search_window = now_utc + timedelta(days=Config.SEARCH_WINDOW_DAYS)

    all_busy_slots = calendar_service.get_events(now_utc, end_of_search_window)

    next_available_slots = []
    check_time_utc = search_start_time

    while len(next_available_slots) < Config.SUGGESTION_COUNT and check_time_utc < end_of_search_window:
        potential_end_time_utc = check_time_utc + timedelta(minutes=Config.APPOINTMENT_DURATION_MINUTES)
        check_time_sast = check_time_utc.astimezone(Config.TIMEZONE)

        if (0 <= check_time_sast.weekday() <= 4 and
                Config.BUSINESS_HOURS_START <= check_time_sast.hour < Config.BUSINESS_HOURS_END):
            is_free = True
            for event in all_busy_slots:
                event_start = parse(event['start'].get('dateTime'))
                event_end = parse(event['end'].get('dateTime'))
                if check_time_utc < event_end and potential_end_time_utc > event_start:
                    is_free = False
                    break
            if is_free:
                next_available_slots.append(check_time_utc)
        
        check_time_utc += timedelta(minutes=15)

    if not next_available_slots:
        return jsonify({"status": "unavailable", "message": "Sorry, I couldn't find any open 1-hour slots in the next two weeks."})

    formatted_suggestions = []
    for slot_utc in next_available_slots:
        dt_sast = slot_utc.astimezone(Config.TIMEZONE)
        human_readable = dt_sast.strftime('%A, %B %d at %-I:%M %p')
        formatted_suggestions.append({"human_readable": human_readable, "iso_8601": slot_utc.isoformat()})
        
    message = "Unfortunately, that time is not available. However, some other times that work are:" if data.start_time else "Sure, here are some upcoming available times:"
    return jsonify({"status": "available_slots_found", "message": message, "next_available_slots": formatted_suggestions})

@app.route('/book-appointment', methods=['POST'])
@require_api_key  # <-- 4. APPLY THE DECORATOR
def book_appointment():
    try:
        data = BookingRequest.model_validate(request.json)
    except ValidationError as e:
        return jsonify({"error": "Missing or invalid required fields.", "details": e.errors()}), 400

    try:
        start_time_dt = parse(data.start_time)
        end_time_dt = start_time_dt + timedelta(minutes=Config.APPOINTMENT_DURATION_MINUTES)
        
        name_parts = data.name.strip().split(' ', 1)
        first_name = name_parts[0]

        summary = f"Onboarding call with {data.name} from {data.company_name} to discuss the 'Project Pipeline AI'."
        description = (
            f"Stated Goal: {data.goal}\n"
            f"Stated Budget: R{data.monthly_budget}/month\n\n"
            f"---\n"
            f"Lead Contact: {data.email}"
        )

        created_event = calendar_service.create_event(summary, description, start_time_dt, end_time_dt)
        
        meeting_data = {
            "full_name": data.name,
            "email": data.email,
            "company_name": data.company_name,
            "start_time": data.start_time,
            "goal": data.goal,
            "monthly_budget": data.monthly_budget,
            "google_calendar_event_id": created_event.get('id')
        }
        supabase_service.save_meeting(meeting_data)

        success_message = (
            f"Perfect, {first_name}! I've successfully booked your 1-hour call. "
            f"Our team will send a calendar invitation to {data.email} shortly to confirm."
        )
        return jsonify({"message": success_message}), 201

    except Exception as e:
        logging.error(f"A general error occurred in /book-appointment: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 8080)))