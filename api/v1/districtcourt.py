import base64
import json
import random
import threading
import time
import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
import re
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv
from core.database import collection, save_case
from core.s3_client import s3_client
from core.lambda_client import lambda_client
from helpers.solve_captcha import solve_captcha
from helpers.requests import safe_get
from helpers.orders import (
    cached_case_needs_orders,
    order_pdf_s3_key,
    orders_stamp,
    stable_order_doc_id,
)
from helpers.ecourts_session import (
    BASE_URL,
    EcourtsBlockedError,
    EcourtsGateError,
    breaker,
    gate_rejected,
    invalidate_gate,
    looks_blocked,
    resolve_gate,
)
import os
from http.client import RemoteDisconnected
load_dotenv()
REGION_NAME = os.getenv("REGION_NAME")
app = APIRouter()
MAX_RETRIES = 5
CAPTCHA_URL = BASE_URL + "vendor/securimage/securimage_show.php"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

APP_TOKEN_RE = re.compile(r'name="app_token"[^>]*value="([^"]+)"')

ECOURTS_PROXY = os.getenv("ECOURTS_PROXY")
POOL_SIZE = int(os.getenv("ECOURTS_POOL_SIZE", "4"))
BLOCK_RETRIES = 4

SESSION_MAX_AGE_SECONDS = int(os.getenv("ECOURTS_SESSION_MAX_AGE", "600"))
SESSION_MAX_USES = int(os.getenv("ECOURTS_SESSION_MAX_USES", "25"))

_pool = []
_pool_lock = threading.Lock()
_ecourts_gate_slot = threading.Semaphore(POOL_SIZE)


def ecourts_gate_headers(session, force_refresh=False):
    """Validated gate headers. The delimeter is global and probe-checked once
    per process, not scraped blind per session."""
    return resolve_gate(session, getattr(session, "_app_token", ""), force_refresh)


def remember_app_token(session, token):
    """eCourts rotates app_token on *every* response. Losing a rotation
    poisons the session for its next request, so persist each one."""
    if token:
        session._app_token = token
    return getattr(session, "_app_token", "")


def session_expired(session):
    if not getattr(session, "_gate_ready", False):
        return True
    age = time.monotonic() - getattr(session, "_created_at", 0)
    if age > SESSION_MAX_AGE_SECONDS:
        return True
    return getattr(session, "_uses", 0) >= SESSION_MAX_USES


def new_ecourts_session():
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    if ECOURTS_PROXY:
        session.proxies.update({"http": ECOURTS_PROXY, "https": ECOURTS_PROXY})

    session._created_at = time.monotonic()
    session._uses = 0
    session._gate_ready = False
    session._search_validated = False

    response = safe_get(session, BASE_URL + "?p=casestatus/index")
    if looks_blocked(response):
        session.close()
        breaker.record_block()
        raise EcourtsBlockedError(
            "eCourts is refusing requests from this IP (HTTP 405 throttle "
            "stub). Retry later, or route through another IP via ECOURTS_PROXY."
        )

    match = APP_TOKEN_RE.search(response.text)
    remember_app_token(session, match.group(1) if match else "")
    ecourts_gate_headers(session)
    session._gate_ready = True
    breaker.record_success()
    return session


def rewarm_session(session):
    response = safe_get(session, BASE_URL + "?p=casestatus/index")
    if looks_blocked(response):
        breaker.record_block()
        raise EcourtsBlockedError(
            "eCourts is refusing requests from this IP (HTTP 405 throttle "
            "stub). Retry later, or route through another IP via ECOURTS_PROXY."
        )
    match = APP_TOKEN_RE.search(response.text)
    remember_app_token(session, match.group(1) if match else "")
    invalidate_gate()
    ecourts_gate_headers(session, force_refresh=True)
    session._search_validated = False
    return session._app_token


def _acquire_session():
    with _pool_lock:
        while _pool:
            session = _pool.pop()
            if not session_expired(session):
                session._uses = getattr(session, "_uses", 0) + 1
                return session
            session.close()

    last_error = None
    for attempt in range(1, BLOCK_RETRIES + 1):
        try:
            session = new_ecourts_session()
            session._uses = 1
            return session
        except EcourtsBlockedError as exc:
            last_error = exc
            breaker.check()
            print(f"[warn] eCourts throttling this IP (attempt {attempt})")
            time.sleep(min(2 ** attempt, 20))
    raise last_error


