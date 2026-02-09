from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
import httpx
from urllib.parse import quote
import uvicorn
import re

app = FastAPI(title="AFT Case Diary Scraper")

class DiaryRequest(BaseModel):
    diary_no: str

def to_camel_case(s: str) -> str:
    """
    Converts a string to camelCase.
    Example: "Petitioner Name" -> "petitionerName"
    """
    s = re.sub(r"[^\w\s]", "", s)  # remove punctuation
    parts = s.strip().split()
    if not parts:
        return ""
    return parts[0].lower() + "".join(word.capitalize() for word in parts[1:])

@app.post("/scrape-diary")
async def scrape_diary(data: DiaryRequest):
    encoded_diary = quote(data.diary_no, safe="")
    url = f"http://aftpb.org/aft/views/diary_cases.php?diary_no={encoded_diary}&results_per_page=1"

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.RequestError as e:
            raise HTTPException(status_code=500, detail=f"Network error: {e}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Failed to fetch page: {e}")

    soup = BeautifulSoup(response.text, "html.parser")

    table = soup.find("table", {"class": "table"})
    if not table:
        raise HTTPException(status_code=404, detail="Could not find data table on page")

    headers = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
    headers = [h for h in headers if h.lower() not in ["actions", "status"]]
    headers_camel = [to_camel_case(h) for h in headers]

    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        cell_values = cells[:len(headers_camel)]
        row_data = dict(zip(headers_camel, cell_values))
        rows.append(row_data)

    return {"diaryNo": data.diary_no, "results": rows}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
