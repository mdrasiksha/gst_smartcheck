import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from ocr import extract_text_from_document
from extractor_wrapper import extract_with_audit
from validators import validate_invoice
from excel_writer import write_to_excel


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

    final_amount_missing = data.get("Final Amount") in (None, 0, "0", "0.0")
    text_quality_poor = _text_quality_is_poor(text)
    min_confidence_for_skip_retry = _env_float("MIN_CONFIDENCE_FOR_SKIP_RETRY", 0.72)
    confidence = data.get("Overall Confidence")
    confidence_below_threshold = isinstance(confidence, (int, float)) and confidence < min_confidence_for_skip_retry

    # Retry OCR only when a manual-review case has a concrete signal of weak extraction.
    return final_amount_missing or text_quality_poor or confidence_below_threshold


def _extract_data_from_pdf_input(pdf_input, source_file_name=None):
    text = extract_text_from_document(pdf_input, source_name=source_file_name)

    if not text or len(text.strip()) < 50:
        raise ValueError("OCR failed or insufficient text extracted")

    data = extract_with_audit(text)

    if _should_retry_force_ocr(data, text):
        retry_text = extract_text_from_document(pdf_input, force_ocr=True, source_name=source_file_name)
        retry_data = extract_with_audit(retry_text)
        if not retry_data.get("Requires Manual Review"):
            data = retry_data

    status = validate_invoice(data)

    if source_file_name:
        data["Source File Name"] = os.path.basename(source_file_name)

    return data, status


def process_invoice(pdf_path, output_path, source_file_name=None):
    resolved_source_file_name = source_file_name or pdf_path
    data, status = _extract_data_from_pdf_input(pdf_path, source_file_name=resolved_source_file_name)
    write_to_excel(data, status, output_path, source_file_name=resolved_source_file_name)
    return data, status


def process_invoice_bytes(pdf_bytes, output_path, source_file_name=None):
    data, status = _extract_data_from_pdf_input(pdf_bytes, source_file_name=source_file_name)
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
    max_workers = max(1, _env_int("BULK_MAX_WORKERS", 4))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(_process_single_job, job): idx
            for idx, job in enumerate(invoice_jobs)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            results[idx] = future.result()

    return results
