import random
import re
import time

import requests
from bs4 import BeautifulSoup, Comment

from helpers.ca_bundle import ca_bundle
from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    ecourts_case_no,
    meaningful,
)

NAME = "mphc"
LABEL = "Madhya Pradesh High Court"
STATE_CODE = "23"
BENCHES = {"1": "Jabalpur", "2": "Indore", "3": "Gwalior"}

ORIGIN = "https://mphc.gov.in"
CASE_STATUS_URL = ORIGIN + "/case-status"
BENCH_SELECT_URL = ORIGIN + "/bench/select"
MODAL_URL = ORIGIN + "/case-status-modal"
TAB_URL = ORIGIN + "/case-status-tab"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 60)
MAX_RETRIES = 3

CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
FIL_NO_RE = re.compile(r'name="fil_no"\s+id="fil_no"\s+value="(\d*)"')
DATE_RE = re.compile(r"\b(\d{2})[-/](\d{2})[-/](\d{4})\b")
CASE_NO_RE = re.compile(r"^(.*?)\(\s*([A-Z0-9.\- ]+?)\s*\)\s*(\d+)\s*/\s*(\d{4})")
CNR_RE = re.compile(r"CNR\s*:\s*([A-Z]{4}\d{12})")
DISPOSAL_DATE_RE = re.compile(r"Disposal Date\s*:\s*(\d{2}-\d{2}-\d{4})", re.I)
BENCH_RE = re.compile(r"Bench\s*:\s*(.+)$", re.I)
ENROLMENT_RE = re.compile(r"\s*\[\d+/\d+\]\s*$")
ACT_RE = re.compile(r"^(?:\d+\s*-\s*)?(.*?)\s*\(\s*Section\s*-\s*(.*)\)\s*$", re.I)
CITATION_RE = re.compile(r"\b\d{4}:MPHC-[A-Z]+:\d+\b")
LISTED_RE = re.compile(r"Connected with\s*:\s*(.+?)\s*\]?$", re.I)


def normalize_date(value):
    match = DATE_RE.search(value or "")
    return "-".join(match.groups()) if match else ""


