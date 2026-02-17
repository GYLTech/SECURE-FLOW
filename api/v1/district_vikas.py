# import random
# import time
# import requests
# from bs4 import BeautifulSoup
# from fastapi import APIRouter
# from fastapi.responses import JSONResponse
# import re
# from pydantic import BaseModel
# from typing import Optional
# from dotenv import load_dotenv
# from core.database import collection
# from core.s3_client import s3_client
# import os
# from http.client import RemoteDisconnected

# load_dotenv()

# BUCKET_NAME = os.getenv("BUCKET_NAME")
# REGION_NAME = os.getenv("REGION_NAME")

# app = APIRouter()

# # =====================================================
# # ------------------ MODELS ---------------------------
# # =====================================================

# class CaseRequest(BaseModel):
#     case_type: str
#     case_reg_no: str
#     rgyear: str
#     state_code: str
#     dist_code: str
#     court_complex_code: str
#     est_code: Optional[str] = None
#     refresh_flag: str


# class CaseRequestBulk(BaseModel):
#     petres_name: str
#     rgyearP: str
#     case_status: str
#     state_code: str
#     dist_code: str
#     court_complex_code: str
#     est_code: Optional[str] = None
#     courtType: Optional[str] = None


# class CaseRequestBulkAdvocate(BaseModel):
#     advocate_name: str
#     rgyear: str
#     case_status: str
#     state_code: str
#     dist_code: str
#     court_complex_code: str
#     caselist_date: str
#     adv_captcha_code: str
#     app_token: str
#     est_code: Optional[str] = None
#     courtType: Optional[str] = None


# # =====================================================
# # ------------------ HELPERS --------------------------
# # =====================================================

# def sanitize_key(key):
#     key = re.sub(r'[.$]', '', key)
#     key = key.replace(':', '').replace(' ', '')
#     return key


# def safe_post(session, url, data, max_retries=3):
#     for attempt in range(max_retries):
#         try:
#             time.sleep(random.uniform(1.2, 2.0))
#             response = session.post(
#                 url,
#                 data=data,
#                 timeout=(10, 120),
#                 headers={"Connection": "close"}
#             )
#             return response
#         except (requests.exceptions.ConnectionError, RemoteDisconnected):
#             session.close()
#             session = requests.Session()
#         except requests.exceptions.Timeout:
#             continue

#     raise Exception("eCourts request failed after retries")


# # =====================================================
# # ------------------ CASE NUMBER SEARCH ---------------
# # =====================================================

# @app.post("/getcaseInfo")
# def fetch_case_info(case_data: CaseRequest):

#     session = requests.Session()
#     query = case_data.dict()

#     ac_query = {
#         "case_reg_no": query.get("case_reg_no"),
#         "rgyear": query.get("rgyear"),
#         "est_code": query.get("est_code"),
#         "case_type": query.get("case_type"),
#         "state_code": query.get("state_code"),
#         "dist_code": query.get("dist_code"),
#         "court_complex_code": query.get("court_complex_code")
#     }

#     if case_data.refresh_flag != "1":
#         existing = collection.find_one(ac_query)
#         if existing:
#             existing["_id"] = str(existing["_id"])
#             return JSONResponse(content=existing)

#     try:
#         payload = {
#             'ajax_req': 'true',
#             'case_type': case_data.case_type,
#             'case_no': case_data.case_reg_no,
#             'rgyear': case_data.rgyear,
#             'state_code': case_data.state_code,
#             'dist_code': case_data.dist_code,
#             'court_complex_code': case_data.court_complex_code,
#             'est_code': case_data.est_code,
#         }

#         search_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/submitCaseNo"

#         response = safe_post(session, search_url, payload)
#         html_content = response.json().get("case_data", "")

#         if "Record not found" in html_content:
#             return JSONResponse(content={"error": "Invalid case details"}, status_code=404)

#         soup = BeautifulSoup(html_content, "html.parser")
#         view_link = soup.find("a", onclick=re.compile("viewHistory"))

#         if not view_link:
#             return JSONResponse(content={"error": "View history not found"}, status_code=404)

#         match = re.search(r"viewHistory\((.*?)\)", view_link.get("onclick", ""))
#         values = [v.strip().strip("'") for v in match.group(1).split(",")]

#         case_info = {
#             "case_no": values[0],
#             "cino": values[1],
#             "court_code": values[2],
#             "state_code": values[5],
#             "dist_code": values[6],
#             "court_complex_code": values[7],
#             "est_code": case_data.est_code,
#             "case_type": case_data.case_type,
#             "rgyear": case_data.rgyear,
#             "case_reg_no": case_data.case_reg_no
#         }

#         second_payload = {
#             "app_token": response.json().get("app_token", ""),
#             "court_code": case_info["court_code"],
#             "state_code": case_info["state_code"],
#             "dist_code": case_info["dist_code"],
#             "court_complex_code": case_info["court_complex_code"],
#             "case_no": case_info["case_no"],
#             "cino": case_info["cino"],
#             "est_code": case_info["est_code"],
#             "search_flag": "CScaseNumber",
#             "search_by": "CScaseNumber",
#             "ajax_req": "true",
#         }

