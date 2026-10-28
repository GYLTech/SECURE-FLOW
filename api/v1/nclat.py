import html
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal, Optional

import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator

from core.database import collection, save_case
from core.s3_client import s3_client
from helpers.causelist import mirror_cause_lists
from helpers.orders import cached_case_needs_orders, order_pdf_s3_key, orders_stamp

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()

BASE_URL = "https://nclat.nic.in"
BOARD_URL = f"{BASE_URL}/display-board"
CAUSE_LIST_URL = f"{BASE_URL}/daily-cause-list"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
TIMEOUT = (
    float(os.getenv("NCLAT_CONNECT_TIMEOUT", "10")),
    float(os.getenv("NCLAT_READ_TIMEOUT", "90")),
)
MAX_RETRIES = 4
ORDER_WORKERS = int(os.getenv("NCLAT_ORDER_WORKERS", "2"))
BENCHES = {"delhi": "Principal Bench, New Delhi", "chennai": "Chennai Bench"}
ORDER_TYPES = {"D": "Daily Order", "DC": "Daily Order (Corrected)", "J": "Judgement", "JC": "Judgement (Corrected)"}

FORM_TEMPLATE = {
    "location": "",
    "case_type": "",
    "exact_search_word": "1",
    "text_name": "",
    "from_date": "",
    "to_date": "",
    "case_status": "all",
    "select_party": "",
    "party_name": "",
    "diary_no": "",
    "case_number": "",
    "advocate_name": "",
    "case_year": "All",
}


class NCLATError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def request_with_retry(session, method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(2 ** attempt, 10) + random.uniform(0, 1))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"[warn] NCLAT {method} {url} failed (attempt {attempt + 1}/{MAX_RETRIES}): {type(exc).__name__}")
            continue
        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            last_error = f"HTTP {response.status_code}"
            continue
        return response
    raise NCLATError(f"NCLAT did not respond after {MAX_RETRIES} attempts: {last_error!r}", 503)


def open_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    page = request_with_retry(session, "GET", f"{BOARD_URL}/cases")
    token = re.search(r'name="_token" value="([^"]+)"', page.text)
    if not token:
        raise NCLATError("Could not open the NCLAT case search")
    return session, token.group(1)


def post_json(session, token, path, data):
    response = request_with_retry(
        session, "POST", f"{BOARD_URL}/{path}", data={"_token": token, **data},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BOARD_URL}/cases"},
    )
    if response.status_code != 200:
        raise NCLATError(f"NCLAT returned {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise NCLATError("NCLAT returned a non JSON response")


def error_response(exc: Exception):
    if isinstance(exc, NCLATError):
        return JSONResponse(content={"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(content={"error": str(exc)}, status_code=500)


def clean(value):
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value)))
    text = re.sub(r"\s+", " ", text).strip().strip(",").strip()
    return text if text and text.upper() not in {"NULL", "NA", "N/A", "NONE", "UNDEFINED"} else None


def to_ddmmyyyy(value):
    text = clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.split(" ")[0], fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return None


def sort_key(ddmmyyyy):
    try:
        return datetime.strptime(ddmmyyyy, "%d-%m-%Y")
    except (TypeError, ValueError):
        return datetime.max


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def upload_order(session, token, bench, filing_no, order, key):
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return s3_url(key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            return None
    try:
        response = request_with_retry(session, "POST", f"{BOARD_URL}/view_order", data={
            "search_type": "view_order", "_token": token, "bench_name": bench, "filing_no": filing_no,
            "order_date": order.get("order_date"), "order_type": order.get("order_type")})
    except NCLATError as exc:
        print(f"[nclat] order download failed for {key}: {exc}")
        return None
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=response.content,
                         ContentType="application/pdf", ContentDisposition="inline")
    return s3_url(key)


def build_orders(session, token, bench, filing_no, order_history):
    prefix = f"case_data/orders/nclat/{filing_no}/"
    entries, seen = [], set()
    for order in order_history or []:
        source = order.get("order_pdf_download") or f"{order.get('order_date')}-{order.get('order_type')}"
        if source in seen:
            continue
        seen.add(source)
        order_date = to_ddmmyyyy(order.get("order_date"))
        entries.append((order, order_date, order_pdf_s3_key(prefix, order_date, source)))
    entries.sort(key=lambda e: sort_key(e[1]))
    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as pool:
        links = list(pool.map(lambda e: upload_order(session, token, bench, filing_no, e[0], e[2]), entries))
    return [
        {
            "order_number": str(i),
            "order_date": order_date,
            "order_type": ORDER_TYPES.get(order.get("order_type"), clean(order.get("order_type"))),
            "order_link": link,
            "order_status": "available" if link else "unavailable",
        }
        for i, ((order, order_date, _), link) in enumerate(zip(entries, links), start=1)
    ]


def names(rows):
    result = []
    for row in rows or []:
        name = clean(row.get("name") if isinstance(row, dict) else row)
        if name and name not in result:
            result.append(name)
    return result


def court_label(court_no, coram=None):
    return ", ".join(filter(None, [f"Court {court_no}" if court_no else None, clean(coram)])) or None


