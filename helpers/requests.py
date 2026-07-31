from http.client import RemoteDisconnected
import random
import requests
import time


def safe_get(session, url, params=None, max_retries=5,headers=None):
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(1.5, 2.0))

            response = session.get(
                url,
                params=params,
                timeout=(30, 180),
                headers=headers
            )

            return response

        except (requests.exceptions.ConnectionError, RemoteDisconnected) as e:
            print(f"[warn] Server disconnected (attempt {attempt + 1})")

            session.close()
            session = requests.Session()

        except requests.exceptions.Timeout:
            print(f"[warn] Timeout (attempt {attempt + 1})")

    raise Exception("[error] GET request failed after retries")

def safe_post(session, url, data, headers,max_retries=5):
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(1.5, 2.0))

            response = session.post(
                url,
                data=data,
                timeout=(30, 180),
                headers=headers
            )
            return response

        except (requests.exceptions.ConnectionError, RemoteDisconnected) as e:
            print(f"[warn] Server disconnected (attempt {attempt+1})")

            session.close()
            session = requests.Session()

        except requests.exceptions.Timeout:
            print(f"[warn] Timeout (attempt {attempt+1})")

    raise Exception("[error] eCourts viewHistory failed after retries")
