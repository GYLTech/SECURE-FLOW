import base64
import html
import json
import os
import random
import re
import time
from http.client import RemoteDisconnected
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.database import collection, save_case
from core.lambda_client import lambda_client
from core.s3_client import s3_client
from helpers.orders import (
    cached_case_needs_orders,
    hc_source_ref,
    order_pdf_s3_key,
    orders_stamp,
    stable_order_doc_id,
)
from helpers.requests import safe_get, safe_post
from helpers.solve_captcha import solve_captcha

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

HC_ORIGIN = "https://hcservices.ecourts.gov.in"
HC_APP_BASE = f"{HC_ORIGIN}/ecourtindiaHC/"
HC_CASES_BASE = HC_APP_BASE + "cases/"
CASE_FORM_URL = HC_CASES_BASE + "case_no.php"
CASE_QRY_URL = HC_CASES_BASE + "case_no_qry.php"
CASE_HISTORY_URL = HC_CASES_BASE + "o_civil_case_history.php"
ADVOCATE_FORM_URL = HC_CASES_BASE + "qs_civil_advocate.php"
ADVOCATE_QRY_URL = HC_CASES_BASE + "qs_civil_advocate_qry.php"
PARTY_FORM_URL = HC_CASES_BASE + "ki_petres.php"
PARTY_QRY_URL = HC_CASES_BASE + "ki_petres_qry.php"
CAPTCHA_URL = HC_APP_BASE + "securimage/securimage_show.php?0.026039539400995126"

S3_BUCKET = "dl-shared-gyl-vidilekh"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

HC_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/x-www-form-urlencoded",
    "origin": HC_ORIGIN,
    "referer": HC_ORIGIN + "/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": USER_AGENT,
}

CASE_PAGE_MARKER = "case_details_table"

app = APIRouter()
MAX_RETRIES = 5


class HighCourtScrapeError(Exception):
    """The high court app refused to serve the case history page."""


class HighCourtCaseNotFound(HighCourtScrapeError):
    """The high court search returned no record for the requested case."""


class CaseRequest(BaseModel):
    case_type: str
    case_reg_no: str
    rgyear: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None
    refresh: int = 0


class CaseAdvocateBulk(BaseModel):
    state_code: str
    dist_code: str
    court_code: str
    advocate_name: str
    courtType: Optional[str] = None
    case_status: str


class CasePartyBulk(BaseModel):
    rgyear: str
    state_code: str
    dist_code: str
    court_code: str
    petres_name: str
    courtType: Optional[str] = None
    case_status: str


class CaseRequestBulkIngest(BaseModel):
    court_code: str
    state_code: str
    dist_code: str
    court_complex_code: str
    case_no: str
    cino: str
    rgyear: str
    courtType: Optional[str] = None
    # Optional overrides; both are normally derived from case_no.
    case_type: Optional[str] = None
    case_reg_no: Optional[str] = None
    refresh: int = 0


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


def upload_case_json_to_s3(s3_client, bucket_name, metadata):
    key = build_case_json_key(metadata)

    s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=json.dumps({**metadata}, ensure_ascii=False),
        ContentType="application/json"
    )

    return f"s3://{bucket_name}/{key}"


def clean_text(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ").strip())


PLACEHOLDERS = ("", "--", "-", "---", "null", "NA")


def status_value(case_status, *labels):
    """First non-placeholder value among the given Case Status labels."""
    for label in labels:
        value = clean_text(case_status.get(label, "") or "")
        if value and value not in PLACEHOLDERS:
            return value
    return ""


MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
ORDINAL_DATE_RE = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})$", re.IGNORECASE)


def normalize_date(value):
    """The full page prints hearing dates as "13th August 2026"."""
    value = clean_text(value)
    if not value or value in PLACEHOLDERS:
        return ""

    match = ORDINAL_DATE_RE.match(value)
    if not match:
        return value

    day, month_name, year = match.groups()
    month = MONTHS.get(month_name[:3].lower())
    if not month:
        return value

    return f"{int(day):02d}-{month:02d}-{year}"


def clean_coram(value):
    """Coram is prefixed with the internal judge code, e.g. "2193HON'BLE ..."."""
    return clean_text(re.sub(r"^\d+(?=[A-Za-z])", "", clean_text(value)))


