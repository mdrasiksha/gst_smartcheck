import os
import sqlite3
from datetime import datetime

DB_PATH = "users.db"
STORAGE_ROOT = "storage"
INVOICE_BUCKET = os.path.join(STORAGE_ROOT, "invoices")
OUTPUT_BUCKET = os.path.join(STORAGE_ROOT, "outputs")


def _connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    os.makedirs(INVOICE_BUCKET, exist_ok=True)
    os.makedirs(OUTPUT_BUCKET, exist_ok=True)

    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            usage_count INTEGER DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            invoice_no TEXT,
            invoice_date TEXT,
            total_amount REAL,
            gst_amount REAL,
            file_url TEXT,
            status TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_usage(email):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT usage_count FROM users WHERE email=?", (email,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def increment_usage(email):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (email, usage_count) VALUES (?, 0)", (email,))
    cursor.execute("UPDATE users SET usage_count = usage_count + 1 WHERE email=?", (email,))
    conn.commit()
    conn.close()


def upload_invoice_pdf(file_name: str, pdf_bytes: bytes) -> str:
    storage_path = os.path.join(INVOICE_BUCKET, file_name)
    with open(storage_path, "wb") as f:
        f.write(pdf_bytes)
    return storage_path


def upload_to_supabase(file_name: str, file_bytes: bytes, bucket: str = "invoices") -> str:
    del bucket  # kept for backwards compatibility with existing callers
    storage_path = os.path.join(OUTPUT_BUCKET, file_name)
    with open(storage_path, "wb") as f:
        f.write(file_bytes)
    return storage_path


def get_public_invoice_url(storage_path: str) -> str:
    return storage_path


def download_invoice_pdf(storage_path: str) -> bytes:
    with open(storage_path, "rb") as f:
        return f.read()


def save_invoice_metadata(email, data, file_url, status):
    conn = _connect()
    cursor = conn.cursor()
    gst_amount = (
        (data.get("CGST Amount") or 0)
        + (data.get("SGST Amount") or 0)
        + (data.get("IGST Amount") or 0)
    )

    cursor.execute(
        """
        INSERT INTO invoices (
            email, created_at, invoice_no, invoice_date, total_amount, gst_amount, file_url, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email,
            datetime.utcnow().isoformat(),
            data.get("Invoice Number"),
            data.get("Invoice Date"),
            data.get("Final Amount"),
            gst_amount,
            file_url,
            status,
        ),
    )
    conn.commit()
    conn.close()


def get_invoice_history(email, limit=10):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, created_at, invoice_no, invoice_date, total_amount, gst_amount, file_url, status
        FROM invoices
        WHERE email = ?
        ORDER BY datetime(created_at) DESC
        LIMIT ?
        """,
        (email, limit),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_invoice_by_id(invoice_id):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, invoice_no, invoice_date, total_amount, gst_amount
        FROM invoices
        WHERE id = ?
        LIMIT 1
        """,
        (invoice_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None
