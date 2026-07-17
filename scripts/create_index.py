from pymongo import ASCENDING, MongoClient
from dotenv import load_dotenv
import os

from core import jobs

load_dotenv()

client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["gylscrdata"]
collection = db["casedetails"]

collection.create_index("cino", unique=True)
collection.create_index("state_code")
collection.create_index("dist_code")
collection.create_index("court_type")
collection.create_index("court_complex_code")
collection.create_index("case_no")
collection.create_index("rgyear")

collection.create_index(
    [
        ("case_reg_no", ASCENDING),
        ("rgyear", ASCENDING),
        ("case_type", ASCENDING),
        ("state_code", ASCENDING),
        ("court_complex_code", ASCENDING),
    ],
    name="hc_getcaseinfo_lookup",
)
collection.create_index(
    [("courtType", ASCENDING), ("cino", ASCENDING)],
    name="hc_bulk_ingest_lookup",
)

jobs.ensure_indexes()

print("Indexes created successfully")
