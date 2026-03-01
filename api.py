from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import time
import uuid
import zipfile

from batch_excel_writer import write_batch_summary, generate_tally_sales_xml
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
)
from main import process_invoice_bytes, process_invoices_bulk

app = FastAPI()

# Enable CORS (important for frontend connection)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # MUST be False when using "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database
init_db()

OUTPUT_FOLDER = "outputs"
MAX_FREE = 10
TALLY_FOLDER = "exports"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(TALLY_FOLDER, exist_ok=True)


def cleanup_old_files():
    now = time.time()
    for file in os.listdir(OUTPUT_FOLDER):
        path = os.path.join(OUTPUT_FOLDER, file)
        if os.path.isfile(path) and now - os.path.getmtime(path) > 86400:
            os.remove(path)


@app.post("/upload")
async def upload_invoice(
    email: str = Form(...),
    file: UploadFile = File(...)
):
    try:
        usage = get_usage(email)

        if usage >= MAX_FREE:
            return JSONResponse(
                status_code=403,
                content={"error": "Free limit reached. Please subscribe.", "remaining": 0},
            )

        pdf_bytes = await file.read()

        unique_id = str(uuid.uuid4())
        storage_file_name = f"{unique_id}.pdf"
        output_path = os.path.join(OUTPUT_FOLDER, f"{unique_id}.xlsx")

        storage_path = upload_invoice_pdf(storage_file_name, pdf_bytes)
        file_url = get_public_invoice_url(storage_path)
        stored_pdf_bytes = download_invoice_pdf(storage_path)

        data, status = process_invoice_bytes(stored_pdf_bytes, output_path)

        save_invoice_metadata(email, data, file_url, status)
        increment_usage(email)
        remaining = MAX_FREE - get_usage(email)

        cleanup_old_files()

        return {
            "success": True,
            "remaining": remaining,
            "data": {
                "invoice_no": data.get("Invoice Number"),
                "date": data.get("Invoice Date"),
                "total": data.get("Final Amount"),
                "gst": (
                    (data.get("CGST Amount") or 0)
                    + (data.get("SGST Amount") or 0)
                    + (data.get("IGST Amount") or 0)
                ),
                "file_url": file_url,
                "status": status,
            },
        }

    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


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
            output_path = os.path.join(OUTPUT_FOLDER, f"{file_id}.xlsx")

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

        summary_path = os.path.join(OUTPUT_FOLDER, f"{run_id}_batch_summary.xlsx")
        write_batch_summary(results, summary_path)

        zip_path = os.path.join(OUTPUT_FOLDER, f"{run_id}_bulk_results.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(summary_path, arcname="batch_summary.xlsx")
            for row in results:
                output_file = row.get("Output File")
                if output_file and os.path.exists(output_file):
                    base = os.path.splitext(row["Invoice"])[0]
                    zf.write(output_file, arcname=f"reports/{base}.xlsx")

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


@app.get("/test")
def test():
    return {"status": "CORS version running"}


@app.get("/history")
async def fetch_history(email: str = Query(...), limit: int = Query(10, ge=1, le=25)):
    history = get_invoice_history(email, limit=limit)
    return {"history": history}


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

    return FileResponse(
        path=xml_path,
        filename=f"tally_{row.get('invoice_no') or invoice_id}.xml",
        media_type="application/xml",
    )


cleanup_old_files()
