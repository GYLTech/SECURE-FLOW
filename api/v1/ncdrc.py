

import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from core.database import collection
from bs4 import BeautifulSoup
import re
import html

app = APIRouter()
BASE_URL = "https://e-jagriti.gov.in"


@app.get("/test")
async def test():
    return {"status": "ok"}


class CaseRequest(BaseModel):
    case_type: str
    case_reg_no: str
    rgyear: str
    state_code: Optional[str] = None
    dist_code: Optional[str] = None
    court_complex_code: Optional[str] = None
    est_code: Optional[str] = None
    refresh_flag: str


def clean_text(text):
    return re.sub(r"\s+", " ", text.strip())


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


def extract_ncdrc_case_history(soup):
    history = []
    for table in soup.find_all("table", {"class": "history_table"}):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Cause List Type" in headers and "Purpose of hearing" in headers:
            rows = table.find_all("tr")[1:]
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 5:
                    cause_list_type = cols[0].get_text(strip=True)
                    judge = cols[1].get_text(strip=True)
                    business_on_date = (
                        cols[2].find("a").get_text(strip=True)
                        if cols[2].find("a")
                        else cols[2].get_text(strip=True)
                    )
                    hearing_date = cols[3].get_text(strip=True)
                    purpose = cols[4].get_text(strip=True)
                    if cause_list_type == "Order Number" or "Order on" in judge or purpose in ["Order Details", "View"]:
                        continue
                    history.append({
                        "causeListType": cause_list_type,
                        "judge": judge,
                        "businessOnDate": business_on_date,
                        "hearingDate": hearing_date,
                        "purpose": purpose,
                        "inputType": "automatic",
                        "lawyerRemark": None
                    })
    return history

def parse_case_history(html_content, payload, second_payload, value, session):
    soup = BeautifulSoup(html_content, "html.parser")
    case_details = extract_table_data(soup, "Case Details", [
        "Filing Number", "Filing Date", "Registration Number", "Registration Date", "CNR Number"])
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
    ncdrc_case_history = extract_ncdrc_case_history(soup)

    return {
        "case_no": second_payload.get("case_no"),
        "case_reg_no": payload.get("case_no"),
        "rgyear": payload.get("rgyear"),
        "cino": second_payload.get("cino"),
        "court_code": payload.get("court_code"),
        "state_code": payload.get("state_code"),
        "dist_code": payload.get("dist_code"),
        "court_complex_code": payload.get("court_complex_code"),
        "est_code": payload.get("est_code"),
        "case_type": payload.get("case_type"),
        "FilingNumber": case_details.get("Filing Number", ""),
        "RegistrationNumber": case_details.get("Registration Number", ""),
        "CNRNumber": second_payload.get("cino"),
        "FirstHearingDate": case_status.get("First Hearing Date", ""),
        "CaseStatus": case_status.get("Stage of Case", ""),
        "CourtNumberandJudge": case_status.get("Coram", ""),
        "petitioner_and_advocate": petitioner_and_advocate,
        "respondent_and_advocate": respondent_and_advocate,
        "category_details": category_details,
        "subordinate_court_information": subordinate_court_info,
        "case_history": ncdrc_case_history,
        "case_transfer": [],
        "orders": []  
    }


def clean_ncdrc_response(case_json: dict) -> dict:
    """
    Convert raw NCDRC API response into a clean, structured format
    """
 
    cleaned = {
        "case_number": case_json.get("caseNumber", ""),
        "case_type": case_json.get("caseType", ""),
        "registration_number": case_json.get("registrationNumber", ""),
        "filing_number": case_json.get("filingNumber", ""),
        "first_hearing_date": case_json.get("firstHearingDate", ""),
        "decision_date": case_json.get("decisionDate", ""),
        "case_status": case_json.get("caseStatus", ""),
        "coram": case_json.get("coram", ""),
        "bench_type": case_json.get("benchType", ""),
        "judicial_branch": case_json.get("judicialBranch", ""),
        "petitioner": case_json.get("petitioner", []),
        "respondent": case_json.get("respondent", []),
        "orders": case_json.get("orders", []),
    }
    return cleaned



def clean_proceeding_text(html_text):
    
    decoded = html.unescape(html_text)
    soup = BeautifulSoup(decoded, "html.parser")
    text = soup.get_text(separator=" ")
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


@app.post("/ncdrc/getcaseInfo")
def fetch_submit_ncdrc_info(case_data: CaseRequest):
    session = requests.Session()
    try:
        case_number = f"{case_data.case_type}/{case_data.case_reg_no}/{case_data.rgyear}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://e-jagriti.gov.in/",
            "Origin": "https://e-jagriti.gov.in",
        }

        response = session.get(
            "https://e-jagriti.gov.in/services/case/caseFilingService/v2/getCaseStatus",
            params={"caseNumber": case_number},
            headers=headers
        )

        if response.status_code != 200:
            return {"error": "HTTP error from NCDRC API", "status_code": response.status_code}

        api_json = response.json()
        if not api_json.get("data"):
            return {"error": "Empty response from NCDRC API"}

        data = api_json["data"]

        clean_data = {
            "case_number": data.get("caseNumber", ""),
            "case_type": case_data.case_type,
            "registration_number": str(data.get("fillingReferenceNumber", "")),
            "filing_number": "",  
            "first_hearing_date": data.get("caseFilingDate", "").split("T")[0] if data.get("caseFilingDate") else "",
            "decision_date": "", 
            "case_status": data.get("caseStage", ""),
            "coram": "", 
            "bench_type": "", 
            "judicial_branch": data.get("filedInComissionName", ""),
            "petitioner": [data.get("complainant", "")] if data.get("complainant") else [],
            "respondent": [data.get("respondent", "")] if data.get("respondent") else [],
            "orders": [
                {
                    "date_of_hearing": h.get("dateOfHearing", ""),
                    "next_hearing_date": h.get("dateOfNextHearing", ""),
                    "case_stage": h.get("caseStage", ""),
                    "proceeding_text": clean_proceeding_text(h.get("proceedingText", ""))
                }
                for h in data.get("caseHearingDetails", [])
            ],
            "complainant_advocate": data.get("complainantAdvocate", []),
            "respondent_advocate": data.get("respondentAdvocate", []),
            "attached_ia_applications": data.get("attachedIAApplicationsWithReason", []),
            "case_type_input": case_data.case_type,
            "case_reg_no_input": case_data.case_reg_no,
            "rgyear_input": case_data.rgyear
        }
        return clean_data
    finally:
        session.close()
