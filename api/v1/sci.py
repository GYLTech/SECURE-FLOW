import base64
import json
import random
import string
import threading
import time
from typing import Optional
import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
import re
from pydantic import BaseModel, field_validator
from pymongo import MongoClient
from dotenv import load_dotenv
import os
from core.s3_client import s3_client
from core.database import collection, save_case
from core.lambda_client import lambda_client
from helpers.solve_captcha import solve_captcha
from helpers.requests import safe_get, safe_post
from helpers.orders import order_pdf_s3_key, stable_order_doc_id

load_dotenv()

BASE_URL = "https://www.sci.gov.in/wp-admin/admin-ajax.php"
CAPTCHA_IMAGE_URL = "https://www.sci.gov.in/?_siwp_captcha&id="

AOR_FORM_URL = "https://www.sci.gov.in/case-status-aor-code/"
AOR_FORM_ID = "sciapi-services-case-status-aor-code"
AOR_ACTION = "get_case_status_aor_code"

PARTY_NAME_FORM_URL = "https://www.sci.gov.in/case-status-party-name/"
PARTY_NAME_FORM_ID = "sciapi-services-case-status-party-name"
PARTY_NAME_ACTION = "get_case_status_party_name"

client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["gylscrdata"]
collection = db["casedetails"]

BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()
MAX_RETRIES = 5
# the solver misreads a share of the arithmetic captchas, so allow a few extra
# reads that never reach the site
MAX_CAPTCHA_READS = 12
FORM_CACHE_TTL = 6 * 60 * 60
NONCE_FAILURE = "Nonce Verification Failed"


def build_case_base_path(case_data: dict):
    return (
        f"cases/"
        f"{case_data['courtType']}/"
        f"{case_data['state_code']}/"
        f"{case_data['dist_code']}/"
        f"{case_data['court_complex_code']}/"
        f"{case_data['rgyear']}/"
        f"{case_data['cino']}/"
    )

def build_orders_prefix(metadata: dict):
    return build_case_base_path(metadata) + "orders/"

def build_case_json_key(metadata: dict):
    return build_case_base_path(metadata) + "metadata.json"

def upload_case_json_to_s3(
    s3_client,
    bucket_name,
    metadata
):
    key = build_case_json_key(metadata)

    payload = {
        **metadata
    }

    s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False),
        ContentType="application/json"
    )

    return f"s3://{bucket_name}/{key}"

def extract_case_data(html_content: str, case_status: str):

    soup = BeautifulSoup(html_content, "html.parser")
    results = []

    rows = soup.select("table tbody tr")

    for row in rows:
        diary_no = row.get("data-diary-no")
        if not diary_no:
            continue
        diary_year = row.get("data-diary-year")

        petitioner = row.select_one("td.petitioners")
        respondent = row.select_one("td.respondents")

        results.append({
            "diary_number": diary_no,
            "year": diary_year,
            "case_status": case_status,
            "petitioner_name": petitioner.get_text(strip=True) if petitioner else None,
            "respondent_name": respondent.get_text(strip=True) if respondent else None
        })

    return results


def cell_text(cells, index):
    return clean_text(cells[index].get_text(" ")) if len(cells) > index else None


def extract_party_name_data(html_content, party_status):
    soup = BeautifulSoup(html_content or "", "html.parser")
    results = []

    for row in soup.select("table tbody tr"):
        diary_no = row.get("data-diary-no")
        if not diary_no:
            continue

        cells = row.select("td")
        petitioner = row.select_one("td.petitioners")
        respondent = row.select_one("td.respondents")
        link = row.select_one("a[href]")

        results.append({
            "diary_number": diary_no,
            "year": row.get("data-diary-year"),
            "case_number": cell_text(cells, 2),
            # the import pipeline reads case_status, the table shows status
            "case_status": party_status,
            "petitioner_name": clean_text(petitioner.get_text(" ")) if petitioner else cell_text(cells, 3),
            "respondent_name": clean_text(respondent.get_text(" ")) if respondent else cell_text(cells, 4),
            "status": cell_text(cells, 5),
            "details_link": (
                "https://www.sci.gov.in/" + link["href"] if link else None
            ),
        })

    return results


_form_cache = {}
_form_lock = threading.Lock()

CAPTCHA_EXPRESSION = re.compile(r"^(\d+)([+\-x*])(\d+)$")


def generate_scid():
    return "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(40)
    )


