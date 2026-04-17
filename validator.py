import datetime as dt
import re
from typing import Any


DATE_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%Y-%m-%d",
    "%d.%m.%Y",
)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.-]", "", value.replace(",", "").strip())
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _is_valid_date(date_text: str | None) -> bool:
    if not date_text or not isinstance(date_text, str):
        return False
    value = date_text.strip()
    if not value:
        return False
    for fmt in DATE_FORMATS:
        try:
            dt.datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def validate_invoice(data):
    errors: list[str] = []

    gstin = (data.get("GSTIN") or data.get("GST Number") or "").strip().upper()
    if gstin and len(gstin) != 15:
        errors.append("GSTIN length must be exactly 15 characters")

    invoice_date = data.get("Invoice Date") or data.get("date")
    if invoice_date and not _is_valid_date(str(invoice_date)):
        errors.append("Invoice date is not in a valid format")

    taxable = _to_float(data.get("Taxable Amount") or data.get("taxable_amount"))
    cgst = _to_float(data.get("CGST Amount") or data.get("cgst")) or 0.0
    sgst = _to_float(data.get("SGST Amount") or data.get("sgst")) or 0.0
    igst = _to_float(data.get("IGST Amount") or data.get("igst")) or 0.0
    total = _to_float(data.get("Final Amount") or data.get("total"))

    if taxable is not None and total is not None:
        expected_total = round(taxable + cgst + sgst + igst, 2)
        if abs(expected_total - total) > 2.0:
            errors.append(f"Total mismatch: expected ~{expected_total}, found {round(total, 2)}")

    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
    }
