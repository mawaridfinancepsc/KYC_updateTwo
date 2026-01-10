import os
import io
import base64
import requests
from flask import Flask, request, jsonify, render_template, session
from google.oauth2 import service_account
import google.auth.transport.requests
import json
from google.auth.transport.requests import Request

import fitz  # PyMuPDF
import gspread
import re
from datetime import datetime, timedelta
import urllib.parse
import random
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

sms_user = os.getenv("SMS_USER")
sms_pwd = os.getenv("SMS-PWD")
API_KEY = os.getenv("API_KEY")
ORGCODE = os.getenv("ORGCODE")
DBID = os.getenv("DBID")
TABLEID = os.getenv("TABLEID")
LSQ_ACCESS_KEY = os.getenv("LSQ_ACCESS_KEY")
LSQ_SECRET_KEY = os.getenv("LSQ_SECRET_KEY")
app = Flask(__name__)
app.secret_key = "supersecret"  # for flash messages
app.permanent_session_lifetime = timedelta(minutes=5)

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

    # Remove spaces
    mobile = mobile.strip()

    # If user entered +971- format correctly
    if mobile.startswith("+971-"):
        number = mobile[6:]  # remove '+971-'
    else:
        # Remove all non-digits
        digits = re.sub(r"[^\d]", "", mobile)

        # Normalize several possible user inputs
        if digits.startswith("00971"):
            digits = digits[5:]
        elif digits.startswith("0971"):
            digits = digits[4:]
        elif digits.startswith("971"):
            digits = digits[3:]
        elif digits.startswith("0") and len(digits) == 10:
            digits = digits[1:]

        number = digits

    # Now validate strict rule: MUST be 9 digits and start with 5
    if not re.fullmatch(r"5\d{8}", number):
        return None

    # Return final required format
    return f"+971-{number}"


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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "googleserviceacc.json")

# -----------------------------
# Create credentials
# -----------------------------
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE)



# ----------------- Vision Client -----------------
def get_vision_client():
    """
    Returns a Google Vision API client using the credentials from environment variable.
    """
    return vision.ImageAnnotatorClient(credentials=credentials)
# -----
# ------------------- Helper Functions -------------------
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
def pdf_first_page_to_bytes(pdf_file):
    doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
    pix = doc[0].get_pixmap()
    return pix.tobytes("png")
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

    # --- Emirates ID Number ---
    for l in lines:
        m = re.search(r'784-\d{4}-\d{7}-\d', l)
        if m:
            data["emirates_id_number"] = m.group()
            break

    # --- Full Name ---
    for l in lines:
        if l.startswith("Name:"):
            data["full_name"] = l.replace("Name:", "").strip()
            break

    # --- Nationality ---
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
    date_matches = []
    for i, l in enumerate(lines):
        match = re.search(r'\d{2}/\d{2}/\d{4}', l)
        if match:
            date_matches.append((i, match.group()))

    # --- DATE OF BIRTH ---
    dob_found = False
    for i, l in enumerate(lines):
        if "Birth" in l or "DOB" in l or "Date of Birth" in l:
            match = re.search(r'\d{2}/\d{2}/\d{4}', l)
            if match:
                data["date_of_birth"] = match.group()
                dob_found = True
                break
            elif i + 1 < len(lines):
                match_next = re.search(r'\d{2}/\d{2}/\d{4}', lines[i+1])
                if match_next:
                    data["date_of_birth"] = match_next.group()
                    dob_found = True
                    break
    if not dob_found and date_matches:
        data["date_of_birth"] = date_matches[0][1]

    # --- ISSUING DATE ---
    issue_found = False
    for i, l in enumerate(lines):
        if "Issue" in l or "Issuing" in l:
            match = re.search(r'\d{2}/\d{2}/\d{4}', l)
            if match:
                data["issuing_date"] = match.group()
                issue_found = True
                break
            elif i + 1 < len(lines):
                match_next = re.search(r'\d{2}/\d{2}/\d{4}', lines[i+1])
                if match_next:
                    data["issuing_date"] = match_next.group()
                    issue_found = True
                    break
    if not issue_found and len(date_matches) >= 2:
        data["issuing_date"] = date_matches[1][1]

    # --- EXPIRY DATE ---
    expiry_found = False
    for i, l in enumerate(lines):
        if "Expiry" in l or "EXP" in l or "انتهاء" in l:
            match = re.search(r'\d{2}/\d{2}/\d{4}', l)
            if match:
                data["expiry_date"] = match.group()
                expiry_found = True
                break
            elif i + 1 < len(lines):
                match_next = re.search(r'\d{2}/\d{2}/\d{4}', lines[i+1])
                if match_next:
                    data["expiry_date"] = match_next.group()
                    expiry_found = True
                    break
    if not expiry_found and len(date_matches) >= 3:
        data["expiry_date"] = date_matches[2][1]

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
        
        sender_id = "SMS Alert"
        # Generate OTP
        
        session.permanent = True
        otp = str(random.randint(1000, 9999))
        session["otp"] = otp
        session["otp_phone"] = phone

        
        mobileno = phone.replace("+", "").replace(" ", "")
        message = f"Your OTP for KYC update is: {otp}. This OTP is valid for 5 minutes.Do not share this OTP with anyone, we will never call you to ask for this OTP. If not requested please call 043040888 immedeiately."

        sms_url = (
            "https://mshastra.com/sendurlcomma.aspx"
            f"?user={sms_user}"
            f"&pwd={sms_pwd}"
            f"&senderid={urllib.parse.quote(sender_id)}"
            f"&mobileno={mobileno}"
            f"&msgtext={urllib.parse.quote(message)}"
            f"&priority=High"
            f"&CountryCode=ALL"
        )

        try:
            sms_response = requests.get(sms_url, timeout=15, verify=False)
            print("SMS sent, response:", sms_response.text.strip())
        except Exception as e:
            flash("Failed to send OTP. Please try again.", "error")
            return redirect("/")

        # Redirect to OTP verification page
        return render_template("verify_otp.html", phone=phone, lead_id=lead["ProspectID"])

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

