from fastapi import FastAPI, UploadFile, File, Form, Query, Request, HTTPException, BackgroundTasks, Depends, Header
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import io
import os
import re
import time
import uuid
import json
import hmac
import hashlib
import base64
import zipfile
import logging
import shutil
import subprocess
import tempfile
from collections import defaultdict, deque

from pypdf.errors import PdfReadError

from batch_excel_writer import write_batch_summary
from excel_writer import write_to_excel
from database import (
    init_db,
    create_or_get_user,
    get_user_by_email,
    get_user_by_id,
    upgrade_user_to_pro,
    upload_invoice_pdf,
    download_invoice_pdf,
    get_public_invoice_url,
    save_invoice_metadata,
    get_invoice_history,
    get_invoice_by_id,
)
from main import process_invoice_bytes, process_invoices_bulk
from ocr import OCREngineError, PDFExtractionError
from tally_writer import build_tally_voucher_xml
from gst_smartcheck.user_store import users, plans

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
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SIMPLE_SUPPORTED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png"}
SIMPLE_SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

ALLOWED_INPUT_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/tiff",
    "image/bmp",
    "application/zip",
    "application/x-zip-compressed",
}
EXTENSION_TO_MIME_PREFIX = {
    ".pdf": ("application/pdf",),
    ".doc": ("application/msword", "application/octet-stream"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    ),
    ".png": ("image/png",),
    ".jpg": ("image/jpeg",),
    ".jpeg": ("image/jpeg",),
    ".webp": ("image/webp",),
    ".tif": ("image/tiff",),
    ".tiff": ("image/tiff",),
    ".bmp": ("image/bmp",),
    ".zip": ("application/zip", "application/x-zip-compressed", "application/octet-stream"),
}
ALLOWED_INPUT_EXTENSIONS = set(EXTENSION_TO_MIME_PREFIX.keys())
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOADS_PER_MINUTE_PER_TYPE = 30
REQUEST_WINDOW_SECONDS = 60
JWT_EXPIRY_DAYS = 7
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
RATE_WINDOW = defaultdict(deque)
logger = logging.getLogger("invoice_upload")


class LoginRequest(BaseModel):
    email: str


class UpgradeRequest(BaseModel):
    email: str


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("utf-8"))


def create_jwt(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def decode_jwt(token: str) -> dict:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid token format.") from exc
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    provided_sig = _b64url_decode(signature_b64)
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(status_code=401, detail="Invalid token signature.")
    payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="Token has expired.")
    return payload


