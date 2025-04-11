import os
import io
import datetime
import time # Ensure time is imported
import re # Import regex for parsing
import json # Import json for handling prescription data

from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_file, redirect, url_for
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

# --- Initialize Flask App & Sock ---
app = Flask(__name__)
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
    """Renders the patient selection page."""
    # Fetch patients to display on the index page
    patients = fetch_all("SELECT id, name FROM Patient ORDER BY name")
    return render_template('index.html', patients=patients)

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
    required_fields = ["patient_id", "doctor_id", "raw_transcript", "ai_summary",
                       "chief_complaints", "clinical_findings", "internal_notes",
                       "diagnosis", "procedures_conducted", "prescription_details",
                       "investigations", "advice_given"] # follow_up_date is optional
                       
    if not data or not all(field in data for field in required_fields):
        missing = [field for field in required_fields if field not in data] if data else required_fields
        return jsonify({"error": f"Missing required data fields: {', '.join(missing)}"}), 400

    # Extract data (handle optional follow_up_date)
    patient_id = data.get("patient_id")
    doctor_id = data.get("doctor_id")
    raw_transcript = data.get("raw_transcript", "")
    ai_summary = data.get("ai_summary", "")
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

    # Updated SQL Query
    query = ("""INSERT INTO Consultation (
                patient_id, doctor_id, consultation_date, raw_transcript, ai_summary,
                chief_complaints, clinical_findings, internal_notes, diagnosis,
                procedures_conducted, prescription_details, investigations, advice_given,
                follow_up_date
             ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""")
    params = (patient_id, doctor_id, consultation_date, raw_transcript, ai_summary,
              chief_complaints, clinical_findings, internal_notes, diagnosis,
              procedures_conducted, prescription_details_json, investigations, advice_given,
              follow_up_date) # Pass None if date was invalid/empty

    consultation_id = execute_query(query, params)

    if consultation_id:
        return jsonify({"success": True, "consultation_id": consultation_id})
    else:
        return jsonify({"error": "Failed to save consultation to database"}), 500

