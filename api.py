from fastapi import FastAPI, UploadFile, File, Form, Query, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import os
import re
import time
import uuid
import zipfile

from pypdf.errors import PdfReadError

from batch_excel_writer import write_batch_summary
from access_manager import (
    get_free_upload_count,
    increment_free_upload_count,
    is_pro_user,
)
from database import (
    init_db,
    get_usage,
    increment_usage,
    upload_invoice_pdf,
    download_invoice_pdf,
    get_public_invoice_url,
    save_invoice_metadata,
    get_invoice_history,
    get_invoice_by_id,
    upload_to_supabase,
)
from main import process_invoice_bytes, process_invoices_bulk
from ocr import OCREngineError, PDFExtractionError
from tally_writer import build_tally_voucher_xml

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

OUTPUT_FOLDER = "outputs"
MAX_FREE = 10
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def ensure_xlsx_filename(filename: str) -> str:
    base, ext = os.path.splitext(filename or "")
    if ext.lower() != ".xlsx":
        return f"{base or 'invoice'}.xlsx"
    return filename


def sanitize_download_filename(filename: str, default_stem: str = "invoice") -> str:
    safe_name = os.path.basename(filename or "")
    stem, ext = os.path.splitext(safe_name)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or default_stem
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    return f"{stem}{ext}"



def cleanup_old_files():
    now = time.time()
    for file in os.listdir(OUTPUT_FOLDER):
        path = os.path.join(OUTPUT_FOLDER, file)
        if os.path.isfile(path) and now - os.path.getmtime(path) > 86400:
            os.remove(path)


@app.exception_handler(PDFExtractionError)
@app.exception_handler(PdfReadError)
@app.exception_handler(OCREngineError)
async def extraction_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "error": "Unable to process this PDF. Please upload a clear PDF with readable invoice text.",
            "details": str(exc),
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Unexpected server error while processing invoice.",
            "details": str(exc),
        },
    )


@app.post("/upload")
async def upload_invoice(
    email: str = Form(...),
    file: UploadFile = File(...),
    output_format: str = Form("xlsx"),
):
    normalized_output_format = (output_format or "xlsx").strip().lower()
    pro_user = is_pro_user(email)

    usage = 0 if pro_user else get_free_upload_count(email)

    if not pro_user and usage >= MAX_FREE:
        return JSONResponse(
            status_code=403,
            content={"error": "Free limit reached. Join the Waitlist for Pro access.", "remaining": 0},
        )

    if normalized_output_format == "xml" and not pro_user:
        return JSONResponse(
            status_code=403,
            content={"error": "XML requires Pro"},
        )

    pdf_bytes = await file.read()
    unique_id = str(uuid.uuid4())
    storage_file_name = f"{unique_id}.pdf"
    excel_file_name = ensure_xlsx_filename(f"{unique_id}.xlsx")
    excel_output_path = os.path.join(OUTPUT_FOLDER, excel_file_name)
    xml_file_name = f"{unique_id}.xml"

    try:
        # 1) Receive bytes -> 2) Extract
        storage_path = upload_invoice_pdf(storage_file_name, pdf_bytes)
        stored_pdf_bytes = download_invoice_pdf(storage_path)

        # 3) Write Excel into outputs so it remains downloadable until cleanup
        data, status = process_invoice_bytes(stored_pdf_bytes, excel_output_path)

        # 4) Upload XLSX output to Supabase only when requested.
        time.sleep(0.5)
        output_file_url = None
        if normalized_output_format != "xml":
            with open(excel_output_path, "rb") as excel_file:
                output_file_url = upload_to_supabase(excel_file_name, excel_file.read())

        # keep source invoice url for history traceability
        invoice_pdf_url = get_public_invoice_url(storage_path)
        save_invoice_metadata(email, data, invoice_pdf_url, status)

        if pro_user:
            increment_usage(email)
            usage_count = usage
            remaining = None
        else:
            usage_count = increment_free_upload_count(email)
            remaining = max(0, MAX_FREE - usage_count)

        gst_total = (
            (data.get("CGST Amount") or 0)
            + (data.get("SGST Amount") or 0)
            + (data.get("IGST Amount") or 0)
        )

        if normalized_output_format == "xml":
            xml_payload = build_tally_voucher_xml(data)
            return Response(
                content=xml_payload,
                media_type="application/xml",
                headers={"Content-Disposition": f'attachment; filename="{xml_file_name}"'},
            )

        return {
            "success": True,
            "remaining": remaining,
            "usage_count": usage_count,
            "can_download_xml": pro_user,
            "is_pro": pro_user,
            "file_url": output_file_url,
            "data_summary": {
                "invoice_no": data.get("Invoice Number"),
                "date": data.get("Invoice Date"),
                "total": data.get("Final Amount"),
                "gst": gst_total,
                "validation": data.get("Validation"),
                "requires_manual_review": data.get("Requires Manual Review", False),
                "status": status,
            },
        }
    finally:
        # 5) Retain generated files for download; cleanup removes files older than 24h
        cleanup_old_files()