def hc_post(session, url, data, max_retries=4):
    """POST on the *given* session so the HCSERVICES_SESSID cookie survives retries.

    helpers.requests.safe_post swaps in a brand new Session when the server
    drops the connection, which would throw away the cookie the token is bound
    to.
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(1.0, 1.8))
            return session.post(url, data=data, headers=HC_HEADERS, timeout=(30, 180))
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                RemoteDisconnected) as exc:
            last_error = exc
            print(f"[hc3] {url} attempt {attempt} failed: {type(exc).__name__}")

    raise HighCourtScrapeError(
        f"high court request to {url} failed after {max_retries} attempts: {last_error}"
    )


def hc_get(session, url, headers=None, max_retries=4):
    """GET on the given session, retrying the connection drops HC hands out.

    Order PDFs go through here: without a retry a single dropped connection
    loses the order for good, and the case is then cached with a null link.
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(1.0, 1.8))
            return session.get(url, headers=headers, timeout=(30, 180))
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                RemoteDisconnected) as exc:
            last_error = exc
            print(f"[hc3] GET {url} attempt {attempt} failed: {type(exc).__name__}")

    raise HighCourtScrapeError(
        f"high court GET {url} failed after {max_retries} attempts: {last_error}"
    )


def open_hc_session(state_code, dist_code, court_code):
    """Fresh session primed with the search form so it carries a real app cookie."""
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT})

    try:
        session.get(
            CASE_FORM_URL,
            params={
                "state_cd": state_code,
                "dist_cd": dist_code,
                "court_code": court_code,
            },
            timeout=(30, 120),
        )
    except requests.exceptions.RequestException as exc:
        print(f"[hc3] session warm-up failed ({type(exc).__name__}); continuing")

    return session


TOKEN_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def record_token(parts):
    for value in reversed(parts):
        if TOKEN_RE.match(value):
            return value
    return ""


def parse_case_records(raw_text):
    text = (raw_text or "").strip().lstrip("﻿").strip()
    if not text or text.upper().startswith("ERROR"):
        return []

    records = []
    for chunk in text.split("##"):
        chunk = chunk.strip().lstrip("﻿")
        if not chunk:
            continue

        parts = [p.strip() for p in chunk.split("~")]
        if len(parts) < 4:
            continue

        records.append({
            "case_no": parts[0],
            "case_number": clean_text(html.unescape(re.sub(r"<br\s*/?>", " ", parts[1]))),
            "party_details": clean_text(html.unescape(re.sub(r"<br\s*/?>", " ", parts[2]))),
            "cino": parts[3],
            "court_code": parts[4] if len(parts) > 4 else "",
            "token": record_token(parts),
        })

    return records


def split_hc_case_no(case_no):
    digits = re.sub(r"\D", "", case_no or "")
    if len(digits) != 15:
        return None

    return {
        "case_type": str(int(digits[1:4])),
        "case_reg_no": str(int(digits[4:11])),
        "rgyear": digits[11:15],
    }


def search_cases(session, *, state_code, dist_code, court_code,
                 case_type, case_reg_no, rgyear):
    response = hc_post(session, CASE_QRY_URL, {
        "action_code": "showRecords",
        "state_code": state_code,
        "dist_code": dist_code,
        "court_code": court_code,
        "case_type": case_type,
        "case_no": case_reg_no,
        "rgyear": rgyear,
        "caseNoType": "new",
        "displayOldCaseNo": "NO",
        "captcha": "",
    })

    return parse_case_records(response.text)


def pick_record(records, case_no, cino):
    if not records:
        return None

    for record in records:
        if cino and record["cino"] == cino:
            return record
        if case_no and record["case_no"] == case_no:
            return record

    return None if (case_no or cino) else records[0]


def load_case_history(session, *, state_code, dist_code, court_code,
                      case_type, case_reg_no, rgyear,
                      case_no="", cino="", attempts=2):
    """Mint a fresh token via search, then pull the full case history page."""
    last_error = None

    for attempt in range(1, attempts + 1):
        records = search_cases(
            session,
            state_code=state_code,
            dist_code=dist_code,
            court_code=court_code,
            case_type=case_type,
            case_reg_no=case_reg_no,
            rgyear=rgyear,
        )
        if not records:
            raise HighCourtCaseNotFound(
                f"no high court record for case type {case_type}, "
                f"number {case_reg_no}, year {rgyear}"
            )

        record = pick_record([r for r in records if r["token"]], case_no, cino)
        if not record:
            raise HighCourtCaseNotFound(
                f"high court search returned {len(records)} record(s) but none "
                f"carried a usable token for {cino or case_no}"
            )

        response = hc_post(session, CASE_HISTORY_URL, {
            "court_code": court_code,
            "state_code": state_code,
            "dist_code": dist_code,
            "case_no": record["case_no"],
            "cino": record["cino"],
            "token": record["token"],
            "appFlag": "",
        })

        page = response.text or ""
        if response.status_code == 200 and CASE_PAGE_MARKER in page:
            return page, record

        last_error = (
            f"case history rejected (status={response.status_code}, "
            f"body={clean_text(page)[:100]!r})"
        )
        print(f"[hc3] attempt {attempt}/{attempts} for {record['cino']}: {last_error}")

    raise HighCourtScrapeError(last_error or "case history request failed")


