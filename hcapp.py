import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import re
import numpy as np
from pydantic import BaseModel
from typing import Optional
import boto3
from pymongo import MongoClient
from dotenv import load_dotenv
import os 
from mangum import Mangum
import pytesseract
import cv2
import time
import random
import json

load_dotenv()


BASE_URL = "https://hcservices.ecourts.gov.in/hcservices/"

client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["gylscrdata"]
collection = db["casedetails"]

BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

s3_client = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_S3_KEY"),
    aws_secret_access_key=os.getenv("AWS_S3SEC_KEY")
)

app = FastAPI()

class CaseRequest(BaseModel):
    case_type: str
    case_no: str
    rgyear: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None
    refresh_flag : str
    CaseType : str

def decode_captcha(session, captcha_url):
    response = session.get(captcha_url, stream=True)

    if response.status_code == 200:
        image = np.asarray(bytearray(response.content), dtype=np.uint8)
        image = cv2.imdecode(image, cv2.IMREAD_GRAYSCALE)

        _, thresh_img = cv2.threshold(image, 150, 255, cv2.THRESH_BINARY_INV)
        pytesseract.pytesseract.tesseract_cmd = r'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'
        captcha_text = pytesseract.image_to_string(thresh_img, config="--psm 6").strip()
        return captcha_text
    return None


def clean_text(text):
    return re.sub(r"\s+", " ", text.strip())

def extract_table_data(soup, heading, fields):
    extracted_data = {}
    heading_element = soup.find("h2", string=re.compile(heading, re.IGNORECASE))
    if heading_element:
        table = heading_element.find_next("table")
        if table:
            rows = table.find_all("tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 2:
                    label = clean_text(cols[0].get_text())
                    value = clean_text(cols[1].get_text())
                    if label in fields:
                        extracted_data[label] = value
    return extracted_data



def extract_party_details(soup, class_name):
    extracted_data = []
    elements = soup.find_all("span", class_=class_name)

    for element in elements:
        for br in element.find_all("br"):
            br.replace_with("\n")
        lines = [line.strip() for line in element.get_text().split("\n") if line.strip()]
        if lines:
            name = lines[0]

            if name[0].isdigit() and ")" in name:
                name = name.split(")", 1)[1].strip()

            extracted_data.append(name)

    return extracted_data

def extract_subordinate_court_info(soup):
    court_info = soup.find("span", class_="Lower_court_table")
    if not court_info:
        return {}

    details = {}
    labels = court_info.find_all("span", style="width:150px;display:inline-block;")
    values = court_info.find_all("label", style="text-align:left")

    for label, value in zip(labels, values):
        key = label.text.strip().replace(":", "")
        val = value.text.strip()
        details[key] = val

    return details

def extract_table_with_headers(soup, heading=None, headers=None, className=None):
    extracted_data = []
    table = None

    if className:
        table = soup.find("table", class_=className)

    elif heading:
        heading_element = soup.find("h2", string=re.compile(heading, re.IGNORECASE))
        if heading_element:
            table = heading_element.find_next("table")

    if table and headers:
        rows = table.find_all("tr")[1:] 
        for row in rows:
            cols = row.find_all("td")
            if len(cols) == len(headers):
                entry = {headers[i]: clean_text(cols[i].get_text()) for i in range(len(headers))}
                extracted_data.append(entry)

    return extracted_data

def extract_and_upload_orders(soup, s3_client, session, BUCKET_NAME, REGION_NAME, case_info):
    orders = []
    table = soup.find("table", class_="order_table")

    if not table:
        return orders

    rows = table.find_all("tr")[1:]  
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 5:
            continue

        order_number = clean_text(cols[0].get_text())
        order_date = clean_text(cols[3].get_text())

        link_tag = cols[4].find("a", href=True)
        if not link_tag:
            continue

        final_pdf_url = BASE_URL + link_tag["href"]

        s3_folder_path = f"case_data/orders/{case_info['cino']}/"
        s3_file_path = f"{s3_folder_path}{case_info['cino']}-{order_number}.pdf"

        try:
            s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_file_path)
            s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
        except s3_client.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "404":
                response = session.get(final_pdf_url, stream=True)
                if response.status_code == 200:
                    s3_client.upload_fileobj(
                        response.raw,
                        BUCKET_NAME,
                        s3_file_path,
                        ExtraArgs={
                            'ContentType': 'application/pdf',
                            'ContentDisposition': 'inline'
                        }
                    )
                    s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
                else:
                    print(f"❌ Failed to fetch PDF from {final_pdf_url}")
                    s3_url = None
            else:
                print(f"❌ S3 Error: {e}")
                s3_url = None

        orders.append({
            "order_number": order_number,
            "order_date": order_date,
            "order_link": s3_url
        })

    return orders



