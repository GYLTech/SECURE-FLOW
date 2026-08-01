import hashlib
import os
import re
import time
from urllib.parse import parse_qs, urlparse

ORDERS_RECHECK_SECONDS = int(os.getenv("ORDERS_RECHECK_SECONDS", str(24 * 3600)))


def orders_stamp() -> float:
    return time.time()


def cached_case_needs_orders(existing_case) -> bool:
    """True when a cached case should be re-scraped just for its orders.

    A case stored with an empty order list is ambiguous: the court may have
    published nothing, or we may have captured it before order extraction
    worked. Serving that cache forever means the order can never appear, so an
    empty list is re-checked - but only once per ORDERS_RECHECK_SECONDS, so a
    genuinely order-less case is not re-scraped on every import.
    """
    if not existing_case:
        return False
    if existing_case.get("orders"):
        return False

    checked_at = existing_case.get("orders_synced_at")
    if not checked_at:
        return True

    try:
        return (time.time() - float(checked_at)) > ORDERS_RECHECK_SECONDS
    except (TypeError, ValueError):
        return True


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
