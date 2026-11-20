import base64
import html
import random
import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_type_name,
    match_portal_case_type,
    meaningful,
)

NAME = "mlhc"
LABEL = "Meghalaya High Court"
STATE_CODE = "21"
BENCHES = {"1": "Shillong"}
PROVIDES_HISTORY = True

ORIGIN = "https://meghalayahighcourt.nic.in"
CASE_STATUS_URL = ORIGIN + "/case-status"
QUESTION_URL = ORIGIN + "/math-captcha/image"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 60)
MAX_RETRIES = 3
SEARCH_ATTEMPTS = 3
TYPES_TTL_SECONDS = 6 * 3600
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "Referer": CASE_STATUS_URL}
SEARCH_LABEL = "\U0001F50D Search"

DATE_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")
QUESTION_RE = re.compile(r"(\d+)\s*([+\-xX×*])\s*(\d+)")
BUILD_ID_RE = re.compile(r'name="form_build_id" value="([^"]+)"')
CASE_LINK_RE = re.compile(r'href="/case-status/([^"]+)"[^>]*>\s*([^<]+?)\s*</a>')
DISPOSAL_WORDS = re.compile(
    r"DISPOS|DISMISS|ALLOW|WITHDRAW|REJECT|DECIDED|ABATE|QUASH|CLOSED|ACQUIT|CONVICT|SETTLED|DECREED", re.I)

_types_cache = {"values": [], "at": 0.0}


def normalize_date(value):
    match = DATE_RE.search(value or "")
    return "-".join(match.groups()) if match else ""


