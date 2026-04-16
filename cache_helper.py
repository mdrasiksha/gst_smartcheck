import copy
import hashlib
import threading


_CACHE_LOCK = threading.Lock()
_INVOICE_RESULT_CACHE: dict[str, tuple[dict, str]] = {}
_MAX_CACHE_ITEMS = 256


def _hash_pdf_bytes(pdf_bytes: bytes) -> str:
    return hashlib.md5(pdf_bytes).hexdigest()


def get_cached_invoice_result(pdf_bytes: bytes):
    if not pdf_bytes:
        return None
    key = _hash_pdf_bytes(pdf_bytes)
    with _CACHE_LOCK:
        value = _INVOICE_RESULT_CACHE.get(key)
        if value is None:
            return None
        data, status = value
        return copy.deepcopy(data), status


def set_cached_invoice_result(pdf_bytes: bytes, data: dict, status: str):
    if not pdf_bytes or not isinstance(data, dict):
        return
    key = _hash_pdf_bytes(pdf_bytes)
    with _CACHE_LOCK:
        if len(_INVOICE_RESULT_CACHE) >= _MAX_CACHE_ITEMS:
            oldest_key = next(iter(_INVOICE_RESULT_CACHE), None)
            if oldest_key is not None:
                _INVOICE_RESULT_CACHE.pop(oldest_key, None)
        _INVOICE_RESULT_CACHE[key] = (copy.deepcopy(data), status)