def evaluate_captcha(expression):
    """SCI captchas are single digit arithmetic ("8 + 5"), so the OCR text must
    carry an operator. A bare number means the operator was lost and the read is
    unusable - answering with it is always wrong, so ask for a fresh captcha
    instead. The OCR also likes to duplicate a digit ("44-1" for "4 - 1"), hence
    reading the operand nearest the operator."""
    if not expression:
        return None

    text = str(expression).strip().replace(" ", "")
    match = CAPTCHA_EXPRESSION.match(text)
    if not match:
        return None

    left, operator, right = int(match.group(1)[-1]), match.group(2), int(match.group(3)[0])
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    return left * right


def results_html(data_value):
    """SCI returns either {"resultsHtml": ...} or the markup directly."""
    if isinstance(data_value, dict):
        return data_value.get("resultsHtml") or ""
    return data_value if isinstance(data_value, str) else ""


def scrape_form_fields(session, form_url, form_id):
    response = safe_get(session, form_url)
    form = BeautifulSoup(response.text, "html.parser").select_one("#" + form_id)
    if not form:
        raise Exception(f"SCI form {form_id} not found")

    return {
        inp.get("name"): inp.get("value", "")
        for inp in form.select("input")
        if inp.get("name")
    }


def get_form_fields(session, form_url, form_id, force_refresh=False):
    with _form_lock:
        cached = _form_cache.get(form_id)
        expired = not cached or time.time() - cached["fetched_at"] > FORM_CACHE_TTL
        if force_refresh or expired:
            _form_cache[form_id] = {
                "fields": scrape_form_fields(session, form_url, form_id),
                "fetched_at": time.time(),
            }
        return dict(_form_cache[form_id]["fields"])


def submit_sci_form(session, form_url, form_id, action, fields):
    force_refresh = False
    submissions = 0

    for _ in range(MAX_CAPTCHA_READS):
        if submissions >= MAX_RETRIES:
            break

        payload = get_form_fields(session, form_url, form_id, force_refresh=force_refresh)
        force_refresh = False
        payload["scid"] = generate_scid()

        captcha_response = safe_get(
            session=session,
            url=CAPTCHA_IMAGE_URL + payload["scid"]
        )
        image_base64 = base64.b64encode(captcha_response.content).decode("utf-8")
        expression = solve_captcha(
            lambda_client=lambda_client, image_base64=image_base64, frm="sci"
        )

        result_captcha = evaluate_captcha(expression)
        if result_captcha is None:
            # unreadable captcha - pull a fresh one without spending a submission
            continue

        submissions += 1
        payload.update(fields)
        payload.update({
            "siwp_captcha_value": str(result_captcha),
            "action": action,
            "es_ajax_request": "1",
            "language": "en",
        })

        headers = {
            "referer": form_url,
            "x-requested-with": "XMLHttpRequest"
        }

        response = safe_post(session, BASE_URL, data=payload, headers=headers)
        response_json = response.json()

        if response_json.get("success") is False:
            if NONCE_FAILURE in str(response_json.get("data", "")):
                force_refresh = True
            continue

        return response_json

    return None


class CaseRequest(BaseModel):
    rgyear: str
    case_no: str
    state_code: Optional[str] = None
    dist_code: Optional[str] = None
    court_complex_code: Optional[str] = None
    est_code: Optional[str] = None
    refresh: int = 0


class CaseRequestAOR(BaseModel):
    aor_code: str
    rgyear: str
    case_status: str
    state_code: Optional[str] = None
    dist_code: Optional[str] = None
    court_complex_code: Optional[str] = None
    est_code: Optional[str] = None


class CaseRequestPartyName(BaseModel):
    party_name: str
    rgyear: str
    party_status: str
    party_type: str = "any"

    @field_validator("party_name")
    @classmethod
    def validate_party_name(cls, value):
        cleaned = clean_text(value or "")
        if len(cleaned) < 2 or not re.search(r"[A-Za-z]", cleaned):
            raise ValueError("party_name must be at least 2 characters and contain a letter")
        if not re.fullmatch(r"[A-Za-z0-9 .,'&/()\-]+", cleaned):
            raise ValueError("party_name contains unsupported characters")
        return cleaned

    @field_validator("party_status")
    @classmethod
    def validate_party_status(cls, value):
        if value not in ("P", "D"):
            raise ValueError("party_status must be 'P' (pending) or 'D' (disposed)")
        return value

    @field_validator("party_type")
    @classmethod
    def validate_party_type(cls, value):
        if value not in ("any", "P", "R"):
            raise ValueError("party_type must be 'any', 'P' or 'R'")
        return value

    @field_validator("rgyear")
    @classmethod
    def validate_rgyear(cls, value):
        if not re.fullmatch(r"\d{4}", value.strip()):
            raise ValueError("rgyear must be a 4 digit year")
        return value.strip()