def _release_session(session, discard=False):
    if discard or session_expired(session):
        session.close()
        return
    with _pool_lock:
        if len(_pool) < POOL_SIZE:
            _pool.append(session)
            return
    session.close()


def acquire_ecourts_session():
    breaker.check()
    _ecourts_gate_slot.acquire()
    try:
        return _acquire_session()
    except BaseException:
        _ecourts_gate_slot.release()
        raise


def release_ecourts_session(session, discard=False):
    try:
        _release_session(session, discard)
    finally:
        _ecourts_gate_slot.release()

class CaseRequest(BaseModel):
    case_type: str
    case_reg_no: str
    rgyear: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None
    courtType: Optional[str] = None
    refresh : int=0

class CaseRequestBulk(BaseModel):
    petres_name: str
    rgyearP: str
    case_status: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None
    courtType: Optional[str] = None

class CaseRequestBulkIngest(BaseModel):
    court_code: str
    state_code: str
    dist_code: str
    court_complex_code: str
    case_no: str
    cino: str
    est_code: Optional[str] = None
    rgyear: str
    courtType: Optional[str] = None
    refresh: int = 0


def build_case_base_path(metadata: dict):
    return (
        f"cases/"
        f"{metadata['courtType']}/"
        f"{metadata['state_code']}/"
        f"{metadata['dist_code']}/"
        f"{metadata['court_complex_code']}/"
        f"{metadata['rgyear']}/"
        f"{metadata['cino']}/"
    )

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

def safe_post(session, url, data, headers=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(1.2, 2.0))

            merged_headers = {"Connection": "close"}
            merged_headers.update(ecourts_gate_headers(session))
            if headers:
                merged_headers.update(headers)

            if isinstance(data, dict) and "app_token" in data:
                data["app_token"] = getattr(session, "_app_token", "") or data["app_token"]

            response = session.post(
                url,
                data=data,
                timeout=(10, 120),
                headers=merged_headers
            )

            if looks_blocked(response):
                breaker.record_block()
                raise EcourtsBlockedError(
                    "eCourts is refusing requests from this IP (HTTP 405 "
                    "throttle stub). Retry later, or route through another IP "
                    "via ECOURTS_PROXY."
                )

            try:
                remember_app_token(session, response.json().get("app_token"))
            except Exception:
                pass

            if gate_rejected(response) and attempt < max_retries - 1:
                print(f"[warn] Gate rejected, re-warming (attempt {attempt+1})")
                token = rewarm_session(session)
                if isinstance(data, dict) and "app_token" in data:
                    data["app_token"] = token
                continue

            breaker.record_success()
            return response

        except (requests.exceptions.ConnectionError, RemoteDisconnected) as e:
            print(f"[warn] Server disconnected (attempt {attempt+1})")
            session.close()

        except requests.exceptions.Timeout:
            print(f"[warn] Timeout (attempt {attempt+1})")

    raise Exception("[error] eCourts request failed after retries")


def sanitize_keys(data):
    clean_data = {}
    for key, value in data.items():
        clean_key = key.replace('.', '').replace('$', '')
        if isinstance(value, dict):
            clean_data[clean_key] = sanitize_keys(value)
        else:
            clean_data[clean_key] = value
    return clean_data



def extract_table_data(soup, table_class):
    tables = soup.find_all("table", {"class": table_class})
    data = {}

    for table in tables:
        rows = table.find_all("tr")

        for row in rows:
            cells = row.find_all(["th", "td"])

            if not cells:
                continue

            headers = row.find_all("th")
            values = row.find_all("td")

            if headers and values:
                for h, v in zip(headers, values):
                    key = h.get_text(strip=True).replace(":", "")
                    key = "".join(key.split())
                    value = v.get_text(" ", strip=True)

                    if "CNR" in key:
                        span = v.find("span")
                        if span:
                            value = span.get_text(strip=True)

                    if key:
                        data[key] = value
                        
            elif len(cells) >= 2:
                key = cells[0].get_text(" ", strip=True).replace(":", "")
                key = "".join(key.split())

                value = cells[1].get_text(" ", strip=True)

                if key:
                    data[key] = value

    return sanitize_keys(data)

