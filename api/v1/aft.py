


from fastapi import APIRouter, Query
import requests
from bs4 import BeautifulSoup
import ast
import html

router = APIRouter()

BASE_URL = "http://aftpb.org/aft/views/diary_cases.php"


@router.get("/aft")
def get_aft_case(
    diary_no: str = Query(..., description="Full diary number, e.g., 1185/2026")
):

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    params = {
        "diary_no": diary_no,
        "date_of_presentation": "",
        "presented_by": "",
        "results_per_page": "10000"
    }

    try:
        response = requests.get(
            BASE_URL,
            params=params,
            headers=headers,
            timeout=20
        )
        response.raise_for_status()

    except requests.RequestException as e:
        return {"status": False, "message": f"Request failed: {str(e)}"}

    soup = BeautifulSoup(response.text, "lxml")
    table = soup.find("table")

    if not table:
        return {"status": False, "message": f"No data found for diary number {diary_no}"}

    rows = table.find_all("tr")
    data = []

    for row in rows:
        cols = [col.get_text(strip=True) for col in row.find_all("td")]

        if len(cols) >= 7 and cols[0] == diary_no:

            extra_details = {}

            view_button = row.find("button", class_="btn-info")

            if view_button and view_button.has_attr("onclick"):

                onclick = view_button["onclick"]

                cleaned = " ".join(onclick.split())

                if "viewDetails(" in cleaned:
                    raw_args = cleaned.split("viewDetails(")[1].rsplit(")", 1)[0]

                    try:

                        parsed_args = ast.literal_eval(f"[{raw_args}]")

                        extra_details = {
                            "case_id": parsed_args[0],
                            "section_officer_remark": html.unescape(parsed_args[7]) if len(parsed_args) > 7 else None,
                            "registration_status": parsed_args[6] if len(parsed_args) > 6 else None,
                            "case_type": parsed_args[11] if len(parsed_args) > 11 else None,
                            "no_of_applicants": parsed_args[12] if len(parsed_args) > 12 else None,
                            "no_of_respondents": parsed_args[13] if len(parsed_args) > 13 else None,
                            "deputy_registrar_remark": parsed_args[8] if len(parsed_args) > 8 else None,
                            "registrar_remark": parsed_args[9] if len(parsed_args) > 9 else None,
                            "not_completed_observations": parsed_args[10] if len(parsed_args) > 10 else None,
                        }

                    except Exception:
                        extra_details = {}

            data.append({
                "diary_no": cols[0],
                "date": cols[1],
                "document_type": cols[2],
                "oa_no": cols[3],
                "presented_by": html.unescape(cols[4]),
                "details": extra_details
            })

    if not data:
        return {"status": False, "message": f"No case found for diary number {diary_no}"}

    return {
        "status": True,
        "total_rows": len(data),
        "data": data
    }
