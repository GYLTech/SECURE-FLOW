
import base64
import uuid




from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
from typing import Optional

app = APIRouter()

class NCLATCaseRequest(BaseModel):
    filing_no: str
    schema_name: str


@app.post("/getcaseInfoo")
def get_case_info(case_data: NCLATCaseRequest):

    session = requests.Session()

    headers = {
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://efiling.nclat.gov.in/nclat/case_status.php",
        "Origin": "https://efiling.nclat.gov.in",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    }

    try:
        
        session.get(
            "https://efiling.nclat.gov.in/nclat/case_status.php",
            headers=headers,
            timeout=30
        )

        
        ajax_url = "https://efiling.nclat.gov.in/nclat/ajax/ajax.php"

        payload = {
            "action": "case_status_case_details",
            "filing_no": case_data.filing_no,
            "schema_name": case_data.schema_name
        }

        response = session.post(
            ajax_url,
            data=payload,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()

        html_content = response.text

        if not html_content.strip():
            return JSONResponse(
                content={"error": "Empty response from server"},
                status_code=404
            )

        soup = BeautifulSoup(html_content, "html.parser")

        
        extracted_data = {}

        tables = soup.find_all("table")

        for index, table in enumerate(tables):
            table_data = {}

            for row in table.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) >= 2:
                    key = cols[0].get_text(strip=True)
                    value = cols[1].get_text(strip=True)
                    table_data[key] = value

            if table_data:
                extracted_data[f"table_{index+1}"] = table_data

        return {
            "filing_no": case_data.filing_no,
            "schema_name": case_data.schema_name,
            "data": extracted_data
        }

    except Exception as e:
        return JSONResponse(
            content={"error": str(e)},
            status_code=500
        )

    finally:
        session.close()