def open_session():
    session = requests.Session()
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
            print(f"[mlhc] {method} {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise PortalError(f"meghalayahighcourt.nic.in {url} returned HTTP {response.status_code}")
        return response
    raise PortalError(f"meghalayahighcourt.nic.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _ajax_commands(response):
    try:
        commands = response.json()
    except ValueError:
        raise PortalError("meghalayahighcourt.nic.in did not return an AJAX response")
    return {c.get("selector"): c.get("data") or "" for c in commands
            if isinstance(c, dict) and c.get("command") == "insert"}


def portal_case_types(session, page=None):
    if _types_cache["values"] and time.time() - _types_cache["at"] < TYPES_TTL_SECONDS:
        return _types_cache["values"]
    page = page or _request(session, "GET", CASE_STATUS_URL).text
    start = page.find('name="case_type"')
    end = page.find("</select>", start)
    values = [html.unescape(v) for v in re.findall(r'<option value="([^"]+)"', page[start:end]) if v]
    if not values:
        raise PortalError("meghalayahighcourt.nic.in case type list could not be read")
    _types_cache.update(values=values, at=time.time())
    return values


def resolve_case_type(session, case_type, page=None):
    types = portal_case_types(session, page)
    raw = clean_text(str(case_type or ""))
    if raw in types:
        return raw
    name = ecourts_case_type_name(STATE_CODE, "1", raw) if raw.isdigit() else raw
    return match_portal_case_type(name, types) if name else ""


def _answer(question):
    match = QUESTION_RE.search(question or "")
    if not match:
        raise PortalError("meghalayahighcourt.nic.in served an unreadable search check")
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    return left * right


def search_case_link(session, case_type, case_reg_no, rgyear):
    last_error = None
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        page = _request(session, "GET", CASE_STATUS_URL).text
        build_id = BUILD_ID_RE.search(page)
        if not build_id:
            raise PortalError("meghalayahighcourt.nic.in search form could not be read")
        portal_type = resolve_case_type(session, case_type, page)
        if not portal_type:
            raise PortalCaseNotFound(f"no Meghalaya High Court case type matches eCourts type {case_type!r}")

        question = _request(session, "GET", QUESTION_URL).json().get("question")
        response = _request(
            session, "POST", CASE_STATUS_URL,
            params={"ajax_form": 1, "_wrapper_format": "drupal_ajax"},
            data={
                "qry": "case",
                "case_type": portal_type,
                "reg_no": str(int(case_reg_no)),
                "year": str(rgyear),
                "ptyname": "",
                "advname": "",
                "captcha[captcha_answer]": str(_answer(question)),
                "form_build_id": build_id.group(1),
                "form_id": "case_status_form1",
                "_triggering_element_name": "op",
                "_triggering_element_value": SEARCH_LABEL,
            },
            headers=AJAX,
        )
        blocks = _ajax_commands(response)
        table = blocks.get(".cstable", "")
        if not table:
            last_error = "search check was not accepted"
            print(f"[mlhc] search attempt {attempt}/{SEARCH_ATTEMPTS}: {last_error}")
            continue

        wanted = clean_text(f"{portal_type}/{int(case_reg_no)}/{rgyear}").replace(" ", "").upper()
        for token, label in CASE_LINK_RE.findall(table):
            if clean_text(label).replace(" ", "").upper() == wanted:
                return token
        raise PortalCaseNotFound(f"meghalayahighcourt.nic.in has no {portal_type} {case_reg_no}/{rgyear}")

    raise PortalError(f"meghalayahighcourt.nic.in search failed: {last_error}")


def cnr_token(cino):
    return quote(base64.b64encode(cino.encode("ascii")).decode("ascii"), safe="")


def _rows(fragment):
    soup = BeautifulSoup(fragment or "", "html.parser")
    body = soup.find("tbody") or soup
    return [[clean_text(td.get_text(" ")) for td in tr.find_all("td")] for tr in body.find_all("tr")
            if tr.find("td")]


def _links(fragment):
    soup = BeautifulSoup(fragment or "", "html.parser")
    body = soup.find("tbody") or soup
    out = []
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        link = tr.find("a", href=True)
        out.append(([clean_text(td.get_text(" ")) for td in cells], link["href"].strip() if link else ""))
    return out


def _split(value, sep):
    return [clean_text(part) for part in (value or "").split(sep)]


def _parties(fragment):
    parties = []
    for cells in _rows(fragment):
        if not cells or not meaningful(cells[0]) or cells[0].lower() == "no data found":
            continue
        parties.append({"name": cells[0], "advocate": cells[1] if len(cells) > 1 else "",
                        "relation": "", "address": ""})
    return parties


def parse_details(blocks):
    details = _rows(blocks.get(".case_detials"))
    status = _rows(blocks.get(".cs_status"))
    category = _rows(blocks.get(".cat_detials"))
    if not details:
        raise PortalCaseNotFound("meghalayahighcourt.nic.in returned no case details")

    type_cnr = _split(details[0][0], "/")
    filing = _split(details[0][1] if len(details[0]) > 1 else "", ":")
    registration = _split(details[0][2] if len(details[0]) > 2 else "", ":")

    status_cells = status[0] if status else ["", "", ""]
    date_part, _, stage = (status_cells[0] or "").partition("/")
    branch = _split(status_cells[2] if len(status_cells) > 2 else "", "/")

    history = []
    for cells in _rows(blocks.get(".daily_proc")):
        if len(cells) < 3:
            continue
        cause_list, _, judge = cells[0].partition("~")
        business, _, hearing = cells[1].partition("~")
        history.append({
            "causeListType": clean_text(cause_list),
            "judge": clean_text(judge),
            "businessOnDate": normalize_date(business),
            "hearingDate": normalize_date(hearing),
            "purpose": cells[2],
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    history = [h for h in history if h["businessOnDate"] or h["hearingDate"]]
    history.sort(key=lambda h: dmy_key(h["businessOnDate"] or h["hearingDate"]))

    orders = []
    for cells, href in _links(blocks.get(".orders")):
        if not href or len(cells) < 3:
            continue
        orders.append({
            "order_number": cells[0],
            "order_on": "Order",
            "judge": cells[1],
            "order_date": normalize_date(cells[2]),
            "order_type": "order",
            "href": href,
            "source_ref": f"{NAME}:{href.rsplit('/', 1)[-1]}",
        })

    decree = _rows(blocks.get(".decree"))
    decree_cells = decree[0] if decree and decree[0] and decree[0][0].lower() != "no data found" else []

    return {
        "portal_type": type_cnr[0] if type_cnr else "",
        "cnr": type_cnr[1] if len(type_cnr) > 1 else "",
        "filing_no": filing[0].replace(" ", "") if filing else "",
        "filing_date": normalize_date(filing[1] if len(filing) > 1 else ""),
        "reg_no": registration[0].replace(" ", "") if registration else "",
        "reg_date": normalize_date(registration[1] if len(registration) > 1 else ""),
        "status_date": normalize_date(date_part),
        "stage": clean_text(stage),
        "coram": clean_text(re.sub(r"^\d+\s*-\s*", "", status_cells[1] if len(status_cells) > 1 else "")),
        "branch": branch[0] if branch else "",
        "bench": branch[1] if len(branch) > 1 else "",
        "cause_list": branch[2] if len(branch) > 2 else "",
        "category": category[0][0] if category and category[0] else "",
        "sub_category": category[0][1] if category and len(category[0]) > 1 else "",
        "petitioners": _parties(blocks.get(".pet_dtl")),
        "respondents": _parties(blocks.get(".res_dtl")),
        "history": history,
        "orders": orders,
        "judgment_reserved_on": normalize_date(decree_cells[0]) if decree_cells else "",
        "operative_part": decree_cells[1] if len(decree_cells) > 1 else "",
        "pronounced_on": normalize_date(decree_cells[2]) if len(decree_cells) > 2 else "",
    }


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    token = cnr_token(cino) if cino and re.fullmatch(r"MLHC\d{12}", cino) else None
    if token is None:
        token = search_case_link(session, case_type, case_reg_no, rgyear)
    else:
        _request(session, "GET", CASE_STATUS_URL)

    response = _request(session, "GET", f"{CASE_STATUS_URL}/{token}",
                        params={"_wrapper_format": "drupal_ajax"}, headers=AJAX)
    info = parse_details(_ajax_commands(response))

    wanted = f"{int(case_reg_no)}/{rgyear}"
    if info["reg_no"] and info["reg_no"] != wanted:
        raise PortalCaseNotFound(f"meghalayahighcourt.nic.in CNR points at {info['reg_no']}, not {wanted}")

    disposed = bool(DISPOSAL_WORDS.search(info["stage"]))
    if disposed and info["history"]:
        info["history"][-1]["hearingDate"] = ""
    for order in info["orders"]:
        if disposed and order["order_date"] and order["order_date"] == info["status_date"]:
            order["order_type"] = "final"
            order["order_on"] = "Final Order"

    petitioners, respondents = info["petitioners"], info["respondents"]
    reg_no, year = wanted.split("/")
    metadata = {
        "case_number": f"{info['portal_type']}/{reg_no}/{year}",
        "cino": info["cnr"],
        "CaseType": info["portal_type"],
        "FilingNumber": info["filing_no"],
        "FilingDate": info["filing_date"],
        "RegistrationNumber": wanted,
        "RegistrationDate": info["reg_date"],
        "CNRNumber": info["cnr"],
        "FirstHearingDate": next((h["businessOnDate"] or h["hearingDate"] for h in info["history"]), ""),
        "NextHearingDate": "" if disposed else info["status_date"],
        "DecisionDate": info["status_date"] if disposed else "",
        "CaseStatus": "Disposed" if disposed else (info["stage"] or "Pending"),
        "PortalStatus": info["stage"],
        "NatureofDisposal": info["stage"] if disposed else "",
        "CourtNumberandJudge": info["coram"],
        "BenchType": info["bench"],
        "BenchName": BENCHES["1"],
        "JudicialBranch": info["branch"],
        "CauseListType": info["cause_list"],
        "State": "MEGHALAYA",
        "District": "",
        "NotBeforeMe": "",
        "JudgmentReservedOn": info["judgment_reserved_on"],
        "JudgmentPronouncedOn": info["pronounced_on"],
        "OperativePart": info["operative_part"],
        "petitioner_and_advocate": [p["name"] for p in petitioners],
        "respondent_and_advocate": [p["name"] for p in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": {},
        "acts": [],
        "category_details": {k: v for k, v in (("Category", info["category"]),
                                                ("Sub Category", info["sub_category"])) if v},
        "subordinate_court_information": {},
        "sub_matters": [],
        "ia_details": [],
        "case_history": info["history"],
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": info["cnr"],
    }
    return metadata, info["orders"]
