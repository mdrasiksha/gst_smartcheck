from openai import OpenAI
import base64
import json

client = OpenAI()


_REQUIRED_FIELDS = {
    "invoice_number": "",
    "date": "",
    "taxable_amount": "",
    "cgst": "",
    "sgst": "",
    "igst": "",
    "final_amount": "",
}


def _json_only_prompt(ocr_text: str) -> str:
    return f"""Extract only these GST invoice fields and return strict JSON only:
{json.dumps(_REQUIRED_FIELDS, indent=2)}

Rules:
- Use null for missing values
- Do not return markdown
- Do not include explanation or extra keys

Invoice text:
<<<
{ocr_text[:3000]}
>>>
"""


def extract_with_gpt(ocr_text: str) -> dict:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": _json_only_prompt(ocr_text)}],
        temperature=0,
        max_tokens=250,
    )

    content = response.choices[0].message.content

    try:
        return json.loads(content)
    except Exception:
        return {
            "error": "Invalid JSON from GPT",
            "raw": content
        }


def extract_from_image_with_gpt(image_bytes: bytes) -> dict:
    encoded = base64.b64encode(image_bytes).decode()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extract only required GST fields: invoice_number, date, taxable_amount, "
                            "cgst, sgst, igst, final_amount. Return strict JSON only with these keys."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{encoded}"
                        }
                    }
                ]
            }
        ],
        temperature=0,
        max_tokens=250,
    )

    content = response.choices[0].message.content

    try:
        return json.loads(content)
    except Exception:
        return {
            "error": "Invalid JSON from GPT Vision",
            "raw": content
        }
