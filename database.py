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


def download_invoice_pdf(storage_path: str) -> bytes:
    client = _get_client()
    return client.storage.from_("invoices").download(storage_path)
