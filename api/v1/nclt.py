import requests
from bs4 import BeautifulSoup
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
BASE_URL = "https://hcservices.ecourts.gov.in/hcservices/"

app = APIRouter()


class CaseRequest(BaseModel):
    case_type: str
    case_reg_no: str
    rgyear: str
    state_code: str
    dist_code: str
    court_complex_code: str
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
        lines = [line.strip()
                 for line in element.get_text().split("\n") if line.strip()]
        if lines:
            name = lines[0]

            if name[0].isdigit() and ")" in name:
                name = name.split(")", 1)[1].strip()

            extracted_data.append(name)

    return extracted_data


def extract_table_data(soup, heading, fields):
    extracted_data = {}
    heading_element = soup.find(
        "h2", string=re.compile(heading, re.IGNORECASE))
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
    labels = court_info.find_all(
        "span", style="width:150px;display:inline-block;")
    values = court_info.find_all("label", style="text-align:left")

    for label, value in zip(labels, values):
        key = label.text.strip().replace(":", "")
        val = value.text.strip()
        details[key] = val

    return details


def extract_high_court_case_history(soup):
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
                    if (
                        cause_list_type == "Order Number"
                        or "Order on" in judge
                        or purpose in ["Order Details", "View"]
                    ):
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


def extract_and_upload_orders(soup, s3_client, session, BUCKET_NAME, REGION_NAME, value):
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

        s3_folder_path = f"case_data/orders/{value}/"
        s3_file_path = f"{s3_folder_path}{value}-{order_number}.pdf"

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


def parse_case_history(html, payload, second_payload, value, session):
    soup = BeautifulSoup(html, "html.parser")

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

    petitioner_and_advocate = extract_party_details(
        soup, "Petitioner_Advocate_table")
    respondent_and_advocate = extract_party_details(
        soup, "Respondent_Advocate_table")
    category_details = extract_table_data(
        soup, "Category", ["Category", "Sub Category"])
    subordinate_court_info = extract_subordinate_court_info(soup)
    high_court_case_history = extract_high_court_case_history(soup)
    orders_hc = extract_and_upload_orders(
        soup,
        s3_client=s3_client,
        session=session,
        BUCKET_NAME=BUCKET_NAME,
        REGION_NAME=REGION_NAME,
        value=value
    )

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
        "CaseType": payload.get("CaseType"),
        "FilingNumber": case_details.get("Filing Number", ""),
        "RegistrationNumber": case_details.get("Registration Number", ""),
        "CNRNumber": second_payload.get("cino"),
        "FirstHearingDate": case_status.get("First Hearing Date", ""),
        "CaseStatus": case_status.get("Stage of Case", ""),
        "CourtNumberandJudge": case_status.get("Coram", ""),
        "petitioner_and_advocate": petitioner_and_advocate,
        "respondent_and_advocate": respondent_and_advocate,
        "actsandSection": {},
        "category_details": category_details,
        "subordinate_court_information": subordinate_court_info,
        "case_history": high_court_case_history,
        "case_transfer": [],
        "orders": orders_hc
    }


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
    if case_data.refresh_flag != "1":
        existing_case = collection.find_one(query)
        if existing_case:
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=existing_case)

    try:
        payload = {
            "action_code": "showRecords",
            "state_code": case_data.state_code,
            "dist_code": "1",
            "case_type": case_data.case_type,
            "case_no": case_data.case_reg_no,
            "rgyear": case_data.rgyear,
            "caseNoType": "new",
            "displayOldCaseNo": "NO",
            "captcha": "",
            "court_code": case_data.court_complex_code,
        }

        headers = {
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://hcservices.ecourts.gov.in',
        }
        response = session.post(
            "https://hcservices.ecourts.gov.in/ecourtindiaHC/cases/case_no_qry.php", headers=headers, data=payload)

        clean_text = response.text.lstrip('\ufeff')
        clean_text = clean_text.replace("<br/>", " ")
        decoded = html.unescape(html.unescape(clean_text))
        values = decoded.split("~")
        values[-1] = values[-1].replace("##", "")
        print(values)
        second_payload = {
            "court_code": case_data.court_complex_code,
            "state_code": case_data.state_code,
            "court_complex_code": case_data.court_complex_code,
            "case_no": values[0],
            "cino": values[3],
        }
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

        testdata = parse_case_history(
            second_resp.text, payload, second_payload, values[3], session)
        result = collection.update_one(
            ac_query, {"$set": testdata}, upsert=True)
        if result.upserted_id:
            testdata["_id"] = str(result.upserted_id)
        else:
            doc = collection.find_one(ac_query)
            testdata["_id"] = str(doc["_id"])

        return testdata

    finally:
        session.close()
