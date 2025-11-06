import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import re
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv
from core.database import collection
from core.s3_client import s3_client
import os
load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

app = APIRouter()

class CaseRequest(BaseModel):
    case_type: str
    case_reg_no: str
    rgyear: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None
    refresh_flag: str


class CaseRequestBulk(BaseModel):
    petres_name: str
    rgyearP: str
    case_status: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None
    courtType: Optional[str] = None

class CaseRequestBulkIngest(BaseModel):
    court_code: str
    state_code: str
    dist_code: str
    court_complex_code: str
    case_no: str
    cino: str
    est_code: Optional[str] = None

@app.post("/getcaseInfo")
def fetch_submit_info(case_data: CaseRequest):
    session = requests.Session()
    query = case_data.dict()
    ac_query = {
        "case_reg_no": query.get("case_reg_no"),
        "rgyear": query.get("rgyear"),
        "est_code": query.get("est_code"),
        "case_type": query.get("case_type"),
        "state_code": query.get("state_code"),
        "dist_code": query.get("dist_code"),
        "court_complex_code": query.get("court_complex_code")
    }
    if case_data.refresh_flag != "1":
        existing_case = collection.find_one(ac_query)
        if existing_case:
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=existing_case)

    case_info = {}

    try:
        payload = {
            'ajax_req': 'true',
            'case_type': case_data.case_type,
            'case_no': case_data.case_reg_no,
            'rgyear': case_data.rgyear,
            'state_code': case_data.state_code,
            'dist_code': case_data.dist_code,
            'court_complex_code': case_data.court_complex_code,
            'est_code': case_data.est_code,
            'search_case_no': case_data.case_reg_no,
        }

        search_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/submitCaseNo"
        response = session.post(search_url, data=payload)

        html_content = response.json().get("case_data", "")

        if "Record not found" in html_content:
            return JSONResponse(content={"error": "Invalid case details"}, status_code=404)

        soup = BeautifulSoup(html_content, "html.parser")
        view_link = soup.find("a", class_="someclass")

        if view_link:
            onClick_data = view_link.get("onclick", "")
            match = re.search(r"viewHistory\((.*?)\)", onClick_data)

            if match:
                params = match.group(1)
                values = [v.strip().strip("'") for v in params.split(",")]

                case_info = {
                    "case_no": values[0],
                    "cino": values[1],
                    "court_code": values[2] or None,
                    "state_code": values[5] or None,
                    "dist_code": values[6] or None,
                    "court_complex_code": values[7] or None,
                    "est_code": case_data.est_code,
                    "case_type": case_data.case_type,
                    "rgyear": case_data.rgyear,
                    "case_reg_no": case_data.case_reg_no
                }

                second_payload = {
                    "app_token": response.json().get("app_token", ""),
                    "court_code": case_info["court_code"],
                    "state_code": case_info["state_code"],
                    "dist_code": case_info["dist_code"],
                    "court_complex_code": case_info["court_complex_code"],
                    "case_no": case_info["case_no"],
                    "cino": case_info["cino"],
                    "est_code": case_info["est_code"],
                    "search_flag": "CScaseNumber",
                    "search_by": "CScaseNumber",
                    "ajax_req": "true",
                }

                second_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=home/viewHistory"

                second_response = session.post(second_url, data=second_payload)

                if second_response.status_code == 200:
                    case_details = second_response.json()
                    soup = BeautifulSoup(case_details.get(
                        "data_list", ""), "html.parser")

                    def extract_table_data(table_class):
                        table = soup.find("table", {"class": table_class})
                        data = {}
                        if table:
                            rows = table.find_all("tr")
                            for row in rows:
                                cells = row.find_all("td")
                                if len(cells) >= 2:
                                    key = cells[0].get_text(strip=True).replace(
                                        ':', '').replace(' ', '')
                                    value = cells[1].get_text(strip=True)
                                    data[key] = value
                        return data

                    case_status = extract_table_data(
                        "table case_status_table table-bordered")
                    case_details = extract_table_data(
                        "table case_details_table table-bordered")

                    def extract_list_data(table_class):
                        table = soup.find("table", {"class": table_class})

                        values = []
                        if table:
                            cell = table.find("td")
                            if cell:
                                values = [
                                    line.strip() for line in cell.stripped_strings if line.strip()]
                        return values

                    case_petitioner = {"petitioner_and_advocate": extract_list_data(
                        "table table-bordered Petitioner_Advocate_table")}
                    case_respondent = {"respondent_and_advocate": extract_list_data(
                        "table table-bordered Respondent_Advocate_table")}

                    def extract_fir_details(table_class):
                        table = soup.find(
                            "table", class_=lambda x: x and table_class in x)
                        details = {}
                        if table:
                            rows = table.find_all("tr")
                            for row in rows:
                                cols = row.find_all("td")
                                if len(cols) == 2:
                                    key = cols[0].get_text(
                                        strip=True).replace(" ", "")
                                    value = cols[1].get_text(strip=True)
                                    details[key] = value
                        return details

                    case_fir_details = {
                        "fir_details": extract_fir_details("FIR_details_table")}

                    act_table = soup.find(
                        "table", {"class": "table acts_table table-bordered"})
                    acts_and_sections = {"actsandSection": {
                        "acts": "null", "section": "null"}}

                    if act_table:
                        rows = act_table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) == 2:
                                acts_and_sections["actsandSection"] = {
                                    "acts": cells[0].get_text(strip=True),
                                    "section": cells[1].get_text(strip=True)
                                }

                    def extract_case_history():
                        table = soup.find("table", {"class": "history_table"})
                        rows = table.find_all("tr") if table else []
                        history = []

                        for row in rows:
                            cols = row.find_all("td")
                            if len(cols) >= 4:
                                history.append({
                                    "judge": cols[0].text.strip(),
                                    "businessOnDate": cols[1].find("a").text.strip() if cols[1].find("a") else "",
                                    "hearingDate": cols[2].text.strip(),
                                    "purpose": cols[3].text.strip(),
                                    "inputType": "automatic",
                                    "lawyerRemark": "null"
                                })

                        return history or []

                    case_history = {"case_history": extract_case_history()}

                    def extract_case_transfer():
                        table = soup.find(
                            "table", {"class": "transfer_table table"})
                        transfers = []

                        if table:
                            rows = table.find_all("tr")[1:]
                            for row in rows:
                                cols = row.find_all("td")
                                if len(cols) >= 4:
                                    transfers.append({
                                        "registrationNumber": cols[0].text.strip(),
                                        "transferDate": cols[1].text.strip(),
                                        "fromCourt": cols[2].text.strip(),
                                        "toCourt": cols[3].text.strip(),
                                        "inputType": "automatic",
                                        "lawyerRemark": None
                                    })

                        return transfers

                    case_transfer = {"case_transfer": extract_case_transfer()}

                    orders = []
                    order_table = soup.find("table", {"class": "order_table"})

                    if not order_table:
                        print("No order_table found.")
                    else:
                        rows = order_table.find_all("tr")[1:]

                        if not rows:
                            print("No rows found in order_table.")

                        app_token = second_response.json().get("app_token", "")

                        for index, row in enumerate(rows):
                            cols = row.find_all("td")
                            if len(cols) < 3:
                                print(
                                    f"⚠️ Skipping row with insufficient columns: {row}")
                                continue

                            order_number = cols[0].text.strip()
                            order_date = cols[1].text.strip()
                            order_link = cols[2].find("a")

                            print("order link", order_link)

                            if not order_link:
                                print(
                                    f"⚠️ No anchor tag found for order {order_number}. Skipping PDF fetch.")
                                orders.append({
                                    "order_number": order_number,
                                    "order_date": order_date,
                                    "order_link": None,
                                    "note": "No order link available"
                                })
                                continue
                            inner_a_tag = None
                            for a in order_link.find_all("a"):
                                if a.get("onclick") and "displayPdf" in a.get("onclick"):
                                    inner_a_tag = a
                                    break
                            order_link = inner_a_tag or order_link
                            onclick_attr = order_link.get("onclick", "")
                            match = re.search(
                                r"displayPdf\((.*?)\)", onclick_attr)

                            if not match:
                                print(
                                    f"⚠️ No 'onclick' match found for order {order_number}. Skipping PDF fetch.")
                                orders.append({
                                    "order_number": order_number,
                                    "order_date": order_date,
                                    "order_link": None,
                                    "note": "No valid onclick for PDF"
                                })
                                continue

                            params = match.group(1)
                            # values = [v.strip().strip("'") for v in params.split("&")]
                            values = [v.strip().strip("'")
                                      for v in params.split(",")]

                            if len(values) < 4:
                                print(
                                    f"⚠️ Invalid onclick parameters for order {order_number}. Skipping.")
                                continue

                            normal_v = values[0]
                            case_val = values[1]
                            court_code = values[2]
                            filename = values[3]
                            app_flag = values[4] if len(values) > 4 else ""

                            # base_path = values[0]
                            # query_string = "&".join(values[1:])
                            # full_url = f"https://services.ecourts.gov.in/ecourtindia_v6/?p={base_path}&{query_string}"

                            order_payload = {
                                "normal_v": normal_v,
                                "case_val": case_val,
                                "court_code": court_code,
                                "filename": filename,
                                "appFlag": app_flag,
                                "ajax_req": "true",
                                "app_token": app_token
                            }

                            print("order_payload", order_payload)
                            full_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=home/display_pdf"

                            order_response = session.post(
                                full_url, data=order_payload)
                            print("order response,", order_response.text)

                            try:
                                token_update = order_response.json()
                                new_app_token = token_update.get("app_token")
                                if new_app_token:
                                    app_token = new_app_token
                            except Exception:
                                pass

                            order_response_data = order_response.json()
                            pdf_file_path = order_response_data.get(
                                "order", "").replace("\\", "")

                            if not pdf_file_path:
                                print(
                                    f"❌ PDF path not found in object for order {order_number}.")
                                continue

                            final_pdf_url = f"https://services.ecourts.gov.in/ecourtindia_v6/{pdf_file_path}"

                            s3_folder_path = f"case_data/orders/{case_info['cino']}/"
                            s3_file_path = f"{s3_folder_path}{case_info['cino']}-{order_number}.pdf"
                            try:
                                s3_client.head_object(
                                    Bucket=BUCKET_NAME, Key=s3_file_path)
                                s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
                            except s3_client.exceptions.ClientError as e:
                                if e.response['Error']['Code'] == "404":
                                    response = session.get(
                                        final_pdf_url, stream=True)
                                    if response.status_code == 200:
                                        s3_client.upload_fileobj(
                                            response.raw,
                                            BUCKET_NAME,
                                            s3_file_path,
                                            ExtraArgs={
                                                'ContentType': 'application/pdf',
                                                'ContentDisposition': 'inline'
                                            }
                                        )
                                        s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
                                    else:
                                        print(
                                            f"❌ Failed to fetch PDF from {final_pdf_url}")
                                        s3_url = None
                                else:
                                    print(f"❌ S3 Error: {e}")
                                    s3_url = None

                            orders.append({
                                "order_number": order_number,
                                "order_date": order_date,
                                "order_link": s3_url
                            })

                    final_response = {**case_info, **case_fir_details, **case_details, **case_status, **case_petitioner,
                                      **case_respondent, **acts_and_sections, **case_history, **case_transfer, "orders": orders}
                    result = collection.update_one(
                        ac_query, {"$set": final_response}, upsert=True)
                    if result.upserted_id:
                        final_response["_id"] = str(result.upserted_id)
                    else:
                        doc = collection.find_one(ac_query)
                        final_response["_id"] = str(doc["_id"])

                    return JSONResponse(content=final_response)
                else:
                    return JSONResponse(content={"error": "Failed to fetch case details"}, status_code=403)

        return JSONResponse(content={"error": "Case details not found"}, status_code=403)

    finally:
        session.close()


