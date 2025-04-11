import os
import io
import datetime
import time # Ensure time is imported
import re # Import regex for parsing
import json # Import json for handling prescription data
import logging

from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for, flash, session # Added session
from flask_sock import Sock # Added for WebSockets
# Import WebSocket exceptions
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

import mysql.connector
from google.cloud import speech
import google.generativeai as genai # Updated import for Gemini API
# Import Google API core exceptions
import google.api_core.exceptions
from fpdf import FPDF
from fpdf.enums import XPos, YPos # Import XPos and YPos for modern API
import requests # Add requests import

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
google_credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
gcp_project_id = os.getenv('GCP_PROJECT_ID')
gemini_api_key = os.getenv('GEMINI_API_KEY')
gemini_model_id = os.getenv('GEMINI_MODEL_ID', 'gemini-1.5-flash-latest') # Default updated
db_host = os.getenv('DB_HOST')
db_user = os.getenv('DB_USER')
db_password = os.getenv('DB_PASSWORD')
db_name = os.getenv('DB_NAME')

# Audio parameters for streaming
STREAMING_RATE = 48000 # Keep 48kHz based on browser reality

# Load OpenFDA API Key
OPENFDA_API_KEY = os.getenv("OPENFDA_API_KEY")
if not OPENFDA_API_KEY:
    logging.warning("OPENFDA_API_KEY not found in .env file. ADR validation will be skipped.")

# --- Initialize Flask App & Sock ---
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'a_default_secret_key_for_development') # Needed for flash messages
sock = Sock(app) # Initialize Flask-Sock

# --- Initialize Google Cloud Clients ---
# STT Client (Uses GOOGLE_APPLICATION_CREDENTIALS env var automatically)
stt_client = speech.SpeechClient()

# Gemini Client (Uses API Key)
genai.configure(api_key=gemini_api_key)
gemini_model = genai.GenerativeModel(gemini_model_id)

# --- Database Connection ---
def get_db_connection():
    """Establishes a new database connection."""
    try:
        conn = mysql.connector.connect(
            host=db_host,
            user=db_user,
            password=db_password,
            database=db_name
        )
        return conn
    except mysql.connector.Error as err:
        print(f"Error connecting to database: {err}")
        return None

def fetch_one(query, params=()):
    """Fetches a single record from the database."""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(query, params)
        result = cursor.fetchone()
        return result
    except mysql.connector.Error as err:
        print(f"Database query error: {err}")
        return None
    finally:
        cursor.close()
        conn.close()

def fetch_all(query, params=()):
    """Fetches all records matching the query."""
    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(query, params)
        results = cursor.fetchall()
        return results
    except mysql.connector.Error as err:
        print(f"Database query error: {err}")
        return []
    finally:
        cursor.close()
        conn.close()

def execute_query(query, params=()):
    """Executes an INSERT, UPDATE, or DELETE query."""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor()
    last_row_id = None
    try:
        cursor.execute(query, params)
        conn.commit()
        last_row_id = cursor.lastrowid
        return last_row_id
    except mysql.connector.Error as err:
        print(f"Database execution error: {err}")
        conn.rollback()
        return None
    finally:
        cursor.close()
        conn.close()

# --- Flask Routes ---
@app.route('/')
def index():
    """Renders the main dashboard page."""
    # Fetch all patients for the search list
    patients = fetch_all("SELECT id, name FROM Patient ORDER BY name")
    
    # Fetch count of consultations for today
    # Assuming doctor_id 1 for now
    doctor_id = 1 # <<< HARDCODED DOCTOR ID
    today_date = datetime.date.today()
    query_today = """SELECT COUNT(*) as count 
                   FROM Consultation 
                   WHERE doctor_id = %s AND DATE(consultation_date) = %s"""
    today_data = fetch_one(query_today, (doctor_id, today_date))
    todays_consultations_count = today_data['count'] if today_data else 0
    
    return render_template(
        'index.html', 
        patients=patients, 
        todays_count=todays_consultations_count
    )

@app.route('/consultation/<int:patient_id>')
def consultation_page(patient_id):
    """Renders the main consultation page for a specific patient."""
    patient = fetch_one("SELECT name FROM Patient WHERE id = %s", (patient_id,))
    if not patient:
        return "Patient not found", 404
    # Assume doctor ID 1 for now, replace with actual login system later
    doctor_id = 1
    return render_template('consultation.html', patient=patient, patient_id=patient_id, doctor_id=doctor_id)

