import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from datetime import datetime
from dotenv import load_dotenv
from core.database import collection
import pytz
import os

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

def transform_case_data(response_json: dict, case_reg_no: str):
    data = response_json.get("data", {})
    hearing_details = data.get("caseHearingDetails", [])
    latest_hearing = hearing_details[0] if hearing_details else {}
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
        "FirstHearingDate": format_date(latest_hearing.get("dateOfHearing")),
        "DecisionDate": format_date(data.get("impungedOrderDate")),
        "CaseStatus": data.get("caseStage"),
        "NatureofDisposal": None,
        "CourtNumberandJudge": None,
        "petitioner_and_advocate": [
            f"{data.get('complainant')}",
        ],
        "respondent_and_advocate": [
            f"{data.get('respondent')}"
        ],
        "actsandSection": {
            "acts": None,
            "section": None
        },
        "case_history": [
            {
                "judge": None,
                "businessOnDate": format_date(latest_hearing.get("dateOfHearing")),
                "hearingDate": format_date(latest_hearing.get("dateOfNextHearing")),
                "purpose": data.get("caseStage"),
                "inputType": "automatic",
                "lawyerRemark": latest_hearing.get("proceedingText")
            }
        ],
        "case_transfer": [],
        "orders": []
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
