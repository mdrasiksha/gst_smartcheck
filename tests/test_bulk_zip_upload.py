import io
import zipfile

from fastapi.testclient import TestClient

import api


def _build_zip_with_members(members: dict[str, bytes]) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return mem.getvalue()


def test_upload_bulk_zip_expands_valid_pdf_members(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "OUTPUT_FOLDER", str(tmp_path))

    captured = {"job_count": 0, "increments": 0}

    def fake_process_invoices_bulk(invoice_jobs):
        captured["job_count"] = len(invoice_jobs)
        rows = []
        for job in invoice_jobs:
            out_path = tmp_path / f"{job['name']}.xlsx"
            out_path.write_bytes(b"xlsx")
            rows.append({"Source File Name": job["name"], "Output File": str(out_path)})
        return rows

    monkeypatch.setattr(api, "process_invoices_bulk", fake_process_invoices_bulk)
    monkeypatch.setattr(api, "get_usage", lambda email: 0)
    monkeypatch.setattr(api, "increment_usage", lambda email: captured.__setitem__("increments", captured["increments"] + 1))
    def fake_upload_invoice_pdf(file_name, pdf_bytes):
        path = tmp_path / file_name
        path.write_bytes(pdf_bytes)
        return str(path)

    monkeypatch.setattr(api, "upload_invoice_pdf", fake_upload_invoice_pdf)
    monkeypatch.setattr(api, "download_invoice_pdf", lambda storage_path: open(storage_path, "rb").read())
    monkeypatch.setattr(api, "write_batch_summary", lambda results, summary_path: open(summary_path, "wb").write(b"summary"))

    client = TestClient(api.app)
    payload = _build_zip_with_members(
        {
            "invoice1.pdf": b"%PDF-1.7 alpha",
            "folder/invoice2.pdf": b"%PDF-1.4 beta",
            "notes.txt": b"not a pdf",
            "fake.pdf": b"not really pdf",
        }
    )

    response = client.post(
        "/upload-bulk",
        data={"email": "zip@example.com"},
        files={"files": ("invoices.zip", payload, "application/zip")},
    )

    assert response.status_code == 200
    assert captured["job_count"] == 2
    assert captured["increments"] == 2


def test_upload_bulk_zip_limit_uses_expanded_pdf_count(monkeypatch):
    monkeypatch.setattr(api, "get_usage", lambda email: api.MAX_FREE - 1)

    client = TestClient(api.app)
    payload = _build_zip_with_members(
        {
            "invoice1.pdf": b"%PDF-1.7 alpha",
            "invoice2.pdf": b"%PDF-1.7 beta",
        }
    )

    response = client.post(
        "/upload-bulk",
        data={"email": "zip@example.com"},
        files={"files": ("invoices.zip", payload, "application/zip")},
    )

    assert response.status_code == 403
    assert response.json()["requested"] == 2