def clean_text(text):
    return re.sub(r"\s+", " ", text.strip())


def extract_party_details_flexible(soup):
    def extract_list_by_label(label):
        parties = []
        possible_labels = soup.find_all(
            "td", text=re.compile(fr"{label}", re.IGNORECASE))
        for lbl in possible_labels:
            next_sib = lbl.find_next_sibling("td")
            if next_sib:
                text = next_sib.get_text(separator="\n").strip()
                lines = [clean_text(line)
                         for line in text.split("\n") if line.strip()]
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
                entry = {headers[i]: clean_text(
                    cols[i].get_text()) for i in range(len(headers))}
                extracted_data.append(entry)

    return extracted_data


def parse_case_history(html, data):

    soup = BeautifulSoup(html, "html.parser")
    case_labels = ["Diary Number", "Case Number", "CNR Number", "Filed On"]
    status_labels = ["Present/Last Listed On",
                     "Status/Stage", "Category", "Coram"]
    case_data = extract_label_value_pairs(soup, case_labels)
    match = re.search(r"([\w()]+)\s*No\.", case_data.get("Case Number", ""))
    caseTy = match.group(1) if match else None
    status_data = extract_label_value_pairs(soup, status_labels)
    judge_match = re.match(
        r"(\d{2}-\d{2}-\d{4})\s*\[(.*)\]", status_data.get("Present/Last Listed On", ""))
    parties = extract_party_details_flexible(soup)

    result = {
        "est_code": None,
        "cino": case_data.get("CNR Number", ""),
        "state_code": data.state_code,
        "court_complex_code": data.court_complex_code,
        "rgyear": data.rgyear,
        "case_type": caseTy,
        "dist_code": data.dist_code,
        "CNRNumber": case_data.get("CNR Number", ""),
        "CaseStatus": re.match(r"([A-Z\s]+)\s*\(", status_data.get("Status/Stage", "")).group(1).strip() if re.match(r"([A-Z\s]+)\s*\(", status_data.get("Status/Stage", "")) else None,
        "CaseType": caseTy,
        "CourtNumberandJudge": judge_match.group(2).replace("and", ", ") if judge_match else None,
        "DecisionDate": None,
        "FilingNumber": data.case_no + "/" + data.rgyear,
        "NatureofDisposal": None,
        "RegistrationNumber": data.case_no + "/" + data.rgyear,
        "actsandSection": {
            "acts": status_data.get("Category", ""),
            "section": None
        },
        "case_history": [],
        "case_no": data.case_no,
        "courtType": "supremecourt",
        "court_code": data.court_complex_code,
        "orders": [],
        "petitioner_and_advocate": parties.get("petitioner", []),
        "respondent_and_advocate": parties.get("respondent", []),
    }
    return result


