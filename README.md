# Smart Care Assistant - Upai Consultation Module

This application implements Part 2 of the Smart Care Assistant project: a web interface for doctors to record dictation during/after patient consultations, get AI-generated summaries and prescription drafts, edit them, save them to a database, and export them as PDFs.

## Technology Stack

*   **Backend:** Python 3.x, Flask
*   **Database:** MySQL (`mysql-connector-python`)
*   **Cloud APIs:**
    *   Google Cloud Speech-to-Text (STT)
    *   Google Generative AI (Gemini)
*   **Frontend:** HTML, CSS, JavaScript (Vanilla JS with `MediaRecorder` and `fetch`)
*   **PDF Generation:** `FPDF2`
*   **Environment Variables:** `python-dotenv`

## Setup Instructions

1.  **Clone the Repository (or ensure files are in one directory):**
    Make sure you have `app.py`, `requirements.txt`, `database_setup.sql`, `.env`, and the `templates/` directory (`index.html`, `consultation.html`) in your project folder.

2.  **Create a Virtual Environment (Recommended):**
    ```bash
    python -m venv venv
    # Activate the virtual environment
    # Windows (Command Prompt/PowerShell)
    .\venv\Scripts\activate
    # macOS/Linux
    source venv/bin/activate
    ```

3.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Set up MySQL Database (GCP Hosted):**
    *   Ensure your GCP MySQL instance is running and accessible.
    *   Connect to your GCP MySQL instance using a suitable client (e.g., Cloud Shell `gcloud sql connect`, MySQL Workbench, DBeaver).
    *   Create the database (if it doesn't exist):
        ```sql
        CREATE DATABASE IF NOT EXISTS upai_consultations;
        ```
    *   Select the database:
        ```sql
        USE upai_consultations;
        ```
    *   Run the `database_setup.sql` script against your `upai_consultations` database to create the necessary tables (`User`, `Patient`, `Consultation`).
        *   Example using mysql client (after connecting): `source /path/to/your/database_setup.sql;`
        *   Or copy/paste the contents into your SQL client.
    *   **Important:** The `database_setup.sql` script includes sample data (commented out). If you want to test, uncomment the `INSERT` statements. Ensure you have a `User` with `id=1` (or adjust `app.py`'s default `doctor_id`).

5.  **Configure Google Cloud Credentials:**
    *   **Service Account Key (for STT):**
        *   Ensure the `steadility-438470812d47.json` file (or your service account key file) is accessible.
        *   Confirm the service account has necessary permissions (e.g., `roles/speech.recognizer`).
    *   **Gemini API Key:**
        *   Ensure you have a valid Gemini API Key generated from Google AI Studio or GCP Console.

6.  **Configure `.env` File:**
    *   Open the `.env` file (created in the previous step).
    *   Fill in the required values:
        *   `GOOGLE_APPLICATION_CREDENTIALS`: The *full path* to your service account JSON key file (e.g., `E:/SSP/Projects/Upai/Prompts/steadility-438470812d47.json`). **Use forward slashes `/` or double backslashes `\\` in the path, especially on Windows.**
        *   `GCP_PROJECT_ID`: Your Google Cloud project ID.
        *   `GEMINI_API_KEY`: Your Gemini API key.
        *   `DB_HOST`: Your GCP MySQL instance's IP address or hostname.
        *   `DB_USER`: Your GCP MySQL username.
        *   `DB_PASSWORD`: Your GCP MySQL password.
        *   `DB_NAME`: `upai_consultations` (or the name you used).

## Running the Application

1.  **Ensure your virtual environment is active.**
2.  **Make sure your GCP MySQL instance is running and accessible.**
3.  **Run the Flask development server:**
    ```bash
    python app.py
    ```
4.  **Open your web browser** and navigate to `http://127.0.0.1:5001/` (or the address shown in the terminal).

## How to Use

1.  The homepage (`/`) will list patients fetched from the `Patient` table. Click on a patient's name to start a consultation.
2.  On the consultation page (`/consultation/<patient_id>`):
    *   Click "Start Recording" to begin capturing audio using your microphone.
    *   Dictate the consultation notes.
    *   Click "Stop Recording & Process".
    *   Wait for the audio to be transcribed (STT) and summarized (Gemini).
    *   Edit the "AI Generated Draft (Editable)" textarea.
    *   Click "Confirm & Save Consultation" to save to the `upai_consultations` database.
    *   Click "Download PDF" to get the summary PDF.

## Notes

*   **Firewall Rules:** Ensure your GCP MySQL instance's firewall rules allow connections from the machine running the Flask app (e.g., your local machine's IP or App Engine/Cloud Run IP if deployed).
*   **Security:** Production environments require proper authentication, input validation, etc.
*   **Audio Format:** Adjust `RecognitionConfig` in `app.py` if browser audio format differs significantly from WAV.
*   **Doctor ID:** The `doctor_id` is hardcoded to `1`. Integrate with a real user system.
*   **Costs:** Monitor Google Cloud STT and Gemini API usage. 