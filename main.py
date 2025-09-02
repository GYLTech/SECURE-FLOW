from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import requests
from bs4 import BeautifulSoup

app = FastAPI()

# ---- Request model ----
class PartySearchRequest(BaseModel):
    party_name: str
    rgyear: Optional[str] = None
    state_code: Optional[str] = None
    dist_code: Optional[str] = None
    court_complex_code: Optional[str] = None
    est_code: Optional[str] = None
    case_type: Optional[str] = None


@app.post("/getCaseByParty")
def fetch_case_by_party(data: PartySearchRequest):
    session = requests.Session()

    payload = {
        "ajax_req": "true",
        "petres_name": data.party_name,   # ✅ search by party name
        "rgyear": data.rgyear,
        "state_code": data.state_code,
        "dist_code": data.dist_code,
        "court_complex_code": data.court_complex_code,
        "court_establishment_code": data.est_code,
        "case_type": data.case_type,
    }

    search_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/submitPartyName"
    response = session.post(search_url, data=payload)

    if response.status_code != 200:
        return JSONResponse(content={"error": "Failed to fetch from eCourts"}, status_code=500)

    html_content = response.json().get("case_data", "")
    if "Record not found" in html_content:
        return JSONResponse(content={"error": "No case found for given party name"}, status_code=404)

    # parse HTML table
    soup = BeautifulSoup(html_content, "html.parser")

    results = []
    rows = soup.find_all("tr")[1:]  # skip header row
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 4:
            case_info = {
                "case_number": cols[0].get_text(strip=True),
                "party_names": cols[1].get_text(strip=True),
                "filing_date": cols[2].get_text(strip=True),
                "status": cols[3].get_text(strip=True),
            }
            results.append(case_info)

    return {"party_name": data.party_name, "results": results}