SEP = "\x1f"


def own_rows(table):
    return [row for row in table.find_all("tr") if row.find_parent("table") is table]


def row_cells(row):
    return [clean_text(cell.get_text(" ", strip=True))
            for cell in row.find_all(["td", "th"])]


def find_heading(soup, *titles):
    wanted = {title.lower() for title in titles}

    for heading in soup.find_all(["h2", "h3"]):
        text = clean_text(heading.get_text(" ", strip=True)).rstrip(":").lower()
        if text in wanted:
            return heading

    return None


def section_table(soup, *titles):
    heading = find_heading(soup, *titles)
    return heading.find_next("table") if heading else None


def label_value_pairs(node):
    pairs = {}
    key = None

    for chunk in node.get_text(SEP, strip=True).split(SEP):
        chunk = clean_text(chunk)
        if not chunk:
            continue

        if chunk.startswith(":"):
            if key:
                pairs[key] = clean_text(chunk.lstrip(":"))
        else:
            key = clean_text(chunk.rstrip(":"))
            pairs.setdefault(key, "")

    return pairs


def extract_case_details(soup):
    details = {}
    for span in soup.find_all("span", class_="case_details_table"):
        details.update(label_value_pairs(span))

    return details


def extract_case_status(soup):
    heading = find_heading(soup, "Case Status")
    if not heading:
        return {}

    block = heading.find_next("div")
    return label_value_pairs(block) if block else {}


def extract_parties(soup, class_name):
    """`1) NAME(...)` followed by one or more `Advocate- ...` lines."""
    parties = []

    for element in soup.find_all("span", class_=class_name):
        for br in element.find_all("br"):
            br.replace_with("\n")

        for line in element.get_text().split("\n"):
            line = clean_text(line)
            if not line:
                continue

            if re.match(r"^\d+\)", line):
                parties.append({
                    "name": clean_text(re.sub(r"^\d+\)\s*", "", line)),
                    "advocate": "",
                })
                continue

            advocate = re.sub(r"^advocate\s*[-:]?", "", line, flags=re.IGNORECASE)
            advocate = clean_text(advocate).strip(",").strip()
            if not advocate:
                continue

            if not parties:
                parties.append({"name": line, "advocate": ""})
            elif parties[-1]["advocate"]:
                parties[-1]["advocate"] += f", {advocate}"
            else:
                parties[-1]["advocate"] = advocate

    return parties


def extract_acts(soup):
    acts = []

    for table in soup.find_all("table", class_="Acts_table"):
        for row in own_rows(table):
            if row.find("th"):
                continue
            cells = row_cells(row)
            if len(cells) >= 2 and (cells[0] or cells[1]):
                acts.append({"act": cells[0], "section": cells[1]})

    return acts


def acts_summary(acts):
    if not acts:
        return {}

    return {
        "acts": ", ".join(act["act"] for act in acts if act["act"]),
        "section": ", ".join(act["section"] for act in acts if act["section"]),
    }


def extract_category_details(soup):
    table = section_table(soup, "Category Details", "Category")
    if not table:
        return {}

    details = {}
    for row in own_rows(table):
        cells = row_cells(row)
        if len(cells) >= 2 and cells[0]:
            details[cells[0]] = cells[1]

    return details


def extract_subordinate_court_info(soup):
    # The block closes its labels with `</br>` instead of `</label>`, so the
    # "Case Decision Date" label swallows the state/District rows. Reading the
    # flat label/value token stream sidesteps the broken nesting.
    court_info = soup.find("span", class_="Lower_court_table")
    return label_value_pairs(court_info) if court_info else {}


def extract_sub_matters(soup):
    sub_matters = []

    for table in soup.find_all("table", class_="MainCase"):
        for row in own_rows(table):
            cells = row_cells(row)
            if len(cells) >= 2 and cells[1]:
                sub_matters.append(cells[1])

    return sub_matters


