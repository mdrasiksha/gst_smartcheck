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
    return "\n".join(selected)


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
        "You are improving OCR invoice extraction with strict rules.\n"
        "Fix OCR mistakes like O->0 and I->1 only when context clearly indicates numbers.\n"
        "Extract these fields only: Invoice Number, Invoice Date (YYYY-MM-DD), Final Amount (float), GST Amount.\n"
        "Map synonyms: Grand Total and Total Amt should be treated as Final Amount.\n"
        "Do not hallucinate. Keep any missing value as null.\n"
        "Return ONLY valid JSON with exactly these keys: "
        "Invoice Number, Invoice Date, Final Amount, GST Amount."
    )

    user_payload = {
        "existing_data": {
            "Invoice Number": data.get("Invoice Number"),
            "Invoice Date": data.get("Invoice Date"),
            "Final Amount": data.get("Final Amount"),
            "GST Amount": data.get("GST Amount"),
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
