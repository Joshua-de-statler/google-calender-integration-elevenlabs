# app.py

import os
import json
import base64
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from functools import wraps

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
    API_KEY = os.getenv("API_KEY")
    
    TIMEZONE = ZoneInfo('Africa/Johannesburg')
    BUSINESS_HOURS_START = 8
    BUSINESS_HOURS_END = 16
    APPOINTMENT_DURATION_MINUTES = 60
    SEARCH_WINDOW_DAYS = 14
    SUGGESTION_COUNT = 5

    @staticmethod
    def setup_logging():
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s %(levelname)s: %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

    @staticmethod
    def get_google_credentials():
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
    start_time: Optional[str] = None

class BookingRequest(BaseModel):
    name: str
    email: EmailStr
    start_time: str
    goal: str = "Not provided"
    monthly_budget: int = 0
    company_name: str = "Not provided"
    client_number: Optional[str] = None  # <-- NEW
    call_duration_seconds: Optional[int] = 0 # <-- NEW

# --- Pydantic Model for Call Logging ---
class CallLogRequest(BaseModel):
    full_name: Optional[str] = "Not provided"
    email: Optional[EmailStr] = None
    company_name: Optional[str] = "Not provided"
    goal: Optional[str] = "Not provided"
    monthly_budget: Optional[int] = 0
    resulted_in_meeting: bool = False # This is the key flag
    disqualification_reason: Optional[str] = None
    client_number: Optional[str] = None  # <-- NEW
    call_duration_seconds: Optional[int] = 0 # <-- NEW

# --- Service Abstractions ---
class GoogleCalendarService:
    SCOPES = ['https://www.googleapis.com/auth/calendar']

    def __init__(self, credentials_info, calendar_id):
        self.credentials = google.oauth2.service_account.Credentials.from_service_account_info(
            credentials_info, scopes=self.SCOPES
        )
        self.service = build('calendar', 'v3', credentials=self.credentials)
        self.calendar_id = calendar_id

    def get_events(self, time_min, time_max):
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
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set.")
        self.supabase: Client = create_client(url, key)

    def save_meeting(self, meeting_data):
        """Saves meeting details to the 'meetings' table."""
        try:
            self.supabase.table("meetings").insert(meeting_data).execute()
            logging.info("Successfully saved lead to Supabase 'meetings' table.")
        except Exception as e:
            logging.error(f"Error saving lead to 'meetings' table: {e}")

    def log_call(self, call_data):
        """Saves call details to the 'call_history' table."""
        try:
            self.supabase.table("call_history").insert(call_data).execute()
            logging.info("Successfully logged call to 'call_history' table.")
        except Exception as e:
            logging.error(f"Error logging call to 'call_history' table: {e}")

# --- Flask App Initialization ---
app = Flask(__name__)
Config.setup_logging()

if not Config.API_KEY:
    logging.critical("API_KEY environment variable not set. Application will not run.")

try:
    google_creds = Config.get_google_credentials()
    calendar_service = GoogleCalendarService(google_creds, Config.GOOGLE_CALENDAR_ID)
    supabase_service = SupabaseService(Config.SUPABASE_URL, Config.SUPABASE_KEY)
except (RuntimeError, ValueError) as e:
    logging.critical(f"Startup configuration error: {e}")
    
# --- API Key Decorator ---
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key:
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
    return jsonify({"status": "healthy"}), 200

@app.route('/get-availability', methods=['POST'])
@require_api_key
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
            return jsonify({"status": "available", "iso_8061": requested_start_utc.isoformat()}) # Corrected typo here

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
        formatted_suggestions.append({"human_readable": human_readable, "iso_8061": slot_utc.isoformat()}) # Corrected typo here
        
    message = "Unfortunately, that time is not available. However, some other times that work are:" if data.start_time else "Sure, here are some upcoming available times:"
    return jsonify({"status": "available_slots_found", "message": message, "next_available_slots": formatted_suggestions})


@app.route('/book-appointment', methods=['POST'])
@require_api_key
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
            f"Lead Contact: {data.email}\n"
            f"Lead Phone: {data.client_number}" # <-- NEW
        )

        created_event = calendar_service.create_event(summary, description, start_time_dt, end_time_dt)
        
        # --- 1. Log to 'meetings' table (Original) ---
        meeting_data = {
            "full_name": data.name,
            "email": data.email,
            "company_name": data.company_name,
            "start_time": data.start_time,
            "goal": data.goal,
            "monthly_budget": data.monthly_budget,
            "google_calendar_event_id": created_event.get('id'),
            "client_number": data.client_number  # <-- NEW
        }
        supabase_service.save_meeting(meeting_data)
        
        # --- 2. MODIFICATION: Log to 'call_history' table (New) ---
        call_data = {
            "full_name": data.name,
            "email": data.email,
            "company_name": data.company_name,
            "goal": data.goal,
            "monthly_budget": data.monthly_budget,
            "resulted_in_meeting": True,
            "disqualification_reason": None,
            "client_number": data.client_number,  # <-- NEW
            "call_duration_seconds": data.call_duration_seconds # <-- NEW
        }
        supabase_service.log_call(call_data)

        success_message = (
            f"Perfect, {first_name}! I've successfully booked your 1-hour call. "
            f"Our team will send a calendar invitation to {data.email} shortly to confirm."
        )
        return jsonify({"message": success_message}), 201

    except Exception as e:
        logging.error(f"A general error occurred in /book-appointment: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

# --- NEW: Endpoint for logging failed/disqualified calls ---
@app.route('/log-call', methods=['POST'])
@require_api_key
def log_call_history():
    """
    Logs the details of a call that did NOT result in a meeting.
    To be called by the agent upon disqualification or call termination.
    """
    try:
        # Validate incoming data against the CallLogRequest model
        data = CallLogRequest.model_validate(request.json)
    except ValidationError as e:
        return jsonify({"error": "Invalid input.", "details": e.errors()}), 400
    
    try:
        # Convert Pydantic model to dict for Supabase
        # .model_dump() is the Pydantic v2 equivalent of .dict()
        call_data = data.model_dump(exclude_unset=True) 
        
        # Ensure the key flag is explicitly set, even if default
        call_data["resulted_in_meeting"] = data.resulted_in_meeting 
        
        supabase_service.log_call(call_data)
        return jsonify({"message": "Call log received."}), 201
    except Exception as e:
        logging.error(f"A general error occurred in /log-call: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 8080)))