def extract_list_data(soup, table_class):
    ul = soup.find("ul", {"class": table_class})
    values = []

    if ul:
        items = ul.find_all("li")

        for item in items:
            text = " ".join(item.stripped_strings)
            values.append(text)

    return values

def extract_fir_details(soup, table_class):
    table = soup.find(
        "table", class_=lambda x: x and table_class in x)
    details = {}
    if table:
        rows = table.find_all("tr")
        for row in rows:
            cols = row.find_all("td")
            if len(cols) == 2:
                key = cols[0].get_text(
                    strip=True).replace(" ", "")
                value = cols[1].get_text(strip=True)
                details[key] = value
    return details


def extract_case_history(soup):
    table = soup.find("table", {"class": "history_table"})
    rows = table.find_all("tr") if table else []
    history = []

    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 4:
            history.append({
                "judge": cols[0].text.strip(),
                "businessOnDate": cols[1].find("a").text.strip() if cols[1].find("a") else cols[2].text.strip(),
                "hearingDate": cols[2].text.strip(),
                "purpose": cols[3].text.strip(),
                "inputType": "automatic",
                "lawyerRemark": "null"
            })

    return history or []


def extract_case_transfer(soup):
    table = soup.find(
        "table", {"class": "transfer_table table"})
    transfers = []

    if table:
        rows = table.find_all("tr")[1:]
        for row in rows:
            cols = row.find_all("td")
            if len(cols) >= 4:
                transfers.append({
                    "registrationNumber": cols[0].text.strip(),
                    "transferDate": cols[1].text.strip(),
                    "fromCourt": cols[2].text.strip(),
                    "toCourt": cols[3].text.strip(),
                    "inputType": "automatic",
                    "lawyerRemark": None
                })

    return transfers


def extract_acts_and_sections(
    soup,
    table_class="table acts_table table-bordered"
):
    acts_and_sections = {
        "actsandSection": {
            "acts": "null",
            "section": "null"
        }
    }

    act_table = soup.find("table", {"class": table_class})
    if not act_table:
        return acts_and_sections

    rows = act_table.find_all("tr")[1:]

    for row in rows:
        cells = row.find_all("td")
        if len(cells) == 2:
            acts_and_sections["actsandSection"] = {
                "acts": cells[0].get_text(strip=True),
                "section": cells[1].get_text(strip=True)
            }

    return acts_and_sections


def order_pdf_headers(session):
    """Gate headers minus the AJAX-only bits - this GET is a document fetch."""
    headers = dict(ecourts_gate_headers(session))
    headers.pop("X-Requested-With", None)
    headers["Accept"] = "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"
    headers["Referer"] = BASE_URL + "?p=home/viewHistory"
    return headers


def download_order_pdf(session, pdf_url):
    """Fetch the temporary PDF URL and return its bytes, or None.

    The old code streamed `response.raw` straight into S3, which skips content
    decoding and never checks that what came back is a PDF at all - an HTML
    "Invalid Request" page would land in the bucket as application/pdf.
    """
    try:
        response = session.get(
            pdf_url,
            headers=order_pdf_headers(session),
            timeout=(30, 180),
        )
    except Exception as exc:
        print(f"[dc/orders] PDF fetch failed for {pdf_url}: {exc}")
        return None

    if response.status_code != 200:
        print(f"[dc/orders] PDF {pdf_url} returned HTTP {response.status_code}")
        return None

    body = response.content
    start = body.find(b"%PDF", 0, 1024)
    if start < 0:
        print(
            f"[dc/orders] {pdf_url} did not return a PDF "
            f"({len(body)} bytes, starts {body[:40]!r})"
        )
        return None

    return body[start:]


def order_section_type(table):
    """Which order table this is, from the heading eCourts prints above it.

    Interim orders and the final judgement land in separate tables that are
    otherwise identical, so the heading is the only thing telling them apart.
    """
    heading = table.find_previous(["h2", "h3", "h4"])
    text = heading.get_text(" ", strip=True).lower() if heading else ""

    if "final" in text or "judgement" in text or "judgment" in text:
        return "final"
    if "interim" in text:
        return "interim"
    return "order"


