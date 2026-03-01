from io import BytesIO
from typing import Union

from pypdf import PdfReader


PdfInput = Union[str, bytes]


class PDFExtractionError(Exception):
    """Raised when PDF parsing fails before OCR fallback can recover."""


class OCREngineError(Exception):
    """Raised when OCR dependencies or OCR processing fails."""


def _extract_text_with_pypdf(pdf_input: PdfInput) -> str:
    if isinstance(pdf_input, bytes):
        reader = PdfReader(BytesIO(pdf_input))
    else:
        reader = PdfReader(pdf_input)

    pages = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(page_text)

    return "\n".join(pages).strip()


def _extract_text_with_ocr(pdf_input: PdfInput) -> str:
    """
    OCR fallback for scanned/image-based PDFs.

    Requires runtime dependencies:
      - pytesseract
      - pdf2image
      - poppler binaries available in PATH
      - tesseract binaries available in PATH
    """
    from pdf2image import convert_from_bytes, convert_from_path
    import pytesseract

    if isinstance(pdf_input, bytes):
        images = convert_from_bytes(pdf_input, dpi=300)
    else:
        images = convert_from_path(pdf_input, dpi=300)

    ocr_pages = []
    for image in images:
        text = pytesseract.image_to_string(image)
        if text and text.strip():
            ocr_pages.append(text)

    return "\n".join(ocr_pages).strip()


def extract_text_from_pdf(pdf_input: PdfInput, force_ocr: bool = False) -> str:
    direct_text = ""

    if not force_ocr:
        try:
            direct_text = _extract_text_with_pypdf(pdf_input)
        except Exception as exc:
            raise PDFExtractionError("Unable to parse PDF text content.") from exc

        if len(direct_text) >= 100:
            return direct_text

    try:
        ocr_text = _extract_text_with_ocr(pdf_input)
    except Exception as exc:
        if direct_text:
            return direct_text
        raise OCREngineError(
            "No extractable text found and OCR fallback failed. "
            "Install/verify Tesseract and Poppler to process scanned PDFs."
        ) from exc

    if not ocr_text:
        if direct_text:
            return direct_text
        raise ValueError("No extractable text found. This PDF appears scanned or empty.")

    return ocr_text
