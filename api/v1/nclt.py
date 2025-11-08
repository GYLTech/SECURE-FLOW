import json
import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import re
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from core.database import collection
from core.s3_client import s3_client
import os
import html
load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")
app = APIRouter()

class CaseRequest(BaseModel):
    case_type: str
    case_reg_no: str
    rgyear: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None


@app.post("/nclt/getcaseInfo")
def fetch_submit_hc_info(case_data: CaseRequest):
    session = requests.Session()
    query = case_data.dict()
    ac_query = {
        "case_reg_no": query.get("case_reg_no"),
        "rgyear": query.get("rgyear"),
        "est_code": query.get("est_code"),
        "case_type": query.get("case_type"),
        "state_code": query.get("state_code"),
        "dist_code": query.get("dist_code"),
        "court_complex_code": query.get("court_complex_code")
    }
    existing_case = collection.find_one(ac_query)
    if existing_case:
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=existing_case)

    try:
        payload = json.dumps({
        "wayofselection": "casenumber",
        "i_bench_id": "0",
        "filing_no": "",
        "i_bench_id_case_no": query.get("court_complex_code"),
        "i_case_type_caseno": query.get("case_type"),
        "i_case_year_caseno": query.get("rgyear"),
        "case_no": query.get("case_reg_no"),
        "i_party_search": "E",
        "i_bench_id_party": "0",
        "party_type_party": "0",
        "party_name_party": "",
        "i_case_year_party": "0",
        "status_party": "0",
        "i_adv_search": "E",
        "i_bench_id_lawyer": "0",
        "party_lawer_name": "",
        "i_case_year_lawyer": "0",
        "bar_council_advocate": "",
       })

        headers = {
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Content-Type': 'application/json',
        'Cookie': 'SERVERID=efiling2-248'
        }
        response = session.post(
            "https://efiling.nclt.gov.in/caseHistoryoptional.drt", headers=headers, data=payload)
        
        # print("response status code:", response.json())

        response_additional = session.get(
            f"https://efiling.nclt.gov.in/caseHistoryalldetails.drt?filing_no={response.json()['mainpanellist'][0]['filing_no']}&flagIA=false", headers=headers, data=payload)
        
        print("additional response status code:", response_additional.json())
        case_history = []

        print("daata___",response_additional.json().get("allproceedingdtls", []))

        for entry in response_additional.json().get("allproceedingdtls", []):
            case_history.append({
                "judge": entry.get("bench_location_name") or "Unknown Bench",
                "businessOnDate": entry.get("listing_date") or None,
                "hearingDate": entry.get("next_list_date") or None,
                "purpose": entry.get("purpose") or None,
                "inputType": "automatic",
                "lawyerRemark": "null"
            })

        result = {
        "est_code": query.get("est_code"),
        "cino": response.json()['mainpanellist'][0]['case_no'],
        "state_code": query.get("state_code"),
        "court_complex_code": query.get("court_complex_code"),
        "rgyear": query.get("rgyear"),
        "case_type": query.get("case_type"),
        "dist_code": "11",
        "CNRNumber": response.json()['mainpanellist'][0]['case_no'],
        "CaseStatus": response_additional.json()['isregistered'][0]['status'],
        "CaseType": response.json()['mainpanellist'][0]['case_type_desc_cis'],
        "FilingNumber": response.json()['mainpanellist'][0]['filing_no'],
        "FirstHearingDate": response_additional.json()['allfinalstatuslist'][0]['listing_date'],
        "RegistrationNumber": response.json()['mainpanellist'][0]['filing_no'],
        "case_history": case_history,
        "orders": [],
        "petitioner_and_advocate": [
            response_additional.json()['partydetailslist'][0]['party_name']
        ],
        "respondent_and_advocate": [
            response_additional.json()['partydetailslist'][1]['party_name']
        ]
        }

        # result = collection.update_one(
        #     ac_query, {"$set": testdata}, upsert=True)
        # if result.upserted_id:
        #     testdata["_id"] = str(result.upserted_id)
        # else:
        #     doc = collection.find_one(ac_query)
        #     testdata["_id"] = str(doc["_id"])

        return JSONResponse(content=result, status_code=200)

    finally:
        session.close()
