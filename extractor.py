import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?!\d)")
_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")

TOTAL_KEYWORDS = ["grand total", "total amount", "net total", "invoice total", "amount payable", "total"]
HEADER_INVOICE_KEYWORDS = ["invoice no", "invoice number", "invoice #", "inv no", "bill no", "quotation", "proforma invoice"]


@dataclass
class Candidate:
    label: str
    value: float
    x: float
    y: float
    score: int


def _to_float(token: str) -> float | None:
    try:
        return float(token.replace(",", "").strip())
    except Exception:
        return None


def _extract_amounts(text: str) -> list[float]:
    values: list[float] = []
    for match in _AMOUNT_RE.findall(text or ""):
        value = _to_float(match)
        if value is not None:
            values.append(round(value, 2))
    return values


def _find_invoice_number(lines: list[dict[str, Any]]) -> str | None:
    top_lines = [l for l in lines if l.get("y", 9999) <= 0.35]
    for line in top_lines:
        low = line["text"].lower()
        if any(key in low for key in HEADER_INVOICE_KEYWORDS):
            parts = re.split(r":|#|-", line["text"], maxsplit=1)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
    return None


def _find_date(lines: list[dict[str, Any]]) -> str | None:
    top_lines = [l for l in lines if l.get("y", 9999) <= 0.45]
    for line in top_lines:
        match = _DATE_RE.search(line["text"])
        if match:
            return match.group(0)
    return None


def _build_lines(ocr_payload: dict[str, Any]) -> list[dict[str, Any]]:
    words = ocr_payload.get("words") or []
    if not words:
        return []

    max_x = max((w.get("x", 0) + w.get("width", 0)) for w in words) or 1
    max_y = max((w.get("y", 0) + w.get("height", 0)) for w in words) or 1

    rows: dict[int, list[dict[str, Any]]] = {}
    for word in sorted(words, key=lambda w: (w.get("y", 0), w.get("x", 0))):
        y = int(word.get("y", 0) / 12)
        rows.setdefault(y, []).append(word)

    lines: list[dict[str, Any]] = []
    for row_words in rows.values():
        ordered = sorted(row_words, key=lambda w: w.get("x", 0))
        text = " ".join((w.get("text") or "").strip() for w in ordered).strip()
        if not text:
            continue
        x = min(w.get("x", 0) for w in ordered) / max_x
        y = min(w.get("y", 0) for w in ordered) / max_y
        lines.append({"text": text, "x": x, "y": y})
    return sorted(lines, key=lambda row: (row["y"], row["x"]))


def _total_candidates(lines: list[dict[str, Any]]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for line in lines:
        lower = line["text"].lower()
        amounts = _extract_amounts(line["text"])
        if not amounts:
            continue

        keyword_points = 0
        for key in TOTAL_KEYWORDS:
            if key in lower:
                keyword_points = max(keyword_points, 40 if key == "grand total" else 28)

        position_points = 20 if (line["y"] > 0.70 and line["x"] > 0.60) else (12 if line["y"] > 0.65 else 0)
        format_points = 20
        score = keyword_points + position_points + format_points

        candidates.append(Candidate(label=line["text"], value=max(amounts), x=line["x"], y=line["y"], score=score))

    return sorted(candidates, key=lambda c: (c.score, c.y, c.value), reverse=True)


def _extract_tax(lines: list[dict[str, Any]], label: str) -> float:
    values: list[float] = []
    for line in lines:
        if label in line["text"].lower():
            values.extend(_extract_amounts(line["text"]))
    return round(sum(values), 2) if values else 0.0


def extract_invoice_structured(ocr_payload: dict[str, Any]) -> dict[str, Any]:
    lines = _build_lines(ocr_payload)
    detected_totals = _total_candidates(lines)

    invoice_number = _find_invoice_number(lines)
    invoice_date = _find_date(lines)

    taxable_candidates = [l for l in lines if "taxable" in l["text"].lower() or "subtotal" in l["text"].lower()]
    taxable_values = [v for c in taxable_candidates for v in _extract_amounts(c["text"])]
    taxable_amount = max(taxable_values) if taxable_values else None

    cgst = _extract_tax(lines, "cgst")
    sgst = _extract_tax(lines, "sgst")
    igst = _extract_tax(lines, "igst")

    final_amount = detected_totals[0].value if detected_totals else None
    gst_total = round((taxable_amount or 0.0) + cgst + sgst + igst, 2)
    if taxable_amount is not None and gst_total > 0 and final_amount is None:
        final_amount = gst_total

    gst_match_bonus = 0
    if final_amount is not None and taxable_amount is not None and abs(final_amount - gst_total) <= 2.0:
        gst_match_bonus = 20

    confidence = 0
    if detected_totals:
        confidence += min(40, detected_totals[0].score)
    if final_amount is not None:
        confidence += 20
    if detected_totals and detected_totals[0].y > 0.70 and detected_totals[0].x > 0.60:
        confidence += 20
    confidence += gst_match_bonus
    confidence = min(confidence, 100)

    logger.info("OCR text: %s", " | ".join(line["text"] for line in lines[:20]))
    logger.info("Detected totals: %s", [f"{round(c.value,2)}@({round(c.x,2)},{round(c.y,2)}) score={c.score}" for c in detected_totals[:5]])
    logger.info("Confidence score: %s", confidence)

    return {
        "invoice_number": invoice_number,
        "date": invoice_date,
        "taxable_amount": taxable_amount,
        "final_amount": final_amount,
        "gst_breakdown": {"cgst": cgst, "sgst": sgst, "igst": igst},
        "detected_totals": [{"text": c.label, "value": c.value, "x": c.x, "y": c.y, "score": c.score} for c in detected_totals],
        "confidence_score": confidence,
    }
