import json
import random
import time
import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
import re
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv
from core.database import collection
from core.s3_client import s3_client
import os
from http.client import RemoteDisconnected
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
    courtType: Optional[str] = None

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
    rgyear: str
    courtType: Optional[str] = None
    refresh: str


def build_case_base_path(metadata: dict):
    return (
        f"cases/"
        f"{metadata['courtType']}/"
        f"{metadata['state_code']}/"
        f"{metadata['dist_code']}/"
        f"{metadata['court_complex_code']}/"
        f"{metadata['rgyear']}/"
        f"{metadata['cino']}/"
    )

def build_orders_prefix(metadata: dict):
    return build_case_base_path(metadata) + "orders/"

def build_case_json_key(metadata: dict):
    return build_case_base_path(metadata) + "metadata.json"

def upload_case_json_to_s3(
    s3_client,
    bucket_name,
    metadata
):
    key = build_case_json_key(metadata)

    payload = {
        **metadata
    }

    s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False),
        ContentType="application/json"
    )

    return f"s3://{bucket_name}/{key}"

def safe_post(session, url, data, max_retries=3):
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(1.2, 2.0))

            response = session.post(
                url,
                data=data,
                timeout=(10, 120),
                headers={"Connection": "close"}
            )
            return response

        except (requests.exceptions.ConnectionError, RemoteDisconnected) as e:
            print(f"⚠️ Server disconnected (attempt {attempt+1})")

            session.close()
            session = requests.Session()

        except requests.exceptions.Timeout:
            print(f"⚠️ Timeout (attempt {attempt+1})")

    raise Exception("❌ eCourts viewHistory failed after retries")


def sanitize_keys(data):
    clean_data = {}
    for key, value in data.items():
        clean_key = key.replace('.', '').replace('$', '')
        if isinstance(value, dict):
            clean_data[clean_key] = sanitize_keys(value)
        else:
            clean_data[clean_key] = value
    return clean_data



def extract_table_data(soup, table_class):
    tables = soup.find_all("table", {"class": table_class})
    data = {}

    for table in tables:
        rows = table.find_all("tr")

        for row in rows:
            headers = row.find_all("th")
            values = row.find_all("td")

            for h, v in zip(headers, values):
                key = h.get_text(strip=True).replace(":", "")
                key = "".join(key.split())  

                value = v.get_text(strip=True)

                if "CNR" in key:
                    span = v.find("span")
                    if span:
                        value = span.get_text(strip=True)

                if key:
                    data[key] = value

    return sanitize_keys(data)

# def extract_list_data(soup, table_class):
#     table = soup.find("table", {"class": table_class})
#     values = []
#     if table:
#         cell = table.find("td")
#         if cell:
#             values = [line.strip()
#                       for line in cell.stripped_strings if line.strip()]
#     return values
def extract_list_data(soup, table_class):
    ul = soup.find("ul", {"class": table_class})
    values = []

    if ul:
        items = ul.find_all("li")

        for item in items:
            text = " ".join(item.stripped_strings)
            values.append(text)

    return values

def extract_fir_details(soup, table_class):
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


def extract_case_history(soup):
    table = soup.find("table", {"class": "history_table"})
    rows = table.find_all("tr") if table else []
    history = []

    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 4:
            history.append({
                "judge": cols[0].text.strip(),
                "businessOnDate": cols[1].find("a").text.strip() if cols[1].find("a") else cols[2].text.strip(),
                "hearingDate": cols[2].text.strip(),
                "purpose": cols[3].text.strip(),
                "inputType": "automatic",
                "lawyerRemark": "null"
            })

    return history or []


def extract_case_transfer(soup):
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


def extract_acts_and_sections(
    soup,
    table_class="table acts_table table-bordered"
):
    acts_and_sections = {
        "actsandSection": {
            "acts": "null",
            "section": "null"
        }
    }

    act_table = soup.find("table", {"class": table_class})
    if not act_table:
        return acts_and_sections

    rows = act_table.find_all("tr")[1:]

    for row in rows:
        cells = row.find_all("td")
        if len(cells) == 2:
            acts_and_sections["actsandSection"] = {
                "acts": cells[0].get_text(strip=True),
                "section": cells[1].get_text(strip=True)
            }

    return acts_and_sections