def extract_ia_details(soup):
    table = section_table(soup, "IA Details")
    if not table:
        return []

    ia_details = []
    for row in own_rows(table):
        if row.find("th"):
            continue

        cells = row_cells(row)
        if len(cells) < 5:
            continue

        ia_number, _, classification = cells[0].partition("Classification :")
        ia_details.append({
            "iaNumber": clean_text(ia_number),
            "classification": clean_text(classification),
            "party": cells[1],
            "dateOfFiling": normalize_date(cells[2]),
            "nextDate": normalize_date(cells[3]),
            "status": cells[4],
        })

    return ia_details


def extract_case_conversion(soup):
    conversions = []

    for table in soup.find_all("table", class_="tbl_case_conversion"):
        for row in own_rows(table):
            if row.find("th"):
                continue

            cells = row_cells(row)
            if len(cells) >= 3 and cells[0]:
                conversions.append({
                    "oldCaseName": cells[0],
                    "newCaseName": cells[1],
                    "date": normalize_date(cells[2]),
                })

    return conversions


def extract_case_history(soup):
    history = []

    for table in soup.find_all("table", class_="history_table"):
        for row in own_rows(table):
            if row.find("th"):
                continue

            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            business_cell = cells[2].find("a") or cells[2]
            history.append({
                "causeListType": clean_text(cells[0].get_text(" ", strip=True)),
                "judge": clean_text(cells[1].get_text(" ", strip=True)),
                "businessOnDate": clean_text(business_cell.get_text(" ", strip=True)),
                "hearingDate": clean_text(cells[3].get_text(" ", strip=True)),
                "purpose": clean_text(cells[4].get_text(" ", strip=True)),
                "inputType": "automatic",
                "lawyerRemark": None,
            })

    return history


def extract_objections(soup):
    table = section_table(soup, "OBJECTION", "OBJECTIONS")
    if not table:
        return []

    objections = []
    for row in own_rows(table):
        cells = row_cells(row)
        if len(cells) < 5 or cells[0].replace(".", "").lower().startswith("sr"):
            continue

        objections.append({
            "srNo": cells[0],
            "scrutinyDate": normalize_date(cells[1]),
            "objection": cells[2],
            "complianceDate": normalize_date(cells[3]),
            "receiptDate": normalize_date(cells[4]),
        })

    return objections


def extract_document_details(soup):
    table = section_table(soup, "Document Details")
    if not table:
        return []

    documents = []
    for row in own_rows(table):
        if row.find("th"):
            continue

        cells = row_cells(row)
        if len(cells) < 6:
            continue

        documents.append({
            "srNo": cells[0],
            "documentNo": cells[1],
            "dateOfReceiving": normalize_date(cells[2]),
            "filedBy": cells[3],
            "advocate": cells[4],
            "documentFiled": cells[5],
        })

    return documents


def order_section_type(table):
    """Which order table this is, from the heading printed above it.

    Interim orders and the final judgement are listed in separate, otherwise
    identical tables, so the heading is the only thing telling them apart.
    """
    heading = table.find_previous(["h2", "h3", "h4"]) if table else None
    text = clean_text(heading.get_text(" ", strip=True)).lower() if heading else ""

    if "final" in text or "judgement" in text or "judgment" in text:
        return "final"
    if "interim" in text:
        return "interim"
    return "order"


def order_row(cells, href, order_type="order"):
    return {
        "order_number": cells[0] if len(cells) > 0 else "",
        "order_on": cells[1] if len(cells) > 1 else "",
        "judge": cells[2] if len(cells) > 2 else "",
        "order_date": normalize_date(cells[3]) if len(cells) > 3 else "",
        "order_type": order_type,
        "href": href,
    }


def extract_orders(soup):
    """Published orders are anchored by a display_pdf.php link.

    The orders table is located from the first such anchor, then walked in
    full: a row the Court has listed without publishing the PDF has no anchor,
    and is still returned (with no href) so it reaches the case file flagged as
    not uploaded rather than vanishing.
    """
    anchors = [
        link for link in soup.find_all("a", href=True)
        if "display_pdf.php" in link["href"]
    ]

    if not anchors:
        return []

    # District courts split interim orders and the final judgement across two
    # otherwise identical tables. High court benches render a single "Orders"
    # table today, but collect every table that holds a PDF link so a split
    # listing cannot silently drop the final order here either.
    tables = []
    for link in anchors:
        table = link.find_parent("table")
        if table is not None and not any(table is seen for seen in tables):
            tables.append(table)

    if not tables:
        return [
            order_row(row_cells(link.find_parent("tr")), link["href"].strip())
            for link in anchors
            if link.find_parent("tr")
        ]

    orders = []
    rows = [(table, row) for table in tables for row in own_rows(table)]

    for table, row in rows:
        if row.find("th"):
            continue

        cells = row_cells(row)
        if not any(cells):
            continue

        link = next(
            (a for a in row.find_all("a", href=True)
             if "display_pdf.php" in a["href"]),
            None,
        )

        # A data row either links its PDF or carries a real order date. This
        # table renders its header with <td> instead of <th>, so it survives the
        # check above and has to be dropped here or every case picks up a bogus
        # "Order Number / Order Date" row.
        if not link and not re.search(r"\d", cells[3] if len(cells) > 3 else ""):
            continue

        orders.append(order_row(
            cells,
            link["href"].strip() if link else "",
            order_section_type(table),
        ))

    return orders


