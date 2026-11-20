import random
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_no,
    meaningful,
    slash_to_dash,
)
from helpers.legacy_tls import legacy_session

NAME = "bhc"
LABEL = "Bombay High Court"
STATE_CODE = "1"
BENCHES = {"1": "Appellate Side, Bombay", "2": "Original Side, Bombay"}
SIDES = {"1": "1", "2": "2"}
PROVIDES_HISTORY = True

ORIGIN = "https://bombayhighcourt.gov.in"
SEARCH = ORIGIN + "/bhc/casestatus/casenumber"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 90)
MAX_RETRIES = 3
DISPOSAL_WORDS = re.compile(r"DISPOS|DISMISS|ALLOW|WITHDRAW|REJECT|DECIDED|ABATE|CLOSED|SETTLED|DECREED", re.I)


def open_session():
    session = legacy_session(ORIGIN)
    session.headers.update({"user-agent": USER_AGENT})
    return session


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.2, 0.5))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[bhc] {method} {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500 or response.status_code == 419:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise PortalError(f"bombayhighcourt.gov.in {url} returned HTTP {response.status_code}")
        return response
    raise PortalError(f"bombayhighcourt.gov.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def search_case(session, side, case_type, reg_no, year):
    page = _request(session, "GET", SEARCH).text
    token = re.search(r'name="_token" value="([^"]+)"', page)
    secret = re.search(r'name="form_secret" value="([^"]+)"', page)
    if not token or not secret:
        raise PortalError("bombayhighcourt.gov.in case search form could not be read")
    response = _request(session, "POST", SEARCH, headers={"X-Requested-With": "XMLHttpRequest",
                                                          "Accept": "application/json"}, data={
        "_token": token.group(1), "form_secret": secret.group(1), "side": side, "stampreg": "R",
        "case_type": str(int(case_type)), "case_no": reg_no, "year": year,
    })
    try:
        data = response.json()
    except ValueError:
        raise PortalError("bombayhighcourt.gov.in case search returned an unexpected response")
    html = data.get("page") or "" if isinstance(data, dict) else ""
    if not data.get("status") or "with CNR No" not in html:
        raise PortalCaseNotFound(f"bombayhighcourt.gov.in has no case {case_type} {reg_no}/{year}"
                                 f" ({clean_text(data.get('message'))})" if isinstance(data, dict) else "")
    return BeautifulSoup(html, "html.parser")


def _rows(pane):
    values = {}
    for row in pane.select("div.row"):
        cols = row.find_all("div", recursive=False)
        if len(cols) < 2:
            continue
        key = clean_text(cols[0].get_text(" ")).lower()
        if key and key not in values:
            values[key] = cols[1]
    return values


def _table(soup, pane_id):
    pane = soup.find(id=pane_id)
    rows = []
    for tr in pane.find_all("tr") if pane else []:
        cells = tr.find_all("td")
        if len(cells) >= 2:
            rows.append(cells)
    return rows


def _names(cell):
    if not cell:
        return []
    names = []
    for p in cell.find_all("p") or [cell]:
        text = clean_text(p.get_text(" "))
        if text:
            names.append(text)
    return names


def _parties(cell, advocate, prefix):
    names = _names(cell)
    parties = []
    for index, name in enumerate(names):
        cleaned = re.sub(r"\s+AND\s+(ANR|ORS|OTHERS)\.?$", "", name, flags=re.I).strip()
        parties.append({"name": cleaned, "advocate": advocate if index == 0 else "",
                        "relation": f"{prefix}{index + 1}", "address": ""})
    return parties


def build_history(rows, next_date):
    entries = {}
    for cells in rows:
        texts = [clean_text(c.get_text(" ")) for c in cells]
        if len(texts) < 3:
            continue
        when = slash_to_dash(texts[1])
        if when and when not in entries:
            entries[when] = {"judge": texts[2], "remark": meaningful(texts[3].strip("—")) if len(texts) > 3 else ""}
    dates = sorted(entries, key=dmy_key)
    history = []
    for index, when in enumerate(dates):
        if index + 1 < len(dates):
            hearing = dates[index + 1]
        elif next_date and dmy_key(next_date) > dmy_key(when):
            hearing = next_date
        else:
            hearing = ""
        history.append({
            "causeListType": "",
            "judge": entries[when]["judge"],
            "businessOnDate": when,
            "hearingDate": hearing,
            "purpose": entries[when]["remark"],
            "action": "",
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    return history


def build_orders(rows, cnr):
    found = []
    for cells in rows:
        texts = [clean_text(c.get_text(" ")) for c in cells]
        if len(texts) < 4:
            continue
        when = slash_to_dash(texts[2])
        kind = re.sub(r"\s*\(\d+\)\s*$", "", texts[3])
        for link in cells[-1].find_all("a", href=re.compile(r"order-pdf")) or [None]:
            href = link["href"] if link else ""
            ref = re.search(r"order-pdf/[^/]+/([^/?]+)/([^/?]+)", href)
            found.append({
                "date": when,
                "kind": kind,
                "judge": texts[1],
                "href": href,
                "ref": f"{cnr}:{ref.group(1)}:{ref.group(2)}" if ref else f"{cnr}:{when}:{texts[0]}",
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
    side = SIDES.get(str(court_code or "1"))
    if not side or not str(case_type or "").isdigit():
        raise PortalCaseNotFound(f"bombayhighcourt.gov.in case search covers the Bombay sides only, "
                                 f"not bench {court_code!r}")
    reg_no, year = str(int(case_reg_no)), str(rgyear)
    soup = search_case(session, side, case_type, reg_no, year)

    pane = soup.find(id="cn_CaseNoUpdates") or soup
    header = clean_text(pane.get_text(" "))
    number = re.search(r"Case No\.\s*([A-Z0-9.()\-]+)\s*/\s*(\d+)\s*/\s*(\d{4})", header)
    cnr = re.search(r"\b([A-Z]{4}\d{12})\b", header)
    if not number or number.group(2).lstrip("0") != reg_no or number.group(3) != year:
        raise PortalCaseNotFound(f"bombayhighcourt.gov.in returned a different case for {reg_no}/{year}")
    short = number.group(1)
    filed = re.search(r"was filed on\s*(\d{2}/\d{2}/\d{4})", header)

    values = _rows(pane)
    text = {k: meaningful(v.get_text(" ")) for k, v in values.items()}
    status = text.get("status", "")
    decision = slash_to_dash(text.get("disposal date") or text.get("date of disposal"))
    disposed = bool(decision) or bool(DISPOSAL_WORDS.search(status))
    today_key = date.today().strftime("%Y%m%d")
    upcoming = slash_to_dash(text.get("next listing date"))
    next_date = "" if disposed or not upcoming or dmy_key(upcoming) < today_key else upcoming

    history = build_history(_table(soup, "cn_CaseNoListing"), next_date)
    orders = build_orders(_table(soup, "cn_CaseNoOrders"), cnr.group(1) if cnr else "")
    if disposed and not decision:
        decision = (text.get("last date") and slash_to_dash(text.get("last date"))) or \
                   (history[-1]["businessOnDate"] if history else "")
    if disposed and orders and not any(o["order_type"] == "final" for o in orders):
        orders[-1]["order_type"] = "final"

    petitioners = _parties(values.get("petitioner"), text.get("petitioner's advocate", ""), "P")
    respondents = _parties(values.get("respondent"), text.get("respondent's advocate", ""), "R")

    sub_matters = []
    for pane_id in ("cn_CaseNoConnMatters", "cn_CaseNoApplCases"):
        for cells in _table(soup, pane_id):
            texts = [clean_text(c.get_text(" ")) for c in cells]
            label = next((t for t in texts[2:] if re.search(r"/\d{4}", t)), "") or (texts[1] if len(texts) > 1 else "")
            if label and label not in sub_matters:
                sub_matters.append(label)

    lower = {}
    lower_rows = _table(soup, "cn_CaseNoLowerCourtDeatils")
    lower_head = soup.find(id="cn_CaseNoLowerCourtDeatils")
    if lower_rows and lower_head:
        heads = [clean_text(th.get_text(" ")) for th in lower_head.find_all("th")]
        cells = [clean_text(c.get_text(" ")) for c in lower_rows[0]]
        lower = {h: v for h, v in zip(heads, cells) if h and v and h != "Sr. No."}
    district = text.get("district", "")
    if district:
        lower.setdefault("District", district)

    objections = []
    for cells in _table(soup, "cn_CaseNoObjections"):
        texts = [clean_text(c.get_text(" ")) for c in cells]
        if len(texts) >= 2 and texts[1]:
            objections.append({"objection": texts[1], "date": texts[2] if len(texts) > 2 else ""})

    act = text.get("act", "")
    section = text.get("under section", "")
    metadata = {
        "case_no": ecourts_case_no(case_type, reg_no, year),
        "case_number": f"{short}/{reg_no}/{year}",
        "cino": cnr.group(1) if cnr else "",
        "CaseType": short,
        "FilingNumber": text.get("stamp number", ""),
        "FilingDate": slash_to_dash(filed.group(1)) if filed else "",
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": slash_to_dash(text.get("registration date")),
        "CNRNumber": cnr.group(1) if cnr else "",
        "EFilingNumber": text.get("e-filing number", ""),
        "NeutralCitation": text.get("neutral citation number", ""),
        "FirstHearingDate": history[0]["businessOnDate"] if history else "",
        "NextHearingDate": next_date,
        "LastListedDate": slash_to_dash(text.get("last date")) or (history[-1]["businessOnDate"] if history else ""),
        "DecisionDate": decision if disposed else "",
        "CaseStatus": "Disposed" if disposed else (status.replace("_", " ").title() or "Pending"),
        "PortalStatus": status,
        "Stage": text.get("stage", ""),
        "NatureofDisposal": (status.replace("_", " ").title() or "Disposed") if disposed else "",
        "CourtNumberandJudge": text.get("last coram", ""),
        "BenchType": "Division Bench" if "," in text.get("last coram", "") else "",
        "BenchName": BENCHES[str(court_code or "1")],
        "JudicialBranch": "",
        "State": "MAHARASHTRA",
        "District": district,
        "NotBeforeMe": "",
        "petitioner_and_advocate": [p["name"] for p in petitioners],
        "respondent_and_advocate": [p["name"] for p in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": {"acts": act, "section": "" if section == "NA" else section} if act else {},
        "acts": [{"act": act, "section": "" if section == "NA" else section}] if act else [],
        "category_details": {},
        "subordinate_court_information": lower,
        "sub_matters": sub_matters,
        "ia_details": [],
        "case_history": history,
        "case_conversion": [],
        "case_transfer": [],
        "objections": objections,
        "document_details": [],
        "portal_ref": f"{side}|{case_type}|{reg_no}|{year}",
    }
    return metadata, orders
