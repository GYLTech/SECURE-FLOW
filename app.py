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

load_dotenv()

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
                'est_code': case_data.est_code
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


# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app,port=8000)


handler = Mangum(app)