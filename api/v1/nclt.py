import json
import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from core.database import collection
from core.s3_client import s3_client
from botocore.exceptions import ClientError
import os

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

def stream_upload_order_nclt(enc_path: str, filing_no: str, order_number: str):
    if not enc_path:
        return None
    final_pdf_url = f"https://efiling.nclt.gov.in/ordersview.drt?path={enc_path}"
    s3_folder_path = f"case_data/orders/{filing_no}/"
    s3_file_path = f"{s3_folder_path}{filing_no}-{order_number}.pdf"
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_file_path)
        return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            return None
    with requests.get(final_pdf_url, stream=True) as response:
        if response.status_code == 200:
            s3_client.upload_fileobj(
                response.raw,
                BUCKET_NAME,
                s3_file_path,
                ExtraArgs={
                    "ContentType": "application/pdf",
                    "ContentDisposition": "inline",
                    "ACL": "public-read"
                }
            )
            return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
        return None

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
        existing_id = str(existing_case["_id"]) if existing_case else None

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
        response = session.post("https://efiling.nclt.gov.in/caseHistoryoptional.drt", headers=headers, data=payload)
        filing_no = response.json()['mainpanellist'][0]['filing_no']
        cino = response.json()['mainpanellist'][0]['case_no']
        response_additional = session.get(
            f"https://efiling.nclt.gov.in/caseHistoryalldetails.drt?filing_no={filing_no}&flagIA=false", headers=headers)
        case_history = []
        orders = []
        for idx, entry in enumerate(response_additional.json().get("allproceedingdtls", []), start=1):
            case_history.append({
                "judge": entry.get("bench_location_name") or "Unknown Bench",
                "businessOnDate": entry.get("listing_date") or None,
                "hearingDate": entry.get("next_list_date") or None,
                "purpose": entry.get("purpose") or None,
                "inputType": "automatic",
                "lawyerRemark": "null"
            })
            order_link = stream_upload_order_nclt(entry.get("encPath"), filing_no, str(idx))
            orders.append({
                "order_number": str(idx),
                "order_date": entry.get("order_upload_date"),
                "order_link": order_link
            })
        final_response = {
            "est_code": query.get("est_code"),
            "cino": cino,
            "state_code": query.get("state_code"),
            "court_complex_code": query.get("court_complex_code"),
            "rgyear": query.get("rgyear"),
            "case_type": query.get("case_type"),
            "dist_code": query.get("dist_code"),
            "CNRNumber": cino,
            "CaseStatus": response_additional.json().get('isregistered', [{}])[0].get('status'),
            "CaseType": response.json()['mainpanellist'][0]['case_type_desc_cis'],
            "FilingNumber": filing_no,
            "FirstHearingDate": response_additional.json().get('allfinalstatuslist', [{}])[0].get('listing_date'),
            "RegistrationNumber": filing_no,
            "case_history": case_history,
            "orders": orders,
            "petitioner_and_advocate": [response_additional.json().get('partydetailslist', [{}])[0].get('party_name')],
            "respondent_and_advocate": [response_additional.json().get('partydetailslist', [{}, {}])[1].get('party_name')]
        }
        result = collection.update_one(ac_query, {"$set": final_response}, upsert=True)
        if result.upserted_id:
            final_response["_id"] = str(result.upserted_id)
        else:
            if existing_id:
                final_response["_id"] = existing_id
            else:
                doc = collection.find_one(ac_query)
                final_response["_id"] = str(doc["_id"])

                final_response["_id"] = existing_id
        return JSONResponse(content=final_response, status_code=200)
    finally:
        session.close()
