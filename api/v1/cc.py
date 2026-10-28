import base64
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Literal, Optional

import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from core.database import collection, save_case
from core.s3_client import s3_client
from helpers.orders import cached_case_needs_orders, orders_stamp

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()

BASE_URL = "https://e-jagriti.gov.in/services"
CASE_SERVICE = f"{BASE_URL}/case/caseFilingService/v2"
REPORT_SERVICE = f"{BASE_URL}/report/report"
JUDGEMENT_SERVICE = f"{BASE_URL}/courtmaster/courtRoom/judgement/v1"
COURTROOM_SERVICE = f"{BASE_URL}/courtmaster/courtRoom/v1"
CAUSELIST_URL = f"{BASE_URL}/v1/dashboard/causelist"

NCDRC_COMMISSION_ID = "11000000"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://e-jagriti.gov.in/",
    "Referer": "https://e-jagriti.gov.in/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
}

TIMEOUT = (
    float(os.getenv("EJAGRITI_CONNECT_TIMEOUT", "10")),
    float(os.getenv("EJAGRITI_READ_TIMEOUT", "90")),
)
MAX_RETRIES = 3
ORDER_WORKERS = int(os.getenv("EJAGRITI_ORDER_WORKERS", "4"))
SEARCH_WORKERS = 4
FILED_CACHE_SECONDS = int(os.getenv("EJAGRITI_FILED_CACHE_SECONDS", str(6 * 3600)))
_filed_cache = {}

CASE_TYPES = {
    1: ("CC", "Consumer Complaint"),
    2: ("RP", "Revision Petition"),
    3: ("TA", "Transfer Application"),
    4: ("EA", "Execution Application"),
    5: ("RA", "Review Application"),
    7: ("CV", "Caveat Application"),
    8: ("FA", "First Appeal"),
    9: ("MA", "Miscellaneous Application"),
    10: ("IA", "Interlocutory Application"),
    14: ("AE", "Appeal Execution"),
    16: ("SA", "Second Appeal"),
    17: ("CCPAA", "CCPA Appeal"),
    18: ("DN", "Diary Case"),
}
CASE_TYPES_BY_CODE = {code: (type_id, name) for type_id, (code, name) in CASE_TYPES.items()}

DISPOSED_STAGES = {
    "DISPOSED OFF", "DISPOSED", "ALLOWED", "DISMISSED", "PARTLY ALLOWED",
    "WITHDRAWN", "DISMISSED AS WITHDRAWN", "DISMISSED IN DEFAULT",
    "DISMISSED FOR NON PROSECUTION", "SETTLED", "CLOSED", "RETURNED",
    "REJECTED", "TRANSFERRED", "ABATED", "COMPROMISED",
}

PARTY_FIELDS = {
    "complainant": ("complainantName",),
    "respondent": ("respondentName",),
    "complainant_advocate": ("complainantAdvocateName",),
    "respondent_advocate": ("respondentAdvocateName",),
    "party": ("complainantName", "respondentName"),
    "advocate": ("complainantAdvocateName", "respondentAdvocateName"),
}

ORDER_SEARCH_TYPES = {
    "case_number": 1,
    "complainant": 2,
    "respondent": 3,
    "complainant_advocate": 4,
    "respondent_advocate": 5,
    "free_text": 8,
}


class EJagritiError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def ejagriti_request(session, method, url, origin=True, **kwargs):
    headers = dict(HEADERS)
    if not origin:
        headers.pop("Origin")
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
        try:
            response = session.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"[warn] e-Jagriti {method} {url} failed (attempt {attempt + 1}/{MAX_RETRIES}): "
                  f"{type(exc).__name__}")
            continue

        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            last_error = f"HTTP {response.status_code}"
            continue

        try:
            body = response.json()
        except ValueError:
            raise EJagritiError(f"e-Jagriti returned a non JSON response ({response.status_code})")

        if response.status_code == 400 or str(body.get("error")).lower() == "true":
            raise EJagritiError(body.get("message") or "Details not available", 404)
        if response.status_code != 200:
            raise EJagritiError(body.get("message") or f"e-Jagriti returned {response.status_code}")
        return body

    raise EJagritiError(f"e-Jagriti did not respond after {MAX_RETRIES} attempts: {last_error!r}", 503)


