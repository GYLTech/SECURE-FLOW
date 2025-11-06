import boto3
from dotenv import load_dotenv
import os 
load_dotenv()

s3_client = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_S3_KEY"),
    aws_secret_access_key=os.getenv("AWS_S3SEC_KEY")
)