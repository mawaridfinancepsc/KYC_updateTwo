import os
import io
import base64
import requests
from flask import Flask, request, jsonify, render_template
from google.oauth2 import service_account
import google.auth.transport.requests
import fitz  # PyMuPDF
import gspread
import re
from datetime import datetime
import json
from io import BytesIO
import fitz  # PyMuPDF
import requests
# Disable SSL warnings

from google.cloud import vision
from google.oauth2 import service_account
from flask import Flask, render_template, request, redirect, flash
import re
import requests
from dotenv import load_dotenv

load_dotenv()   

API_KEY = os.getenv("API_KEY")
ORGCODE = os.getenv("ORGCODE")
DBID = os.getenv("DBID")
TABLEID = os.getenv("TABLEID")
LSQ_ACCESS_KEY = os.getenv("LSQ_ACCESS_KEY")
LSQ_SECRET_KEY = os.getenv("LSQ_SECRET_KEY")
app = Flask(__name__)
app.secret_key = "supersecret"  # for flash messages

# LSQ credentials


def log_error_to_mavis(mobile,  error_message):
    insert_url = f"https://mavis-rest-in21.leadsquared.com/api/{DBID}/{TABLEID}/rows?orgcode={ORGCODE}&append=true"
    headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}

    api_row = [
        {"ColumnId": "emirates_id_number", "ColumnValue": mobile},
        
        {"ColumnId": "STATUS", "ColumnValue": error_message},
        
    ]

    payload = {
        "Data": [api_row],
        "DateTimeFormat": "yyyy-MM-dd HH:mm:ss"
    }

    requests.post(insert_url, headers=headers, json=payload, verify=False)

# MAVIS / DB credentials here if needed

# ---------------- Helper Functions ----------------
def normalize_mobile(mobile):
    if not mobile:
        return None
    mobile = re.sub(r"[^\d]", "", str(mobile))
    if mobile.startswith("00971"):
        mobile = mobile[2:]
    if mobile.startswith("0") and len(mobile) == 10:
        mobile = "971" + mobile[1:]
    if not mobile.startswith("971") or len(mobile) != 12:
        return None
    return f"+{mobile[:3]}-{mobile[3:]}"

def search_lead_by_phone(phone):
    url = "https://api-in21.leadsquared.com/v2/LeadManagement.svc/Leads.Get"
    params = {"accessKey": LSQ_ACCESS_KEY, "secretKey": LSQ_SECRET_KEY}
    headers = {"Content-Type": "application/json"}
    body = {
        "Parameter": {"LookupName": "Phone", "LookupValue": phone, "SqlOperator": "="},
        "Sorting": {"ColumnName": "CreatedOn", "Direction": "1"},
        "Paging": {"PageIndex": 1, "PageSize": 10}
    }
    response = requests.post(url, headers=headers, params=params, json=body, verify=False)
    if response.status_code != 200:
        return [], f"Failed to retrieve lead: {response.status_code}"
    data = response.json()
    leads = data.get("Leads", []) if isinstance(data, dict) else data
    return leads, None






# In Render, set environment variable GOOGLE_APPLICATION_JSON with the full JSON content

# -----------------------------
# Create credentials
# -----------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "googleserviceacc.json")

# -----------------------------
# Create credentials
# -----------------------------
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE)
# Optional: refresh token manually if needed for REST calls


# ----------------- Vision Client -----------------
def get_vision_client():
    """
    Returns a Google Vision API client using the credentials from environment variable.
    """
    return vision.ImageAnnotatorClient(credentials=credentials)
# ----------------- OCR Function -----------------
def safe_ocr(file_bytes):
    """
    Takes file bytes (PDF first page converted to PNG or image) and returns text using Google Vision OCR.
    """
    if not file_bytes:
        return None

    try:
        client = get_vision_client()
        image = vision.Image(content=file_bytes)
        response = client.document_text_detection(image=image)

        if response.error.message:
            print("Vision API error:", response.error.message)
            return None

        texts = response.text_annotations
        return texts[0].description if texts else None

    except Exception as e:
        print("OCR failed:", e)
        return None

# ----------------- PDF to Image -----------------
def pdf_first_page_to_bytes(pdf_file):
    doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
    pix = doc[0].get_pixmap()
    return pix.tobytes("png")

