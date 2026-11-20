import base64
import io
import json
import random
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup
from PIL import Image

from core.lambda_client import lambda_client
from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_no,
    ecourts_case_type_name,
    meaningful,
    short_case_code,
    slash_to_dash,
    squash,
)
from helpers.solve_captcha import solve_captcha

NAME = "rhc"
LABEL = "Rajasthan High Court"
STATE_CODE = "9"
BENCHES = {"1": "Jaipur", "2": "Jodhpur"}
PROVIDES_HISTORY = False

ORIGIN = "https://hcraj.nic.in"
CASE_PATHS = {"1": "cishcraj-jp", "2": "cishcraj-jdp"}
CNR_BENCH = {"RJHC02": "1", "RJHC01": "2"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 90)
MAX_RETRIES = 3
MAX_CODE_ATTEMPTS = 5
TYPES_TTL_SECONDS = 6 * 3600
TABLES = ("1", "2")

_types_cache = {}


def open_session():
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT})
    return session


def _b64(value):
    return base64.b64encode(str(value).encode()).decode("ascii")


def _post(session, url, data, referer=None):
    headers = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded"}
    if referer:
        headers["Referer"] = referer
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.2, 0.5))
        try:
            response = session.post(url, data=data, headers=headers, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[rhc] POST {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise PortalError(f"hcraj.nic.in {url} returned HTTP {response.status_code}")
        return response.text
    raise PortalError(f"hcraj.nic.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _html(text):
    text = (text or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    return data.get("html", "") if isinstance(data, dict) else str(data or "")


def _base(court_code):
    bench = str(court_code or "1")
    if bench not in CASE_PATHS:
        raise PortalCaseNotFound(f"unknown Rajasthan High Court bench {court_code!r}")
    return f"{ORIGIN}/{CASE_PATHS[bench]}"


def portal_case_types(session, base):
    cached = _types_cache.get(base)
    if cached and time.time() - cached["at"] < TYPES_TTL_SECONDS:
        return cached["values"]
    values = {}
    for category in ("1", "2", "3"):
        page = _post(session, f"{base}/filters/getajaxdata",
                     f"basetype={_b64(category)}&queryType=getCaseTpyes&_csrfToken=false")
        for code, label in re.findall(r'value="([^"]*)"[^>]*>([^<]*)<', page):
            if code and code != "0":
                values[code] = {"category": category, "name": clean_text(label)}
    if not values:
        raise PortalError("hcraj.nic.in case type list could not be read")
    _types_cache[base] = {"values": values, "at": time.time()}
    return values


def resolve_case_type(session, base, court_code, case_type):
    types = portal_case_types(session, base)
    raw = clean_text(str(case_type or ""))
    ecourts_name = ecourts_case_type_name(STATE_CODE, court_code, raw) if raw.isdigit() else raw
    long_name = re.sub(r"^[^(]*\(|\)$", "", ecourts_name) if "(" in ecourts_name else ecourts_name
    if raw.isdigit() and raw in types and squash(types[raw]["name"]) == squash(long_name):
        return raw, types[raw], short_case_code(ecourts_name)
    for code, item in types.items():
        if squash(item["name"]) == squash(long_name):
            return code, item, short_case_code(ecourts_name)
    if raw.isdigit() and raw in types:
        return raw, types[raw], short_case_code(ecourts_name)
    return "", None, ""


def _clean_code_image(raw):
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    out = Image.new("L", image.size, 255)
    src, dst = image.load(), out.load()
    for x in range(image.width):
        for y in range(image.height):
            r, g, b = src[x, y]
            if r > 170 and g > 170 and b > 170:
                dst[x, y] = 0
    buffer = io.BytesIO()
    out.save(buffer, "PNG")
    return buffer.getvalue()


def _read_code(session, base):
    try:
        data = json.loads(_post(session, f"{base}/causelists/setgetcaptcha?{random.random()}", ""))
        raw = base64.b64decode(data["image"])
    except (ValueError, KeyError, TypeError):
        raise PortalError("hcraj.nic.in verification image could not be read")
    text = solve_captcha(lambda_client=lambda_client,
                         image_base64=base64.b64encode(_clean_code_image(raw)).decode("ascii"), frm="hc")
    return (text or "").upper()


def _search_fields(category, code, reg_no, year):
    fields = dict(basetype=category, casetype=code, caseno=reg_no, caseyear=year, diarycno="", diaryyr="0",
                  partytname="", advctname="", adv_code="", filingfromdate="", filingtodate="", depttype="0",
                  deptcode="0", regfromdate="", regtodate="", pstatus="", dstatus="", sbstatus="", dbtatus="",
                  petres="0", prvcno="", prvcyear="0", jshipdt="", judships="0", judshipplace="0",
                  judshipcourt="0", firno="", firyear="0", firdistrict="0", firpolicestation="0", cnr="",
                  case_no="", searchtype="2")
    return "&".join(f"{key}={_b64(value)}" for key, value in fields.items())


def _same_case(label, short_code, reg_no, year):
    parts = [p.strip() for p in re.split(r"\s*/\s*", clean_text(label))]
    if len(parts) < 3:
        return False
    return (squash(parts[0]) == squash(short_code) and parts[1].lstrip("0") == reg_no.lstrip("0")
            and parts[2][:4] == year)


def search_case(session, base, item, code, short_code, reg_no, year):
    session.get(f"{base}/", timeout=TIMEOUT)
    fields = _search_fields(item["category"], code, reg_no, year)
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        answer = _read_code(session, base)
        if len(answer) != 6:
            continue
        body = f"{fields}&captcha={answer}&queryType=getRecord"
        count = clean_text(_post(session, f"{base}/filters/getrecord", body + "&csrfToken=false&requestType=count"))
        if "SecCode" in count:
            print(f"[rhc] verification attempt {attempt} not accepted")
            continue
        if count == "0":
            raise PortalCaseNotFound(f"hcraj.nic.in has no {short_code} {reg_no}/{year}")
        if count == "input_error":
            raise PortalError("hcraj.nic.in rejected the case search")
        page = _html(_post(session, f"{base}/filters/getrecord", body + "&requestType=data"))
        if "SecCode" in page[:50]:
            continue
        for row in BeautifulSoup(page, "html.parser").select("tbody tr"):
            link = row.find(attrs={"data-target": True})
            cells = row.find_all("td")
            if not link or len(cells) < 2:
                continue
            label = clean_text(cells[1].get_text(" ")).split("URN:")[0]
            if _same_case(label, short_code, reg_no, year):
                cino, _, table = link["data-target"].partition("~")
                return cino, table
        raise PortalCaseNotFound(f"hcraj.nic.in search did not return {short_code} {reg_no}/{year}")
    raise PortalError(f"hcraj.nic.in case search was not accepted after {MAX_CODE_ATTEMPTS} attempts")


def load_details(session, base, cino, tables=TABLES):
    for table in tables:
        page = _html(_post(session, f"{base}/filings/casedtlpartone",
                           f"cino={_b64(cino)}&tb_type={_b64(table)}&QueryType=UsingCaseNo"))
        if page and "CNR" in page:
            return page
    return ""


def _labelled(soup):
    values = {}
    for cell in soup.find_all("td"):
        if cell.find("table"):
            continue
        text = clean_text(cell.get_text(" "))
        if ":" not in text:
            continue
        label, _, value = text.partition(":")
        label = clean_text(label).rstrip(".").lower()
        if label and label not in values:
            values[label] = clean_text(value)
    return values


def _tables(soup):
    found = {}
    for table in soup.find_all("table"):
        if table.find("table"):
            continue
        rows = [[clean_text(c.get_text(" ")) for c in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if rows and len(rows[0]) == 1 and len(rows) > 1:
            found[rows[0][0].lower()] = rows[1:]
    return found


def _long_date(value):
    text = clean_text(value)
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return ""


def build_orders(session, base, soup):
    found = []
    for row in soup.find_all("tr"):
        button = row.find(attrs={"data-filename": True})
        cells = row.find_all("td")
        if not button or len(cells) < 3:
            continue
        kind = clean_text(cells[1].get_text(" "))
        filename, year = button["data-filename"], button.get("data-fileyear", "")
        href = ""
        try:
            path = clean_text(_post(session, f"{base}/DwonloadOrdJud.php",
                                    f"filename={_b64(filename)}&fileyear={_b64(year)}"))
            if path and path != "NAN" and not path.startswith("<"):
                href = f"{base}/{path.lstrip('/')}"
        except PortalError as exc:
            print(f"[rhc] order link failed for {filename}: {exc}")
        citation = re.search(r"\(([^)]*:[^)]*)\)", kind)
        found.append({
            "date": _long_date(cells[2].get_text(" ")),
            "kind": kind,
            "citation": citation.group(1) if citation else "",
            "href": href,
            "ref": filename,
        })
    found.sort(key=lambda o: dmy_key(o["date"]))
    return [{
        "order_number": str(number),
        "order_on": o["kind"] or "Order/Judgement",
        "judge": "",
        "order_date": o["date"],
        "order_type": "order",
        "neutral_citation": o["citation"],
        "href": o["href"],
        "source_ref": f"{NAME}:{o['ref']}",
    } for number, o in enumerate(found, start=1)]


def _more_info(session, base, soup):
    button = soup.find(attrs={"onclick": re.compile(r"LoadMoreInfo")})
    if not button or not button.get("id"):
        return BeautifulSoup("", "html.parser")
    try:
        page = _html(_post(session, f"{base}/filings/casedtlparttwo", f"casedetails={_b64(button['id'])}"))
    except PortalError as exc:
        print(f"[rhc] more information failed: {exc}")
        page = ""
    return BeautifulSoup(page, "html.parser")


def _advocates(rows, side):
    return ", ".join(r[1] for r in rows if len(r) >= 2 and r[0].upper() == side and r[1] and r[1] != "Adv Name")


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    bench = str(court_code or "1")
    base = _base(bench)
    reg_no, year = str(int(case_reg_no)), str(rgyear)
    code, item, short_code = resolve_case_type(session, base, bench, case_type)
    if not item:
        raise PortalCaseNotFound(f"no Rajasthan High Court case type matches eCourts type {case_type!r}")

    page = ""
    known = clean_text(cino).upper()
    if re.fullmatch(r"RJHC\d{12}", known) and CNR_BENCH.get(known[:6], bench) == bench:
        page = load_details(session, base, known)
    if not page:
        found_cino, table = search_case(session, base, item, code, short_code, reg_no, year)
        page = load_details(session, base, found_cino, (table,) + tuple(t for t in TABLES if t != table))
    if not page:
        raise PortalCaseNotFound(f"hcraj.nic.in returned no details for {short_code} {reg_no}/{year}")

    soup = BeautifulSoup(page, "html.parser")
    values = _labelled(soup)
    registration = values.get("reg. details", "")
    if registration and not _same_case(registration, short_code, reg_no, year):
        raise PortalCaseNotFound(f"hcraj.nic.in returned {registration} instead of {short_code} {reg_no}/{year}")

    first_row = clean_text(soup.find("td").get_text(" ")) if soup.find("td") else ""
    status = first_row.upper() if ":" not in first_row else ""
    decision = slash_to_dash(values.get("disposal date"))
    disposed = "DISPOS" in status or bool(decision)

    today_key = date.today().strftime("%Y%m%d")
    listing = slash_to_dash(values.get("date of listing"))
    next_date = "" if disposed or not listing or dmy_key(listing) < today_key else listing

    more = _more_info(session, base, soup)
    extra = _tables(more)
    advocates = extra.get("advocate details", [])

    petitioner = meaningful(values.get("petitioner"))
    respondent = meaningful(values.get("respondent"))
    petitioners = [{"name": petitioner, "relation": "",
                    "advocate": _advocates(advocates, "P") or meaningful(values.get("petitioner advocate")),
                    "address": ""}] if petitioner else []
    respondents = [{"name": respondent, "relation": "",
                    "advocate": _advocates(advocates, "R") or meaningful(values.get("respondent advocate")),
                    "address": ""}] if respondent else []

    lower = {}
    for row in extra.get("lower court details", []):
        if len(row) >= 5 and row[0] != "Case No.":
            lower = {"Case Number": row[0], "District": row[1], "Place": row[2], "Court": row[3],
                     "Decision Date": slash_to_dash(row[4]) or row[4]}
            break
    fir = next((r for r in extra.get("fir details", []) if len(r) >= 3 and r[0] != "FIR NO."), None)
    if fir:
        lower.update({"FIR Number": fir[0], "Police Station": fir[2], "FIR District": fir[1]})

    connected = [f"{r[0]} (Reg. {r[1]})" if meaningful(r[1].replace("/", "")) else r[0]
                 for r in extra.get("cases in which this is main case", [])
                 if len(r) >= 2 and r[0] != "Filling Number"]

    department = next((r for r in extra.get("department details", []) if len(r) >= 3
                       and r[0] != "Department Type"), None)

    orders = build_orders(session, base, soup)
    citations = [o["neutral_citation"] for o in orders if o["neutral_citation"]]
    found_cino = (re.search(r"RJHC\d{12}", values.get("cnr", "")) or re.search(r"RJHC\d{12}", page))
    stage = meaningful(values.get("stage"))
    court_no = "" if disposed else (meaningful(values.get("listed in court no")) or meaningful(values.get("court no")))

    metadata = {
        "case_no": ecourts_case_no(case_type, reg_no, year) if str(case_type).isdigit() else "",
        "case_number": f"{short_code}/{reg_no}/{year}",
        "cino": found_cino.group(0) if found_cino else "",
        "CaseType": short_code,
        "CaseTypeName": item["name"],
        "FilingNumber": meaningful(values.get("filing details")),
        "FilingDate": slash_to_dash(values.get("filed on")),
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": slash_to_dash(values.get("registered on")),
        "CNRNumber": found_cino.group(0) if found_cino else "",
        "URN": meaningful(values.get("urn")),
        "FirstHearingDate": "",
        "NextHearingDate": next_date,
        "LastListedDate": "",
        "DecisionDate": decision if disposed else "",
        "CaseStatus": "Disposed" if disposed else (stage.title() if stage else (status.title() or "Pending")),
        "PortalStatus": status,
        "Stage": stage,
        "NatureofDisposal": (meaningful(values.get("remark")) or "Disposed") if disposed else "",
        "CourtNumberandJudge": f"Court No. {court_no}" if court_no else "",
        "BenchType": meaningful(values.get("bench")),
        "BenchName": BENCHES[bench],
        "JudicialBranch": "",
        "State": "RAJASTHAN",
        "District": lower.get("District", ""),
        "NotBeforeMe": "",
        "NeutralCitation": citations[-1] if citations else "",
        "petitioner_and_advocate": [p["name"] for p in petitioners],
        "respondent_and_advocate": [p["name"] for p in respondents],
        "petitioner_details": petitioners,
        "respondent_details": respondents,
        "actsandSection": {},
        "acts": [],
        "category_details": {"Department": department[2], "Department Type": department[0]} if department else {},
        "subordinate_court_information": lower,
        "sub_matters": connected,
        "ia_details": [],
        "case_history": [],
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": f"{bench}|{code}|{reg_no}|{year}",
    }
    if disposed and orders:
        orders[-1]["order_type"] = "final"
    return metadata, orders
