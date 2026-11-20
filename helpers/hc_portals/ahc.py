import base64
import html
import io
import random
import re
import time

import requests
from bs4 import BeautifulSoup
from PIL import Image

from core.lambda_client import lambda_client
from helpers.hc_portals.base import (
    PortalCaseNotFound,
    PortalError,
    clean_text,
    dmy_key,
    ecourts_case_type_name,
    match_portal_case_type,
    meaningful,
)
from helpers.solve_captcha import solve_captcha

NAME = "ahc"
LABEL = "Allahabad High Court"
STATE_CODE = "13"
BENCHES = {"1": "Allahabad", "2": "Lucknow"}
PROVIDES_HISTORY = True

BENCH_BASES = {
    "1": "https://www.allahabadhighcourt.in/apps/status_ccms/index.php",
    "2": "https://hclko.allahabadhighcourt.in/status/index.php",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = (15, 90)
MAX_RETRIES = 3
SEARCH_ATTEMPTS = 6
XHR = {"X-Requested-With": "XMLHttpRequest"}

CAPTCHA_ID_RE = re.compile(r'name="captcha_id" value="([^"]+)"')
VIEW_RE = re.compile(r"viewCaseData\(([^)]*)\)")
ORDER_SHEET_RE = re.compile(r"viewOrderSheet\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")
DATE_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")
ORDINAL_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})")
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
ENROLMENT_RE = re.compile(r"\s*\([^)]*/[^)]*\)\s*")


def normalize_date(value):
    value = clean_text(value)
    match = DATE_RE.search(value)
    if match:
        return "-".join(match.groups())
    match = ORDINAL_RE.search(value)
    if match and match.group(2)[:3].lower() in MONTHS:
        return f"{int(match.group(1)):02d}-{MONTHS[match.group(2)[:3].lower()]:02d}-{match.group(3)}"
    return ""


def open_session():
    session = requests.Session()
    session.headers.update({"user-agent": USER_AGENT})
    return session


def _base(court_code):
    bench = str(int(court_code)) if str(court_code or "").isdigit() else "1"
    if bench not in BENCH_BASES:
        raise PortalCaseNotFound(f"unknown Allahabad High Court bench {court_code!r}")
    return bench, BENCH_BASES[bench]


def _request(session, method, url, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(random.uniform(0.3, 0.8))
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"[ahc] {method} {url} attempt {attempt} failed: {type(exc).__name__}")
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise PortalError(f"allahabadhighcourt.in {url} returned HTTP {response.status_code}")
        return response
    raise PortalError(f"allahabadhighcourt.in {url} failed after {MAX_RETRIES} attempts: {last_error}")


def _isolate_digits(raw):
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    out = Image.new("RGB", image.size, (255, 255, 255))
    src, dst = image.load(), out.load()
    for x in range(image.width):
        for y in range(image.height):
            r, g, b = src[x, y]
            if b > r + 25 and g > r + 15 and r < 120:
                dst[x, y] = (0, 0, 0)
    buffer = io.BytesIO()
    out.save(buffer, "PNG")
    return buffer.getvalue()


def _read_code(session, base, captcha_id):
    params = {"captcha_id": captcha_id} if captcha_id else None
    raw = _request(session, "GET", f"{base}/secureimage/securimage", params=params).content
    text = solve_captcha(lambda_client=lambda_client,
                         image_base64=base64.b64encode(_isolate_digits(raw)).decode("ascii"), frm="hc")
    return re.sub(r"\D", "", text or "")


def _portal_types(page):
    start = page.find('id="case_type"')
    end = page.find("</select>", start)
    return [(v, html.unescape(clean_text(label))) for v, label in
            re.findall(r'<option value="([^"]+)"[^>]*>\s*([^<]*?)\s*<', page[start:end])]


