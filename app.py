import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from dateutil.parser import parse
from datetime import datetime, timedelta
import google.oauth2.service_account
from googleapiclient.discovery import build
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
    GOOGLE_CREDENTIALS_DICT = json.loads(GOOGLE_CREDENTIALS_STR)
except json.JSONDecodeError:
    raise ValueError("GOOGLE_CREDENTIALS_JSON is not a valid JSON string.")

# --- Google API & UTC Timezone Setup ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
credentials = google.oauth2.service_account.Credentials.from_service_account_info(
    GOOGLE_CREDENTIALS_DICT, scopes=SCOPES
)
service = build('calendar', 'v3', credentials=credentials)
UTC = pytz.utc

# --- Health Check Endpoint ---
@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

# --- Main API Endpoints ---
@app.route('/get-availability', methods=['POST'])
def get_availability():
    try:
        data = request.json or {}
        requested_start_str = data.get('start_time')
        
        # Get the current time in UTC
        now_utc = datetime.now(UTC)

        # --- Scenario 1: User requested a specific time ---
        if requested_start_str:
            try:
                # Parse the incoming time, assuming it is already in UTC
                requested_start_utc = parse(requested_start_str).astimezone(UTC)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid date format. Please state the date and time again."}), 400
            
            # This comparison is now a simple and reliable UTC vs UTC check
            if requested_start_utc < now_utc:
                return jsonify({"status": "unavailable", "message": "Sorry, that time is in the past. Please suggest a future time."})
            
            # Check business hours (Mon-Fri, 8 AM-4 PM SAST, which is 6 AM-2 PM UTC)
            # 0=Monday, 4=Friday
            if not (0 <= requested_start_utc.weekday() <= 4 and 6 <= requested_start_utc.hour < 14):
                 return jsonify({"status": "unavailable", "message": "Apologies, that's outside our business hours of Monday to Friday, 8 AM to 4 PM South Africa time."})

            requested_end_utc = requested_start_utc + timedelta(minutes=60)

            # Check for conflicting events
            events_result = service.events().list(
                calendarId=GOOGLE_CALENDAR_ID, timeMin=requested_start_utc.isoformat(),
                timeMax=requested_end_utc.isoformat(), singleEvents=True).execute()
            
            if not events_result.get('items', []):
                return jsonify({"status": "available", "iso_8601": requested_start_utc.isoformat()})
            else:
                pass # Fall through to find other slots

        # --- Scenario 2: Find the next available slots ---
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
            
            # Enforce business hours in UTC (6 AM to 2 PM)
            if (0 <= check_time_utc.weekday() <= 4 and 6 <= check_time_utc.hour < 14):
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
            return jsonify({"status": "unavailable", "message": "Sorry, I couldn't find any open 1-hour slots in the next two weeks."})

        formatted_suggestions = []
        for slot_iso in next_available_slots:
            dt_utc = parse(slot_iso)
            # Convert back to SAST for the user-facing message
            dt_sast = dt_utc.astimezone(pytz.timezone('Africa/Johannesburg'))
            human_readable = dt_sast.strftime('%A, %B %d at %-I:%M %p')
            formatted_suggestions.append({"human_readable": human_readable, "iso_8601": slot_iso})
            
        message = "Unfortunately, that specific time is not available. However, some other times that work are:" if requested_start_str else "Sure, here are some upcoming available times:"
        return jsonify({
            "status": "available_slots_found",
            "message": message,
            "next_available_slots": formatted_suggestions
        })

    except Exception as e:
        print(f"A general error occurred in /get-availability: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

@app.route('/book-appointment', methods=['POST'])
def book_appointment():
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