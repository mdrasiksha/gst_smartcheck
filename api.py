from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import time
import uuid
import zipfile

from batch_excel_writer import write_batch_summary
from database import (
    init_db,
    get_usage,
    increment_usage,
    upload_invoice_pdf,
    download_invoice_pdf,
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

os.makedirs(OUTPUT_FOLDER, exist_ok=True)


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

        increment_usage(email)
        remaining = MAX_FREE - get_usage(email)

        unique_id = str(uuid.uuid4())
        storage_file_name = f"{unique_id}.pdf"
        output_path = os.path.join(OUTPUT_FOLDER, f"{unique_id}.xlsx")

        storage_path = upload_invoice_pdf(storage_file_name, pdf_bytes)
        stored_pdf_bytes = download_invoice_pdf(storage_path)

        process_invoice_bytes(stored_pdf_bytes, output_path)

        cleanup_old_files()

        headers = {"X-Remaining": str(remaining)}

        return FileResponse(
            path=output_path,
            filename="invoice_output.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=headers,
        )

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


cleanup_old_files()
