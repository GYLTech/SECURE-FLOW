import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import re
from pydantic import BaseModel
from typing import Optional
import boto3
from pymongo import MongoClient
from dotenv import load_dotenv
import os 
from mangum import Mangum
import pytesseract
import cv2
import random
import json
import numpy as np

load_dotenv()

BASE_URL = "https://hcservices.ecourts.gov.in/hcservices/"

client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["gylscrdata"]
collection = db["casedetails"]

BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")

s3_client = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_S3_KEY"),
    aws_secret_access_key=os.getenv("AWS_S3SEC_KEY")
)

app = FastAPI()

class CaseRequest(BaseModel):
    case_type: str
    case_no: str
    rgyear: str
    state_code: str
    dist_code: str
    court_complex_code: str
    est_code: Optional[str] = None
    refresh_flag : str
    

@app.get("/")
async def root():
    return {"message": "Hello World"}

def decode_captcha(session, captcha_url):
    response = session.get(captcha_url, stream=True)

    if response.status_code == 200:
        image = np.asarray(bytearray(response.content), dtype=np.uint8)
        image = cv2.imdecode(image, cv2.IMREAD_GRAYSCALE)

        _, thresh_img = cv2.threshold(image, 150, 255, cv2.THRESH_BINARY_INV)
        pytesseract.pytesseract.tesseract_cmd = r'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'
        captcha_text = pytesseract.image_to_string(thresh_img, config="--psm 6").strip()
        return captcha_text
    return None


def clean_text(text):
    return re.sub(r"\s+", " ", text.strip())

def extract_table_data(soup, heading, fields):
    extracted_data = {}
    heading_element = soup.find("h2", string=re.compile(heading, re.IGNORECASE))
    if heading_element:
        table = heading_element.find_next("table")
        if table:
            rows = table.find_all("tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 2:
                    label = clean_text(cols[0].get_text())
                    value = clean_text(cols[1].get_text())
                    if label in fields:
                        extracted_data[label] = value
    return extracted_data



def extract_party_details(soup, class_name):
    extracted_data = []
    elements = soup.find_all("span", class_=class_name)

    for element in elements:
        for br in element.find_all("br"):
            br.replace_with("\n")
        lines = [line.strip() for line in element.get_text().split("\n") if line.strip()]
        if lines:
            name = lines[0]

            if name[0].isdigit() and ")" in name:
                name = name.split(")", 1)[1].strip()

            extracted_data.append(name)

    return extracted_data

def extract_subordinate_court_info(soup):
    court_info = soup.find("span", class_="Lower_court_table")
    if not court_info:
        return {}

    details = {}
    labels = court_info.find_all("span", style="width:150px;display:inline-block;")
    values = court_info.find_all("label", style="text-align:left")

    for label, value in zip(labels, values):
        key = label.text.strip().replace(":", "")
        val = value.text.strip()
        details[key] = val

    return details

def extract_table_with_headers(soup, heading=None, headers=None, className=None):
    extracted_data = []
    table = None

    if className:
        table = soup.find("table", class_=className)

    elif heading:
        heading_element = soup.find("h2", string=re.compile(heading, re.IGNORECASE))
        if heading_element:
            table = heading_element.find_next("table")

    if table and headers:
        rows = table.find_all("tr")[1:] 
        for row in rows:
            cols = row.find_all("td")
            if len(cols) == len(headers):
                entry = {headers[i]: clean_text(cols[i].get_text()) for i in range(len(headers))}
                extracted_data.append(entry)

    return extracted_data

def extract_and_upload_orders(soup, s3_client, session, BUCKET_NAME, REGION_NAME, case_info):
    orders = []
    table = soup.find("table", class_="order_table")

    if not table:
        return orders

    rows = table.find_all("tr")[1:]  
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 5:
            continue

        order_number = clean_text(cols[0].get_text())
        order_date = clean_text(cols[3].get_text())

        link_tag = cols[4].find("a", href=True)
        if not link_tag:
            continue

        final_pdf_url = BASE_URL + link_tag["href"]

        s3_folder_path = f"case_data/orders/{case_info['cino']}/"
        s3_file_path = f"{s3_folder_path}{case_info['cino']}-{order_number}.pdf"

        try:
            s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_file_path)
            s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
        except s3_client.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "404":
                response = session.get(final_pdf_url, stream=True)
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
                    print(f"❌ Failed to fetch PDF from {final_pdf_url}")
                    s3_url = None
            else:
                print(f"❌ S3 Error: {e}")
                s3_url = None

        orders.append({
            "order_number": order_number,
            "order_date": order_date,
            "order_link": s3_url
        })

    return orders
