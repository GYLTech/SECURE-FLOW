import random
import re
import time
from datetime import date

import requests

from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_type_name,
    iso_to_dmy,
    match_portal_case_type,
    meaningful,
)

NAME = "phhc"
LABEL = "Punjab and Haryana High Court"
STATE_CODE = "22"
BENCHES = {"1": "Chandigarh"}
PROVIDES_HISTORY = True

API = "https://livedb9010.phhc.gov.in"
SITE = "https://new.phhc.gov.in"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 60)
MAX_RETRIES = 3
TYPES_TTL_SECONDS = 6 * 3600

DISPOSAL_WORDS = re.compile(
    r"DISPOS|DISMISS|ALLOW|WITHDRAW|REJECT|DECIDED|ABATE|QUASH|RETURN|CLOSED|ACQUIT|CONVICT|SETTLED|DECREED",
    re.I,
)
PENDING_WORDS = re.compile(r"PENDING|INTERIM|ADMIT|NOTICE", re.I)

_types_cache = {"values": [], "at": 0.0}


def open_session():
    session = requests.Session()
    session.headers.update({
        "user-agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": SITE,
        "Referer": SITE + "/",
    })
    return session


def _get(session, path, params=None, allow_404=False):
    url = path if path.startswith("http") else API + path
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.2, 0.6))
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[phhc] GET {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise PortalError(f"phhc.gov.in {url} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise PortalError(f"phhc.gov.in {url} did not return JSON")
    raise PortalError(f"phhc.gov.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return inner
        return [data] if data else []
    return []


def portal_case_types(session):
    if _types_cache["values"] and time.time() - _types_cache["at"] < TYPES_TTL_SECONDS:
        return _types_cache["values"]
    values = [clean_text(t.get("name")) for t in _as_list(_get(session, "/masters-casetype/public/list"))
              if clean_text(t.get("name"))]
    if not values:
        raise PortalError("phhc.gov.in case type list could not be read")
    _types_cache.update(values=values, at=time.time())
    return values


def resolve_case_type(session, case_type):
    types = portal_case_types(session)
    raw = clean_text(str(case_type or ""))
    if raw.upper() in (t.upper() for t in types):
        return raw.upper()
    name = ecourts_case_type_name(STATE_CODE, "1", raw) if raw.isdigit() else raw
    return match_portal_case_type(name, types) if name else ""


def _name(value):
    return clean_text(value) if meaningful(value) and str(value) != "None" else ""


def build_history(listings, next_date):
    rows = {}
    for item in listings:
        when = iso_to_dmy(item.get("cl_date"))
        if not when:
            continue
        bench = (item.get("benchDetails") or {}).get("bench_name") or item.get("distribution_bench_name") or ""
        candidate = {
            "date": when,
            "judge": clean_text(bench),
            "list_type": clean_text(item.get("cl_type")),
            "item": item.get("sr_no"),
            "heard": str(item.get("hearing_status") or "").upper() == "Y",
        }
        current = rows.get(when)
        if current is None or (candidate["heard"] and not current["heard"]):
            rows[when] = candidate

    dates = sorted(rows, key=dmy_key)
    history = []
    for index, when in enumerate(dates):
        row = rows[when]
        if index + 1 < len(dates):
            hearing = dates[index + 1]
        elif next_date and dmy_key(next_date) > dmy_key(when):
            hearing = next_date
        else:
            hearing = ""
        history.append({
            "causeListType": row["list_type"],
            "judge": row["judge"],
            "businessOnDate": when,
            "hearingDate": hearing,
            "purpose": f"Item {row['item']}" if row["item"] else "",
            "action": "Heard" if row["heard"] else "",
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    return history


def build_orders(judgments):
    found = []
    for item in judgments:
        link = item.get("order") or ""
        if not link:
            continue
        kind = str(item.get("order_type") or "").upper()
        found.append({
            "date": iso_to_dmy(item.get("orderdate")),
            "kind": kind,
            "judge": clean_text(item.get("bench_name")),
            "href": link,
            "ref": f"{item.get('id')}:{item.get('pdfname')}",
        })
    found.sort(key=lambda o: dmy_key(o["date"]))
    return [{
        "order_number": str(number),
        "order_on": "Final Order" if o["kind"] == "F" else "Interim Order",
        "judge": o["judge"],
        "order_date": o["date"],
        "order_type": "final" if o["kind"] == "F" else "order",
        "href": o["href"],
        "source_ref": f"{NAME}:{o['ref']}",
    } for number, o in enumerate(found, start=1)]


def build_parties(main_name, main_advocate, extra):
    parties = []
    if main_name:
        parties.append({"name": re.sub(r"\s+AND\s+ORS\.?$", "", main_name, flags=re.I).strip(),
                        "advocate": main_advocate, "relation": "", "address": ""})
    for item in extra or []:
        name = _name(item.get("partyname"))
        if not name or (parties and name.upper() == parties[0]["name"].upper()):
            continue
        relation = " ".join(p for p in (_name(item.get("party_relation")), _name(item.get("prfhname"))) if p)
        advocates = ", ".join(_name(a.get("adv_name") or a.get("name")) for a in item.get("advocates") or []
                              if isinstance(a, dict) and _name(a.get("adv_name") or a.get("name")))
        parties.append({"name": name, "advocate": advocates, "relation": relation,
                        "address": _name(item.get("address"))})
    return parties


def _optional(session, path, params, label):
    try:
        return _as_list(_get(session, path, params=params, allow_404=True))
    except PortalError as exc:
        print(f"[phhc] {label} failed: {exc}")
        return []


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    portal_type = resolve_case_type(session, case_type)
    if not portal_type:
        raise PortalCaseNotFound(f"no Punjab and Haryana High Court case type matches eCourts type {case_type!r}")

    reg_no, year = str(int(case_reg_no)), str(rgyear)
    params = {"case_no": reg_no, "case_type": portal_type, "case_year": year}
    case = _get(session, "/public/getCaseDetailsOptimized", params=params, allow_404=True)
    if not case or not isinstance(case, dict) or not case.get("case_no"):
        raise PortalCaseNotFound(f"phhc.gov.in has no {portal_type} {reg_no}/{year}")

    listings = _optional(session, "/case_listing_detail/public/search",
                         {"case_no": reg_no, "case_year": year, "case_type": portal_type}, "listing")
    judgments = _optional(session, f"/cis_filing/public/judgmentDetails/{reg_no}/{year}/{portal_type}",
                          {"skip": 0, "limit": 1000}, "judgments")
    related = _optional(session, "/cis_filing/public/relatedCases",
                        {"case_type": portal_type, "case_no": reg_no, "case_year": year, "limit": 1000},
                        "related cases")

    status = case.get("status") or {}
    status_desc = clean_text(status.get("status_desc"))
    status_reason = _name(case.get("case_status2"))
    disposed = bool(DISPOSAL_WORDS.search(status_desc)) and not PENDING_WORDS.search(status_desc)

    orders = build_orders(judgments)
    final_dates = [o["order_date"] for o in orders if o["order_type"] == "final" and o["order_date"]]

    today = date.today().strftime("%d-%m-%Y")
    upcoming = [iso_to_dmy(case.get(k)) for k in
                ("listing_or_proposal_date", "proposed_cause_list_date", "cause_list_date")]
    upcoming = [d for d in upcoming if d and dmy_key(d) >= dmy_key(today)]
    next_date = "" if disposed else (min(upcoming, key=dmy_key) if upcoming else "")

    history = build_history(listings, next_date)
    decision = ""
    if disposed:
        decision = (iso_to_dmy(case.get("disposal_date"))
                    or iso_to_dmy(case.get("t_status_date"))
                    or (max(final_dates, key=dmy_key) if final_dates else "")
                    or (history[-1]["businessOnDate"] if history else ""))

    nature = ""
    if disposed:
        nature = status_desc.title()
        if status_reason and status_reason.upper() != "NO REASON":
            nature = f"{nature} - {status_reason.title()}"

    party_record = {}
    try:
        party_record = _get(session, "/cis_filing/public/getCase",
                            params={"case_no": reg_no, "case_year": year, "case_type": portal_type},
                            allow_404=True) or {}
    except PortalError as exc:
        print(f"[phhc] party list failed: {exc}")

    petitioners = build_parties(_name(case.get("pet_name")), _name(case.get("pet_adv_name")),
                                party_record.get("petitionerDetails") if isinstance(party_record, dict) else [])
    respondents = build_parties(_name(case.get("res_name")), _name(case.get("res_adv_name")),
                                party_record.get("respondentDetails") if isinstance(party_record, dict) else [])
    district = _name((case.get("district") or {}).get("name"))
    category = _name(case.get("cat_desc"))

    sub_matters = []
    for item in related:
        label = " ".join(str(item.get(k) or "") for k in ("case_type", "case_no", "case_year")).strip()
        if label and label != f"{portal_type} {reg_no} {year}":
            sub_matters.append(label)

    metadata = {
        "case_number": f"{portal_type}/{reg_no}/{year}",
        "cino": _name(case.get("cnr_no")),
        "CaseType": portal_type,
        "FilingNumber": _name(case.get("filling_no")),
        "FilingDate": iso_to_dmy(case.get("filling_date")),
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": iso_to_dmy(case.get("reg_date")),
        "CNRNumber": _name(case.get("cnr_no")),
        "DiaryNumber": _name(case.get("case_diary_no")),
        "FirstHearingDate": history[0]["businessOnDate"] if history else "",
        "NextHearingDate": next_date,
        "LastListedDate": history[-1]["businessOnDate"] if history else "",
        "DecisionDate": decision,
        "CaseStatus": "Disposed" if disposed else (status_desc.title() or "Pending"),
        "PortalStatus": " - ".join(p for p in (status_desc, status_reason) if p),
        "NatureofDisposal": nature,
        "CourtNumberandJudge": _name(case.get("bench_name")),
        "BenchType": {"S": "Single Bench", "D": "Division Bench", "F": "Full Bench"}.get(
            str(case.get("bench_type") or "").upper(), _name(case.get("bench_type"))),
        "BenchName": BENCHES["1"],
        "JudicialBranch": "",
        "State": "",
        "District": district,
        "NotBeforeMe": "",
        "ListRemark": _name(case.get("list_remark")),
        "petitioner_and_advocate": [p["name"] for p in petitioners],
        "respondent_and_advocate": [p["name"] for p in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": {},
        "acts": [],
        "category_details": {"Category": category, "Code": _name(case.get("category"))} if category else {},
        "subordinate_court_information": {"District": district} if district else {},
        "sub_matters": sub_matters,
        "ia_details": [],
        "case_history": history,
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": f"{portal_type}|{reg_no}|{year}",
    }
    return metadata, orders