def fetch_and_store_orders(
    soup,
    session,
    metadata,
    case_details,
    s3_client,
    bucket_name,
    region_name,
    table_class="order_table",
    pdf_endpoint="https://services.ecourts.gov.in/ecourtindia_v6/?p=home/display_pdf",
    pdf_base_url="https://services.ecourts.gov.in/ecourtindia_v6/"
):
    orders_prefix = build_case_base_path(metadata) + "orders/"
    orders = []
    seen_doc_ids = set()

    # eCourts splits orders across two tables - "Interim Orders" and "Final
    # Orders / Judgements". Reading only the first silently drops every final
    # judgement, which is usually the order that actually matters.
    order_tables = soup.find_all("table", {"class": table_class})

    if not order_tables:
        print(f"[dc/orders] no '{table_class}' table in markup, 0 orders")
        return orders

    rows = [
        (order_section_type(table), row)
        for table in order_tables
        for row in table.find_all("tr")
    ]
    remember_app_token(session, case_details.get("app_token", ""))

    for order_type, row in rows:
        if row.find("th"):
            continue

        cols = row.find_all("td")
        if len(cols) < 3:
            continue

        order_number = cols[0].text.strip()
        order_date = cols[1].text.strip()

        anchor = next(
            (a for a in row.find_all("a")
             if "displayPdf" in a.get("onclick", "")),
            None,
        )

        # A data row either links its PDF or carries a real date. A header row
        # rendered with <td> instead of <th> has neither.
        if not anchor and not re.search(r"\d", order_date):
            continue

        def keep(link, status, order_type=order_type):
            orders.append({
                "order_number": order_number,
                "order_date": order_date,
                "order_link": link,
                "order_status": status,
                "order_type": order_type,
            })

        if not anchor:
            keep(None, "not_uploaded")
            continue

        match = re.search(r"displayPdf\((.*?)\)", anchor.get("onclick", ""))

        if not match:
            keep(None, "not_uploaded")
            continue

        values = [v.strip().strip("'") for v in match.group(1).split(",")]
        if len(values) < 4:
            keep(None, "unavailable")
            continue

        doc_id = stable_order_doc_id(values[3])
        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)

        order_payload = {
            "normal_v": values[0],
            "case_val": values[1],
            "court_code": values[2],
            "filename": values[3],
            "appFlag": values[4] if len(values) > 4 else "",
            "ajax_req": "true",
            "app_token": getattr(session, "_app_token", "")
        }

        try:
            order_response = safe_post(session, pdf_endpoint, order_payload)
        except Exception as exc:
            print(f"[dc/orders] display_pdf request failed for {values[3]}: {exc}")
            keep(None, "unavailable")
            continue

        try:
            pdf_path = order_response.json().get("order", "").replace("\\", "")
        except Exception:
            print(
                f"[dc/orders] display_pdf non-JSON for {values[3]}: "
                f"HTTP {order_response.status_code} "
                f"{order_response.text[:160]!r}"
            )
            keep(None, "unavailable")
            continue

        if not pdf_path:
            keep(None, "not_uploaded")
            continue

        final_pdf_url = f"{pdf_base_url}{pdf_path}"
        s3_key = order_pdf_s3_key(orders_prefix, order_date, values[3])
        s3_url = f"https://{bucket_name}.s3.{region_name}.amazonaws.com/{s3_key}"

        try:
            try:
                s3_client.head_object(Bucket=bucket_name, Key=s3_key)
            except s3_client.exceptions.ClientError as e:
                if e.response["Error"]["Code"] != "404":
                    raise
                body = download_order_pdf(session, final_pdf_url)
                if body is None:
                    keep(None, "unavailable")
                    continue
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key=s3_key,
                    Body=body,
                    ContentType="application/pdf",
                    ContentDisposition="inline",
                )
                print(f"[dc/orders] stored {s3_key} ({len(body)} bytes)")
        except Exception as exc:
            print(f"[dc/orders] S3 store failed for {s3_key}: {exc}")
            keep(None, "unavailable")
            continue

        keep(s3_url, "available")

    print(
        f"[dc/orders] rows={len(rows)} kept={len(orders)} "
        f"withLink={sum(1 for o in orders if o['order_link'])}"
    )
    return orders


