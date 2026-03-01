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