def resolve_case_type(page, bench, case_type):
    options = _portal_types(page)
    raw = clean_text(str(case_type or ""))
    by_value = {v: label for v, label in options}
    if raw in by_value:
        return raw, by_value[raw].split(" - ")[0].strip()

    name = ecourts_case_type_name(STATE_CODE, bench, raw) if raw.isdigit() else raw
    abbreviations = {label.split(" - ")[0].strip(): v for v, label in options}
    best = match_portal_case_type(name, list(abbreviations)) if name else ""
    return (abbreviations[best], best) if best else ("", "")


def _view_args(raw_args):
    return [a.strip().strip("'\"") for a in raw_args.split(",")]


def search_case(session, base, bench, case_type, case_reg_no, rgyear):
    wanted_no, wanted_year = str(int(case_reg_no)), str(rgyear)
    last_error = "search check was not accepted"
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        page = _request(session, "GET", f"{base}/case-number").text
        type_value, abbreviation = resolve_case_type(page, bench, case_type)
        if not type_value:
            raise PortalCaseNotFound(f"no Allahabad High Court case type matches eCourts type {case_type!r}")

        captcha_id = CAPTCHA_ID_RE.search(page)
        captcha_id = captcha_id.group(1) if captcha_id else ""
        code = _read_code(session, base, captcha_id)
        if not code:
            continue

        data = {"case_type": type_value, "case_no": wanted_no, "case_year": wanted_year, "captchacode": code}
        if captcha_id:
            data["captcha_id"] = captcha_id
        body = _request(session, "POST", f"{base}/get_CaseInfo", data=data, headers=XHR).text

        if "not match" in body.lower():
            print(f"[ahc] search attempt {attempt}/{SEARCH_ATTEMPTS}: code not accepted")
            continue

        target = f"{abbreviation}/{wanted_no}/{wanted_year}".upper()
        soup = BeautifulSoup(body, "html.parser")
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            link = row.find(attrs={"onclick": VIEW_RE})
            if not cells or link is None:
                continue
            labels = [re.split(r"[\s(]", clean_text(c.get_text(" ")), 1)[0].upper() for c in cells]
            if target in labels:
                return _view_args(VIEW_RE.search(link["onclick"]).group(1)), abbreviation, ""
        raise PortalCaseNotFound(f"allahabadhighcourt.in has no {abbreviation} {wanted_no}/{wanted_year}")

    raise PortalError(f"allahabadhighcourt.in search failed: {last_error}")


def _key_values(soup):
    pairs = {}
    for row in soup.find_all("tr"):
        head = row.find("th")
        cell = row.find("td")
        if head is not None and cell is not None and not row.find("thead"):
            pairs[clean_text(head.get_text(" ")).rstrip(":").lower()] = clean_text(cell.get_text(" "))
    return pairs


def _parties(cell):
    if cell is None:
        return []
    for br in cell.find_all("br"):
        br.replace_with("\n")
    parties, advocate = [], ""
    for line in cell.get_text().split("\n"):
        line = clean_text(line)
        if not line:
            continue
        if line.lower().startswith("advocate"):
            advocate = clean_text(line.split("-", 1)[1] if "-" in line else "")
            continue
        parties.append({"name": clean_text(re.sub(r"^\d+\.\s*", "", line)), "advocate": "",
                        "relation": "", "address": ""})
    advocate = ", ".join(clean_text(ENROLMENT_RE.sub("", part)) for part in advocate.split("),")
                         if clean_text(ENROLMENT_RE.sub("", part)))
    if parties and advocate:
        parties[0]["advocate"] = advocate
    return parties


def _table_rows(soup, css):
    table = soup.find("table", class_=css)
    if table is None:
        return []
    body = table.find("tbody") or table
    return [[clean_text(td.get_text(" ")) for td in tr.find_all("td")]
            for tr in body.find_all("tr") if tr.find("td")]