@app.post("/getcaseInfo")
def fetch_submit_info(case_data: CaseRequest):
    query = case_data.dict()
    ac_query = {
        "courtType": query.get("courtType"),
        "case_reg_no": query.get("case_reg_no"),
        "rgyear": query.get("rgyear"),
        "est_code": query.get("est_code"),
        "case_type": query.get("case_type"),
        "state_code": query.get("state_code"),
        "dist_code": query.get("dist_code"),
        "court_complex_code": query.get("court_complex_code")
    }
    existing_case = collection.find_one(ac_query)

    if (
        existing_case
        and case_data.refresh == 0
        and not cached_case_needs_orders(existing_case)
    ):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    existing_case_id = existing_case["_id"] if existing_case else None

    try:
        session = acquire_ecourts_session()
    except EcourtsBlockedError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=503)

    case_info = {}
    discard = False

    try:
        html_content, search_app_token = submit_search_with_captcha(
            session,
            BASE_URL + "?p=casestatus/submitCaseNo",
            {
                'case_type': case_data.case_type,
                'case_no': case_data.case_reg_no,
                'rgyear': case_data.rgyear,
                'state_code': case_data.state_code,
                'dist_code': case_data.dist_code,
                'court_complex_code': case_data.court_complex_code,
                'est_code': case_data.est_code if case_data.est_code else 'null',
                'search_case_no': case_data.case_reg_no,
            },
            "case_data",
            captcha_field="case_captcha_code",
        )

        if not html_content:
            return JSONResponse(
                content={"error": "Unable to get response from Ecourts at this moment"},
                status_code=404
            )

        if "Record not found" in html_content:
            return JSONResponse(content={"error": "Invalid case details"}, status_code=404)

        soup = BeautifulSoup(html_content, "html.parser")
        view_link = soup.find("a", class_="someclass")

        if view_link:
            onClick_data = view_link.get("onclick", "")
            match = re.search(r"viewHistory\((.*?)\)", onClick_data)

            if match:
                params = match.group(1)
                values = [v.strip().strip("'") for v in params.split(",")]

                case_info = {
                    "case_no": values[0],
                    "cino": values[1],
                    "court_code": values[2] or None,
                    "state_code": values[5] or None,
                    "dist_code": values[6] or None,
                    "court_complex_code": values[7] or None,
                    "est_code": case_data.est_code,
                    "case_type": case_data.case_type,
                    "rgyear": case_data.rgyear,
                    "case_reg_no": case_data.case_reg_no,
                    "courtType" : case_data.courtType,
                }

                second_payload = {
                    "app_token": search_app_token,
                    "court_code": case_info["court_code"],
                    "state_code": case_info["state_code"],
                    "dist_code": case_info["dist_code"],
                    "court_complex_code": case_info["court_complex_code"],
                    "case_no": case_info["case_no"],
                    "cino": case_info["cino"],
                    "est_code": case_info["est_code"],
                    "search_flag": "CScaseNumber",
                    "search_by": "CScaseNumber",
                    "ajax_req": "true",
                }

                second_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=home/viewHistory"

                second_response = safe_post(
                    session, second_url, second_payload)

                if second_response.status_code == 200:
                    case_details = second_response.json()
                    soup = BeautifulSoup(case_details.get(
                        "data_list", ""), "html.parser")

                    case_status = extract_table_data(
                        soup, "table case_status_table table-bordered")
                    case_details = extract_table_data(
                        soup, "table case_details_table table-bordered")
                    case_petitioner = {"petitioner_and_advocate": extract_list_data(
                        soup, "table table-bordered Petitioner_Advocate_table petitioner-advocate-list border")}
                    case_respondent = {"respondent_and_advocate": extract_list_data(
                        soup, "table table-bordered Respondent_Advocate_table respondent-advocate-list border")}
                    case_fir_details = {"fir_details": extract_fir_details(
                        soup, "FIR_details_table")}
                    acts_and_sections = extract_acts_and_sections(soup)
                    case_history = {"case_history": extract_case_history(soup)}
                    case_transfer = {
                        "case_transfer": extract_case_transfer(soup)}

                    metadata = {
                      **case_info, **case_fir_details, **case_details, **case_status, **case_petitioner,
                                      **case_respondent, **acts_and_sections, **case_history, **case_transfer  
                    }

                    case_json_s3_path = upload_case_json_to_s3(
                    s3_client,"dl-shared-gyl-vidilekh",metadata
                    )

                    orders = fetch_and_store_orders(
                        soup,
                        session,
                        metadata,
                        case_details,
                        s3_client,
                        "dl-shared-gyl-vidilekh",
                        REGION_NAME
                    )


                    final_response = {**case_info, **case_fir_details, **case_details, **case_status, **case_petitioner,
                                      **case_respondent, **acts_and_sections, **case_history, **case_transfer,"s3_prefix" : case_json_s3_path, "orders": orders, "orders_synced_at": orders_stamp()}

                    
                    final_response["_id"] = save_case(final_response, existing_case_id)

                    return JSONResponse(content=final_response, status_code=200)
                else:
                    return JSONResponse(content={"error": "Failed to fetch case details"}, status_code=403)

        return JSONResponse(content={"error": "Case details not found"}, status_code=403)

    except (EcourtsBlockedError, EcourtsGateError) as exc:
        discard = True
        return JSONResponse(content={"error": str(exc)}, status_code=503)

    finally:
        release_ecourts_session(session, discard)