PDF_HEADERS = {
    "accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    "referer": HC_CASES_BASE,
    "user-agent": USER_AGENT,
}


def download_order_pdf(session, pdf_url):
    """Fetch an order PDF; returns (bytes, status).

    Status uses the district court vocabulary so both courts read the same in
    the case file: `not_uploaded` when the court says the order is not there,
    `unavailable` when the fetch itself failed.
    """
    try:
        response = hc_get(session, pdf_url, headers=PDF_HEADERS)
    except HighCourtScrapeError as exc:
        print(f"[hc/orders] PDF fetch failed for {pdf_url}: {exc}")
        return None, "unavailable"

    if response.status_code != 200:
        print(f"[hc/orders] PDF {pdf_url} returned HTTP {response.status_code}")
        return None, "unavailable"

    body = response.content
    # display_pdf.php leaks a UTF-8 BOM ahead of the %PDF header.
    start = body.find(b"%PDF", 0, 1024)
    if start >= 0:
        return body[start:], "available"

    # An unpublished order answers HTTP 200 with
    # "Orders is not uploaded for case number ..." instead of a PDF.
    if "not uploaded" in body[:500].decode("utf-8", "ignore").lower():
        return None, "not_uploaded"

    print(
        f"[hc/orders] {pdf_url} did not return a PDF "
        f"({len(body)} bytes, starts {body[:40]!r})"
    )
    return None, "unavailable"


def store_orders(orders, session, metadata):
    orders_prefix = build_orders_prefix(metadata)
    seen_doc_ids = set()
    stored = []

    for order in orders:
        href = order.pop("href", "")

        if not href:
            order["order_link"] = None
            order["order_status"] = "not_uploaded"
            stored.append(order)
            continue

        source_ref = hc_source_ref(href)
        doc_id = stable_order_doc_id(source_ref)
        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)

        s3_key = order_pdf_s3_key(orders_prefix, order.get("order_date"), source_ref)
        s3_url = f"https://{S3_BUCKET}.s3.{REGION_NAME}.amazonaws.com/{s3_key}"
        status = "available"

        try:
            try:
                s3_client.head_object(Bucket=S3_BUCKET, Key=s3_key)
            except s3_client.exceptions.ClientError as e:
                if e.response["Error"]["Code"] != "404":
                    raise
                body, status = download_order_pdf(
                    session, urljoin(HC_CASES_BASE, href))
                if body is None:
                    s3_url = None
                else:
                    s3_client.put_object(
                        Bucket=S3_BUCKET,
                        Key=s3_key,
                        Body=body,
                        ContentType="application/pdf",
                        ContentDisposition="inline",
                    )
                    print(f"[hc/orders] stored {s3_key} ({len(body)} bytes)")
        except Exception as exc:
            print(f"[hc/orders] S3 store failed for {s3_key}: {exc}")
            s3_url, status = None, "unavailable"

        order["order_link"] = s3_url
        order["order_status"] = status
        stored.append(order)

    print(
        f"[hc/orders] rows={len(orders)} kept={len(stored)} "
        f"withLink={sum(1 for o in stored if o.get('order_link'))}"
    )
    return stored


