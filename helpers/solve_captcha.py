import json
import re


# SCI serves arithmetic captchas ("8 + 5"), so the operator has to survive
# cleaning. eCourts captchas are alphanumeric and keep the old behaviour.
MATH_CHARS = "+-x*"

# OCR sometimes returns the typographic variants of the operators
OPERATOR_LOOKALIKES = str.maketrans({"−": "-", "–": "-", "—": "-", "×": "x", "X": "x"})


def clean_captcha_text(text, keep=""):
    if not text:
        return ""
    allowed = "A-Za-z0-9" + re.escape(keep)
    normalized = str(text).translate(OPERATOR_LOOKALIKES) if keep else str(text)
    return re.sub(r"[^" + allowed + r"]", "", normalized)


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

        keep = MATH_CHARS if frm == "sci" else ""
        return clean_captcha_text(lambda_data.get("text"), keep=keep) or None

    except Exception as e:
        print(f"Error solving captcha: {e}")
        return None
