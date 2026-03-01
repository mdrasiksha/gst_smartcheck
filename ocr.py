from pypdf import PdfReader


def _extract_text_with_pypdf(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    pages = []

    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(page_text)

    return "\n".join(pages).strip()


def _extract_text_with_ocr(pdf_path: str) -> str:
    """
    OCR fallback for scanned/image-based PDFs.

    Requires runtime dependencies:
      - pytesseract
      - pdf2image
      - poppler binaries available in PATH
      - tesseract binaries available in PATH
    """
    from pdf2image import convert_from_path
    import pytesseract

    images = convert_from_path(pdf_path, dpi=300)

    ocr_pages = []
    for image in images:
        text = pytesseract.image_to_string(image)
        if text and text.strip():
            ocr_pages.append(text)

    return "\n".join(ocr_pages).strip()


def extract_text_from_pdf(pdf_path: str) -> str:
    direct_text = _extract_text_with_pypdf(pdf_path)
    if len(direct_text) >= 50:
        return direct_text

    try:
        ocr_text = _extract_text_with_ocr(pdf_path)
    except Exception as exc:
        if direct_text:
            return direct_text
        raise ValueError(
            "No extractable text found and OCR fallback failed. "
            "Install/verify Tesseract and Poppler to process scanned PDFs."
        ) from exc

    if not ocr_text:
        if direct_text:
            return direct_text
        raise ValueError("No extractable text found. This PDF appears scanned or empty.")

    return ocr_text
