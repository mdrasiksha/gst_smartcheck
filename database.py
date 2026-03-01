import os
from supabase import Client, create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://kdzqkfkpqcuzmggtaziv.supabase.co")
SUPABASE_KEY = os.getenv(
    "SUPABASE_KEY",
    "sb_publishable_5s9hIo9opkRzLWW0mxbzBw_NMjw7APH",
)


def _get_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def init_db():
    """
    Supabase is schema-managed; this verifies connectivity.
    The `profiles` table should include: email (text primary key), usage_count (int).
    """
    client = _get_client()
    client.table("profiles").select("email").limit(1).execute()


def get_usage(email):
    client = _get_client()
    response = (
        client.table("profiles")
        .select("usage_count")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    rows = response.data or []
    if rows:
        return int(rows[0].get("usage_count", 0) or 0)
    return 0


def increment_usage(email):
    client = _get_client()
    current = get_usage(email)

    payload = {"email": email, "usage_count": current + 1}
    client.table("profiles").upsert(payload, on_conflict="email").execute()


def upload_invoice_pdf(file_name: str, pdf_bytes: bytes) -> str:
    client = _get_client()
    storage_path = f"uploads/{file_name}"
    client.storage.from_("invoices").upload(
        storage_path,
        pdf_bytes,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )
    return storage_path


def get_public_invoice_url(storage_path: str) -> str:
    client = _get_client()
    response = client.storage.from_("invoices").get_public_url(storage_path)
    if isinstance(response, dict):
        return response.get("publicUrl") or response.get("publicURL") or ""
    return str(response)


def download_invoice_pdf(storage_path: str) -> bytes:
    client = _get_client()
    return client.storage.from_("invoices").download(storage_path)


def save_invoice_metadata(email, data, file_url, status):
    """Stores extraction output metadata for dashboard history."""
    client = _get_client()
    client.table("invoices").insert(
        {
            "email": email,
            "invoice_no": data.get("Invoice Number"),
            "invoice_date": data.get("Invoice Date"),
            "total_amount": data.get("Final Amount"),
            "gst_amount": (
                (data.get("CGST Amount") or 0)
                + (data.get("SGST Amount") or 0)
                + (data.get("IGST Amount") or 0)
            ),
            "file_url": file_url,
            "status": status,
        }
    ).execute()


def get_invoice_history(email, limit=10):
    """Retrieves recent invoice metadata rows for one user."""
    client = _get_client()
    response = (
        client.table("invoices")
        .select("id,created_at,invoice_no,invoice_date,total_amount,gst_amount,file_url,status")
        .eq("email", email)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


def get_invoice_by_id(invoice_id):
    client = _get_client()
    response = (
        client.table("invoices")
        .select("id,invoice_no,invoice_date,total_amount,gst_amount")
        .eq("id", invoice_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None