def parse_case_history(page, context, session):
    soup = BeautifulSoup(page, "html.parser")

    case_details = extract_case_details(soup)
    case_status = extract_case_status(soup)
    acts = extract_acts(soup)
    petitioners = extract_parties(soup, "Petitioner_Advocate_table")
    respondents = extract_parties(soup, "Respondent_Advocate_table")

    registration_number = case_details.get("Registration Number", "")
    rgyear = (
        context.get("rgyear")
        or (registration_number.split("/")[-1] if "/" in registration_number else "")
        or "unknown"
    )

    metadata = {
        "case_no": context.get("case_no"),
        "courtType": "highcourt",
        "case_reg_no": context.get("case_reg_no"),
        "rgyear": rgyear,
        "cino": context.get("cino"),
        "court_code": context.get("court_code"),
        "state_code": context.get("state_code"),
        "dist_code": context.get("dist_code"),
        "court_complex_code": context.get("court_complex_code"),
        "est_code": context.get("est_code"),
        "case_type": context.get("case_type"),
        "case_number": context.get("case_number", ""),
        "CaseType": case_details.get("Case Type", ""),
        "FilingNumber": case_details.get("Filing Number", ""),
        "FilingDate": normalize_date(case_details.get("Filing Date", "")),
        "RegistrationNumber": registration_number,
        "RegistrationDate": normalize_date(case_details.get("Registration Date", "")),
        "CNRNumber": case_details.get("CNR Number", "") or context.get("cino"),
        "FirstHearingDate": normalize_date(
            status_value(case_status, "First Hearing Date")),
        "NextHearingDate": normalize_date(status_value(
            case_status, "Next Hearing Date", "Next Date", "Tentative Date")),
        "DecisionDate": normalize_date(status_value(case_status, "Decision Date")),
        "CaseStatus": status_value(case_status, "Stage of Case", "Case Status"),
        "NatureofDisposal": status_value(case_status, "Nature of Disposal"),
        "CourtNumberandJudge": clean_coram(status_value(case_status, "Coram")),
        "BenchType": status_value(case_status, "Bench", "Bench Type"),
        "JudicialBranch": status_value(case_status, "Judicial", "Judicial Branch"),
        "State": status_value(case_status, "State"),
        "District": status_value(case_status, "District"),
        "NotBeforeMe": status_value(case_status, "Not Before Me"),
        "petitioner_and_advocate": [party["name"] for party in petitioners],
        "respondent_and_advocate": [party["name"] for party in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": acts_summary(acts),
        "acts": acts,
        "category_details": extract_category_details(soup),
        "subordinate_court_information": extract_subordinate_court_info(soup),
        "sub_matters": extract_sub_matters(soup),
        "ia_details": extract_ia_details(soup),
        "case_history": extract_case_history(soup),
        "case_conversion": extract_case_conversion(soup),
        "case_transfer": [],
        "objections": extract_objections(soup),
        "document_details": extract_document_details(soup),
        "orders": [],
    }

    metadata["orders"] = store_orders(extract_orders(soup), session, metadata)
    metadata["orders_synced_at"] = orders_stamp()
    metadata["s3_prefix"] = upload_case_json_to_s3(
        s3_client, S3_BUCKET, metadata=metadata)

    return metadata


def extract_case_data(case_data, raw_text):
    results = []

    for record in parse_case_records(raw_text):
        case_number = record["case_number"]
        results.append({
            "case_no": record["case_no"],
            "case_number": case_number,
            "cino": record["cino"],
            "state_code": case_data.state_code,
            "dist_code": case_data.dist_code,
            "court_code": record["court_code"] or case_data.court_code,
            "courtType": case_data.courtType,
            "rgyear": case_number.split("/")[-1] if "/" in case_number else None,
            "party_details": record["party_details"],
        })

    return results


BULK_ERRORS = {
    "error1": "Invalid Captcha",
    "invalid captcha": "Invalid Captcha",
    "error3": "Year must be numeric and 4 digits",
    "error4": "Please select one value",
    "error7": "Invalid name",
    "error8": "Invalid search type",
    "error9": "Invalid bar state",
    "error10": "Invalid bar code",
    "error11": "Invalid date",
    "errordatalimit": "Access limit exceeded, please try again later",
}

RETRYABLE_BULK_ERRORS = {"error1", "invalid captcha"}


def classify_bulk_response(raw_text):
    """("ok" | "retry" | "empty" | "error", message) for a bulk query body."""
    text = (raw_text or "").strip().lstrip("﻿").strip()
    if not text:
        return "empty", "Record Not Found"

    key = text.lower()
    if key in RETRYABLE_BULK_ERRORS:
        return "retry", BULK_ERRORS[key]
    if key.startswith("error"):
        return "error", BULK_ERRORS.get(key, "The high court site rejected the search")

    return "ok", ""


CSRF_TOKEN_RE = re.compile(r'csrfMagicToken\s*=\s*"([^"]+)"')


def open_bulk_session(form_url, state_code, dist_code, court_code, fallback_csrf):
    """Warm a session on the search form so the captcha binds to a real cookie.

    csrf-magic mints its token per session, so it has to be read off the form
    rather than hardcoded.
    """
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT})
    csrf = fallback_csrf

    try:
        page = session.get(
            form_url,
            params={
                "state_cd": state_code,
                "dist_cd": dist_code,
                "court_code": court_code,
            },
            timeout=(30, 120),
        ).text
        match = CSRF_TOKEN_RE.search(page)
        if match:
            csrf = match.group(1)
    except requests.exceptions.RequestException as exc:
        print(f"[hc3] bulk session warm-up failed ({type(exc).__name__}); continuing")

    return session, csrf