@app.route("/verify_otp", methods=["POST"])
def verify_otp():
    user_otp = request.form.get("otp")
    phone = request.form.get("phone")
    lead_id = request.form.get("lead_id")

    if "otp" not in session or session.get("otp_phone") != phone:
        flash("OTP session expired. Please request again.", "error")
        return redirect("/")

    if user_otp != session["otp"]:
        flash("Invalid OTP. Please try again.", "error")
        return render_template("verify_otp.html", phone=phone, lead_id=lead_id)

    # OTP correct, clear session
    session.pop("otp")
    session.pop("otp_phone")

    # Proceed to document upload
    return render_template("upload_form.html", phone=phone, lead_id=lead_id)


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

    files = request.files.getlist("id_files")
    if len(files) != 2:
        return render_template(
            "message.html",
            status="error",
            message="Please upload exactly 2 files: front and back."
        )
    
    # Allowed file types
    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
    MAX_FILE_SIZE = 16 * 1024 * 1024  # 16 MB
    
    front_file, back_file = files[0], files[1]
    
    def read_file_bytes(f):
        filename = f.filename.lower()
        if not filename.endswith(tuple(ALLOWED_EXTENSIONS)):
            raise ValueError("Invalid file type")
        
        f.seek(0, 2)
        if f.tell() > MAX_FILE_SIZE:
            raise ValueError("File too large")
        f.seek(0)
    
        file_bytes = f.read()
        
        # If PDF, convert first page to image
        if filename.endswith(".pdf"):
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            pix = doc[0].get_pixmap()
            return pix.tobytes("png")
        
        return file_bytes
    
    try:
        front_bytes = read_file_bytes(front_file)
        back_bytes = read_file_bytes(back_file)
    except ValueError as e:
        return render_template("message.html", status="error", message=str(e))
    
    front_text = safe_ocr(front_bytes)
    back_text = safe_ocr(back_bytes)
    # If front does NOT contain Emirates ID number → treat as wrong document
    if not re.search(r"784-\d{4}-\d{7}-\d", front_text):
        return render_template(
            "message.html",
            status="error",
            message="Invalid document uploaded. Please upload a valid and clear Emirates ID."
        )
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
    # UPLOAD FRONT
    if front_bytes:
        front_stream = BytesIO(front_bytes)  # new stream for front
        files_payload = {
            "uploadFiles": ("front_" + front_file.filename, front_stream, front_file.content_type)
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
        "SchemaName": "mx_CustomObject_4",
        "EntitySchemaName": "mx_AECB_Report",
        "Entity": 0,
        "StorageVersion": 0
    }
    # UPLOAD BACK
    if back_bytes:
        back_stream = BytesIO(back_bytes)  # new stream for back
        files_payload = {
            "uploadFiles": ("back_" + back_file.filename, back_stream, back_file.content_type)
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
    # -------------------- EXPIRY CHECK --------------------
    expiry_str = full_data.get("expiry_date")
    if expiry_str:
        try:
            expiry_date = datetime.strptime(expiry_str, "%d/%m/%Y").date()
            today = datetime.today().date()
            if expiry_date < today:
                log_error_to_mavis(
                    mobile=phone,
                    error_message=f"Emirates ID expired on {expiry_str}"
                )
                return render_template(
                    "message.html",
                    status="error",
                    message=f"Emirates ID has expired on {expiry_str}. Upload not allowed."
                )
        except ValueError:
            # Handle wrong format gracefully
            log_error_to_mavis(
                mobile=phone,
                error_message=f"Invalid expiry date format: {expiry_str}"
            )
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
