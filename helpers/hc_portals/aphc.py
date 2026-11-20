import base64
import json
import random
import re
import time
from datetime import date

import requests

from core.lambda_client import lambda_client
from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_no,
    ecourts_case_type_name,
    iso_to_dmy,
    meaningful,
    short_case_code,
    slash_to_dash,
)
from helpers.solve_captcha import solve_captcha

NAME = "aphc"
LABEL = "Andhra Pradesh High Court"
STATE_CODE = "2"
BENCHES = {"1": "Amaravati"}
PROVIDES_HISTORY = False

CASES = "https://digi-courts.aphc.ap.gov.in/csis_ap"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 60)
MAX_RETRIES = 3
MAX_CODE_ATTEMPTS = 5
NO_DATE = ("01/01/1900", "01/01/5000")


def open_session():
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT, "Referer": f"{CASES}/"})
    return session


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.2, 0.5))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[aphc] {method} {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise PortalError(f"aphc.gov.in {url} returned HTTP {response.status_code}")
        return response
    raise PortalError(f"aphc.gov.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _get(session, url):
    return _request(session, "GET", url).text


def _decode(text):
    try:
        return json.loads(base64.b64decode(text.strip()).decode("utf-8", "ignore") or "[]")
    except ValueError:
        raise PortalError("aphc.gov.in case search returned an unexpected response")


def _answer(session):
    raw = _request(session, "GET", f"{CASES}/loadCaptcha", params={"date": str(time.time())}).content
    text = solve_captcha(lambda_client=lambda_client, image_base64=base64.b64encode(raw).decode("ascii"),
                         frm="hc") or ""
    match = re.fullmatch(r"(\d+)(plus|minus|into|x|times)(\d+)", text.lower())
    if not match:
        return None
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    if operator == "plus":
        return left + right
    if operator == "minus":
        return left - right
    return left * right


def search_case(session, case_code, reg_no, year):
    _request(session, "GET", f"{CASES}/")
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        answer = _answer(session)
        if answer is None:
            continue
        params = {"searchtype": "casenumber", "mtype": case_code, "mno": reg_no, "myear": year,
                  "captcha": str(answer)}
        rows = _decode(_request(session, "POST", f"{CASES}/getCaseDetails.action", params=params).text)
        if not rows:
            raise PortalCaseNotFound(f"aphc.gov.in has no {case_code} {reg_no}/{year}")
        error = clean_text(rows[0].get("error"))
        if error and error != "-":
            if "captcha" in error.lower():
                print(f"[aphc] verification attempt {attempt} not accepted")
                continue
            raise PortalError(f"aphc.gov.in case search said: {error}")
        return rows[0]
    raise PortalError(f"aphc.gov.in case search was not accepted after {MAX_CODE_ATTEMPTS} attempts")


def _parties(rows, number_key, name_key, advocate):
    parties = []
    for index, row in enumerate(rows or []):
        name = meaningful(row.get(name_key))
        if not name:
            continue
        number = meaningful(row.get(number_key))
        parties.append({
            "name": name,
            "advocate": advocate if index == 0 else "",
            "relation": f"{'P' if number_key == 'psno' else 'R'}{number}" if number else "",
            "address": meaningful(row.get("address")),
        })
    return parties


def _ia_details(case):
    items = []
    for row in case.get("mpdetails") or []:
        items.append({
            "iaNumber": meaningful(row.get("mpcasenum")),
            "serialNumber": meaningful(row.get("oldmpcasenum")),
            "classification": meaningful(row.get("papertype")),
            "party": meaningful(row.get("advname")),
            "dateOfFiling": slash_to_dash(row.get("dateofiafiling")),
            "nextDate": "",
            "status": meaningful(row.get("order")) or meaningful(row.get("orderdate")),
        })
    for row in case.get("iasrdetails") or []:
        items.append({
            "iaNumber": meaningful(row.get("iasrnum")),
            "serialNumber": "",
            "classification": meaningful(row.get("iasrtype")),
            "party": meaningful(row.get("iasradv")),
            "dateOfFiling": slash_to_dash(row.get("iasrdate")),
            "nextDate": "",
            "status": meaningful(row.get("iasrstatus")).title(),
        })
    return items


def _first_text(row, keys):
    for key in keys:
        value = meaningful(row.get(key))
        if value:
            return value
    return ""


def _connected(case, own):
    matters = []
    for row in case.get("condetails") or []:
        label = _first_text(row, ("concasenum", "casenum", "caseno", "maincasenum")) if isinstance(row, dict) else ""
        if label and label.upper() != own.upper() and label not in matters:
            matters.append(label)
    return matters


def _lower_court(case):
    rows = case.get("lcdetails") or []
    if not rows or not isinstance(rows[0], dict):
        return {}
    row = rows[0]
    info = {}
    for key, label in (("lccourt", "Court"), ("lcourt", "Court"), ("courtname", "Court"),
                       ("lccaseno", "Case Number"), ("lcno", "Case Number"), ("lccasenum", "Case Number"),
                       ("lcjudge", "Judge"), ("lcdate", "Decision Date"), ("lcorderdate", "Decision Date")):
        value = meaningful(row.get(key))
        if value and label not in info:
            info[label] = value
    district = meaningful(case.get("district"))
    if district:
        info["District"] = district
    return info


def build_orders(case):
    found = []
    for row in case.get("orderdetails") or []:
        name = meaningful(row.get("orderno"))
        if not name:
            continue
        kind = meaningful(row.get("docuname"))
        found.append({
            "date": iso_to_dmy(row.get("orderdt")) or slash_to_dash(row.get("orderdt")),
            "kind": kind,
            "judge": meaningful(row.get("judgename")),
            "href": f"{CASES}/sdcorderpdf.action?year={meaningful(row.get('myear'))}"
                    f"&fname={name}&otype={meaningful(row.get('doctype'))}",
            "ref": f"{name}:{meaningful(row.get('doctype'))}",
        })
    found.sort(key=lambda o: dmy_key(o["date"]))
    return [{
        "order_number": str(number),
        "order_on": o["kind"] or "Order",
        "judge": o["judge"],
        "order_date": o["date"],
        "order_type": "final" if re.search(r"final|judg", o["kind"], re.I) else "order",
        "href": o["href"],
        "source_ref": f"{NAME}:{o['ref']}",
    } for number, o in enumerate(found, start=1)]


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    raw = clean_text(str(case_type or ""))
    name = ecourts_case_type_name(STATE_CODE, court_code or "1", raw) if raw.isdigit() else raw
    case_code = short_case_code(name).replace(" ", "")
    if not case_code:
        raise PortalCaseNotFound(f"no Andhra Pradesh High Court case type matches eCourts type {case_type!r}")

    reg_no, year = str(int(case_reg_no)), str(rgyear)
    case = search_case(session, case_code, reg_no, year)

    status = meaningful(case.get("pdrstatus")).upper()
    disposed = "DISPOS" in status or bool(meaningful(case.get("dispdate")))
    decision = slash_to_dash(case.get("dispdate")) if disposed else ""

    today_key = date.today().strftime("%Y%m%d")
    upcoming = [slash_to_dash(v) for v in (case.get("nexthdate"), case.get("listdate"))
                if meaningful(v) and clean_text(v) not in NO_DATE]
    upcoming = [d for d in upcoming if d and dmy_key(d) >= today_key]
    next_date = "" if disposed or not upcoming else min(upcoming, key=dmy_key)

    petitioners = _parties(case.get("petdetails"), "psno", "pname", meaningful(case.get("petitioneradv")))
    respondents = _parties(case.get("resdetails"), "rsno", "rname", meaningful(case.get("respondentadv")))
    if not petitioners and meaningful(case.get("petitioner")):
        petitioners = [{"name": meaningful(case.get("petitioner")).rstrip(","),
                        "advocate": meaningful(case.get("petitioneradv")), "relation": "", "address": ""}]
    if not respondents and meaningful(case.get("respondent")):
        respondents = [{"name": meaningful(case.get("respondent")).rstrip(","),
                        "advocate": meaningful(case.get("respondentadv")), "relation": "", "address": ""}]

    own = meaningful(case.get("maincasenum")) or f"{case_code} {reg_no}/{year}"
    orders = build_orders(case)
    category = " / ".join(p for p in (meaningful(case.get("subject1")), meaningful(case.get("subject2"))) if p)
    nature = meaningful(case.get("disptype")).title() if disposed else ""

    metadata = {
        "case_no": ecourts_case_no(case_type, reg_no, year) if raw.isdigit() else "",
        "case_number": f"{case_code}/{reg_no}/{year}",
        "cino": meaningful(case.get("cino")),
        "CaseType": case_code,
        "CaseTypeName": name,
        "FilingNumber": meaningful(case.get("srcasenum")),
        "FilingDate": slash_to_dash(case.get("filingdate")),
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": slash_to_dash(case.get("registrationdate")),
        "CNRNumber": meaningful(case.get("cino")),
        "FirstHearingDate": "",
        "NextHearingDate": next_date,
        "LastListedDate": "",
        "DecisionDate": decision,
        "CaseStatus": "Disposed" if disposed else (status.title() or "Pending"),
        "PortalStatus": " - ".join(p for p in (status, meaningful(case.get("disptype"))) if p),
        "Stage": meaningful(case.get("purname")),
        "NatureofDisposal": (nature or "Disposed") if disposed else "",
        "CourtNumberandJudge": meaningful(case.get("judname")),
        "BenchType": "",
        "BenchName": BENCHES["1"],
        "JudicialBranch": "",
        "State": "ANDHRA PRADESH",
        "District": meaningful(case.get("district")),
        "NotBeforeMe": "",
        "Prayer": meaningful(case.get("prayer")),
        "petitioner_and_advocate": [p["name"] for p in petitioners],
        "respondent_and_advocate": [p["name"] for p in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": {},
        "acts": [],
        "category_details": {"Category": category, "Nature": meaningful(case.get("naturecd"))} if category else {},
        "subordinate_court_information": _lower_court(case),
        "sub_matters": _connected(case, own),
        "ia_details": _ia_details(case),
        "case_history": [],
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": f"{case_code}|{reg_no}|{year}",
    }
    return metadata, orders