@app.post("/upload-bulk")
async def upload_bulk_invoices(
    email: str = Form(...),
    files: list[UploadFile] = File(...),
):
    try:
        usage = get_usage(email)
        batch_size = len(files)

        if usage + batch_size > MAX_FREE:
            remaining = max(0, MAX_FREE - usage)
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Free limit exceeded for bulk upload. Please subscribe.",
                    "remaining": remaining,
                    "requested": batch_size,
                },
            )

        invoice_jobs = []
        run_id = str(uuid.uuid4())

        for index, upload_file in enumerate(files):
            if not upload_file.filename.lower().endswith(".pdf"):
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unsupported file type: {upload_file.filename}"},
                )

            file_id = f"{run_id}_{index}"
            safe_name = os.path.basename(upload_file.filename)
            output_path = os.path.join(OUTPUT_FOLDER, ensure_xlsx_filename(f"{file_id}.xlsx"))

            pdf_bytes = await upload_file.read()
            storage_path = upload_invoice_pdf(f"{file_id}.pdf", pdf_bytes)
            stored_pdf_bytes = download_invoice_pdf(storage_path)

            invoice_jobs.append(
                {
                    "name": safe_name,
                    "pdf_bytes": stored_pdf_bytes,
                    "output_path": output_path,
                }
            )

        results = process_invoices_bulk(invoice_jobs)

        for _ in files:
            increment_usage(email)

        summary_path = os.path.join(OUTPUT_FOLDER, ensure_xlsx_filename(f"{run_id}_batch_summary.xlsx"))
        write_batch_summary(results, summary_path)

        zip_path = os.path.join(OUTPUT_FOLDER, f"{run_id}_bulk_results.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(summary_path, arcname="batch_summary.xlsx")
            for row in results:
                output_file = row.get("Output File")
                if output_file and os.path.exists(output_file):
                    base = os.path.splitext(row.get("Source File Name") or "invoice")[0]
                    zf.write(output_file, arcname=f"reports/{ensure_xlsx_filename(base)}")

        cleanup_old_files()

        headers = {"X-Remaining": str(MAX_FREE - get_usage(email))}
        return FileResponse(
            path=zip_path,
            filename="bulk_invoice_results.zip",
            media_type="application/zip",
            headers=headers,
        )

    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/downloads/{filename}")
async def download_excel(filename: str):
    safe_name = os.path.basename(filename)
    safe_name = ensure_xlsx_filename(safe_name)
    file_path = os.path.join(OUTPUT_FOLDER, safe_name)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    download_filename = sanitize_download_filename(safe_name, default_stem="invoice")
    time.sleep(0.5)
    return FileResponse(
        path=file_path,
        filename=download_filename,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{download_filename}"'},
    )


@app.get("/test")
def test():
    return {"status": "CORS version running"}


@app.get("/history")
async def fetch_history(email: str = Query(...), limit: int = Query(10, ge=1, le=25)):
    history = get_invoice_history(email, limit=limit)
    usage_count = get_usage(email)
    return {
        "history": history,
        "usage_count": usage_count,
        "can_download_xml": usage_count <= MAX_FREE,
    }


@app.get("/export/tally")
async def export_tally(invoice_id: str = Query(...)):
    row = get_invoice_by_id(invoice_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Invoice not found"})

    xml_data = {
        "Date": row.get("invoice_date") or "",
        "GSTIN": "",
        "Total": row.get("total_amount") or 0,
        "Tax": row.get("gst_amount") or 0,
    }

    xml_path = os.path.join(TALLY_FOLDER, f"tally_{invoice_id}.xml")
    generate_tally_sales_xml(xml_data, xml_path)

    xml_filename = sanitize_download_filename(
        f"tally_{row.get('invoice_no') or invoice_id}.xml",
        default_stem=f"tally_{invoice_id}",
    )
    return FileResponse(
        path=xml_path,
        filename=xml_filename,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{xml_filename}"'},
    )


cleanup_old_files()
