import os
import json
import base64
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from dateutil.parser import parse
from datetime import datetime, timedelta
import google.oauth2.service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError  # ✅ --- IMPORT ADDED
import pytz

# Load environment variables
load_dotenv()

app = Flask(__name__)

# --- Configuration ---
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_STR = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not GOOGLE_CALENDAR_ID or not GOOGLE_CREDENTIALS_STR:
    raise RuntimeError("GOOGLE_CALENDAR_ID and GOOGLE_CREDENTIALS_JSON must be set.")

try:
    # This handles both raw JSON and Base64 encoded JSON for credentials
    if GOOGLE_CREDENTIALS_STR.startswith('{'):
        GOOGLE_CREDENTIALS_DICT = json.loads(GOOGLE_CREDENTIALS_STR)
    else:
        decoded_creds_str = base64.b64decode(GOOGLE_CREDENTIALS_STR).decode('utf-8')
        GOOGLE_CREDENTIALS_DICT = json.loads(decoded_creds_str)
except Exception as e:
    raise ValueError(f"Failed to decode GOOGLE_CREDENTIALS_JSON. Error: {e}")

# --- Google API & Timezone Setup ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
credentials = google.oauth2.service_account.Credentials.from_service_account_info(
    GOOGLE_CREDENTIALS_DICT, scopes=SCOPES
)
service = build('calendar', 'v3', credentials=credentials)
UTC = pytz.utc
SAST = pytz.timezone('Africa/Johannesburg')

# --- Health Check Endpoint ---
@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

