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


def _clean_ocr_text(text: str) -> str:
    text = text or ""
    text = text.replace("\n\n", "\n")
    text = text.replace("  ", " ")
    text = text.strip()
    return text[:3000]


def _json_only_prompt(ocr_text: str) -> str:
    cleaned_text = _clean_ocr_text(ocr_text)
    return f"""You are an expert in Indian GST invoice processing.

Extract structured data and return STRICT JSON:

{json.dumps(_REQUIRED_FIELDS, indent=2)}

Rules:
- "final_amount" MUST be the final payable amount (Grand Total / Net Total)
- If multiple totals exist, always choose final payable amount
- Ignore handwritten text, stamps, logos
- Handle quotation/proforma invoices
- All values must be numeric (no commas, no ₹ symbol)
- If missing, return null

Validation rule:
final_amount = taxable_amount + cgst + sgst + igst (if available)

Return ONLY JSON (no explanation)

Invoice text:
<<<
{cleaned_text}
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
                        "text": _json_only_prompt("Read this invoice image and extract fields."),
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
