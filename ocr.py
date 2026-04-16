from io import BytesIO
from typing import Union
import os
import shutil
import subprocess
import tempfile
import zipfile
from xml.etree import ElementTree as ET

from pypdf import PdfReader


PdfInput = Union[str, bytes]

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
_WORD_EXTENSIONS = {".docx", ".doc"}


class PDFExtractionError(Exception):
    """Raised when PDF parsing fails before OCR fallback can recover."""


class OCREngineError(Exception):
    """Raised when OCR dependencies or OCR processing fails."""


def _env_int(name: str, default: int) -> int:
    """Read integer env config with a safe fallback."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _count_invoice_anchors(text: str) -> int:
    if not text:
        return 0
    upper = text.upper()
    anchors = ("INVOICE", "GST", "TOTAL", "TAX", "AMOUNT")
    return sum(1 for anchor in anchors if anchor in upper)


def _is_strong_ocr_text(text: str) -> bool:
    """
    Decide if OCR text is good enough to avoid slower OCR retries/engines.
    Env controls:
      - OCR_MIN_TEXT_LEN (default 180)
      - OCR_MIN_ANCHOR_MATCHES (default 2)
    """
    min_len = _env_int("OCR_MIN_TEXT_LEN", 180)
    min_anchor_matches = _env_int("OCR_MIN_ANCHOR_MATCHES", 2)
    return len((text or "").strip()) >= min_len and _count_invoice_anchors(text) >= min_anchor_matches


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


def _looks_like_docx_bytes(file_bytes: bytes) -> bool:
    if not file_bytes or not file_bytes.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as docx_zip:
            names = set(docx_zip.namelist())
        return "word/document.xml" in names and "[Content_Types].xml" in names
    except Exception:
        return False


def _looks_like_doc_bytes(file_bytes: bytes) -> bool:
    return bool(file_bytes and file_bytes.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"))


def _is_word_input(doc_input: PdfInput, source_name: str | None = None) -> bool:
    if isinstance(doc_input, bytes):
        return _looks_like_docx_bytes(doc_input) or _looks_like_doc_bytes(doc_input)

    candidate = source_name or doc_input
    ext = os.path.splitext(candidate)[1].lower()
    return ext in _WORD_EXTENSIONS


def _extract_text_from_docx(doc_input: PdfInput) -> str:
    if isinstance(doc_input, bytes):
        docx_stream = BytesIO(doc_input)
    else:
        docx_stream = doc_input

    with zipfile.ZipFile(docx_stream) as docx_zip:
        xml_bytes = docx_zip.read("word/document.xml")

    root = ET.fromstring(xml_bytes)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines: list[str] = []

    for paragraph in root.findall(".//w:p", namespace):
        chunks = [node.text for node in paragraph.findall(".//w:t", namespace) if node.text]
        line = "".join(chunks).strip()
        if line:
            lines.append(line)

    return "\n".join(lines).strip()


def _extract_text_from_doc(doc_input: PdfInput) -> str:
    parser = shutil.which("antiword") or shutil.which("catdoc")
    if not parser:
        raise OCREngineError(
            "Legacy .doc parsing requires antiword or catdoc. Convert DOC to DOCX or install parser."
        )

    tmp_path = None
    try:
        if isinstance(doc_input, bytes):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".doc") as tmp:
                tmp.write(doc_input)
                tmp_path = tmp.name
            doc_path = tmp_path
        else:
            doc_path = doc_input

        result = subprocess.run(
            [parser, doc_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise OCREngineError((result.stderr or "DOC parser execution failed.").strip())
        return (result.stdout or "").strip()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


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

    # OCR rendering DPI controls (faster defaults than 300).
    dpi = _env_int("OCR_DPI", 220)
    fallback_dpi = _env_int("OCR_DPI_FALLBACK", 200)

    if isinstance(pdf_input, bytes):
        images = convert_from_bytes(pdf_input, dpi=dpi)
    else:
        images = convert_from_path(pdf_input, dpi=dpi)

    client = vision.ImageAnnotatorClient()

    ocr_pages = []
    for page_index, image in enumerate(images):

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

        response = client.text_detection(image=vision.Image(content=image_bytes))

        if response.error.message:
            raise RuntimeError(response.error.message)

        page_text = ""
        if response.text_annotations:
            page_text = (response.text_annotations[0].description or "").strip()

        # If OCR text is weak, retry only this page once at fallback DPI.
        if (not _is_strong_ocr_text(page_text)) and fallback_dpi != dpi:
            if isinstance(pdf_input, bytes):
                fallback_images = convert_from_bytes(
                    pdf_input,
                    dpi=fallback_dpi,
                    first_page=page_index + 1,
                    last_page=page_index + 1,
                )
            else:
                fallback_images = convert_from_path(
                    pdf_input,
                    dpi=fallback_dpi,
                    first_page=page_index + 1,
                    last_page=page_index + 1,
                )

            if fallback_images:
                fallback_buffer = BytesIO()
                fallback_images[0].save(fallback_buffer, format="PNG")
                fallback_response = client.text_detection(
                    image=vision.Image(content=fallback_buffer.getvalue())
                )
                if fallback_response.error.message:
                    raise RuntimeError(fallback_response.error.message)
                if fallback_response.text_annotations:
                    fallback_text = (fallback_response.text_annotations[0].description or "").strip()
                    if len(fallback_text) > len(page_text):
                        page_text = fallback_text

        if page_text:
            ocr_pages.append(page_text)

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

    # OCR rendering DPI controls (faster defaults than 300).
    dpi = _env_int("OCR_DPI", 220)
    fallback_dpi = _env_int("OCR_DPI_FALLBACK", 200)

    if isinstance(pdf_input, bytes):
        images = convert_from_bytes(pdf_input, dpi=dpi)
    else:
        images = convert_from_path(pdf_input, dpi=dpi)

    ocr_pages = []
    for page_index, image in enumerate(images):
        page_text = (pytesseract.image_to_string(image, config="--psm 6") or "").strip()

        # If OCR text is weak, retry only this page once at fallback DPI.
        if (not _is_strong_ocr_text(page_text)) and fallback_dpi != dpi:
            if isinstance(pdf_input, bytes):
                fallback_images = convert_from_bytes(
                    pdf_input,
                    dpi=fallback_dpi,
                    first_page=page_index + 1,
                    last_page=page_index + 1,
                )
            else:
                fallback_images = convert_from_path(
                    pdf_input,
                    dpi=fallback_dpi,
                    first_page=page_index + 1,
                    last_page=page_index + 1,
                )

            if fallback_images:
                fallback_text = (pytesseract.image_to_string(fallback_images[0], config="--psm 6") or "").strip()
                if len(fallback_text) > len(page_text):
                    page_text = fallback_text

        if page_text:
            ocr_pages.append(page_text)

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
    return _count_invoice_anchors(text) >= 2


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
    Extract text from PDF, Word, or image content.

    Supports:
      - PDF path/bytes (digital + scanned)
      - DOCX path/bytes
      - DOC path/bytes (antiword/catdoc required)
      - Image path/bytes (jpg/png/webp/tiff/bmp)
    """
    direct_text = ""
    is_image = _is_image_input(doc_input, source_name=source_name)
    is_word = _is_word_input(doc_input, source_name=source_name)

    # Byte inputs default to PDF unless a known image/word signature is detected.
    if isinstance(doc_input, bytes) and not is_image and not is_word and not _looks_like_pdf_bytes(doc_input):
        raise ValueError("Unsupported file bytes. Expected PDF, Word, or image content.")

    if is_word:
        source_ext = os.path.splitext((source_name or "") if isinstance(doc_input, bytes) else str(doc_input))[1].lower()
        if source_ext == ".doc":
            text = _extract_text_from_doc(doc_input)
        else:
            text = _extract_text_from_docx(doc_input)

        if not text:
            raise ValueError("Word document uploaded but no readable text/tables were found.")
        return text

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

    # Skip slower Tesseract OCR when Google Vision text is already strong enough.
    if not _is_strong_ocr_text(ocr_text):
        try:
            tesseract_text = _extract_text_with_ocr(doc_input)
            if len(tesseract_text) > len(ocr_text):
                ocr_text = tesseract_text
            elif ocr_text and tesseract_text:
                ocr_text = f"{ocr_text}\n{tesseract_text}".strip()
            else:
                ocr_text = tesseract_text or ocr_text
        except Exception as exc:
            if direct_text or ocr_text:
                return _merge_text_candidates(direct_text, ocr_text)

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