def fetch_and_store_orders(
    soup,
    session,
    metadata,
    case_details,
    s3_client,
    bucket_name,
    region_name,
    table_class="order_table",
    pdf_endpoint="https://services.ecourts.gov.in/ecourtindia_v6/?p=home/display_pdf",
    pdf_base_url="https://services.ecourts.gov.in/ecourtindia_v6/"
):
    orders_prefix = build_case_base_path(metadata) + "orders/"
    orders = []
    order_table = soup.find("table", {"class": table_class})

    if not order_table:
        return orders

    rows = order_table.find_all("tr")[1:]
    app_token = case_details.get("app_token", "")

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 3:
            continue

        order_number = cols[0].text.strip()
        order_date = cols[1].text.strip()

        order_link = cols[2].find("a")
        if not order_link:
            continue

        inner_a_tag = None
        for a in order_link.find_all("a"):
            onclick = a.get("onclick", "")
            if "displayPdf" in onclick:
                inner_a_tag = a
                break

        order_link = inner_a_tag or order_link

        onclick_attr = order_link.get("onclick", "")
        match = re.search(r"displayPdf\((.*?)\)", onclick_attr)

        if not match:
            continue

        values = [v.strip().strip("'") for v in match.group(1).split(",")]
        if len(values) < 4:
            continue

        order_payload = {
            "normal_v": values[0],
            "case_val": values[1],
            "court_code": values[2],
            "filename": values[3],
            "appFlag": values[4] if len(values) > 4 else "",
            "ajax_req": "true",
            "app_token": app_token
        }

        order_response = safe_post(session, pdf_endpoint, order_payload)

        try:
            response_json = order_response.json()
            app_token = response_json.get("app_token", app_token)
            pdf_path = response_json.get("order", "").replace("\\", "")
        except Exception:
            continue

        if not pdf_path:
            continue

        final_pdf_url = f"{pdf_base_url}{pdf_path}"
        s3_key = f"{orders_prefix}order-{order_number.zfill(3)}.pdf"

        try:
            s3_client.head_object(Bucket=bucket_name, Key=s3_key)
            s3_url = f"https://{bucket_name}.s3.{region_name}.amazonaws.com/{s3_key}"
        except s3_client.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                pdf_response = session.get(final_pdf_url, stream=True)
                if pdf_response.status_code == 200:
                    s3_client.upload_fileobj(
                        pdf_response.raw,
                        bucket_name,
                        s3_key,
                        ExtraArgs={
                            "ContentType": "application/pdf",
                            "ContentDisposition": "inline"
                        }
                    )
                    s3_url = f"https://{bucket_name}.s3.{region_name}.amazonaws.com/{s3_key}"
                else:
                    s3_url = None
            else:
                s3_url = None

        orders.append({
            "order_number": order_number,
            "order_date": order_date,
            "order_link": s3_url
        })

    return orders


