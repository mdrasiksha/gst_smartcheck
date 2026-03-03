import json
import os
from threading import Lock

PAID_USERS = [
    "pro@example.com",
    "finance@company.com",
]

_FREE_UPLOADS_FILE = os.path.join(os.path.dirname(__file__), "free_uploads.json")
_FREE_UPLOADS_LOCK = Lock()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_pro_user(email: str) -> bool:
    normalized = normalize_email(email)
    return normalized in {normalize_email(user_email) for user_email in PAID_USERS}


def _read_upload_counts() -> dict[str, int]:
    if not os.path.exists(_FREE_UPLOADS_FILE):
        return {}

    try:
        with open(_FREE_UPLOADS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        pass

    return {}


def _write_upload_counts(data: dict[str, int]) -> None:
    with open(_FREE_UPLOADS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def get_free_upload_count(email: str) -> int:
    normalized = normalize_email(email)
    with _FREE_UPLOADS_LOCK:
        data = _read_upload_counts()
        return int(data.get(normalized, 0))


def increment_free_upload_count(email: str) -> int:
    normalized = normalize_email(email)
    with _FREE_UPLOADS_LOCK:
        data = _read_upload_counts()
        updated_count = int(data.get(normalized, 0)) + 1
        data[normalized] = updated_count
        _write_upload_counts(data)
    return updated_count
