import hashlib
import re
from urllib.parse import parse_qs, urlparse


def stable_order_doc_id(source_ref: str) -> str:
    return hashlib.md5(source_ref.encode("utf-8")).hexdigest()[:12]


def order_pdf_s3_key(orders_prefix: str, order_date: str, source_ref: str) -> str:
    date_slug = re.sub(r"[^0-9A-Za-z]+", "-", (order_date or "").strip()).strip("-")
    date_slug = date_slug or "undated"
    return f"{orders_prefix}order-{date_slug}-{stable_order_doc_id(source_ref)}.pdf"


def hc_source_ref(pdf_url: str) -> str:
    query = parse_qs(urlparse(pdf_url).query)
    values = query.get("filename")
    return values[0] if values else pdf_url
