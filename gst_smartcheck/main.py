import logging
from typing import Optional

from .ai_extractor import extract_from_image_with_gpt, extract_with_gpt

logger = logging.getLogger(__name__)

# Simple in-memory cache for duplicate OCR content.
cache: dict[int, dict] = {}


def extract_with_audit(text: str) -> dict:
    """Low-cost fallback extractor used before GPT calls."""
    raw_text = text or ""
    return {
        "Invoice Number": "" if not raw_text else None,
        "Invoice Date": None,
        "Taxable Amount": None,
        "CGST Amount": None,
        "SGST Amount": None,
        "IGST Amount": None,
        "Final Amount": None,
    }


def _normalize_basic_result(raw_result: Optional[dict]) -> dict:
    raw_result = raw_result or {}
    return {
        "invoice_number": raw_result.get("Invoice Number"),
        "date": raw_result.get("Invoice Date"),
        "taxable_amount": raw_result.get("Taxable Amount"),
        "cgst": raw_result.get("CGST Amount"),
        "sgst": raw_result.get("SGST Amount"),
        "igst": raw_result.get("IGST Amount"),
        "final_amount": raw_result.get("Final Amount"),
    }


def basic_extraction(text: str) -> dict:
    """Low-cost OCR/regex-first extraction."""
    return _normalize_basic_result(extract_with_audit(text or ""))


def validate_result(result: dict) -> bool:
    try:
        total = float(result.get("final_amount") or 0)
        taxable = float(result.get("taxable_amount") or 0)
        cgst = float(result.get("cgst") or 0)
        sgst = float(result.get("sgst") or 0)
        igst = float(result.get("igst") or 0)

        calculated = taxable + cgst + sgst + igst

        if total == 0:
            return False

        if abs(calculated - total) > 10:
            return False

        return True
    except:
        return False


def calculate_confidence(result):

    score = 0

    if result.get("final_amount"):
        score += 40

    if result.get("invoice_number"):
        score += 20

    if validate_result(result):
        score += 40

    return score


def extract_invoice_smart(text: str, image_bytes: Optional[bytes] = None) -> dict:
    """
    Cost-optimized extraction route:
    OCR only -> GPT text -> GPT vision.
    """
    text = (text or "")
    text = text.replace("\n\n", "\n")
    text = text.replace("  ", " ")
    text = text.strip()
    text = text[:3000]
    print("CLEAN TEXT:", text[:200])
    file_hash = hash(text)

    if file_hash in cache:
        return cache[file_hash]

    ocr_result = basic_extraction(text)
    confidence = calculate_confidence(ocr_result)
    print("CONFIDENCE:", confidence)

    method_name = "OCR_ONLY"
    final_result = ocr_result

    try:
        if confidence >= 70:
            print("Using OCR result (no cost)")
            method_name = "OCR_ONLY"
            final_result = ocr_result

        elif confidence >= 40:
            print("Using GPT TEXT extraction")
            method_name = "GPT_TEXT"
            result = extract_with_gpt(text)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError("GPT extraction failed")

            if not validate_result(result):
                print("Retrying GPT with correction prompt")

                correction_prompt = f"""
Previous extraction may be incorrect.

Fix this result:
{result}

Ensure:
- final_amount is correct payable amount
- totals match tax calculation

Return corrected JSON only.
"""

                corrected = extract_with_gpt(correction_prompt)
                if isinstance(corrected, dict) and "error" not in corrected:
                    result = corrected

            print("GPT RESULT:", result)
            print("CONFIDENCE:", calculate_confidence(result))
            final_result = result

        elif image_bytes:
            print("Using GPT VISION extraction")
            method_name = "GPT_VISION"
            result = extract_from_image_with_gpt(image_bytes)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError("GPT vision extraction failed")

            if not validate_result(result):
                print("Retrying GPT with correction prompt")

                correction_prompt = f"""
Previous extraction may be incorrect.

Fix this result:
{result}

Ensure:
- final_amount is correct payable amount
- totals match tax calculation

Return corrected JSON only.
"""

                corrected = extract_with_gpt(correction_prompt)
                if isinstance(corrected, dict) and "error" not in corrected:
                    result = corrected

            print("GPT RESULT:", result)
            print("CONFIDENCE:", calculate_confidence(result))
            final_result = result

        else:
            # No image available; keep OCR result as safe fallback.
            method_name = "OCR_ONLY"
            final_result = ocr_result

    except Exception:
        logger.exception("GPT extraction failed; falling back to OCR result")
        final_result = ocr_result
        method_name = "OCR_FALLBACK"

    print("METHOD USED:", method_name)

    cache[file_hash] = final_result
    return final_result
