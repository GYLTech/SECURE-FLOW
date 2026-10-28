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
from core.s3_client import s3_client
from helpers.orders import cached_case_needs_orders, order_pdf_s3_key, orders_stamp

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()

BASE_URL = "https://cis.cgat.gov.in/catlive"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
TIMEOUT = (
    float(os.getenv("CGAT_CONNECT_TIMEOUT", "10")),
    float(os.getenv("CGAT_READ_TIMEOUT", "90")),
)
MAX_RETRIES = 3
ORDER_WORKERS = int(os.getenv("CGAT_ORDER_WORKERS", "4"))
DISPOSED_WORDS = ("dispos", "dismiss", "withdrawn", "allowed", "closed", "abated", "rejected")


class CGATError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def open_session(page="case_status.php"):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    request_with_retry(session, "GET", f"{BASE_URL}/{page}")
    return session


def request_with_retry(session, method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"[warn] CGAT {method} {url} failed (attempt {attempt + 1}/{MAX_RETRIES}): {type(exc).__name__}")
            continue
        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise CGATError(f"CGAT returned {response.status_code}")
        return response
    raise CGATError(f"CGAT did not respond after {MAX_RETRIES} attempts: {last_error!r}", 503)


def error_response(exc: Exception):
    if isinstance(exc, CGATError):
        return JSONResponse(content={"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(content={"error": str(exc)}, status_code=500)


def clean(value):
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value)).replace("\xa0", " "))
    text = re.sub(r"\s+", " ", text).strip().strip(",").strip()
    return text if text and text.upper() not in {"NULL", "NA", "N/A", "NONE", "-", "1."} else None


def to_ddmmyyyy(value):
    text = clean(value)
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
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


def table_rows(page):
    return [
        (row, re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S))
        for row in re.findall(r"<tr.*?</tr>", page, re.S)
    ]