# ----------------- Normalize Text -----------------
def normalize_text(text):
    text = text.replace('\n', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text



def extract_front_emirates_id(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    data = {
        "emirates_id_number": "",
        "full_name": "",
        "date_of_birth": "",
        "nationality": "",
        "sex": "",
        "issuing_date": "",
        "expiry_date": ""
    }

    # Emirates ID Number
    for l in lines:
        m = re.search(r'784-\d{4}-\d{7}-\d', l)
        if m:
            data["emirates_id_number"] = m.group()
            break

    # Full Name (English)
    for l in lines:
        if l.startswith("Name:"):
            data["full_name"] = l.replace("Name:", "").strip()
            break

    # --- DOB (same line OR next line) ---
    

    # --- Nationality (English FIRST, Arabic fallback) ---
    for l in lines:
        if l.startswith("Nationality"):
            data["nationality"] = l.split()[-1].strip()
            break

    if not data["nationality"]:
        for l in lines:
            if l.startswith("الجنسية"):
                data["nationality"] = l.split(":")[-1].strip()
                break

    # --- Sex ---
    for l in lines:
        if "Sex" in l:
            if "M" in l:
                data["sex"] = "Male"
            elif "F" in l:
                data["sex"] = "Female"
            break
        if l == "ذكر":
            data["sex"] = "Male"
            break
        if l == "أنثى":
            data["sex"] = "Female"
            break

    # --- Collect all dates with positions ---
    date_positions = []
    for i, l in enumerate(lines):
        if re.fullmatch(r'\d{2}/\d{2}/\d{4}', l):
            date_positions.append((i, l))
# Dates (collect all dates in order of appearance)
    dates = []
    for l in lines:
        if re.match(r'\d{2}/\d{2}/\d{4}', l):
            dates.append(l)

    # Emirates ID date logic (reliable)
    if len(dates) >= 1:
        data["date_of_birth"] = dates[0]
    if len(dates) >= 2:
        data["issuing_date"] = dates[1]
    if len(dates) >= 3:
        data["expiry_date"] = dates[2]
    return data




def extract_back_emirates_id(text):
    data = {}
    occ_match = re.search(r'Occupation[:\s]*([A-Za-z\s]+)', text)
    data["occupation"] = occ_match.group(1).strip() if occ_match else ""
    emp_match = re.search(r'Employer[:\s]*([A-Za-z0-9\s.&]+)', text)
    data["employer_or_sponsor"] = emp_match.group(1).strip() if emp_match else ""
    place_match = re.search(r'Issuing Place[:\s]*([A-Za-z\s]+)', text)
    data["issuing_place"] = place_match.group(1).strip() if place_match else ""
    return data
# ------------------- Routes -------------------



# MAVIS

MAVIS_INSERT_URL = f"https://mavis-rest-in21.leadsquared.com/api/{DBID}/{TABLEID}/rows?orgcode={ORGCODE}"

MAVIS_HEADERS = {
    "Content-Type": "application/json",
    "apikey": API_KEY
}

@app.route("/", methods=["GET", "POST"])
def enter_mobile():
    if request.method == "POST":
        mobile_input = request.form.get("mobile")
        phone = normalize_mobile(mobile_input)
        if not phone:
            flash("Invalid mobile number format!", "error")
            return redirect("/")
        
        leads, error = search_lead_by_phone(phone)
        if error:
            flash(error, "error")
            return redirect("/")
        
        if not leads:
            flash("No lead found for this mobile number.", "error")
            return redirect("/")
        
        # Pass the lead info to the upload page
        lead = leads[0]  # choose first matching lead
        lead_id = lead["ProspectID"]  # this is the GUID used for document uploads

        return render_template("upload_form.html", lead=lead, phone=phone, lead_id=lead_id)
        
    return render_template("enter_mobile.html")
def mavis_record_exists_by_phone(phone):
    query_url = f"https://mavis-rest-in21.leadsquared.com/api/{DBID}/{TABLEID}/rows/query?orgcode={ORGCODE}"
    headers = {
        "x-api-key": API_KEY,
        "Content-Type": "application/json"
    }

    page = 1
    page_size = 200
    phone_found = False

    while True:
        payload = {
            "Select": ["Phone"],  # fetch only the Phone column
            "Paging": {"PageSize": page_size, "PageIndex": page}
        }

        resp = requests.post(query_url, headers=headers, json=payload, verify=False)

        if resp.status_code != 200:
            print("MAVIS query failed:", resp.text)
            break

        data = resp.json().get("Data", [])
        rows = data if isinstance(data, list) else data.get("Rows", [])

        if not rows:
            break

        # Normalize phones and check
        for row in rows:
            row_phone = row.get("Phone")  # exact column name
            if row_phone == phone:
                phone_found = True
                break

        if phone_found or len(rows) < page_size:
            break

        page += 1

    return phone_found



@app.route("/upload", methods=["POST"])
def upload_file():
    files = request.files.getlist("id_files")
    lead_id = request.form.get("lead_id")
    phone = request.form.get("phone")

    if not lead_id:
        return "Lead ID not provided", 400

    if not files:
        return "No file uploaded", 400

    if mavis_record_exists_by_phone(phone):
        return render_template(
            "message.html",
            status="error",
            message="Details already submitted for this mobile number. Document upload is not allowed again."
        )

    front_bytes = None
    back_bytes = None
    front_file = None
    back_file = None
    uploaded_files = []

    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
    MAX_FILE_SIZE = 16 * 1024 * 1024  # 10 MB

    # -------------------- FILE LOOP --------------------
    
    
    for f in files:

        # ✅ Skip empty inputs (CRITICAL FIX)
        if not f or not f.filename:
            continue

        filename = f.filename.lower()

        # 1️⃣ Extension check
        if not filename.endswith(tuple(ALLOWED_EXTENSIONS)):
            return render_template(
                "message.html",
                status="error",
                message="Only PDF, JPG, JPEG, and PNG files are allowed."
            )

        # 2️⃣ Size check
        f.seek(0, 2)
        file_size = f.tell()
        f.seek(0)

        if file_size > MAX_FILE_SIZE:
            return render_template(
                "message.html",
                status="error",
                message="File size exceeds 10 MB limit."
            )

        # 3️⃣ Read file safely
        file_bytes = f.read()
        file_stream = BytesIO(file_bytes)

        # -------------------- OCR PREP --------------------
        if filename.endswith(".pdf"):
            doc = fitz.open(stream=file_bytes, filetype="pdf")

            if len(doc) >= 1 and not front_bytes:
                front_bytes = doc[0].get_pixmap().tobytes("png")

            if len(doc) >= 2 and not back_bytes:
                back_bytes = doc[1].get_pixmap().tobytes("png")

        else:
            if not front_bytes:
                front_bytes = file_bytes
            elif not back_bytes:
                back_bytes = file_bytes

        # -------------------- UPLOAD FRONT --------------------
        upload_url = "https://files-in21.leadsquared.com/File/Upload"

        form_data = {
            "FileType": 7,
            "AccessKey": LSQ_ACCESS_KEY,
            "SecretKey": LSQ_SECRET_KEY,
            "FileStorageType": 0,
            "EnableResize": "false",
            "Id": lead_id,
            "SchemaName": "mx_CustomObject_2",
            "EntitySchemaName": "mx_AECB_Report",
            "Entity": 0,
            "StorageVersion": 0
        }
        if front_bytes:
            file_stream.seek(0)
            files_payload = {
                "uploadFiles": (f.filename, file_stream, f.content_type)
            }
    
            lsq_resp = requests.post(
                upload_url,
                data=form_data,
                files=files_payload,
                verify=False
            )
    
            if lsq_resp.status_code == 200:
                result = lsq_resp.json()
                uploaded_file_name = result.get("uploadedFile") or result.get("UploadedFile")
                uploaded_files.append(uploaded_file_name)
            else:
                log_error_to_mavis(
                    mobile=phone,
                    error_message=lsq_resp.text
                )
                return render_template(
                    "message.html",
                    status="error",
                    message="Document upload failed. Please try again."
                )
        upload_url = "https://files-in21.leadsquared.com/File/Upload"

        form_data2 = {
            "FileType": 7,
            "AccessKey": LSQ_ACCESS_KEY,
            "SecretKey": LSQ_SECRET_KEY,
            "FileStorageType": 0,
            "EnableResize": "false",
            "Id": lead_id,
            "SchemaName": "mx_CustomObject_2",
            "EntitySchemaName": "mx_AECB_Report",
            "Entity": 0,
            "StorageVersion": 0
        }
        if back_bytes:
            file_stream.seek(0)
            files_payload = {
                "uploadFiles": (f.filename, file_stream, f.content_type)
            }
    
            lsq_resp = requests.post(
                upload_url,
                data=form_data2,
                files=files_payload,
                verify=False
            )
    
            if lsq_resp.status_code == 200:
                result = lsq_resp.json()
                uploaded_file_name = result.get("uploadedFile") or result.get("UploadedFile")
                uploaded_files.append(uploaded_file_name)
            else:
                log_error_to_mavis(
                    mobile=phone,
                    error_message=lsq_resp.text
                )
                return render_template(
                    "message.html",
                    status="error",
                    message="Document upload failed. Please try again."
                )

 
    # -------------------- UPDATE LEAD --------------------
    if uploaded_files:
        update_url = "https://api-in21.leadsquared.com/v2/LeadManagement.svc/Lead.Update"
        payload = [
            {
                "Attribute": "mx_AECB_Reportmx_CustomObject_2",
                "Value": uploaded_files[0] if len(uploaded_files) > 0 else ""
            },
            {
                "Attribute": "mx_AECB_Reportmx_CustomObject_4",
                "Value": uploaded_files[1] if len(uploaded_files) > 1 else ""
            }
        ]
        headers = {"Content-Type": "application/json"}
        params = {
            "accessKey": LSQ_ACCESS_KEY,
            "secretKey": LSQ_SECRET_KEY,
            "leadId": lead_id,
            "postUpdatedLead": "true"
        }
        r = requests.post(
            update_url,
            headers=headers,
            params=params,
            json=payload,
            verify=False
        )

    # -------------------- OCR --------------------
    front_text = safe_ocr(front_bytes)
    back_text = safe_ocr(back_bytes)

    if front_text is None and back_text is None:
        log_error_to_mavis(
            mobile=phone,
            error_message="OCR not captured, Kindly recheck the document"
        )
        return render_template(
            "message.html",
            status="success",
            message="Documents uploaded successfully."
        )

    front_data = extract_front_emirates_id(front_text or {}) or {}
    back_data = extract_back_emirates_id(back_text or {}) or {}

    full_data = {**front_data, **back_data}
    eid = full_data.get("emirates_id_number")

    if not eid:
        log_error_to_mavis(
            mobile=phone,
            error_message="OCR not captured, Kindly recheck the document"
        )
        return render_template(
            "message.html",
            status="success",
            message="Documents uploaded successfully."
        )

    full_data["phone"] = phone

    # -------------------- FINAL RESPONSE --------------------
    return render_template(
        "results.html",
        data=full_data,
        lead_id=lead_id,
        uploaded_files=uploaded_files
    )

@app.route("/confirm", methods=["POST"])
def confirm():
    data = request.form.to_dict()
    eid = data.get("emirates_id_number")
    phone = data.get("phone")  # optional check too
     # Use the same paging query logic to check existence
    query_url = f"https://mavis-rest-in21.leadsquared.com/api/{DBID}/{TABLEID}/rows/query?orgcode={ORGCODE}"
    headers = {
        "x-api-key": API_KEY,
        "Content-Type": "application/json"
    }
 
    page = 1
    page_size = 200
    eid_exists = False
 
    while True:
        payload_query = {
            "Select": ["emirates_id_number"],
            "Paging": {"PageSize": page_size, "PageIndex": page}
        }
        
        resp = requests.post(query_url, headers=headers, json=payload_query, verify=False)
        if resp.status_code != 200:
            print("MAVIS query failed:", resp.text)
            break

        data_resp = resp.json().get("Data", [])
        rows = data_resp if isinstance(data_resp, list) else data_resp.get("Rows", [])

        if not rows:
            break

        for row in rows:
            row_eid = row.get("emirates_id_number")
            if row_eid == eid:
                eid_exists = True
                break

        if eid_exists or len(rows) < page_size:
            break
        page += 1

    if eid_exists:
        return render_template("message.html", status="error", message="Record is already updated for this Emirates ID.")

   
    insert_url = f"https://mavis-rest-in21.leadsquared.com/api/{DBID}/{TABLEID}/rows?orgcode={ORGCODE}&append=true"
    headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}

    # Map only to existing columns in MAVIS table
    existing_columns = [
        "emirates_id_number", "full_name", "date_of_birth",
        "nationality", "sex", "issuing_date", "Expiry_Date",
        "occupation", "employer_or_sponsor", "issuing_place", "Phone"
    ]
    api_row = []
    for col in existing_columns:
        value = data.get(col.lower())
        api_row.append({"ColumnId": col, "ColumnValue": str(value) if value else None})

    payload = {
        "Data": [api_row],
        "DateFormat": "yyyy-MM-dd",
        "TimeFormat": "HH:mm:ss",
        "DateTimeFormat": "yyyy-MM-dd HH:mm:ss"
    }

    resp = requests.post(insert_url, headers=headers, json=payload, verify=False)

    if resp.status_code not in [200, 201]:
        return render_template("message.html", status="error", message="Failed to submit details to MAVIS.")

    return render_template("message.html", status="success", message="Details submitted successfully")

# ------------------- Run Flask -------------------
if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 8000))
    serve(app, host="0.0.0.0", port=port)
