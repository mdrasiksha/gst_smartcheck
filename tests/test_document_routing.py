import io
import zipfile

import pytest

import ocr


def _docx_bytes_with_text(text: str) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")
        zf.writestr(
            "word/document.xml",
            f"""<?xml version='1.0' encoding='UTF-8'?>
<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
  </w:body>
</w:document>""",
        )
    return mem.getvalue()


def test_pdf_bytes_routes_to_pdf_parser(monkeypatch):
    monkeypatch.setattr(ocr, "_extract_text_with_pypdf", lambda _: "INVOICE GST TOTAL AMOUNT TAX")
    result = ocr.extract_text_from_document(b"%PDF-1.7 fake")
    assert "INVOICE" in result


def test_docx_bytes_extract_text():
    payload = _docx_bytes_with_text("Invoice 1001 GST Total")
    result = ocr.extract_text_from_document(payload, source_name="invoice.docx")
    assert "Invoice 1001" in result


def test_jpg_bytes_routes_to_image_ocr(monkeypatch):
    jpeg_sig = b"\xff\xd8\xff\xe0" + b"x" * 32
    monkeypatch.setattr(ocr, "_extract_text_with_google_vision_image", lambda _: "Invoice image text")
    result = ocr.extract_text_from_document(jpeg_sig, source_name="photo.jpg")
    assert result == "Invoice image text"


def test_invalid_bytes_rejected():
    with pytest.raises(ValueError, match="Unsupported file bytes"):
        ocr.extract_text_from_document(b"not-supported")


def test_pdf_parser_failure_falls_back_to_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "_extract_text_with_pypdf", lambda _: (_ for _ in ()).throw(RuntimeError("bad pdf")))
    monkeypatch.setattr(ocr, "_extract_text_with_google_vision_pdf", lambda _: "")
    monkeypatch.setattr(ocr, "_extract_text_with_ocr", lambda _: "INVOICE GST TOTAL AMOUNT")
    result = ocr.extract_text_from_document(b"%PDF-1.7 fake")
    assert "INVOICE" in result
