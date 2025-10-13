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

# --- Health Check Endpoint ---
@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

# --- Main API Endpoints ---
@app.route('/get-availability', methods=['POST'])
def get_availability():
    try:
        data = request.json or {}
        # The agent can optionally pass a specific date to check
        requested_date_str = data.get('date')

        utc = pytz.UTC
        now = datetime.utcnow().replace(tzinfo=utc)
        
        # Determine the search window
        if requested_date_str:
            try:
                # Search a specific day requested by the user
                start_of_day = parse(requested_date_str).replace(hour=0, minute=0, second=0, tzinfo=utc)
                # If the user asks for a day in the past, search today instead
                if start_of_day < now.replace(hour=0, minute=0, second=0, microsecond=0):
                    start_of_search = now + timedelta(minutes=5)
                    end_of_search = start_of_search.replace(hour=23, minute=59, second=59)
                else:
                    start_of_search = start_of_day
                    end_of_search = start_of_day.replace(hour=23, minute=59, second=59)
            except (ValueError, TypeError):
                # If date parsing fails, default to the next 7 days
                start_of_search = now + timedelta(minutes=5)
                end_of_search = now + timedelta(days=7)
        else:
            # Default to searching the next 7 days
            start_of_search = now + timedelta(minutes=5)
            end_of_search = now + timedelta(days=7)

        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start_of_search.isoformat(),
            timeMax=end_of_search.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        busy_slots = events_result.get('items', [])

        free_slots = []
        session_duration = timedelta(minutes=60)
        
        # Start checking for slots from the beginning of our search window
        check_time = start_of_search.replace(tzinfo=utc)
        
        while len(free_slots) < 5 and check_time < end_of_search:
            potential_end_time = check_time + session_duration

            # Define business hours (9 AM to 5 PM SAST is 7:00 to 17:00 UTC)
            # and ensure the full 1-hour slot fits within these hours.
            if 7 <= check_time.hour and potential_end_time.hour < 17:
                is_free = True
                # Check for overlaps with any existing busy events
                for event in busy_slots:
                    event_start = parse(event['start'].get('dateTime'))
                    event_end = parse(event['end'].get('dateTime'))
                    # Overlap condition: (StartA < EndB) and (EndA > StartB)
                    if check_time < event_end and potential_end_time > event_start:
                        is_free = False
                        break  # This slot is busy, move to the next check
                
                if is_free:
                    free_slots.append(check_time.isoformat())

            # Move to the next 15-minute increment to check for the next potential slot
            check_time += timedelta(minutes=15)

        if not free_slots:
            return jsonify({"message": f"Sorry, no 1-hour slots are available for the requested period."})

        # Format the found slots for the agent to read out
        formatted_times = []
        for slot_iso in free_slots:
            dt = parse(slot_iso)
            human_readable = dt.strftime('%A, %B %d at %I:%M %p')
            formatted_times.append({
                "human_readable": human_readable,
                "iso_8601": slot_iso
            })
        return jsonify({"available_slots": formatted_times})

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
        # ✅ **MODIFICATION:** Duration is now 60 minutes.
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