def get_app_token(session, force_refresh=False):
    cached = getattr(session, "_app_token", "")
    if cached and not force_refresh:
        return cached

    response = safe_get(session, BASE_URL + "?p=casestatus/index")
    if looks_blocked(response):
        breaker.record_block()
        raise EcourtsBlockedError(
            "eCourts is refusing requests from this IP while refreshing the "
            "app_token. Retry later, or route through another IP via ECOURTS_PROXY."
        )
    match = APP_TOKEN_RE.search(response.text)
    if not match:
        match = re.search(r'app_token=([0-9a-f]{64})', response.text)
    return remember_app_token(session, match.group(1) if match else "")


def submit_search_with_captcha(session, url, payload, result_key,
                               captcha_field="fcaptcha_code"):
    app_token = get_app_token(session)

    for attempt in range(1, MAX_RETRIES + 1):
        captcha_response = safe_get(session, f"{CAPTCHA_URL}? {random.random()}")
        image_base64 = base64.b64encode(captcha_response.content).decode("utf-8")
        captcha_text = solve_captcha(
            lambda_client=lambda_client, image_base64=image_base64, frm="hc")
        if not captcha_text:
            print(f"[warn] Captcha solver returned nothing (attempt {attempt})")
            continue

        body = dict(payload)
        body[captcha_field] = str(captcha_text).strip()
        body["ajax_req"] = "true"
        body["app_token"] = app_token

        response = safe_post(session, url, body)

        try:
            response_json = response.json()
        except Exception:
            print(f"[warn] Non-JSON response (attempt {attempt})")
            continue

        app_token = remember_app_token(session, response_json.get("app_token") or "")

        error_msg = str(response_json.get("errormsg", "") or "").lower()

        if "invalid request" in error_msg:
            print(f"[warn] Gate rejected the request (attempt {attempt})")
            invalidate_gate()
            ecourts_gate_headers(session, force_refresh=True)
            app_token = get_app_token(session, force_refresh=True)
            continue

        if "captcha" in error_msg:
            print(f"[warn] Invalid captcha (attempt {attempt})")
            continue

        if "session" in error_msg or "expire" in error_msg:
            print(f"[warn] Session expired, refreshing token (attempt {attempt})")
            app_token = get_app_token(session, force_refresh=True)
            continue

        html = response_json.get(result_key, "")
        if not html:
            print(f"[warn] Empty {result_key} (attempt {attempt})")
            continue

        session._search_validated = True
        return html, app_token

    return None, app_token