def extract_high_court_case_history(soup):
    history = []
    for table in soup.find_all("table", {"class": "history_table"}):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]

        if "Cause List Type" in headers and "Purpose of hearing" in headers:
            rows = table.find_all("tr")[1:]  
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 5:
                    cause_list_type = cols[0].get_text(strip=True)
                    judge = cols[1].get_text(strip=True)
                    business_on_date = (
                        cols[2].find("a").get_text(strip=True)
                        if cols[2].find("a")
                        else cols[2].get_text(strip=True)
                    )
                    hearing_date = cols[3].get_text(strip=True)
                    purpose = cols[4].get_text(strip=True)
                    if (
                        cause_list_type == "Order Number"
                        or "Order on" in judge
                        or purpose in ["Order Details", "View"]
                    ):
                        continue

                    history.append({
                        "causeListType": cause_list_type,
                        "judge": judge,
                        "businessOnDate": business_on_date,
                        "hearingDate": hearing_date,
                        "purpose": purpose,
                        "inputType": "automatic",
                        "lawyerRemark": None
                    })
    return history



def parse_case_history(html, payload, second_payload, case_info, session):
    soup = BeautifulSoup(html, "html.parser")

    case_details = extract_table_data(soup, "Case Details", ["Filing Number", "Filing Date", "Registration Number", "Registration Date", "CNR Number"])
    case_status = extract_table_data(soup, "Case Status", [
        "First Hearing Date",
        "Decision Date",
        "Case Status",
        "Nature of Disposal",
        "Coram",
        "Bench Type",
        "Judicial Branch",
        "State",
        "District",
        "Not Before Me",
        "Stage of Case"
    ])

    petitioner_and_advocate = extract_party_details(soup, "Petitioner_Advocate_table")
    respondent_and_advocate = extract_party_details(soup, "Respondent_Advocate_table")
    category_details = extract_table_data(soup, "Category", ["Category", "Sub Category"])
    subordinate_court_info = extract_subordinate_court_info(soup)
    high_court_case_history = extract_high_court_case_history(soup)
    orders_hc = extract_and_upload_orders(
        soup,
        s3_client=s3_client,
        session=session,
        BUCKET_NAME=BUCKET_NAME,
        REGION_NAME=REGION_NAME,
        case_info=case_info
    )

    return {
            "case_no": second_payload.get("case_no"),
            "cino": second_payload.get("cino"),
            "court_code": second_payload.get("court_code"),
            "state_code": payload.get("state_code"),
            "dist_code": payload.get("dist_code"),
            "court_complex_code": payload.get("court_complex_code"),
            "est_code": payload.get("est_code"),
            "case_type": payload.get("case_type"),
            "CaseType": payload.get("CaseType"),
            "FilingNumber": case_details.get("Filing Number", ""),
            "RegistrationNumber": case_details.get("Registration Number", ""),
            "CNRNumber": second_payload.get("cino"),
            "FirstHearingDate": case_status.get("First Hearing Date", ""),
            "CaseStatus": case_status.get("Stage of Case", ""),
            "CourtNumberandJudge": case_status.get("Coram", ""),
            "petitioner_and_advocate": petitioner_and_advocate,
            "respondent_and_advocate": respondent_and_advocate,
            "actsandSection": {},
            "category_details": category_details,
            "subordinate_court_information": subordinate_court_info,
            "case_history": high_court_case_history,
            "case_transfer" : [],
            "orders": orders_hc 
    }