def transform_case(data, bench, orders, request_fields):
    case = (data.get("case_details") or [{}])[0]
    filing_no = clean(case.get("filing_no"))
    case_type = clean(case.get("case_type"))
    case_no = clean(case.get("case_no"))
    case_year = clean(case.get("case_year"))
    registration = f"{case_type} - {case_no}/{case_year}" if case_type and case_no and case_year else None
    disposed = case.get("status") == "D"
    first = data.get("first_hearing_details") or {}
    last = data.get("last_hearing_details") or {}
    following = data.get("next_hearing_details") or {}

    rows = sorted(data.get("case_history") or [], key=lambda r: r.get("hearing_date") or "")
    case_history, seen = [], set()
    for row in rows:
        heard = to_ddmmyyyy(row.get("hearing_date"))
        if not heard or heard in seen:
            continue
        seen.add(heard)
        case_history.append({
            "judge": court_label(row.get("court_no")),
            "businessOnDate": heard,
            "hearingDate": None,
            "purpose": clean(row.get("purpose")),
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    for current, nxt in zip(case_history, case_history[1:]):
        current["hearingDate"] = nxt["businessOnDate"]

    next_hearing = to_ddmmyyyy(following.get("hearing_date"))
    if next_hearing and case_history and next_hearing == case_history[-1]["businessOnDate"]:
        next_hearing = None
    if case_history and not disposed:
        case_history[-1]["hearingDate"] = next_hearing
    if last.get("coram") and case_history:
        case_history[-1]["judge"] = court_label(last.get("court_no"), last.get("coram"))

    party = data.get("party_details") or {}
    legal = data.get("legal_representative") or {}

    return {
        "courtType": "nclat",
        "case_no": filing_no,
        "cino": registration or filing_no,
        "court_complex_code": bench,
        "state_code": bench,
        "bench_name": f"National Company Law Appellate Tribunal - {BENCHES.get(bench, bench)}",
        "case_reg_no": case_no,
        "rgyear": case_year,
        "case_type": request_fields.get("case_type") or case_type,
        "fir_details": {},
        "CaseType": case_type,
        "FilingNumber": filing_no,
        "RegistrationNumber": registration,
        "CNRNumber": registration or filing_no,
        "FilingDate": to_ddmmyyyy(case.get("date_of_filing")),
        "RegistrationDate": to_ddmmyyyy(case.get("registration_date")),
        "FirstHearingDate": to_ddmmyyyy(first.get("hearing_date")) or (
            case_history[0]["businessOnDate"] if case_history else None),
        "NextHearingDate": None if disposed else next_hearing,
        "DecisionDate": case_history[-1]["businessOnDate"] if disposed and case_history else None,
        "CaseStatus": "Disposed" if disposed else "Pending",
        "CaseStage": clean(following.get("stage_of_case")) or clean(last.get("stage_of_case")),
        "NatureofDisposal": "Disposed" if disposed else None,
        "CourtNumberandJudge": court_label(last.get("court_no"), last.get("coram")),
        "petitioner_and_advocate": names(party.get("applicant_name")) or [""],
        "respondent_and_advocate": names(party.get("respondant_name")) or [""],
        "petitioner_advocates": names(legal.get("applicant_legal_representative_name")),
        "respondent_advocates": names(legal.get("respondent_legal_representative_name")),
        "applications_filed": [
            {"filing_no": clean(ia.get("filing_no")), "case_no": f"{clean(ia.get('case_type'))} - {ia.get('case_no')}/{ia.get('case_year')}",
             "filing_date": to_ddmmyyyy(ia.get("filing_date")), "status": {"D": "Disposed", "P": "Pending"}.get(ia.get("status"))}
            for ia in data.get("ias_other_application") or []
        ],
        "connected_cases": [
            {"filing_no": clean(c.get("filing_no")), "case_no": clean(c.get("case_no"))}
            for c in data.get("connected_cases") or [] if isinstance(c, dict)
        ],
        "actsandSection": {"acts": "Companies Act, 2013 / Insolvency and Bankruptcy Code, 2016", "section": None},
        "case_history": case_history,
        "case_transfer": [],
        "orders": orders,
        "orders_synced_at": orders_stamp(),
    }


def search_rows(session, token, payload):
    body = post_json(session, token, "cases_details", {**FORM_TEMPLATE, **payload})
    if body.get("status") is False:
        errors = body.get("errors") or {}
        message = next((v[0] for v in errors.values() if v), None) if isinstance(errors, dict) else None
        raise NCLATError(message or "Invalid search details", 422)
    results, seen = [], set()
    for row in body.get("data") or []:
        filing = re.search(r'data-filing_no="(\d+)"', str(row[6])) if len(row) > 6 else None
        filing_no = filing.group(1) if filing else clean(row[1])
        if not filing_no or filing_no in seen:
            continue
        seen.add(filing_no)
        title = clean(row[3]) or ""
        petitioner, _, respondent = title.partition(" VS ")
        results.append({
            "bench": payload.get("location"),
            "filing_no": filing_no,
            "case_no": clean(row[2]),
            "petitioner_name": clean(petitioner),
            "respondent_name": clean(respondent),
            "registration_date": to_ddmmyyyy(row[4]),
            "status": clean(row[5]),
        })
    return results


class CaseRequest(BaseModel):
    bench: Literal["delhi", "chennai"] = "delhi"
    filing_no: Optional[str] = None
    case_type: Optional[str] = None
    case_no: Optional[str] = None
    case_year: Optional[str] = None
    refresh: int = 0

    @model_validator(mode="after")
    def check_identifiers(self):
        if self.filing_no:
            self.filing_no = re.sub(r"\D", "", self.filing_no)
            return self
        if self.case_type and self.case_no and self.case_year:
            return self
        raise ValueError("Enter the filing number, or the case type, number and year")


@app.post("/nclat/getcaseInfo")
def fetch_nclat_case(case_data: CaseRequest):
    if case_data.filing_no:
        ac_query = {"courtType": "nclat", "FilingNumber": case_data.filing_no}
    else:
        ac_query = {"courtType": "nclat", "court_complex_code": case_data.bench, "case_type": case_data.case_type,
                    "case_reg_no": case_data.case_no, "rgyear": case_data.case_year}
    existing_case = collection.find_one(ac_query)
    if existing_case and case_data.refresh == 0 and not cached_case_needs_orders(existing_case):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    try:
        session, token = open_session()
        filing_no = case_data.filing_no
        if not filing_no:
            rows = search_rows(session, token, {
                "search_by": "case_no_wise", "location": case_data.bench, "case_type": case_data.case_type,
                "case_number": case_data.case_no, "case_year": case_data.case_year})
            if not rows:
                return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)
            filing_no = rows[0]["filing_no"]

        body = post_json(session, token, "view_details",
                         {"search_type": "view_details", "filing_no": filing_no, "bench_name": case_data.bench})
        data = body.get("data") or {}
        if not data.get("case_details"):
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        orders = build_orders(session, token, case_data.bench, filing_no, data.get("order_history"))
        result = transform_case(data, case_data.bench, orders, case_data.model_dump())
        result["_id"] = save_case(result, existing_case["_id"] if existing_case else None)
        return JSONResponse(content=jsonable_encoder(result), status_code=200)
    except Exception as exc:
        return error_response(exc)


class SearchRequest(BaseModel):
    bench: Literal["delhi", "chennai"] = "delhi"
    search_by: Literal["party", "advocate"]
    name: str
    party_role: Literal["1", "2"] = "1"
    case_status: Literal["all", "P", "D"] = "all"
    match: Literal["1", "2"] = "1"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = clean(value) or ""
        if len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value


@app.post("/nclat/bulk_q/search")
def nclat_search(search: SearchRequest):
    try:
        session, token = open_session()
        if search.search_by == "party":
            payload = {"search_by": "party_wise", "location": search.bench, "select_party": search.party_role,
                       "party_name": search.name, "case_status": search.case_status,
                       "exact_search_word": search.match}
        else:
            payload = {"search_by": "advocate_wise", "location": search.bench, "advocate_name": search.name,
                       "case_status": search.case_status, "exact_search_word": search.match}
        return JSONResponse(content={"data": search_rows(session, token, payload)})
    except Exception as exc:
        return error_response(exc)


class CauseListRequest(BaseModel):
    hearing_date: str

    @field_validator("hearing_date")
    @classmethod
    def validate_date(cls, value):
        datetime.strptime(value, "%Y-%m-%d")
        return value


@app.post("/nclat/causelist")
def cause_list(request: CauseListRequest):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        response = request_with_retry(session, "GET", CAUSE_LIST_URL, params={
            "field_final_date_value": request.hearing_date, "field_final_date_value_1": request.hearing_date})
        lists = []
        for row in re.findall(r"<tr.*?</tr>", response.text, re.S):
            cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            link = re.search(r'href="([^"]+\.pdf)"', row)
            if len(cells) < 4 or not link:
                continue
            href = link.group(1)
            lists.append({
                "court_id": str(len(lists) + 1),
                "court_name": cells[1],
                "description": cells[2],
                "causelist_link": href if href.startswith("http") else f"{BASE_URL}{href}",
            })
        return JSONResponse(content={"data": mirror_cause_lists(lists, "nclat")})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


@app.get("/nclat/display_board")
def display_board():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"})
    try:
        response = request_with_retry(session, "GET", f"{BOARD_URL}/user-display-board/get-list")
        cases = (response.json().get("data") or {}).get("cases") or []
        board = []
        for row in cases:
            sitting = bool(row.get("item_number"))
            case_number = clean(row.get("case_number"))
            board.append({
                "bench": f"{clean(row.get('court_name'))} ({clean(row.get('bench_name'))})",
                "item_number": row.get("item_number"),
                "case_no": f"{case_number}/{row.get('case_year')}" if case_number and row.get("case_year") else case_number,
                "cause_title": clean(row.get("case_title")),
                "sitting": sitting,
            })
        board.sort(key=lambda b: not b["sitting"])
        return JSONResponse(content={"data": board})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()