def open_session():
    session = requests.Session()
    session.verify = ca_bundle()
    session.headers.update({"user-agent": USER_AGENT})
    return session


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.3, 0.8))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[mphc] {method} {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500 or response.status_code == 419:
            last_error = f"HTTP {response.status_code}"
            print(f"[mphc] {method} {url} attempt {attempt}: {last_error}")
            continue
        return response
    raise PortalError(f"mphc.gov.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _csrf(session):
    page = _request(session, "GET", CASE_STATUS_URL).text
    match = CSRF_RE.search(page)
    if not match:
        raise PortalError("mphc.gov.in case status page carried no CSRF token")
    return match.group(1), page


def _ajax_headers(token):
    return {
        "X-CSRF-TOKEN": token,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
        "Referer": CASE_STATUS_URL,
        "Origin": ORIGIN,
    }


def _post_json(session, url, data, token):
    response = _request(session, "POST", url, data=data, headers=_ajax_headers(token))
    if response.status_code != 200:
        raise PortalError(f"mphc.gov.in {url} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise PortalError(f"mphc.gov.in {url} did not return JSON")


def build_fil_no(court_code, case_type, case_reg_no, rgyear):
    reg_no = int(case_reg_no)
    if reg_no > 99999:
        return None
    return f"{int(court_code):02d}{int(case_type):03d}{reg_no:05d}{int(rgyear):04d}"


def _search_fil_no(session, token, court_code, case_type, case_reg_no, rgyear):
    bench = str(int(court_code))
    _request(session, "POST", BENCH_SELECT_URL,
             data={"_token": token, "bench_code": bench},
             headers={"Referer": CASE_STATUS_URL})
    token, _ = _csrf(session)
    page = _request(session, "POST", CASE_STATUS_URL, data={
        "_token": token,
        "branch": f"{int(court_code):02d}",
        "fil_no": "",
        "case_type": str(int(case_type)),
        "case_no": str(int(case_reg_no)),
        "year_registration": str(rgyear),
    }, headers={"Referer": CASE_STATUS_URL}).text
    match = FIL_NO_RE.search(page)
    return (match.group(1) if match and match.group(1) else None), token


def _fetch_modal(session, token, fil_no):
    result = _post_json(session, MODAL_URL, {"fil_no": fil_no}, token)
    if not result.get("status"):
        return None
    return result.get("page") or ""


def _fetch_tab(session, token, fil_no, tab):
    try:
        result = _post_json(session, TAB_URL, {"tab_data": tab, "fil_no": fil_no}, token)
    except PortalError as exc:
        print(f"[mphc] tab {tab} for {fil_no} failed: {exc}")
        return None
    return result.get("page") or ""


def _soup(fragment):
    soup = BeautifulSoup(fragment or "", "html.parser")
    for node in soup.find_all(string=lambda text: isinstance(text, Comment)):
        node.extract()
    for node in soup.find_all(["script", "style"]):
        node.decompose()
    return soup


def _cell_lines(cell):
    for br in cell.find_all("br"):
        br.replace_with("\n")
    return [clean_text(line) for line in cell.get_text().split("\n") if clean_text(line)]


def _detail_rows(soup):
    home = soup.find(id="modal_home") or soup
    table = home.find("table", class_="case-details-table")
    if table is None:
        raise PortalError("mphc.gov.in case page had no details table")

    header_text = ""
    rows = {}
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label = clean_text(cells[0].get_text(" ")).rstrip(":")
        if not label:
            header_text = clean_text(cells[1].get_text(" "))
            continue
        rows[label.lower()] = cells[1]
    return header_text, rows


def _parties(cell):
    parties = []
    if cell is None:
        return parties
    for block in cell.select("div.border-bottom"):
        name = block.find("div", class_="fw-semibold")
        if not name:
            continue
        muted = [clean_text(d.get_text(" ")) for d in block.find_all("div", class_="text-muted")]
        relation = ""
        if muted and muted[0].upper().startswith("S/D/W/THRU"):
            relation = re.sub(r"^S/D/W/Thru:\s*", "", muted.pop(0), flags=re.I)
        parties.append({
            "name": clean_text(name.get_text(" ")),
            "advocate": "",
            "relation": relation,
            "address": " ".join(muted).replace("�", "-"),
        })
    return parties


def _advocates(cell):
    if cell is None:
        return []
    names = []
    for span in cell.find_all("span", class_="fw-semibold"):
        name = ENROLMENT_RE.sub("", clean_text(span.get_text(" ")))
        if name:
            names.append(name)
    return names


def _acts(cell):
    acts = []
    if cell is None:
        return acts
    for line in _cell_lines(cell):
        match = ACT_RE.match(line)
        if match:
            acts.append({"act": clean_text(match.group(1)), "section": clean_text(match.group(2))})
        else:
            acts.append({"act": clean_text(re.sub(r"^\d+\s*-\s*", "", line)), "section": ""})
    return acts


def _category(cell):
    if cell is None:
        return {}
    labels = ("Category", "Sub Category", "Sub Sub Category")
    parts = [clean_text(s.get_text(" ")) for s in cell.select("div.flex-wrap > span")
             if "text-muted" not in (s.get("class") or [])]
    parts = [p for p in parts if p]
    return {labels[i] if i < len(labels) else f"Level {i + 1}": part
            for i, part in enumerate(parts)}


def _value(rows, *labels):
    for label in labels:
        cell = rows.get(label.lower())
        if cell is not None:
            value = meaningful(cell.get_text(" "))
            if value:
                return value
    return ""


def parse_details(page):
    soup = _soup(page)
    header_text, rows = _detail_rows(soup)

    case_no_text = _value(rows, "Case No.")
    case_match = CASE_NO_RE.search(case_no_text)
    cnr_match = CNR_RE.search(case_no_text)

    status_text = _value(rows, "Status")
    disposed = status_text.lower().startswith("disposed") or (
        "disposed" in header_text.lower() and not status_text.lower().startswith("pending"))
    disposal_match = DISPOSAL_DATE_RE.search(status_text)

    stage_cell = rows.get("stage")
    stage_text, bench = "", ""
    if stage_cell is not None:
        bench_span = stage_cell.find("span")
        if bench_span is not None:
            bench_match = BENCH_RE.search(clean_text(bench_span.get_text(" ")))
            bench = clean_text(bench_match.group(1)) if bench_match else ""
            bench_span.extract()
        stage_text = " ".join(_cell_lines(stage_cell))

    listed_cell = rows.get("last listed on")
    last_listed, last_coram, connected_with = "", "", ""
    if listed_cell is not None:
        spans = [clean_text(s.get_text(" ")) for s in listed_cell.find_all("span")]
        last_listed = normalize_date(spans[0]) if spans else ""
        last_coram = spans[1] if len(spans) > 1 else ""
        connected = LISTED_RE.search(clean_text(listed_cell.get_text(" ")))
        connected_with = clean_text(connected.group(1)) if connected else ""

    list_before_cell = rows.get("list before")
    list_before = ""
    if list_before_cell is not None:
        for font in list_before_cell.find_all("font"):
            font.decompose()
        list_before = meaningful(list_before_cell.get_text(" "))

    petitioners = _parties(rows.get("petitioner(s)"))
    respondents = _parties(rows.get("respondent(s)"))
    pet_advocates = _advocates(rows.get("petitioner advocate(s)"))
    res_advocates = _advocates(rows.get("respondent. advocate(s)") or rows.get("respondent advocate(s)"))
    if petitioners and pet_advocates:
        petitioners[0]["advocate"] = ", ".join(pet_advocates)
    if respondents and res_advocates:
        respondents[0]["advocate"] = ", ".join(res_advocates)

    acts = _acts(rows.get("act"))
    under_section = _value(rows, "U/Section")
    citation = CITATION_RE.search(header_text)

    return {
        "case_type_name": clean_text(case_match.group(1)) if case_match else "",
        "CaseType": clean_text(case_match.group(2)) if case_match else "",
        "reg_no": case_match.group(3) if case_match else "",
        "reg_year": case_match.group(4) if case_match else "",
        "RegistrationDate": normalize_date(
            case_no_text.split("Registered On", 1)[1] if "Registered On" in case_no_text else ""),
        "CNRNumber": cnr_match.group(1) if cnr_match else "",
        "disposed": disposed,
        "status_text": status_text,
        "DecisionDate": disposal_match.group(1) if disposal_match else "",
        "NatureofDisposal": _value(rows, "Disp.Type", "Disp. Type") if disposed else "",
        "stage_text": stage_text,
        "BenchType": bench,
        "last_listed": last_listed,
        "last_coram": last_coram,
        "connected_with": connected_with,
        "list_before": list_before,
        "last_order": _value(rows, "Last Order"),
        "tentative_date": normalize_date(_value(rows, "Tentative date")),
        "statutory_info": _value(rows, "Statutory Info.").strip("[] "),
        "District": _value(rows, "District"),
        "caveat_no": _value(rows, "Caveat No."),
        "bail_no": _value(rows, "Bail No."),
        "neutral_citation": citation.group(0) if citation else "",
        "petitioners": petitioners,
        "respondents": respondents,
        "acts": acts,
        "under_section": under_section,
        "category_details": _category(rows.get("category")),
    }


def _table_rows(fragment):
    soup = _soup(fragment)
    table = soup.find("table")
    if table is None:
        return []
    body = table.find("tbody") or table
    return [row for row in body.find_all("tr") if not row.find("th")]


def parse_listing(fragment):
    entries = []
    for row in _table_rows(fragment):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        date = normalize_date(cells[0].get_text(" "))
        if not date:
            continue
        coram = ", ".join(clean_text(d.get_text(" ")) for d in cells[1].find_all("div")) \
            or clean_text(cells[1].get_text(" "))
        purpose_div = cells[2].find("div")
        sub = cells[2].find("small")
        action = clean_text(cells[3].get_text(" ")).rstrip("-").strip()
        entries.append({
            "date": date,
            "coram": coram,
            "purpose": clean_text(purpose_div.get_text(" ")) if purpose_div else clean_text(cells[2].get_text(" ")),
            "list_type": clean_text(sub.get_text(" ")) if sub else "",
            "action": "" if action in ("", "-") else action,
        })
    return entries


def _date_key(value):
    day, month, year = value.split("-")
    return f"{year}{month}{day}"


def _best_listing(rows):
    def score(row):
        return (
            1 if row["action"] else 0,
            1 if "JUSTICE" in row["coram"].upper() else 0,
        )
    return max(rows, key=score)


def build_case_history(listing, next_date):
    by_date = {}
    order = []
    for row in listing:
        if row["date"] not in by_date:
            by_date[row["date"]] = []
            order.append(row["date"])
        by_date[row["date"]].append(row)

    dates = sorted(order, key=_date_key)
    history = []
    for index, date in enumerate(dates):
        row = _best_listing(by_date[date])
        if index + 1 < len(dates):
            hearing = dates[index + 1]
        elif next_date and _date_key(next_date) > _date_key(date):
            hearing = next_date
        else:
            hearing = ""
        history.append({
            "causeListType": row["list_type"],
            "judge": row["coram"],
            "businessOnDate": date,
            "hearingDate": hearing,
            "purpose": row["purpose"],
            "action": row["action"],
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    return history


def parse_orders(fragment, fil_no):
    soup = _soup(fragment)
    found = []
    seen_hrefs = set()
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if "/order/download/" not in href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        spans = [clean_text(s.get_text(" ")) for s in link.find_all("span")]
        date = normalize_date(spans[0]) if spans else normalize_date(link.get_text(" "))
        label = spans[1] if len(spans) > 1 else "Order"
        found.append((date, label, href))

    found.sort(key=lambda item: _date_key(item[0]) if item[0] else "")
    occurrences = {}
    orders = []
    for number, (date, label, href) in enumerate(found, start=1):
        key = (date, label.lower())
        occurrences[key] = occurrences.get(key, 0) + 1
        lowered = label.lower()
        orders.append({
            "order_number": str(number),
            "order_on": label,
            "judge": "",
            "order_date": date,
            "order_type": "final" if ("final" in lowered or "judg" in lowered) else "order",
            "href": href,
            "source_ref": f"{NAME}:{fil_no}:{date}:{lowered}:{occurrences[key]}",
        })
    return orders


def parse_ia(fragment):
    items = []
    for row in _table_rows(fragment):
        cells = [clean_text(c.get_text(" ")) for c in row.find_all("td")]
        if len(cells) < 6:
            continue
        items.append({
            "iaNumber": cells[0] or cells[4],
            "serialNumber": cells[4],
            "classification": cells[1],
            "party": cells[2],
            "dateOfFiling": normalize_date(cells[3]),
            "nextDate": "",
            "status": cells[5],
        })
    return items


def parse_connected(fragment, own_case):
    matters = []
    own = re.sub(r"\s+", "", own_case or "").upper()
    for row in _table_rows(fragment):
        cells = [clean_text(c.get_text(" ")) for c in row.find_all("td")]
        if len(cells) < 2 or not cells[1]:
            continue
        if re.sub(r"\s+", "", cells[1]).upper() == own:
            continue
        matters.append(f"{cells[1]} ({cells[0]})" if cells[0] else cells[1])
    return matters


def parse_earlier_court(fragment):
    soup = _soup(fragment)
    info = {}
    for row in soup.find_all("tr"):
        head = row.find("th")
        cell = row.find("td")
        if head is None or cell is None:
            continue
        info[clean_text(head.get_text(" "))] = meaningful(cell.get_text(" "))
    if not re.search(r"[1-9]", info.get("Case Number", "")):
        return {}
    return info


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    if str(int(court_code)) not in BENCHES:
        raise PortalCaseNotFound(f"unknown MP High Court bench {court_code!r}")

    token, _ = _csrf(session)
    fil_no = build_fil_no(court_code, case_type, case_reg_no, rgyear)
    page = _fetch_modal(session, token, fil_no) if fil_no else None

    if page is None:
        fil_no, token = _search_fil_no(session, token, court_code, case_type, case_reg_no, rgyear)
        page = _fetch_modal(session, token, fil_no) if fil_no else None

    if page is None:
        raise PortalCaseNotFound(
            f"mphc.gov.in has no case type {case_type}, number {case_reg_no}, "
            f"year {rgyear} at bench {court_code}")

    details = parse_details(page)
    own_case = f"{details['CaseType']} {details['reg_no']}/{details['reg_year']}"

    listing = parse_listing(_fetch_tab(session, token, fil_no, "listing") or "")
    orders = parse_orders(_fetch_tab(session, token, fil_no, "judgement") or "", fil_no)
    ia_details = parse_ia(_fetch_tab(session, token, fil_no, "ia") or "")
    connected = parse_connected(_fetch_tab(session, token, fil_no, "connected") or "", own_case)
    earlier = parse_earlier_court(_fetch_tab(session, token, fil_no, "ecourt") or "")

    disposed = details["disposed"]
    tentative = details["tentative_date"]
    today_key = time.strftime("%Y%m%d")
    next_date = "" if disposed or not tentative or _date_key(tentative) < today_key else tentative
    history = build_case_history(listing, next_date)
    acts = details["acts"]
    if not acts and details["under_section"]:
        acts = [{"act": "", "section": details["under_section"]}]

    reg_no = details["reg_no"] or str(int(case_reg_no))
    reg_year = details["reg_year"] or str(rgyear)
    coram = details["list_before"] or details["last_coram"]

    if disposed:
        case_status = "Disposed"
    else:
        case_status = details["stage_text"] or details["status_text"]

    metadata = {
        "case_no": ecourts_case_no(case_type, reg_no, reg_year),
        "case_number": f"{details['CaseType']}/{reg_no}/{reg_year}" if details["CaseType"] else "",
        "cino": details["CNRNumber"],
        "CaseType": details["CaseType"],
        "CaseTypeName": details["case_type_name"],
        "FilingNumber": "",
        "FilingDate": "",
        "RegistrationNumber": f"{reg_no}/{reg_year}",
        "RegistrationDate": details["RegistrationDate"],
        "CNRNumber": details["CNRNumber"],
        "FirstHearingDate": history[0]["businessOnDate"] if history else "",
        "NextHearingDate": next_date,
        "LastListedDate": details["last_listed"],
        "LastOrder": details["last_order"],
        "DecisionDate": details["DecisionDate"] if disposed else "",
        "CaseStatus": case_status,
        "PortalStatus": details["status_text"],
        "Stage": details["stage_text"],
        "NatureofDisposal": (details["NatureofDisposal"] or "Disposed") if disposed else "",
        "CourtNumberandJudge": coram,
        "BenchType": details["BenchType"],
        "BenchName": BENCHES[str(int(court_code))],
        "JudicialBranch": "",
        "State": "MADHYA PRADESH",
        "District": details["District"],
        "NotBeforeMe": "",
        "NeutralCitation": details["neutral_citation"],
        "StatutoryInfo": details["statutory_info"],
        "ConnectedWith": details["connected_with"],
        "CaveatNumber": details["caveat_no"],
        "BailNumber": details["bail_no"],
        "petitioner_and_advocate": [p["name"] for p in details["petitioners"]],
        "respondent_and_advocate": [p["name"] for p in details["respondents"]],
        "petitioner_details": details["petitioners"],
        "respondent_details": details["respondents"],
        "actsandSection": {
            "acts": ", ".join(a["act"] for a in acts if a["act"]),
            "section": ", ".join(a["section"] for a in acts if a["section"]),
        } if acts else {},
        "acts": acts,
        "category_details": details["category_details"],
        "subordinate_court_information": earlier,
        "sub_matters": connected,
        "ia_details": ia_details,
        "case_history": history,
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": fil_no,
    }

    if not (metadata["cino"] or metadata["case_history"] or details["CaseType"]):
        raise PortalError(f"mphc.gov.in returned an empty case page for {fil_no}")

    return metadata, orders