@app.post("/getcaseInfo")
def fetch_submit_info(case_data: CaseRequest):
    session = requests.Session()
    query = case_data.dict()

    if case_data.refresh_flag != "1":
        existing_case = collection.find_one(query)
        if existing_case:
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=existing_case)
    
    case_info = {}

    try:
            payload = {
                'ajax_req': 'true',
                'case_type': case_data.case_type,
                'case_no': case_data.case_no,
                'rgyear': case_data.rgyear,
                'state_code': case_data.state_code,
                'dist_code': case_data.dist_code,
                'court_complex_code': case_data.court_complex_code,
                'est_code': case_data.est_code,
                'search_case_no' : case_data.case_no
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
                        "court_code": int(values[2]) if values[2].isdigit() else None,
                        "state_code": int(values[5]) if values[5].isdigit() else None,
                        "dist_code": int(values[6]) if values[6].isdigit() else None,
                        "court_complex_code": int(values[7]) if values[7].isdigit() else None,
                        "est_code":case_data.est_code,
                        "case_type" : case_data.case_type
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
                        soup = BeautifulSoup(case_details.get("data_list", ""), "html.parser")
                        def extract_table_data(table_class):
                            table = soup.find("table", {"class": table_class})
                            data = {}
                            if table:
                                rows = table.find_all("tr")
                                for row in rows:
                                    cells = row.find_all("td")
                                    if len(cells) >= 2:
                                        key = cells[0].get_text(strip=True).replace(':', '').replace(' ', '')
                                        value = cells[1].get_text(strip=True)
                                        data[key] = value
                            return data

                        case_status = extract_table_data("table case_status_table table-bordered")
                        case_details = extract_table_data("table case_details_table table-bordered")

                        def extract_list_data(table_class):
                            table = soup.find("table", {"class": table_class})
                          
                            values = []
                            if table:
                                cell = table.find("td")
                                if cell:
                                    values = [line.strip() for line in cell.stripped_strings if line.strip()]
                            return values

                        case_petitioner = {"petitioner_and_advocate": extract_list_data("table table-bordered Petitioner_Advocate_table")}
                        case_respondent = {"respondent_and_advocate": extract_list_data("table table-bordered Respondent_Advocate_table")}
                        def extract_fir_details(table_class):
                            table = soup.find("table", class_=lambda x: x and table_class in x)
                            print(table)
                            details = {}
                            if table:
                                rows = table.find_all("tr")
                                for row in rows:
                                    cols = row.find_all("td")
                                    if len(cols) == 2:
                                        key = cols[0].get_text(strip=True).replace(" ", "")
                                        value = cols[1].get_text(strip=True)
                                        details[key] = value
                            return details

                        case_fir_details = {"fir_details": extract_fir_details("FIR_details_table")}



                        act_table = soup.find("table", {"class": "table acts_table table-bordered"})
                        acts_and_sections = {"actsandSection": {"acts": "null", "section": "null"}}

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
                            table = soup.find("table", {"class": "transfer_table table"})
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
                                    print(f"⚠️ Skipping row with insufficient columns: {row}")
                                    continue

                                order_number = cols[0].text.strip()
                                order_date = cols[1].text.strip()
                                order_link = cols[2].find("a")

                                if not order_link:
                                    print(f"⚠️ No anchor tag found for order {order_number}. Skipping PDF fetch.")
                                    orders.append({
                                        "order_number": order_number,
                                        "order_date": order_date,
                                        "order_link": None,
                                        "note": "No order link available"
                                    })
                                    continue

                                onclick_attr = order_link.get("onclick", "")
                                match = re.search(r"displayPdf\((.*?)\)", onclick_attr)

                                if not match:
                                    print(f"⚠️ No 'onclick' match found for order {order_number}. Skipping PDF fetch.")
                                    orders.append({
                                        "order_number": order_number,
                                        "order_date": order_date,
                                        "order_link": None,
                                        "note": "No valid onclick for PDF"
                                    })
                                    continue

                                params = match.group(1)
                                values = [v.strip().strip("'") for v in params.split("&")]

                                if not values:
                                    print(f"⚠️ Invalid onclick parameters for order {order_number}. Skipping.")
                                    continue

                                base_path = values[0]
                                query_string = "&".join(values[1:])
                                full_url = f"https://services.ecourts.gov.in/ecourtindia_v6/?p={base_path}&{query_string}"

                                order_payload = {
                                    "app_token": app_token,
                                    "ajax_req": "true"
                                }

                                order_response = session.post(full_url, data=order_payload)

                                try:
                                    token_update = order_response.json()
                                    new_app_token = token_update.get("app_token")
                                    if new_app_token:
                                        app_token = new_app_token
                                except Exception:
                                    pass
                                
                                order_response_data = order_response.json()
                                print("order_response_data", order_response_data)

                                pdf_file_path = order_response_data.get("order", "").replace("\\", "")

                                if not pdf_file_path:
                                    print(f"❌ PDF path not found in object for order {order_number}.")
                                    continue

                                final_pdf_url = f"https://services.ecourts.gov.in/ecourtindia_v6/{pdf_file_path}"
                                print(f"✅ Final PDF URL: {final_pdf_url}")


                                s3_folder_path = f"case_data/orders/{case_info['cino']}/"
                                s3_file_path = f"{s3_folder_path}{case_info['cino']}-{order_number}.pdf"
                                try:
                                    s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_file_path)
                                    s3_url = f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{s3_file_path}"
                                except s3_client.exceptions.ClientError as e:
                                    if e.response['Error']['Code'] == "404":
                                        response = session.get(final_pdf_url, stream=True)
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
                                            print(f"❌ Failed to fetch PDF from {final_pdf_url}")
                                            s3_url = None
                                    else:
                                        print(f"❌ S3 Error: {e}")
                                        s3_url = None

                                orders.append({
                                    "order_number": order_number,
                                    "order_date": order_date,
                                    "order_link": s3_url
                                })

                        final_response = {**case_info,**case_fir_details, **case_details, **case_status, **case_petitioner, **case_respondent, **acts_and_sections, **case_history, **case_transfer,"orders": orders}
                        inserted_doc = collection.insert_one(final_response)
                        final_response["_id"] = str(inserted_doc.inserted_id)

                        return JSONResponse(content=final_response)
                    else:
                        return JSONResponse(content={"error": "Failed to fetch case details"}, status_code=403)

            return JSONResponse(content={"error": "Case details not found"}, status_code=403)


    finally:
        session.close()


