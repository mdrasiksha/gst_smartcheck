from ocr import extract_text_from_pdf
from extractor_wrapper import extract_with_audit
from validators import validate_invoice
from excel_writer import write_to_excel


def process_invoice(pdf_path, output_path):
    text = extract_text_from_pdf(pdf_path)

    if not text or len(text.strip()) < 50:
        raise ValueError("OCR failed or insufficient text extracted")

    data = extract_with_audit(text)

    status = validate_invoice(data)

    write_to_excel(data, status, output_path)

    return data, status


def process_invoices_bulk(invoice_jobs):
    """
    Process a list of invoices in one pass.

    Args:
        invoice_jobs: iterable of dicts with keys:
            - name: display/original file name
            - pdf_path: input PDF path
            - output_path: target XLSX path

    Returns:
        List[dict]: summary rows for each invoice.
    """
    results = []

    for job in invoice_jobs:
        name = job["name"]
        pdf_path = job["pdf_path"]
        output_path = job["output_path"]

        try:
            data, status = process_invoice(pdf_path, output_path)
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
