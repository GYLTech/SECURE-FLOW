from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["test"]
collection = db["casedetails"]

collection.create_index("cino", unique=True)
collection.create_index("state_code")
collection.create_index("dist_code")
collection.create_index("court_type")
collection.create_index("court_complex_code")
collection.create_index("case_no")
collection.create_index("rgyear")

print("Indexes created successfully")
