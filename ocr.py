from pypdf import PdfReader


def extract_text_from_pdf(pdf_path):
    reader = PdfReader(pdf_path)
    full_text = ""

    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            full_text += page_text + "\n"

    if not full_text.strip():
        raise ValueError(
            "No extractable text found. This PDF appears scanned."
        )

    return full_text