def parse_search(page, bench):
    headers = [(clean(h) or "").lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", page, re.S)]

    def column(cells, name, fallback=None):
        index = next((i for i, h in enumerate(headers) if h.startswith(name)), fallback)
        return clean(cells[index]) if index is not None and index < len(cells) else None

    results, seen = [], set()
    for row, cells in table_rows(page):
        ref = re.search(r"popsurety_detailreport\('([^']+)'\)", row)
        if not ref or len(cells) < 5 or ref.group(1) in seen:
            continue
        seen.add(ref.group(1))
        diary = column(cells, "diary") or ""
        diary_no, _, diary_year = diary.partition("/")
        results.append({
            "bench": str(bench),
            "details_ref": ref.group(1),
            "diary_no": diary_no or None,
            "diary_year": diary_year or None,
            "case_no": column(cells, "case no"),
            "petitioner_name": column(cells, "applicant"),
            "respondent_name": column(cells, "respondent") or column(cells, "defendent"),
            "advocate": column(cells, "advocate"),
            "filing_date": to_ddmmyyyy(column(cells, "date of filing")),
        })
    return results


def search(session, params):
    page = request_with_retry(session, "GET", f"{BASE_URL}/partyDetail.php", params=params).text
    return page


DETAIL_LABELS = (
    "Diary No.-", "Filing Date", "Location", "Case Number", "Status / Stage", "Disposal Nature",
    "Date of Disposal", "In the Court no.", "Petitioner(s)", "Petitoner Advocate(s)", "Respondent(s)",
    "Respondent Advocate(s)", "Subject", "Next Date of Hearing",
)
STOP_LABELS = DETAIL_LABELS + (
    "Court Fee", "Group", "Petitioner(s) Address", "Respondent(s) Address", "Vs",
    "REFERANCE CASE DETAILS", "CONNECT CASE DETAILS", "DOCUMENT FILING DETAILS",
)


def details_values(page):
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    tokens = [t for t in (clean(x) for x in re.split(r"<[^>]+>", body)) if t]
    values = {}
    for i, token in enumerate(tokens):
        label = token.rstrip(":").strip()
        if label in DETAIL_LABELS and label not in values:
            following = tokens[i + 1] if i + 1 < len(tokens) else None
            values[label] = None if following and following.rstrip(":").strip() in STOP_LABELS else following
        if token == "Vs" and "Parties" not in values and 0 < i < len(tokens) - 1:
            values["Parties"] = (tokens[i - 1], tokens[i + 1])
    return values


def split_names(value):
    names = []
    for part in re.split(r",", value or ""):
        name = clean(re.sub(r"\((?:M|F)\)", "", part))
        if name and name not in names:
            names.append(name)
    return names


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def upload_order(file_param, key):
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return s3_url(key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            return None
    try:
        response = requests.get(f"{BASE_URL}/pdf/order.php", params={"file": file_param},
                                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        print(f"[cgat] order download failed for {key}: {exc}")
        return None
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=response.content,
                         ContentType="application/pdf", ContentDisposition="inline")
    return s3_url(key)


def build_orders(session, bench, diary_no, diary_year):
    page = request_with_retry(session, "GET", f"{BASE_URL}/order_detail.php", params={
        "dairy_no": diary_no, "benchCode2": bench, "dairy_year": diary_year, "id": "dairynowise1"}).text
    prefix = f"case_data/orders/cgat/{bench}/{diary_no}-{diary_year}/"
    entries, seen = [], set()
    for row, cells in table_rows(page):
        link = re.search(r"pdf/order\.php\?file=([^\"'&]+)", row)
        if not link or link.group(1) in seen or len(cells) < 4:
            continue
        seen.add(link.group(1))
        order_date = to_ddmmyyyy(cells[3])
        entries.append((link.group(1), order_date, order_pdf_s3_key(prefix, order_date, link.group(1))))
    entries.sort(key=lambda e: sort_key(e[1]))
    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as pool:
        links = list(pool.map(lambda e: upload_order(e[0], e[2]), entries))
    return [
        {
            "order_number": str(i),
            "order_date": order_date,
            "order_type": "Daily Order",
            "order_link": link,
            "order_status": "available" if link else "unavailable",
        }
        for i, ((_, order_date, _), link) in enumerate(zip(entries, links), start=1)
    ]


def transform_case(values, bench, details_ref, orders):
    diary = values.get("Diary No.-") or ""
    diary_no, _, diary_year = diary.partition("/")
    case_number = values.get("Case Number")
    main_number = (case_number or "").split(" IN ")[0].strip() or None
    number_parts = re.match(r"(.+?)/(\d+)/(\d{4})", main_number or "")
    status = values.get("Status / Stage")
    disposal = values.get("Disposal Nature")
    disposed = any(w in f"{status} {disposal}".lower() for w in DISPOSED_WORDS)
    parties = values.get("Parties") or (None, None)

    case_history = []
    for current, nxt in zip(orders, orders[1:] + [None]):
        case_history.append({
            "judge": None,
            "businessOnDate": current["order_date"],
            "hearingDate": nxt["order_date"] if nxt else None,
            "purpose": "Daily Order",
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    next_hearing = to_ddmmyyyy(values.get("Next Date of Hearing") or values.get("Next Hearing Date"))
    if case_history and not disposed:
        case_history[-1]["hearingDate"] = next_hearing

    filing_ref = base64.b64decode(details_ref + "==").decode(errors="ignore").split("/")[0]

    return {
        "courtType": "cgat",
        "case_no": re.sub(r"\D", "", filing_ref) or diary,
        "cino": f"CAT-{bench}-{main_number or diary}",
        "details_ref": details_ref,
        "court_complex_code": str(bench),
        "case_reg_no": number_parts.group(2) if number_parts else None,
        "rgyear": number_parts.group(3) if number_parts else None,
        "case_type": number_parts.group(1) if number_parts else None,
        "fir_details": {},
        "CaseType": number_parts.group(1) if number_parts else None,
        "FilingNumber": diary or None,
        "DiaryNo": diary_no or None,
        "DiaryYear": diary_year or None,
        "RegistrationNumber": case_number,
        "CNRNumber": case_number,
        "FilingDate": to_ddmmyyyy(values.get("Filing Date")),
        "FirstHearingDate": case_history[0]["businessOnDate"] if case_history else None,
        "NextHearingDate": None if disposed else next_hearing,
        "DecisionDate": to_ddmmyyyy(values.get("Date of Disposal")) if disposed else None,
        "CaseStatus": status,
        "CaseStage": status,
        "NatureofDisposal": disposal if disposed else None,
        "CourtNumberandJudge": values.get("In the Court no."),
        "petitioner_and_advocate": split_names(values.get("Petitioner(s)")) or [clean(parties[0]) or ""],
        "respondent_and_advocate": split_names(values.get("Respondent(s)")) or [clean(parties[1]) or ""],
        "petitioner_advocates": split_names(values.get("Petitoner Advocate(s)") or values.get("Petitioner Advocate(s)")),
        "respondent_advocates": split_names(values.get("Respondent Advocate(s)")),
        "subject": values.get("Subject"),
        "actsandSection": {"acts": "Administrative Tribunals Act, 1985", "section": values.get("Subject")},
        "case_history": case_history,
        "case_transfer": [],
        "orders": orders,
        "orders_synced_at": orders_stamp(),
    }


class CaseRequest(BaseModel):
    bench: str
    diary_no: Optional[str] = None
    diary_year: Optional[str] = None
    case_type: Optional[str] = None
    case_no: Optional[str] = None
    case_year: Optional[str] = None
    details_ref: Optional[str] = None
    refresh: int = 0

    @model_validator(mode="after")
    def check_identifiers(self):
        if not self.bench.isdigit():
            raise ValueError("Select the CAT bench")
        if self.details_ref:
            if not re.fullmatch(r"[A-Za-z0-9+/=]+", self.details_ref):
                raise ValueError("Invalid CAT case reference")
            return self
        if self.diary_no and self.diary_year:
            return self
        if self.case_type and self.case_no and self.case_year:
            return self
        raise ValueError("Enter the diary number and year, or the case type, number and year")


@app.post("/cgat/getcaseInfo")
def fetch_cgat_case(case_data: CaseRequest):
    if case_data.details_ref:
        ac_query = {"courtType": "cgat", "details_ref": case_data.details_ref}
    elif case_data.diary_no:
        ac_query = {"courtType": "cgat", "court_complex_code": case_data.bench,
                    "FilingNumber": f"{case_data.diary_no}/{case_data.diary_year}"}
    else:
        ac_query = {"courtType": "cgat", "court_complex_code": case_data.bench,
                    "case_reg_no": case_data.case_no, "rgyear": case_data.case_year}
    existing_case = collection.find_one(ac_query)
    if existing_case and case_data.refresh == 0 and not cached_case_needs_orders(existing_case):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    session = open_session()
    try:
        details_ref = case_data.details_ref
        if not details_ref:
            if case_data.diary_no:
                page = search(session, {"dairy_no": case_data.diary_no, "benchCode2": case_data.bench,
                                        "dairy_year": case_data.diary_year, "id": "dairynowise1"})
            else:
                page = search(session, {"caseNo": case_data.case_no, "benchCode1": case_data.bench,
                                        "caseType": case_data.case_type, "year": case_data.case_year,
                                        "id": "casetypewise"})
            rows = parse_search(page, case_data.bench)
            if case_data.diary_no:
                rows = [r for r in rows if r["diary_no"] == case_data.diary_no] or rows
            if not rows:
                return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)
            details_ref = rows[0]["details_ref"]

        page = request_with_retry(session, "GET", f"{BASE_URL}/Misdetailreport123.php", params={"no": details_ref}).text
        values = details_values(page)
        diary = values.get("Diary No.-")
        if not diary:
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)
        diary_no, _, diary_year = diary.partition("/")
        orders = build_orders(session, case_data.bench, diary_no, diary_year)
        result = transform_case(values, case_data.bench, details_ref, orders)
        result["_id"] = save_case(result, existing_case["_id"] if existing_case else None)
        return JSONResponse(content=jsonable_encoder(result), status_code=200)
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


class SearchRequest(BaseModel):
    bench: str
    search_by: Literal["party", "advocate"]
    name: str
    party_type: Literal["1", "2", "3"] = "1"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = clean(value) or ""
        if len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value.upper()

    @field_validator("bench")
    @classmethod
    def validate_bench(cls, value):
        if not value.isdigit():
            raise ValueError("Select the CAT bench")
        return value


@app.post("/cgat/bulk_q/search")
def cgat_search(request: SearchRequest):
    session = open_session()
    try:
        if request.search_by == "party":
            params = {"nameParty": request.name, "benchCode3": request.bench, "partyType": request.party_type,
                      "id": "partynamewise"}
        else:
            params = {"nameAdv": request.name, "benchCode4": request.bench, "AdvocateType": request.party_type,
                      "id": "advnamewise"}
        return JSONResponse(content={"data": parse_search(search(session, params), request.bench)})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


class BenchRequest(BaseModel):
    bench: str

    @field_validator("bench")
    @classmethod
    def validate_bench(cls, value):
        if not value.isdigit():
            raise ValueError("Select the CAT bench")
        return value


@app.post("/cgat/causelist")
def cause_list(request: BenchRequest):
    session = open_session("display_causelist.php")
    try:
        page = request_with_retry(session, "POST", f"{BASE_URL}/display_causelist.php",
                                  data={"schemas": request.bench, "submit1": "Submit"}).text
        lists = []
        for kind, script in (("Cause List", "public_causelist_save.php"),
                             ("Supplementary Cause List", "public_sup_causelist_print.php")):
            name = "popsurety_pet_adv_name_save" if kind == "Cause List" else "popsurety_pet_adv_name1_savesap"
            for filing, list_id in re.findall(name + r"\('([^']+)','([^']+)'\)", page):
                decoded = base64.b64decode(filing + "==").decode(errors="ignore")
                listed = to_ddmmyyyy(decoded.split("#")[0])
                lists.append({
                    "court_id": f"{kind}-{filing}",
                    "court_name": kind,
                    "description": f"{kind} for {listed}" if listed else kind,
                    "causelist_link": f"{BASE_URL}/{script}?filing_no={filing}&id={list_id}",
                })
        return JSONResponse(content={"data": lists})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


@app.post("/cgat/display_board")
def display_board(request: BenchRequest):
    session = open_session("display_bench1.php")
    try:
        page = request_with_retry(session, "POST", f"{BASE_URL}/Displayboard.php",
                                  data={"schemas": request.bench, "submit1": "Submit"}).text
        body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
        board, started = [], False
        for _, cells in table_rows(body):
            texts = [clean(c) for c in cells]
            if "Court No." in texts:
                started = True
                continue
            if not started or len(texts) < 3:
                continue
            court = texts[1]
            item = texts[2] if len(texts) > 3 else None
            session_text = texts[3] if len(texts) > 3 else texts[2]
            sitting = bool(item) and "over" not in (session_text or "").lower()
            board.append({
                "bench": f"Court {court}" if court else "Court",
                "item_number": item if sitting else None,
                "case_no": None,
                "cause_title": texts[4] if sitting and len(texts) > 4 else session_text,
                "sitting": sitting,
            })
        board.sort(key=lambda b: not b["sitting"])
        return JSONResponse(content={"data": board})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()