def error_response(exc: Exception):
    if isinstance(exc, EJagritiError):
        return JSONResponse(content={"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(content={"error": str(exc)}, status_code=500)


def clean(value):
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if text and text.upper() not in {"NULL", "NA", "N/A", "NONE"} else None


def iso_to_ddmmyyyy(value):
    text = clean(value)
    if not text:
        return None
    if re.match(r"^\d{2}-\d{2}-\d{4}$", text):
        return text
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return None


def sort_key(ddmmyyyy):
    try:
        return datetime.strptime(ddmmyyyy, "%d-%m-%Y")
    except (TypeError, ValueError):
        return datetime.max


def normalize_case_number(case_no: str) -> str:
    return re.sub(r"\s+", "", case_no or "").strip("/").upper()


def resolve_case_type(case_type_id, case_number):
    try:
        known = CASE_TYPES.get(int(case_type_id))
    except (TypeError, ValueError):
        known = None
    if known:
        return int(case_type_id), known[0], known[1]

    parts = (case_number or "").split("/")
    code = parts[-3] if len(parts) >= 3 else None
    by_code = CASE_TYPES_BY_CODE.get(code)
    if by_code:
        return by_code[0], code, by_code[1]
    return None, None, None


def is_html(document: str) -> bool:
    return document.lstrip()[:20].lower().startswith(("<html", "<!doctype", "<table", "<p", "<div"))


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def s3_exists(key):
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_document(document: str, key_base: str):
    if not document:
        return None
    if is_html(document):
        key, body, content_type = f"{key_base}.html", document.encode("utf-8"), "text/html; charset=utf-8"
    else:
        key, content_type = f"{key_base}.pdf", "application/pdf"
        body = None

    if s3_exists(key):
        return s3_url(key)

    if body is None:
        try:
            body = base64.b64decode(document)
        except (ValueError, TypeError):
            return None

    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=body,
        ContentType=content_type,
        ContentDisposition="inline",
    )
    return s3_url(key)


def case_slug(case_number: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "-", case_number).strip("-")


def order_key_base(case_number, hearing_date, order_type_id):
    kind = "judgement" if str(order_type_id) == "2" else "order"
    return f"case_data/orders/consumer/{case_slug(case_number)}/{kind}-{hearing_date}"


def fetch_order_document(filing_reference_number, hearing_date, order_type_id):
    session = requests.Session()
    try:
        body = ejagriti_request(
            session, "GET", f"{JUDGEMENT_SERVICE}/getDailyOrderJudgementPdf",
            params={
                "filingReferenceNumber": filing_reference_number,
                "dateOfHearing": hearing_date,
                "orderTypeId": order_type_id,
            },
        )
        return (body.get("data") or {}).get("dailyOrderPdf")
    finally:
        session.close()


def load_order(case_number, filing_reference_number, hearing):
    hearing_date = hearing["dateOfHearing"]
    order_type_id = hearing.get("orderTypeId") or 1
    key_base = order_key_base(case_number, hearing_date, order_type_id)

    for ext in (".pdf", ".html"):
        if s3_exists(key_base + ext):
            return s3_url(key_base + ext)
    try:
        document = fetch_order_document(filing_reference_number, hearing_date, order_type_id)
    except EJagritiError as exc:
        print(f"[cc] no order for {case_number} on {hearing_date}: {exc}")
        return None
    return upload_document(document, key_base)


def build_orders(case_number, filing_reference_number, hearings):
    with_orders = [
        h for h in hearings
        if h.get("dateOfHearing") and str(h.get("dailyOrderStatus")).lower() == "true"
    ]
    with_orders.sort(key=lambda h: h["dateOfHearing"])

    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as pool:
        links = list(pool.map(
            lambda h: load_order(case_number, filing_reference_number, h), with_orders))

    orders = []
    for hearing, link in zip(with_orders, links):
        orders.append({
            "order_number": str(len(orders) + 1),
            "order_date": iso_to_ddmmyyyy(hearing["dateOfHearing"]),
            "order_type": "Judgement" if str(hearing.get("orderTypeId")) == "2" else "Daily Order",
            "order_link": link,
            "order_status": "available" if link else "unavailable",
        })
    return orders


def transform_case(status: dict, history: list, orders: list):
    case_number = status.get("caseNumber")
    hearings = status.get("caseHearingDetails") or []

    judges_by_date = {}
    for row in history or []:
        action_date = iso_to_ddmmyyyy(row.get("dateOfAction"))
        if not action_date:
            continue
        judges = clean(row.get("judgesName"))
        room = clean(row.get("courtRoomName"))
        label = ", ".join(filter(None, [f"Court {room}" if room else None, judges]))
        judges_by_date[action_date] = label or None

    case_history = []
    seen = set()
    for hearing in hearings:
        business_on = iso_to_ddmmyyyy(hearing.get("dateOfHearing"))
        next_date = iso_to_ddmmyyyy(hearing.get("dateOfNextHearing"))
        if not business_on and not next_date:
            continue
        if (business_on, next_date) in seen:
            continue
        seen.add((business_on, next_date))
        case_history.append({
            "judge": judges_by_date.get(business_on),
            "businessOnDate": business_on,
            "hearingDate": next_date,
            "purpose": clean(hearing.get("caseStage")),
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    case_history.sort(key=lambda h: (
        sort_key(h["businessOnDate"] or h["hearingDate"]), h["businessOnDate"] is not None))

    stage = clean(status.get("caseStage"))
    next_hearing = iso_to_ddmmyyyy(status.get("dateOfNextearing"))
    disposed = bool(stage) and stage.upper() in DISPOSED_STAGES and not next_hearing

    heard_dates = [h["businessOnDate"] for h in case_history if h["businessOnDate"]]
    first_hearing = heard_dates[0] if heard_dates else (
        case_history[0]["hearingDate"] if case_history else None)
    last_heard = heard_dates[-1] if heard_dates else None

    type_id, type_code, type_name = resolve_case_type(status.get("caseTypeId"), case_number)

    latest_bench = None
    for heard in reversed(heard_dates):
        if judges_by_date.get(heard):
            latest_bench = judges_by_date[heard]
            break

    complainant_advocates = [a for a in map(clean, status.get("complainantAdvocate") or []) if a]
    respondent_advocates = [a for a in map(clean, status.get("respondentAdvocate") or []) if a]
    filing_reference = status.get("fillingReferenceNumber")

    return {
        "courtType": "consumercourt",
        "case_no": case_number,
        "cino": case_number,
        "case_type": str(type_id) if type_id else None,
        "case_type_code": type_code,
        "commission_name": clean(status.get("filedInComissionName")),
        "fir_details": {},
        "CaseType": type_name,
        "FilingNumber": str(filing_reference) if filing_reference else None,
        "RegistrationNumber": case_number,
        "CNRNumber": case_number,
        "FilingDate": iso_to_ddmmyyyy(status.get("caseFilingDate")),
        "FirstHearingDate": first_hearing,
        "NextHearingDate": None if disposed else next_hearing,
        "DecisionDate": last_heard if disposed else None,
        "CaseStatus": stage,
        "CaseStage": stage,
        "NatureofDisposal": stage if disposed else None,
        "CourtNumberandJudge": latest_bench,
        "petitioner_and_advocate": [clean(status.get("complainant")) or ""],
        "respondent_and_advocate": [clean(status.get("respondent")) or ""],
        "complainant_advocates": complainant_advocates,
        "respondent_advocates": respondent_advocates,
        "applications_filed": status.get("applicationsFiled") or [],
        "lower_court_cases": status.get("attachedLowerCourt") or [],
        "cause_of_action_date": iso_to_ddmmyyyy(status.get("dateOfCause")),
        "actsandSection": {"acts": "Consumer Protection Act, 2019", "section": None},
        "case_history": case_history,
        "case_transfer": [],
        "orders": orders,
        "orders_synced_at": orders_stamp(),
    }


class CaseRequest(BaseModel):
    case_no: str
    refresh: int = 0

    @field_validator("case_no")
    @classmethod
    def validate_case_no(cls, value):
        value = normalize_case_number(value)
        if value.count("/") < 2:
            raise ValueError("Enter the full e-Jagriti case number, e.g. NC/CC/30/2025")
        return value


@app.post("/cc/getcaseInfo")
def fetch_consumer_case(case_data: CaseRequest):
    ac_query = {"courtType": "consumercourt", "cino": case_data.case_no}
    existing_case = collection.find_one(ac_query)

    if (
        existing_case
        and case_data.refresh == 0
        and not cached_case_needs_orders(existing_case)
    ):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    existing_case_id = existing_case["_id"] if existing_case else None
    session = requests.Session()
    try:
        status = ejagriti_request(
            session, "GET", f"{CASE_SERVICE}/getCaseStatus",
            params={"caseNumber": case_data.case_no},
        ).get("data")
        if not status or not status.get("caseNumber"):
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        try:
            history = ejagriti_request(
                session, "GET", f"{CASE_SERVICE}/getCaseDetailForHistoryDashBoard",
                params={"caseNumber": case_data.case_no},
            ).get("data") or []
        except EJagritiError as exc:
            print(f"[cc] no history for {case_data.case_no}: {exc}")
            history = []

        orders = build_orders(
            status["caseNumber"],
            status.get("fillingReferenceNumber"),
            status.get("caseHearingDetails") or [],
        )
        result = transform_case(status, history, orders)
        result["_id"] = save_case(result, existing_case_id)
        return JSONResponse(content=jsonable_encoder(result), status_code=200)
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


def month_windows(start: date, end: date):
    cursor = start
    while cursor <= end:
        next_month = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        window_end = min(next_month - timedelta(days=1), end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


class SearchWindow(BaseModel):
    commission_id: str
    from_date: str
    to_date: str

    @field_validator("from_date", "to_date")
    @classmethod
    def validate_date(cls, value):
        parse_iso_date(value)
        return value

    def window(self):
        start, end = parse_iso_date(self.from_date), parse_iso_date(self.to_date)
        if end < start:
            raise ValueError("To date must be after from date")
        return start, end


class AdvanceSearchRequest(SearchWindow):
    search_by: Literal[
        "complainant", "respondent", "complainant_advocate",
        "respondent_advocate", "party", "advocate",
    ]
    search_value: str
    case_type: Optional[str] = None

    @field_validator("search_value")
    @classmethod
    def validate_search_value(cls, value):
        value = clean(value) or ""
        if len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value


def fetch_filed_cases(commission_id, start, end):
    key = (str(commission_id), start, end)
    cached = _filed_cache.get(key)
    if cached and time.time() - cached[0] < FILED_CACHE_SECONDS:
        return cached[1]
    rows = _fetch_filed_cases(commission_id, start, end)
    if end < date.today():
        _filed_cache[key] = (time.time(), rows)
    return rows


def _fetch_filed_cases(commission_id, start, end):
    session = requests.Session()
    try:
        body = ejagriti_request(
            session, "POST", f"{REPORT_SERVICE}/advance-search-case-details",
            json={
                "commissionId": commission_id,
                "fromDate": start.isoformat(),
                "toDate": end.isoformat(),
                "selectAnyOne": "EitherOfAbove",
                "searchOptionDropdownOne": "",
                "searchOptionValueOne": "",
            },
        )
        return body.get("data") or []
    except EJagritiError as exc:
        if exc.status_code == 404:
            return []
        raise
    finally:
        session.close()


def search_row(row):
    case_number = row.get("caseNumber")
    _, type_code, type_name = resolve_case_type(None, case_number)
    return {
        "case_no": case_number,
        "case_type": clean(row.get("caseTypeName")) or type_name,
        "case_type_code": type_code,
        "commission_name": clean(row.get("commissionName")),
        "filing_reference_number": row.get("filingReferenceNumber"),
        "complainant_name": clean(row.get("complainantName")),
        "respondent_name": clean(row.get("respondentName")),
        "complainant_advocate": clean(row.get("complainantAdvocateName")),
        "respondent_advocate": clean(row.get("respondentAdvocateName")),
        "case_stage": clean(row.get("caseStageName")),
        "case_category": clean(row.get("caseCategoryName")),
        "filing_date": iso_to_ddmmyyyy(row.get("caseFilingDate")),
        "last_hearing_date": iso_to_ddmmyyyy(row.get("dateOfHearing")),
        "next_hearing_date": iso_to_ddmmyyyy(row.get("dateOfNextHearing")),
    }


@app.post("/cc/bulk_q/advance")
def advance_search(search: AdvanceSearchRequest):
    try:
        start, end = search.window()
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=422)

    try:
        windows = list(month_windows(start, end))
        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
            chunks = list(pool.map(
                lambda w: fetch_filed_cases(search.commission_id, *w), windows))
    except Exception as exc:
        return error_response(exc)

    needle = search.search_value.upper()
    fields = PARTY_FIELDS[search.search_by]
    type_filter = (search.case_type or "").upper() or None

    results, seen = [], set()
    for row in (r for chunk in chunks for r in chunk):
        case_number = row.get("caseNumber")
        if not case_number or case_number in seen:
            continue
        if not any(needle in (row.get(f) or "").upper() for f in fields):
            continue
        item = search_row(row)
        if type_filter and item["case_type_code"] != type_filter:
            continue
        seen.add(case_number)
        results.append(item)

    return JSONResponse(content={"data": results, "scanned": sum(len(c) for c in chunks)})


class OrderSearchRequest(SearchWindow):
    search_by: Literal[
        "case_number", "complainant", "respondent",
        "complainant_advocate", "respondent_advocate", "free_text",
    ]
    search_value: str
    order_type: Literal[1, 2] = 2
    date_type: Literal[1, 2] = 2
    page: int = 0
    size: int = 30

    @field_validator("search_value")
    @classmethod
    def validate_search_value(cls, value):
        value = clean(value) or ""
        if len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value


@app.post("/cc/bulk_q/orders")
def order_search(search: OrderSearchRequest):
    try:
        search.window()
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=422)

    session = requests.Session()
    try:
        body = ejagriti_request(
            session, "POST", f"{CASE_SERVICE}/getCaseDetailsBySearchType",
            json={
                "commissionId": int(search.commission_id),
                "page": search.page,
                "size": min(max(search.size, 1), 100),
                "fromDate": search.from_date,
                "toDate": search.to_date,
                "dateRequestType": search.date_type,
                "serchType": ORDER_SEARCH_TYPES[search.search_by],
                "serchTypeValue": search.search_value,
                "judgeId": "",
                "orderType": search.order_type,
            },
        )
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()

    results = []
    for row in body.get("data") or []:
        item = search_row(row)
        has_document = bool(row.get("judgmentOrderDocumentBase64") or row.get("documentBase64"))
        item.update({
            "order_type": search.order_type,
            "order_date": iso_to_ddmmyyyy(
                row.get("judgemtmentDate") or row.get("orderDate") or row.get("dateOfHearing")),
            "hearing_date": clean(row.get("dateOfHearing")),
            "disposal_date": iso_to_ddmmyyyy(row.get("dateOfDisposal")),
            "additional_complainants": [
                clean(a.get("additional_complainant_name"))
                for a in row.get("additionalComplainantList") or []
            ],
            "additional_respondents": [
                clean(a.get("additional_respondent_name"))
                for a in row.get("additionalRespondantList") or []
            ],
            "has_document": has_document,
        })
        results.append(item)

    return JSONResponse(content={"data": results, "page": search.page})


class OrderDocumentRequest(BaseModel):
    case_no: str
    filing_reference_number: str
    hearing_date: str
    order_type: Literal[1, 2] = 1


@app.post("/cc/order")
def order_document(request: OrderDocumentRequest):
    case_no = normalize_case_number(request.case_no)
    key_base = order_key_base(case_no, request.hearing_date, request.order_type)
    try:
        for ext in (".pdf", ".html"):
            if s3_exists(key_base + ext):
                return JSONResponse(content={"order_link": s3_url(key_base + ext)})
        document = fetch_order_document(
            request.filing_reference_number, request.hearing_date, request.order_type)
        link = upload_document(document, key_base)
        if not link:
            return JSONResponse(content={"error": "Order not available"}, status_code=404)
        return JSONResponse(content={"order_link": link})
    except Exception as exc:
        return error_response(exc)


class CauseListRequest(BaseModel):
    commission_id: str
    court_id: Optional[str] = None
    hearing_date: str

    @field_validator("hearing_date")
    @classmethod
    def validate_date(cls, value):
        parse_iso_date(value)
        return value


def fetch_courts(session, commission_id):
    body = ejagriti_request(
        session, "POST", f"{REPORT_SERVICE}/court-id-by-commission-id",
        json={"commissionId": int(commission_id)},
    )
    return [
        {"court_id": str(c.get("courtId")), "court_name": clean(c.get("courtNameEn"))}
        for c in body.get("data") or []
    ]


def load_cause_list(commission_id, court_id, hearing_date):
    key = f"case_data/causelists/consumer/{commission_id}/{hearing_date}-court-{court_id}.pdf"
    if parse_iso_date(hearing_date) < date.today() and s3_exists(key):
        return s3_url(key)

    session = requests.Session()
    try:
        body = ejagriti_request(
            session, "GET", CAUSELIST_URL, origin=False,
            params={
                "dateOfNextHearing": hearing_date,
                "courtId": court_id,
                "commissionId": commission_id,
            },
        )
    except EJagritiError as exc:
        if exc.status_code == 404:
            return None
        raise
    finally:
        session.close()

    document = body.get("data")
    if not document:
        return None
    s3_client.put_object(
        Bucket=BUCKET_NAME, Key=key, Body=base64.b64decode(document),
        ContentType="application/pdf", ContentDisposition="inline",
    )
    return s3_url(key)


@app.post("/cc/causelist")
def cause_list(request: CauseListRequest):
    session = requests.Session()
    try:
        courts = fetch_courts(session, request.commission_id)
        if request.court_id:
            courts = [c for c in courts if c["court_id"] == str(request.court_id)] or [
                {"court_id": str(request.court_id), "court_name": str(request.court_id)}]

        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
            links = list(pool.map(
                lambda c: load_cause_list(request.commission_id, c["court_id"], request.hearing_date),
                courts))

        lists = [
            {**court, "causelist_link": link}
            for court, link in zip(courts, links) if link
        ]
        return JSONResponse(content={"data": lists})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


@app.get("/cc/courts")
def commission_courts(commission_id: str):
    session = requests.Session()
    try:
        return JSONResponse(content={"data": fetch_courts(session, commission_id)})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


@app.get("/cc/display_board")
def display_board(commission_id: str):
    session = requests.Session()
    try:
        body = ejagriti_request(
            session, "GET", f"{COURTROOM_SERVICE}/getDisplayBoardDetails",
            params={"commissionId": commission_id},
        )
        rows = body.get("data") or []
        rows = [r for item in rows for r in (item if isinstance(item, list) else [item])]
        board = [
            {
                "bench": clean(r.get("benchnumber")),
                "item_number": r.get("itemnumber") or None,
                "case_no": clean(r.get("caseNumber")),
                "cause_title": clean(r.get("causetitle")),
                "sitting": bool(clean(r.get("caseNumber"))),
            }
            for r in rows
        ]
        board.sort(key=lambda b: (not b["sitting"], int(b["bench"]) if (b["bench"] or "").isdigit() else 999))
        return JSONResponse(content={"data": board})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()