# UPDATED Route: Process accumulated transcript text via Gemini
@app.route('/process_transcript_text', methods=['POST'])
def process_transcript_text():
    """Processes the final transcript text using Gemini, aiming for structured output."""
    data = request.json
    if not data or 'transcript_text' not in data:
        return jsonify({"error": "Missing 'transcript_text' in request"}), 400

    raw_transcript = data['transcript_text']
    print(f"Received transcript text for processing: {len(raw_transcript)} chars")

    # Define the empty structure here for reuse
    empty_structure = {
        "chief_complaints": "",
        "clinical_findings": "",
        "internal_notes": "", # Added internal notes field
        "diagnosis": "",
        "procedures_conducted": "",
        "prescription_details": [],
        "investigations": "",
        "advice_given": "",
        "follow_up_date": ""
    }

    if not raw_transcript or raw_transcript == "(Listening...)":
        print("Received empty or placeholder transcript.")
        return jsonify({"ai_draft": empty_structure}) # Return empty structure

    try:
        # --- Prepare Gemini Prompt (Updated for more structure) ---
        prompt = f"""Analyze the following doctor-patient consultation transcript. Extract the relevant medical information and structure it clearly under the specified headings. Be concise and accurate. If information for a heading is not present, leave it blank or write 'None mentioned'.

TRANSCRIPT:
```
{raw_transcript}
```

EXTRACTED INFORMATION:
Chief Complaints:
[Extract chief complaints here]

Clinical Findings:
[Extract clinical findings here]

Internal Notes:
[Extract any notes clearly intended for the doctor only, if any. If none, state 'None mentioned']

Diagnosis:
[Extract diagnosis here]

Procedures Conducted:
[Extract procedures conducted, if any. If none, state 'None mentioned']

Prescription:
[List each prescribed medicine on a new line in the format: Medicine Name | Dosage | Duration/Total. Example: Tab Metformin | 500mg | 1 tab twice daily for 30 days. If no prescription, state 'None mentioned']

Investigations:
[List investigations ordered, if any. Example: CBC, X-Ray Chest. If none, state 'None mentioned']

Advice Given:
[Extract advice given to the patient]

Follow-Up Date:
[Extract follow-up date, if mentioned, in YYYY-MM-DD format. If not mentioned, state 'None mentioned']
"""

        # --- Call Gemini API ---
        print("Calling Gemini API...")
        gemini_response = gemini_model.generate_content(prompt)
        print("Gemini API call finished.")

        # Check for safety ratings or blocks
        if not gemini_response.candidates or not hasattr(gemini_response, 'text'):
             ai_generated_draft_text = "(AI analysis failed or response structure invalid)"
             print("Gemini response was blocked or structure invalid.")
             return jsonify({
                 "ai_draft": ai_generated_draft_text, 
                 "original_gemini_text": ai_generated_draft_text # Return error as original text too
                 })

        original_gemini_text = gemini_response.text # Store the original text
        print(f"""Gemini generated text ({len(original_gemini_text)} chars):
---
{original_gemini_text}
---""")

        # --- Parse the Gemini Output ---
        ai_draft_structured = empty_structure.copy()
        ai_draft_structured["prescription_details"] = [] # Ensure list is empty

        # Helper function to extract section text (more robust)
        def extract_section(text, heading):
            # Regex explanation:
            # ^                       - Start of line (due to re.MULTILINE)
            # {re.escape(heading)}    - The literal heading string, escaping special chars
            # :?                      - Optional colon
            # \s*                    - Optional whitespace
            # (                       - Start capturing group 1
            #   [\s\S]*?            - Any character (including newlines), non-greedy
            # )                       - End capturing group 1
            # (?=                     - Positive lookahead (pattern must follow, but isn't captured)
            #    \n^[A-Z][a-zA-Z ]+:?  - Newline, then a likely next heading (starts with capital, has letters/spaces, optional colon)
            #   |                      - OR
            #    \Z                   - End of the entire string
            # )
            # Handle case where "None mentioned" is the value
            regex_str = f"^{re.escape(heading)}:?\\s*([\s\S]*?)(?=\n^[A-Z][a-zA-Z ]+:?|\Z)"
            match = re.search(regex_str, text, re.MULTILINE | re.IGNORECASE)
            content = match.group(1).strip() if match else ""
            # If extracted content is effectively "None mentioned", return empty string
            return "" if content.lower().startswith(('none mentioned', 'n/a')) else content

        ai_draft_structured["chief_complaints"] = extract_section(original_gemini_text, "Chief Complaints")
        ai_draft_structured["clinical_findings"] = extract_section(original_gemini_text, "Clinical Findings")
        ai_draft_structured["internal_notes"] = extract_section(original_gemini_text, "Internal Notes")
        ai_draft_structured["diagnosis"] = extract_section(original_gemini_text, "Diagnosis")
        ai_draft_structured["procedures_conducted"] = extract_section(original_gemini_text, "Procedures Conducted")
        ai_draft_structured["investigations"] = extract_section(original_gemini_text, "Investigations")
        ai_draft_structured["advice_given"] = extract_section(original_gemini_text, "Advice Given")
        ai_draft_structured["follow_up_date"] = extract_section(original_gemini_text, "Follow-Up Date")

        # Parse Prescription section
        prescription_text = extract_section(original_gemini_text, "Prescription")
        if prescription_text:
            lines = prescription_text.strip().split('\n')
            for line in lines:
                line = line.strip()
                if not line or line.lower().startswith(('none mentioned', 'n/a')):
                    continue
                parts = [p.strip() for p in line.split('|')]
                if len(parts) == 3:
                    ai_draft_structured["prescription_details"].append({
                        "medicine": parts[0],
                        "dosage": parts[1],
                        "duration": parts[2]
                    })
                elif len(parts) > 0 and parts[0]: # Fallback for less structured lines
                    ai_draft_structured["prescription_details"].append({
                        "medicine": line, # Use the whole line as name
                        "dosage": "",
                        "duration": ""
                    })
        print("Parsed structured draft:", ai_draft_structured)
        ai_generated_draft = ai_draft_structured

    except Exception as e:
        import traceback
        print(f"Error during Gemini processing or parsing: {e}\n{traceback.format_exc()}")
        error_message = f"Gemini processing/parsing failed: {type(e).__name__}"
        # Return error string in both fields
        return jsonify({
            "ai_draft": f"ERROR: {error_message}",
            "original_gemini_text": f"ERROR: {error_message}"
            }), 500

    # Return both structured draft and original text
    return jsonify({
        "ai_draft": ai_generated_draft,
        "original_gemini_text": original_gemini_text
    })

