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
import pytz
from supabase import create_client, Client # ✅ --- IMPORT ADDED ---

# Load environment variables
load_dotenv()

app = Flask(__name__)

# --- Configuration ---
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_STR = os.getenv("GOOGLE_CREDENTIALS_JSON")
# ✅ --- SUPABASE CONFIG ADDED ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([GOOGLE_CALENDAR_ID, GOOGLE_CREDENTIALS_STR, SUPABASE_URL, SUPABASE_KEY]):
    raise RuntimeError("All environment variables must be set.")

try:
    # This handles both raw JSON for local testing and Base64 for production
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
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) # ✅ --- SUPABASE CLIENT ADDED ---
SAST = pytz.timezone('Africa/Johannesburg')

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

            requested_start_utc = requested_start_sast.astimezone(pytz.utc)
            requested_end_utc = requested_start_utc + timedelta(minutes=60)

            events_result = google_service.events().list(
                calendarId=GOOGLE_CALENDAR_ID, timeMin=requested_start_utc.isoformat(),
                timeMax=requested_end_utc.isoformat(), singleEvents=True).execute()
            
            if not events_result.get('items', []):
                return jsonify({"status": "available", "iso_8601": requested_start_utc.isoformat()})
            else:
                pass

        now_utc = now_sast.astimezone(pytz.utc)
        search_start_time = now_utc + timedelta(minutes=15)
        end_of_search_window = now_utc + timedelta(days=14)
        
        all_busy_slots_result = google_service.events().list(
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

        created_event = google_service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event
        ).execute()
        
        # ✅ --- SAVE TO SUPABASE ---
        try:
            supabase.table("meetings").insert({
                "full_name": data["name"],
                "email": data["email"],
                "start_time": data["start_time"],
                "google_calendar_event_id": created_event.get('id')
            }).execute()
            print("Successfully saved lead to Supabase.")
        except Exception as e:
            # If Supabase fails, we don't want to crash the whole request.
            # We'll just log the error and continue.
            print(f"Error saving lead to Supabase: {e}")
        # --- END SAVE TO SUPABASE ---

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