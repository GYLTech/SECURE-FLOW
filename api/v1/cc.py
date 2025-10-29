import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from core.database import collection
import pytz
import os
import base64
import boto3
import requests
from datetime import datetime

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")
BASE_URL = "https://hcservices.ecourts.gov.in/hcservices/"

app = APIRouter()
IST = pytz.timezone("Asia/Kolkata")

class CaseRequest(BaseModel):
    case_reg_no: str
    refresh_flag: str

def format_date(date_str):
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        dt_ist = dt.astimezone(IST)
        return dt_ist.strftime("%d-%m-%Y")
    except Exception:
        return date_str

def transform_case_data(response_json: dict, case_reg_no: str, s3_bucket_name: str):
    def format_date(date_str):
        """Convert 'YYYY-MM-DD' to 'DD-MM-YYYY'."""
        if not date_str:
            return None
        try:
            return "-".join(reversed(date_str.split("-")))
        except Exception:
            return date_str

    def upload_pdf_to_s3(base64_data: str, case_number: str, hearing_date: str):
        """Upload decoded PDF to S3 and return the public URL."""
        try:
            pdf_bytes = base64.b64decode(base64_data)
            file_name = f"case_data/orders/{case_number}_{hearing_date}.pdf"

            s3_client = boto3.client("s3")
            s3_client.put_object(
                Bucket=s3_bucket_name,
                Key=file_name,
                Body=pdf_bytes,
                ContentType="application/pdf"
            )

            s3_url = f"https://{s3_bucket_name}.s3.amazonaws.com/{file_name}"
            return s3_url
        except Exception as e:
            print(f"❌ Error uploading PDF for {case_number} ({hearing_date}): {e}")
            return None

    data = response_json.get("data", {})
    hearing_details = data.get("caseHearingDetails", [])

    case_history = []
    orders = []
    seen = set()
    order_counter = 1

    # Core loop for hearing + order processing
    for hearing in hearing_details:
        entry = {
            "judge": None,
            "businessOnDate": format_date(hearing.get("dateOfHearing")),
            "hearingDate": format_date(hearing.get("dateOfNextHearing")),
            "purpose": hearing.get("caseStage"),
            "inputType": "automatic",
            "lawyerRemark": None
        }

        key = (entry["businessOnDate"], entry["hearingDate"], entry["purpose"])
        if key not in seen:
            seen.add(key)
            case_history.append(entry)

        # === Hit e-Jagriti API for daily order PDF ===
        case_number = data.get("caseNumber")
        hearing_date = hearing.get("dateOfHearing")
        order_type_id = hearing.get("orderTypeId", 1)

        if not case_number or not hearing_date:
            continue

        api_url = (
            "https://e-jagriti.gov.in/services//courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf"
            f"?caseNumber={case_number}&dateOfHearing={hearing_date}&orderTypeId={order_type_id}"
        )

        try:
            response = requests.get(api_url, timeout=15)
            res_json = response.json()

            # ✅ Only proceed if success and PDF is present
            if response.status_code == 200 and res_json.get("data", {}).get("dailyOrderPdf"):
                base64_blob = res_json["data"]["dailyOrderPdf"]

                s3_url = upload_pdf_to_s3(
                    base64_data=base64_blob,
                    case_number=case_number,
                    hearing_date=format_date(hearing_date)
                )

                if s3_url:
                    orders.append({
                        "order_number": str(order_counter),
                        "order_date": format_date(hearing_date),
                        "order_link": s3_url
                    })
                    order_counter += 1
            else:
                print(f"ℹ️ No daily order found for hearing on {hearing_date}")

        except Exception as e:
            print(f"⚠️ Error fetching order for hearing {hearing_date}: {e}")
            continue

    # Get latest hearing details
    latest_hearing = hearing_details[-1] if hearing_details else {}

    formatted_data = {
        "case_no": data.get("fillingReferenceNumber", case_reg_no),
        "cino": data.get("caseNumber"),
        "court_code": None,
        "state_code": None,
        "dist_code": None,
        "court_complex_code": None,
        "est_code": None,
        "case_type": str(data.get("caseTypeId")),
        "fir_details": {},
        "CaseType": "Consumer Case",
        "FilingNumber": str(data.get("fillingReferenceNumber")),
        "RegistrationNumber": data.get("caseNumber"),
        "CNRNumber": None,
        "FirstHearingDate": format_date(hearing_details[0].get("dateOfHearing")) if hearing_details else None,
        "DecisionDate": format_date(latest_hearing.get("dateOfHearing")),
        "CaseStatus": latest_hearing.get("caseStage") if latest_hearing else None,
        "NatureofDisposal": None,
        "CourtNumberandJudge": None,
        "petitioner_and_advocate": [
            f"{data.get('complainant')}"
        ],
        "respondent_and_advocate": [
            f"{data.get('respondent')}"
        ],
        "actsandSection": {"acts": None, "section": None},
        "case_history": case_history,
        "case_transfer": [],
        "orders": orders
    }

    return formatted_data

@app.post("/cc/getcaseInfo")
def fetch_submit_hc_info(case_data: CaseRequest):
    session = requests.Session()
    query = case_data.dict()
    ac_query = {"case_reg_no": query.get("case_reg_no")}
    if case_data.refresh_flag != "1":
        existing_case = collection.find_one(ac_query)
        if existing_case:
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=existing_case)
    try:
        headers = {
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'Referer': 'https://e-jagriti.gov.in/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
            'sec-ch-ua': '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"'
        }
        response = session.get(
            f"https://e-jagriti.gov.in/services/case/caseFilingService/v2/getCaseStatus?caseNumber={query.get('case_reg_no')}",
            headers=headers
        )
        response_json = response.json()
        transformed_data = transform_case_data(response_json, query.get("case_reg_no"))
        return JSONResponse(content=transformed_data)
    finally:
        session.close()
