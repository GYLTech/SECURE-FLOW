import json
import os
import re


class PortalError(Exception):
    pass


class PortalCaseNotFound(PortalError):
    pass


PLACEHOLDERS = ("", "-", "--", "---", "—", "–", "null", "NA", "N/A", "None")


def clean_text(text):
    text = "" if text is None else str(text)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def meaningful(value):
    value = clean_text(value)
    return "" if value in PLACEHOLDERS else value


def ecourts_case_no(case_type, case_reg_no, rgyear):
    return f"2{int(case_type):03d}{int(case_reg_no):07d}{int(rgyear):04d}"


_CASE_TYPES = None
CASE_TYPES_PATH = os.path.join(os.path.dirname(__file__), "data", "case_types.json")


def ecourts_case_type_name(state_code, court_code, case_type):
    global _CASE_TYPES
    if _CASE_TYPES is None:
        with open(CASE_TYPES_PATH, encoding="utf-8") as handle:
            _CASE_TYPES = json.load(handle)

    benches = _CASE_TYPES.get(str(state_code), {})
    code = str(int(case_type)) if str(case_type or "").isdigit() else str(case_type or "")
    for bench in (str(court_code), *benches.keys()):
        name = benches.get(bench, {}).get(code)
        if name:
            return re.sub(r"-\d+\s*$", "", name).strip()
    return ""


def short_case_code(name):
    name = clean_text(name)
    if not name.endswith(")"):
        return name
    depth = 0
    for index in range(len(name) - 1, -1, -1):
        if name[index] == ")":
            depth += 1
        elif name[index] == "(":
            depth -= 1
            if depth == 0:
                return name[:index].strip() or name
    return name


def slash_to_dash(value):
    match = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", str(value or ""))
    return f"{int(match.group(1)):02d}-{int(match.group(2)):02d}-{match.group(3)}" if match else ""


def squash(text):
    return re.sub(r"[\s]+", "", text or "").upper()


def match_portal_case_type(ecourts_name, portal_types):
    target = squash(ecourts_name)
    best = ""
    for option in portal_types:
        key = squash(option)
        if not key or not target.startswith(key) or len(key) <= len(squash(best)):
            continue
        following = target[len(key):len(key) + 1]
        if following and following.isalnum():
            continue
        best = option
    return best


def iso_to_dmy(value):
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else ""


def dmy_key(value):
    parts = (value or "").split("-")
    return f"{parts[2]}{parts[1]}{parts[0]}" if len(parts) == 3 else ""