@app.post("/dc/bulk_q/partyname")
def fetch_submit_info(case_data: CaseRequestBulk):
    session = requests.Session()
    case_info = {}

    try:
        payload = {
            'ajax_req': 'true',
            'petres_name': case_data.petres_name,
            'rgyearP': case_data.rgyearP,
            'case_status': case_data.case_status,
            'state_code': case_data.state_code,
            'dist_code': case_data.dist_code,
            'court_complex_code': case_data.court_complex_code,
            'est_code': case_data.est_code,
        }

        search_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/submitPartyName"
        response = session.post(search_url, data=payload)

        html_content = response.json().get("party_data", "")

        if "Record not found" in html_content:
            return JSONResponse(content={"error": "Invalid case details"}, status_code=404)

        soup = BeautifulSoup(html_content, "html.parser")
        view_link = soup.find("a", class_="someclass")
        rows = soup.find_all("tr")

        results = []
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            case_number = cols[1].get_text(strip=True)
            party_details = cols[2].get_text(" ", strip=True)
            party_details = re.sub(
                r"\s*Vs\.?\s*", " Vs ", party_details, flags=re.IGNORECASE)
            party_details = re.sub(r"\s+", " ", party_details).strip()

            view_link = row.find("a", class_="someclass")
            if not view_link:
                continue

            onClick_data = view_link.get("onclick", "")
            match = re.search(r"viewHistory\((.*?)\)", onClick_data)

            if not match:
                continue

            params = match.group(1)
            values = [v.strip().strip("'") for v in params.split(",")]

            case_info = {
                "case_no": values[0],
                "cino": values[1],
                "court_code": values[2] or None,
                "state_code": values[5] or None,
                "dist_code": values[6] or None,
                "court_complex_code": values[7] or None,
                "est_code": case_data.est_code or None,
                "rgyear": case_data.rgyearP,
                "case_number": case_number,
                "party_details": party_details,
                "courtType": case_data.courtType
            }

            results.append(case_info)

        return JSONResponse(content={"data": results}, status_code=200)

    finally:
        session.close()