@app.post("/dc/bulk_q/partyname")
def fetch_submit_info(case_data: CaseRequestBulk):
    case_info = {}

    try:
        session = acquire_ecourts_session()
    except EcourtsBlockedError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=503)

    discard = False

    try:
        html_content, _ = submit_search_with_captcha(
            session,
            BASE_URL + "?p=casestatus/submitPartyName",
            {
                'petres_name': case_data.petres_name,
                'rgyearP': case_data.rgyearP,
                'case_status': case_data.case_status,
                'state_code': case_data.state_code,
                'dist_code': case_data.dist_code,
                'court_complex_code': case_data.court_complex_code,
                'est_code': case_data.est_code if case_data.est_code else 'null',
            },
            "party_data",
        )

        if not html_content:
            return JSONResponse(
                content={"error": "Unable to get response from Ecourts at this moment"},
                status_code=404
            )

        if "Record not found" in html_content:
            return JSONResponse(content={"error": "Invalid case details"}, status_code=404)

        soup = BeautifulSoup(html_content, "html.parser")
        view_link = soup.find("a", class_="someclass")
        rows = soup.find_all("tr")

        results = []
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            case_number = cols[1].get_text(strip=True)
            party_details = cols[2].get_text(" ", strip=True)
            party_details = re.sub(
                r"\s*Vs\.?\s*", " Vs ", party_details, flags=re.IGNORECASE)
            party_details = re.sub(r"\s+", " ", party_details).strip()

            view_link = row.find("a", class_="someclass")
            if not view_link:
                continue

            onClick_data = view_link.get("onclick", "")
            match = re.search(r"viewHistory\((.*?)\)", onClick_data)

            if not match:
                continue

            params = match.group(1)
            values = [v.strip().strip("'") for v in params.split(",")]

            case_info = {
                "case_no": values[0],
                "cino": values[1],
                "court_code": values[2] or None,
                "state_code": values[5] or None,
                "dist_code": values[6] or None,
                "court_complex_code": values[7] or None,
                "est_code": case_data.est_code or None,
                "rgyear": case_data.rgyearP,
                "case_number": case_number,
                "party_details": party_details,
                "courtType": case_data.courtType
            }

            results.append(case_info)

        return JSONResponse(content={"data": results}, status_code=200)

    except (EcourtsBlockedError, EcourtsGateError) as exc:
        discard = True
        return JSONResponse(content={"error": str(exc)}, status_code=503)

    finally:
        release_ecourts_session(session, discard)

CNR_HISTORY_URL = BASE_URL + "?p=cnr_status/viewCNRHistory/"
HISTORY_URL = BASE_URL + "?p=home/viewHistory"


def fetch_history_by_search(session, case_info):
    """Pull case history through the search route.

    Only this route renders the `displayPdf(...)` order anchors that carry the
    filename token. The CNR route below returns the same case with the order
    cells stripped of their links - just the text and an empty <span> where the
    anchor would be - so orders scraped from it can never be downloaded. Try
    this first and keep the CNR route as the fallback.
    """
    payload = {
        "court_code": str(case_info.get("court_code") or ""),
        "state_code": str(case_info.get("state_code") or ""),
        "dist_code": str(case_info.get("dist_code") or ""),
        "court_complex_code": str(case_info.get("court_complex_code") or ""),
        "case_no": str(case_info.get("case_no") or ""),
        "cino": str(case_info.get("cino") or ""),
        "rgyear": str(case_info.get("rgyear") or ""),
        "search_flag": "CScaseNumber",
        "search_by": "CScaseNumber",
        "ajax_req": "true",
        "app_token": getattr(session, "_app_token", ""),
    }

    if case_info.get("est_code") is not None:
        payload["est_code"] = str(case_info["est_code"])

    response = safe_post(session, HISTORY_URL, payload)

    if response.status_code != 200:
        return "", f"viewHistory returned HTTP {response.status_code}"

    try:
        body = response.json()
    except Exception:
        return "", "viewHistory returned a non-JSON response"

    return body.get("data_list") or "", str(body.get("errormsg", "") or "")


def fetch_history_by_cnr(session, cino):
    """Pull case history straight from a CNR.

    `home/viewHistory` needs prior search state in the PHP session, so calling
    it on a freshly pooled session is a coin flip that mostly loses - that is
    what produced the "Invalid Request" wall. The CNR endpoint takes the cino
    on its own, needs no captcha, and returns the same markup under
    `casetype_list`.
    """
    payload = {
        "cino": str(cino),
        "ajax_req": "true",
        "app_token": getattr(session, "_app_token", ""),
    }

    response = safe_post(session, CNR_HISTORY_URL, payload)

    if response.status_code != 200:
        return "", f"CNR lookup returned HTTP {response.status_code}"

    try:
        body = response.json()
    except Exception:
        return "", "CNR lookup returned a non-JSON response"

    html = body.get("casetype_list") or body.get("data_list") or ""
    return html, str(body.get("errormsg", "") or "")


