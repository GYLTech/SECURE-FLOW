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

BASE_URL = "https://efiling.nclt.gov.in"
FORM_PAGE = f"{BASE_URL}/casehistorybeforeloginmenutrue.drt"
SEARCH_URL = f"{BASE_URL}/caseHistoryoptional.drt"
CIN_SEARCH_URL = f"{BASE_URL}/caseHistoryoptionalCIN.drt"
COMPANY_URL = f"{BASE_URL}/getCompanyName.drt"
DETAILS_URL = f"{BASE_URL}/caseHistoryalldetails.drt"
ORDER_URL = f"{BASE_URL}/ordersview.drt"

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": FORM_PAGE,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
}
TIMEOUT = (
    float(os.getenv("NCLT_CONNECT_TIMEOUT", "10")),
    float(os.getenv("NCLT_READ_TIMEOUT", "90")),
)
MAX_RETRIES = 3
ORDER_WORKERS = int(os.getenv("NCLT_ORDER_WORKERS", "4"))
NO_DATE = "1111-11-11"

SEARCH_TEMPLATE = {
    "wayofselection": "none",
    "i_bench_id": "0",
    "filing_no": "",
    "i_bench_id_case_no": "0",
    "i_case_type_caseno": "0",
    "i_case_year_caseno": "0",
    "case_no": "",
    "i_party_search": "E",
    "i_bench_id_party": "0",
    "party_type_party": "0",
    "party_name_party": "",
    "i_case_year_party": "0",
    "status_party": "0",
    "i_adv_search": "E",
    "i_bench_id_lawyer": "0",
    "party_lawer_name": "",
    "i_case_year_lawyer": "0",
    "bar_council_advocate": "",
}

DISPOSED_WORDS = ("dispose", "dismiss", "withdrawn", "allowed", "closed", "rejected", "abated")


class NCLTError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def open_session():
    session = requests.Session()
    session.headers.update({"User-Agent": HEADERS["User-Agent"]})
    try:
        session.get(FORM_PAGE, timeout=TIMEOUT)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        pass
    return session