# UPDATED Route: PDF Download (fixed encoding error and deprecations)
@app.route('/download_pdf/<int:consultation_id>')
def download_pdf(consultation_id):
    """Generates and sends a PDF for the given consultation using structured data."""
    # Fetch structured consultation details
    query = """
    SELECT c.*,
           p.name AS patient_name, p.dob AS patient_dob, p.gender AS patient_gender, p.address AS patient_address,
           d.name AS doctor_name
           -- Add doctor details like M.B.B.S etc. from User table if needed
    FROM Consultation c
    JOIN Patient p ON c.patient_id = p.id
    JOIN User d ON c.doctor_id = d.id
    WHERE c.id = %s
    """
    consultation_data = fetch_one(query, (consultation_id,))

    if not consultation_data:
        return "Consultation not found", 404

    try:
        # --- Generate PDF using FPDF2 (Updated for structure & modern API) ---
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)

        # --- Header ---
        pdf.set_font("Helvetica", 'B', 11)
        pdf.cell(0, 5, f"Dr. {consultation_data.get('doctor_name', 'N/A')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Use new API
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 4, "M.B.B.S., M.D., M.S. | Reg. No: XXXXXX", new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Placeholder
        pdf.cell(0, 4, "Mob. No: XXXXXXXX", new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Placeholder
        pdf.ln(4)

        # Clinic Details (Placeholder)
        current_y = pdf.get_y()
        pdf.set_xy(130, 10) # Position to the right
        pdf.set_font("Helvetica", 'B', 11)
        pdf.cell(0, 5, "Care Clinic", align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 4, "Kothrud, Pune - 411038.", align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(0, 4, "Ph: 094233 80390", align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(0, 4, "Timing: 09:00 AM - 02:00 PM | Closed: Thursday", align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_y(current_y + 20) # Ensure we are below the header block
        pdf.set_x(10)

        pdf.line(10, pdf.get_y() + 2, 200, pdf.get_y() + 2)
        pdf.ln(5)

        # --- Patient Details & Date ---
        # Calculate Age (Example)
        age = 'N/A'
        if dob := consultation_data.get('patient_dob'):
            today = datetime.date.today()
            age_val = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            age = f"{age_val} Y"
            
        patient_display = f"ID: {consultation_data['patient_id']} - {consultation_data['patient_name']} ({consultation_data.get('patient_gender', 'N/A')} / {age})"
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(120, 5, patient_display, new_x=XPos.RIGHT, new_y=YPos.TOP) # Cell 1 (Patient Info)
        pdf.set_font("Helvetica", '', 10)
        date_str = consultation_data['consultation_date'].strftime("%d-%b-%Y, %I:%M %p")
        pdf.cell(0, 5, f"Date: {date_str}", align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Cell 2 (Date), move to next line

        pdf.set_font("Helvetica", '', 9)
        pdf.cell(0, 4, f"Address: {consultation_data.get('patient_address', 'N/A')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(0, 4, f"Weight(kg): N/A  Height (cms): N/A  BP: N/A", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(0, 4, f"Referred By: N/A", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(0, 4, f"Known History Of: N/A", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)

        # --- Consultation Details ---
        col_width = 95

        def add_section(heading, content):
            pdf.set_font("Helvetica", 'B', 10)
            pdf.cell(col_width, 6, heading, border='T', new_x=XPos.RIGHT, new_y=YPos.TOP) # Keep cursor on same line
            pdf.ln(4)
            pdf.set_font("Helvetica", '', 9)
            start_y = pdf.get_y()
            pdf.multi_cell(col_width - 5, 5, content or "N/A", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            end_y = pdf.get_y()
            pdf.set_xy(pdf.get_x() + col_width, start_y) # Move to start of next column
            return end_y

        # First Row: Chief Complaints & Clinical Findings
        y_complaints = add_section("Chief Complaints", consultation_data.get('chief_complaints'))
        pdf.set_xy(10 + col_width, pdf.get_y() - (y_complaints - (pdf.get_y() - 6))) # Reset Y
        y_findings = add_section("Clinical Findings", consultation_data.get('clinical_findings'))
        pdf.set_y(max(y_complaints, y_findings))
        pdf.set_x(10)
        pdf.line(10, pdf.get_y(), 10 + col_width * 2, pdf.get_y())
        pdf.ln(1)

        # Second Row: Notes & Diagnosis
        y_notes = add_section("Notes", consultation_data.get('internal_notes'))
        pdf.set_xy(10 + col_width, pdf.get_y() - (y_notes - (pdf.get_y() - 6))) # Reset Y
        y_diag = add_section("Diagnosis", consultation_data.get('diagnosis'))
        pdf.set_y(max(y_notes, y_diag))
        pdf.set_x(10)
        pdf.line(10, pdf.get_y(), 10 + col_width * 2, pdf.get_y())
        pdf.ln(1)
        
        # Third Row: Procedures & Investigations
        y_proc = add_section("Procedures conducted", consultation_data.get('procedures_conducted'))
        pdf.set_xy(10 + col_width, pdf.get_y() - (y_proc - (pdf.get_y() - 6))) # Reset Y
        y_inv = add_section("Investigations", consultation_data.get('investigations'))
        pdf.set_y(max(y_proc, y_inv))
        pdf.set_x(10)
        pdf.line(10, pdf.get_y(), 10 + col_width * 2, pdf.get_y())
        pdf.ln(1)

        # --- Prescription Table ---
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(0, 8, "R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_fill_color(230, 230, 230)
        pdf.set_font("Helvetica", 'B', 9)
        # Table Header
        pdf.cell(10, 6, "Sr.", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(70, 6, "Medicine Name", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(60, 6, "Dosage", border=1, fill=True, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.cell(50, 6, "Duration", border=1, fill=True, align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("Helvetica", '', 9)
        prescription_list = json.loads(consultation_data.get('prescription_details', '[]'))
        if prescription_list:
            for i, item in enumerate(prescription_list, 1):
                pdf.cell(10, 6, str(i), border=1, align='C', new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.cell(70, 6, item.get('medicine', ''), border=1, new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.cell(60, 6, item.get('dosage', ''), border=1, new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.cell(50, 6, item.get('duration', ''), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            pdf.cell(190, 6, "No medication prescribed.", border=1, align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(5)

        # --- Advice & Follow Up ---
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(0, 6, "Advice Given:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", '', 9)
        pdf.multi_cell(0, 5, consultation_data.get('advice_given') or "N/A", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)

        pdf.set_font("Helvetica", 'B', 10)
        follow_up = consultation_data.get('follow_up_date')
        follow_up_str = follow_up.strftime("%d-%m-%Y") if follow_up else "N/A"
        pdf.cell(0, 6, f"Follow Up: {follow_up_str}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(10)

        # --- Signature ---
        current_y = pdf.get_y() # Get y before potentially long signature block
        pdf.set_y(max(current_y, 250)) # Move signature towards bottom, but ensure space
        pdf.cell(0, 5, "Signature", align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(0, 5, consultation_data.get('doctor_name', 'N/A'), align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", '', 9)
        pdf.cell(0, 4, "M.B.B.S., M.D., M.S.", align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT) # Placeholder

        # --- Save PDF to Buffer --- 
        pdf_buffer = io.BytesIO()
        # pdf.output() returns bytes directly, no need to encode
        # Remove deprecated 'dest' parameter
        pdf_output = pdf.output() 
        pdf_buffer.write(pdf_output)
        pdf_buffer.seek(0)

        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=f"consultation_{consultation_id}_{consultation_data['patient_name']}.pdf",
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

# --- Run the App ---
if __name__ == '__main__':
    # Use werkzeug server for WebSocket support if not using flask run
    # Or use a production server like gunicorn with gevent/eventlet
    print("Starting Flask app with WebSocket support...")
    # Make sure debug=False for production!
    app.run(debug=True, port=5001, host='0.0.0.0') # Host needed for sock 