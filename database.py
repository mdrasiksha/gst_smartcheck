import os
import sqlite3
import uuid
from datetime import datetime

DB_PATH = "users.db"
STORAGE_ROOT = "storage"
INVOICE_BUCKET = os.path.join(STORAGE_ROOT, "invoices")
OUTPUT_BUCKET = os.path.join(STORAGE_ROOT, "outputs")
FREE_PLAN_LIMIT = 5
PRO_PLAN_LIMIT = 1000


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
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            is_pro INTEGER NOT NULL DEFAULT 0,
            usage_count INTEGER NOT NULL DEFAULT 0,
            max_limit INTEGER NOT NULL DEFAULT 5,
            created_at TEXT NOT NULL
        )
        """
    )
    cursor.execute("PRAGMA table_info(users)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "id" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN id TEXT")
    if "is_pro" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_pro INTEGER NOT NULL DEFAULT 0")
    if "max_limit" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN max_limit INTEGER NOT NULL DEFAULT 5")
    if "created_at" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
    cursor.execute("UPDATE users SET id = COALESCE(id, lower(hex(randomblob(16))))")
    cursor.execute("UPDATE users SET created_at = COALESCE(created_at, ?)", (datetime.utcnow().isoformat(),))
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
    user = get_user_by_email(email)
    return int(user["usage_count"]) if user else 0


def get_user_stats(email):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM invoices WHERE email=?", (email,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def increment_usage(email):
    user = create_or_get_user(email)
    increment_usage_for_user(user["id"])


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def create_or_get_user(email: str) -> dict:
    normalized_email = _normalize_email(email)
    user = get_user_by_email(normalized_email)
    if user:
        return user

    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    user_id = str(uuid.uuid4())
    cursor.execute(
        """
        INSERT INTO users (id, email, is_pro, usage_count, max_limit, created_at)
        VALUES (?, ?, 0, 0, ?, ?)
        """,
        (user_id, normalized_email, FREE_PLAN_LIMIT, now),
    )
    conn.commit()
    conn.close()
    return get_user_by_email(normalized_email)


def get_user_by_email(email: str) -> dict | None:
    normalized_email = _normalize_email(email)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, email, is_pro, usage_count, max_limit, created_at
        FROM users
        WHERE email = ?
        LIMIT 1
        """,
        (normalized_email,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, email, is_pro, usage_count, max_limit, created_at
        FROM users
        WHERE id = ?
        LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def increment_usage_for_user(user_id: str) -> dict:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET usage_count = usage_count + 1 WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()
    return get_user_by_id(user_id)


def upgrade_user_to_pro(email: str) -> dict:
    user = create_or_get_user(email)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE users
        SET is_pro = 1, max_limit = ?, usage_count = 0
        WHERE id = ?
        """,
        (PRO_PLAN_LIMIT, user["id"]),
    )
    conn.commit()
    conn.close()
    return get_user_by_id(user["id"])


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
        SELECT id
        FROM invoices
        WHERE email = ?
        ORDER BY datetime(created_at) ASC, id ASC
        LIMIT 10
        """,
        (email,),
    )
    first_ten_ids = {row["id"] for row in cursor.fetchall()}

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
    rows = []
    for row in cursor.fetchall():
        row_data = dict(row)
        row_data["can_download_xml"] = row_data.get("id") in first_ten_ids
        rows.append(row_data)
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