@app.post("/hc3/getcaseInfo")
def fetch_submit_hc_info(case_data: CaseRequest):
    query = case_data.dict()
    ac_query = {
        "case_reg_no": query.get("case_reg_no"),
        "rgyear": query.get("rgyear"),
        "case_type": query.get("case_type"),
        "state_code": query.get("state_code"),
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
    dist_code = case_data.dist_code or "1"
    session = open_hc_session(
        case_data.state_code, dist_code, case_data.court_complex_code)

    try:
        page, record = load_case_history(
            session,
            state_code=case_data.state_code,
            dist_code=dist_code,
            court_code=case_data.court_complex_code,
            case_type=case_data.case_type,
            case_reg_no=case_data.case_reg_no,
            rgyear=case_data.rgyear,
        )

        context = {
            "case_no": record["case_no"],
            "cino": record["cino"],
            "case_number": record["case_number"],
            "case_reg_no": case_data.case_reg_no,
            "rgyear": case_data.rgyear,
            "case_type": case_data.case_type,
            "court_code": case_data.court_complex_code,
            "state_code": case_data.state_code,
            "dist_code": dist_code,
            "court_complex_code": case_data.court_complex_code,
            "est_code": case_data.est_code,
        }

        result = parse_case_history(page, context, session=session)
        result["_id"] = save_case(result, existing_case_id)
        return JSONResponse(content=result, status_code=200)

    except HighCourtCaseNotFound as e:
        return JSONResponse(
            content={"data": "Invalid Case Details", "error": str(e)},
            status_code=404
        )
    except HighCourtScrapeError as e:
        return JSONResponse(content={"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        session.close()


@app.post("/hc3/bulk_q/advname")
def fetch_submit_adv_info(case_data: CaseAdvocateBulk):
    session, csrf = open_bulk_session(
        ADVOCATE_FORM_URL,
        case_data.state_code,
        case_data.dist_code,
        case_data.court_code,
        "sid:7567188e555e8f8123a017fcb8690f2099cd60c9",
    )
    last_error = "Unable to get response from HC at this moment"

    try:
        for attempt in range(1, MAX_RETRIES + 1):
            captcha_response = safe_get(session=session, url=CAPTCHA_URL)
            image_base64 = base64.b64encode(captcha_response.content).decode("utf-8")
            expression = solve_captcha(
                lambda_client=lambda_client, image_base64=image_base64, frm="hc")
            if not expression:
                last_error = "Could not read the high court captcha"
                continue

            payload = {
                "__csrf_magic": csrf,
                "party_type": "any",
                "action_code": "showRecords",
                "state_code": case_data.state_code,
                "dist_code": case_data.dist_code,
                "court_code": case_data.court_code,
                "advocate_name": case_data.advocate_name,
                "search_type": "1",
                "f": case_data.case_status,
                "captcha": str(expression),
            }

            response = safe_post(
                session, url=ADVOCATE_QRY_URL, data=payload, headers=HC_HEADERS)
            if response.status_code == 403:
                last_error = "The high court site refused the request"
                continue
            if response.status_code != 200:
                return JSONResponse(
                    content={"error": f"Upstream returned {response.status_code}"},
                    status_code=502
                )

            state, message = classify_bulk_response(response.text)
            if state == "retry":
                last_error = message
                print(f"[hc3] advname attempt {attempt}/{MAX_RETRIES}: {message}")
                continue
            if state == "error":
                return JSONResponse(content={"error": message}, status_code=400)
            if state == "empty":
                return JSONResponse(content={"data": []}, status_code=200)

            return JSONResponse(
                content={"data": extract_case_data(case_data, response.text)},
                status_code=200
            )

        return JSONResponse(content={"error": last_error}, status_code=502)

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

    finally:
        session.close()


@app.post("/hc3/bulk_q/partyname")
def fetch_submit_party_info(case_data: CasePartyBulk):
    session, csrf = open_bulk_session(
        PARTY_FORM_URL,
        case_data.state_code,
        case_data.dist_code,
        case_data.court_code,
        "sid:23d31510c4c2834412b00b753ea0836fcf4f1ca8",
    )
    last_error = "Unable to get response from Ecourts at this moment"

    try:
        for attempt in range(1, MAX_RETRIES + 1):
            captcha_response = safe_get(session=session, url=CAPTCHA_URL)
            image_base64 = base64.b64encode(captcha_response.content).decode("utf-8")
            expression = solve_captcha(
                lambda_client=lambda_client, image_base64=image_base64, frm="hc")
            if not expression:
                last_error = "Could not read the high court captcha"
                continue

            payload = {
                "__csrf_magic": csrf,
                "action_code": "showRecords",
                "rgyear": case_data.rgyear,
                "state_code": case_data.state_code,
                "dist_code": case_data.dist_code,
                "court_code": case_data.court_code,
                "petres_name": case_data.petres_name,
                "f": case_data.case_status,
                "captcha": str(expression),
            }

            response = safe_post(
                session, url=PARTY_QRY_URL, data=payload, headers=HC_HEADERS)
            if response.status_code == 403:
                last_error = "The high court site refused the request"
                continue
            if response.status_code != 200:
                return JSONResponse(
                    content={"error": f"Upstream returned {response.status_code}"},
                    status_code=502
                )

            state, message = classify_bulk_response(response.text)
            if state == "retry":
                last_error = message
                print(f"[hc3] partyname attempt {attempt}/{MAX_RETRIES}: {message}")
                continue
            if state == "error":
                return JSONResponse(content={"error": message}, status_code=400)
            if state == "empty":
                return JSONResponse(content={"data": []}, status_code=200)

            return JSONResponse(
                content={"data": extract_case_data(case_data, response.text)},
                status_code=200
            )

        return JSONResponse(content={"error": last_error}, status_code=502)

    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

    finally:
        session.close()


@app.post("/hc3/bulk_i")
def fetch_submit_hc_bulk_ingest(case_data: CaseRequestBulkIngest):
    ac_query = {
        "courtType": "highcourt",
        "cino": case_data.cino,
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

    derived = split_hc_case_no(case_data.case_no) or {}
    case_type = case_data.case_type or derived.get("case_type")
    case_reg_no = case_data.case_reg_no or derived.get("case_reg_no")
    rgyear = derived.get("rgyear") or case_data.rgyear

    if not (case_type and case_reg_no and rgyear):
        return JSONResponse(
            content={"error": f"cannot derive case type/number from case_no "
                              f"{case_data.case_no!r}; pass case_type and case_reg_no"},
            status_code=400
        )

    court_code = case_data.court_complex_code or case_data.court_code
    dist_code = case_data.dist_code or "1"
    session = open_hc_session(case_data.state_code, dist_code, court_code)

    try:
        page, record = load_case_history(
            session,
            state_code=case_data.state_code,
            dist_code=dist_code,
            court_code=court_code,
            case_type=case_type,
            case_reg_no=case_reg_no,
            rgyear=rgyear,
            case_no=case_data.case_no,
            cino=case_data.cino,
        )

        context = {
            "case_no": record["case_no"],
            "cino": record["cino"],
            "case_number": record["case_number"],
            "case_reg_no": case_reg_no,
            "rgyear": rgyear,
            "case_type": case_type,
            "court_code": court_code,
            "state_code": case_data.state_code,
            "dist_code": dist_code,
            "court_complex_code": court_code,
            "est_code": None,
        }

        result = parse_case_history(page, context, session=session)

        print(
            f"[hc3/bulk_i] cino={case_data.cino} "
            f"history_rows={len(result.get('case_history') or [])} "
            f"orders={len(result.get('orders') or [])} "
            f"reg_no={result.get('RegistrationNumber')!r} "
            f"next_hearing={result.get('NextHearingDate')!r}"
        )

        if not any((
            result.get("case_history"),
            result.get("RegistrationNumber"),
            result.get("FilingNumber"),
            result.get("orders"),
        )):
            return JSONResponse(
                content={"error": "HC services returned a page with no case data"},
                status_code=502
            )

        result["_id"] = save_case(result, existing_case_id)
        return JSONResponse(content=result, status_code=200)

    except HighCourtCaseNotFound as e:
        return JSONResponse(content={"error": str(e)}, status_code=404)
    except HighCourtScrapeError as e:
        return JSONResponse(content={"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        session.close()