# UPDATED Route: Save consultation details (structured)
@app.route('/save_consultation', methods=['POST']) # Removed patient_id from URL
def save_consultation():
    """Saves the confirmed structured consultation data to the database."""
    data = request.json
    # Get doctor_id from session <-- Temporarily disable session check for MVP
    # doctor_id = session.get('user_id')

    # Validate session <-- Temporarily disable session check for MVP
    # if not doctor_id:
    #     return jsonify({"error": "User not logged in or session expired"}), 401 # Unauthorized

    doctor_id = 1 # <<< TEMPORARY HARDCODED DOCTOR ID FOR MVP

    # Define required fields from the incoming JSON data
    # doctor_id is now handled via session
    required_fields_from_data = ["patient_id", "raw_transcript", # Expecting raw_transcript
                                 "chief_complaints", "clinical_findings", "internal_notes",
                                 "diagnosis", "procedures_conducted", "prescription_details",
                                 "investigations", "advice_given"]
                                 # follow_up_date is optional
                                 # ai_summary is optional or can be derived/ignored

    # Validate incoming data
    if not data or not all(field in data for field in required_fields_from_data):
        missing = [field for field in required_fields_from_data if not data or field not in data]
        return jsonify({"error": f"Missing required data fields: {', '.join(missing)}"}), 400

    # Extract data (handle optional follow_up_date)
    patient_id = data.get("patient_id")
    # doctor_id = data.get("doctor_id") # Removed - get from session
    raw_transcript = data.get("raw_transcript", "")
    # ai_summary = data.get("ai_summary", "") # ai_summary is no longer explicitly sent or required here
    chief_complaints = data.get("chief_complaints", "")
    clinical_findings = data.get("clinical_findings", "")
    internal_notes = data.get("internal_notes", "")
    diagnosis = data.get("diagnosis", "")
    procedures_conducted = data.get("procedures_conducted", "")
    investigations = data.get("investigations", "")
    advice_given = data.get("advice_given", "")
    follow_up_date_str = data.get("follow_up_date") # Get as string
    prescription_details_list = data.get("prescription_details", []) # Get as list

    # Convert follow_up_date string to Date object or None
    follow_up_date = None
    if follow_up_date_str:
        try:
            follow_up_date = datetime.datetime.strptime(follow_up_date_str, '%Y-%m-%d').date()
        except ValueError:
            print(f"Warning: Invalid follow-up date format received: {follow_up_date_str}. Saving as NULL.")
            # Optionally return error: return jsonify({"error": "Invalid follow_up_date format. Use YYYY-MM-DD."}), 400
            follow_up_date = None # Ensure it's None if format is wrong

    # Serialize prescription details list to JSON string for DB
    prescription_details_json = json.dumps(prescription_details_list)

    consultation_date = datetime.datetime.now()

    # --- Derive ai_summary (Optional: Can generate a simple summary or use first part of raw transcript) ---
    # Option 1: Use first N chars of raw_transcript as summary
    ai_summary = (raw_transcript[:500] + '...') if len(raw_transcript) > 500 else raw_transcript
    # Option 2: Leave ai_summary blank or NULL in DB (adjust query/params if needed)
    # ai_summary = "" # Or handle NULL in DB

    # Updated SQL Query (ensure ai_summary placeholder exists if using Option 1)
    query = ("""INSERT INTO Consultation (
                patient_id, doctor_id, consultation_date, raw_transcript, ai_summary,
                chief_complaints, clinical_findings, internal_notes, diagnosis,
                procedures_conducted, prescription_details, investigations, advice_given,
                follow_up_date
             ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""")
    params = (patient_id, doctor_id, consultation_date, raw_transcript, ai_summary, # Use derived ai_summary
              chief_complaints, clinical_findings, internal_notes, diagnosis,
              procedures_conducted, prescription_details_json, investigations, advice_given,
              follow_up_date) # Pass None if date was invalid/empty

    consultation_id = execute_query(query, params)

    if consultation_id:
        return jsonify({"success": True, "consultation_id": consultation_id})
    else:
        return jsonify({"error": "Failed to save consultation to database"}), 500

# --- Settings Routes ---
@app.route('/settings')
def settings_page():
    """Displays the settings page for the logged-in doctor."""
    # Assume doctor ID 1 for now
    doctor_id = 1 # <<< HARDCODED DOCTOR ID
    
    doctor_details = fetch_one("SELECT * FROM User WHERE id = %s", (doctor_id,))
    
    if not doctor_details:
        flash("Doctor details not found.", "error")
        return redirect(url_for('index')) # Redirect if doctor doesn't exist
        
    return render_template('settings.html', doctor=doctor_details)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    """Updates the settings for the logged-in doctor."""
    # Assume doctor ID 1 for now
    doctor_id = 1 # <<< HARDCODED DOCTOR ID

    try:
        # Get data from form
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone_number')
        reg_no = request.form.get('registration_number')
        qual = request.form.get('qualifications')
        clinic_name = request.form.get('clinic_name')
        clinic_addr = request.form.get('clinic_address')
        clinic_time = request.form.get('clinic_timings')
        clinic_closed = request.form.get('clinic_closed_days')

        # Basic validation (add more as needed)
        if not name or not email:
            flash("Doctor Name and Email are required.", "error")
            return redirect(url_for('settings_page'))

        query = """UPDATE User SET 
                    name = %s, email = %s, phone_number = %s, registration_number = %s, 
                    qualifications = %s, clinic_name = %s, clinic_address = %s, 
                    clinic_timings = %s, clinic_closed_days = %s 
                 WHERE id = %s"""
        params = (name, email, phone, reg_no, qual, clinic_name, clinic_addr, 
                  clinic_time, clinic_closed, doctor_id)
        
        execute_query(query, params)
        flash("Settings updated successfully!", "success")

    except Exception as e:
        print(f"Error updating settings: {e}")
        flash(f"Error updating settings: {e}", "error")

    return redirect(url_for('settings_page'))

