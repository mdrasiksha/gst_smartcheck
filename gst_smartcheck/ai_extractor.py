from openai import OpenAI
import json

client = OpenAI()

def extract_with_gpt(ocr_text: str) -> dict:
    prompt = f"""
Extract structured GST invoice data.

Return STRICT JSON:

{{
  "invoice_number": "",
  "date": "",
  "taxable_amount": "",
  "cgst": "",
  "sgst": "",
  "igst": "",
  "final_amount": ""
}}

Rules:
- Prefer "Grand Total" for final_amount
- Ignore handwritten notes
- Handle proforma/quotation invoices
- If not found, return null
- Return only JSON (no explanation)

Invoice:
<<<
{ocr_text}
>>>
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    content = response.choices[0].message.content

    try:
        return json.loads(content)
    except:
        return {
            "error": "Invalid JSON from GPT",
            "raw": content
        }
