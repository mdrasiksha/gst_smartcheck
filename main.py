import os
import time
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from ocr import extract_text_from_document
from extractor_wrapper import extract_with_audit
from validators import validate_invoice
from excel_writer import write_to_excel
from llm_refiner import refine_with_llm
from cache_helper import get_cached_invoice_result, set_cached_invoice_result


logger = logging.getLogger(__name__)
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_INVALID_TEXT_VALUES = {"", ".", "-", "missing", "na", "null", "n/a", "unknown", "original"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _text_quality_is_poor(text: str) -> bool:
    # Reuse OCR-quality gates so we only rerun force_ocr when text quality is genuinely weak.
    min_text_len = _env_int("OCR_MIN_TEXT_LEN", 180)
    min_anchor_matches = _env_int("OCR_MIN_ANCHOR_MATCHES", 2)
    anchors = ("INVOICE", "GST", "TOTAL", "TAX", "AMOUNT")
    upper = (text or "").upper()
    anchor_matches = sum(1 for anchor in anchors if anchor in upper)
    return len((text or "").strip()) < min_text_len or anchor_matches < min_anchor_matches


def _should_retry_force_ocr(data: dict, text: str) -> bool:
    # Backward-compatible gate with opt-out switch.
    if not _env_bool("RETRY_ON_MANUAL_REVIEW", True):
        return False
    if not data.get("Requires Manual Review"):
        return False
    if len((text or "").strip()) > _env_int("OCR_RETRY_TEXT_LEN_SKIP", 450):
        return False

    final_amount_missing = data.get("Final Amount") in (None, 0, "0", "0.0")
    text_quality_poor = _text_quality_is_poor(text)
    min_confidence_for_skip_retry = _env_float("MIN_CONFIDENCE_FOR_SKIP_RETRY", 0.75)
    confidence = data.get("Overall Confidence")
    if isinstance(confidence, (int, float)) and confidence >= min_confidence_for_skip_retry:
        return False
    confidence_below_threshold = isinstance(confidence, (int, float)) and confidence < min_confidence_for_skip_retry

    # Retry OCR only when a manual-review case has a concrete signal of weak extraction.
    return final_amount_missing or text_quality_poor or confidence_below_threshold




def _is_missing(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_valid_value(val: str) -> bool:
    if val is None:
        return False
    text = str(val).strip()
    if not text:
        return False
    if text.lower() in _INVALID_TEXT_VALUES:
        return False
    if len(re.sub(r"[^A-Za-z0-9]", "", text)) < 2:
        return False
    if not re.search(r"[A-Za-z0-9]", text):
        return False
    return True


def _is_validation_failed(status: str) -> bool:
    return status not in {"VALID", "VALID (NON-GST)", "Non GST Invoice"}


def _safe_confidence(data: dict) -> float | None:
    value = data.get("Overall Confidence")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _is_calculation_incorrect(data: dict) -> bool:
    taxable = _to_float(data.get("Taxable Amount"))
    cgst = _to_float(data.get("CGST Amount"))
    sgst = _to_float(data.get("SGST Amount"))
    igst = _to_float(data.get("IGST Amount"))
    total = _to_float(data.get("Final Amount"))

    tax_fields = (cgst, sgst, igst)
    if any(value is None for value in tax_fields):
        return True
    if total in (None, 0.0):
        return True
    if taxable is None:
        return True

    expected_total = taxable + cgst + sgst + igst
    return abs(expected_total - total) > 1.0


def _is_gst_percentage_misinterpreted(data: dict) -> bool:
    taxable = _to_float(data.get("Taxable Amount"))
    cgst = _to_float(data.get("CGST Amount"))
    sgst = _to_float(data.get("SGST Amount"))
    igst = _to_float(data.get("IGST Amount"))
    tax_values = [v for v in (cgst, sgst, igst) if v is not None]

    if taxable is None or taxable <= 500 or not tax_values:
        return False

    if any(v in {2.5, 5.0, 12.0, 18.0, 28.0} for v in tax_values):
        return True

    if any(v < 50 for v in tax_values):
        return True

    return False


def _detect_gst_rates(text: str) -> dict:
    upper = (text or "").upper()
    slab_pattern = r"(?:2\.5|5|12|18|28)"

    igst_matches = [float(v) for v in re.findall(rf"\bIGST\s*({slab_pattern})\s*%", upper)]
    cgst_matches = [float(v) for v in re.findall(rf"\bCGST\s*({slab_pattern})\s*%", upper)]
    sgst_matches = [float(v) for v in re.findall(rf"\bSGST\s*({slab_pattern})\s*%", upper)]
    generic_matches = [float(v) for v in re.findall(rf"\b({slab_pattern})\s*%", upper)]

    if igst_matches:
        return {"mode": "igst", "rate": max(igst_matches)}

    if cgst_matches and sgst_matches:
        return {"mode": "cgst_sgst", "cgst_rate": max(cgst_matches), "sgst_rate": max(sgst_matches)}

    if generic_matches:
        return {"mode": "cgst_sgst", "rate": max(generic_matches)}

    return {}


def _fix_gst_calculation(data: dict, text: str) -> dict:
    corrected = dict(data)
    corrected["_gst_fixed"] = False

    taxable = _to_float(corrected.get("Taxable Amount"))
    if taxable is None or taxable <= 0:
        return corrected

    rates = _detect_gst_rates(text)
    if not rates:
        return corrected

    should_fix = _is_gst_percentage_misinterpreted(corrected) or _is_calculation_incorrect(corrected)
    if not should_fix:
        return corrected

    cgst = 0.0
    sgst = 0.0
    igst = 0.0

    if rates.get("mode") == "igst":
        gst_rate = float(rates.get("rate") or 0.0)
        igst = round(taxable * gst_rate / 100, 2)
    else:
        cgst_rate = rates.get("cgst_rate")
        sgst_rate = rates.get("sgst_rate")
        if cgst_rate is None or sgst_rate is None:
            total_rate = float(rates.get("rate") or 0.0)
            cgst_rate = total_rate / 2
            sgst_rate = total_rate / 2
        cgst = round(taxable * float(cgst_rate) / 100, 2)
        sgst = round(taxable * float(sgst_rate) / 100, 2)

    total = round(taxable + cgst + sgst + igst, 2)
    corrected["CGST Amount"] = cgst
    corrected["SGST Amount"] = sgst
    corrected["IGST Amount"] = igst
    corrected["Final Amount"] = total
    corrected["_gst_fixed"] = True
    return corrected


def _recalculate_total(data: dict) -> dict:
    recalculated = dict(data)
    taxable = _to_float(recalculated.get("Taxable Amount"))
    cgst = _to_float(recalculated.get("CGST Amount"))
    sgst = _to_float(recalculated.get("SGST Amount"))
    igst = _to_float(recalculated.get("IGST Amount"))
    if None in (taxable, cgst, sgst, igst):
        return recalculated
    recalculated["Final Amount"] = round(taxable + cgst + sgst + igst, 2)
    return recalculated


def _should_use_llm(data: dict, status: str, text: str) -> bool:
    if _is_gst_percentage_misinterpreted(data):
        logger.info("LLM triggered due to GST percentage misinterpretation")
        return True

    if _is_calculation_incorrect(data):
        logger.info("LLM triggered due to calculation mismatch")
        return True
    if not _is_valid_value(data.get("Invoice Number")) or not _is_valid_value(data.get("Vendor Name")):
        logger.info("LLM triggered due to missing/invalid header identity fields")
        return True

    final_amount_missing = _is_missing(data.get("Final Amount"))
    return bool(
        data.get("Requires Manual Review")
        or final_amount_missing
    )


def _status_rank(status: str) -> int:
    ranking = {
        "VALID": 6,
        "VALID (NON-GST)": 5,
        "Non GST Invoice": 4,
        "FINAL AMOUNT MISSING": 3,
        "REQUIRES MANUAL REVIEW": 2,
        "INVALID DATA": 1,
    }
    return ranking.get(status, 0)


def _apply_llm_updates_safely(original_data: dict, improved: dict) -> dict:
    merged = dict(original_data)

    for key in (
        "Invoice Number",
        "Vendor Name",
        "Taxable Amount",
        "CGST Amount",
        "SGST Amount",
        "IGST Amount",
        "Final Amount",
    ):
        if key not in improved or improved.get(key) in (None, ""):
            continue
        if key in {"Invoice Number", "Vendor Name"}:
            if _is_valid_value(improved.get(key)):
                merged[key] = str(improved.get(key)).strip()
            continue
        value = _to_float(improved.get(key))
        if value is not None:
            merged[key] = value

    gstin = improved.get("GSTIN")
    if isinstance(gstin, str) and gstin.strip():
        merged["GSTIN"] = gstin.strip().upper()
        merged["GST Number"] = gstin.strip().upper()

    merged = _recalculate_total(merged)
    return merged

def _extract_data_from_document_input(document_input, source_file_name=None):
    total_start = time.time()
    ocr_start = time.time()
    text = extract_text_from_document(document_input, source_name=source_file_name)
    ocr_end = time.time()
    logger.info("perf source=%s stage=ocr duration=%.3fs", source_file_name or "unknown", ocr_end - ocr_start)

    if not text or len(text.strip()) < 50:
        raise ValueError("OCR failed or insufficient text extracted")

    extraction_start = time.time()
    data = extract_with_audit(text)
    extraction_end = time.time()
    logger.info("perf source=%s stage=extract duration=%.3fs", source_file_name or "unknown", extraction_end - extraction_start)

    if _should_retry_force_ocr(data, text):
        retry_ocr_start = time.time()
        retry_text = extract_text_from_document(document_input, force_ocr=True, source_name=source_file_name)
        retry_ocr_end = time.time()
        logger.info("perf source=%s stage=ocr_retry duration=%.3fs", source_file_name or "unknown", retry_ocr_end - retry_ocr_start)

        retry_extract_start = time.time()
        retry_data = extract_with_audit(retry_text)
        retry_extract_end = time.time()
        logger.info("perf source=%s stage=extract_retry duration=%.3fs", source_file_name or "unknown", retry_extract_end - retry_extract_start)
        if not retry_data.get("Requires Manual Review"):
            data = retry_data

    data = _fix_gst_calculation(data, text)
    if not _is_valid_value(data.get("Invoice Number")):
        data["Invoice Number"] = None
    if not _is_valid_value(data.get("Vendor Name")):
        data["Vendor Name"] = None
    status = validate_invoice(data)

    data["_llm_used"] = False
    data["_llm_improved"] = False
    data["_calc_mismatch"] = _is_calculation_incorrect(data)
    data["_llm_fix_applied"] = False

    llm_future = None
    llm_start = None
    if _should_use_llm(data, status, text):
        llm_start = time.time()
        llm_future = _LLM_EXECUTOR.submit(refine_with_llm, text, data)

    if llm_future is not None:
        improved = None
        try:
            improved = llm_future.result(timeout=5)
        except Exception:
            improved = data
        finally:
            logger.info("perf source=%s stage=llm duration=%.3fs", source_file_name or "unknown", time.time() - llm_start)

        if isinstance(improved, dict) and improved and improved is not data:
            original_data = dict(data)
            original_status = status

            candidate_data = _apply_llm_updates_safely(data, improved)
            candidate_is_consistent = not _is_calculation_incorrect(candidate_data)
            candidate_status = validate_invoice(candidate_data)

            original_score = _status_rank(original_status)
            candidate_score = _status_rank(candidate_status)
            original_conf = _safe_confidence(original_data) or 0.0
            candidate_conf = _safe_confidence(candidate_data) or 0.0

            candidate_better = (
                candidate_score > original_score
                or (candidate_score == original_score and candidate_conf >= original_conf)
            )

            data["_llm_used"] = True
            if candidate_is_consistent and candidate_better:
                data = candidate_data
                status = candidate_status
                data["_llm_improved"] = True
                data["_llm_fix_applied"] = True
            else:
                data = original_data
                status = original_status
                data["_llm_improved"] = False
                data["_llm_fix_applied"] = False

    data["_calc_mismatch"] = _is_calculation_incorrect(data)
    if not _is_valid_value(data.get("Invoice Number")):
        raise ValueError("Unable to extract invoice number")

    if source_file_name:
        data["Source File Name"] = os.path.basename(source_file_name)

    logger.info("perf source=%s stage=total duration=%.3fs", source_file_name or "unknown", time.time() - total_start)
    return data, status


def process_invoice(pdf_path, output_path, source_file_name=None):
    resolved_source_file_name = source_file_name or pdf_path
    data, status = _extract_data_from_document_input(pdf_path, source_file_name=resolved_source_file_name)
    write_to_excel(data, status, output_path, source_file_name=resolved_source_file_name)
    return data, status


def process_invoice_bytes(pdf_bytes, output_path, source_file_name=None, write_excel_file=True):
    start = time.time()
    cached = get_cached_invoice_result(pdf_bytes)
    if cached is not None:
        data, status = cached
        if source_file_name:
            data["Source File Name"] = os.path.basename(source_file_name)
        logger.info("perf source=%s stage=cache_hit duration=%.3fs", source_file_name or "unknown", time.time() - start)
    else:
        data, status = _extract_data_from_document_input(pdf_bytes, source_file_name=source_file_name)
        set_cached_invoice_result(pdf_bytes, data, status)
    if write_excel_file:
        write_to_excel(data, status, output_path, source_file_name=source_file_name)
    return data, status


def process_invoices_bulk(invoice_jobs):
    """
    Process a list of invoices in one pass.

    Args:
        invoice_jobs: iterable of dicts with keys:
            - name: display/original file name
            - pdf_path or pdf_bytes: input PDF source
            - output_path: target XLSX path

    Returns:
        List[dict]: summary rows for each invoice.
    """
    invoice_jobs = list(invoice_jobs)
    results = [None] * len(invoice_jobs)

    def _process_single_job(job):
        name = job["name"]
        output_path = job["output_path"]

        try:
            if "pdf_bytes" in job:
                data, status = process_invoice_bytes(
                    job["pdf_bytes"], output_path, source_file_name=name
                )
            else:
                data, status = process_invoice(
                    job["pdf_path"], output_path, source_file_name=name
                )

            confidence = data.get("Confidence") if isinstance(data.get("Confidence"), dict) else {}
            confidence_score = round((sum(confidence.values()) / len(confidence)) * 100, 2) if confidence else None

            return {
                "Source File Name": name,
                "Invoice No": data.get("Invoice Number"),
                "Date": data.get("Invoice Date"),
                "GSTIN": data.get("GST Number"),
                "Taxable Value": data.get("Taxable Amount"),
                "CGST": data.get("CGST Amount"),
                "SGST": data.get("SGST Amount"),
                "IGST": data.get("IGST Amount"),
                "Total": data.get("Final Amount"),
                "Validation Status": status,
                "Confidence Score": confidence_score,
                "Rules Applied": ", ".join(data.get("_rules_applied", [])),
                "Output File": output_path,
            }
        except Exception as exc:
            return {
                "Source File Name": name,
                "Invoice No": None,
                "Date": None,
                "GSTIN": None,
                "Taxable Value": None,
                "CGST": None,
                "SGST": None,
                "IGST": None,
                "Total": None,
                "Validation Status": "FAILED",
                "Confidence Score": None,
                "Rules Applied": str(exc),
                "Output File": None,
            }

    # Parallel bulk mode (configurable) with per-file isolation preserved.
    default_workers = min(8, max(1, (os.cpu_count() or 1) * 2))
    max_workers = max(1, _env_int("BULK_MAX_WORKERS", default_workers))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(_process_single_job, job): idx
            for idx, job in enumerate(invoice_jobs)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            results[idx] = future.result()

    return results
