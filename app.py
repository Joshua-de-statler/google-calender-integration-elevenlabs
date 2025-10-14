import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from dateutil.parser import parse
from dateutil import tz  # <-- Make sure this is imported
from datetime import datetime, timedelta
import google.oauth2.service_account
from googleapiclient.discovery import build
import pytz

# Load environment variables for local testing
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

# --- Google API Setup ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
credentials = google.oauth2.service_account.Credentials.from_service_account_info(
    GOOGLE_CREDENTIALS_DICT, scopes=SCOPES
)
service = build('calendar', 'v3', credentials=credentials)
utc = pytz.UTC

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
        now = datetime.utcnow().replace(tzinfo=utc)

        # --- Scenario 1: User requested a specific time ---
        if requested_start_str:
            try:
                # ✅ FIX: This line now assumes any time provided is in SAST (UTC+2)
                # It will correctly convert the user's local time to UTC for the server.
                sast_tz = tz.gettz('Africa/Johannesburg')
                requested_start = parse(requested_start_str, default=datetime.now(sast_tz)).astimezone(utc)

            except (ValueError, TypeError):
                return jsonify({"error": "Invalid date format. Please state the date and time again."}), 400
            
            requested_end = requested_start + timedelta(minutes=60)

            if requested_start < now:
                return jsonify({"status": "unavailable", "message": "Sorry, that time is in the past."})
            
            # Business hours (9 AM to 5 PM SAST is 7:00 to 17:00 UTC)
            if not (7 <= requested_start.hour and requested_end.hour <= 17):
                return jsonify({"status": "unavailable", "message": "Apologies, that's outside our business hours of 9 AM to 5 PM."})

            events_result = service.events().list(
                calendarId=GOOGLE_CALENDAR_ID, timeMin=requested_start.isoformat(),
                timeMax=requested_end.isoformat(), singleEvents=True).execute()
            
            if not events_result.get('items', []):
                return jsonify({"status": "available", "iso_8601": requested_start.isoformat()})
            else:
                # Fall through to find other slots if the requested one is busy
                pass
        
        # --- Scenario 2: Find next available slots ---
        search_start_time = now + timedelta(minutes=15)
        end_of_search_window = now + timedelta(days=14)
        
        all_busy_slots_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID, timeMin=now.isoformat(),
            timeMax=end_of_search_window.isoformat(), singleEvents=True, orderBy='startTime').execute()
        all_busy_slots = all_busy_slots_result.get('items', [])

        next_available_slots = []
        check_time = search_start_time
        
        while len(next_available_slots) < 5 and check_time < end_of_search_window:
            potential_end_time = check_time + timedelta(minutes=60)
            if 7 <= check_time.hour and potential_end_time.hour < 17:
                is_free = True
                for event in all_busy_slots:
                    event_start = parse(event['start'].get('dateTime'))
                    event_end = parse(event['end'].get('dateTime'))
                    if check_time < event_end and potential_end_time > event_start:
                        is_free = False
                        break
                if is_free:
                    next_available_slots.append(check_time.isoformat())
            check_time += timedelta(minutes=15)

        if not next_available_slots:
            return jsonify({"status": "unavailable", "message": "Sorry, I couldn't find any open 1-hour slots in the next two weeks."})

        formatted_suggestions = []
        for slot_iso in next_available_slots:
            dt = parse(slot_iso)
            human_readable = dt.strftime('%A, %B %d at %I:%M %p')
            formatted_suggestions.append({"human_readable": human_readable, "iso_8601": slot_iso})
            
        if requested_start_str:
             return jsonify({
                "status": "unavailable",
                "message": "Unfortunately, that time is not available. Some other times that work are:",
                "next_available_slots": formatted_suggestions
            })
        else:
             return jsonify({
                "status": "available_slots_found",
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