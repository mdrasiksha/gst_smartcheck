import logging
from typing import Optional

from extractor_wrapper import extract_with_audit
from gst_smartcheck.ai_extractor import extract_from_image_with_gpt, extract_with_gpt

logger = logging.getLogger(__name__)

# Simple in-memory cache for duplicate OCR content.
cache: dict[int, dict] = {}


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


def calculate_confidence(result: dict) -> int:
    score = 0

    if result.get("final_amount"):
        score += 40

    if result.get("invoice_number"):
        score += 20

    if result.get("date"):
        score += 10

    if result.get("taxable_amount"):
        score += 20

    if any(result.get(field) for field in ("cgst", "sgst", "igst")):
        score += 10

    return score


def extract_invoice_smart(text: str, image_bytes: Optional[bytes] = None) -> dict:
    """
    Cost-optimized extraction route:
    OCR only -> GPT text -> GPT vision.
    """
    text = (text or "")[:3000]
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
            final_result = extract_with_gpt(text)

        elif image_bytes:
            print("Using GPT VISION extraction")
            method_name = "GPT_VISION"
            final_result = extract_from_image_with_gpt(image_bytes)

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
