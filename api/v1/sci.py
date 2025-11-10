from typing import Optional
import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
import re
from pydantic import BaseModel
from pymongo import MongoClient
from dotenv import load_dotenv
import os
from core.s3_client import s3_client

load_dotenv()

BASE_URL = "https://www.sci.gov.in/wp-admin/admin-ajax.php"

client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["gylscrdata"]
collection = db["casedetails"]

BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()

class CaseRequest(BaseModel):
    diary_year: str
    diary_no: str
    state_code: Optional[str] = None
    dist_code: Optional[str] = None
    court_complex_code: Optional[str] = None
    est_code: Optional[str] = None

def clean_text(text):
    return re.sub(r"\s+", " ", text.strip())

def extract_party_details_flexible(soup):
    def extract_list_by_label(label):
        parties = []
        possible_labels = soup.find_all("td", text=re.compile(fr"{label}", re.IGNORECASE))
        for lbl in possible_labels:
            next_sib = lbl.find_next_sibling("td")
            if next_sib:
                text = next_sib.get_text(separator="\n").strip()
                lines = [clean_text(line) for line in text.split("\n") if line.strip()]
                cleaned = [re.sub(r"^\d+\s*", "", line) for line in lines]
                parties.extend(cleaned)
        return parties

    return {
        "petitioner": extract_list_by_label("Petitioner"),
        "respondent": extract_list_by_label("Respondent")
    }

def extract_label_value_pairs(soup, labels):
    tds = [td.get_text(strip=True) for td in soup.find_all("td")]
    data = {}
    for label in labels:
        try:
            idx = tds.index(label)
            data[label] = tds[idx + 1] if idx + 1 < len(tds) else None
        except ValueError:
            data[label] = None
    return data

def extract_table_with_headers(soup, className=None, headers=None):
    extracted_data = []
    table = None

    if className:
        table = soup.find("table", class_=className)

    if table and headers:
        rows = table.find_all("tr")[1:] 
        for row in rows:
            cols = row.find_all("td")
            if len(cols) == len(headers):
                entry = {headers[i]: clean_text(cols[i].get_text()) for i in range(len(headers))}
                extracted_data.append(entry)

    return extracted_data

def parse_case_history(html, data, session):

    soup = BeautifulSoup(html, "html.parser")
    case_labels = ["Diary Number", "Case Number", "CNR Number", "Filed On"]
    status_labels = ["Present/Last Listed On", "Status/Stage", "Category", "Coram"]
    case_data = extract_label_value_pairs(soup, case_labels)
    match = re.search(r"([\w()]+)\s*No\.", case_data.get("Case Number", ""))
    caseTy = match.group(1) if match else None
    status_data = extract_label_value_pairs(soup, status_labels)
    judge_match = re.match(r"(\d{2}-\d{2}-\d{4})\s*\[(.*)\]", status_data.get("Present/Last Listed On", ""))
    parties = extract_party_details_flexible(soup)

    result = {
    "est_code": None,
    "cino": case_data.get("CNR Number", ""),
    "state_code": data.state_code,
    "court_complex_code": data.court_complex_code,
    "rgyear": data.diary_year,
    "case_type": caseTy,
    "dist_code": data.dist_code,
    "CNRNumber": case_data.get("CNR Number", ""),
    "CaseStatus": re.match(r"([A-Z\s]+)\s*\(", status_data.get("Status/Stage", "")).group(1).strip() if re.match(r"([A-Z\s]+)\s*\(", status_data.get("Status/Stage", "")) else None,
    "CaseType": caseTy,
    "CourtNumberandJudge": judge_match.group(2).replace("and", ", ") if judge_match else None,
    "DecisionDate": None,
    "FilingNumber": data.diary_no + "/" + data.diary_year,
    "NatureofDisposal": None,
    "RegistrationNumber": data.diary_no + "/" + data.diary_year,
    "actsandSection": {
        "acts": status_data.get("Category", ""),
        "section": None
    },
    "case_history": [],
    "case_no": data.diary_no,
    "courtType": "sci",
    "court_code": data.court_complex_code,
    "orders": [],
    "petitioner_and_advocate": parties.get("petitioner", []),
    "respondent_and_advocate": parties.get("respondent", []),
    }
    return result

@app.post("/sci/getcaseInfo")
def fetch_submit_info(case_data: CaseRequest):
    session = requests.Session()
    try:
        payload = {
            'diary_no': case_data.diary_no,
            'diary_year': case_data.diary_year,
            'action': "get_case_details",
            'es_ajax_request': '1',
            'language': 'en'
        }
        headers = {
            'referer': 'https://www.sci.gov.in/case-status-case-no/',
            'x-requested-with': 'XMLHttpRequest'
        }

        response = session.get(BASE_URL, params=payload, headers=headers)
        response_json = response.json()
        data_value = response_json.get("data")
        if not data_value or "No records found" in str(data_value):
           return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        listing_response = session.get(BASE_URL, params={
            'diary_no': case_data.diary_no,
            'diary_year': case_data.diary_year,
            'tab_name': "listing_dates",
            'action': "get_case_details",
            'es_ajax_request': '1',
            'language': 'en'
        }, headers=headers)
        listing_html = listing_response.json().get("data", "")

        order_response = session.get(BASE_URL, params={
            'diary_no': case_data.diary_no,
            'diary_year': case_data.diary_year,
            'tab_name': "judgement_orders",
            'action': "get_case_details",
            'es_ajax_request': '1',
            'language': 'en'
        }, headers=headers)
        order_html = order_response.json().get("data", "")

        result = parse_case_history(data_value, case_data, session)

        case_history = []
        if listing_html:
            listing_soup = BeautifulSoup(listing_html, "html.parser")
            rows = listing_soup.find_all("tr")[2:]
            for row in rows:
                cols = [clean_text(td.get_text()) for td in row.find_all("td")]
                if len(cols) >= 8:
                    case_history.append({
                        "judge": cols[5] if len(cols) > 5 else "",
                        "businessOnDate": cols[0],
                        "hearingDate": cols[0],
                        "purpose": cols[3],
                        "inputType": "automatic",
                        "lawyerRemark": cols[7] if len(cols) > 7 else "null"
                    })
        result["case_history"] = case_history

        orders = []
        if order_html:
            order_soup = BeautifulSoup(order_html, "html.parser")
            links = order_soup.find_all("a", href=True)
            for idx, a in enumerate(links, start=1):
                order_date = clean_text(a.text)
                final_pdf_url = a["href"]
                order_number = str(idx)
                s3_folder_path = f"case_data/orders/{result['cino']}/"
                s3_file_path = f"{s3_folder_path}{result['cino']}-{order_number}.pdf"
                try:
                    s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_file_path)
                    s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
                except s3_client.exceptions.ClientError as e:
                    if e.response['Error']['Code'] == "404":
                        response_pdf = session.get(final_pdf_url, stream=True)
                        if response_pdf.status_code == 200:
                            s3_client.upload_fileobj(
                                response_pdf.raw,
                                BUCKET_NAME,
                                s3_file_path,
                                ExtraArgs={
                                    'ContentType': 'application/pdf',
                                    'ContentDisposition': 'inline'
                                }
                            )
                            s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
                        else:
                            s3_url = None
                    else:
                        s3_url = None
                orders.append({
                    "order_number": order_number,
                    "order_date": order_date,
                    "order_link": s3_url
                })
        result["orders"] = orders
        return JSONResponse(content=result, status_code=200)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        session.close()
