

import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from core.database import collection
app = APIRouter()



class DRTCaseRequest(BaseModel):
    caseNumber: str
    caseYear: str
    caseTypeID: str
    courtID: str
    stateID: str
    districtID: str
    courtType: str = "drt"



def parse_date(date_str: Optional[str]):
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").isoformat()
    except Exception:
        return None


def uploadOrderDRT(enc_url: Optional[str]):
    
    return enc_url if enc_url else None



@app.post("/drt/getcaseInfo")
def fetch_submit_drt_info(case_data: DRTCaseRequest):

    query = case_data.dict()

    ac_query = {
        "caseNumber": query.get("caseNumber"),
        "caseYear": query.get("caseYear"),
        "caseTypeID": query.get("caseTypeID"),
        "courtID": query.get("courtID"),
        "courtType": "drt"
    }

    existing_case = collection.find_one(ac_query)
    existing_id = str(existing_case["_id"]) if existing_case else None

    baseURL = "https://drt.gov.in/drtapi/getCaseDetailCaseNoWise"

    try:


        formData = {
            "caseNo": query.get("caseNumber"),
            "caseYear": query.get("caseYear"),
            "casetypeId": query.get("caseTypeID"),
            "schemeNameDrtId": query.get("courtID")
        }

        response = requests.post(baseURL, data=formData, timeout=30)
        response.raise_for_status()

        response_data = response.json()



        case_history = []
        orders = []

        for idx, item in enumerate(response_data.get("caseProceedingDetails", []), start=1):
            case_history.append({
                "judge": item.get("judge"),
                "businessOnDate": parse_date(item.get("causelistdate")),
                "hearingDate": parse_date(item.get("dateOfNextHearing")),
                "purpose": item.get("purpose"),
                "inputType": item.get("inputType", "automatic"),
                "lawyerRemark": None if item.get("lawyerRemark") == "null" else item.get("lawyerRemark")
            })

            orders.append({
                "order_number": str(idx),
                "order_date": parse_date(item.get("causelistdate")),
                "order_link": uploadOrderDRT(item.get("orderUrl"))
            })



        final_response = {
            "caseName": f"{response_data.get('respondentName')} vs {response_data.get('petitionerName')}",
            "courtType": "drt",
            "stateID": query.get("stateID"),
            "districtID": query.get("districtID"),
            "caseTypeID": query.get("caseTypeID"),
            "govComplexCode": query.get("courtID"),
            "caseType": response_data.get("casetype"),
            "courtName": response_data.get("courtName"),
            "caseNumber": response_data.get("maincasecaseno"),
            "caseYear": response_data.get("caseyear"),
            "registrationNumber": f"{response_data.get('casetype')}/{query.get('caseNumber')}/{query.get('caseYear')}",
            "govCaseNumber": response_data.get("caseno"),
            "eCourtStage": response_data.get("casestatus"),
            "natureOfDisposal": response_data.get("disposalNature"),
            "courtNumberAndJudge": response_data.get("courtNo"),
            "petitionerName": response_data.get("petitionerName"),
            "respondentName": response_data.get("respondentName"),
            "case_history": case_history,
            "orders": orders,
            "updatedAt": datetime.utcnow().isoformat()
        }

        result = collection.update_one(
            ac_query,
            {"$set": final_response},
            upsert=True
        )

        if result.upserted_id:
            final_response["_id"] = str(result.upserted_id)
        else:
            final_response["_id"] = existing_id

        return JSONResponse(content=final_response, status_code=200)

    except requests.exceptions.HTTPError as error:
        if error.response.status_code == 403:
            return JSONResponse(
                content={"success": False, "message": "DRT service blocked"},
                status_code=403
            )
        raise