def parse_case_history(html, payload, second_payload, case_info, session):

    soup = BeautifulSoup(html, "html.parser")

    case_details = extract_table_data(soup, "Case Details", ["Filing Number", "Filing Date", "Registration Number", "Registration Date", "CNR Number"])

    case_status = extract_table_data(soup, "Case Status", [
        "First Hearing Date",
        "Decision Date",
        "Case Status",
        "Nature of Disposal",
        "Coram",
        "Bench Type",
        "Judicial Branch",
        "State",
        "District",
        "Not Before Me",
        "Stage of Case"
    ])

    

    petitioner_and_advocate = extract_party_details(soup, "Petitioner_Advocate_table")
    respondent_and_advocate = extract_party_details(soup, "Respondent_Advocate_table")

    category_details = extract_table_data(soup, "Category", ["Category", "Sub Category"])

    subordinate_court_info = extract_subordinate_court_info(soup)

    raw_case_hearing_history  = extract_table_with_headers(soup, "History of Case Hearing", [
        "Cause List Type",
        "Judge",
        "Business On Date",
        "Hearing Date",
        "Purpose of hearing"
    ],
    className="history_table")

    case_hearing_history = []
    for entry in raw_case_hearing_history:
        case_hearing_history.append({
            "judge": entry.get("Judge", ""),
            "businessOnDate": entry.get("Business On Date", ""),
            "hearingDate": entry.get("Hearing Date", ""),
            "purpose": entry.get("Purpose of hearing", ""),
            "inputType": "automatic",
            "lawyerRemark": None
        })


        orders = extract_and_upload_orders(
            soup,
            s3_client=s3_client,
            session=session,
            BUCKET_NAME=BUCKET_NAME,
            REGION_NAME=REGION_NAME,
            case_info=case_info
)

    return {
            "success" : True,
            "data": {
            "case_no" : second_payload.get("case_no"),
            "cino": second_payload.get("cino"),
            "court_code" : second_payload.get("court_code"),
            "state_code" : payload.get("state_code"),
            "dist_code" : payload.get("dist_code"),
            "court_complex_code" : payload.get("court_complex_code"),
            "est_code" : payload.get("est_code"),
            "case_type" : payload.get("case_type"),
            "CaseType" : payload.get("CaseType"),
            "FilingNumber" : case_details.get("Filing Number", ""),
            "RegistrationNumber" : case_details.get("Registration Number", ""),
            "CNRNumber" : second_payload.get("cino"),
            "FirstHearingDate" : case_status.get("First Hearing Date", ""),
            "CaseStatus": case_status.get("Stage of Case", ""),
            "CourtNumberandJudge" : case_status.get("Coram", ""),
            "petitioner_and_advocate": petitioner_and_advocate,
            "respondent_and_advocate": respondent_and_advocate,
            "actsandSection" : {},
            "category_details": category_details,
            "subordinate_court_information": subordinate_court_info,
            "case_history": case_hearing_history,
            "orders": orders
        }
    }


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.post("/hc/getcaseInfo")
def fetch_submit_info(case_data: CaseRequest):
    session = requests.Session()
    query = case_data.dict()
    if case_data.refresh_flag != "1":
        existing_case = collection.find_one(query)
        if existing_case:
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=existing_case)

    try:
        payload = {
            'court_code': case_data.court_complex_code,
            'case_type': case_data.case_type,
            'case_no': case_data.case_no,
            'rgyear': case_data.rgyear,
            'state_code': case_data.state_code,
            'dist_code': case_data.dist_code,
            'caseStatusSearchType': 'CScaseNumber',
            'court_complex_code': case_data.court_complex_code,
            'est_code': case_data.est_code,
            'caseNoType': 'new',
            'search_case_no': case_data.case_no
        }

        max_attempts = 20
        for attempt in range(max_attempts):
            captcha_url = f"https://hcservices.ecourts.gov.in/hcservices/securimage/securimage_show.php?{random.randint(100000,999999)}"
            captcha_text = decode_captcha(session, captcha_url)
            captcha_text = re.sub(r'[^A-Za-z0-9]', '', captcha_text)
            payload['captcha'] = captcha_text

            response = session.post(
                "https://hcservices.ecourts.gov.in/hcservices/cases_qry/index_qry.php?action_code=showRecords",
                data=payload
            )

            print("Response Status Code:", response.text)

            raw_text = response.text.strip()
            soup = BeautifulSoup(raw_text, "html.parser")
            clean_text = soup.get_text()

            tot_records_match = re.search(r'"totRecords"\s*:\s*(\d+)', clean_text)
            tot_records = int(tot_records_match.group(1)) if tot_records_match else None

            if tot_records == 0:
                return JSONResponse(
                    content={"error": "Invalid case details"},
                    status_code=404
                )

            if '"Invalid Captcha"' in clean_text or '"ERROR_VAL"' in clean_text:
                continue  

            con_match = re.search(r'"con"\s*:\s*\["(.*?)"\]', clean_text)
            case_details = None
            if con_match:
                con_data = con_match.group(1).encode('utf-8').decode('unicode_escape')
                try:
                    case_details = json.loads(con_data)[0]
                except Exception:
                    pass

            if case_details:
                second_payload = {
                    "court_code": case_data.court_complex_code,
                    "state_code": case_data.state_code,
                    "court_complex_code": case_data.court_complex_code,
                    "case_no": case_details.get("case_no"),
                    "cino": case_details.get("cino"),
                }

                print("Second Payload:", second_payload)
                headers = {
                'Origin': 'https://hcservices.ecourts.gov.in',
                'Referer': 'https://hcservices.ecourts.gov.in/',
                'Content-Type': 'application/x-www-form-urlencoded',
                }
                second_resp = session.post(
                    "https://hcservices.ecourts.gov.in/hcservices/cases_qry/o_civil_case_history.php",
                    data=second_payload,
                    headers=headers
                )

                return parse_case_history(second_resp.text,payload,second_payload, case_info=case_details, session=session)

        else:
            return JSONResponse(
                content={"error": "Failed to fetch case info after multiple attempts"},
                status_code=400
            )

    finally:
        session.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


# handler = Mangum(app)