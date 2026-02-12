
from fastapi import APIRouter, Query
import requests_cache
from bs4 import BeautifulSoup

router = APIRouter()

session = requests_cache.CachedSession(
    cache_name="aft_cache",
    backend="sqlite",
    expire_after=3600,
)

BASE_URL = "http://aftpb.org/aft/views/diary_cases.php"


@router.get("/aft")
def get_aft_case(diary_no: str = Query(..., description="Full diary number, e.g., 1185/2026")):
   
    
    params = {
        "diary_no": diary_no,
        "date_of_presentation": "",
        "presented_by": "",
        "results_per_page": "50"
    }

    response = session.get(BASE_URL, params=params)
    soup = BeautifulSoup(response.text, "lxml")
    table = soup.find("table")

    if not table:
        return {"status": False, "message": f"No data found for diary number {diary_no}"}

    rows = table.find_all("tr")
    data = []

    for row in rows:
        cols = [col.get_text(strip=True) for col in row.find_all("td")]
        if cols and cols[0] == diary_no:  
            data.append({
                "diary_no": cols[0],
                "date": cols[1],
                "document_type": cols[2],
                "oa_no": cols[3],
                "presented_by": cols[4]
            })

    if not data:
        return {"status": False, "message": f"No case found for diary number {diary_no}"}

    return {
        "status": True,
        "from_cache": getattr(response, "from_cache", False),
        "total_rows": len(data),
        "data": data
    }