def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token.")
    token = auth_header.split(" ", 1)[1].strip()
    payload = decode_jwt(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload.")
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found for token.")
    return user


def get_api_key(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API key")
    return x_api_key


def check_limit(user):
    plan = user["plan"]
    limit = plans[plan]["limit"]

    if user["usage"] >= limit:
        return False
    return True


def is_supported_invoice_filename(filename: str) -> bool:
    _, ext = os.path.splitext((filename or "").lower())
    return ext in ALLOWED_INPUT_EXTENSIONS


def get_file_extension(filename: str) -> str:
    return os.path.splitext((filename or "").lower())[1]


def is_supported_mime_for_extension(content_type: str, extension: str) -> bool:
    if not content_type:
        return False
    allowed_mimes = EXTENSION_TO_MIME_PREFIX.get(extension.lower(), ())
    return content_type.lower() in allowed_mimes


def detect_supported_upload_type(filename: str, content_type: str) -> str | None:
    extension = get_file_extension(filename)
    normalized_mime = (content_type or "").lower().strip()

    if normalized_mime in SIMPLE_SUPPORTED_MIME_TYPES:
        return "pdf" if normalized_mime == "application/pdf" else "image"

    if extension in SIMPLE_SUPPORTED_EXTENSIONS:
        return "pdf" if extension == ".pdf" else "image"

    return None


def _is_likely_pdf(file_bytes: bytes) -> bool:
    return file_bytes.startswith(b"%PDF-")


def _extract_pdf_members_from_zip(zip_bytes: bytes, source_name: str) -> list[tuple[str, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zip_ref:
            extracted_pdfs: list[tuple[str, bytes]] = []
            for member in zip_ref.infolist():
                if member.is_dir():
                    continue
                member_name = member.filename or ""
                if not member_name.lower().endswith(".pdf"):
                    continue
                member_bytes = zip_ref.read(member)
                if not _is_likely_pdf(member_bytes):
                    continue
                extracted_pdfs.append((os.path.basename(member_name), member_bytes))
            return extracted_pdfs
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ZIP file: {source_name}") from exc


def enforce_upload_rate_limit(email: str, extension: str):
    now = time.time()
    key = (email.lower().strip(), extension.lower().strip())
    bucket = RATE_WINDOW[key]
    while bucket and now - bucket[0] > REQUEST_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= MAX_UPLOADS_PER_MINUTE_PER_TYPE:
        raise HTTPException(
            status_code=429,
            detail=f"Too many {extension} uploads. Please retry in a minute.",
        )
    bucket.append(now)


def ensure_not_infected(file_bytes: bytes):
    eicar_marker = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    if eicar_marker in file_bytes:
        raise HTTPException(status_code=400, detail="Upload blocked by virus scan.")

    scanner = shutil.which("clamscan")
    if not scanner:
        return

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [scanner, "--no-summary", tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode == 1:
            raise HTTPException(status_code=400, detail="Upload blocked by virus scan.")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def build_storage_input_name(unique_id: str, original_filename: str) -> str:
    _, ext = os.path.splitext((original_filename or "").lower())
    if ext not in ALLOWED_INPUT_EXTENSIONS:
        ext = ".pdf"
    return f"{unique_id}{ext}"


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
            "error": "Unable to process this file. Please upload a supported file with readable invoice text.",
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


@app.post("/auth/login")
async def auth_login(payload: LoginRequest):
    normalized_email = _normalize_email(payload.email)
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="Valid email is required.")

    user = create_or_get_user(normalized_email)
    exp = int(time.time()) + (JWT_EXPIRY_DAYS * 24 * 60 * 60)
    token = create_jwt({"sub": user["id"], "email": user["email"], "exp": exp})

    return {
        "token": token,
        "email": user["email"],
        "usage_count": int(user["usage_count"]),
        "max_limit": int(user["max_limit"]),
        "is_pro": bool(user["is_pro"]),
    }


@app.post("/upload")
async def upload_invoice(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    output_format: str = Form("xlsx"),
    x_api_key: str = Depends(get_api_key),
):
    print("Public upload endpoint hit")
    normalized_output_format = (output_format or "xlsx").strip().lower()
    email = "public-upload"
    user = users.get(x_api_key)

    if not user:
        logger.warning("upload_auth_failed invalid_api_key=%s", x_api_key)
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not check_limit(user):
        logger.info(
            "upload_limit_exceeded api_key=%s plan=%s usage=%d file=%s",
            x_api_key,
            user["plan"],
            user["usage"],
            os.path.basename(file.filename or "unknown"),
        )
        return JSONResponse(
            status_code=403,
            content={"message": "Free limit reached. Upgrade coming soon."},
        )

    original_filename = os.path.basename(file.filename or "")
    extension = get_file_extension(original_filename)
    mime_type = (file.content_type or "").lower().strip()

    detected_file_type = detect_supported_upload_type(original_filename, mime_type)
    if not detected_file_type:
        return JSONResponse(
            status_code=400,
            content={"error": "Unsupported file type. Upload PDF, JPG, JPEG, or PNG."},
        )

    enforce_upload_rate_limit(email, extension)

    file_bytes = await file.read()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=400,
            content={"error": f"File too large. Maximum supported size is {MAX_UPLOAD_BYTES // (1024 * 1024)}MB."},
        )
    ensure_not_infected(file_bytes)
    logger.info(
        "invoice_upload api_key=%s type=%s mime=%s email=%s size=%d file=%s usage=%d",
        x_api_key,
        extension,
        mime_type,
        email,
        len(file_bytes),
        original_filename,
        user["usage"],
    )

    unique_id = str(uuid.uuid4())
    storage_file_name = build_storage_input_name(unique_id, original_filename)
    excel_file_name = ensure_xlsx_filename(f"{unique_id}.xlsx")
    excel_output_path = os.path.join(OUTPUT_FOLDER, excel_file_name)
    xml_file_name = f"{unique_id}.xml"

    try:
        # 1) Receive bytes -> 2) Extract
        storage_path = upload_invoice_pdf(storage_file_name, file_bytes)
        stored_invoice_bytes = download_invoice_pdf(storage_path)

        # 3) Write Excel into outputs so it remains downloadable until cleanup
        data, status = process_invoice_bytes(
            stored_invoice_bytes,
            excel_output_path,
            source_file_name=original_filename,
            write_excel_file=False,
        )
        background_tasks.add_task(
            write_to_excel,
            data,
            status,
            excel_output_path,
            source_file_name=original_filename,
        )

        # 4) Upload XLSX output to Supabase only when requested.
        output_file_url = None
        if normalized_output_format != "xml":
            output_file_url = f"/downloads/{excel_file_name}"

        # keep source invoice url for history traceability
        invoice_pdf_url = get_public_invoice_url(storage_path)
        save_invoice_metadata(email, data, invoice_pdf_url, status)

        gst_total = (
            (data.get("CGST Amount") or 0)
            + (data.get("SGST Amount") or 0)
            + (data.get("IGST Amount") or 0)
        )
        user["usage"] += 1
        remaining = max(0, plans[user["plan"]]["limit"] - user["usage"])

        dynamic_message = "You have free uploads available."
        if remaining <= 3:
            dynamic_message = "You are nearing your free limit 🚀"
        if remaining == 0:
            dynamic_message = "Free limit reached. Upgrade coming soon."

        logger.info(
            "upload_usage_incremented api_key=%s plan=%s usage=%d remaining=%d file=%s",
            x_api_key,
            user["plan"],
            user["usage"],
            remaining,
            original_filename,
        )

        if normalized_output_format == "xml":
            xml_payload = build_tally_voucher_xml(data)
            return Response(
                content=xml_payload,
                media_type="application/xml",
                headers={"Content-Disposition": f'attachment; filename="{xml_file_name}"'},
            )

        result = {
            "can_download_xml": True,
            "is_pro": user["plan"] == "pro",
            "file_url": output_file_url,
            "detected_file_type": data.get("Detected File Type", detected_file_type),
            "extracted_data": data,
            "confidence_score": data.get("confidence_score"),
            "data_summary": {
                "invoice_no": data.get("Invoice Number"),
                "date": data.get("Invoice Date"),
                "total": data.get("Final Amount"),
                "gst": gst_total,
                "validation": data.get("Validation"),
                "requires_manual_review": data.get("Requires Manual Review", False),
                "status": status,
            },
            "confidence": {
                "overall": data.get("Overall Confidence"),
                "fields": data.get("Field Confidence", {}),
            },
        }

        return {
            "data": result,
            "usage": user["usage"],
            "remaining": remaining,
            "plan": user["plan"],
            "message": dynamic_message,
        }
    finally:
        # 5) Retain generated files for download; cleanup removes files older than 24h
        cleanup_old_files()


@app.post("/upload-bulk")
async def upload_bulk_invoices(
    files: list[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user),
):
    try:
        email = current_user["email"]
        usage = int(current_user["usage_count"])
        max_limit = int(current_user["max_limit"])
        run_id = str(uuid.uuid4())
        expanded_invoice_jobs: list[dict] = []

        for index, upload_file in enumerate(files):
            if not is_supported_invoice_filename(upload_file.filename):
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unsupported file type: {upload_file.filename}"},
                )

            safe_name = os.path.basename(upload_file.filename)
            extension = get_file_extension(safe_name)
            mime_type = (upload_file.content_type or "").lower().strip()
            if extension == ".docm":
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unsupported macro-enabled file: {safe_name}"},
                )
            if mime_type not in ALLOWED_INPUT_MIME_TYPES or not is_supported_mime_for_extension(mime_type, extension):
                return JSONResponse(
                    status_code=400,
                    content={"error": f"File type mismatch: {safe_name} ({mime_type or 'unknown'})"},
                )

            enforce_upload_rate_limit(email, extension)

            file_bytes = await upload_file.read()
            if len(file_bytes) > MAX_UPLOAD_BYTES:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"{safe_name} exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit."},
                )
            ensure_not_infected(file_bytes)

            if extension == ".zip":
                extracted_pdfs = _extract_pdf_members_from_zip(file_bytes, safe_name)
                if not extracted_pdfs:
                    return JSONResponse(
                        status_code=400,
                        content={"error": f"ZIP contains no readable PDFs: {safe_name}"},
                    )
                for member_index, (member_name, member_bytes) in enumerate(extracted_pdfs):
                    expanded_invoice_jobs.append(
                        {
                            "source_name": member_name,
                            "source_bytes": member_bytes,
                            "storage_extension": ".pdf",
                            "job_index": f"{index}_{member_index}",
                        }
                    )
                logger.info(
                    "invoice_upload_bulk_zip type=%s mime=%s email=%s size=%d extracted=%d",
                    extension,
                    mime_type,
                    email,
                    len(file_bytes),
                    len(extracted_pdfs),
                )
                continue

            expanded_invoice_jobs.append(
                {
                    "source_name": safe_name,
                    "source_bytes": file_bytes,
                    "storage_extension": extension,
                    "job_index": str(index),
                }
            )
            logger.info("invoice_upload_bulk type=%s mime=%s email=%s size=%d", extension, mime_type, email, len(file_bytes))

        expanded_job_count = len(expanded_invoice_jobs)
        if usage + expanded_job_count > max_limit:
            remaining = max(0, max_limit - usage)
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Limit reached",
                    "upgrade_required": True,
                    "remaining": remaining,
                    "requested": expanded_job_count,
                },
            )

        invoice_jobs = []
        for item in expanded_invoice_jobs:
            file_id = f"{run_id}_{item['job_index']}"
            output_path = os.path.join(OUTPUT_FOLDER, ensure_xlsx_filename(f"{file_id}.xlsx"))
            source_name = item["source_name"]
            storage_extension = item["storage_extension"]
            storage_source_name = f"invoice{storage_extension}"
            storage_name = build_storage_input_name(file_id, storage_source_name)
            storage_path = upload_invoice_pdf(storage_name, item["source_bytes"])
            stored_invoice_bytes = download_invoice_pdf(storage_path)
            invoice_jobs.append(
                {
                    "name": source_name,
                    "pdf_bytes": stored_invoice_bytes,
                    "output_path": output_path,
                }
            )

        results = process_invoices_bulk(invoice_jobs)

        updated_user = current_user
        for _ in range(expanded_job_count):
            updated_user = increment_usage_for_user(updated_user["id"])

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

        headers = {"X-Remaining": str(max(0, int(updated_user["max_limit"]) - int(updated_user["usage_count"])))}
        return FileResponse(
            path=zip_path,
            filename="bulk_results.zip",
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
    return FileResponse(
        path=file_path,
        filename=download_filename,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{download_filename}"'},
    )


@app.post("/collect-email")
async def collect_email(email: str = Form(...)):
    normalized_email = _normalize_email(email)
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="Valid email is required.")

    file_path = "emails.txt"

    try:
        if not os.path.exists(file_path):
            with open(file_path, "w", encoding="utf-8"):
                pass

        with open(file_path, "r", encoding="utf-8") as email_file:
            existing_emails = {line.strip().lower() for line in email_file if line.strip()}

        if normalized_email in existing_emails:
            return {"message": "Email already registered"}

        with open(file_path, "a", encoding="utf-8") as email_file:
            email_file.write(f"{normalized_email}\n")
    except OSError as exc:
        logger.warning("email_file_write_failed email=%s error=%s", normalized_email, exc)
        raise HTTPException(status_code=500, detail="Unable to save email right now.") from exc

    print(f"New email collected: {normalized_email}")
    logger.info("collect_email email=%s", normalized_email)
    return {"message": "Thanks! We’ll notify you 🚀"}


