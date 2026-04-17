import re
from typing import Any

KEYWORDS = {
    "invoice_number": ("invoice no", "invoice number", "invoice #", "inv no", "invoice"),
    "date": ("invoice date", "date"),
    "gstin": ("gstin", "gst no", "gst number"),
    "taxable_amount": ("taxable", "subtotal", "sub total", "taxable amount"),
    "cgst": ("cgst",),
    "sgst": ("sgst",),
    "igst": ("igst",),
    "total": ("grand total", "invoice total", "total"),
}

NUMERIC_PATTERN = re.compile(r"[-+]?\d[\d,]*(?:\.\d{1,2})?")
DATE_PATTERN = re.compile(r"\b\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4}\b")
GSTIN_PATTERN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z2][A-Z0-9]\b", re.IGNORECASE)


def _clean_word(word: str) -> str:
    return re.sub(r"\s+", " ", (word or "")).strip()


def _word_center(word: dict[str, Any]) -> tuple[float, float]:
    return (word.get("x", 0) + (word.get("width", 0) / 2.0), word.get("y", 0) + (word.get("height", 0) / 2.0))


def _distance(anchor: dict[str, Any], candidate: dict[str, Any]) -> float:
    ax, ay = _word_center(anchor)
    cx, cy = _word_center(candidate)
    return abs(ax - cx) + abs(ay - cy)


def _match_field_value(field: str, candidate: str) -> bool:
    value = candidate.strip()
    if not value:
        return False
    if field in {"taxable_amount", "cgst", "sgst", "igst", "total"}:
        return bool(NUMERIC_PATTERN.search(value))
    if field == "date":
        return bool(DATE_PATTERN.search(value))
    if field == "gstin":
        return bool(GSTIN_PATTERN.search(value))
    return bool(re.search(r"[A-Za-z0-9]", value))


def _extract_nearest_value(anchor: dict[str, Any], words: list[dict[str, Any]], field: str) -> str:
    x, y = anchor.get("x", 0), anchor.get("y", 0)
    w, h = anchor.get("width", 0), anchor.get("height", 0)

    right_candidates = [
        word for word in words
        if word is not anchor
        and word.get("x", 0) >= x + w - 2
        and abs(word.get("y", 0) - y) <= max(24, h * 2)
        and _match_field_value(field, word.get("text", ""))
    ]
    below_candidates = [
        word for word in words
        if word is not anchor
        and word.get("y", 0) >= y + h - 2
        and abs(word.get("x", 0) - x) <= max(140, w * 5)
        and _match_field_value(field, word.get("text", ""))
    ]

    candidates = right_candidates + below_candidates
    if not candidates:
        return ""

    winner = min(candidates, key=lambda cand: _distance(anchor, cand))
    return _clean_word(winner.get("text", ""))


def _cluster_rows(words: list[dict[str, Any]], y_gap: int = 10) -> list[list[dict[str, Any]]]:
    sorted_words = sorted(words, key=lambda w: (w.get("y", 0), w.get("x", 0)))
    rows: list[list[dict[str, Any]]] = []

    for word in sorted_words:
        if not rows:
            rows.append([word])
            continue

        prev_row = rows[-1]
        prev_y = sum(w.get("y", 0) for w in prev_row) / max(1, len(prev_row))
        if abs(word.get("y", 0) - prev_y) <= y_gap:
            prev_row.append(word)
        else:
            rows.append([word])

    return rows


def _extract_line_items(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _cluster_rows(words)
    line_items: list[dict[str, Any]] = []

    for row in rows:
        row_text = " ".join(_clean_word(w.get("text", "")) for w in sorted(row, key=lambda it: it.get("x", 0))).strip()
        if not row_text:
            continue
        numeric_hits = NUMERIC_PATTERN.findall(row_text)
        if len(numeric_hits) >= 2 and re.search(r"[A-Za-z]", row_text):
            line_items.append({
                "row_text": row_text,
                "amount_candidates": numeric_hits,
            })

    return line_items[:30]


def extract_layout_fields(ocr_data) -> dict:
    words = [w for w in (ocr_data or {}).get("words", []) if _clean_word(w.get("text", ""))]

    extracted = {
        "invoice_number": "",
        "date": "",
        "gstin": "",
        "taxable_amount": "",
        "cgst": "",
        "sgst": "",
        "igst": "",
        "total": "",
        "line_items": [],
    }

    if not words:
        return extracted

    lowered_words = [(word, _clean_word(word.get("text", "")).lower()) for word in words]

    for field, aliases in KEYWORDS.items():
        anchors = [word for word, token in lowered_words if any(alias in token for alias in aliases)]
        for anchor in anchors:
            value = _extract_nearest_value(anchor, words, field)
            if value:
                extracted[field] = value
                break

    if not extracted["gstin"]:
        for word in words:
            match = GSTIN_PATTERN.search(_clean_word(word.get("text", "")))
            if match:
                extracted["gstin"] = match.group(0).upper()
                break

    extracted["line_items"] = _extract_line_items(words)
    return extracted