#         second_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=home/viewHistory"
#         second_response = safe_post(session, second_url, second_payload)

#         if second_response.status_code != 200:
#             return JSONResponse(content={"error": "History fetch failed"}, status_code=500)

#         data_json = second_response.json()
#         soup = BeautifulSoup(data_json.get("data_list", ""), "html.parser")

#         def extract_table(table_class):
#             table = soup.find("table", {"class": table_class})
#             data = {}
#             if table:
#                 for row in table.find_all("tr"):
#                     cols = row.find_all("td")
#                     if len(cols) >= 2:
#                         key = sanitize_key(cols[0].get_text(strip=True))
#                         data[key] = cols[1].get_text(strip=True)
#             return data

#         case_status = extract_table("table case_status_table table-bordered")
#         case_details = extract_table("table case_details_table table-bordered")

#         final_response = {**case_info, **case_status, **case_details}

#         result = collection.update_one(ac_query, {"$set": final_response}, upsert=True)

#         if result.upserted_id:
#             final_response["_id"] = str(result.upserted_id)
#         else:
#             doc = collection.find_one(ac_query)
#             final_response["_id"] = str(doc["_id"])

#         return JSONResponse(content=final_response)

#     finally:
#         session.close()


# # =====================================================
# # ------------------ PARTY NAME SEARCH ----------------
# # =====================================================

# @app.post("/dc/bulk_q/partyname")
# def fetch_party_cases(case_data: CaseRequestBulk):

#     session = requests.Session()

#     try:
#         payload = {
#             'ajax_req': 'true',
#             'petres_name': case_data.petres_name,
#             'rgyearP': case_data.rgyearP,
#             'case_status': case_data.case_status,
#             'state_code': case_data.state_code,
#             'dist_code': case_data.dist_code,
#             'court_complex_code': case_data.court_complex_code,
#             'est_code': case_data.est_code,
#         }

#         search_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/submitPartyName"
#         response = safe_post(session, search_url, payload)

#         html_content = response.json().get("party_data", "")
#         soup = BeautifulSoup(html_content, "html.parser")

#         results = []

#         for row in soup.find_all("tr"):
#             cols = row.find_all("td")
#             if len(cols) < 3:
#                 continue

#             view_link = row.find("a", onclick=re.compile("viewHistory"))
#             if not view_link:
#                 continue

#             match = re.search(r"viewHistory\((.*?)\)", view_link.get("onclick", ""))
#             values = [v.strip().strip("'") for v in match.group(1).split(",")]

#             results.append({
#                 "case_no": values[0],
#                 "cino": values[1],
#                 "court_code": values[2],
#                 "state_code": values[5],
#                 "dist_code": values[6],
#                 "court_complex_code": values[7],
#                 "case_number": cols[1].get_text(strip=True),
#                 "party_details": cols[2].get_text(" ", strip=True),
#                 "rgyear": case_data.rgyearP
#             })

#         return JSONResponse(content={"data": results})

#     finally:
#         session.close()


# # =====================================================
# # ------------------ ADVOCATE NAME SEARCH -------------
# # =====================================================

# @app.post("/dc/bulk_q/advocatename")
# def fetch_advocate_cases(case_data: CaseRequestBulkAdvocate):

#     session = requests.Session()

#     try:
#         payload = {
#             "radAdvt": "1",
#             "advocate_name": case_data.advocate_name,
#             "adv_bar_state": "",
#             "adv_bar_code": "",
#             "adv_bar_year": "",
#             "case_status": case_data.case_status,
#             "caselist_date": case_data.caselist_date,
#             "adv_captcha_code": case_data.adv_captcha_code,
#             "state_code": case_data.state_code,
#             "dist_code": case_data.dist_code,
#             "court_complex_code": case_data.court_complex_code,
#             "est_code": case_data.est_code,
#             "case_type": "",
#             "ajax_req": "true",
#             "app_token": case_data.app_token
#         }

#         search_url = "https://services.ecourts.gov.in/ecourtindia_v6/?p=casestatus/submitAdvName"
#         response = safe_post(session, search_url, payload)

#         html_content = response.json().get("advocate_data", "")
#         soup = BeautifulSoup(html_content, "html.parser")

#         results = []

#         for row in soup.find_all("tr"):
#             cols = row.find_all("td")
#             if len(cols) < 3:
#                 continue

#             view_link = row.find("a", onclick=re.compile("viewHistory"))
#             if not view_link:
#                 continue

#             match = re.search(r"viewHistory\((.*?)\)", view_link.get("onclick", ""))
#             values = [v.strip().strip("'") for v in match.group(1).split(",")]

#             results.append({
#                 "case_no": values[0],
#                 "cino": values[1],
#                 "court_code": values[2],
#                 "state_code": values[5],
#                 "dist_code": values[6],
#                 "court_complex_code": values[7],
#                 "case_number": cols[1].get_text(strip=True),
#                 "party_details": cols[2].get_text(" ", strip=True),
#                 "rgyear": case_data.rgyear
#             })

#         return JSONResponse(content={"data": results})

#     finally:
#         session.close()
