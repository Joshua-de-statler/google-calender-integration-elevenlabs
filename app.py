# app.py
import os
import json
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from dateutil.parser import parse
from datetime import datetime, timedelta
import pytz
from supabase import create_client, Client

# Local imports from our new tool file
from google_calendar_tool import create_calendar_event, google_service

# Load environment variables
load_dotenv()

app = Flask(__name__)

# --- Configuration ---
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([GOOGLE_CALENDAR_ID, SUPABASE_URL, SUPABASE_KEY]):
    raise RuntimeError("All required environment variables must be set.")

# --- API Client Setup ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
UTC = pytz.utc
SAST = pytz.timezone('Africa/Johannesburg')

# --- Health Check Endpoint ---
@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

# --- Get Availability Endpoint (No changes needed here) ---
@app.route('/get-availability', methods=['POST'])
def get_availability():
    try:
        data = request.json or {}
        requested_start_str = data.get('start_time')
        now_sast = datetime.now(SAST)

        if requested_start_str:
            try:
                parsed_dt = parse(requested_start_str)
                if parsed_dt.tzinfo is None:
                    requested_start_sast = SAST.localize(parsed_dt)
                else:
                    requested_start_sast = parsed_dt.astimezone(SAST)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid date format."}), 400

            if requested_start_sast < now_sast:
                return jsonify({"status": "unavailable", "message": "Sorry, that time is in the past."})
            
            if not (0 <= requested_start_sast.weekday() <= 4 and 8 <= requested_start_sast.hour < 16):
                 return jsonify({"status": "unavailable", "message": "Apologies, that's outside our business hours of Monday to Friday, 8 AM to 4 PM."})

            requested_start_utc = requested_start_sast.astimezone(UTC)
            requested_end_utc = requested_start_utc + timedelta(minutes=60)
            events_result = google_service.events().list(
                calendarId=GOOGLE_CALENDAR_ID, timeMin=requested_start_utc.isoformat(),
                timeMax=requested_end_utc.isoformat(), singleEvents=True).execute()
            
            if not events_result.get('items', []):
                return jsonify({"status": "available", "iso_8601": requested_start_utc.isoformat()})
            else:
                pass

        now_utc = now_sast.astimezone(UTC)
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
                        is_free = False; break
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

# --- MODIFIED BOOKING ENDPOINT ---
@app.route('/book-appointment', methods=['POST'])
def book_appointment():
    try:
        data = request.json
        required_fields = ["name", "email", "start_time", "monthly_budget"]
        if not all(k in data for k in required_fields):
            return jsonify({"error": "Missing required fields."}), 400

        full_name = data["name"]
        email = data["email"]
        company_name = data.get("company_name", "Not provided")
        goal = data.get("goal", "Not provided")
        monthly_budget = float(data["monthly_budget"])
        start_time = data["start_time"]
        
        # Disqualification logic from your system prompt
        if monthly_budget < 8000:
            return jsonify({
                "message": "I appreciate you sharing that. Based on the budget you provided, it seems like our 'Project Pipeline AI' might not be the right fit. I appreciate your time and honesty!"
            }), 200
        
        # Define summary and description for the calendar event
        summary = f"Onboard Call with {company_name} | Zappies AI"
        description = (
            f"Onboarding call with {full_name} from {company_name} to discuss the 'Project Pipeline AI'.\n\n"
            f"Stated Goal: {goal}\n"
            f"Stated Budget: R{monthly_budget}/month"
        )
        
        # STEP 1: Create the Google Calendar event using the robust tool
        created_event = create_calendar_event(
            start_time=start_time,
            summary=summary,
            description=description,
            attendees=[email]
        )
        
        # STEP 2: Save the lead and the event ID to Supabase
        supabase.table("meetings").insert({
            "full_name": full_name,
            "email": email,
            "company_name": company_name,
            "start_time": start_time,
            "goal": goal,
            "monthly_budget": monthly_budget,
            "google_calendar_event_id": created_event.get('id')
        }).execute()

        first_name = full_name.split(' ')[0]
        success_message = (
            f"Perfect, {first_name}! I've successfully booked your call. "
            f"You will receive a calendar invitation to {email} shortly to confirm."
        )
        return jsonify({"message": success_message}), 201

    except Exception as e:
        print(f"A general error occurred in /book-appointment: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 8080)))