@app.get("/test")
def test():
    return {"status": "CORS version running"}


@app.get("/history")
async def fetch_history(limit: int = Query(10, ge=1, le=25), current_user: dict = Depends(get_current_user)):
    email = current_user["email"]
    history = get_invoice_history(email, limit=limit)
    usage_count = int(current_user["usage_count"])
    return {
        "history": history,
        "usage_count": usage_count,
        "can_download_xml": usage_count <= int(current_user["max_limit"]),
    }


@app.get("/export/tally")
async def export_tally(invoice_id: str = Query(...), current_user: dict = Depends(get_current_user)):
    del current_user
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


@app.post("/upgrade")
async def upgrade_plan(payload: UpgradeRequest, current_user: dict = Depends(get_current_user)):
    requested_email = _normalize_email(payload.email)
    if requested_email != current_user["email"]:
        raise HTTPException(status_code=403, detail="You can only upgrade your own account.")
    upgraded = upgrade_user_to_pro(requested_email)
    return {
        "email": upgraded["email"],
        "is_pro": bool(upgraded["is_pro"]),
        "usage_count": int(upgraded["usage_count"]),
        "max_limit": int(upgraded["max_limit"]),
    }


@app.get("/usage")
async def usage(current_user: dict = Depends(get_current_user)):
    refreshed_user = get_user_by_email(current_user["email"])
    return {
        "used": int(refreshed_user["usage_count"]),
        "limit": int(refreshed_user["max_limit"]),
        "is_pro": bool(refreshed_user["is_pro"]),
    }


@app.post("/create-checkout-session")
async def create_checkout_session(current_user: dict = Depends(get_current_user)):
    return {
        "checkout_url": f"https://payments.example.com/checkout?email={current_user['email']}",
        "plan": "pro_monthly_inr_299",
        "amount_inr": 299,
    }