@app.post("/dc/bulk_i/partyname")
def fetch_submit_info(single_case: CaseRequestBulkIngest):
    try:
        session = acquire_ecourts_session()
    except EcourtsBlockedError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=503)

    discard = False

    try:
        query = single_case.dict()

        ac_query = {
            "courtType": "distcourts",
            "cino": query.get("cino")
        }

        existing_case = collection.find_one(ac_query)

        if (
            existing_case
            and single_case.refresh == 0
            and not cached_case_needs_orders(existing_case)
        ):
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=jsonable_encoder(existing_case))

        existing_case_id = existing_case["_id"] if existing_case else None

        case_info = {
            "case_no": single_case.case_no,
            "cino": single_case.cino,
            "court_code": single_case.court_code or None,
            "state_code": single_case.state_code,
            "dist_code": single_case.dist_code,
            "court_complex_code": single_case.court_complex_code,
            "est_code": single_case.court_code or None,
            "rgyear": single_case.rgyear,
            "courtType": "distcourts"
        }

        get_app_token(session)

        # Search route first - it is the only one whose markup carries the
        # order tokens. Fall back to the CNR route when the PHP session has no
        # search state, accepting that those orders arrive without links.
        data_list, errormsg = fetch_history_by_search(session, case_info)
        source = "viewHistory"

        if not data_list.strip():
            data_list, errormsg = fetch_history_by_cnr(session, case_info["cino"])
            source = "viewCNRHistory"

        print(
            f"[dc/bulk_i] cino={single_case.cino} source={source} "
            f"html_len={len(data_list)} order_tokens={data_list.count('displayPdf')} "
            f"errormsg={errormsg[:150]!r}"
        )

        if not data_list.strip():
            discard = True
            return JSONResponse(
                content={"error": errormsg or "eCourts returned no case history for this CNR"},
                status_code=502
            )

        soup = BeautifulSoup(data_list, "html.parser")

        case_status = extract_table_data(soup, "table case_status_table table-bordered")
        case_details = extract_table_data(soup, "table case_details_table table-bordered")
        case_petitioner = {"petitioner_and_advocate": extract_list_data(soup, "table table-bordered Petitioner_Advocate_table petitioner-advocate-list border")}
        case_respondent = {"respondent_and_advocate": extract_list_data(soup, "table table-bordered Respondent_Advocate_table respondent-advocate-list border")}
        case_fir_details = {"fir_details": extract_fir_details(soup, "FIR_details_table")}
        acts_and_sections = extract_acts_and_sections(soup)
        case_history = {"case_history": extract_case_history(soup)}
        case_transfer = {"case_transfer": extract_case_transfer(soup)}

        print(
            f"[dc/bulk_i] cino={single_case.cino} history_rows={len(case_history['case_history'])} "
            f"sample_dates={[h.get('hearingDate') for h in case_history['case_history'][:3]]}"
        )

        metadata = {
            **case_info,
            **case_fir_details,
            **case_details,
            **case_status,
            **case_petitioner,
            **case_respondent,
            **acts_and_sections,
            **case_history,
            **case_transfer
        }

        case_json_s3_path = upload_case_json_to_s3(
            s3_client,
            "dl-shared-gyl-vidilekh",
            metadata
        )

        orders = fetch_and_store_orders(
            soup,
            session,
            metadata,
            {"app_token": getattr(session, "_app_token", "")},
            s3_client,
            "dl-shared-gyl-vidilekh",
            REGION_NAME
        )

        final_response = {
            **metadata,
            "s3_prefix": case_json_s3_path,
            "orders": orders,
            "orders_synced_at": orders_stamp()
        }
    

        final_response["_id"] = save_case(final_response, existing_case_id)

        return JSONResponse(content=final_response, status_code=200)

    except (EcourtsBlockedError, EcourtsGateError) as exc:
        discard = True
        return JSONResponse(content={"error": str(exc)}, status_code=503)

    finally:
        release_ecourts_session(session, discard)