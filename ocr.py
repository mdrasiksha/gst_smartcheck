from io import BytesIO
from typing import Union
import os

from pypdf import PdfReader


PdfInput = Union[str, bytes]

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}


class PDFExtractionError(Exception):
    """Raised when PDF parsing fails before OCR fallback can recover."""


class OCREngineError(Exception):
    """Raised when OCR dependencies or OCR processing fails."""


def _looks_like_pdf_bytes(file_bytes: bytes) -> bool:
    return bool(file_bytes and file_bytes[:5] == b"%PDF-")


def _looks_like_image_bytes(file_bytes: bytes) -> bool:
    if not file_bytes:
        return False

    # Common file signatures.
    signatures = (
        file_bytes.startswith(b"\x89PNG\r\n\x1a\n"),
        file_bytes.startswith(b"\xff\xd8\xff"),  # JPEG
        file_bytes.startswith(b"RIFF") and b"WEBP" in file_bytes[:16],
        file_bytes.startswith((b"II*\x00", b"MM\x00*")),  # TIFF
        file_bytes.startswith(b"BM"),  # BMP
    )
    return any(signatures)


def _is_image_input(doc_input: PdfInput, source_name: str | None = None) -> bool:
    if isinstance(doc_input, bytes):
        return _looks_like_image_bytes(doc_input)

    candidate = source_name or doc_input
    ext = os.path.splitext(candidate)[1].lower()
    return ext in _IMAGE_EXTENSIONS


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


def _extract_text_with_google_vision_pdf(pdf_input: PdfInput) -> str:
    """
    OCR PDF pages using Google Vision API.

    Requires runtime dependencies:
      - google-cloud-vision
      - pdf2image
      - poppler binaries available in PATH
      - Google credentials configured for Vision API access
    """
    from google.cloud import vision
    from pdf2image import convert_from_bytes, convert_from_path

    if isinstance(pdf_input, bytes):
        images = convert_from_bytes(pdf_input, dpi=300)
    else:
        images = convert_from_path(pdf_input, dpi=300)

    client = vision.ImageAnnotatorClient()

    ocr_pages = []
    for image in images:
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

        response = client.text_detection(image=vision.Image(content=image_bytes))

        if response.error.message:
            raise RuntimeError(response.error.message)

        if response.text_annotations:
            text = response.text_annotations[0].description or ""
            if text.strip():
                ocr_pages.append(text)

    return "\n".join(ocr_pages).strip()


def _extract_text_with_google_vision_image(image_input: PdfInput) -> str:
    """OCR image using Google Vision API."""
    from google.cloud import vision

    if isinstance(image_input, bytes):
        image_bytes = image_input
    else:
        with open(image_input, "rb") as image_file:
            image_bytes = image_file.read()

    client = vision.ImageAnnotatorClient()
    response = client.text_detection(image=vision.Image(content=image_bytes))

    if response.error.message:
        raise RuntimeError(response.error.message)

    if response.text_annotations:
        return (response.text_annotations[0].description or "").strip()

    return ""


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
        text = pytesseract.image_to_string(image, config="--psm 6")
        if text and text.strip():
            ocr_pages.append(text)

    return "\n".join(ocr_pages).strip()


def _extract_text_with_ocr_image(image_input: PdfInput) -> str:
    """OCR a non-PDF image input with Tesseract."""
    from PIL import Image
    import pytesseract

    if isinstance(image_input, bytes):
        image = Image.open(BytesIO(image_input))
    else:
        image = Image.open(image_input)

    text = pytesseract.image_to_string(image, config="--psm 6")
    return (text or "").strip()


def _contains_invoice_anchors(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    anchors = ("INVOICE", "GST", "TOTAL", "TAX", "AMOUNT")
    return sum(1 for anchor in anchors if anchor in upper) >= 2


def _merge_text_candidates(direct_text: str, ocr_text: str) -> str:
    """
    Keep direct text when strong, otherwise blend direct and OCR output.
    """
    if not direct_text:
        return ocr_text
    if not ocr_text:
        return direct_text

    # If direct extraction looks healthy and longer, trust it.
    if len(direct_text) >= 300 and _contains_invoice_anchors(direct_text):
        return direct_text

    # Hybrid path for mixed quality documents.
    if len(ocr_text) > len(direct_text):
        return f"{direct_text}\n{ocr_text}".strip()

    return f"{ocr_text}\n{direct_text}".strip()


def extract_text_from_pdf(pdf_input: PdfInput, force_ocr: bool = False) -> str:
    """Backward-compatible PDF-only extraction entrypoint."""
    return extract_text_from_document(pdf_input, force_ocr=force_ocr)


def extract_text_from_document(
    doc_input: PdfInput,
    force_ocr: bool = False,
    source_name: str | None = None,
) -> str:
    """
    Extract text from either PDF or image content.

    Supports:
      - PDF path/bytes (digital + scanned)
      - Image path/bytes (jpg/png/webp/tiff/bmp)
    """
    direct_text = ""
    is_image = _is_image_input(doc_input, source_name=source_name)

    # Byte inputs default to PDF unless image signature is detected.
    if isinstance(doc_input, bytes) and not is_image and not _looks_like_pdf_bytes(doc_input):
        raise ValueError("Unsupported file bytes. Expected PDF or image content.")

    if is_image:
        vision_failed = False
        try:
            vision_text = _extract_text_with_google_vision_image(doc_input)
            if vision_text:
                return vision_text
        except Exception:
            vision_failed = True

        try:
            ocr_text = _extract_text_with_ocr_image(doc_input)
        except Exception as exc:
            error_msg = "Unable to OCR image invoice. Verify OCR dependencies and image quality."
            if vision_failed:
                error_msg = (
                    "Google Vision OCR failed and Tesseract OCR fallback failed for image input. "
                    "Install/verify Tesseract and verify Google Vision API configuration."
                )
            raise OCREngineError(error_msg) from exc

        if not ocr_text:
            raise ValueError("No extractable text found. This image appears empty or unreadable.")

        return ocr_text

    if not force_ocr:
        try:
            direct_text = _extract_text_with_pypdf(doc_input)
        except Exception as exc:
            raise PDFExtractionError("Unable to parse PDF text content.") from exc

        if len(direct_text) >= 250 and _contains_invoice_anchors(direct_text):
            return direct_text

    vision_failed = False
    ocr_text = ""
    try:
        vision_text = _extract_text_with_google_vision_pdf(doc_input)
        if vision_text:
            ocr_text = vision_text
    except Exception:
        vision_failed = True

    if not ocr_text:
        try:
            ocr_text = _extract_text_with_ocr(doc_input)
        except Exception as exc:
            if direct_text:
                return direct_text

            error_msg = (
                "No extractable text found and OCR fallback failed. "
                "Install/verify Tesseract and Poppler to process scanned PDFs."
            )
            if vision_failed:
                error_msg = (
                    "Google Vision OCR failed and Tesseract OCR fallback failed. "
                    "Install/verify Tesseract and Poppler, and verify Google Vision API configuration."
                )

            raise OCREngineError(error_msg) from exc

    merged_text = _merge_text_candidates(direct_text, ocr_text)
    if not merged_text:
        raise ValueError("No extractable text found. This document appears scanned or empty.")

    return merged_text