def parse_details(page):
    soup = BeautifulSoup(page, "html.parser")
    text = clean_text(soup.get_text(" "))
    pairs = _key_values(soup)

    header = re.search(r"\b(DISPOSED|PENDING)\b", text)
    filing_date = re.search(r"Filing Date\s*:\s*(\d{2}-\d{2}-\d{4})", text)
    reg_date = re.search(r"Date of Registration\s*:\s*(\d{2}-\d{2}-\d{4})", text)
    diary = re.search(r"Diary/Token No\.\s*:\s*(\d+)", text)
    cnr = re.search(r"\b(UPHC\d{12})\b", text)

    adv = soup.find("table", class_="table-adv")
    adv_cells = adv.find("tbody").find_all("td", recursive=False) if adv and adv.find("tbody") else []
    if not adv_cells and adv is not None:
        adv_cells = adv.find_all("td")

    acts = [{"act": r[0], "section": r[1] if len(r) > 1 else ""} for r in _table_rows(soup, "table-act") if r and r[0]]
    lower = {r[0]: r[1].lstrip(": ").strip() for r in _table_rows(soup, "table-lower-court") if len(r) > 1}
    fir = {r[0]: r[1].lstrip(": ").strip() for r in _table_rows(soup, "table-fir") if len(r) > 1}

    ia_details = []
    for r in _table_rows(soup, "table-filing"):
        if len(r) < 5:
            continue
        number, _, classification = r[0].partition("Classification :")
        ia_details.append({
            "iaNumber": clean_text(number), "classification": clean_text(classification),
            "party": clean_text(r[1]), "dateOfFiling": normalize_date(r[2]),
            "nextDate": normalize_date(r[3]), "status": r[4],
        })

    listing = []
    for r in _table_rows(soup, "table-hist"):
        if len(r) < 4 or not normalize_date(r[3]):
            continue
        listing.append({"list_type": r[0], "judge": r[1], "court": r[2],
                        "date": normalize_date(r[3]), "short_order": r[4] if len(r) > 4 else ""})

    order_sheet = ORDER_SHEET_RE.search(page)

    return {
        "status_word": header.group(1) if header else "",
        "filing_no": clean_text(pairs.get("filing no.", "").split("Filing Date")[0]),
        "filing_date": filing_date.group(1) if filing_date else "",
        "reg_date": reg_date.group(1) if reg_date else "",
        "diary": diary.group(1) if diary else "",
        "cnr": cnr.group(1) if cnr else "",
        "first_hearing": normalize_date(pairs.get("first hearing date", "")),
        "next_hearing": normalize_date(pairs.get("next hearing date", "")),
        "decision_date": normalize_date(pairs.get("date of decision", "")),
        "case_status": meaningful(pairs.get("case status", "")),
        "nature": meaningful(pairs.get("nature of disposal", "")),
        "coram": meaningful(pairs.get("coram", "")),
        "bench_type": meaningful(pairs.get("bench type", "")),
        "cause_list": meaningful(pairs.get("causelist type", "")),
        "district": meaningful(pairs.get("district", "")),
        "category": meaningful(pairs.get("category", "")),
        "sub_category": meaningful(pairs.get("sub category", "")),
        "petitioners": _parties(adv_cells[0] if adv_cells else None),
        "respondents": _parties(adv_cells[1] if len(adv_cells) > 1 else None),
        "acts": acts,
        "lower_court": {**lower, **{f"FIR {k}": v for k, v in fir.items()}},
        "ia_details": ia_details,
        "listing": listing,
        "order_sheet": order_sheet.groups() if order_sheet else None,
    }


def fetch_orders(session, base, sheet_args):
    if not sheet_args:
        return []
    ct, cn, cy = sheet_args
    body = _request(session, "POST", f"{base}/get-order-sheets",
                    data={"ct": ct, "cn": cn, "cy": cy}, headers=XHR).text
    soup = BeautifulSoup(body, "html.parser")
    orders = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        link = cells[2].find("a", href=True)
        if link is None:
            continue
        href = link["href"].strip()
        orders.append({
            "date": normalize_date(cells[1].get_text(" ")),
            "href": href,
            "ref": re.search(r"judgmentID=(\d+)", href).group(1) if "judgmentID=" in href else href,
        })
    orders.sort(key=lambda o: dmy_key(o["date"]))
    return orders


def build_history(listing, next_date):
    rows = {}
    for item in listing:
        current = rows.get(item["date"])
        if current is None or (item["short_order"] and not current["short_order"]):
            rows[item["date"]] = item
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
            "purpose": row["court"],
            "action": row["short_order"],
            "inputType": "automatic",
            "lawyerRemark": None,
        })
    return history


