# google_calendar_tool.py
import os
import json
import base64
import datetime
from dateutil.parser import parse
import pytz
import google.oauth2.service_account
from googleapiclient.discovery import build

# --- Configuration ---
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_STR = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not all([GOOGLE_CALENDAR_ID, GOOGLE_CREDENTIALS_STR]):
    raise RuntimeError("Google Calendar environment variables must be set.")

try:
    if GOOGLE_CREDENTIALS_STR.startswith('{'):
        GOOGLE_CREDENTIALS_DICT = json.loads(GOOGLE_CREDENTIALS_STR)
    else:
        decoded_creds_str = base64.b64decode(GOOGLE_CREDENTIALS_STR).decode('utf-8')
        GOOGLE_CREDENTIALS_DICT = json.loads(decoded_creds_str)
except Exception as e:
    raise ValueError(f"Failed to decode GOOGLE_CREDENTIALS_JSON. Error: {e}")

# --- API Client Setup ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
credentials = google.oauth2.service_account.Credentials.from_service_account_info(
    GOOGLE_CREDENTIALS_DICT, scopes=SCOPES
)
google_service = build('calendar', 'v3', credentials=credentials)
sast_tz = pytz.timezone("Africa/Johannesburg")

def create_calendar_event(start_time: str, summary: str, description: str, attendees: list[str]) -> dict:
    """Creates a new event with an explicit timezone, adapted for direct booking."""
    parsed_start_time = parse(start_time)

    if parsed_start_time.tzinfo is not None:
        start = parsed_start_time.astimezone(sast_tz)
    else:
        start = sast_tz.localize(parsed_start_time)

    now_sast = datetime.datetime.now(sast_tz)
    if start < now_sast:
        raise ValueError("Cannot book an appointment in the past.")
    if start.date() == now_sast.date():
        raise ValueError("Cannot book a same-day appointment. Please book for the next business day or later.")

    end = start + datetime.timedelta(minutes=60)

    full_description = description
    lead_email = attendees[0] if attendees else 'N/A'
    if lead_email != 'N/A':
        full_description += f"\n\n---\nLead Contact: {lead_email}"

    event = {
        'summary': summary,
        'description': full_description,
        'start': {'dateTime': start.isoformat(), 'timeZone': 'Africa/Johannesburg'},
        'end': {'dateTime': end.isoformat(), 'timeZone': 'Africa/Johannesburg'},
        'attendees': [{'email': email} for email in attendees],
        'reminders': {'useDefault': False, 'overrides': [{'method': 'email', 'minutes': 24 * 60}, {'method': 'popup', 'minutes': 10}]},
    }
    created_event = google_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created_event