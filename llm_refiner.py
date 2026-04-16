import json
import os
import re

try:
    from openai import OpenAI
    print("OpenAI loaded successfully")
except ImportError:
    OpenAI = None


def _relevant_ocr_snippet(raw_text: str) -> str:
    keywords = ["invoice", "total", "amount", "gst", "date"]
    lines = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    filtered = [line for line in lines if any(keyword in line.lower() for keyword in keywords)]
    selected = filtered[:80] if filtered else lines[:80]
    snippet = "\n".join(selected)
    return snippet[:1500]


def _clean_json_block(content: str) -> str:
    if not content:
        return ""
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return cleaned


def refine_with_llm(raw_text: str, data: dict) -> dict:
    if not raw_text or not isinstance(data, dict):
        return data
    if OpenAI is None:
        return data

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    snippet = _relevant_ocr_snippet(raw_text)
    prompt = (
        "You are correcting financial fields from OCR invoice extraction.\n"
        "GST values like 5%, 12%, 18%, 28% are TAX RATES, not tax amounts.\n"
        "Calculate tax amount using: tax = taxable * rate / 100.\n"
        "If CGST + SGST is present, split tax equally unless OCR explicitly gives each rate.\n"
        "If IGST is present, compute IGST directly from taxable amount and IGST rate.\n"
        "Always enforce: Final Amount = Taxable Amount + CGST Amount + SGST Amount + IGST Amount.\n"
        "Extract GSTIN carefully and preserve invoice identity fields.\n"
        "Do not hallucinate unsupported values.\n"
        "Return ONLY valid JSON with exactly these keys:\n"
        "{"
        "\"Invoice Number\":\"\","
        "\"Vendor Name\":\"\","
        "\"GSTIN\":\"\","
        "\"Taxable Amount\":0.0,"
        "\"CGST Amount\":0.0,"
        "\"SGST Amount\":0.0,"
        "\"IGST Amount\":0.0,"
        "\"Final Amount\":0.0"
        "}."
    )

    user_payload = {
        "existing_data": {
            "Invoice Number": data.get("Invoice Number"),
            "Vendor Name": data.get("Vendor Name"),
            "GSTIN": data.get("GSTIN") or data.get("GST Number"),
            "Taxable Amount": data.get("Taxable Amount"),
            "CGST Amount": data.get("CGST Amount"),
            "SGST Amount": data.get("SGST Amount"),
            "IGST Amount": data.get("IGST Amount"),
            "Final Amount": data.get("Final Amount"),
        },
        "ocr_relevant_lines": snippet,
    }

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            timeout=5,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        )
    except Exception:
        return data

    try:
        content = response.choices[0].message.content or ""
        parsed = json.loads(_clean_json_block(content))
        if not isinstance(parsed, dict):
            return data
        return parsed
    except Exception:
        return data