def fetch_case(session, *, court_code, case_type, case_reg_no, rgyear, cino=None):
    bench, base = _base(court_code)
    view_args, abbreviation, _ = search_case(session, base, bench, case_type, case_reg_no, rgyear)

    data = {"cino": view_args[0], "source": view_args[1] if len(view_args) > 1 else "undefined"}
    if len(view_args) > 2 and view_args[2]:
        data["iemi"] = view_args[2]
    page = _request(session, "POST", f"{base}/get_CaseDetails", data=data, headers=XHR).text
    if "PHP Error" in page[:2000] or "case-heading" not in page:
        raise PortalError("allahabadhighcourt.in returned an empty case page")

    info = parse_details(page)
    try:
        raw_orders = fetch_orders(session, base, info["order_sheet"])
    except PortalError as exc:
        print(f"[ahc] order sheet failed: {exc}")
        raw_orders = []

    disposed = info["status_word"] == "DISPOSED" or "dispos" in info["case_status"].lower()
    next_date = "" if disposed else info["next_hearing"]
    history = build_history(info["listing"], next_date)
    decision = info["decision_date"] or (history[-1]["businessOnDate"] if disposed and history else "")

    orders = []
    for number, item in enumerate(raw_orders, start=1):
        final = disposed and item["date"] and item["date"] == decision
        orders.append({
            "order_number": str(number),
            "order_on": "Judgment" if final else "Order",
            "judge": "",
            "order_date": item["date"],
            "order_type": "final" if final else "order",
            "href": item["href"],
            "source_ref": f"{NAME}:{bench}:{item['ref']}",
            "link_only": True,
        })

    reg_no, year = str(int(case_reg_no)), str(rgyear)
    acts = info["acts"]
    metadata = {
        "case_number": f"{abbreviation}/{reg_no}/{year}",
        "cino": "",
        "PortalCNR": info["cnr"],
        "CaseType": abbreviation,
        "FilingNumber": info["filing_no"],
        "FilingDate": info["filing_date"],
        "RegistrationNumber": f"{reg_no}/{year}",
        "RegistrationDate": info["reg_date"],
        "CNRNumber": "",
        "DiaryNumber": info["diary"],
        "FirstHearingDate": info["first_hearing"] or (history[0]["businessOnDate"] if history else ""),
        "NextHearingDate": next_date,
        "DecisionDate": decision if disposed else "",
        "CaseStatus": "Disposed" if disposed else (info["case_status"] or "Pending"),
        "PortalStatus": info["case_status"] or info["status_word"].title(),
        "NatureofDisposal": (info["nature"] or "Disposed") if disposed else "",
        "CourtNumberandJudge": info["coram"] or (history[-1]["judge"] if history else ""),
        "BenchType": info["bench_type"],
        "BenchName": BENCHES[bench],
        "JudicialBranch": "",
        "CauseListType": info["cause_list"],
        "State": "UTTAR PRADESH",
        "District": info["district"],
        "NotBeforeMe": "",
        "petitioner_and_advocate": [p["name"] for p in info["petitioners"]],
        "respondent_and_advocate": [p["name"] for p in info["respondents"]],
        "petitioner_details": info["petitioners"],
        "respondent_details": info["respondents"],
        "actsandSection": {
            "acts": ", ".join(a["act"] for a in acts if a["act"]),
            "section": ", ".join(a["section"] for a in acts if a["section"]),
        } if acts else {},
        "acts": acts,
        "category_details": {k: v for k, v in (("Category", info["category"]),
                                                ("Sub Category", info["sub_category"])) if v},
        "subordinate_court_information": info["lower_court"],
        "sub_matters": [],
        "ia_details": info["ia_details"],
        "case_history": history,
        "case_conversion": [],
        "case_transfer": [],
        "objections": [],
        "document_details": [],
        "portal_ref": f"{bench}|{view_args[0]}",
    }
    return metadata, orders
