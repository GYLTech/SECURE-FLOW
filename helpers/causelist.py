import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from core.s3_client import s3_client

load_dotenv()
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION_NAME = os.getenv("REGION_NAME")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def s3_url(key):
    return f"https://{BUCKET_NAME}.s3.{REGION_NAME}.amazonaws.com/{key}"


def mirror_pdf(url, folder):
    key = f"case_data/causelists/{folder}/{hashlib.md5(url.encode('utf-8')).hexdigest()}.pdf"
    try:
        s3_client.head_object(Bucket=BUCKET_NAME, Key=key)
        return s3_url(key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            return url
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(10, 90))
    except requests.exceptions.RequestException:
        return url
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return url
    s3_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=response.content,
                         ContentType="application/pdf", ContentDisposition="inline")
    return s3_url(key)


def mirror_cause_lists(lists, folder, workers=4):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        links = list(pool.map(lambda item: mirror_pdf(item["causelist_link"], folder), lists))
    for item, link in zip(lists, links):
        item["source_link"] = item["causelist_link"]
        item["causelist_link"] = link
    return lists
