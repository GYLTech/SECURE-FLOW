import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from core.database import collection
import pytz
import os
import base64
from core.s3_client import s3_client
import requests
from datetime import datetime

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

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

from datetime import datetime

def transform_case_data(response_json: dict, case_reg_no: str):
    def format_date(date_str):
        if not date_str:
            return None
        try:
            return "-".join(reversed(date_str.split("-")))
        except Exception:
            return date_str

    def parse_date(date_str):
        """Convert 'YYYY-MM-DD' to datetime for sorting."""
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            return None

    def upload_pdf_to_s3(base64_data: str, case_number: str, hearing_date: str):
        try:
            file_name = f"case_data/orders/{case_number}_{hearing_date}.pdf"
            s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{file_name}"
            try:
                s3_client.head_object(Bucket=BUCKET_NAME, Key=file_name)
                print(f"✅ File already exists in S3: {s3_url}")
                return s3_url
            except s3_client.exceptions.ClientError as e:
                if e.response["Error"]["Code"] != "404":
                    print(f"❌ S3 Error checking file: {e}")
                    return None

            pdf_bytes = base64.b64decode(base64_data)

            s3_client.put_object(
                Bucket=BUCKET_NAME,
                Key=file_name,
                Body=pdf_bytes,
                ContentType="application/pdf",
                ContentDisposition="inline"
            )

            print(f"📄 Uploaded new PDF to S3: {s3_url}")
            return s3_url

        except Exception as e:
            print(f"❌ Error uploading PDF for {case_number} ({hearing_date}): {e}")
            return None

    data = response_json.get("data", {})
    hearing_details = data.get("caseHearingDetails", [])

    case_history_raw = []
    seen_dates = set()
    orders = []
    order_counter = 1

    for hearing in hearing_details:
        raw_date = hearing.get("dateOfHearing")
        if not raw_date:
            continue

        formatted_date = format_date(raw_date)
        next_date = format_date(hearing.get("dateOfNextHearing"))
        purpose = (hearing.get("caseStage") or "").strip()

        if formatted_date in seen_dates:
            continue
        seen_dates.add(formatted_date)

        case_history_raw.append({
            "judge": None,
            "businessOnDate": formatted_date,
            "hearingDate": next_date,
            "purpose": purpose,
            "inputType": "automatic",
            "lawyerRemark": None,
            "_sort_key": parse_date(raw_date)
        })


        case_number = data.get("caseNumber")
        order_type_id = hearing.get("orderTypeId", 1)

        if not case_number:
            continue

        api_url = (
            "https://e-jagriti.gov.in/services/courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf"
            f"?caseNumber={case_number}&dateOfHearing={raw_date}&orderTypeId={order_type_id}"
        )

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

        try:
            response = requests.get(api_url, headers=headers, timeout=15)
            res_json = response.json()

            if response.status_code == 200 and res_json.get("data", {}).get("dailyOrderPdf"):
                base64_blob = res_json["data"]["dailyOrderPdf"]
                s3_url = upload_pdf_to_s3(base64_blob, case_number, formatted_date)

                if s3_url:
                    orders.append({
                        "order_number": str(order_counter),
                        "order_date": formatted_date,
                        "order_link": s3_url
                    })
                    order_counter += 1
            else:
                print(f"ℹ️ No daily order found for hearing on {formatted_date}")

        except Exception as e:
            print(f"⚠️ Error fetching order for hearing {formatted_date}: {e}")
            continue

    case_history_sorted = sorted(
        [ch for ch in case_history_raw if ch["_sort_key"]],
        key=lambda x: x["_sort_key"]
    )
    # Drop helper key
    for ch in case_history_sorted:
        ch.pop("_sort_key", None)

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
        "FirstHearingDate": case_history_sorted[0]["businessOnDate"] if case_history_sorted else None,
        "DecisionDate": format_date(latest_hearing.get("dateOfHearing")),
        "CaseStatus": latest_hearing.get("caseStage") if latest_hearing else None,
        "NatureofDisposal": None,
        "CourtNumberandJudge": None,
        "petitioner_and_advocate": [f"{data.get('complainant')}"],
        "respondent_and_advocate": [f"{data.get('respondent')}"],
        "actsandSection": {"acts": None, "section": None},
        "case_history": case_history_sorted,
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
        result = collection.update_one(
                ac_query, {"$set": transformed_data}, upsert=True)
        if result.upserted_id:
                transformed_data["_id"] = str(result.upserted_id)
        else:
                doc = collection.find_one(ac_query)
                transformed_data["_id"] = str(doc["_id"])
                
        return JSONResponse(content=transformed_data)
    finally:
        session.close()