# UPDATED Route: PDF Download (Using Dynamic Doctor/Clinic Data)
@app.route('/download_pdf/<int:consultation_id>')
def download_pdf(consultation_id):
    """Generates and sends a PDF for the given consultation using structured data."""
    # Fetch consultation AND the specific doctor's details for this consultation
    query = """SELECT c.*, 
                  p.name AS patient_name, p.dob AS patient_dob, 
                  p.gender AS patient_gender, p.address AS patient_address, 
                  d.name AS doctor_name, d.phone_number AS doctor_phone, 
                  d.registration_number AS doctor_reg_no, d.qualifications AS doctor_qual, 
                  d.clinic_name, d.clinic_address, d.clinic_timings, d.clinic_closed_days 
           FROM Consultation c 
           JOIN Patient p ON c.patient_id = p.id 
           JOIN User d ON c.doctor_id = d.id 
           WHERE c.id = %s"""
    consultation_data = fetch_one(query, (consultation_id,))

    if not consultation_data:
        return "Consultation not found", 404

    try:
        pdf = FPDF(orientation="P", unit="mm", format="A4")
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.set_font("Helvetica", size=10)
        page_width = pdf.w - 2 * pdf.l_margin

        # --- Header (Using Dynamic Data) ---
        header_y_start = pdf.get_y()
        pdf.set_font("Helvetica", 'B', 11)
        pdf.cell(page_width * 0.6, 5, f"{consultation_data.get('doctor_name', 'N/A')}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", size=9)
        qual_reg = f"{consultation_data.get('doctor_qual', 'N/A')} | Reg. No: {consultation_data.get('doctor_reg_no', 'N/A')}"
        pdf.cell(page_width * 0.6, 4, qual_reg, border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(page_width * 0.6, 4, f"Mob. No: {consultation_data.get('doctor_phone', 'N/A')}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        header_y_end_left = pdf.get_y()

        # Clinic Details (Dynamic)
        pdf.set_y(header_y_start)
        pdf.set_x(pdf.l_margin + page_width * 0.6)
        pdf.set_font("Helvetica", 'B', 11)
        pdf.multi_cell(page_width * 0.4, 5, f"{consultation_data.get('clinic_name', 'Clinic Name N/A')}", border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_x(pdf.l_margin + page_width * 0.6)
        pdf.set_font("Helvetica", size=9)
        pdf.multi_cell(page_width * 0.4, 4, f"{consultation_data.get('clinic_address', 'Address N/A')}", border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_x(pdf.l_margin + page_width * 0.6)
        pdf.multi_cell(page_width * 0.4, 4, f"Ph: {consultation_data.get('doctor_phone', 'N/A')}", border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Assuming clinic phone is doctor's phone for now
        pdf.set_x(pdf.l_margin + page_width * 0.6)
        timing_str = f"Timing: {consultation_data.get('clinic_timings', 'N/A')}"
        if closed_days := consultation_data.get('clinic_closed_days'):
            timing_str += f" | Closed: {closed_days}"
        pdf.multi_cell(page_width * 0.4, 4, timing_str, border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        header_y_end_right = pdf.get_y()

        # Move below the taller header block
        pdf.set_y(max(header_y_end_left, header_y_end_right) + 2)

        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y()) # Full width line
        pdf.ln(4) # Space after header line

        # --- Patient Details & Date ---
        # Calculate Age (Example)
        age = 'N/A'
        if dob := consultation_data.get('patient_dob'):
            today = datetime.date.today()
            try:
                age_val = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                age = f"{age_val} Y"
            except TypeError:
                age = "Invalid DOB"
                
        patient_gender = consultation_data.get('patient_gender', 'N/A') or 'N/A' # Handle None or empty string
        patient_display = f"ID: {consultation_data['patient_id']} - {consultation_data['patient_name']} ({patient_gender} / {age})"
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(page_width * 0.7, 5, patient_display, border=0, new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.set_font("Helvetica", '', 10)
        date_str = consultation_data['consultation_date'].strftime("%d-%b-%Y, %I:%M %p")
        pdf.cell(page_width * 0.3, 5, f"Date: {date_str}", border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("Helvetica", '', 9)
        pdf.cell(page_width, 4, f"Address: {consultation_data.get('patient_address') or 'N/A'}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(page_width, 4, f"Weight(kg): N/A  Height (cms): N/A  BP: N/A", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(page_width, 4, f"Referred By: N/A", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(page_width, 4, f"Known History Of: N/A", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4) # More space before consultation details

        # --- Consultation Details (Two Columns - Reworked Logic) ---
        col_width = page_width / 2 - 2 # Column width with small gap
        line_height = 5
        section_padding_bottom = 2 # Space after content within a section box
        row_gap = 2 # Vertical space between rows of sections

        # --- Helper Function to calculate multicell height ---
        def get_multicell_height(width, text):
            pdf.set_font("Helvetica", '', 9) # Ensure correct font is set for calculation
            lines = pdf.multi_cell(width, line_height, text or "N/A", border=0, align='L', split_only=True)
            return len(lines) * line_height + section_padding_bottom

        # --- Helper to draw a bordered section box with content ---
        def draw_bordered_section(x, y, height, heading, content):
            pdf.set_xy(x, y)
            # Draw top border
            pdf.line(x, y, x + col_width, y)
            # Heading
            pdf.set_font("Helvetica", 'B', 10)
            pdf.cell(col_width, 6, " " + heading, border=0, align='L', new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Add space before heading
            # Content
            content_start_y = pdf.get_y()
            pdf.set_x(x + 1) # Indent content slightly
            pdf.set_font("Helvetica", '', 9)
            pdf.multi_cell(col_width - 2, line_height, content or "N/A", border=0, align='L')
            # Draw side borders based on calculated height
            pdf.line(x, y, x, y + height)
            pdf.line(x + col_width, y, x + col_width, y + height)
            # Draw bottom border
            pdf.line(x, y + height, x + col_width, y + height)

        # --- Draw Sections Row by Row ---
        current_y = pdf.get_y()
        left_x = pdf.l_margin
        right_x = pdf.l_margin + col_width + 4

        # Row 1: Chief Complaints & Clinical Findings
        cc_content = consultation_data.get('chief_complaints')
        cf_content = consultation_data.get('clinical_findings')
        h1 = get_multicell_height(col_width - 2, cc_content) + 6 # Add heading height
        h2 = get_multicell_height(col_width - 2, cf_content) + 6
        max_h_row1 = max(h1, h2)
        draw_bordered_section(left_x, current_y, max_h_row1, "Chief Complaints", cc_content)
        draw_bordered_section(right_x, current_y, max_h_row1, "Clinical Findings", cf_content)
        current_y += max_h_row1 + row_gap

        # Row 2: Notes & Diagnosis
        notes_content = consultation_data.get('internal_notes') # Use internal_notes field
        diag_content = consultation_data.get('diagnosis')
        h1 = get_multicell_height(col_width - 2, notes_content) + 6
        h2 = get_multicell_height(col_width - 2, diag_content) + 6
        max_h_row2 = max(h1, h2)
        draw_bordered_section(left_x, current_y, max_h_row2, "Notes", notes_content)
        draw_bordered_section(right_x, current_y, max_h_row2, "Diagnosis", diag_content)
        current_y += max_h_row2 + row_gap

        # Row 3: Procedures & Investigations
        proc_content = consultation_data.get('procedures_conducted')
        inv_content = consultation_data.get('investigations')
        h1 = get_multicell_height(col_width - 2, proc_content) + 6
        h2 = get_multicell_height(col_width - 2, inv_content) + 6
        max_h_row3 = max(h1, h2)
        draw_bordered_section(left_x, current_y, max_h_row3, "Procedures conducted", proc_content)
        draw_bordered_section(right_x, current_y, max_h_row3, "Investigations", inv_content)
        current_y += max_h_row3 + 4 # Extra gap before prescription
        pdf.set_y(current_y)

        # --- Prescription Table (Updated for all fields) ---
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(0, 8, "Prescription", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_fill_color(230, 230, 230)
        pdf.set_font("Helvetica", 'B', 9)
        
        # Define column widths (Adjusted for new columns)
        col_sr = 10
        col_med = 50 # Reduced
        col_dose = 30 # Reduced
        col_freq = 30 # Added
        col_dur = 30 # Reduced
        col_inst = page_width - col_sr - col_med - col_dose - col_freq - col_dur # Added Instructions
        row_height = 6 # Base row height

        # Table Header (Updated)
        start_x = pdf.get_x()
        pdf.cell(col_sr, row_height, "Sr.", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(col_med, row_height, "Medicine Name", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(col_dose, row_height, "Dosage", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(col_freq, row_height, "Frequency", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP) # Added Frequency Header
        pdf.cell(col_dur, row_height, "Duration", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(col_inst, row_height, "Instructions", border=1, fill=True, align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Added Instructions Header

        pdf.set_font("Helvetica", '', 9)
        pdf.set_fill_color(255, 255, 255) # Reset fill
        prescription_list = json.loads(consultation_data.get('prescription_details', '[]') or '[]') # Handle None
        
        if prescription_list:
            for i, item in enumerate(prescription_list, 1):
                start_y = pdf.get_y()
                start_x = pdf.get_x()
                
                # Calculate height needed for each cell in this row
                pdf.set_xy(start_x + col_sr, start_y) # Position for Medicine cell
                pdf.multi_cell(col_med, line_height, item.get('medicine_name', ''), border=0, align='L')
                h_med = pdf.get_y() - start_y

                pdf.set_xy(start_x + col_sr + col_med, start_y) # Position for Dosage cell
                pdf.multi_cell(col_dose, line_height, item.get('dosage', ''), border=0, align='L')
                h_dose = pdf.get_y() - start_y

                pdf.set_xy(start_x + col_sr + col_med + col_dose, start_y) # Position for Frequency cell
                pdf.multi_cell(col_freq, line_height, item.get('frequency', ''), border=0, align='L')
                h_freq = pdf.get_y() - start_y

                pdf.set_xy(start_x + col_sr + col_med + col_dose + col_freq, start_y) # Position for Duration cell
                pdf.multi_cell(col_dur, line_height, item.get('duration', ''), border=0, align='L')
                h_dur = pdf.get_y() - start_y

                pdf.set_xy(start_x + col_sr + col_med + col_dose + col_freq + col_dur, start_y) # Position for Instructions cell
                pdf.multi_cell(col_inst, line_height, item.get('instructions', ''), border=0, align='L')
                h_inst = pdf.get_y() - start_y

                # Determine max height for the row
                max_h = max(h_med, h_dose, h_freq, h_dur, h_inst, row_height) # Added h_freq, h_inst

                # Draw the cells with borders and calculated height
                pdf.set_xy(start_x, start_y)
                pdf.cell(col_sr, max_h, str(i), border=1, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.set_xy(start_x + col_sr, start_y) # Need xy for multi_cell positioning
                pdf.multi_cell(col_med, line_height, item.get('medicine_name', ''), border='LR', align='L')
                pdf.set_xy(start_x + col_sr + col_med, start_y)
                pdf.multi_cell(col_dose, line_height, item.get('dosage', ''), border='LR', align='L')
                pdf.set_xy(start_x + col_sr + col_med + col_dose, start_y)
                pdf.multi_cell(col_freq, line_height, item.get('frequency', ''), border='LR', align='L') # Added Frequency Cell Draw
                pdf.set_xy(start_x + col_sr + col_med + col_dose + col_freq, start_y)
                pdf.multi_cell(col_dur, line_height, item.get('duration', ''), border='LR', align='L')
                pdf.set_xy(start_x + col_sr + col_med + col_dose + col_freq + col_dur, start_y)
                pdf.multi_cell(col_inst, line_height, item.get('instructions', ''), border='LR', align='L') # Added Instructions Cell Draw
                
                # Draw bottom border for the row
                pdf.set_y(start_y + max_h) # Move Y below the row
                pdf.line(start_x, pdf.get_y(), start_x + page_width, pdf.get_y())
                pdf.set_x(start_x) # Reset X for next potential row
        else:
             pdf.cell(page_width, row_height, "No medication prescribed.", border=1, align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(5) # Space after table

        # --- Advice & Follow Up (Improved Spacing) ---
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(page_width, 6, "Advice Given:", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", '', 9)
        pdf.multi_cell(page_width, line_height, consultation_data.get('advice_given') or "N/A", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4) # Increased space

        pdf.set_font("Helvetica", 'B', 10)
        follow_up = consultation_data.get('follow_up_date')
        follow_up_str = follow_up.strftime("%d-%m-%Y") if follow_up else "N/A"
        pdf.cell(page_width, 6, f"Follow Up: {follow_up_str}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(12) # More space before signature

        # --- Signature ---
        current_y = pdf.get_y()
        pdf.set_y(max(current_y, pdf.h - pdf.b_margin - 25)) # Move towards bottom margin
        pdf.cell(page_width, 5, "Signature", border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(page_width, 5, consultation_data.get('doctor_name', 'N/A'), border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", '', 9)
        pdf.cell(page_width, 4, consultation_data.get('doctor_qual', 'N/A'), border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Use dynamic qualifications

        # --- Save PDF to Buffer ---
        pdf_buffer = io.BytesIO()
        pdf_output = pdf.output()
        pdf_buffer.write(pdf_output)
        pdf_buffer.seek(0)

        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=f"consultation_{consultation_id}_{consultation_data['patient_name'].replace(' ', '_')}.pdf", # Sanitize filename
            mimetype='application/pdf'
        )

    except Exception as e:
        import traceback
        print(f"Error generating PDF: {e}\n{traceback.format_exc()}")
        return jsonify({"error": f"Failed to generate PDF: {e}"}), 500

# --- WebSocket Route for Live Transcription Demo ---
@sock.route('/live_transcript')
def live_transcript(ws): # ws is the WebSocket connection object
    print("Live transcript WebSocket connected")
    first_chunk_received = False
    last_activity_time = time.time()
    TIMEOUT_SECONDS = 7 # Timeout if no results after 7s of first chunk

    # Simplified request generator directly using ws.receive()
    def request_generator():
        nonlocal first_chunk_received, last_activity_time
        try:
            while True:
                chunk = ws.receive(timeout=10) # Keep timeout for receiving
                if chunk is None:
                    print("WS Generator: Received None, breaking.")
                    break
                if not first_chunk_received:
                    first_chunk_received = True
                    last_activity_time = time.time() # Start timeout timer on first chunk
                yield speech.StreamingRecognizeRequest(audio_content=chunk)
        except TimeoutError:
            print("WS Generator: Receive timed out.")
        except ConnectionClosedOK:
            print("WS Generator: Connection closed normally during receive.")
        except ConnectionClosedError as e:
            print(f"WS Generator: Connection closed error during receive: {e}")
        except Exception as e:
            print(f"WS Generator: Error: {type(e).__name__}: {e}")
        finally:
            print("WS Generator: Finished.")
            # No need to yield None here, API handles stream end on generator exit/close

    # Configure STT for streaming - Match browser's likely sample rate for demo
    recognition_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16, # RE-ADDED: Explicitly require LINEAR16 again
        sample_rate_hertz=48000, # UPDATED: Expect 48kHz based on console logs
        language_code="en-US",
        enable_automatic_punctuation=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=True
    )

    try:
        print("Starting STT streaming_recognize call (with auto-detect encoding)...")
        responses = stt_client.streaming_recognize(
            config=streaming_config,
            requests=request_generator()
        )
        print("STT streaming_recognize call returned. Processing responses...")

        # Process responses, includes timeout check
        process_stt_responses(ws, responses, last_activity_time, TIMEOUT_SECONDS, first_chunk_received)

    except ConnectionClosedOK:
        print("WebSocket connection closed normally (main loop).")
    except ConnectionClosedError as e:
        print(f"WebSocket connection closed with error (main loop): {e}")
    except google.api_core.exceptions.Cancelled as e:
        print(f"STT streaming cancelled, likely due to WebSocket closure (main loop): {e}")
    except Exception as e:
        print(f"Error during live transcription processing (main loop): {type(e).__name__}: {e}")
        try:
            if ws.connected:
                ws.send(f"ERROR: Server encountered an issue - {type(e).__name__}")
        except Exception as send_err:
            print(f"Failed to send error to client: {send_err}")
    finally:
        print("Live transcript main handler finished. Ensuring WebSocket is closed.")
        if ws.connected:
             try:
                 ws.close() # Use Flask-Sock's close method signature
                 print("WebSocket explicitly closed in finally block.")
             except Exception as close_err:
                 print(f"Error closing WebSocket in finally block: {close_err}")

def process_stt_responses(ws, responses, start_time, timeout_duration, chunk_received_flag):
    """Processes STT responses and sends transcripts back over WebSocket, includes timeout."""
    print("Starting to process STT responses...")
    transcript_sent = False
    try:
        for response in responses:
            start_time = time.time() # Reset timeout on any response from API
            # Log the raw response structure slightly for debugging
            # print(f"STT Response received: {response}")
            if not response.results:
                continue

            result = response.results[0]
            if not result.alternatives:
                continue

            transcript = result.alternatives[0].transcript

            if not ws.connected:
                print("WebSocket no longer connected, stopping response processing.")
                break

            if result.is_final:
                print(f"STT Final Result: '{transcript}'")
                ws.send(f"FINAL: {transcript}")
                transcript_sent = True
            elif transcript:
                print(f"STT Interim Result: '{transcript}'")
                ws.send(f"INTERIM: {transcript}")
                transcript_sent = True

        # After loop finishes check if ANY transcript was ever sent
        if ws.connected and not transcript_sent:
             print("STT stream finished but no transcripts were sent.")
             ws.send("STATUS: No transcript generated. Check mic or audio format.")

    except google.api_core.exceptions.Cancelled as e:
         print(f"STT response processing cancelled, likely due to WebSocket closure: {e}")
    except ConnectionClosedOK:
        print("WebSocket closed normally during send processing.")
    except ConnectionClosedError as e:
        print(f"WebSocket send error during processing (connection closed): {e}")
    except Exception as e:
        print(f"Error processing/sending STT response: {type(e).__name__}: {e}")
        if ws.connected:
            try:
                ws.send(f"ERROR: Processing STT response failed - {type(e).__name__}")
            except Exception as send_err:
                print(f"Failed to send processing error to client: {send_err}")
    finally:
        print(f"Process STT responses loop finished. (Transcripts sent: {transcript_sent})")

# --- New EOD Summary Route ---
@app.route('/get_eod_data')
def get_eod_data():
    """Fetches details of consultations created today and doctor info."""
    # Assuming doctor ID 1 for now
    doctor_id = 1 # <<< HARDCODED DOCTOR ID
    today_date = datetime.date.today()
    
    # Fetch Doctor's Name
    doctor_info = fetch_one("SELECT name FROM User WHERE id = %s", (doctor_id,))
    doctor_name = doctor_info['name'] if doctor_info else "Unknown Doctor"
    
    # Fetch Consultations
    query = """SELECT p.name AS patient_name, c.diagnosis 
               FROM Consultation c 
               JOIN Patient p ON c.patient_id = p.id 
               WHERE c.doctor_id = %s AND DATE(c.consultation_date) = %s 
               ORDER BY c.consultation_date"""
    
    todays_consultations = fetch_all(query, (doctor_id, today_date))
    
    return jsonify({
        "doctor_name": doctor_name,
        "consultations": todays_consultations
        })

# --- New Route for Live ADR Check ---
@app.route('/check_adr', methods=['POST'])
def check_adr():
    if not OPENFDA_API_KEY:
        return jsonify({"error": "OpenFDA API Key not configured"}), 500

    # --- START: Simple MVP OpenFDA Check (Always Runs) ---
    try:
        # Use a common, known drug for this simple check
        fixed_drug_term_for_check = "aspirin"
        # Simple query for the label endpoint
        simple_label_query = f'(openfda.brand_name:"{fixed_drug_term_for_check}"+OR+openfda.generic_name:"{fixed_drug_term_for_check}")'
        simple_label_url = f"https://api.fda.gov/drug/label.json?api_key={OPENFDA_API_KEY}&search={simple_label_query}&limit=1"
        
        # Log the attempt clearly
        logging.info(f"[MVP VALIDATION CHECK] Attempting hardcoded OpenFDA query for '{fixed_drug_term_for_check}'. URL: {simple_label_url}")
        
        # Make the request
        simple_fda_response = requests.get(simple_label_url, timeout=10)
        
        # Log the outcome clearly
        logging.info(f"[MVP VALIDATION CHECK] OpenFDA Response Status for '{fixed_drug_term_for_check}': {simple_fda_response.status_code}")
        
    except requests.exceptions.RequestException as mvp_e:
        logging.error(f"[MVP VALIDATION CHECK] OpenFDA API request failed for fixed term '{fixed_drug_term_for_check}': {mvp_e}")
    except Exception as mvp_e_generic:
         logging.error(f"[MVP VALIDATION CHECK] Unexpected error during simple OpenFDA check: {mvp_e_generic}")
    # --- END: Simple MVP OpenFDA Check ---

    data = request.get_json()
    transcript = data.get('transcript', '')

    if not transcript or len(transcript.split()) < 10: # Avoid checking very short transcripts
        return jsonify({"validated_adrs": []})

    logging.info(f"Checking ADR for transcript segment: {transcript[:100]}...")

    validated_adrs = []
    try:
        # 1. Call Gemini to identify potential Drug Names with context awareness
        prompt = f"""
        Analyze the following medical consultation transcript. Identify potential drug names mentioned.
        Note that the transcript may contain errors due to speech-to-text inaccuracies. Use your knowledge to infer the most likely correct drug names based on the context.
        Format the output STRICTLY as a JSON list of strings, where each string is a potential drug name.
        Example: ["Metformin", "Lisinopril"]
        If no drug names are clearly mentioned or inferable, return an empty list [].

        Transcript:
        "{transcript}"

        Potential Drug Names (JSON list of strings):
        """
        response = gemini_model.generate_content(prompt)
        logging.debug(f"Gemini Drug Name check response: {response.text}")

        # Attempt to parse the JSON list of drug names from Gemini
        potential_drug_names = []
        try:
            # Clean the response text if necessary (remove markdown, etc.)
            cleaned_response = response.text.strip().replace('```json', '').replace('```', '').strip()
            potential_drug_names = json.loads(cleaned_response)
            # Validate that it's a list of strings
            if not isinstance(potential_drug_names, list) or not all(isinstance(item, str) for item in potential_drug_names):
                 logging.warning(f"Gemini did not return a valid list of strings: {cleaned_response}")
                 potential_drug_names = []
        except json.JSONDecodeError:
            logging.error(f"Failed to decode JSON list from Gemini drug name response: {response.text}")
            potential_drug_names = [] # Proceed without potential drugs if parsing fails

        # 2. Validate potential drug names with OpenFDA label endpoint
        if potential_drug_names:
            logging.info(f"Gemini identified potential drug names: {potential_drug_names}")
            session_requests = requests.Session() # Use session for potential connection reuse
            # Use a set to avoid duplicate alerts for the same drug in one transcript segment
            validated_drugs_in_segment = set()

            for drug_name in potential_drug_names:
                # Skip if already validated in this segment
                if not drug_name or drug_name.lower() in validated_drugs_in_segment:
                    continue

                drug_term = drug_name.replace(' ', '+').strip()

                # --- SIMPLIFIED VALIDATION: Check if drug exists in OpenFDA labels ---
                label_query = f'(openfda.brand_name:"{drug_term}"+OR+openfda.generic_name:"{drug_term}")'
                label_url = f"https://api.fda.gov/drug/label.json?api_key={OPENFDA_API_KEY}&search={label_query}&limit=1"

                try:
                    logging.debug(f"Querying OpenFDA Drug Label: {label_url}") # DEBUG level log for URL
                    fda_response = session_requests.get(label_url, timeout=10)

                    # Handle 404 Not Found gracefully (drug not in label DB)
                    if fda_response.status_code == 404:
                        logging.info(f"OpenFDA Drug Label check FAILED for '{drug_name}' (404 Not Found in labels)")
                        continue # Skip to the next potential drug identified by Gemini

                    # Raise errors for other non-404 issues (like 500, timeout, etc.)
                    fda_response.raise_for_status()

                    fda_data = fda_response.json()
                    # Log the result count for clarity
                    total_found = fda_data.get("meta", {}).get("results", {}).get("total", 0)
                    logging.info(f"OpenFDA Label response for '{drug_name}': Status={fda_response.status_code}, Total={total_found}")

                    # Check if any results were found (drug label exists)
                    if total_found > 0:
                        logging.info(f"OpenFDA Drug Label validation SUCCESS for '{drug_name}'. Adding alert.")
                        # Add to validated list with a generic symptom message
                        validated_adrs.append({"drug": drug_name, "symptom": "Potential Interaction/Side Effect Mentioned"})
                        validated_drugs_in_segment.add(drug_name.lower()) # Add to set to prevent duplicates
                    else:
                         # This case might occur if the API returns 200 OK but total is 0
                         logging.info(f"OpenFDA Drug Label validation FAILED for '{drug_name}' (No matching labels found, total=0)")

                except requests.exceptions.RequestException as e:
                    # Log API errors but don't stop the whole process
                    logging.error(f"OpenFDA API request failed for drug label '{drug_name}': {e}")
                except json.JSONDecodeError:
                    logging.error(f"Failed to decode JSON from OpenFDA label response for '{drug_name}'")
                # Let other unexpected errors propagate up if necessary

    except Exception as e:
        import traceback # Ensure traceback is imported if used here
        logging.error(f"Error during ADR check: {e}", exc_info=True)

    logging.info(f"Returning validated ADRs: {validated_adrs}")
    return jsonify({"validated_adrs": validated_adrs})

# --- Run the App ---
if __name__ == '__main__':
    # Use werkzeug server for WebSocket support if not using flask run
    # Or use a production server like gunicorn with gevent/eventlet
    print("Starting Flask app with WebSocket support...")
    # Make sure debug=False for production!
    app.run(debug=True, port=5001, host='0.0.0.0') # Host needed for sock 