@app.post("/getcaseInfo")
def fetch_submit_info(case_data: CaseRequest):
    query = case_data.dict()
    ac_query = {
        "courtType": query.get("courtType"),
        "case_reg_no": query.get("case_reg_no"),
        "rgyear": query.get("rgyear"),
        "est_code": query.get("est_code"),
        "case_type": query.get("case_type"),
        "state_code": query.get("state_code"),
        "dist_code": query.get("dist_code"),
        "court_complex_code": query.get("court_complex_code")
    }
    existing_case = collection.find_one(ac_query)

    if existing_case and case_data.refresh == "0":
        existing_case["_id"] = str(existing_case["_id"])
        return JSONResponse(content=jsonable_encoder(existing_case))

    existing_case_id = existing_case["_id"] if existing_case else None
    
    session = requests.Session()
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
        response = safe_post(session, search_url, payload)

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
                    "case_reg_no": case_data.case_reg_no,
                    "courtType" : case_data.courtType,
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

                second_response = safe_post(
                    session, second_url, second_payload)

                if second_response.status_code == 200:
                    case_details = second_response.json()
                    soup = BeautifulSoup(case_details.get(
                        "data_list", ""), "html.parser")

                    case_status = extract_table_data(
                        soup, "table case_status_table table-bordered")
                    case_details = extract_table_data(
                        soup, "table case_details_table table-bordered")
                    case_petitioner = {"petitioner_and_advocate": extract_list_data(
                        soup, "table table-bordered Petitioner_Advocate_table petitioner-advocate-list border")}
                    case_respondent = {"respondent_and_advocate": extract_list_data(
                        soup, "table table-bordered Respondent_Advocate_table respondent-advocate-list border")}
                    case_fir_details = {"fir_details": extract_fir_details(
                        soup, "FIR_details_table")}
                    acts_and_sections = extract_acts_and_sections(soup)
                    case_history = {"case_history": extract_case_history(soup)}
                    case_transfer = {
                        "case_transfer": extract_case_transfer(soup)}

                    metadata = {
                      **case_info, **case_fir_details, **case_details, **case_status, **case_petitioner,
                                      **case_respondent, **acts_and_sections, **case_history, **case_transfer  
                    }

                    case_json_s3_path = upload_case_json_to_s3(
                    s3_client,"dl-shared-gyl-vidilekh",metadata
                    )

                    orders = fetch_and_store_orders(
                        soup,
                        session,
                        metadata,
                        case_details,
                        s3_client,
                        "dl-shared-gyl-vidilekh",
                        REGION_NAME
                    )


                    final_response = {**case_info, **case_fir_details, **case_details, **case_status, **case_petitioner,
                                      **case_respondent, **acts_and_sections, **case_history, **case_transfer,"s3_prefix" : case_json_s3_path, "orders": orders}

                    
                    if existing_case_id:
                        collection.update_one(
                            {"_id": existing_case_id},
                            {"$set": final_response}
                        )
                        final_response["_id"] = str(existing_case_id)
                    else:
                        insert_result = collection.insert_one(
                            {**final_response}
                        )
                        final_response["_id"] = str(insert_result.inserted_id)

                    return JSONResponse(content=final_response, status_code=200)
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
        response = safe_post(session, search_url, payload)

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
def fetch_submit_info(single_case: CaseRequestBulkIngest):
    session = requests.Session()
    try:
        query = single_case.dict()

        ac_query = {
            "courtType": "distcourts",
            "cino": query.get("cino"),
            "rgyear": query.get("rgyear"),
            "court_code": query.get("court_code"),
            "case_type": query.get("case_type"),
            "state_code": query.get("state_code"),
            "dist_code": query.get("dist_code"),
            "court_complex_code": query.get("court_complex_code")
        }

        existing_case = collection.find_one(ac_query)

        # print("existing case-------------------->", ac_query)

        if existing_case and single_case.refresh == "0":
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=jsonable_encoder(existing_case))

        existing_case_id = existing_case["_id"] if existing_case else None

        case_info = {
            "case_no": single_case.case_no,
            "cino": single_case.cino,
            "court_code": single_case.court_code or None,
            "state_code": single_case.state_code,
            "dist_code": single_case.dist_code,
            "court_complex_code": single_case.court_complex_code,
            "est_code": single_case.court_code or None,
            "rgyear": single_case.rgyear,
            "courtType": "distcourts"
        }

        second_payload = {
            "court_code": str(case_info.get("court_code", "")),
            "state_code": str(case_info.get("state_code", "")),
            "dist_code": str(case_info.get("dist_code", "")),
            "court_complex_code": str(case_info.get("court_complex_code", "")),
            "case_no": str(case_info.get("case_no", "")),
            "cino": str(case_info.get("cino", "")),
            "rgyear": str(case_info.get("rgyear", "")),
            "search_flag": "CScaseNumber",
            "search_by": "CScaseNumber",
            "ajax_req": "true"
        }

        if case_info.get("est_code") is not None:
            second_payload["est_code"] = str(case_info["est_code"])

        second_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=home/viewHistory"
        second_response = safe_post(session, second_url, second_payload)

        if second_response.status_code != 200:
            return JSONResponse(content={"error": "Failed request"}, status_code=500)

        case_data = second_response.json()
        soup = BeautifulSoup(case_data.get("data_list", ""), "html.parser")

        case_status = extract_table_data(soup, "table case_status_table table-bordered")
        case_details = extract_table_data(soup, "table case_details_table table-bordered")
        case_petitioner = {"petitioner_and_advocate": extract_list_data(soup, "table table-bordered Petitioner_Advocate_table petitioner-advocate-list border")}
        case_respondent = {"respondent_and_advocate": extract_list_data(soup, "table table-bordered Respondent_Advocate_table respondent-advocate-list border")}
        case_fir_details = {"fir_details": extract_fir_details(soup, "FIR_details_table")}
        acts_and_sections = extract_acts_and_sections(soup)
        case_history = {"case_history": extract_case_history(soup)}
        case_transfer = {"case_transfer": extract_case_transfer(soup)}

        metadata = {
            **case_info,
            **case_fir_details,
            **case_details,
            **case_status,
            **case_petitioner,
            **case_respondent,
            **acts_and_sections,
            **case_history,
            **case_transfer
        }

        case_json_s3_path = upload_case_json_to_s3(
            s3_client,
            "dl-shared-gyl-vidilekh",
            metadata
        )

        orders = fetch_and_store_orders(
            soup,
            session,
            metadata,
            case_data,
            s3_client,
            "dl-shared-gyl-vidilekh",
            REGION_NAME
        )

        final_response = {
            **metadata,
            "s3_prefix": case_json_s3_path,
            "orders": orders
        }
        print("final_response",final_response)

        if existing_case_id:
            collection.update_one(
                {"_id": existing_case_id},
                {"$set": final_response}
            )
            final_response["_id"] = str(existing_case_id)
        else:
            insert_result = collection.insert_one(
                {**final_response}
            )
            final_response["_id"] = str(insert_result.inserted_id)

        return JSONResponse(content=final_response, status_code=200)

    finally:
        session.close()