# --- API Endpoints ---
@app.route('/get-availability', methods=['POST'])
def get_availability():
    # This function remains the same
    try:
        data = request.json or {}
        requested_start_str = data.get('start_time')
        now_sast = datetime.now(SAST)
        if requested_start_str:
            try:
                naive_dt = parse(requested_start_str)
                requested_start_sast = SAST.localize(naive_dt)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid date format."}), 400
            if requested_start_sast < now_sast:
                return jsonify({"status": "unavailable", "message": "Sorry, that time is in the past."})
            if not (0 <= requested_start_sast.weekday() <= 4 and 8 <= requested_start_sast.hour < 16):
                 return jsonify({"status": "unavailable", "message": "Apologies, that's outside our business hours of Monday to Friday, 8 AM to 4 PM."})
            requested_start_utc = requested_start_sast.astimezone(UTC)
            requested_end_utc = requested_start_utc + timedelta(minutes=60)
            events_result = service.events().list(
                calendarId=GOOGLE_CALENDAR_ID, timeMin=requested_start_utc.isoformat(),
                timeMax=requested_end_utc.isoformat(), singleEvents=True).execute()
            if not events_result.get('items', []):
                return jsonify({"status": "available", "iso_8601": requested_start_utc.isoformat()})
            else:
                pass
        now_utc = now_sast.astimezone(UTC)
        search_start_time = now_utc + timedelta(minutes=15)
        end_of_search_window = now_utc + timedelta(days=14)
        all_busy_slots_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID, timeMin=now_utc.isoformat(),
            timeMax=end_of_search_window.isoformat(), singleEvents=True, orderBy='startTime').execute()
        all_busy_slots = all_busy_slots_result.get('items', [])
        next_available_slots = []
        check_time_utc = search_start_time
        while len(next_available_slots) < 5 and check_time_utc < end_of_search_window:
            potential_end_time_utc = check_time_utc + timedelta(minutes=60)
            check_time_sast = check_time_utc.astimezone(SAST)
            if (0 <= check_time_sast.weekday() <= 4 and 8 <= check_time_sast.hour < 16):
                is_free = True
                for event in all_busy_slots:
                    event_start = parse(event['start'].get('dateTime'))
                    event_end = parse(event['end'].get('dateTime'))
                    if check_time_utc < event_end and potential_end_time_utc > event_start:
                        is_free = False
                        break
                if is_free:
                    next_available_slots.append(check_time_utc.isoformat())
            check_time_utc += timedelta(minutes=15)
        if not next_available_slots:
            return jsonify({"status": "unavailable", "message": "Sorry, I couldn't find any open 1-hour slots."})
        formatted_suggestions = []
        for slot_iso in next_available_slots:
            dt_utc = parse(slot_iso)
            dt_sast = dt_utc.astimezone(SAST)
            human_readable = dt_sast.strftime('%A, %B %d at %-I:%M %p')
            formatted_suggestions.append({"human_readable": human_readable, "iso_8601": slot_iso})
        message = "Unfortunately, that time is not available. However, some other times that work are:" if requested_start_str else "Sure, here are some upcoming available times:"
        return jsonify({"status": "available_slots_found", "message": message, "next_available_slots": formatted_suggestions})
    except Exception as e:
        print(f"A general error occurred in /get-availability: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

@app.route('/find-appointment', methods=['POST'])
def find_appointment():
    # This function remains the same
    try:
        data = request.json
        if not data or 'email' not in data:
            return jsonify({"error": "An email address must be provided."}), 400
        email_to_find = data['email'].lower()
        now_utc = datetime.now(UTC)
        end_of_search = now_utc + timedelta(days=30)
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID, timeMin=now_utc.isoformat(),
            timeMax=end_of_search.isoformat(), singleEvents=True, orderBy='startTime').execute()
        found_events = []
        for event in events_result.get('items', []):
            description = event.get('description', '')
            if email_to_find in description.lower():
                dt_utc = parse(event['start'].get('dateTime'))
                dt_sast = dt_utc.astimezone(SAST)
                found_events.append({
                    "event_id": event['id'],
                    "summary": event['summary'],
                    "human_readable_time": dt_sast.strftime('%A, %B %d at %-I:%M %p'),
                })
        if not found_events:
            return jsonify({"message": f"I'm sorry, I couldn't find any upcoming appointments for {email_to_find}."})
        return jsonify({"found_events": found_events})
    except Exception as e:
        print(f"A general error occurred in /find-appointment: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

@app.route('/cancel-appointment', methods=['POST'])
def cancel_appointment():
    # ✅ --- MODIFICATION: ADDED SPECIFIC ERROR HANDLING ---
    try:
        data = request.json
        if not data or 'event_id' not in data:
            return jsonify({"error": "An event_id must be provided."}), 400
        
        event_id_to_cancel = data['event_id']

        service.events().delete(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id_to_cancel
        ).execute()

        return jsonify({"message": "The old appointment has been successfully cancelled."})

    except HttpError as e:
        # This specifically catches errors from the Google API.
        # If the event ID doesn't exist, Google returns a 404 error.
        if e.resp.status == 404:
            print(f"Attempted to cancel a non-existent event: {event_id_to_cancel}")
            return jsonify({"error": "It seems that appointment does not exist or may have already been cancelled."}), 404
        else:
            # For other Google API errors (like permissions issues)
            print(f"A Google API error occurred in /cancel-appointment: {e}")
            return jsonify({"error": "An issue occurred with the Google Calendar API."}), 500
    except Exception as e:
        # This catches any other unexpected errors in the code.
        print(f"A general error occurred in /cancel-appointment: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500


@app.route('/book-appointment', methods=['POST'])
def book_appointment():
    # This function remains the same
    try:
        data = request.json
        if not all(k in data for k in ["name", "email", "start_time"]):
            return jsonify({"error": "Missing name, email, or start_time."}), 400
        name_parts = data["name"].strip().split(' ', 1)
        first_name = name_parts[0]
        start_time_dt = parse(data["start_time"])
        end_time_dt = start_time_dt + timedelta(minutes=60)
        event = {
            'summary': f'Onboarding Call with {data["name"]}',
            'location': 'Video Call - Link to follow',
            'description': f'A 60-minute onboarding call for {data["name"]}. Invitee email: {data["email"]}',
            'start': {'dateTime': start_time_dt.isoformat(), 'timeZone': 'UTC'},
            'end': {'dateTime': end_time_dt.isoformat(), 'timeZone': 'UTC'},
            'reminders': {'useDefault': True},
        }
        created_event = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event
        ).execute()
        success_message = (
            f"Perfect, {first_name}! I have successfully reserved that 1-hour time slot on our calendar. "
            f"Our team will send a manual calendar invitation to {data['email']} shortly to confirm."
        )
        return jsonify({"message": success_message}), 201
    except Exception as e:
        print(f"A general error occurred in /book-appointment: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 8080)))