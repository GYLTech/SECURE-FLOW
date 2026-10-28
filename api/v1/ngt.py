import base64
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
from core.lambda_client import lambda_client
from core.s3_client import s3_client
from helpers.causelist import mirror_cause_lists
from helpers.orders import cached_case_needs_orders, order_pdf_s3_key, orders_stamp
from helpers.solve_captcha import solve_captcha

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()

BASE_URL = "https://www.greentribunal.gov.in"
CAPTCHA_URL = f"{BASE_URL}/sites/all/modules/custom/case_status/captcha.php"
ORDER_URL = f"{BASE_URL}/gen_pdf_test.php"
BOARD_URL = "https://ngtonline.nic.in/dashboard/showCaseList"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
TIMEOUT = (
    float(os.getenv("NGT_CONNECT_TIMEOUT", "10")),
    float(os.getenv("NGT_READ_TIMEOUT", "90")),
)
MAX_RETRIES = 3
MAX_CAPTCHA_READS = int(os.getenv("NGT_MAX_CAPTCHA_READS", "6"))
ORDER_WORKERS = int(os.getenv("NGT_ORDER_WORKERS", "4"))
CAPTCHA_REJECTED = "Captcha is incorrect"

BENCHES = {
    "1": "National Green Tribunal - Principal Bench, New Delhi",
    "2": "National Green Tribunal - Eastern Zone Bench, Kolkata",
    "3": "National Green Tribunal - Central Zone Bench, Bhopal",
    "4": "National Green Tribunal - Western Zone Bench, Pune",
    "5": "National Green Tribunal - Southern Zone Bench, Chennai",
}
CAUSE_LIST_PAGES = {
    "1": [("Chairperson Bench", "principal-chairperson-bench"), ("Court No-II", "principal-court-II-cause-list"),
          ("Court No-III", "principal-court-III-cause-list"), ("RG Court", "principal-rg-court-cause-list")],
    "2": [("Eastern Zone", "eastern-zone-cause-list")],
    "3": [("Central Zone", "central-zone-cause-list")],
    "4": [("Western Zone", "western-zone-cause-list")],
    "5": [("Southern Zone", "southern-zone-cause-list")],
}
DISPOSED_WORDS = ("dispos", "dismiss", "withdrawn", "allowed", "closed", "abated", "rejected")


