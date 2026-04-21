"""Minimal OCR placeholder utilities for package import stability."""


def extract_text(file_bytes: bytes) -> str:
    """Return decoded text from bytes when possible (best-effort helper)."""
    return (file_bytes or b"").decode("utf-8", errors="ignore")