@app.post("/dc/bulk_i/partyname")
def fetch_submit_info(case_data: List[CaseRequestBulkIngest]):
    session = requests.Session()
    case_info = {}

    try:
        payload = {
            'ajax_req': 'true',
            'court_code': case_data.court_code,
            'state_code': case_data.state_code,
            'dist_code': case_data.dist_code,
            'court_complex_code': case_data.court_complex_code,
            'dist_code': case_data.dist_code,
            'court_complex_code': case_data.court_complex_code,
            'est_code': case_data.est_code,
        }

        search_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/submitPartyName"
        response = session.post(search_url, data=payload)

        html_content = response.json().get("party_data", "")

        if "Record not found" in html_content:
            return JSONResponse(content={"error": "Invalid case details"}, status_code=404)

        soup = BeautifulSoup(html_content, "html.parser")
        view_link = soup.find("a", class_="someclass")
        rows = soup.find_all("tr")

        results = []
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            case_number = cols[1].get_text(strip=True)
            party_details = cols[2].get_text(" ", strip=True)
            party_details = re.sub(
                r"\s*Vs\.?\s*", " Vs ", party_details, flags=re.IGNORECASE)
            party_details = re.sub(r"\s+", " ", party_details).strip()

            view_link = row.find("a", class_="someclass")
            if not view_link:
                continue

            onClick_data = view_link.get("onclick", "")
            match = re.search(r"viewHistory\((.*?)\)", onClick_data)

            if not match:
                continue

            params = match.group(1)
            values = [v.strip().strip("'") for v in params.split(",")]

            case_info = {
                "case_no": values[0],
                "cino": values[1],
                "court_code": values[2] or None,
                "state_code": values[5] or None,
                "dist_code": values[6] or None,
                "court_complex_code": values[7] or None,
                "est_code": case_data.est_code or None,
                "rgyear": case_data.rgyearP,
                "case_number": case_number,
                "party_details": party_details,
                "courtType": case_data.courtType
            }

            results.append(case_info)

        return JSONResponse(content={"data": results}, status_code=200)

    finally:
        session.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
