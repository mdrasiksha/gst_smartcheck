from ocr import extract_text_from_pdf
from extractor_wrapper import extract_with_audit
from validators import validate_invoice
from excel_writer import write_to_excel


def _extract_data_from_pdf_input(pdf_input):
    text = extract_text_from_pdf(pdf_input)

    if not text or len(text.strip()) < 50:
        raise ValueError("OCR failed or insufficient text extracted")

    data = extract_with_audit(text)
    status = validate_invoice(data)
    return data, status


def process_invoice(pdf_path, output_path):
    data, status = _extract_data_from_pdf_input(pdf_path)
    write_to_excel(data, status, output_path)
    return data, status


def process_invoice_bytes(pdf_bytes, output_path):
    data, status = _extract_data_from_pdf_input(pdf_bytes)
    write_to_excel(data, status, output_path)
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
    results = []

    for job in invoice_jobs:
        name = job["name"]
        output_path = job["output_path"]

        try:
            if "pdf_bytes" in job:
                data, status = process_invoice_bytes(job["pdf_bytes"], output_path)
            else:
                data, status = process_invoice(job["pdf_path"], output_path)

            results.append(
                {
                    "Invoice": name,
                    "Status": status,
                    "Final Amount": data.get("Final Amount"),
                    "Rules Applied": ", ".join(data.get("_rules_applied", [])),
                    "Output File": output_path,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "Invoice": name,
                    "Status": "FAILED",
                    "Final Amount": None,
                    "Rules Applied": str(exc),
                    "Output File": None,
                }
            )

    return results
