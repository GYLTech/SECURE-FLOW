import html
import random
import re
import time

import requests

from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    ecourts_case_type_name,
    match_portal_case_type,
    meaningful,
)

NAME = "dhc"
LABEL = "Delhi High Court"
STATE_CODE = "26"
BENCHES = {"1": "New Delhi"}
PROVIDES_HISTORY = False

ORIGIN = "https://delhihighcourt.nic.in"
FORM_URL = ORIGIN + "/app/get-case-type-status"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 60)
MAX_RETRIES = 3
TYPES_TTL_SECONDS = 6 * 3600
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "Referer": FORM_URL}

OPTION_RE = re.compile(r'<option value="([^"]+)"[^>]*>')
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
LINK_RE = re.compile(r"href=['\"]?(https://delhihighcourt\.nic\.in/app/[^'\" >]+)")

_types_cache = {"values": [], "at": 0.0}


def normalize_date(value):
    match = DATE_RE.search(value or "")
    return "-".join(match.groups()) if match else ""


def open_session():
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT})
    return session


def _request(session, url, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.3, 0.8))
        try:
            response = session.get(url, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[dhc] GET {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise PortalError(f"delhihighcourt.nic.in {url} returned HTTP {response.status_code}")
        return response
    raise PortalError(f"delhihighcourt.nic.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _json(session, url, params):
    response = _request(session, url, params=params, headers=XHR)
    try:
        return response.json()
    except ValueError:
        raise PortalError(f"delhihighcourt.nic.in {url} did not return JSON")


def portal_case_types(session):
    if _types_cache["values"] and time.time() - _types_cache["at"] < TYPES_TTL_SECONDS:
        return _types_cache["values"]

    page = _request(session, FORM_URL).text
    start = page.find('id="case_type"')
    end = page.find("</select>", start)
    values = [html.unescape(v) for v in OPTION_RE.findall(page[start:end]) if v]
    if not values:
        raise PortalError("delhihighcourt.nic.in case type list could not be read")

    _types_cache.update(values=values, at=time.time())
    return values


def resolve_case_type(session, case_type):
    types = portal_case_types(session)
    raw = str(case_type or "").strip()
    if raw in types:
        return raw

    name = ecourts_case_type_name(STATE_CODE, "1", raw) if raw.isdigit() else raw
    return match_portal_case_type(name, types) if name else ""


def _lines(fragment):
    text = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return [clean_text(line) for line in text.split("\n") if clean_text(line)]


def _labelled(lines, label):
    for line in lines:
        if line.upper().startswith(label.upper()):
            return meaningful(line.split(":", 1)[1] if ":" in line else "")
    return ""


def parse_row(row):
    ctype_lines = _lines(row.get("ctype"))
    pet_lines = _lines(row.get("pet"))
    date_lines = _lines(row.get("orderdate"))

    petitioner = pet_lines[0] if pet_lines else ""
    respondent = html.unescape(clean_text(row.get("res") or ""))
    pet_adv = meaningful(row.get("pet_adv"))
    res_adv = meaningful(row.get("res_adv"))

    links = LINK_RE.findall(row.get("ctype") or "")
    return {
        "heading": ctype_lines[0] if ctype_lines else "",
        "disposed": str(row.get("status") or "").upper() == "D",
        "status_code": str(row.get("status") or ""),
        "next_date": normalize_date(_labelled(date_lines, "NEXT DATE")),
        "last_date": normalize_date(_labelled(date_lines, "Last Date")) or normalize_date(row.get("old_h_dt")),
        "court_no": _labelled(date_lines, "COURT NO") or meaningful(row.get("courtno")),
        "petitioner": petitioner,
        "respondent": respondent,
        "pet_adv": pet_adv,
        "res_adv": res_adv,
        "diary": f"{row.get('diary_no')}/{row.get('diary_yr')}" if meaningful(row.get("diary_no")) else "",
        "category_code": meaningful(row.get("catcode")),
        "orders_url": next((u for u in links if "case-type-status-details" in u), ""),
        "judgments_url": next((u for u in links if "case-type-status-judgment" in u), ""),
    }


def fetch_orders(session, orders_url):
    if not orders_url:
        return []
    data = _json(session, orders_url, {"draw": 1, "start": 0, "length": -1})
    found = []
    for row in data.get("data") or []:
        link = re.search(r"href=['\"]?([^'\" >]+)", row.get("case_no_order_link") or "")
        date = normalize_date(row.get("orddate"))
        found.append({
            "date": date,
            "href": link.group(1) if link else "",
            "pdf": meaningful(row.get("pdffilename")),
            "kind": str(row.get("order_judgement_status") or "O").upper(),
            "remark": meaningful(row.get("remark")) if row.get("remark") != "None" else "",
        })

    def sort_key(item):
        day, month, year = (item["date"] or "00-00-0000").split("-")
        return f"{year}{month}{day}"

    found.sort(key=sort_key)
    orders = []
    for number, item in enumerate(found, start=1):
        orders.append({
            "order_number": str(number),
            "order_on": "Judgment" if item["kind"] == "J" else "Order",
            "judge": "",
            "order_date": item["date"],
            "order_type": "final" if item["kind"] == "J" else "order",
            "href": item["href"],
            "source_ref": f"{NAME}:{item['pdf'] or item['href']}",
        })
    return orders


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    portal_type = resolve_case_type(session, case_type)
    if not portal_type:
        raise PortalCaseNotFound(f"no Delhi High Court case type matches eCourts type {case_type!r}")

    data = _json(session, FORM_URL, {
        "case_type": portal_type,
        "case_number": str(int(case_reg_no)),
        "case_year": str(rgyear),
        "draw": 1,
        "start": 0,
        "length": 50,
    })

    wanted = (str(int(case_reg_no)), str(rgyear))
    row = next((r for r in data.get("data") or []
                if (str(r.get("cno") or "").strip(), str(r.get("cyear") or "").strip()) == wanted), None)
    if row is None:
        raise PortalCaseNotFound(
            f"delhihighcourt.nic.in has no {portal_type} {case_reg_no}/{rgyear}")

    details = parse_row(row)
    try:
        orders = fetch_orders(session, details["orders_url"])
    except PortalError as exc:
        print(f"[dhc] orders for {portal_type} {case_reg_no}/{rgyear} failed: {exc}")
        orders = []

    disposed = details["disposed"]
    reg_no, year = wanted
    first_order = orders[0]["order_date"] if orders else ""

    metadata = {
        "case_number": f"{portal_type}/{reg_no}/{year}",
        "cino": "",
        "CaseType": portal_type,
        "FilingNumber": details["diary"],
        "FilingDate": "",
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": "",
        "CNRNumber": "",
        "FirstHearingDate": first_order,
        "NextHearingDate": "" if disposed else details["next_date"],
        "LastListedDate": details["last_date"],
        "DecisionDate": details["last_date"] if disposed else "",
        "CaseStatus": "Disposed" if disposed else "Pending",
        "PortalStatus": "Disposed" if disposed else "Pending",
        "NatureofDisposal": "Disposed" if disposed else "",
        "CourtNumberandJudge": f"Court No. {details['court_no']}" if details["court_no"] else "",
        "BenchType": "",
        "BenchName": BENCHES["1"],
        "JudicialBranch": "",
        "State": "DELHI",
        "District": "",
        "NotBeforeMe": "",
        "CategoryCode": details["category_code"],
        "petitioner_and_advocate": [details["petitioner"]] if details["petitioner"] else [],
        "respondent_and_advocate": [details["respondent"]] if details["respondent"] else [],
        "petitioner_details": [{"name": details["petitioner"], "advocate": details["pet_adv"]}]
        if details["petitioner"] else [],
        "respondent_details": [{"name": details["respondent"], "advocate": details["res_adv"]}]
        if details["respondent"] else [],
        "actsandSection": {},
        "acts": [],
        "category_details": {},
        "subordinate_court_information": {},
        "sub_matters": [],
        "ia_details": [],
        "case_history": [],
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": f"{portal_type}|{reg_no}|{year}",
    }
    return metadata, orders
