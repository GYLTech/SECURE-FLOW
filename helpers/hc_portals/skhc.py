import json
import random
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

from helpers.ca_bundle import ca_bundle
from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_no,
    meaningful,
    slash_to_dash,
)

NAME = "skhc"
LABEL = "Sikkim High Court"
STATE_CODE = "24"
BENCHES = {"1": "Gangtok"}
PROVIDES_HISTORY = True

ORIGIN = "https://hcs.gov.in"
SEARCH = ORIGIN + "/hcs/hcourt/hg_case_search"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 60)
MAX_RETRIES = 3
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*"}


def open_session():
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT})
    session.verify = ca_bundle()
    return session


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.2, 0.5))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[skhc] {method} {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PortalError(f"hcs.gov.in {url} returned HTTP {response.status_code}")
        return response
    raise PortalError(f"hcs.gov.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _inserted_html(text):
    text = (text or "").strip()
    if not text.startswith("["):
        return text
    try:
        commands = json.loads(text)
    except ValueError:
        return text
    return max((c.get("data") for c in commands if isinstance(c, dict) and isinstance(c.get("data"), str)),
               key=len, default="")


def search_cnr(session, case_type, reg_no, year):
    page = _request(session, "GET", SEARCH)
    build_id = re.search(r'name="form_build_id" value="([^"]+)"', page.text if page else "")
    if not build_id:
        raise PortalError("hcs.gov.in case search form could not be read")
    response = _request(session, "POST", SEARCH, params={"ajax_form": 1, "_wrapper_format": "drupal_ajax"},
                        headers=AJAX, data={
                            "casetype": str(int(case_type)), "caseno": reg_no, "caseyear": year,
                            "form_build_id": build_id.group(1), "form_id": "ajax_example_form",
                            "_triggering_element_name": "op", "_triggering_element_value": "Search",
                        })
    soup = BeautifulSoup(_inserted_html(response.text if response else ""), "html.parser")
    for row in soup.find_all("tr"):
        link = row.find("a", href=re.compile(r"/hgcases/SKHC\d{12}"))
        cells = [clean_text(c.get_text(" ")) for c in row.find_all("td")]
        if not link or not cells:
            continue
        parts = [p.strip() for p in cells[0].split("/")]
        if len(parts) >= 3 and parts[-2].lstrip("0") == reg_no and parts[-1][:4] == year:
            return link["href"].rsplit("/", 1)[-1]
    raise PortalCaseNotFound(f"hcs.gov.in has no case {case_type} {reg_no}/{year}")


def _pairs(soup):
    values = {}
    labels = soup.find_all("div", class_=re.compile(r"col-md-(4|6)"))
    for label in labels:
        value = label.find_next_sibling("div")
        if not value or label.find("h3") or value.find("h3"):
            continue
        key = clean_text(label.get_text(" ")).rstrip(":").strip().lower()
        if key and key not in values:
            values[key] = value
    return values


def _section_table(soup, title):
    heading = soup.find("h3", string=re.compile(title, re.I))
    table = heading.find_next("table") if heading else None
    if not table:
        return []
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if cells:
            rows.append(cells)
    return rows


def _party_block(soup, title, prefix, address_key, advocate_key):
    heading = soup.find("h3", string=re.compile(title, re.I))
    block = heading.parent if heading else None
    if not block:
        return []
    values = _pairs(block)
    main = values.get(f"{'petitioner' if prefix == 'P' else 'respondent'}(s)")
    main_name = re.sub(r"^\d+\.\s*", "", clean_text(main.get_text(" "))) if main else ""
    main_name = re.sub(r"\s+AND\s+(ORS|OTHERS)\.?$", "", main_name, flags=re.I).strip()
    address = clean_text(values[address_key].get_text(" ")) if values.get(address_key) else ""
    advocate = clean_text(values[advocate_key].get_text(" ")).strip(" ,") if values.get(advocate_key) else ""
    advocate = re.sub(r"\s*,\s*,?", ", ", advocate).strip(" ,")
    parties = [{"name": main_name, "advocate": advocate, "relation": f"{prefix}1", "address": address}] \
        if main_name else []
    extra = values.get("extra parties")
    if extra:
        lines = [clean_text(t) for t in extra.get_text("\n").split("\n")]
        current = None
        for line in lines:
            if not line:
                current = None
                continue
            numbered = re.match(r"^(\d+)\.\s*(.+)$", line)
            if numbered:
                current = {"name": numbered.group(2), "advocate": "", "relation": f"{prefix}{numbered.group(1)}",
                           "address": ""}
                parties.append(current)
            elif current is not None:
                current["address"] = ", ".join(p for p in (current["address"], line) if p)
    return parties


def build_history(rows, next_date):
    entries = []
    for cells in rows:
        texts = [clean_text(c.get_text(" ")) for c in cells]
        if len(texts) < 2 or not re.match(r"\d{2}-\d{2}-\d{4}", texts[1]):
            continue
        entries.append({"judge": texts[0], "date": texts[1], "purpose": texts[2] if len(texts) > 2 else ""})
    entries.sort(key=lambda e: dmy_key(e["date"]))
    history = []
    for index, entry in enumerate(entries):
        if index + 1 < len(entries):
            hearing = entries[index + 1]["date"]
        elif next_date and dmy_key(next_date) > dmy_key(entry["date"]):
            hearing = next_date
        else:
            hearing = ""
        history.append({
            "causeListType": "",
            "judge": entry["judge"],
            "businessOnDate": entry["date"],
            "hearingDate": hearing,
            "purpose": entry["purpose"],
            "action": "",
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    return history


def build_orders(soup):
    found = []
    for title, final in (("DAILY ORDERS", False), ("JUDGMENT", True)):
        for cells in _section_table(soup, title):
            texts = [clean_text(c.get_text(" ")) for c in cells]
            link = next((a["href"] for c in cells for a in c.find_all("a", href=True)
                         if a["href"].lower().endswith(".pdf") and "/hg_orders/" in a["href"]), "")
            if not texts or not re.match(r"\d{2}-\d{2}-\d{4}", texts[0]):
                continue
            found.append({
                "date": texts[0],
                "judge": texts[1] if len(texts) > 1 else "",
                "final": final,
                "href": (ORIGIN + link) if link.startswith("/") else link,
                "ref": link.rsplit("/", 1)[-1] or f"{title}:{texts[0]}",
            })
    found.sort(key=lambda o: (dmy_key(o["date"]), o["final"]))
    return [{
        "order_number": str(number),
        "order_on": "Judgment/Final Order" if o["final"] else "Daily Order",
        "judge": o["judge"],
        "order_date": o["date"],
        "order_type": "final" if o["final"] else "order",
        "href": o["href"],
        "source_ref": f"{NAME}:{o['ref']}:{'F' if o['final'] else 'D'}",
    } for number, o in enumerate(found, start=1)]


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    if not str(case_type or "").isdigit():
        raise PortalCaseNotFound(f"Sikkim High Court needs the eCourts case type code, got {case_type!r}")
    reg_no, year = str(int(case_reg_no)), str(rgyear)
    known = clean_text(cino).upper()
    found = known if re.fullmatch(r"SKHC\d{12}", known) else search_cnr(session, case_type, reg_no, year)

    response = _request(session, "GET", f"{ORIGIN}/hcs/hgcases/{found}")
    soup = BeautifulSoup(_inserted_html(response.text if response else ""), "html.parser")
    values = _pairs(soup)
    if "cnr" not in values:
        if found == known:
            return fetch_case(session, court_code=court_code, case_type=case_type,
                              case_reg_no=case_reg_no, rgyear=rgyear)
        raise PortalCaseNotFound(f"hcs.gov.in returned no details for {found}")

    text = {k: clean_text(v.get_text(" ")) for k, v in values.items()}
    number = text.get("case number", "")
    parts = [p.strip() for p in number.split("/")]
    if len(parts) < 3 or parts[-2].lstrip("0") != reg_no or parts[-1][:4] != year:
        raise PortalCaseNotFound(f"hcs.gov.in returned {number} instead of {reg_no}/{year}")
    short = re.sub(r"\s+", "", parts[0])

    status = text.get("case status", "")
    decision = slash_to_dash(text.get("disposed date"))
    disposed = "DISPOS" in status.upper() or bool(decision)
    today_key = date.today().strftime("%Y%m%d")
    upcoming = slash_to_dash(text.get("next date") or text.get("next hearing date") or text.get("next date of hearing"))
    next_date = "" if disposed or not upcoming or dmy_key(upcoming) < today_key else upcoming

    history = build_history(_section_table(soup, "CASE HISTORY"), next_date)
    if disposed and history:
        history[-1]["hearingDate"] = ""

    acts = []
    for cells in _section_table(soup, "Acts"):
        texts = [clean_text(c.get_text(" ")) for c in cells]
        if len(texts) >= 3 and texts[1]:
            acts.append({"act": texts[1], "section": texts[2]})

    ia_details = []
    for cells in _section_table(soup, "INTERLOCUTORY"):
        texts = [clean_text(c.get_text(" ")) for c in cells]
        if len(texts) >= 4 and texts[0]:
            ia_details.append({"iaNumber": texts[0], "serialNumber": "", "classification": texts[2],
                               "party": "", "dateOfFiling": slash_to_dash(texts[1]), "nextDate": "",
                               "status": texts[3]})

    petitioners = _party_block(soup, r"PETITIONER\(s\)", "P", "address", "advocate(s)")
    respondents = _party_block(soup, r"RESPONDENT\(s\)", "R", "address", "advocate(s)")
    orders = build_orders(soup)
    bench = text.get("bench", "")

    metadata = {
        "case_no": ecourts_case_no(case_type, reg_no, year),
        "case_number": f"{short}/{reg_no}/{year}",
        "cino": text.get("cnr", found),
        "CaseType": short,
        "CaseTypeName": text.get("case type", ""),
        "FilingNumber": text.get("token number", ""),
        "FilingDate": slash_to_dash(text.get("date of filing")),
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": slash_to_dash(text.get("date of registration") or text.get("registration date")),
        "CNRNumber": text.get("cnr", found),
        "FirstHearingDate": history[0]["businessOnDate"] if history else "",
        "NextHearingDate": next_date,
        "LastListedDate": history[-1]["businessOnDate"] if history else "",
        "DecisionDate": decision if disposed else "",
        "CaseStatus": "Disposed" if disposed else (status.title() or "Pending"),
        "PortalStatus": status,
        "NatureofDisposal": "Disposed" if disposed else "",
        "CourtNumberandJudge": bench,
        "BenchType": "Division Bench" if "&" in bench else ("Single Bench" if bench else ""),
        "BenchName": BENCHES["1"],
        "JudicialBranch": "",
        "State": "SIKKIM",
        "District": "",
        "NotBeforeMe": "",
        "petitioner_and_advocate": [p["name"] for p in petitioners],
        "respondent_and_advocate": [p["name"] for p in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": {
            "acts": ", ".join(a["act"] for a in acts),
            "section": ", ".join(a["section"] for a in acts if a["section"]),
        } if acts else {},
        "acts": acts,
        "category_details": {},
        "subordinate_court_information": {},
        "sub_matters": [],
        "ia_details": ia_details,
        "case_history": history,
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": found,
    }
    if not meaningful(metadata["cino"]):
        raise PortalError(f"hcs.gov.in returned an empty case page for {found}")
    return metadata, orders
