import html
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal, Optional
from urllib.parse import parse_qs, urlparse

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

API_URL = "https://drt.gov.in/drtapi"
ORDER_PDF_URL = "https://cis.drt.gov.in/drtlive/order/secure-pdf-serve.php"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://drt.gov.in",
    "Referer": "https://drt.gov.in/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
}
TIMEOUT = (
    float(os.getenv("DRT_CONNECT_TIMEOUT", "10")),
    float(os.getenv("DRT_READ_TIMEOUT", "90")),
)
MAX_RETRIES = 3
ORDER_WORKERS = int(os.getenv("DRT_ORDER_WORKERS", "4"))
DRAT_FROM = 100

DISPOSED_WORDS = ("dispos", "dismiss", "withdrawn", "allowed", "closed", "abated", "rejected")


class DRTError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def is_drat(scheme_id) -> bool:
    try:
        return int(scheme_id) >= DRAT_FROM
    except (TypeError, ValueError):
        return False


def drt_post(endpoint, fields=None):
    files = {k: (None, str(v)) for k, v in (fields or {}).items()} or None
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
        try:
            response = requests.post(f"{API_URL}/{endpoint}", files=files, data=None if files else {},
                                     headers=HEADERS, timeout=TIMEOUT)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"[warn] DRT {endpoint} failed (attempt {attempt + 1}/{MAX_RETRIES}): {type(exc).__name__}")
            continue
        if response.status_code == 500 and not response.text.strip(" \r\n\""):
            return None
        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise DRTError(f"DRT returned {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise DRTError("DRT returned a non JSON response")
        if isinstance(body, dict) and body.get("status") and len(body) == 1:
            return None
        return body
    raise DRTError(f"DRT did not respond after {MAX_RETRIES} attempts: {last_error!r}", 503)


def error_response(exc: Exception):
    if isinstance(exc, DRTError):
        return JSONResponse(content={"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(content={"error": str(exc)}, status_code=500)


def clean(value):
    if value is None:
        return None
    text = re.sub(r"\s+", " ", html.unescape(str(value))).strip().strip(",").strip()
    return text if text and text.upper() not in {"NULL", "NA", "N/A", "NONE", "UNDEFINED"} else None


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


def is_disposed(*values):
    text = " ".join(v for v in values if v).lower()
    return any(word in text for word in DISPOSED_WORDS)


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def order_file_param(link):
    if not link:
        return None
    values = parse_qs(urlparse(link).query).get("file")
    return values[0] if values else None


def upload_order(file_param, key):
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return s3_url(key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            return None
    try:
        response = requests.get(ORDER_PDF_URL, params={"file": file_param},
                                headers={"User-Agent": HEADERS["User-Agent"]}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        print(f"[drt] order download failed for {key}: {exc}")
        return None
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=response.content,
                         ContentType="application/pdf", ContentDisposition="inline")
    return s3_url(key)


def order_rows(scheme_id, diary_no, diary_year):
    fields = {"schemeNameDrtId": scheme_id, "diaryNo": diary_no, "diaryYear": diary_year}
    if is_drat(scheme_id):
        lookups = [("getDratOrderJudgementReportDiaryNo", {**fields, "dratDailyFinalOrderId": "1"}, "Daily Order"),
                   ("getDratOrderJudgementReportDiaryNo", {**fields, "dratDailyFinalOrderId": "2"}, "Final Order")]
    else:
        lookups = [("getDrtDailyOrderReportDiaryNo", fields, "Daily Order"),
                   ("getDrtFinalOrderReportDiaryNo", fields, "Final Order")]
    rows = []
    for endpoint, payload, kind in lookups:
        try:
            found = drt_post(endpoint, payload)
        except DRTError as exc:
            print(f"[drt] {endpoint} failed for {fields}: {exc}")
            continue
        rows.extend((r, kind) for r in found or [] if isinstance(r, dict))
    return rows


def build_orders(scheme_id, diary_no, diary_year):
    prefix = f"case_data/orders/drt/{scheme_id}/{diary_no}-{diary_year}/"
    entries, seen = [], set()
    for row, kind in order_rows(scheme_id, diary_no, diary_year):
        link = next((v for v in row.values() if isinstance(v, str) and v.startswith("http")), None)
        file_param = order_file_param(link)
        if not file_param or file_param in seen:
            continue
        seen.add(file_param)
        order_date = to_ddmmyyyy(row.get("orderDate") or row.get("dateoforder") or row.get("dateoffiling"))
        entries.append({
            "file": file_param,
            "order_date": order_date,
            "order_type": kind,
            "key": order_pdf_s3_key(prefix, order_date, file_param),
        })
    entries.sort(key=lambda e: sort_key(e["order_date"]))
    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as pool:
        links = list(pool.map(lambda e: upload_order(e["file"], e["key"]), entries))
    return [
        {
            "order_number": str(i),
            "order_date": entry["order_date"],
            "order_type": entry["order_type"],
            "order_link": link,
            "order_status": "available" if link else "unavailable",
        }
        for i, (entry, link) in enumerate(zip(entries, links), start=1)
    ]


def transform_case(detail, scheme_id, scheme_name, orders):
    proceedings = detail.get("caseProceedingDetails") or []
    case_history, seen = [], set()
    for row in proceedings:
        listed = to_ddmmyyyy(row.get("causelistdate"))
        purpose = clean(row.get("purpose"))
        if not listed or (listed, purpose) in seen:
            continue
        seen.add((listed, purpose))
        court = clean(row.get("courtNo"))
        bench = clean(row.get("courtName") or row.get("ascourtName"))
        case_history.append({
            "judge": ", ".join(filter(None, [f"Court {court}" if court else None, bench])) or None,
            "businessOnDate": listed,
            "hearingDate": None,
            "purpose": purpose,
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    case_history.sort(key=lambda h: sort_key(h["businessOnDate"]))
    for current, following in zip(case_history, case_history[1:]):
        current["hearingDate"] = following["businessOnDate"]

    status = clean(detail.get("casestatus"))
    disposal_nature = clean(detail.get("disposalNature"))
    disposed = detail.get("status") == "D" or is_disposed(status, disposal_nature)
    next_hearing = to_ddmmyyyy(detail.get("nextlistingdate"))
    if case_history and not disposed:
        case_history[-1]["hearingDate"] = next_hearing

    case_type = clean(detail.get("casetype"))
    case_no = clean(detail.get("caseno"))
    case_year = clean(detail.get("caseyear"))
    diary_no = clean(detail.get("diaryno"))
    diary_year = clean(detail.get("diaryyear"))
    registration = f"{case_type}/{case_no}/{case_year}" if case_type and case_no and case_year else None
    court = clean(detail.get("courtNo"))

    petitioners = [n for n in [clean(detail.get("petitionerName")), clean(detail.get("additionalpartypet"))] if n]
    respondents = [n for n in [clean(detail.get("respondentName")), clean(detail.get("additionalpartyres"))] if n]

    return {
        "courtType": "drat" if is_drat(scheme_id) else "drt",
        "case_no": f"{diary_no}/{diary_year}",
        "cino": f"{'DRAT' if is_drat(scheme_id) else 'DRT'}-{scheme_id}-{registration or f'D{diary_no}/{diary_year}'}",
        "court_complex_code": str(scheme_id),
        "bench_name": scheme_name,
        "case_reg_no": case_no,
        "rgyear": case_year,
        "case_type": case_type,
        "fir_details": {},
        "CaseType": case_type,
        "FilingNumber": f"{diary_no}/{diary_year}",
        "RegistrationNumber": registration,
        "CNRNumber": registration,
        "FilingDate": to_ddmmyyyy(detail.get("dateoffiling")),
        "FirstHearingDate": case_history[0]["businessOnDate"] if case_history else None,
        "NextHearingDate": None if disposed else next_hearing,
        "NextHearingPurpose": None if disposed else clean(detail.get("nextListingPurpose")),
        "DecisionDate": to_ddmmyyyy(detail.get("dateofdisposal")) if disposed else None,
        "CaseStatus": status,
        "CaseStage": clean(detail.get("nextListingPurpose")) or status,
        "NatureofDisposal": disposal_nature if disposed else None,
        "CourtNumberandJudge": ", ".join(filter(None, [f"Court {court}" if court else None,
                                                       clean(detail.get("courtName"))])) or None,
        "petitioner_and_advocate": petitioners or [""],
        "respondent_and_advocate": respondents or [""],
        "petitioner_advocates": [a for a in [clean(detail.get("advocatePetName"))] if a],
        "respondent_advocates": [a for a in [clean(detail.get("advocateResName"))] if a],
        "main_case": clean(detail.get("maincasecaseno")) or clean(detail.get("maincasediaryno")),
        "applications_filed": [
            {"case_no": clean(ia.get("iano")), "filing_date": to_ddmmyyyy(ia.get("iadateoffiling")),
             "order_date": to_ddmmyyyy(ia.get("iaorderdate"))}
            for ia in detail.get("iaDetails") or []
        ],
        "connected_cases": [
            {"case_no": clean(c.get("maincaseno")), "diary_no": clean(c.get("maincasedairy"))}
            for c in detail.get("othercase") or []
        ],
        "actsandSection": {"acts": "Recovery of Debts and Bankruptcy Act, 1993", "section": None},
        "case_history": case_history,
        "case_transfer": [],
        "orders": orders,
        "orders_synced_at": orders_stamp(),
    }


def scheme_name(scheme_id):
    schemes = drt_post("getDrtDratScheamName") or []
    match = next((s for s in schemes if str(s.get("schemeNameDrtId")) == str(scheme_id)), None)
    return clean(match.get("SchemaName")) if match else None


class CaseRequest(BaseModel):
    scheme_id: str
    diary_no: Optional[str] = None
    diary_year: Optional[str] = None
    case_type: Optional[str] = None
    case_no: Optional[str] = None
    case_year: Optional[str] = None
    refresh: int = 0

    @model_validator(mode="after")
    def check_identifiers(self):
        if not self.scheme_id.isdigit():
            raise ValueError("Select the DRT or DRAT")
        if self.diary_no and self.diary_year:
            return self
        if self.case_type and self.case_no and self.case_year:
            return self
        raise ValueError("Enter the diary number and year, or the case type, number and year")


@app.post("/drt/getcaseInfo")
def fetch_drt_case(case_data: CaseRequest):
    drat = is_drat(case_data.scheme_id)
    if case_data.diary_no:
        ac_query = {"courtType": "drat" if drat else "drt", "court_complex_code": case_data.scheme_id,
                    "FilingNumber": f"{case_data.diary_no}/{case_data.diary_year}"}
    else:
        ac_query = {"courtType": "drat" if drat else "drt", "court_complex_code": case_data.scheme_id,
                    "case_reg_no": case_data.case_no, "rgyear": case_data.case_year}
    existing_case = collection.find_one(ac_query)
    if existing_case and case_data.refresh == 0 and not cached_case_needs_orders(existing_case):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    try:
        if case_data.diary_no:
            detail = drt_post("getDratCaseDetailDiaryNoWise" if drat else "getCaseDetailDiaryNoWise", {
                "schemeNameDrtId": case_data.scheme_id, "diaryNo": case_data.diary_no,
                "diaryYear": case_data.diary_year})
        else:
            detail = drt_post("getDratCaseDetailCaseNoWise" if drat else "getCaseDetailCaseNoWise", {
                "schemeNameDrtId": case_data.scheme_id, "casetypeId": case_data.case_type,
                "caseNo": case_data.case_no, "caseYear": case_data.case_year})
        if not isinstance(detail, dict) or not detail.get("diaryno"):
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        orders = build_orders(case_data.scheme_id, detail["diaryno"], detail["diaryyear"])
        result = transform_case(detail, case_data.scheme_id, scheme_name(case_data.scheme_id), orders)
        result["_id"] = save_case(result, existing_case["_id"] if existing_case else None)
        return JSONResponse(content=jsonable_encoder(result), status_code=200)
    except Exception as exc:
        return error_response(exc)


def search_row(row, scheme_id):
    diary = clean(row.get("diaryno"))
    diary_no, _, diary_year = (diary or "").partition("/")
    return {
        "scheme_id": str(scheme_id),
        "diary_no": diary_no or None,
        "diary_year": diary_year or None,
        "case_no": clean(row.get("caseno")),
        "case_type": clean(row.get("casetype")),
        "petitioner_name": clean(row.get("applicant")),
        "respondent_name": clean(row.get("respondent")),
        "petitioner_advocate": clean(row.get("applicantadvocate")),
        "respondent_advocate": clean(row.get("respondentadvocate")),
        "filing_date": to_ddmmyyyy(row.get("dateoffiling")),
        "status": {"D": "Disposed", "P": "Pending"}.get(row.get("stage"), clean(row.get("stage"))),
        "filing_no": clean(row.get("filingNo")),
    }


def search_results(rows, scheme_id):
    results, seen = [], set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = search_row(row, scheme_id)
        key = (item["diary_no"], item["diary_year"])
        if not item["diary_no"] or key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results


class PartySearchRequest(BaseModel):
    scheme_id: str
    party_name: str

    @field_validator("party_name")
    @classmethod
    def validate_name(cls, value):
        value = clean(value) or ""
        if len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value.upper()


@app.post("/drt/bulk_q/party")
def party_search(search: PartySearchRequest):
    try:
        rows = drt_post("drat_party_name_wise" if is_drat(search.scheme_id) else "casedetail_party_name_wise",
                        {"schemeNameDratDrtId": search.scheme_id, "partyName": search.party_name})
        return JSONResponse(content={"data": search_results(rows, search.scheme_id)})
    except Exception as exc:
        return error_response(exc)


class AdvocateSearchRequest(BaseModel):
    scheme_id: str
    advocate_name: str
    advocate_type: Literal["1", "2"] = "1"

    @field_validator("advocate_name")
    @classmethod
    def validate_name(cls, value):
        value = clean(value) or ""
        if len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value.upper()


@app.post("/drt/bulk_q/advocate")
def advocate_search(search: AdvocateSearchRequest):
    try:
        rows = drt_post("casedetail_adv_name_wise_mob", {
            "catschemaId": search.scheme_id, "advName": search.advocate_name, "advType": search.advocate_type})
        return JSONResponse(content={"data": search_results(rows, search.scheme_id)})
    except Exception as exc:
        return error_response(exc)


class CauseListRequest(BaseModel):
    scheme_id: str
    hearing_date: str

    @field_validator("hearing_date")
    @classmethod
    def validate_date(cls, value):
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")


@app.post("/drt/causelist")
def cause_list(request: CauseListRequest):
    drat = is_drat(request.scheme_id)
    try:
        benches = drt_post("getDratCourtName" if drat else "getCourtName",
                           {"schemeNameDrtId": request.scheme_id, "listingDate": request.hearing_date}) or []
        lists = []
        for bench in benches if isinstance(benches, list) else []:
            court_name_id = bench.get("courtNameId")
            numbers = drt_post("getDratCourtNo" if drat else "getDrtDratCourtNo", {
                "schemeNameDrtId": request.scheme_id, "listingDate": request.hearing_date,
                "courtNameId": court_name_id, "benchNature": court_name_id}) or []
            numbers = numbers if isinstance(numbers, list) else [numbers]
            for number in numbers:
                court_no = number.get("courtNo") if isinstance(number, dict) else None
                if not court_no:
                    continue
                report = drt_post("getDratCauselistReport" if drat else "getDrtCauselistReport", {
                    "schemeNameDrtId": request.scheme_id, "causeListDate": request.hearing_date,
                    "courtNameId": court_name_id, "courtNo": court_no}) or {}
                daily = report.get("dailyCauseListLink") if isinstance(report, dict) else None
                if not daily:
                    continue
                lists.append({
                    "court_id": f"{court_name_id}-{court_no}",
                    "court_name": f"{clean(bench.get('judgeName') or bench.get('benchName')) or 'Court'} - Court {court_no}",
                    "causelist_link": daily,
                    "supplementary_link": report.get("supplementaryCauseListLink"),
                })
        return JSONResponse(content={"data": lists})
    except Exception as exc:
        return error_response(exc)
