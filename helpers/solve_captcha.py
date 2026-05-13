import json

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

        return lambda_data.get("text")

    except Exception as e:
        print(f"Error solving captcha: {e}")
        return None