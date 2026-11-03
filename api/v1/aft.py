import base64
import html
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime
from typing import Dict, Literal, Optional

import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator

from core.database import collection, save_case
from core.lambda_client import lambda_client
from core.s3_client import s3_client
from helpers.causelist import mirror_cause_lists
from helpers.orders import cached_case_needs_orders, order_pdf_s3_key, orders_stamp
from helpers.solve_captcha import solve_captcha

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()

BASE_URL = "https://aft.gov.in"
CAPTCHA_URL = f"{BASE_URL}/admin/captcha"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
TIMEOUT = (
    float(os.getenv("AFT_CONNECT_TIMEOUT", "10")),
    float(os.getenv("AFT_READ_TIMEOUT", "90")),
)
MAX_RETRIES = 3
MAX_CAPTCHA_READS = int(os.getenv("AFT_MAX_CAPTCHA_READS", "6"))
MAX_PAGES = int(os.getenv("AFT_MAX_PAGES", "30"))
SEARCH_BUDGET = float(os.getenv("AFT_SEARCH_BUDGET", "60"))
# AFT's firewall answers 403 to any search containing SQL words such as "union" or "select"
BLOCKED_WORDS = ("The AFT website rejects searches containing words like 'union' or 'select'. "
                 "Search with another word, e.g. 'UOI' for Union of India.")
WORKERS = int(os.getenv("AFT_WORKERS", "6"))
BENCH = "1"
BENCH_NAME = "Armed Forces Tribunal - Principal Bench, New Delhi"
CASE_TYPES = {"1": "OA", "2": "TA", "3": "AT", "4": "CA", "5": "RA", "6": "MA", "7": "OA(A)"}
CASE_TYPE_CODES = {name: code for code, name in CASE_TYPES.items()}
# Cause lists dated before this day are published as one list per day, after it per court and bench
CAUSE_LIST_CUTOFF = "2026-09-08"

# search_by -> (path suffix, fields the AFT form expects)
STATUS_SEARCHES = {
    "party": ("partyname", ("party_name", "case_year")),
    "advocate": ("advocatename", ("advocate_name", "advocate_no", "case_year")),
    "freetext": ("freetext", ("search_by", "text_name")),
    "daterange": ("daterange", ("start_date", "end_date")),
    "coram": ("coram", ("coram_name",)),
}
JUDGEMENT_SEARCHES = {
    "daterange": ("daterange", ("start_date", "end_date")),
    "party": ("partyname", ("party_name", "case_year")),
    "advocate": ("advocatename", ("advocate_name", "advocate_no", "case_year")),
    "casenumber": ("casenumber", ("case_number",)),
    "casetype": ("casetype", ("case_type", "case_year")),
    "freetext": ("freetext", ("search_by", "text_name")),
    "respondent": ("respondent", ("respondent_name",)),
    "subject": ("subject", ("subject_name",)),
}
# Fields a search row may hand back to getcaseInfo so an import skips a second captcha search
PREFILL_FIELDS = ("case_no", "diary_no", "diary_year", "petitioner_name", "respondent_name", "advocate",
                  "respondent_advocate", "next_date", "court", "last_listed", "filing_date", "associated_with",
                  "subject", "presented_by")


