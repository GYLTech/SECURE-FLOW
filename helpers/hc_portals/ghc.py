import random
import re
import time
from datetime import date, datetime, timedelta

import requests

from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_no,
    meaningful,
)

NAME = "ghc"
LABEL = "Gauhati High Court"
STATE_CODE = "6"
BENCHES = {"1": "Guwahati"}
PROVIDES_HISTORY = False

SEARCH = "https://ghcservices.assam.gov.in/casestatus"
API = "https://ghcservices.assam.gov.in/case-status"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 60)
MAX_RETRIES = 3
MAX_ORDER_PAGES = 40


def open_session():
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"})
    return session


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.2, 0.5))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[ghc] {method} {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PortalError(f"ghcservices.assam.gov.in {url} returned HTTP {response.status_code}")
        return response
    raise PortalError(f"ghcservices.assam.gov.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _json(session, url, **kwargs):
    response = _request(session, "GET", url, headers={"Accept": "application/json"}, **kwargs)
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        raise PortalError(f"ghcservices.assam.gov.in {url} did not return JSON")


def ist_date(value):
    text = clean_text(value)
    if not text or text == "None":
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else ""
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None) - moment.utcoffset() + timedelta(hours=5, minutes=30)
    return moment.strftime("%d-%m-%Y")


def find_cino(session, case_type, reg_no, year):
    form = {"casestate": "", "category": "", "type": str(int(case_type)), "reg_number": reg_no,
            "reg_year": year, "CNR": ""}
    response = _request(session, "POST", f"{SEARCH}/getrecord.php", data=form)
    if response is None:
        raise PortalError("ghcservices.assam.gov.in case search is unavailable")
    text = response.text.strip()
    if not text.startswith("["):
        raise PortalCaseNotFound(f"ghcservices.assam.gov.in has no case {case_type} {reg_no}/{year}")
    try:
        rows = response.json()
    except ValueError:
        raise PortalError("ghcservices.assam.gov.in case search returned an unexpected response")
    for row in rows:
        if (str(row.get("reg_no")) == reg_no and str(row.get("reg_year")) == year
                and meaningful(row.get("cino"))):
            return row["cino"]
    raise PortalCaseNotFound(f"ghcservices.assam.gov.in has no case {case_type} {reg_no}/{year}")


def _strip_others(name):
    return re.sub(r"\s+(AND|&)\s+\d+\s+(ORS|OTHERS)\.?$", "", clean_text(name), flags=re.I).strip()


def _advocates(block):
    if not isinstance(block, dict):
        return ""
    names = [meaningful(block.get("adv_name"))]
    names += [meaningful(a.get("adv_name")) for a in block.get("extra_advocates") or [] if isinstance(a, dict)]
    return ", ".join(n for n in names if n)


def _parties(main_name, main_advocate, extra, prefix):
    parties = []
    main = _strip_others(main_name)
    if main:
        parties.append({"name": main, "advocate": main_advocate, "relation": f"{prefix}1", "address": ""})
    for row in extra or []:
        name = meaningful(row.get("name"))
        if not name or (parties and name.upper() == parties[0]["name"].upper()):
            continue
        number = row.get("party_no")
        parties.append({
            "name": name,
            "advocate": meaningful(row.get("adv_name")),
            "relation": f"{prefix}{int(number) + 1}" if str(number or "").isdigit() else "",
            "address": meaningful(row.get("address")).rstrip(", "),
        })
    return parties


def fetch_orders(session, cino):
    rows = []
    for page in range(1, MAX_ORDER_PAGES + 1):
        data = _json(session, f"{API}/case-orders/{cino}", params={"page": page})
        block = (data or {}).get("data") or {}
        items = block.get("data") if isinstance(block, dict) else block
        rows.extend(items or [])
        if not isinstance(block, dict) or not block.get("next_page_url"):
            break
    return rows


def build_orders(rows, decision):
    found = []
    for row in rows:
        when = ist_date(row.get("order_dt"))
        kind = meaningful((row.get("document_type") or {}).get("docu_name")) or "ORDER"
        name, year = meaningful(row.get("uploaded_file_name")), meaningful(row.get("uploaded_file_year"))
        href = f"{API}/order-document/{year}/{name}/stream" if name and year and row.get("uploaded_file_exists") else ""
        final = (str(row.get("judgment") or "").upper() == "Y" or re.search(r"JUDG|FINAL", kind, re.I)
                 or (decision and when == decision))
        found.append({
            "date": when,
            "kind": kind.title(),
            "final": bool(final),
            "citation": meaningful(row.get("nc")),
            "href": href,
            "ref": f"{row.get('cino')}:{row.get('order_no')}",
        })
    found.sort(key=lambda o: dmy_key(o["date"]))
    return [{
        "order_number": str(number),
        "order_on": o["kind"],
        "judge": "",
        "order_date": o["date"],
        "order_type": "final" if o["final"] else "order",
        "neutral_citation": o["citation"],
        "href": o["href"],
        "source_ref": f"{NAME}:{o['ref']}",
    } for number, o in enumerate(found, start=1)]


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    if str(court_code or "1") != "1" or not str(case_type or "").isdigit():
        raise PortalCaseNotFound("the Gauhati High Court server covers the principal seat only")
    reg_no, year = str(int(case_reg_no)), str(rgyear)
    known = clean_text(cino).upper()
    found = known if re.fullmatch(r"GAHC01\d{10}", known) else find_cino(session, case_type, reg_no, year)

    data = _json(session, f"{API}/proxy/cases/{found}")
    case = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(case, dict) or not case.get("cino"):
        if found == known:
            found = find_cino(session, case_type, reg_no, year)
            data = _json(session, f"{API}/proxy/cases/{found}")
            case = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(case, dict) or not case.get("cino"):
            raise PortalCaseNotFound(f"ghcservices.assam.gov.in returned no details for {found}")
    if str(case.get("reg_no")) != reg_no or str(case.get("reg_year")) != year:
        raise PortalCaseNotFound(f"ghcservices.assam.gov.in returned {case.get('reg_no')}/{case.get('reg_year')} "
                                 f"instead of {reg_no}/{year}")

    state = meaningful(case.get("case_state")).lower()
    disposed = state == "disposed" or (state != "pending" and bool(ist_date(case.get("date_of_decision"))))
    decision = ist_date(case.get("date_of_decision")) if disposed else ""
    today_key = date.today().strftime("%Y%m%d")
    upcoming = ist_date(case.get("date_next_list"))
    next_date = "" if disposed or not upcoming or dmy_key(upcoming) < today_key else upcoming

    case_type_info = case.get("registered_case_type") or {}
    short = meaningful(case_type_info.get("type_name"))
    filing_type = meaningful((case.get("filing_case_type") or {}).get("type_name")) or short
    petitioners = _parties(case.get("pet_name"), _advocates(case.get("petitioner_advocate")) or
                           meaningful(case.get("pet_adv")), case.get("petitioners"), "P")
    respondents = _parties(case.get("res_name"), _advocates(case.get("respondent_advocate")) or
                           meaningful(case.get("res_adv")), case.get("respondents"), "R")

    sub_matters = []
    for path in ("case-sub", "case.link"):
        try:
            linked = (_json(session, f"{API}/{path}/{found}") or {}).get("data") or []
        except PortalError as exc:
            print(f"[ghc] {path} failed: {exc}")
            linked = []
        for row in linked if isinstance(linked, list) else []:
            label = " ".join(str(row.get(k) or "") for k in ("type_name", "reg_no", "reg_year")).strip() \
                if isinstance(row, dict) else clean_text(row)
            label = label or (meaningful(row.get("cino")) if isinstance(row, dict) else "")
            if label and label not in sub_matters:
                sub_matters.append(label)

    try:
        orders = build_orders(fetch_orders(session, found), decision)
    except PortalError as exc:
        print(f"[ghc] orders failed: {exc}")
        orders = []

    subject = meaningful((case.get("subject") or {}).get("subject_name"))
    bench = meaningful((case.get("bench") or {}).get("bench_type_name"))
    purpose = meaningful((case.get("next_purpose") or {}).get("purpose_name"))

    metadata = {
        "case_no": ecourts_case_no(case_type, reg_no, year),
        "case_number": f"{short}/{reg_no}/{year}" if short else "",
        "cino": case["cino"],
        "CaseType": short,
        "CaseTypeName": meaningful(case_type_info.get("full_form")),
        "FilingNumber": f"{filing_type}/{case.get('fil_no')}/{case.get('fil_year')}" if case.get("fil_no") else "",
        "FilingDate": ist_date(case.get("date_of_filing")),
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": ist_date(case.get("dt_regis")),
        "CNRNumber": case["cino"],
        "EFilingNumber": meaningful(case.get("efilno")),
        "FirstHearingDate": ist_date(case.get("date_first_list")),
        "NextHearingDate": next_date,
        "LastListedDate": ist_date(case.get("date_last_list")),
        "DecisionDate": decision,
        "CaseStatus": "Disposed" if disposed else (purpose.title() if purpose else "Pending"),
        "PortalStatus": state.upper(),
        "Stage": purpose,
        "NatureofDisposal": "Disposed" if disposed else "",
        "CourtNumberandJudge": "",
        "BenchType": bench,
        "BenchName": BENCHES["1"],
        "JudicialBranch": "",
        "State": "ASSAM",
        "District": "",
        "NotBeforeMe": meaningful(case.get("not_before_me")),
        "NeutralCitation": next((o["neutral_citation"] for o in reversed(orders) if o["neutral_citation"]), ""),
        "petitioner_and_advocate": [p["name"] for p in petitioners],
        "respondent_and_advocate": [p["name"] for p in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": {},
        "acts": [],
        "category_details": {"Category": subject} if subject else {},
        "subordinate_court_information": {},
        "sub_matters": sub_matters,
        "ia_details": [],
        "case_history": [],
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": case["cino"],
    }
    return metadata, orders