@app.post("/sci/getcaseInfo")
def fetch_submit_info(case_data: CaseRequest):
    query = case_data.dict()
    ac_query = {
        "courtType": "supremecourt",
        "case_no": query.get("case_no"),
        "state_code": query.get("state_code"),
        "dist_code": query.get("dist_code"),
        "rgyear": query.get("rgyear")
    }
    print(ac_query)
    existing_case = collection.find_one(ac_query)

    if existing_case and case_data.refresh == 0:
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))
    
    existing_case_id = existing_case["_id"] if existing_case else None

    session = requests.Session()
    try:
        payload = {
            'diary_no': case_data.case_no,
            'diary_year': case_data.rgyear,
            'action': "get_case_details",
            'es_ajax_request': '1',
            'language': 'en'
        }
        headers = {
            'referer': 'https://www.sci.gov.in/case-status-case-no/',
            'x-requested-with': 'XMLHttpRequest'
        }

        response = safe_get(session, BASE_URL, params=payload, headers=headers)
        response_json = response.json()
        data_value = response_json.get("data")
        if not data_value or "No records found" in str(data_value):
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        listing_response = safe_get(session, BASE_URL, params={
            'diary_no': case_data.case_no,
            'diary_year': case_data.rgyear,
            'tab_name': "listing_dates",
            'action': "get_case_details",
            'es_ajax_request': '1',
            'language': 'en'
        }, headers=headers)
        listing_html = listing_response.json().get("data", "")

        order_response = safe_get(session, BASE_URL, params={
            'diary_no': case_data.case_no,
            'diary_year': case_data.rgyear,
            'tab_name': "judgement_orders",
            'action': "get_case_details",
            'es_ajax_request': '1',
            'language': 'en'
        }, headers=headers)
        order_html = order_response.json().get("data", "")

        result = parse_case_history(data_value, case_data)

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

        print(
            f"[sci/getcaseInfo] case_no={case_data.case_no}/{case_data.rgyear} "
            f"listing_html_len={len(listing_html or '')} history_rows={len(case_history)} "
            f"sample_dates={[h.get('hearingDate') for h in case_history[:3]]}"
        )

        orders_prefix = build_case_base_path(result) + "orders/"
        case_json_s3_path = upload_case_json_to_s3(
        s3_client,"dl-shared-gyl-vidilekh",result
        )
        result["s3_prefix"] = case_json_s3_path

        orders = []
        seen_doc_ids = set()
        if order_html:
            order_soup = BeautifulSoup(order_html, "html.parser")
            links = order_soup.find_all("a", href=True)
            for a in links:
                order_date = clean_text(a.text)
                final_pdf_url = a["href"]
                source_ref = final_pdf_url.split("?")[0]
                doc_id = stable_order_doc_id(source_ref)
                if doc_id in seen_doc_ids:
                    continue
                seen_doc_ids.add(doc_id)
                s3_key = order_pdf_s3_key(orders_prefix, order_date, source_ref)
                try:
                    s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_key)
                    s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_key}"
                except s3_client.exceptions.ClientError as e:
                    if e.response['Error']['Code'] == "404":
                        response_pdf = session.get(final_pdf_url, stream=True)
                        if response_pdf.status_code == 200:
                            s3_client.upload_fileobj(
                                response_pdf.raw,
                                BUCKET_NAME,
                                s3_key,
                                ExtraArgs={
                                    'ContentType': 'application/pdf',
                                    'ContentDisposition': 'inline'
                                }
                            )
                            s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_key}"
                        else:
                            s3_url = None
                    else:
                        s3_url = None
                orders.append({
                    "order_number": str(len(orders) + 1),
                    "order_date": order_date,
                    "order_link": s3_url
                })
        result["orders"] = orders
        result["_id"] = save_case(result, existing_case_id)
        return JSONResponse(content=result, status_code=200)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        session.close()


@app.post("/sci/bulk_q/aor")
def fetch_aor_info(case_data: CaseRequestAOR):
    session = requests.Session()

    try:
        response_json = submit_sci_form(
            session,
            AOR_FORM_URL,
            AOR_FORM_ID,
            AOR_ACTION,
            {
                "party_type": "any",
                "aor_code": case_data.aor_code,
                "year": case_data.rgyear,
                "case_status": case_data.case_status,
            },
        )

        if response_json is None:
            return JSONResponse(
                content={"error": "Unable to get response from SCI at this moment"},
                status_code=404
            )

        data_value = response_json.get("data")
        if not data_value or "No records found" in str(data_value):
            return JSONResponse(
                content={"error": "No cases found for this AOR code"},
                status_code=404
            )

        cases = extract_case_data(results_html(data_value), case_data.case_status)
        if not cases:
            return JSONResponse(
                content={"error": "No cases found for this AOR code"},
                status_code=404
            )

        return JSONResponse(content={
            "success": True,
            "count": len(cases),
            "data": cases
        }, status_code=200)

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

    finally:
        session.close()


@app.post("/sci/bulk_q/party_name")
def fetch_party_name_info(case_data: CaseRequestPartyName):
    session = requests.Session()

    try:
        response_json = submit_sci_form(
            session,
            PARTY_NAME_FORM_URL,
            PARTY_NAME_FORM_ID,
            PARTY_NAME_ACTION,
            {
                "party_type": case_data.party_type,
                "party_name": case_data.party_name,
                "year": case_data.rgyear,
                "party_status": case_data.party_status,
            },
        )

        if response_json is None:
            return JSONResponse(
                content={"error": "Unable to get response from SCI at this moment"},
                status_code=404
            )

        data_value = response_json.get("data")
        if not data_value or "No records found" in str(data_value):
            return JSONResponse(
                content={"error": "No cases found for this party name"},
                status_code=404
            )

        cases = extract_party_name_data(results_html(data_value), case_data.party_status)
        if not cases:
            return JSONResponse(
                content={"error": "No cases found for this party name"},
                status_code=404
            )

        return JSONResponse(content={
            "success": True,
            "count": len(cases),
            "data": cases
        }, status_code=200)

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

    finally:
        session.close()