class AFTError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def request_with_retry(session, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
        try:
            response = session.get(url, timeout=TIMEOUT, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"[warn] AFT GET {url} failed (attempt {attempt + 1}/{MAX_RETRIES}): {type(exc).__name__}")
            continue
        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise AFTError(f"AFT returned {response.status_code}", 403 if response.status_code == 403 else 502)
        return response
    raise AFTError(f"AFT did not respond after {MAX_RETRIES} attempts: {last_error!r}", 503)


def open_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def form_errors(page):
    """Validation messages AFT shows when it bounces a search back to the form."""
    messages = re.findall(r'class="[^"]*(?:text-danger|invalid-feedback|alert-danger)[^"]*"[^>]*>(.*?)</', page, re.S)
    messages += re.findall(r"toastr\.error\(['\"]([^'\"]+)", page)
    return [m for m in (clean(x) for x in messages) if m and m != "*"]


def captcha_search(session, path, params):
    """Run an AFT search and return every result page. All search forms share one session captcha,
    and the result pages are reached by repeating the search with the same captcha and a page number."""
    form_url = f"{BASE_URL}/{path}"
    request_with_retry(session, form_url)
    for _ in range(MAX_CAPTCHA_READS):
        image = request_with_retry(session, f"{CAPTCHA_URL}?{int(time.time() * 1000)}").content
        text = solve_captcha(lambda_client=lambda_client, image_base64=base64.b64encode(image).decode(), frm="hc")
        if not text:
            continue
        query = {**params, "captcha": text}
        try:
            response = request_with_retry(session, f"{form_url}/search", params=query, headers={"Referer": form_url})
        except AFTError as exc:
            if exc.status_code == 403:
                raise AFTError(BLOCKED_WORDS, 422)
            raise
        if "/search" not in response.url:
            # A rejected search redirects back to the empty form with the reason
            errors = form_errors(response.text)
            if errors and not any("captcha" in e.lower() for e in errors):
                raise AFTError(errors[0], 422)
            continue
        return fetch_pages(session, f"{form_url}/search", query, response.text)
    raise AFTError("Could not read the AFT captcha. Please try again.", 503)


def fetch_pages(session, url, query, first_page):
    """The remaining result pages, fetched in parallel within SEARCH_BUDGET seconds. Some AFT searches
    (coram) take over a minute per page, so pages that are not back in time are left out."""
    # pagination links are html-escaped: ...&amp;page=2
    last = min(max([int(n) for n in re.findall(r"[?&;]page=(\d+)", first_page)] or [1]), MAX_PAGES)
    if last == 1:
        return [first_page]
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = [pool.submit(request_with_retry, session, url, params={**query, "page": n}) for n in range(2, last + 1)]
    done, _ = wait(futures, timeout=SEARCH_BUDGET)
    pool.shutdown(wait=False, cancel_futures=True)
    pages = [first_page]
    for future in futures:
        if future in done and not future.exception():
            pages.append(future.result().text)
    return pages


def error_response(exc: Exception):
    if isinstance(exc, AFTError):
        return JSONResponse(content={"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(content={"error": str(exc)}, status_code=500)


def clean(value):
    if value is None:
        return None
    text = re.sub(r"<br\s*/?>", ", ", str(value), flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html.unescape(text)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip().strip(",:").strip()
    if not text or re.fullmatch(r"[-–—\s]+", text):
        return None
    return None if text.upper() in {"NULL", "NA", "N/A", "NONE", "NOT AVAILABLE"} else text


def to_ddmmyyyy(value):
    text = clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text.split(" 00:")[0], fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return None


def sort_key(ddmmyyyy):
    try:
        return datetime.strptime(ddmmyyyy, "%d-%m-%Y")
    except (TypeError, ValueError):
        return datetime.max


def parse_case_number(text):
    """'OA 123/2024', 'O.A. No. 123 of 2024' -> ('1', 'OA', '123', '2024')."""
    match = re.search(r"([A-Z][A-Z.()]*?)\s*(?:NO\.?)?\s*(\d+)\s*(?:/|OF)\s*(\d{4})", (clean(text) or "").upper())
    if not match:
        return None, None, None, None
    name = match.group(1).replace(".", "")
    return CASE_TYPE_CODES.get(name), name, match.group(2), match.group(3)


def split_diary(text):
    match = re.search(r"(\d+)\s*(?:/|of)\s*(\d{4})", clean(text) or "", re.I)
    return (match.group(1), match.group(2)) if match else (None, None)


def result_records(page):
    """Rows of the results table as {header: cell html}; '(no records)' rows are skipped."""
    start = page.find("<!-- Right Content -->")
    table = re.search(r"<table.*?</table>", page[start if start >= 0 else 0:], re.S)
    if not table:
        return []
    headers = [clean(h) or "" for h in re.findall(r"<th[^>]*>(.*?)</th>", table.group(0), re.S)]
    records = []
    for row in re.findall(r"<tr[^>]*>\s*<td.*?</tr>", table.group(0), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) == len(headers):
            records.append((dict(zip(headers, cells)), row))
    return records


def data_id(cell_html, marker=""):
    match = re.search(marker + r'[^>]*data-id="(\d+)"', cell_html or "") or re.search(r'data-id="(\d+)"', cell_html or "")
    return match.group(1) if match else None


def href(cell_html):
    match = re.search(r'href="(https?://[^"]+)"', cell_html or "")
    return html.unescape(match.group(1)).replace("http://", "https://", 1) if match else None


def finish_row(row):
    type_code, type_name, case_number, case_year = parse_case_number(row.get("case_no"))
    diary_no, diary_year = split_diary(row.pop("diary", None))
    row.update({
        "bench": BENCH,
        "case_type": type_code,
        "case_type_name": type_name,
        "case_number": case_number,
        "case_year": case_year,
        "diary_no": diary_no,
        "diary_year": diary_year,
        "diary": f"{diary_no}/{diary_year}" if diary_no else None,
    })
    row["ref"] = row["case_no"] or (f"D-{row['diary']}" if diary_no else None)
    return row


def case_status_row(record):
    """casestatus/* result: Reg No., Diary No., Next Date, Order, Court, Date Listing, Applicant, ..."""
    get = lambda key: clean(record.get(key))
    return finish_row({
        "source": "case",
        "case_no": get("Reg No."),
        "diary": get("Diary No."),
        "next_date": to_ddmmyyyy(get("Next Date")),
        "orders_id": data_id(record.get("Order")),
        "court": get("Court"),
        "last_listed": to_ddmmyyyy(get("Date Listing")),
        "petitioner_name": get("Applicant"),
        "respondent_name": get("Respondent"),
        "advocate": get("PAdvocate"),
        "respondent_advocate": get("RAdvocate"),
    })


def diary_status_row(record):
    """diarystatus/* result: Diary No, RegNo., Date of Presentation, Nature of Document, Associated With, ..."""
    get = lambda key: clean(record.get(key))
    return finish_row({
        "source": "diary",
        "case_no": get("RegNo."),
        "diary": get("Diary No"),
        "filing_date": to_ddmmyyyy(get("Date of Presentation")),
        "nature": get("Nature of Document"),
        "associated_with": get("Associated With"),
        "presented_by": get("Presented By"),
        "petitioner_name": get("Applicant"),
        "diary_id": data_id(record.get("Actions"), 'id="viewcase"'),
    })


def judgement_row(record):
    """judgements/* result: Details, RegNo., Judgement Pdf, Subject, Petitioner, Respondent, Padvocate, GNO, DOD, ..."""
    get = lambda key: clean(record.get(key))
    return finish_row({
        "source": "judgement",
        "case_no": get("RegNo."),
        "diary": get("GNO"),
        "subject": get("Subject"),
        "petitioner_name": get("Petitioner"),
        "respondent_name": get("Respondent"),
        "advocate": get("Padvocate"),
        "associated_with": get("Associated"),
        "court": get("Court No."),
        "date": to_ddmmyyyy(get("DOD")),
        "pdf_link": href(record.get("Judgement Pdf")),
        "details_url": href(record.get("Details")),
    })


def parse_rows(pages, parse):
    rows, seen = [], set()
    for page in pages:
        for record, _ in result_records(page):
            row = parse(record)
            key = row["ref"] or id(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def labelled_values(fragment):
    """'<p><strong>Label:</strong> value</p>' pairs from AFT's detail popups."""
    return {
        (clean(label) or "").lower(): clean(value)
        for label, value in re.findall(r"<(?:p|h5)[^>]*>\s*<strong>(.*?)</strong>(.*?)</(?:p|h5)>", fragment, re.S)
    }


def diary_details(session, diary_id):
    page = request_with_retry(session, f"{BASE_URL}/casedetails/viewcasedetails/{diary_id}").text
    values = labelled_values(page)
    return {
        "respondent_name": values.get("respondent name"),
        "presented_by": values.get("presented by"),
        "reviewed_by": values.get("reviewed by"),
        "associated_with": values.get("associated with"),
        "filing_date": to_ddmmyyyy(values.get("date of presentation")),
        "nature": values.get("case type"),
    }


def enrich_diary_rows(session, rows):
    """Diary search rows lack the respondent; each diary's detail popup has it."""
    def enrich(row):
        if row.get("diary_id"):
            try:
                details = diary_details(session, row["diary_id"])
            except AFTError:
                return
            for key, value in details.items():
                row[key] = row.get(key) or value
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(enrich, rows))
    return rows


def hearings_of(session, orders_id):
    """Every listing of the case: date, type, court, coram, status and the order PDF."""
    page = request_with_retry(session, f"{BASE_URL}/caseorders/orders-views/{orders_id}").text
    hearings = []
    for record, _ in result_records("<!-- Right Content -->" + page):
        hearings.append({
            "date": to_ddmmyyyy(record.get("Hearing Date")),
            "type": clean(record.get("Hearing Type")),
            "court": clean(record.get("Court")),
            "coram": clean(record.get("CORAM")),
            "status": clean(record.get("Status")),
            "remark": clean(record.get("Remark")),
            "pdf": href(record.get("Action")),
        })
    hearings.sort(key=lambda h: sort_key(h["date"]))
    return hearings


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def upload_order(link, key):
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return s3_url(key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            return None
    try:
        response = requests.get(link, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        print(f"[aft] order download failed for {key}: {exc}")
        return None
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=response.content,
                         ContentType="application/pdf", ContentDisposition="inline")
    return s3_url(key)


def build_orders(hearings, case_key, disposed):
    prefix = f"case_data/orders/aft/{case_key}/"
    entries = [h for h in hearings if h["pdf"]]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        links = list(pool.map(lambda h: upload_order(h["pdf"], order_pdf_s3_key(prefix, h["date"], h["pdf"])), entries))
    return [
        {
            "order_number": str(i),
            "order_date": h["date"],
            "order_type": "Final Order" if disposed and i == len(entries) else "Daily Order",
            "order_link": link,
            "order_status": "available" if link else "unavailable",
        }
        for i, (h, link) in enumerate(zip(entries, links), start=1)
    ]


def split_names(value):
    names = []
    for part in re.split(r",(?![^()]*\))|\s+&\s+(?!Ors)|\s+and\s+", value or ""):
        name = clean(part)
        if name and name not in names:
            names.append(name)
    return names


def transform_case(row, hearings):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    held = [h for h in hearings
            if (h["status"] or "").lower().startswith("close") or (h["date"] and sort_key(h["date"]) < today)]
    upcoming = [h for h in hearings if h not in held and h["date"]]
    pending = any((h["status"] or "").lower().startswith("pend") for h in hearings)
    # The search row's Next Date can be a listing that already passed; a pending listing from today on wins
    next_hearing = upcoming[0]["date"] if upcoming else row.get("next_date")
    # AFT never labels a disposal; a registered case with hearings but nothing pending or listed ahead has been decided
    disposed = bool(row.get("case_no") and held and not pending and not next_hearing)
    latest = held[-1] if held else None

    case_history = []
    for current, nxt in zip(held, held[1:] + [None]):
        case_history.append({
            "judge": current["coram"],
            "businessOnDate": current["date"],
            "hearingDate": nxt["date"] if nxt else (None if disposed else next_hearing),
            "purpose": " - ".join(p for p in (current["type"], f"Court {current['court']}" if (current["court"] or "").isdigit()
                                               else current["court"], current["remark"]) if p) or "Hearing",
            "inputType": "automatic",
            "lawyerRemark": None,
        })

    type_code, type_name = row.get("case_type"), row.get("case_type_name")
    case_number, case_year = row.get("case_number"), row.get("case_year")
    diary_no, diary_year = row.get("diary_no"), row.get("diary_year")
    registration = row.get("case_no")
    court = (latest or {}).get("court") or row.get("court")
    court_label = f"Court No. {court}" if (court or "").isdigit() else court
    coram = next((h["coram"] for h in reversed(held) if h["coram"]), None)
    case_key = re.sub(r"[^\w-]+", "-", registration or f"D-{diary_no}-{diary_year}")
    subject = row.get("subject")

    return {
        "courtType": "aft",
        "case_no": case_number or diary_no,
        "cino": f"AFT-{type_name}-{case_number}-{case_year}" if registration and case_number else f"AFT-D-{diary_no}-{diary_year}",
        "court_complex_code": BENCH,
        "bench_name": BENCH_NAME,
        "case_reg_no": case_number,
        "rgyear": case_year,
        "case_type": type_code,
        "fir_details": {},
        "CaseType": type_name or row.get("nature"),
        "FilingNumber": f"{diary_no}/{diary_year}" if diary_no and diary_year else None,
        "DiaryNo": diary_no,
        "DiaryYear": diary_year,
        "RegistrationNumber": registration,
        "CNRNumber": registration or (f"{diary_no}/{diary_year}" if diary_no else None),
        "FilingDate": row.get("filing_date"),
        "RegistrationDate": None,
        "FirstHearingDate": held[0]["date"] if held else None,
        "LastHearingDate": latest["date"] if latest else row.get("last_listed"),
        "NextHearingDate": None if disposed else next_hearing,
        "DecisionDate": latest["date"] if disposed else None,
        "CaseStatus": "Disposed" if disposed else ("Pending" if registration else "Under Scrutiny"),
        "CaseStage": (latest or {}).get("type") or ("Registered" if registration else "Diary"),
        "NatureofDisposal": "Decided" if disposed else None,
        "CourtNumberandJudge": ", ".join(p for p in (court_label, coram) if p) or None,
        "AssociatedWith": row.get("associated_with"),
        "PresentedBy": row.get("presented_by"),
        "petitioner_and_advocate": split_names(row.get("petitioner_name")) or [""],
        "respondent_and_advocate": split_names(row.get("respondent_name")) or [""],
        "petitioner_advocates": split_names(row.get("advocate")),
        "respondent_advocates": split_names(row.get("respondent_advocate")),
        "subject": subject,
        "actsandSection": {"acts": "Armed Forces Tribunal Act, 2007", "section": subject},
        "case_history": case_history,
        "case_transfer": [],
        "orders": build_orders(hearings, case_key, disposed),
        "orders_synced_at": orders_stamp(),
    }


def case_type_code(value):
    value = (value or "").strip()
    if value in CASE_TYPES:
        return value
    return CASE_TYPE_CODES.get(value.upper().replace(".", ""))


def find_registered(session, type_code, case_number, case_year):
    pages = captcha_search(session, "casestatus/casenumber", {
        "case_type": type_code, "case_number": case_number, "case_year": case_year})
    rows = parse_rows(pages, case_status_row)
    return next((r for r in rows if r["case_number"] == str(int(case_number)) and r["case_year"] == case_year), None)


def find_by_diary(session, family, parse, diary_no, diary_year):
    rows = parse_rows(captcha_search(session, f"{family}/diarynumber", {
        "diary_no": diary_no, "case_year": diary_year}), parse)
    return next((r for r in rows if r["diary_no"] == str(int(diary_no)) and r["diary_year"] == diary_year), None)


def find_diary(session, diary_no, diary_year):
    """Diary status row plus its detail popup: presentation date, presented by, respondent."""
    row = find_by_diary(session, "diarystatus", diary_status_row, diary_no, diary_year)
    if row and row.get("diary_id"):
        for key, value in diary_details(session, row["diary_id"]).items():
            row[key] = row.get(key) or value
    return row


def merge(*rows):
    merged = {}
    for row in rows:
        for key, value in (row or {}).items():
            if value not in (None, "") or key not in merged:
                merged[key] = value
    return merged


class CaseRequest(BaseModel):
    bench: str = BENCH
    diary_no: Optional[str] = None
    diary_year: Optional[str] = None
    case_type: Optional[str] = None
    case_no: Optional[str] = None
    case_year: Optional[str] = None
    orders_id: Optional[str] = None
    prefill: Optional[Dict[str, Optional[str]]] = None
    refresh: int = 0

    @model_validator(mode="after")
    def check_identifiers(self):
        if self.orders_id and not self.orders_id.isdigit():
            raise ValueError("Invalid AFT case reference")
        self.prefill = {k: clean(v) for k, v in (self.prefill or {}).items() if k in PREFILL_FIELDS} or None
        if self.case_type and self.case_no and self.case_year:
            self.case_type = case_type_code(self.case_type)
            if not self.case_type:
                raise ValueError("Select a valid AFT case type")
            return self
        if self.diary_no and self.diary_year:
            return self
        raise ValueError("Enter the case type, number and year, or the diary number and year")


@app.post("/aft/getcaseInfo")
def fetch_aft_case(case_data: CaseRequest):
    by_case = bool(case_data.case_type and case_data.case_no)
    if by_case:
        ac_query = {"courtType": "aft", "case_type": case_data.case_type,
                    "case_reg_no": str(int(case_data.case_no)), "rgyear": case_data.case_year}
    else:
        ac_query = {"courtType": "aft", "FilingNumber": f"{int(case_data.diary_no)}/{case_data.diary_year}"}
    existing_case = collection.find_one(ac_query)
    if existing_case and case_data.refresh == 0 and not cached_case_needs_orders(existing_case):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    session = open_session()
    try:
        if by_case and case_data.orders_id and case_data.prefill:
            # Imported from a search result: the row already carries what the case status search would return
            row = finish_row({"source": "case", **case_data.prefill, "orders_id": case_data.orders_id,
                              "diary": f"{case_data.prefill.get('diary_no')}/{case_data.prefill.get('diary_year')}"
                              if case_data.prefill.get("diary_no") else None})
        elif by_case:
            row = find_registered(session, case_data.case_type, case_data.case_no, case_data.case_year)
            if not row and not case_data.diary_no:
                # Not in case status (an MA or CA): diary status can find it by its registration
                rows = parse_rows(captcha_search(session, "diarystatus/casenumber", {
                    "case_type": case_data.case_type, "case_number": case_data.case_no,
                    "case_year": case_data.case_year}), diary_status_row)
                filed = next((r for r in rows if r["case_number"] == str(int(case_data.case_no))
                              and r["case_year"] == case_data.case_year), None)
                if filed:
                    case_data.diary_no, case_data.diary_year = filed["diary_no"], filed["diary_year"]
        else:
            # Case status has the listings and orders; applications filed inside another case
            # (MA, CA...) are only in diary status
            row = find_by_diary(session, "casestatus", case_status_row, case_data.diary_no, case_data.diary_year)
        diary_no, diary_year = ((row["diary_no"], row["diary_year"]) if row and row.get("diary_no")
                                else (case_data.diary_no, case_data.diary_year))
        # An MA or CA is never in case status, so a case-number lookup falls back to its diary
        diary = find_diary(session, diary_no, diary_year) if diary_no and diary_year else None
        if not row and not diary:
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)
        # the case status row wins; the diary adds presentation details and the registration of MAs
        row = merge(diary, row)

        hearings = hearings_of(session, row["orders_id"]) if row.get("orders_id") else []
        result = transform_case(row, hearings)
        result["_id"] = save_case(result, existing_case["_id"] if existing_case else None)
        return JSONResponse(content=jsonable_encoder(result), status_code=200)
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


def check_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError("Enter dates as YYYY-MM-DD")


class SearchRequest(BaseModel):
    search_by: str
    scope: Literal["case", "diary"] = "case"
    name: Optional[str] = None
    advocate_no: Optional[str] = None
    year: Optional[str] = None
    match: Literal["1", "2"] = "1"
    case_type: Optional[str] = None
    case_number: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = clean(value)
        if value and len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value

    @model_validator(mode="after")
    def check_fields(self):
        if self.search_by == "daterange":
            start, end = check_date(self.start_date), check_date(self.end_date)
            if start > end:
                raise ValueError("The start date must be on or before the end date")
            self.start_date, self.end_date = start, end
        elif self.search_by == "advocate":
            if not (self.name or self.advocate_no):
                raise ValueError("Enter the advocate name or enrolment number")
        elif self.search_by == "casenumber":
            if not self.case_number:
                raise ValueError("Enter the case number")
        elif self.search_by == "casetype":
            self.case_type = case_type_code(self.case_type)
            if not (self.case_type and self.year):
                raise ValueError("Select the case type and year")
        elif not self.name:
            raise ValueError("Enter at least 3 characters to search")
        return self

    def params(self, fields):
        values = {
            "party_name": self.name, "advocate_name": self.name or "", "advocate_no": self.advocate_no or "",
            "case_year": self.year or "", "search_by": self.match, "text_name": self.name,
            "start_date": self.start_date, "end_date": self.end_date, "coram_name": self.name,
            "respondent_name": self.name, "subject_name": self.name, "case_type": self.case_type,
            "case_number": self.case_number,
        }
        return {field: values[field] for field in fields}


def run_search(request, searches, family, parse):
    if request.search_by not in searches:
        return JSONResponse(content={"error": f"Unsupported AFT search: {request.search_by}"}, status_code=422)
    path, fields = searches[request.search_by]
    session = open_session()
    try:
        rows = parse_rows(captcha_search(session, f"{family}/{path}", request.params(fields)), parse)
        if parse is diary_status_row:
            enrich_diary_rows(session, rows)
        return JSONResponse(content={"data": rows})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


@app.post("/aft/bulk_q/search")
def aft_search(request: SearchRequest):
    if request.scope == "diary":
        searches = {k: v for k, v in STATUS_SEARCHES.items() if k != "coram"}
        return run_search(request, searches, "diarystatus", diary_status_row)
    return run_search(request, STATUS_SEARCHES, "casestatus", case_status_row)


@app.post("/aft/judgements")
def aft_judgements(request: SearchRequest):
    return run_search(request, JUDGEMENT_SEARCHES, "judgements", judgement_row)


class CauseListRequest(BaseModel):
    date: str
    bench: str = BENCH

    @field_validator("date")
    @classmethod
    def validate_date(cls, value):
        return check_date(value)


@app.post("/aft/causelist")
def cause_list(request: CauseListRequest):
    session = open_session()
    try:
        pdf = f"{BASE_URL}/daily-cause-pdf/{request.date}"
        if request.date < CAUSE_LIST_CUTOFF:
            lists = [{"court_id": request.date, "court_name": "Cause List",
                      "description": f"Cause list for {request.date}", "causelist_link": f"{pdf}//"}]
        else:
            courts = request_with_retry(session, f"{BASE_URL}/get-courts/{request.date}").json().get("courts") or []
            lists = []
            for court in courts:
                name = court.get("court_name") or ""
                label = f"Court {name}" if name.isdigit() else name
                lists.append({"court_id": f"{court['id']}", "court_name": label,
                              "description": f"{label} — {court.get('timing') or ''}".strip(" —"),
                              "causelist_link": f"{pdf}/{court['id']}/"})
                benches = request_with_retry(session, f"{BASE_URL}/get-benches",
                                             params={"court_id": court["id"], "date": request.date}).json()
                for bench in benches if isinstance(benches, list) else []:
                    lists.append({"court_id": f"{court['id']}-{bench['id']}",
                                  "court_name": f"{label} · {clean(bench.get('name')) or 'Bench'}",
                                  "description": f"{label}, {clean(bench.get('name')) or 'bench list'}",
                                  "causelist_link": f"{pdf}/{court['id']}/{bench['id']}"})
        return JSONResponse(content={"data": mirror_cause_lists(lists, f"aft/{request.date}")})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()
