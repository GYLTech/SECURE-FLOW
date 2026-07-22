import json
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv
import os
load_dotenv()
client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["gylscrdata"]
collection = db["casedetails"]



APPEND_ONLY_ARRAYS = {
    "case_history": (),
    "case_transfer": (),
}

REPLACE_ARRAYS = ("orders",)

_MERGE_PROJECTION = {
    field: 1 for field in (*APPEND_ONLY_ARRAYS, *REPLACE_ARRAYS)
}

def _item_key(item, key_fields):
    """Return a hashable identity for a list item used to detect duplicates."""
    if isinstance(item, dict) and key_fields:
        return tuple(item.get(k) for k in key_fields)

    return json.dumps(item, sort_keys=True, default=str)


def _merge_append_only(existing_doc, result):
    if not existing_doc:
        return result

    for field in REPLACE_ARRAYS:
        new_items = result.get(field)
        if isinstance(new_items, list) and not new_items:
            old_items = existing_doc.get(field)
            if isinstance(old_items, list) and old_items:
                result[field] = old_items

    for field, key_fields in APPEND_ONLY_ARRAYS.items():
        new_items = result.get(field)
        if not isinstance(new_items, list):
            continue  # nothing fresh to merge for this field
        old_items = existing_doc.get(field)
        old_items = old_items if isinstance(old_items, list) else []
        seen = {_item_key(it, key_fields) for it in old_items}
        combined = list(old_items)
        for it in new_items:
            k = _item_key(it, key_fields)
            if k not in seen:
                seen.add(k)
                combined.append(it)
        result[field] = combined
    return result


def save_case(result, existing_case_id=None):

    if existing_case_id:
        existing = collection.find_one({"_id": existing_case_id}, _MERGE_PROJECTION)
        _merge_append_only(existing, result)
        collection.update_one({"_id": existing_case_id}, {"$set": result})
        return str(existing_case_id)

    try:
        insert_result = collection.insert_one({**result})
        return str(insert_result.inserted_id)
    except DuplicateKeyError:
        cino = result.get("cino")
        existing = collection.find_one({"cino": cino}, _MERGE_PROJECTION)
        _merge_append_only(existing, result)
        collection.update_one({"cino": cino}, {"$set": result})
        return str(existing["_id"]) if existing else ""
