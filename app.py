import os
import io
import datetime
import time # Ensure time is imported

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

# NEW Route: Process accumulated transcript text via Gemini
@app.route('/process_transcript_text', methods=['POST'])
def process_transcript_text():
    """Processes the final transcript text using Gemini."""
    data = request.json
    if not data or 'transcript_text' not in data:
        return jsonify({"error": "Missing 'transcript_text' in request"}), 400

    raw_transcript = data['transcript_text']
    print(f"Received transcript text for processing: {len(raw_transcript)} chars")

    if not raw_transcript or raw_transcript == "(Listening...)":
        return jsonify({"ai_generated_draft": "No valid transcript received to process."})

    try:
        # --- Prepare Gemini Prompt ---
        prompt = f"""You are a medical assistant. Analyze the following doctor's dictation transcript and extract the key information into a structured format. Output ONLY the structured information under these headings:
Chief Complaint:
Key Symptoms:
Assessment/Diagnosis:
Prescribed Medications (Format: Drug Name - Dosage - Frequency - Duration):
Follow-up Instructions:

Transcript:
{raw_transcript}
"""

        # --- Call Gemini API ---
        print("Calling Gemini API...")
        gemini_response = gemini_model.generate_content(prompt)
        print("Gemini API call finished.")

        # Check for safety ratings or blocks if necessary (basic check)
        if not gemini_response.candidates or not gemini_response.candidates[0].content.parts:
             ai_generated_draft = "(AI analysis failed or was blocked)"
             print("Gemini response was blocked or empty.")
        else:
            ai_generated_draft = gemini_response.text # Access text directly
            print(f"Gemini generated draft: {len(ai_generated_draft)} chars")

    except Exception as e:
        print(f"Error during Gemini processing: {e}")
        error_message = f"Gemini processing failed: {type(e).__name__}: {e}"
        return jsonify({"error": error_message}), 500

    return jsonify({
        "ai_generated_draft": ai_generated_draft
    })

@app.route('/save_consultation/<int:patient_id>', methods=['POST'])
def save_consultation(patient_id):
    """Saves the confirmed consultation notes to the database."""
    data = request.json
    if not data or 'final_notes' not in data or 'raw_transcript' not in data or 'doctor_id' not in data:
        return jsonify({"error": "Missing required data (final_notes, raw_transcript, doctor_id)"}), 400

    final_notes = data['final_notes']
    raw_transcript = data['raw_transcript']
    doctor_id = data['doctor_id'] # Get doctor_id from request
    ai_summary = data.get('ai_summary', '') # Optional initial AI summary
    consultation_date = datetime.datetime.now()

    query = ("INSERT INTO Consultation "
             "(patient_id, doctor_id, consultation_date, raw_transcript, ai_summary, final_prescription_notes) "
             "VALUES (%s, %s, %s, %s, %s, %s)")
    params = (patient_id, doctor_id, consultation_date, raw_transcript, ai_summary, final_notes)

    consultation_id = execute_query(query, params)

    if consultation_id:
        return jsonify({"success": True, "consultation_id": consultation_id})
    else:
        return jsonify({"error": "Failed to save consultation to database"}), 500

@app.route('/download_pdf/<int:consultation_id>')
def download_pdf(consultation_id):
    """Generates and sends a PDF for the given consultation."""
    # Fetch consultation details
    query = """
    SELECT c.consultation_date, c.final_prescription_notes,
           p.name AS patient_name, p.dob AS patient_dob,
           d.name AS doctor_name
    FROM Consultation c
    JOIN Patient p ON c.patient_id = p.id
    JOIN User d ON c.doctor_id = d.id
    WHERE c.id = %s
    """
    consultation_data = fetch_one(query, (consultation_id,))

    if not consultation_data:
        return "Consultation not found", 404

    try:
        # --- Generate PDF using FPDF2 ---
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)

        # Header
        pdf.set_font("Helvetica", 'B', 16)
        pdf.cell(0, 10, "Consultation Summary", 0, 1, 'C')
        pdf.ln(10)

        # Patient and Doctor Info
        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(40, 10, "Patient Name:", 0, 0)
        pdf.set_font("Helvetica", '', 12)
        pdf.cell(0, 10, consultation_data['patient_name'], 0, 1)

        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(40, 10, "Patient DOB:", 0, 0)
        pdf.set_font("Helvetica", '', 12)
        pdf.cell(0, 10, str(consultation_data.get('patient_dob', 'N/A')), 0, 1)

        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(40, 10, "Doctor Name:", 0, 0)
        pdf.set_font("Helvetica", '', 12)
        pdf.cell(0, 10, consultation_data['doctor_name'], 0, 1)

        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(40, 10, "Consultation Date:", 0, 0)
        pdf.set_font("Helvetica", '', 12)
        pdf.cell(0, 10, consultation_data['consultation_date'].strftime("%Y-%m-%d %H:%M:%S"), 0, 1)
        pdf.ln(10)

        # Final Notes / Prescription
        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(0, 10, "Final Prescription & Notes:", 0, 1)
        pdf.set_font("Helvetica", '', 12)
        # Use multi_cell for potentially long text
        pdf.multi_cell(0, 7, consultation_data['final_prescription_notes'])
        pdf.ln(5)

        # Save PDF to a BytesIO buffer
        pdf_buffer = io.BytesIO()
        pdf_output = pdf.output(dest='S').encode('latin-1') # Output as bytes
        pdf_buffer.write(pdf_output)
        pdf_buffer.seek(0)

        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=f"consultation_{consultation_id}.pdf",
            mimetype='application/pdf'
        )

    except Exception as e:
        print(f"Error generating PDF: {e}")
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