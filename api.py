from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import shutil
import os
import uuid
import time

from main import process_invoice
from database import init_db, get_usage, increment_usage

app = FastAPI()

# Enable CORS (important for frontend connection)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # allow all origins
    allow_credentials=False,  # MUST be False when using "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database
init_db()

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"

def cleanup_old_files():
    now = time.time()
    for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
        for file in os.listdir(folder):
            path = os.path.join(folder, file)
            if os.path.isfile(path):
                if now - os.path.getmtime(path) > 86400:  # 24 hours
                    os.remove(path)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


MAX_FREE = 10

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
                content={
                    "error": "Free limit reached. Please subscribe.",
                    "remaining": 0
                }
            )

        increment_usage(email)
        remaining = MAX_FREE - get_usage(email)

        unique_id = str(uuid.uuid4())
        pdf_path = os.path.join(UPLOAD_FOLDER, f"{unique_id}.pdf")
        output_path = os.path.join(OUTPUT_FOLDER, f"{unique_id}.xlsx")

        with open(pdf_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        data, status = process_invoice(pdf_path, output_path)

        cleanup_old_files()

        headers = {"X-Remaining": str(remaining)}

        return FileResponse(
            path=output_path,
            filename="invoice_output.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=headers
        )

    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e)}
        )

@app.get("/test")
def test():
    return {"status": "CORS version running"}


def cleanup_old_files():
    now = time.time()
    for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
        for file in os.listdir(folder):
            path = os.path.join(folder, file)
            if os.path.isfile(path):
                if now - os.path.getmtime(path) > 86400:
                    os.remove(path)

cleanup_old_files()