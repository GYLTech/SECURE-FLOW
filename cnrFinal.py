from fastapi import FastAPI
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import pytesseract
import time
import json
from bs4 import BeautifulSoup
import re

app = FastAPI()

class CaseRequest(BaseModel):
    case_number: str

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--incognito")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920x1080")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

service = Service(
    'C:/Users/hunter/Downloads/chromedriver-win32/chromedriver-win32/chromedriver.exe'
)

def extract_pdf_link(display_pdf_function: str):
    try:
        match = re.search(r"displayPdf\('(.+?)'\)", display_pdf_function)
        if match:
            params = match.group(1)
            base_url = "https://services.ecourts.gov.in/ecourtindia_v6/"
            param_dict = dict(re.findall(r"(\w+)=([^&]+)", params))
            filename = param_dict.get("filename", "")
            if filename:
                pdf_url = f"{base_url}reports/{filename.strip()}"
                return pdf_url

        return "N/A"
    except Exception as e:
        print(f"Error parsing PDF link: {e}")
        return "N/A"

def scrape_case_details(case_number: str):
    retries = 3
    retry_count = 0

    while retry_count < retries:
        driver = webdriver.Chrome(service=service, options=chrome_options)
        try:
            driver.get(
                "https://services.ecourts.gov.in/ecourtindia_v6/?p=home/index&app_token=afc5f6c6ea828cb82e85acffa921a9042b857cfe3c889eb4e1c27e88942a741a")
            time.sleep(1)

            input_field = driver.find_element(
                By.XPATH, '/html/body/div[1]/div/main/div[2]/div/form/input')
            input_field.send_keys(case_number)

            # Retry loop for CAPTCHA
            for attempt in range(3):
                captcha_image = driver.find_element(
                    By.XPATH, '/html/body/div[1]/div/main/div[2]/div/form/div/div/div/div/img')
                captcha_image.screenshot('captcha_image.png')
                captcha_text = pytesseract.image_to_string(
                    'captcha_image.png', config='--psm 6').strip()
                print(f"CAPTCHA Text (Attempt {attempt + 1}): {captcha_text}")

                captcha_input = driver.find_element(
                    By.XPATH, '/html/body/div[1]/div/main/div[2]/div/form/div/input')
                captcha_input.clear()
                captcha_input.send_keys(captcha_text)

                submit_button = driver.find_element(
                    By.XPATH, '/html/body/div[1]/div/main/div[2]/div/form/button[1]')
                submit_button.click()

                time.sleep(5)

                try:
                    error_message = driver.find_element(
                        By.XPATH, "//span[contains(text(), 'Invalid CAPTCHA')]")
                    if error_message.is_displayed():
                        print(
                            f"Invalid CAPTCHA detected, retrying... (Attempt {attempt + 1})")

                        # Retry after clicking 'Invalid CAPTCHA' button
                        invalid_captcha_button = driver.find_element(
                            By.XPATH, '/html/body/div[7]/div/div/div[1]/button')
                        invalid_captcha_button.click()

                        back_button = driver.find_element(
                            By.XPATH, '/html/body/div[1]/div/main/p/button')
                        back_button.click()

                        time.sleep(2)
                        continue

                except:
                    # If no error message, CAPTCHA was correct
                    break

            else:
                # After 3 attempts, if CAPTCHA is still invalid, continue to the next retry
                print("Failed to solve CAPTCHA after multiple attempts.")
                retry_count += 1
                continue  # Move to the next retry cycle without quitting the browser

            try:
                case_details_div = driver.find_element(By.ID, "history_cnr")
                full_html_content = case_details_div.get_attribute("outerHTML")
                soup = BeautifulSoup(full_html_content, "html.parser")

                court = soup.find("h2", {"id": "chHeading"}).text.strip() if soup.find("h2", {"id": "chHeading"}) else "N/A"
                case_details_table = soup.find("table", {"class": "case_details_table"})
                rows = case_details_table.find_all("tr") if case_details_table else []

                case_type = rows[0].find_all("td")[1].text.strip() if len(rows) > 0 else "N/A"
                filing_number = rows[1].find_all("td")[1].text.strip() if len(rows) > 1 else "N/A"
                filing_date = rows[1].find_all("td")[3].text.strip() if len(rows) > 1 else "N/A"
                registration_number = rows[2].find_all("td")[1].text.strip() if len(rows) > 2 else "N/A"
                registration_date = rows[2].find_all("td")[3].text.strip() if len(rows) > 2 else "N/A"
                cnr_number = rows[3].find_all("td")[1].text.strip() if len(rows) > 3 else "N/A"

                case_status_table = soup.find("table", {"class": "case_status_table"})
                
                first_hearing_date_cell = case_status_table.find("td", string="First Hearing Date")
                first_hearing_date = first_hearing_date_cell.find_next("td").text.strip() if first_hearing_date_cell else "N/A"

                next_hearing_date_cell = case_status_table.find("td", string="Next Hearing Date")
                next_hearing_date = next_hearing_date_cell.find_next("td").text.strip() if next_hearing_date_cell else "N/A"

                case_stage_cell = case_status_table.find("td", string="Case Stage")
                case_stage = case_stage_cell.find_next("td").text.strip() if case_stage_cell else "N/A"

                case_sub_stage_cell = case_status_table.find("td", string="Sub Stage ")
                case_sub_stage = case_sub_stage_cell.find_next("td").text.strip() if case_sub_stage_cell else "N/A"

                court_number_and_judge_cell = case_status_table.find("td", string="Court Number and Judge")
                court_number_and_judge = court_number_and_judge_cell.find_next("td").text.strip() if court_number_and_judge_cell else "N/A"

                petitioner = soup.find("table", {"class": "Petitioner_Advocate_table"}).text.strip() if soup.find("table", {"class": "Petitioner_Advocate_table"}) else "N/A"
                respondent = soup.find("table", {"class": "Respondent_Advocate_table"}).text.strip() if soup.find("table", {"class": "Respondent_Advocate_table"}) else "N/A"

                acts = [
                    {
                        "act": act.text.strip(),
                        "section": section.text.strip(),
                    }
                    for act, section in zip(soup.select("table#act_table td:nth-child(1)"), soup.select("table#act_table td:nth-child(2)"))
                ]

                history = [
                    {
                        "judge": row.find_all("td")[0].text.strip(),
                        "business_on_date": row.find_all("td")[1].text.strip(),
                        "hearing_date": row.find_all("td")[2].text.strip(),
                        "purpose_of_hearing": row.find_all("td")[3].text.strip(),
                    }
                    for row in soup.select("table.history_table tbody tr")
                ]

                order_modal = soup.find("div", {"id": "modal_order_body"})
                pdf_link_match = re.search(r'reports/\S+\.pdf', order_modal.prettify()) if order_modal else None
                pdf_link = f"https://services.ecourts.gov.in/ecourtindia_v6/{pdf_link_match.group()}" if pdf_link_match else "N/A"

                orders = []
                order_table = soup.find("table", {"class": "order_table"})
                if order_table:
                    for row in order_table.find_all("tr")[1:]:
                        cells = row.find_all("td")
                        order_number = cells[0].text.strip()
                        order_date = cells[1].text.strip()
                        order_details = cells[2].text.strip()
                        orders.append({
                            "order_number": order_number,
                            "order_date": order_date,
                            "order_details": order_details,
                            "pdf_link": pdf_link,
                        })

                transfer_table = soup.find("table", {"class": "transfer_table"})
                transfer_history = []
                if transfer_table:
                    rows = transfer_table.find_all("tr")[1:]  
                    for row in rows:
                        cells = row.find_all("td")
                        transfer_history.append({
                            "registration_number": cells[0].text.strip(),
                            "transfer_date": cells[1].text.strip(),
                            "from_court_and_judge": cells[2].text.strip(),
                            "to_court_and_judge": cells[3].text.strip()
                        }) 
                Fir_table = soup.find("table", {"class": "FIR_details_table table table_o"})
                fir_details = {}
                if Fir_table:
                    rows = Fir_table.find_all("tr")
                    for row in rows:
                        cells = row.find_all("td")
                        if len(cells) == 2:
                            label = cells[0].text.strip()
                            value = cells[1].text.strip()
                            if label == "Police Station":
                                fir_details["Police_station"] = value
                            elif label == "FIR Number":
                                fir_details["FirNumber"] = value
                            elif label == "Year":
                                fir_details["Year"] = value

                if not fir_details:
                    fir_details = {
                        "Police_station": "N/A",
                        "FirNumber": "N/A",
                        "Year": "N/A"
                    }            
                return {
                    "message": "Case details scraped successfully!",
                    "court": court,
                    "case_details": {
                        "case_type": case_type,
                        "filing_number": filing_number,
                        "filing_date": filing_date,
                        "registration_number": registration_number,
                        "registration_date": registration_date,
                        "cnr_number": cnr_number,
                        "first_hearing_date": first_hearing_date,
                        "next_hearing_date": next_hearing_date,
                        "case_stage": case_stage,
                        "case_sub_stage": case_sub_stage,
                        "court_number_and_judge": court_number_and_judge
                    },
                    "respondent":respondent,
                    "petitioner":petitioner,
                    "acts": acts,
                    "history": history,
                    "orders": orders,
                    "fir_details": fir_details,
                    "transfer_history": transfer_history
                }

            except Exception as e:
                print(f"Error scraping case details for case number {case_number}: {e}")
                retry_count += 1
                continue  

        finally:
            driver.quit()

@app.post("/scrape_case")
async def scrape_case(case: CaseRequest):
    result = scrape_case_details(case.case_number)
    return result