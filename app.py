import os
import io
import datetime
import time # Ensure time is imported
import re # Import regex for parsing
import json # Import json for handling prescription data
import logging
from functools import wraps # Import wraps for decorators

from dotenv import load_dotenv
from flask import ( # Organize imports
    Flask, request, jsonify, render_template, send_file, 
    redirect, url_for, flash, session, abort 
)
from flask_sock import Sock # Added for WebSockets
# Import WebSocket exceptions
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
from werkzeug.security import generate_password_hash, check_password_hash # For password hashing

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

# --- Login / Auth Helper Functions & Decorators ---

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def role_required(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash("Please log in to access this page.", "warning")
                return redirect(url_for('login', next=request.url))
            if session.get('user_role') != role:
                flash(f"You do not have permission ({role} required) to access this page.", "danger")
                # Redirect to index or a specific 'unauthorized' page if preferred
                return redirect(url_for('index')) 
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# --- PDF Generation Class (Redesigned Layout - Revision 3) ---

class ConsultationPDF(FPDF):
    def __init__(self, doctor_data, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.doctor_data = doctor_data
        self.alias_nb_pages()
        self.set_margins(12, 15, 12)
        self.set_auto_page_break(auto=True, margin=20)
        self.set_font("helvetica", size=9)
        self.line_height = 4.5
        self.section_title_height = self.line_height * 1.3
        self.gap_after_section_title = 1.5
        self.gap_between_sections = 3 # Renamed for clarity
        self.gap_after_final_section = 4
        self.page_content_width = self.w - self.l_margin - self.r_margin
        self.col_width = (self.page_content_width - 6) / 2
        self.col_gap = 6

    def header(self):
        header_y_start = self.get_y()
        col1_width = self.page_content_width * 0.55
        col2_width = self.page_content_width * 0.45
        
        # Left Block: Doctor Details
        self.set_font("helvetica", "B", 11)
        doctor_name = self.doctor_data.get('name', 'Dr. Default')
        self.cell(col1_width, self.line_height + 1, doctor_name, border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
        self.set_font("helvetica", "", 9)
        qualifications = self.doctor_data.get('qualifications', '')
        if qualifications:
             self.multi_cell(col1_width, self.line_height, qualifications, border=0, align='L', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        reg_no = self.doctor_data.get('registration_number', '')
        if reg_no:
             # <<< FIX: Ensure Reg No is Left Aligned within its block >>>
            self.cell(col1_width, self.line_height, f"Reg. No: {reg_no}", border=0, align='L', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        phone = self.doctor_data.get('phone_number', '')
        if phone:
             # <<< FIX: Ensure Mob No is Left Aligned within its block >>>
             self.cell(col1_width, self.line_height, f"Mob. No: {phone}", border=0, align='L', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
             
        y_after_left = self.get_y()
        
        # Right Block: Clinic Details
        self.set_xy(self.l_margin + col1_width, header_y_start)
        self.set_font("helvetica", "B", 11)
        clinic_name = self.doctor_data.get('clinic_name', 'Clinic Name')
        # Use multi_cell for potential wrapping of clinic name
        self.multi_cell(col2_width, self.line_height + 1, clinic_name, border=0, align='R')
        y_after_clinic_name = self.get_y()
        current_x_right = self.l_margin + col1_width
        
        self.set_xy(current_x_right, y_after_clinic_name)
        self.set_font("helvetica", "", 9)
        address = self.doctor_data.get('clinic_address', '')
        if address:
            self.multi_cell(col2_width, self.line_height, address, border=0, align='R')
        timings = self.doctor_data.get('clinic_timings', '')
        if timings:
            self.set_x(current_x_right) # Reset X
            self.cell(col2_width, self.line_height, f"Timing: {timings}", border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        closed = self.doctor_data.get('clinic_closed_days', '')
        if closed:
            self.set_x(current_x_right) # Reset X
            self.cell(col2_width, self.line_height, f"Closed: {closed}", border=0, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
        y_after_right = self.get_y()
        
        # Draw line below header
        self.set_y(max(y_after_left, y_after_right) + 2) # Increased gap
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4) # Increased gap

    def footer(self):
        self.set_y(-18) # Position further up for signature space
        
        # Page Number (Centered)
        page_num_y = self.get_y()
        self.set_font("helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C", border=0)
        
        # Signature Area (Bottom Right) - Adjusted Y positioning relative to bottom
        sig_width = 60
        sig_x = self.w - self.r_margin - sig_width
        sig_y = self.h - self.b_margin - 15 # Y position for the signature line itself
        
        self.set_y(sig_y) # Go to Y pos for signature line
        self.line(sig_x, sig_y, sig_x + sig_width, sig_y) # Draw signature line
        
        # Position text below the line
        self.set_xy(sig_x, sig_y + 1) 
        self.set_font("helvetica", "B", 9)
        doctor_name = self.doctor_data.get('name', 'Dr. Default')
        self.cell(sig_width, self.line_height, doctor_name, align="C", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
        self.set_x(sig_x)
        self.set_font("helvetica", "", 8)
        qualifications = self.doctor_data.get('qualifications', '')
        if qualifications:
            self.cell(sig_width, self.line_height, qualifications, align="C", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        reg_no = self.doctor_data.get('registration_number', '')
        if reg_no:
            self.set_x(sig_x)
            self.cell(sig_width, self.line_height, f"Reg. No.: {reg_no}", align="C", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
        # Reset Y to avoid interfering with page numbering if content was short
        self.set_y(page_num_y)

    def add_patient_details(self, name, patient_id, dob, gender):
        self.set_font("helvetica", "", 9)
        id_text = f"ID: {patient_id}"
        id_width = self.get_string_width(id_text) + 5
        self.cell(id_width, self.line_height, id_text)
        name_text = f"{name}"
        name_width = self.get_string_width(name_text) + 5
        self.cell(name_width, self.line_height, name_text)
        age = ''
        if isinstance(dob, (datetime.date, datetime.datetime)):
            today = datetime.date.today()
            age_years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            age = f"({age_years} Yrs)"
        gender_age_text = f"{gender} / {age}"
        self.cell(0, self.line_height, gender_age_text, align='R')
        self.ln(self.line_height + 1)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4) # Increased gap

    def add_consultation_date(self, date):
        self.set_font("helvetica", "B", 9)
        date_str = date.strftime('%d-%b-%Y %I:%M %p') if isinstance(date, datetime.datetime) else str(date)
        current_y = self.get_y()
        # Position X for right alignment before calling cell
        date_width = self.get_string_width(f"Date: {date_str}")
        self.set_x(self.w - self.r_margin - date_width)
        self.cell(date_width, self.line_height, f"Date: {date_str}", align="R")
        # Use ln() to move below the date cell's height
        self.ln(self.line_height + 3)

    def add_vitals(self, vitals_dict):
        self.set_font("helvetica", "B", 10)
        title = "Vitals"
        title_width = self.get_string_width(title) + 2
        self.cell(title_width, self.section_title_height, title)
        self.ln(self.section_title_height + self.gap_after_section_title)
        
        if not vitals_dict: 
            self.set_font("helvetica", "I", 9)
            self.cell(0, self.line_height, "(No vitals recorded)")
            self.ln(self.line_height + self.gap_between_sections)
            # No line below vitals
            return
        
        self.set_font("helvetica", "", 9)
        bp = f"BP: {vitals_dict.get('bp_systolic', '--')}/{vitals_dict.get('bp_diastolic', '--')} mmHg"
        hr = f"HR: {vitals_dict.get('heart_rate', '--')} bpm"
        temp = f"Temp: {vitals_dict.get('temperature', '--')} °C"
        spo2 = f"SpO2: {vitals_dict.get('spo2', '--')} %"
        weight = f"Wt: {vitals_dict.get('weight_kg', '--')} kg"
        height = f"Ht: {vitals_dict.get('height_cm', '--')} cm"
        
        # 3-column layout for vitals
        vitals_col_width = self.page_content_width / 3
        # Row 1
        self.cell(vitals_col_width, self.line_height, bp)
        self.cell(vitals_col_width, self.line_height, hr)
        self.cell(vitals_col_width, self.line_height, temp, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        # Row 2
        self.cell(vitals_col_width, self.line_height, spo2)
        self.cell(vitals_col_width, self.line_height, weight)
        self.cell(vitals_col_width, self.line_height, height, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
        self.ln(self.gap_between_sections)

    def add_section_in_column(self, title, content, width):
        start_x = self.get_x() # Remember column starting X
        y_start = self.get_y()
        
        # --- Draw Title --- 
        self.set_font("helvetica", "B", 10)
        self.cell(width, self.section_title_height, title, border="B", align="L")
        # Y position after title and gap
        y_after_title = y_start + self.section_title_height + self.gap_after_section_title
        self.set_xy(start_x, y_after_title)
            
        # --- Draw Content --- 
        if content:
            self.set_font("helvetica", "", 9)
            # Use split_lines=True to calculate height correctly before drawing
            # This is more reliable than relying on get_y() immediately after multi_cell with ln=3
            lines = self.multi_cell(width, self.line_height, str(content), border=0, align="L", split_only=True)
            num_lines = len(lines)
            content_height = num_lines * self.line_height
            
            # Check for page break BEFORE drawing the content multi_cell
            if self.get_y() + content_height > self.page_break_trigger:
                # This section is too long to fit, ideally handle this better
                # For now, let auto-page-break handle it during the actual multi_cell draw
                # Or add logic here to draw title on new page if content won't fit below it
                pass 
                
            # Draw the actual content
            self.multi_cell(width, self.line_height, str(content), border=0, align="L")
            # Calculate Y position after drawing the content
            y_after_content = y_after_title + content_height
            self.set_y(y_after_content)
        else: # No content, Y position is just after title gap
            self.set_y(y_after_title)
            
        self.set_x(start_x) # Reset X to column start
        return self.get_y() # Return final Y position after drawing this section

    def add_full_width_section(self, title, content):
         if content:
            self.set_font("helvetica", "B", 10)
            self.cell(0, self.section_title_height, title, border="B", align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(self.gap_after_section_title)
            self.set_font("helvetica", "", 9)
            self.multi_cell(0, self.line_height, str(content))
            self.ln(self.gap_between_sections)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(2)

    def add_two_column_sections(self, left_sections, right_sections):
        y_start_columns = self.get_y()
        y_left_ends = []
        y_right_ends = []
        left_items = list(left_sections.items())
        right_items = list(right_sections.items())
        
        # Draw Left Column Sections
        current_x_left = self.l_margin
        self.set_xy(current_x_left, y_start_columns)
        current_y_left = y_start_columns
        for i, (title, content) in enumerate(left_items):
            self.set_xy(current_x_left, current_y_left) # Set position for this section
            current_y_left = self.add_section_in_column(title, content, self.col_width)
            y_left_ends.append(current_y_left) # Store end Y for each section
            # Add gap between sections in the same column
            if i < len(left_items) - 1:
                 current_y_left += self.gap_between_sections
            
        # Set position for Right Column Sections
        current_x_right = self.l_margin + self.col_width + self.col_gap
        self.set_xy(current_x_right, y_start_columns) # Start right col at same Y
        current_y_right = y_start_columns
        for i, (title, content) in enumerate(right_items):
            self.set_xy(current_x_right, current_y_right) # Set position for this section
            current_y_right = self.add_section_in_column(title, content, self.col_width)
            y_right_ends.append(current_y_right)
             # Add gap between sections in the same column
            if i < len(right_items) - 1:
                 current_y_right += self.gap_between_sections
            
        # Determine the overall final Y position based on the tallest column's last section end
        final_y = max(y_left_ends[-1] if y_left_ends else y_start_columns, 
                      y_right_ends[-1] if y_right_ends else y_start_columns)
        self.set_y(final_y)
        self.ln(self.gap_after_final_section) # Use final gap after the whole block
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def add_prescription_table(self, prescriptions):
        self.set_font("helvetica", "B", 10)
        self.cell(0, self.section_title_height, "Prescription (Rx)", border="B", align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(self.gap_after_section_title)
        if not prescriptions: 
            self.set_font("helvetica", "I", 9)
            self.cell(0, self.line_height, "(No prescription details recorded)")
            self.ln(self.line_height + self.gap_between_sections)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(2)
            return

        self.set_font("helvetica", "B", 9)
        total_width = self.page_content_width
        sno_width = 8
        med_width = total_width * 0.35 
        dos_width = total_width * 0.15
        instr_width = total_width - sno_width - med_width - dos_width # Remaining width
        col_widths = (sno_width, med_width, dos_width, instr_width)
        headers = ("S.No.", "Medicine", "Dosage", "Instructions")
        
        # Draw Table Header
        for i, header in enumerate(headers):
            self.cell(col_widths[i], self.line_height * 1.3, header, border=1, align="C")
        self.ln()

        self.set_font("helvetica", "", 9)
        for idx, med in enumerate(prescriptions):
            y_before = self.get_y()
            x_before = self.get_x()
            cell_padding = 1.2 
            
            sno = str(idx + 1) + "."
            # Use correct keys from JSON
            text_medicine = str(med.get('medicine_name', ''))
            text_dosage = str(med.get('dosage', ''))
            # Combine frequency, duration, instructions
            freq = med.get('frequency', '')
            dur = med.get('duration', '')
            instr = med.get('instructions', '')
            text_instructions = f"{freq}{' for ' + dur if dur else ''}{('. ' + instr) if instr else ''}".strip()
            if not text_instructions: # Handle empty case
                 text_instructions = dur # Fallback to just duration if freq/instr are empty
            
            # Calculate max lines needed using multi_cell dry run
            max_lines = 1
            all_texts = [text_medicine, text_dosage, text_instructions]
            all_widths = [col_widths[1], col_widths[2], col_widths[3]]
            for i, text in enumerate(all_texts):
                 lines = self.multi_cell(all_widths[i], self.line_height, text, border=0, align='L', dry_run=True, output='LINES')
                 max_lines = max(max_lines, len(lines))
            
            row_height = max_lines * self.line_height * cell_padding
            row_height = max(row_height, self.line_height * 1.4) # Min height

            # Page break check
            if self.get_y() + row_height > self.page_break_trigger:
                 self.add_page()
                 self.set_font("helvetica", "B", 9)
                 for i, header in enumerate(headers):
                     self.cell(col_widths[i], self.line_height * 1.3, header, border=1, align="C")
                 self.ln()
                 self.set_font("helvetica", "", 9)
                 y_before = self.get_y()
                 x_before = self.get_x()

            # Draw cells
            current_x = x_before
            self.set_y(y_before)
            self.multi_cell(col_widths[0], row_height, sno, border='L', align='C', ln=3, max_line_height=self.line_height)
            self.set_xy(current_x + col_widths[0], y_before)
            self.multi_cell(col_widths[1], row_height, text_medicine, border='L', align='L', ln=3, max_line_height=self.line_height)
            self.set_xy(current_x + col_widths[0] + col_widths[1], y_before)
            self.multi_cell(col_widths[2], row_height, text_dosage, border='L', align='L', ln=3, max_line_height=self.line_height)
            self.set_xy(current_x + col_widths[0] + col_widths[1] + col_widths[2], y_before)
            self.multi_cell(col_widths[3], row_height, text_instructions, border='LR', align='L', ln=3, max_line_height=self.line_height)
            
            self.line(x_before, y_before + row_height, x_before + sum(col_widths), y_before + row_height)
            self.set_y(y_before + row_height)
        
        self.ln(self.gap_between_sections)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def add_follow_up(self, date):
        if not date: return
        self.set_font("helvetica", "B", 9)
        date_str = date.strftime('%d-%b-%Y') if isinstance(date, datetime.date) else str(date)
        self.cell(0, self.line_height, f"Follow-up advised on: {date_str}", align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(self.gap_after_final_section)
        # No line needed if this is the last item before footer

# --- Flask Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        error = None

        if not email or not password:
            error = 'Email and Password are required.'
            flash(error, 'danger')
            return render_template('login.html')

        user = fetch_one("SELECT id, name, email, role, password_hash FROM User WHERE email = %s", (email,))

        if user is None or not check_password_hash(user['password_hash'], password):
            error = 'Incorrect email or password.'
            flash(error, 'danger')
            return render_template('login.html')
        
        # Login successful - store user info in session
        session.clear()
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['user_email'] = user['email']
        session['user_role'] = user['role']
        flash(f"Welcome back, {user['name']}!", 'success')

        # Redirect to appropriate page based on role
        if user['role'] == 'doctor':
            return redirect(url_for('index')) # Doctor goes to patient dashboard
        elif user['role'] == 'operator':
             return redirect(url_for('check_in_dashboard')) # Operator goes to check-in
        else: # Patient role (if implemented later)
             return redirect(url_for('index')) # Or a patient-specific view

    # GET request or failed login
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

@app.route('/')
@login_required # Protect the dashboard
def index():
    """Renders the main dashboard page (now primarily for doctors)."""
    # --- DEBUGGING LOG --- 
    logging.info(f"Accessing index route. Session User ID: {session.get('user_id')}, Role: {session.get('user_role')}")
    
    # Redirect Operators away from doctor dashboard
    if session['user_role'] == 'operator':
        logging.info(f"User is an operator, redirecting to check_in_dashboard.")
        return redirect(url_for('check_in_dashboard'))
    elif session['user_role'] != 'doctor':
        # Redirect other roles (like patient) if needed
        logging.warning(f"User role '{session['user_role']}' is not doctor, redirecting to login.")
        flash("Access denied.", "danger")
        return redirect(url_for('login'))
        
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
    
    # --- DEBUGGING LOG ---
    logging.info(f"User is a doctor, proceeding to render index.html.")
    
    return render_template(
        'index.html', 
        patients=patients, 
        todays_count=todays_consultations_count
    )

@app.route('/consultation/<int:patient_id>')
@login_required # Protect consultation page
# @role_required('doctor') # Assuming only doctors consult for now
def consultation_page(patient_id):
    """Renders the main consultation page for a specific patient."""
    # Check if user is a doctor (can be refined based on requirements)
    if session.get('user_role') != 'doctor':
        flash("Only doctors can access the consultation page.", "danger")
        return redirect(url_for('index'))
        
    patient = fetch_one("SELECT id, name FROM Patient WHERE id = %s", (patient_id,))
    if not patient:
        flash(f"Patient with ID {patient_id} not found.", "error")
        return redirect(url_for('index'))
        
    # Get doctor_id from session now
    doctor_id = session.get('user_id') 
    if not doctor_id: # Should be caught by @login_required, but extra check
        flash("Session error. Please log in again.", "warning")
        return redirect(url_for('login'))
        
    return render_template('consultation.html', patient=patient, patient_id=patient_id, doctor_id=doctor_id)

# UPDATED Route: Process accumulated transcript text via Gemini
@app.route('/process_transcript_text', methods=['POST'])
@login_required # Secure this endpoint
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
@login_required # Secure this endpoint
def save_consultation():
    """Saves the confirmed structured consultation data to the database."""
    data = request.json
    # Get doctor_id from session
    doctor_id = session.get('user_id')

    # Validate session
    if not doctor_id:
        # This should ideally not be reached due to @login_required
        return jsonify({"error": "User not logged in or session expired"}), 401 # Unauthorized
        
    # Validate role (ensure only doctor saves consultation)
    if session.get('user_role') != 'doctor':
        return jsonify({"error": "Only doctors can save consultations."}), 403 # Forbidden

    # doctor_id = 1 # <<< REMOVE TEMPORARY HARDCODED DOCTOR ID FOR MVP

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
@login_required
@role_required('doctor') # Only doctors can access settings
def settings_page():
    """Displays the settings page for the logged-in doctor."""
    # Get doctor ID from session
    doctor_id = session.get('user_id')
    # doctor_id = 1 # <<< REMOVE HARDCODED DOCTOR ID
    
    doctor_details = fetch_one("SELECT * FROM User WHERE id = %s", (doctor_id,))
    
    if not doctor_details:
        flash("Doctor details not found.", "error")
        return redirect(url_for('index')) # Redirect if doctor doesn't exist
        
    return render_template('settings.html', doctor=doctor_details)

@app.route('/update_settings', methods=['POST'])
@login_required
@role_required('doctor') # Only doctors can update settings
def update_settings():
    """Updates the settings for the logged-in doctor."""
    # Get doctor ID from session
    doctor_id = session.get('user_id')
    # doctor_id = 1 # <<< REMOVE HARDCODED DOCTOR ID

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
@login_required
def download_pdf(consultation_id):
    """Generates and returns a PDF for a specific consultation."""
    try:
        # 1. Fetch Consultation Data
        consultation_query = """
            SELECT c.*, p.name as patient_name, p.dob as patient_dob, p.gender as patient_gender
            FROM Consultation c
            JOIN Patient p ON c.patient_id = p.id
            WHERE c.id = %s
        """
        consultation_data = fetch_one(consultation_query, (consultation_id,))
        if not consultation_data: return jsonify({"error": "Consultation not found"}), 404

        # 2. Fetch Doctor/Clinic Data (as before)
        doctor_id = consultation_data.get('doctor_id', 1)
        doctor_query = """
            SELECT name, email, phone_number, registration_number, qualifications,
                   clinic_name, clinic_address, clinic_timings, clinic_closed_days
            FROM User WHERE id = %s AND role = 'doctor' 
        """
        doctor_data = fetch_one(doctor_query, (doctor_id,))
        if not doctor_data: doctor_data = fetch_one(doctor_query, (1,))
        if not doctor_data:
            doctor_data = {'name': 'Dr. Default', 'qualifications': '', 'registration_number': '', 'clinic_name': 'Default Clinic', 'clinic_address': '', 'clinic_timings': '', 'clinic_closed_days': ''}

        # 3. Fetch Latest Vitals (as before)
        latest_vitals = None
        vitals_query = "SELECT * FROM Vitals WHERE patient_id = %s ORDER BY checkin_time DESC LIMIT 1"
        latest_vitals = fetch_one(vitals_query, (consultation_data['patient_id'],))
        if latest_vitals is None: latest_vitals = {}

        # 4. Create PDF
        pdf = ConsultationPDF(doctor_data)
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)

        # Add Patient Details & Date (using updated methods)
        pdf.add_patient_details(consultation_data['patient_name'], consultation_data['patient_id'], consultation_data['patient_dob'], consultation_data['patient_gender'])
        pdf.add_consultation_date(consultation_data['consultation_date'])
        
        # Add Vitals Section (using updated method)
        pdf.add_vitals(latest_vitals)
        
        # --- Add Consultation Sections in Two Columns --- 
        left_column_data = {
            "Chief Complaints": consultation_data.get('chief_complaints', ''),
            "Clinical Findings": consultation_data.get('clinical_findings', ''),
            "Procedures Conducted": consultation_data.get('procedures_conducted', '')
        }
        right_column_data = {
            "Diagnosis": consultation_data.get('diagnosis', ''),
            "Investigations": consultation_data.get('investigations', ''),
            "Advice Given": consultation_data.get('advice_given', '')
        }
        pdf.add_two_column_sections(left_column_data, right_column_data)
        # --- End Two Column Section --- 
        
        # Add Prescription Table (Full Width)
        prescription_details = consultation_data.get('prescription_details')
        if isinstance(prescription_details, str): # Handle JSON string from DB
            try: prescription_details = json.loads(prescription_details)
            except json.JSONDecodeError: prescription_details = None
        
        if prescription_details and isinstance(prescription_details, list) and len(prescription_details) > 0:
             pdf.add_prescription_table(prescription_details)
        else:
             # Add note if no prescription (using full_width_section for consistency)
             pdf.add_full_width_section("Prescription", "(No prescription details recorded)")

        # Add Follow-up Date (Full Width)
        if consultation_data.get('follow_up_date'):
            pdf.add_follow_up(consultation_data['follow_up_date'])

        # Generate PDF output
        pdf_output = pdf.output()

        # Send PDF as a file download
        return send_file(
            io.BytesIO(pdf_output),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'Consultation_{consultation_data["patient_name"]}_{consultation_id}.pdf'
        )

    except Exception as e:
        print(f"Error generating PDF: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Failed to generate PDF: {e}"}), 500

# --- Helper to fetch latest vitals before a certain time ---
def get_latest_vitals(patient_id, before_time):
    query = """SELECT * FROM Vitals 
               WHERE patient_id = %s AND checkin_time < %s 
               ORDER BY checkin_time DESC 
               LIMIT 1"""
    return fetch_one(query, (patient_id, before_time))

# --- WebSocket Route for Live Transcription Demo ---
@sock.route('/live_transcript')
@login_required # Secure WebSocket endpoint
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

# --- New EOD Summary Route --- (Used by Doctor Dashboard)
@app.route('/get_eod_data')
@login_required
@role_required('doctor') # Only doctors view EOD summary
def get_eod_data():
    """Fetches details of consultations created today and doctor info."""
    # Get doctor ID from session
    doctor_id = session.get('user_id')
    # doctor_id = 1 # <<< REMOVE HARDCODED DOCTOR ID
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

# --- New Route for Live ADR Check --- (Used by Consultation Page)
@app.route('/check_adr', methods=['POST'])
@login_required # Secure this endpoint
def check_adr():
    # Optional: Add role check if needed (e.g., ensure only doctor triggers)
    # if session.get('user_role') != 'doctor':
    #     return jsonify({"error": "Unauthorized"}), 403
    
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

# --- Check-in / Vitals Routes (Operator Interface) ---

@app.route('/check-in')
@login_required
@role_required('operator') # Only operators access check-in
def check_in_dashboard():
    """Displays the operator check-in dashboard."""
    # Fetch patients for selection
    patients = fetch_all("SELECT id, name, dob FROM Patient ORDER BY name")
    return render_template('check_in.html', patients=patients)

@app.route('/add_patient', methods=['GET', 'POST'])
@login_required
# Allow both Doctor and Operator to add patients
# @role_required('operator') 
def add_patient():
    """Handles adding a new patient."""
    if session.get('user_role') not in ['doctor', 'operator']:
        flash("You do not have permission to add patients.", "danger")
        return redirect(url_for('index'))

    if request.method == 'POST':
        name = request.form.get('name')
        dob = request.form.get('dob') # Expect YYYY-MM-DD
        gender = request.form.get('gender')
        address = request.form.get('address')

        if not name or not dob or not gender:
            flash("Patient Name, Date of Birth, and Gender are required.", "error")
            # Return the form again, perhaps prepopulating if needed
            return render_template('add_patient.html') 

        # Validate DOB format (basic)
        try:
            datetime.datetime.strptime(dob, '%Y-%m-%d')
        except ValueError:
             flash("Invalid Date of Birth format. Please use YYYY-MM-DD.", "error")
             return render_template('add_patient.html')
             
        # Validate Gender
        if gender not in ['M', 'F', 'O']:
            flash("Invalid Gender selected.", "error")
            return render_template('add_patient.html')

        query = "INSERT INTO Patient (name, dob, gender, address) VALUES (%s, %s, %s, %s)"
        params = (name, dob, gender, address)
        patient_id = execute_query(query, params)

        if patient_id:
            flash(f"Patient '{name}' added successfully (ID: {patient_id}).", "success")
            # Redirect back to appropriate dashboard based on role
            if session['user_role'] == 'operator':
                 return redirect(url_for('check_in_dashboard'))
            else: # Doctor
                 return redirect(url_for('index'))
        else:
            flash("Error adding patient to database.", "error")
            return render_template('add_patient.html')

    # GET request
    return render_template('add_patient.html')

@app.route('/record_vitals', methods=['POST'])
@login_required
@role_required('operator') # Only operators record vitals
def record_vitals():
    """Records patient vitals submitted from the check-in form."""
    patient_id = request.form.get('patient_id')
    bp_systolic = request.form.get('bp_systolic', type=int)
    bp_diastolic = request.form.get('bp_diastolic', type=int)
    heart_rate = request.form.get('heart_rate', type=int)
    temperature = request.form.get('temperature', type=float)
    spo2 = request.form.get('spo2', type=int)
    weight_kg = request.form.get('weight_kg', type=float)
    height_cm = request.form.get('height_cm', type=float)
    notes = request.form.get('notes')
    operator_id = session.get('user_id') # Get the ID of the operator recording vitals
    checkin_time = datetime.datetime.now() # Get current timestamp
    checkin_date = checkin_time.date() # <<< Extract date part

    if not patient_id:
        flash("Please select a patient.", "danger")
        return redirect(url_for('check_in_dashboard'))

    # Ensure at least one vital sign is entered (optional, based on requirements)
    if not any([bp_systolic, bp_diastolic, heart_rate, temperature, spo2, weight_kg, height_cm]):
        flash("Please enter at least one vital sign.", "warning")
        # Redirect back, potentially preserving selected patient?
        return redirect(url_for('check_in_dashboard'))

    # Insert into Vitals table (including the new checkin_date)
    query = """INSERT INTO Vitals (
                   patient_id, checkin_date, checkin_time, operator_id, bp_systolic, bp_diastolic, 
                   heart_rate, temperature, spo2, weight_kg, height_cm, notes
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
    params = (
        patient_id, checkin_date, checkin_time, operator_id, bp_systolic, bp_diastolic, 
        heart_rate, temperature, spo2, weight_kg, height_cm, notes
    )
    
    vital_id = execute_query(query, params)

    if vital_id:
        flash("Vitals recorded successfully.", "success")
    else:
        flash("Error recording vitals.", "danger")

    return redirect(url_for('check_in_dashboard'))


# --- User Management Routes (Doctor Only) ---
@app.route('/manage_users')
@login_required
@role_required('doctor')
def manage_users():
    """Renders the user management hub page (for doctors)."""
    # No data fetching needed for the hub page itself
    # patients = fetch_all("SELECT id, name FROM Patient ORDER BY name") # <<< Remove this line
    return render_template('manage_users.html') # Pass only the template

@app.route('/manage_operators')
@login_required
@role_required('doctor')
def manage_operators():
    """Displays a list of operators for management."""
    # Fetch operators (users with role 'operator') from DB
    operators = fetch_all("SELECT id, name, email FROM User WHERE role = %s ORDER BY name", ('operator',))
    # Render the specific template for managing operators
    return render_template('manage_operators.html', operators=operators)
    # flash("Operator management page not yet implemented.", "info") # Remove placeholder
    # return redirect(url_for('manage_users')) # Remove placeholder

@app.route('/manage_patients')
@login_required
@role_required('doctor')
def manage_patients():
    """Displays a list of patients for management."""
    # Fetch all patients from DB
    patients = fetch_all("SELECT id, name, dob, gender, address FROM Patient ORDER BY name")
    # Render the specific template for managing patients
    return render_template('manage_patients.html', patients=patients)
    # flash("Patient management page not yet implemented.", "info") # Remove placeholder
    # return redirect(url_for('manage_users')) # Remove placeholder

@app.route('/add_user', methods=['GET', 'POST'])
@login_required
@role_required('doctor') # Only doctors can add operators
def add_user():
    """Handles adding a new user with the 'operator' role."""
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        role = 'operator' # Hardcode role to operator
        error = None

        if not name or not email or not password:
            error = 'Name, Email, and Password are required.'
        
        # Check if email already exists
        if not error:
            existing_user = fetch_one("SELECT id FROM User WHERE email = %s", (email,))
            if existing_user:
                error = f'Email "{email}" is already registered.'

        if error:
            flash(error, 'danger')
            # Return the add_user template again, pre-filling name/email if possible
            return render_template('add_user.html', name=name, email=email)
        else:
            # Hash the password
            password_hash = generate_password_hash(password)

            # Insert new operator into User table
            query = """INSERT INTO User (name, email, password_hash, role)
                       VALUES (%s, %s, %s, %s)"""
            user_id = execute_query(query, (name, email, password_hash, role))

            if user_id:
                flash(f'Operator "{name}" added successfully.', 'success')
                return redirect(url_for('manage_operators')) # Redirect to operator list
            else:
                flash('Error adding operator to the database.', 'danger')
                return render_template('add_user.html', name=name, email=email)

    # GET request: just render the empty form
    return render_template('add_user.html')

# --- Run the App ---
if __name__ == '__main__':
    # Use werkzeug server for WebSocket support if not using flask run
    # Or use a production server like gunicorn with gevent/eventlet
    print("Starting Flask app with WebSocket support...")
    # Make sure debug=False for production!
    app.run(debug=True, port=5001, host='0.0.0.0') # Host needed for sock 