class NGTError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def get_with_retry(session, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
        try:
            response = session.get(url, timeout=TIMEOUT, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"[warn] NGT GET {url} failed (attempt {attempt + 1}/{MAX_RETRIES}): {type(exc).__name__}")
            continue
        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise NGTError(f"NGT returned {response.status_code}")
        return response
    raise NGTError(f"NGT did not respond after {MAX_RETRIES} attempts: {last_error!r}", 503)


def open_session(form_page="casenumber"):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    get_with_retry(session, f"{BASE_URL}/casestatus/{form_page}")
    return session


def captcha_search(session, endpoint, params):
    for _ in range(MAX_CAPTCHA_READS):
        image = get_with_retry(session, f"{CAPTCHA_URL}?{random.random()}").content
        text = solve_captcha(lambda_client=lambda_client, image_base64=base64.b64encode(image).decode(), frm="hc")
        if not text:
            continue
        response = get_with_retry(session, f"{BASE_URL}/casestatus/{endpoint}", params={**params, "captcha_input": text})
        if CAPTCHA_REJECTED not in response.text:
            return response.text
    raise NGTError("Could not read the NGT captcha. Please try again.", 503)


def error_response(exc: Exception):
    if isinstance(exc, NGTError):
        return JSONResponse(content={"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(content={"error": str(exc)}, status_code=500)


def clean(value):
    if value is None:
        return None
    text = re.sub(r"<br\s*/?>", ", ", str(value), flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html.unescape(text))
    text = re.sub(r"\s+", " ", text).strip().strip(",").strip()
    text = re.sub(r"\s*,\s*,", ",", text)
    return text if text and text.upper() not in {"NULL", "NA", "N/A", "NONE", "-"} else None


def to_ddmmyyyy(value):
    text = clean(value)
    if not text:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
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


def table_rows(table_html):
    rows = []
    for row in re.findall(r"<tr.*?</tr>", table_html, re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        rows.append((row, cells))
    return rows


def details_path_of(link):
    match = re.search(r"/caseDetails/[^\"'\s<>]+", link or "")
    return match.group(0) if match else None


def parse_results(page):
    table = re.search(r'<table class="table-bordered customtable".*?</table>', page, re.S)
    table_html = table.group(0) if table else ""
    headers = [(clean(h) or "").lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.S)]

    def column(cells, name, fallback):
        index = next((i for i, h in enumerate(headers) if name in h), fallback)
        return cells[index] if index is not None and index < len(cells) else None

    results, seen = [], set()
    for row, cells in table_rows(table_html):
        if len(cells) < 7:
            continue
        path = details_path_of(row)
        if not path or path in seen:
            continue
        seen.add(path)
        case_cell = clean(column(cells, "case number", 3)) or ""
        parties = clean(column(cells, "party", 4)) or ""
        petitioner, _, respondent = parties.partition(" VS ")
        results.append({
            "details_path": path,
            "bench": clean(column(cells, "bench", 1)),
            "diary_no": clean(column(cells, "diary", 2)),
            "case_no": case_cell.split(" / ")[0] if case_cell else None,
            "petitioner_name": clean(petitioner),
            "respondent_name": clean(respondent),
            "filing_date": to_ddmmyyyy(column(cells, "filing date", None)),
            "registration_date": to_ddmmyyyy(column(cells, "registration", 5)),
            "status": clean(column(cells, "status", len(cells) - 1)),
        })
    return results


def section(page, title):
    start = page.find(title)
    if start < 0:
        return ""
    table = re.search(r"<table.*?</table>", page[start:], re.S)
    return table.group(0) if table else ""


def key_values(page):
    start = page.find('id="page-title"')
    table = re.search(r"<table.*?</table>", page[start:], re.S) if start >= 0 else None
    values = {}
    for _, cells in table_rows(table.group(0) if table else ""):
        texts = [clean(c) for c in cells]
        for label, value in zip(texts[0::2], texts[1::2]):
            if label:
                values[label.rstrip(":").strip()] = value
    return values


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def upload_order(filepath, key):
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return s3_url(key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            return None
    try:
        response = requests.get(ORDER_URL, params={"filepath": filepath},
                                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        print(f"[ngt] order download failed for {key}: {exc}")
        return None
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=response.content,
                         ContentType="application/pdf", ContentDisposition="inline")
    return s3_url(key)


def parse_listings(page):
    listings = []
    for row, cells in table_rows(section(page, "Listing History")):
        if len(cells) < 4 or row.lstrip().startswith("<tr>\n<th") or "<th" in row:
            continue
        listed = to_ddmmyyyy(cells[1])
        if not listed:
            continue
        encoded = re.search(r"myFunctionTest\('([^']+)'\)", row)
        is_judgement = "/judgements/" in row
        listings.append({
            "listed": listed,
            "uploaded": to_ddmmyyyy(cells[2]),
            "coram": clean(cells[3]) if len(cells) > 4 else None,
            "file": encoded.group(1) if encoded else None,
            "kind": "Judgement" if is_judgement else "Daily Order",
        })
    listings.sort(key=lambda l: sort_key(l["listed"]))
    return listings


def build_orders(details_path, listings):
    prefix = f"case_data/orders/ngt/{re.sub(r'[^0-9A-Za-z]+', '-', details_path).strip('-')}/"
    entries, seen = [], set()
    for listing in listings:
        if not listing["file"] or listing["file"] in seen:
            continue
        seen.add(listing["file"])
        entries.append((listing, order_pdf_s3_key(prefix, listing["listed"], listing["file"])))
    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as pool:
        links = list(pool.map(lambda e: upload_order(e[0]["file"], e[1]), entries))
    return [
        {
            "order_number": str(i),
            "order_date": listing["listed"],
            "order_type": listing["kind"],
            "order_link": link,
            "order_status": "available" if link else "unavailable",
        }
        for i, ((listing, _), link) in enumerate(zip(entries, links), start=1)
    ]


def split_case_number(case_number):
    match = re.match(r"(.+?)\s+No\.\s*(\d+)\s*/\s*(\d{4})", case_number or "")
    return (match.group(1).strip(), match.group(2), match.group(3)) if match else (None, None, None)


def transform_case(page, details_path, zone, orders, listings):
    values = key_values(page)
    case_number = values.get("Case Number")
    case_type, case_no, case_year = split_case_number(case_number)
    status = values.get("Case Status")
    disposed = any(word in (status or "").lower() for word in DISPOSED_WORDS)

    parties = table_rows(section(page, "All Parties"))
    petitioners, respondents = [], []
    for row, cells in parties:
        if "<th" in row or len(cells) < 2:
            continue
        petitioners += [p for p in re.split(r"<br\s*/?>|\n", cells[0]) if clean(p)]
        respondents += [r for r in re.split(r"<br\s*/?>|\n", cells[1]) if clean(r)]
    petitioners = [clean(p) for p in petitioners]
    respondents = [clean(r) for r in respondents]
    title = values.get("Party Name") or ""
    title_pet, _, title_res = title.partition(" VS ")

    case_history = []
    for current, nxt in zip(listings, listings[1:] + [None]):
        case_history.append({
            "judge": current["coram"],
            "businessOnDate": current["listed"],
            "hearingDate": nxt["listed"] if nxt else None,
            "purpose": current["kind"] if current["file"] else None,
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    next_hearing = to_ddmmyyyy(values.get("Next Date") or values.get("Next Hearing Date") or values.get("Next Listing Date"))
    if case_history and not disposed:
        case_history[-1]["hearingDate"] = next_hearing

    filing_no = values.get("Filing Number")
    ias = []
    for row, cells in table_rows(section(page, "IA/MA")):
        if "<th" in row or len(cells) < 4:
            continue
        ias.append({"filing_no": clean(cells[1]), "case_type": clean(cells[2]), "case_no": clean(cells[3])})
    connected = []
    for row, cells in table_rows(section(page, "Connected Matters")):
        if "<th" in row or len(cells) < 6:
            continue
        connected.append({"diary_no": clean(cells[1]), "case_no": clean(cells[2]), "status": clean(cells[5])})

    return {
        "courtType": "ngt",
        "case_no": re.sub(r"\D", "", filing_no or "") or details_path,
        "cino": f"NGT-{zone}-{case_number}" if case_number else details_path,
        "details_path": details_path,
        "court_complex_code": str(zone),
        "bench_name": BENCHES.get(str(zone)),
        "case_reg_no": case_no,
        "rgyear": case_year,
        "case_type": case_type,
        "fir_details": {},
        "CaseType": case_type,
        "FilingNumber": filing_no,
        "RegistrationNumber": case_number,
        "CNRNumber": case_number,
        "FilingDate": to_ddmmyyyy(values.get("Filing Date")),
        "RegistrationDate": to_ddmmyyyy(values.get("Registered On")),
        "FirstHearingDate": listings[0]["listed"] if listings else None,
        "NextHearingDate": None if disposed else next_hearing,
        "DecisionDate": to_ddmmyyyy(values.get("Last Listed")) if disposed else None,
        "CaseStatus": status,
        "CaseStage": status,
        "NatureofDisposal": status if disposed else None,
        "CourtNumberandJudge": listings[-1]["coram"] if listings else None,
        "petitioner_and_advocate": petitioners or [clean(title_pet) or ""],
        "respondent_and_advocate": respondents or [clean(title_res) or ""],
        "petitioner_advocates": [a for a in re.split(r",\s*", values.get("Petitioner Advocate(s)") or "") if a],
        "respondent_advocates": [a for a in re.split(r",\s*", values.get("Respondent Advocate(s)") or "") if a],
        "applications_filed": ias,
        "connected_cases": connected,
        "actsandSection": {"acts": values.get("Act") or "National Green Tribunal Act, 2010", "section": None},
        "case_history": case_history,
        "case_transfer": [],
        "orders": orders,
        "orders_synced_at": orders_stamp(),
    }


class CaseRequest(BaseModel):
    zone: Literal["1", "2", "3", "4", "5"]
    diary_no: Optional[str] = None
    diary_year: Optional[str] = None
    case_type: Optional[str] = None
    case_no: Optional[str] = None
    case_year: Optional[str] = None
    details_path: Optional[str] = None
    refresh: int = 0

    @model_validator(mode="after")
    def check_identifiers(self):
        if self.details_path:
            if not re.fullmatch(r"/caseDetails/[A-Za-z]+/\d+", self.details_path):
                raise ValueError("Invalid NGT case reference")
            return self
        if self.diary_no and self.diary_year:
            return self
        if self.case_type and self.case_no and self.case_year:
            return self
        raise ValueError("Enter the diary number and year, or the case type, number and year")


def find_case(session, case_data):
    if case_data.diary_no:
        page = captcha_search(session, "dairyData", {
            "zone_type": case_data.zone, "diary_no": case_data.diary_no, "case_year": case_data.diary_year})
    else:
        page = captcha_search(session, "caseNumberData", {
            "zone_type": case_data.zone, "case_type": case_data.case_type,
            "case_number": case_data.case_no, "case_year": case_data.case_year})
    results = parse_results(page)
    return results[0]["details_path"] if results else None


@app.post("/ngt/getcaseInfo")
def fetch_ngt_case(case_data: CaseRequest):
    if case_data.details_path:
        ac_query = {"courtType": "ngt", "details_path": case_data.details_path}
    elif case_data.diary_no:
        ac_query = {"courtType": "ngt", "court_complex_code": case_data.zone,
                    "FilingNumber": {"$regex": f"^0*{re.escape(case_data.diary_no)}/{case_data.diary_year}$"}}
    else:
        ac_query = {"courtType": "ngt", "court_complex_code": case_data.zone, "case_reg_no": case_data.case_no,
                    "rgyear": case_data.case_year}
    existing_case = collection.find_one(ac_query)
    if existing_case and case_data.refresh == 0 and not cached_case_needs_orders(existing_case):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    session = open_session()
    try:
        details_path = case_data.details_path
        if details_path:
            captcha_search(session, "caseNumberData", {
                "zone_type": case_data.zone, "case_type": "1", "case_number": "1", "case_year": str(datetime.now().year)})
        else:
            details_path = find_case(session, case_data)
        if not details_path:
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        page = get_with_retry(session, f"{BASE_URL}{details_path}").text
        if CAPTCHA_REJECTED in page or "Filing Number" not in page:
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        listings = parse_listings(page)
        orders = build_orders(details_path, listings)
        result = transform_case(page, details_path, case_data.zone, orders, listings)
        result["_id"] = save_case(result, existing_case["_id"] if existing_case else None)
        return JSONResponse(content=jsonable_encoder(result), status_code=200)
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


def search_year(value):
    value = (value or "").strip()
    if value and not re.fullmatch(r"\d{4}", value):
        raise ValueError("Enter a four digit year")
    return value


class SearchRequest(BaseModel):
    zone: Literal["1", "2", "3", "4", "5"]
    search_by: Literal["party", "advocate"]
    name: Optional[str] = None
    advocate_no: Optional[str] = None
    party_type: Literal["1", "2", "3"] = "3"
    status: Literal["", "P", "D", "X"] = ""
    year: Optional[str] = None

    @field_validator("year")
    @classmethod
    def validate_year(cls, value):
        return search_year(value)

    @model_validator(mode="after")
    def check_name(self):
        self.name = clean(self.name) or ""
        self.advocate_no = clean(self.advocate_no) or ""
        if len(self.name) < 3 and not (self.search_by == "advocate" and self.advocate_no):
            raise ValueError("Enter at least 3 characters to search")
        return self


@app.post("/ngt/bulk_q/search")
def ngt_search(search: SearchRequest):
    form_page = "partyname" if search.search_by == "party" else "advocatename"
    session = open_session(form_page)
    try:
        if search.search_by == "party":
            page = captcha_search(session, "partyData", {
                "zone_type": search.zone, "party_type": search.party_type, "party_name": search.name,
                "case_year": search.year, "status": search.status})
        else:
            page = captcha_search(session, "advocateData", {
                "zone_type": search.zone, "party_type": search.party_type, "advocate_name": search.name,
                "advocate_no": search.advocate_no, "case_year": search.year, "case_status": search.status})
        rows = parse_results(page)
        for row in rows:
            row["zone"] = search.zone
        return JSONResponse(content={"data": rows})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


class CauseListRequest(BaseModel):
    zone: Literal["1", "2", "3", "4", "5"]


@app.post("/ngt/causelist")
def cause_list(request: CauseListRequest):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        lists = []
        for court_name, slug in CAUSE_LIST_PAGES[request.zone]:
            page = get_with_retry(session, f"{BASE_URL}/{slug}").text
            for row, cells in table_rows(page):
                link = re.search(r'href="([^"]+\.pdf)"', row, re.I)
                if not link or len(cells) < 2:
                    continue
                lists.append({
                    "court_id": f"{slug}-{len(lists) + 1}",
                    "court_name": court_name,
                    "description": clean(cells[1]),
                    "causelist_link": requests.utils.requote_uri(link.group(1)),
                })
        return JSONResponse(content={"data": mirror_cause_lists(lists, "ngt")})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


@app.get("/ngt/display_board")
def display_board(zone: Literal["1", "2", "3", "4", "5"]):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        page = get_with_retry(session, BOARD_URL, params={"benchId": zone, "courtId": "1"}).text
        body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
        board, court = [], None
        for row, cells in table_rows(body):
            texts = [clean(c) for c in cells]
            if len(texts) == 1 and texts[0] and texts[0].lower().startswith("court"):
                court = texts[0]
                continue
            if len(texts) == 1 and texts[0] and "not in session" in texts[0].lower():
                board.append({"bench": court or "Court 1", "item_number": None, "case_no": None,
                              "cause_title": None, "sitting": False})
                continue
            if len(texts) >= 3 and texts[0] and texts[0].lower() not in ("item", "court number"):
                item, case_no, title = (texts + [None, None, None])[:3] if len(texts) < 5 else texts[1:4]
                board.append({"bench": court or "Court 1", "item_number": item, "case_no": case_no,
                              "cause_title": title, "sitting": bool(case_no)})
        board.sort(key=lambda b: not b["sitting"])
        return JSONResponse(content={"data": board})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()
