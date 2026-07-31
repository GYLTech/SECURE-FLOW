import json
import re


def clean_captcha_text(text):
    if not text:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(text))


def solve_captcha(lambda_client, image_base64, frm="hc", function_name="GYL-MS-Swipe-Captcha-Solver-V1"):
    
    try:
        lambda_payload = {
            "image_base64": image_base64,
            "frm": frm
        }

        lambda_response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(lambda_payload)
        )

        response_payload = lambda_response["Payload"].read().decode()
        lambda_data = json.loads(response_payload)

        return clean_captcha_text(lambda_data.get("text")) or None

    except Exception as e:
        print(f"Error solving captcha: {e}")
        return None