@app.post("/hc/getcaseInfo")
def fetch_submit_hc_info(case_data: CaseRequest):
    session = requests.Session()
    query = case_data.dict()
    if case_data.refresh_flag != "1":
        existing_case = collection.find_one(query)
        if existing_case:
            existing_case["_id"] = str(existing_case["_id"])
            return JSONResponse(content=existing_case)

    try:
        payload = {
            'court_code': case_data.court_complex_code,
            'case_type': case_data.case_type,
            'case_no': case_data.case_no,
            'rgyear': case_data.rgyear,
            'state_code': case_data.state_code,
            'dist_code': case_data.dist_code,
            'caseStatusSearchType': 'CScaseNumber',
            'court_complex_code': case_data.court_complex_code,
            'est_code': case_data.est_code,
            'caseNoType': 'new',
            'search_case_no': case_data.case_no
        }

        max_attempts = 20
        for attempt in range(max_attempts):
            captcha_url = f"https://hcservices.ecourts.gov.in/hcservices/securimage/securimage_show.php?{random.randint(100000,999999)}"
            captcha_text = decode_captcha(session, captcha_url)
            captcha_text = re.sub(r'[^A-Za-z0-9]', '', captcha_text)
            payload['captcha'] = captcha_text

            response = session.post(
                "https://hcservices.ecourts.gov.in/hcservices/cases_qry/index_qry.php?action_code=showRecords",
                data=payload
            )

            raw_text = response.text.strip()
            soup = BeautifulSoup(raw_text, "html.parser")
            clean_text = soup.get_text()

            tot_records_match = re.search(r'"totRecords"\s*:\s*(\d+)', clean_text)
            tot_records = int(tot_records_match.group(1)) if tot_records_match else None

            if tot_records == 0:
                return JSONResponse(
                    content={"error": "Invalid case details"},
                    status_code=404
                )

            if '"Invalid Captcha"' in clean_text or '"ERROR_VAL"' in clean_text:
                continue  

            con_match = re.search(r'"con"\s*:\s*\["(.*?)"\]', clean_text)
            case_details = None
            if con_match:
                con_data = con_match.group(1).encode('utf-8').decode('unicode_escape')
                try:
                    case_details = json.loads(con_data)[0]
                except Exception:
                    pass

            if case_details:
                second_payload = {
                    "court_code": case_data.court_complex_code,
                    "state_code": case_data.state_code,
                    "court_complex_code": case_data.court_complex_code,
                    "case_no": case_details.get("case_no"),
                    "cino": case_details.get("cino"),
                }

                headers = {
                'Origin': 'https://hcservices.ecourts.gov.in',
                'Referer': 'https://hcservices.ecourts.gov.in/',
                'Content-Type': 'application/x-www-form-urlencoded',
                }
                second_resp = session.post(
                    "https://hcservices.ecourts.gov.in/hcservices/cases_qry/o_civil_case_history.php",
                    data=second_payload,
                    headers=headers
                )

                testdata = parse_case_history(second_resp.text,payload,second_payload, case_details, session)

                print(testdata)

                return testdata

        else:
            return JSONResponse(
                content={"error": "Failed to fetch case info after multiple attempts"},
                status_code=400
            )

    finally:
        session.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,port=8000)


# handler = Mangum(app)