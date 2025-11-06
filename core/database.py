from pymongo import MongoClient
from dotenv import load_dotenv
import os 
load_dotenv()
client = MongoClient(os.getenv("MONGOCLIENT"))
db = client["gylscrdata"]
collection = db["casedetails"]
