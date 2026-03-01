from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import shutil
import os
import uuid

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

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


@app.post("/upload")
async def upload_invoice(
    email: str = Form(...),
    file: UploadFile = File(...)
):
    try:
        # -------------------------
        # STEP 1: Check free usage
        # -------------------------
        usage = get_usage(email)

        if usage >= 10:
            return JSONResponse(
                status_code=403,
                content={"error": "Free limit reached. Please subscribe."}
            )

        # Increment usage count
        increment_usage(email)

        # -------------------------
        # STEP 2: Save uploaded PDF
        # -------------------------
        unique_id = str(uuid.uuid4())
        pdf_path = os.path.join(UPLOAD_FOLDER, f"{unique_id}.pdf")
        output_path = os.path.join(OUTPUT_FOLDER, f"{unique_id}.xlsx")

        with open(pdf_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # -------------------------
        # STEP 3: Run OCR logic
        # -------------------------
        data, status = process_invoice(pdf_path, output_path)

        # -------------------------
        # STEP 4: Return Excel file
        # -------------------------
        return FileResponse(
            path=output_path,
            filename="invoice_output.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e)}
        )

@app.get("/test")
def test():
    return {"status": "CORS version running"}