def nclt_request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
        try:
            response = session.request(method, url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"[warn] NCLT {method} {url} failed (attempt {attempt + 1}/{MAX_RETRIES}): "
                  f"{type(exc).__name__}")
            continue
        if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise NCLTError(f"NCLT returned {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise NCLTError("NCLT returned a non JSON response")
    raise NCLTError(f"NCLT did not respond after {MAX_RETRIES} attempts: {last_error!r}", 503)


def error_response(exc: Exception):
    if isinstance(exc, NCLTError):
        return JSONResponse(content={"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(content={"error": str(exc)}, status_code=500)


def clean(value):
    if value is None:
        return None
    text = re.sub(r"\s+", " ", html.unescape(str(value))).strip().strip(",").strip()
    text = re.sub(r"\.{2,}$", "", text).strip()
    if text.startswith("[Ljava") or text == "Dispose":
        return "Disposed" if text == "Dispose" else None
    return text if text and text.upper() not in {"NULL", "NA", "N/A", "NONE", "UNDEFINED"} else None


def to_ddmmyyyy(value):
    text = clean(value)
    if not text or text.startswith(NO_DATE):
        return None
    text = text.split(" ")[0]
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return None


def sort_key(ddmmyyyy):
    try:
        return datetime.strptime(ddmmyyyy, "%d-%m-%Y")
    except (TypeError, ValueError):
        return datetime.max


def is_disposed(status):
    text = (status or "").lower()
    return any(word in text for word in DISPOSED_WORDS)


def search_cases(session, payload):
    body = nclt_request(session, "POST", SEARCH_URL, json={**SEARCH_TEMPLATE, **payload})
    rows = body.get("mainpanellist") or []
    if not rows and body.get("errormsg"):
        message = clean(body["errormsg"]) or "No cases found"
        if "valid bench" in message.lower():
            raise NCLTError("Please select a bench", 422)
    return rows


def search_row(row):
    return {
        "filing_no": clean(row.get("filing_no")),
        "case_no": clean(row.get("case_no")) or clean(row.get("caseNo1")),
        "case_type": clean(row.get("case_type_desc_cis")),
        "petitioner_name": clean(row.get("case_title1")),
        "respondent_name": clean(row.get("case_title2")),
        "bench": clean(row.get("bench_location_name")),
        "court_no": clean(row.get("court_no")),
        "main_case_filing_no": clean(row.get("main_case_fno")),
        "filing_date": to_ddmmyyyy(row.get("date_of_filing")),
        "registration_date": to_ddmmyyyy(row.get("regis_date")),
        "next_listing_date": to_ddmmyyyy(row.get("next_list_date")),
        "disposal_date": to_ddmmyyyy(row.get("disposal_date")),
        "status": clean(row.get("status")) or {"P": "Pending", "D": "Disposed"}.get(row.get("case_status")),
        "case_stage": clean(row.get("case_stage")) or clean(row.get("caseStageMainPanel")),
        "party_role": {"P": "Petitioner", "R": "Respondent"}.get(row.get("partyFlag")),
        "all_hearings": clean(row.get("allhearing")),
    }


def unique_rows(rows):
    seen, results = set(), []
    for row in rows:
        item = search_row(row)
        if not item["filing_no"] or item["filing_no"] in seen:
            continue
        seen.add(item["filing_no"])
        results.append(item)
    return results


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def upload_order(enc_path, key):
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return s3_url(key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            return None

    try:
        with requests.get(ORDER_URL, params={"path": enc_path}, headers={"User-Agent": HEADERS["User-Agent"]},
                          stream=True, timeout=TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            if response.status_code != 200 or "pdf" not in content_type.lower():
                return None
            s3_client.upload_fileobj(
                response.raw, BUCKET_NAME, key,
                ExtraArgs={"ContentType": "application/pdf", "ContentDisposition": "inline"},
            )
            return s3_url(key)
    except requests.exceptions.RequestException as exc:
        print(f"[nclt] order download failed for {key}: {exc}")
        return None


def build_orders(filing_no, proceedings):
    prefix = f"case_data/orders/nclt/{filing_no}/"
    by_path = {}
    for row in proceedings:
        path = clean(row.get("path"))
        if not path or not row.get("encPath") or path in by_path:
            continue
        order_date = to_ddmmyyyy(row.get("order_upload_date")) or to_ddmmyyyy(row.get("listing_date"))
        by_path[path] = {
            "enc_path": row["encPath"],
            "order_date": order_date,
            "order_type": clean(row.get("path_descr")),
            "key": order_pdf_s3_key(prefix, order_date, path),
        }

    entries = sorted(by_path.values(), key=lambda o: sort_key(o["order_date"]))
    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as pool:
        links = list(pool.map(lambda o: upload_order(o["enc_path"], o["key"]), entries))

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


def split_advocates(value):
    names = []
    for name in (clean(value) or "").split(","):
        name = clean(name)
        if name and name not in names:
            names.append(name)
    return names


def parse_parties(party_rows):
    parties = {"P": [], "R": []}
    advocates = {"P": [], "R": []}
    for row in party_rows:
        side = (row.get("party_type") or "")[:1].upper()
        if side not in parties:
            continue
        name = clean(row.get("party_name"))
        if name and name not in parties[side]:
            parties[side].append(name)
        for adv in split_advocates(row.get("party_lawer_name")):
            if adv not in advocates[side]:
                advocates[side].append(adv)
    return parties, advocates


def transform_case(main_row, details, orders, request_fields):
    filing_no = clean(main_row.get("filing_no"))
    registered = (details.get("isregistered") or [{}])[0]
    final_status = (details.get("allfinalstatuslist") or [{}])[0]
    disposal = (details.get("disposedetailslist") or [{}])[0]
    last_proceeding = (details.get("get_last_proceeding") or [{}])[0]
    proceedings = details.get("allproceedingdtls") or []

    case_history, seen = [], set()
    for row in proceedings:
        business_on = to_ddmmyyyy(row.get("listing_date"))
        next_date = to_ddmmyyyy(row.get("next_list_date"))
        if not business_on and not next_date:
            continue
        if (business_on, next_date) in seen:
            continue
        seen.add((business_on, next_date))
        court = clean(row.get("court_no"))
        bench = clean(row.get("bench_location_name"))
        case_history.append({
            "judge": ", ".join(filter(None, [f"Court {court}" if court else None, bench.title() if bench else None])) or None,
            "businessOnDate": business_on,
            "hearingDate": next_date,
            "purpose": clean(row.get("today_action")) or clean(row.get("purpose")) or clean(row.get("next_listing_purpose")),
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    case_history.sort(key=lambda h: (sort_key(h["businessOnDate"] or h["hearingDate"]), h["businessOnDate"] is not None))

    parties, advocates = parse_parties(details.get("partydetailslist") or [])
    petitioner = parties["P"] or [clean(main_row.get("case_title1")) or ""]
    respondent = parties["R"] or [clean(main_row.get("case_title2")) or ""]

    status = clean(registered.get("status")) or clean(main_row.get("status")) or clean(final_status.get("case_status"))
    disposed = is_disposed(status) or bool(clean(disposal.get("action_type")))
    next_hearing = to_ddmmyyyy(last_proceeding.get("next_list_date")) or to_ddmmyyyy(main_row.get("next_list_date"))
    decision_date = to_ddmmyyyy(disposal.get("disposal_date")) or to_ddmmyyyy(main_row.get("disposal_date"))
    case_no = clean(main_row.get("case_no")) or clean(registered.get("case_no"))
    court_no = clean(main_row.get("court_no")) or clean(registered.get("court_no"))
    bench = clean(main_row.get("bench_location_name"))

    applications = [
        {
            "filing_no": clean(ia.get("filing_no")),
            "case_no": clean(ia.get("case_no")),
            "case_type": clean(ia.get("case_type_desc_cis")),
            "filing_date": to_ddmmyyyy(ia.get("date_of_filing")),
            "status": clean(ia.get("status")),
            "disposal": clean(ia.get("action_type")),
            "disposal_date": to_ddmmyyyy(ia.get("disposal_date")),
        }
        for ia in details.get("mainFilnowithIaNoList") or []
    ]

    return {
        "courtType": "nclt",
        "case_no": filing_no,
        "cino": case_no or filing_no,
        "est_code": request_fields.get("est_code"),
        "state_code": request_fields.get("state_code"),
        "dist_code": request_fields.get("dist_code"),
        "court_complex_code": request_fields.get("court_complex_code"),
        "rgyear": request_fields.get("rgyear"),
        "case_type": clean(registered.get("case_type")) or request_fields.get("case_type"),
        "fir_details": {},
        "bench_name": bench,
        "CaseType": clean(main_row.get("case_type_desc_cis")) or clean(registered.get("case_type_desc_cis")),
        "FilingNumber": filing_no,
        "RegistrationNumber": case_no or filing_no,
        "CNRNumber": case_no or filing_no,
        "FilingDate": to_ddmmyyyy(main_row.get("date_of_filing")),
        "RegistrationDate": to_ddmmyyyy(main_row.get("regis_date")),
        "FirstHearingDate": to_ddmmyyyy(final_status.get("listing_date")) or (
            case_history[0]["businessOnDate"] if case_history else None),
        "NextHearingDate": None if disposed else next_hearing,
        "DecisionDate": decision_date if disposed else None,
        "CaseStatus": clean(final_status.get("current_status")) or status,
        "CaseStage": clean(main_row.get("case_stage")) or status,
        "NatureofDisposal": clean(disposal.get("action_type")) if disposed else None,
        "CourtNumberandJudge": ", ".join(filter(None, [
            f"Court {court_no}" if court_no else None, bench.title() if bench else None])) or None,
        "petitioner_and_advocate": petitioner,
        "respondent_and_advocate": respondent,
        "petitioner_advocates": advocates["P"],
        "respondent_advocates": advocates["R"],
        "case_filed_by": clean(final_status.get("case_filed_by")),
        "main_case_filing_no": clean(main_row.get("main_case_fno")),
        "applications_filed": applications,
        "defective_dates": [to_ddmmyyyy(d.get("defective_date")) for d in details.get("Case_Defective_Dates") or []],
        "actsandSection": {
            "acts": "Insolvency and Bankruptcy Code, 2016" if "IB" in (clean(main_row.get("case_type_desc_cis")) or "") else "Companies Act, 2013",
            "section": None,
        },
        "case_history": case_history,
        "case_transfer": [],
        "orders": orders,
        "orders_synced_at": orders_stamp(),
    }


class CaseRequest(BaseModel):
    filing_no: Optional[str] = None
    case_type: Optional[str] = None
    case_reg_no: Optional[str] = None
    rgyear: Optional[str] = None
    state_code: Optional[str] = None
    dist_code: Optional[str] = None
    court_complex_code: Optional[str] = None
    est_code: Optional[str] = None
    refresh: int = 0

    @model_validator(mode="after")
    def check_identifiers(self):
        if self.filing_no:
            self.filing_no = re.sub(r"\D", "", self.filing_no)
            return self
        if not all([self.case_type, self.case_reg_no, self.rgyear, self.court_complex_code]):
            raise ValueError("Enter the filing number, or the bench, case type, case number and year")
        return self


@app.post("/nclt/getcaseInfo")
def fetch_nclt_case(case_data: CaseRequest):
    query = case_data.model_dump()
    if case_data.filing_no:
        ac_query = {"courtType": "nclt", "FilingNumber": case_data.filing_no}
    else:
        ac_query = {
            "courtType": "nclt",
            "court_complex_code": case_data.court_complex_code,
            "case_type": case_data.case_type,
            "rgyear": case_data.rgyear,
            "case_reg_no": case_data.case_reg_no,
        }
    existing_case = collection.find_one(ac_query)
    if existing_case and case_data.refresh == 0 and not cached_case_needs_orders(existing_case):
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    existing_case_id = existing_case["_id"] if existing_case else None
    session = open_session()
    try:
        if case_data.filing_no:
            rows = search_cases(session, {
                "wayofselection": "filingnumber",
                "i_bench_id": case_data.court_complex_code or "0",
                "filing_no": case_data.filing_no,
            })
        else:
            rows = search_cases(session, {
                "wayofselection": "casenumber",
                "i_bench_id_case_no": case_data.court_complex_code,
                "i_case_type_caseno": case_data.case_type,
                "i_case_year_caseno": case_data.rgyear,
                "case_no": case_data.case_reg_no,
            })
        main_row = next((r for r in rows if not case_data.filing_no or r.get("filing_no") == case_data.filing_no), None)
        if not main_row or not main_row.get("filing_no"):
            return JSONResponse(content={"error": "Invalid Case Details"}, status_code=404)

        filing_no = main_row["filing_no"]
        details = nclt_request(session, "GET", DETAILS_URL, params={"filing_no": filing_no, "flagIA": "false"})
        orders = build_orders(filing_no, details.get("allproceedingdtls") or [])
        result = transform_case(main_row, details, orders, query)
        result["case_reg_no"] = case_data.case_reg_no or (result["cino"].split("/")[1] if result["cino"].count("/") >= 2 else None)
        if not result["rgyear"] and result["cino"]:
            year = re.search(r"(\d{4})\s*$", result["cino"])
            result["rgyear"] = year.group(1) if year else None
        result["_id"] = save_case(result, existing_case_id)
        return JSONResponse(content=jsonable_encoder(result), status_code=200)
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


def bench_id(value):
    value = (value or "").strip()
    if not value.isdigit() or value == "0":
        raise ValueError("Please select a bench")
    return value


def search_year(value):
    value = (value or "").strip()
    if not value or value == "0":
        return "0"
    if not re.fullmatch(r"\d{4}", value):
        raise ValueError("Enter a four digit year")
    return value


class PartySearchRequest(BaseModel):
    bench_id: str
    party_name: str
    party_type: Literal["P", "R", "0"] = "0"
    year: Optional[str] = None
    status: Literal["P", "D", "0"] = "0"
    match: Literal["E", "W"] = "W"

    @field_validator("party_name")
    @classmethod
    def validate_name(cls, value):
        value = clean(value) or ""
        if len(value) < 3:
            raise ValueError("Enter at least 3 characters to search")
        return value

    @field_validator("bench_id")
    @classmethod
    def validate_bench(cls, value):
        return bench_id(value)

    @field_validator("year")
    @classmethod
    def validate_year(cls, value):
        return search_year(value)


@app.post("/nclt/bulk_q/party")
def party_search(search: PartySearchRequest):
    session = open_session()
    try:
        rows = search_cases(session, {
            "wayofselection": "partyname",
            "i_party_search": search.match,
            "i_bench_id_party": search.bench_id,
            "party_type_party": search.party_type,
            "party_name_party": search.party_name,
            "i_case_year_party": search.year,
            "status_party": search.status,
        })
        return JSONResponse(content={"data": unique_rows(rows)})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


class AdvocateSearchRequest(BaseModel):
    bench_id: str
    advocate_name: Optional[str] = None
    bar_council_no: Optional[str] = None
    year: Optional[str] = None
    match: Literal["E", "W"] = "W"

    @field_validator("bench_id")
    @classmethod
    def validate_bench(cls, value):
        return bench_id(value)

    @field_validator("year")
    @classmethod
    def validate_year(cls, value):
        return search_year(value)

    @model_validator(mode="after")
    def check_advocate(self):
        self.advocate_name = clean(self.advocate_name) or ""
        self.bar_council_no = clean(self.bar_council_no) or ""
        if len(self.advocate_name) < 3 and not self.bar_council_no:
            raise ValueError("Enter the advocate name (at least 3 characters) or bar council number")
        return self


@app.post("/nclt/bulk_q/advocate")
def advocate_search(search: AdvocateSearchRequest):
    session = open_session()
    try:
        rows = search_cases(session, {
            "wayofselection": "advocatename",
            "i_adv_search": search.match,
            "i_bench_id_lawyer": search.bench_id,
            "party_lawer_name": search.advocate_name,
            "i_case_year_lawyer": search.year,
            "bar_council_advocate": search.bar_council_no,
        })
        return JSONResponse(content={"data": unique_rows(rows)})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


class CinRequest(BaseModel):
    cin: str

    @field_validator("cin")
    @classmethod
    def validate_cin(cls, value):
        value = re.sub(r"\s+", "", value or "").upper()
        if not re.fullmatch(r"[A-Z0-9]{21}", value):
            raise ValueError("Enter a valid 21 character CIN")
        return value


def company_name(session, cin):
    body = nclt_request(session, "POST", COMPANY_URL, json={"cin": cin})
    name = clean(body.get("companyName"))
    if not name or "valid cin" in name.lower():
        return None
    return name


@app.post("/nclt/company")
def company_lookup(request: CinRequest):
    session = open_session()
    try:
        name = company_name(session, request.cin)
        if not name:
            return JSONResponse(content={"error": "No company found for this CIN"}, status_code=404)
        return JSONResponse(content={"company_name": name})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()


@app.post("/nclt/bulk_q/cin")
def cin_search(request: CinRequest):
    session = open_session()
    try:
        name = company_name(session, request.cin)
        if not name:
            return JSONResponse(content={"error": "No company found for this CIN"}, status_code=404)
        body = nclt_request(session, "POST", CIN_SEARCH_URL, json={"cin_number": request.cin})
        return JSONResponse(content={"data": unique_rows(body.get("byCinNumber") or []), "company_name": name})
    except Exception as exc:
        return error_response(exc)
    finally:
        session.close()
