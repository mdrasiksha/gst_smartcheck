import json
import os
import re
from typing import Dict
from urllib import error, request

GSTIN_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def validate_gstin_checksum(gstin: str) -> bool:
    """Validate a 15-character Indian GSTIN using the official Mod 36 checksum."""
    if not isinstance(gstin, str):
        return False

    candidate = gstin.strip().upper()
    if len(candidate) != 15 or any(ch not in GSTIN_CHARSET for ch in candidate):
        return False

    factor = 1
    total = 0

    for char in candidate[:14]:
        code_point = GSTIN_CHARSET.index(char)
        addend = factor * code_point
        factor = 2 if factor == 1 else 1
        addend = (addend // 36) + (addend % 36)
        total += addend

    remainder = total % 36
    check_code_point = (36 - remainder) % 36
    return candidate[-1] == GSTIN_CHARSET[check_code_point]



def normalize_text(text: str) -> str:
    text = text.upper()
    text = re.sub(r"(₹|INR|RS\.?)", "", text)
    text = text.replace(",", "")
    text = text.replace("\r", "\n")
    text = re.sub(r"[^\x00-\x7F]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()

def is_address_number(num_str, text):
    """
    Blocks PIN codes, area codes, and address numbers.
    """
    # Common Indian PIN code pattern
    if re.fullmatch(r"\d{6}", num_str):
        return True

    # If number is near address keywords, block it
    address_keywords = ["PIN", "CODE", "HARYANA", "KARNATAKA", "TAMIL", "NADU", "DELHI", "MUMBAI", "BENGALURU", "BANGALORE"]
    for word in address_keywords:
        if word in text:
            return True

    return False

def is_hsn_code(num_str, line_text):
    """
    Blocks HSN / SAC codes from being treated as money
    """
    if "HSN" in line_text or "SAC" in line_text:
        return True
    return False

def is_non_invoice_identifier(value: str) -> bool:
    blacklist_keywords = ["UDYAM", "MSME", "LUT", "ARN"]
    return any(k in value for k in blacklist_keywords)


applied_rules = []


def _is_close(left: float, right: float, tolerance: float = 1.5) -> bool:
    return abs((left or 0.0) - (right or 0.0)) <= tolerance


_NUMBER_WORDS = {
    "ZERO": 0,
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
    "SIX": 6,
    "SEVEN": 7,
    "EIGHT": 8,
    "NINE": 9,
    "TEN": 10,
    "ELEVEN": 11,
    "TWELVE": 12,
    "THIRTEEN": 13,
    "FOURTEEN": 14,
    "FIFTEEN": 15,
    "SIXTEEN": 16,
    "SEVENTEEN": 17,
    "EIGHTEEN": 18,
    "NINETEEN": 19,
    "TWENTY": 20,
    "THIRTY": 30,
    "FORTY": 40,
    "FIFTY": 50,
    "SIXTY": 60,
    "SEVENTY": 70,
    "EIGHTY": 80,
    "NINETY": 90,
}


def _words_to_number(words: str) -> float | None:
    if not words:
        return None

    normalized = re.sub(r"[^A-Z\s-]", " ", words.upper()).replace("-", " ")
    tokens = [tok for tok in normalized.split() if tok not in {"RUPEES", "RUPEE", "ONLY", "AND", "PAISE", "PAISA"}]
    if not tokens:
        return None

    total = 0
    current = 0
    parsed_any = False

    for token in tokens:
        if token in _NUMBER_WORDS:
            current += _NUMBER_WORDS[token]
            parsed_any = True
        elif token == "HUNDRED":
            current = (current or 1) * 100
            parsed_any = True
        elif token == "THOUSAND":
            total += (current or 1) * 1000
            current = 0
            parsed_any = True
        elif token == "LAKH":
            total += (current or 1) * 100000
            current = 0
            parsed_any = True
        elif token == "CRORE":
            total += (current or 1) * 10000000
            current = 0
            parsed_any = True

    if not parsed_any:
        return None

    return float(total + current)


def _extract_amount_chargeable_in_words(text: str) -> float | None:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "AMOUNT CHARGEABLE (IN WORDS)" not in line:
            continue

        candidates = []
        inline = re.search(r"AMOUNT\s+CHARGEABLE\s*\(IN\s+WORDS\)\s*[:\-]?\s*(.+)$", line)
        if inline:
            candidates.append(inline.group(1).strip())

        if i + 1 < len(lines):
            candidates.append(lines[i + 1].strip())

        for candidate in candidates:
            parsed = _words_to_number(candidate)
            if parsed is not None:
                return round(parsed, 2)
    return None


def _extract_summary_totals(text: str) -> tuple[float | None, float | None]:
    table_anchor = re.search(r"HSN/SAC\s+TAXABLE\s+VALUE[\s\S]{0,1200}?\bTOTAL\b", text)
    if not table_anchor:
        return None, None

    segment = text[table_anchor.start(): table_anchor.end() + 200]
    total_row = re.search(r"\bTOTAL\b\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)", segment)
    if not total_row:
        return None, None

    taxable = float(total_row.group(1).replace(",", ""))
    total_tax = float(total_row.group(2).replace(",", ""))
    return taxable, total_tax


def _extract_tax_summary_details(text: str) -> dict:
    lines = text.split("\n")
    header_idx = None
    for i, line in enumerate(lines):
        if (
            "HSN/SAC" in line
            and "TAXABLE VALUE" in line
            and any(k in line for k in ["IGST", "CGST", "SGST"])
        ):
            header_idx = i
            break

    if header_idx is None:
        return {}

    table_lines = lines[header_idx: min(len(lines), header_idx + 45)]
    total_taxable = None
    total_tax = None
    line_taxable_sum = 0.0
    line_tax_sum = 0.0
    saw_line_items = False

    for raw in table_lines:
        line = raw.strip()
        if not line:
            continue

        amounts = [float(v.replace(",", "")) for v in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+", line)]
        if len(amounts) < 2:
            continue

        if "TOTAL" in line:
            total_taxable = amounts[0]
            total_tax = round(sum(amounts[1:]), 2)
            continue

        row_amounts = amounts
        if row_amounts and re.fullmatch(r"\d{4,8}", str(int(row_amounts[0]))):
            row_amounts = row_amounts[1:]

        if len(row_amounts) < 2:
            continue

        saw_line_items = True
        line_taxable_sum += row_amounts[0]
        line_tax_sum += sum(row_amounts[1:])

    result = {}
    if total_taxable is not None and total_tax is not None:
        result["summary_taxable"] = round(total_taxable, 2)
        result["summary_tax"] = round(total_tax, 2)
    if saw_line_items:
        result["line_taxable_sum"] = round(line_taxable_sum, 2)
        result["line_tax_sum"] = round(line_tax_sum, 2)
    return result


def _line_total_candidates(line: str) -> list[float]:
    amounts = []
    for match in re.finditer(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+", line):
        suffix = line[match.end(): match.end() + 10].strip().upper()
        if suffix.startswith("NOS") or suffix.startswith("UNITS") or suffix.startswith("PCS") or suffix.startswith("QTY"):
            continue
        amounts.append(float(match.group().replace(",", "")))
    return amounts


def _find_larger_total_candidate(lines: list[str], minimum: float) -> float | None:
    for line in reversed(lines):
        if not re.search(r"\b(TOTAL|GRAND TOTAL|AMOUNT PAYABLE|NET PAYABLE)\b", line):
            continue
        for value in reversed(_line_total_candidates(line)):
            if value > minimum:
                return value
    return None


def _validate_tax_math(data: Dict) -> tuple[bool, float]:
    taxable = float(data.get("Taxable Amount") or 0)
    cgst = float(data.get("CGST Amount") or 0)
    sgst = float(data.get("SGST Amount") or 0)
    igst = float(data.get("IGST Amount") or 0)
    total_tax = data.get("Total Tax Amount")
    final_amount = data.get("Final Amount")

    if final_amount is None:
        return False, 0.0

    tax_value = float(total_tax) if total_tax is not None else (igst if igst > 0 else (cgst + sgst))
    expected = round(taxable + tax_value, 2)
    actual = float(final_amount)
    return abs(expected - actual) <= 0.01, expected


def _retry_with_aggressive_patterns(text: str, data: Dict) -> Dict:
    retry_data = dict(data)

    total_patterns = [
        r"(?:TOTAL\s*AMOUNT|GRAND\s*TOTAL|AMOUNT\s*PAYABLE|NET\s*PAYABLE)[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
        r"\bTOTAL\b[^\d]{0,15}(\d{3,}(?:\.\d{1,2})?)",
    ]
    taxable_patterns = [
        r"(?:TAXABLE\s*VALUE|TAXABLE\s*AMOUNT|SUB\s*TOTAL|BASIC\s*AMOUNT)[^\d]{0,20}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
    ]

    if retry_data.get("Final Amount") in (None, 0):
        for pattern in total_patterns:
            match = re.search(pattern, text)
            if match:
                retry_data["Final Amount"] = float(match.group(1).replace(",", ""))
                retry_data.setdefault("Confidence", {})["Final Amount"] = 0.75
                break

    if retry_data.get("Taxable Amount") in (None, 0):
        for pattern in taxable_patterns:
            match = re.search(pattern, text)
            if match:
                taxable_val = float(match.group(1).replace(",", ""))
                retry_data["Taxable Amount"] = taxable_val
                retry_data["Sub Total"] = taxable_val
                retry_data.setdefault("Confidence", {})["Taxable Amount"] = 0.75
                break

    return retry_data


def run_validation_engine(text: str, data: Dict) -> Dict:
    validated = dict(data)
    validated.setdefault("Confidence", {})

    for key in ("Taxable Amount", "CGST Amount", "SGST Amount", "IGST Amount", "Final Amount"):
        validated["Confidence"].setdefault(key, 0.4 if validated.get(key) is None else 0.7)

    is_valid, expected = _validate_tax_math(validated)
    if not is_valid:
        validated = _retry_with_aggressive_patterns(text, validated)
        is_valid, expected = _validate_tax_math(validated)

    validated["Validation"] = "Verified" if is_valid else "Math Mismatch"
    validated["Requires Manual Review"] = bool((validated.get("Final Amount") in (None, 0)) or not is_valid)
    validated["Math Expected Total"] = expected

    if validated["Requires Manual Review"]:
        validated["Confidence"]["Final Amount"] = min(validated["Confidence"].get("Final Amount", 0.7), 0.4)

    if validated["Confidence"]:
        validated["Overall Confidence"] = round(sum(validated["Confidence"].values()) / len(validated["Confidence"]) * 100, 2)

    return validated


def _extract_invoice_fields_regex(text: str) -> dict:
    applied_rules = []
    text = normalize_text(text)

    data = {
        "Invoice Number": None,
        "Invoice Date": None,
        "GST Number": None,
        "Taxable Amount": None,
        "Sub Total": None,
        "CGST Amount": 0.0,
        "SGST Amount": 0.0,
        "IGST Amount": 0.0,
        "Total Tax Amount": None,
        "Final Amount": None,
        "Is GST Invoice": False,
        "Confidence": {},
    }

    lines = text.split("\n")

    gst = re.search(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", text)
    if gst and validate_gstin_checksum(gst.group()):
        data["GST Number"] = gst.group()
        data["Confidence"]["GST Number"] = 0.95

    inv_patterns = [
        r"(?:INVOICE|INV)\s*(?:NO|NUMBER)?\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]*\d(?:[A-Z0-9\-/]*))",
        r"\b(\d{3,8}/\d{2}-\d{2})\b",
    ]
    for pattern in inv_patterns:
        inv = re.search(pattern, text)
        if inv:
            candidate = inv.group(1).strip(" -:/")
            if not is_non_invoice_identifier(candidate):
                data["Invoice Number"] = candidate
                data["Confidence"]["Invoice Number"] = 0.95
                applied_rules.append("INVOICE_NO_WITH_SUFFIX")
                break

    date_patterns = [
        r"\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b",
        r"\b\d{1,2}\s+[A-Z]{3,9}\s+\d{2,4}\b",
        r"\b\d{1,2}-[A-Z]{3}-\d{2,4}\b",
    ]
    for line in lines:
        for pat in date_patterns:
            m = re.search(pat, line)
            if m:
                data["Invoice Date"] = m.group()
                data["Confidence"]["Invoice Date"] = 0.95
                break
        if data["Invoice Date"]:
            break

    words_total = _extract_amount_chargeable_in_words(text)
    if words_total is not None:
        data["Amount Chargeable (in words) Parsed"] = words_total
        data["Confidence"]["Amount Chargeable (in words) Parsed"] = 0.98
        applied_rules.append("ANCHOR_TOTAL_FROM_WORDS")

    summary = _extract_tax_summary_details(text)
    if summary.get("summary_taxable") is not None and summary.get("summary_tax") is not None:
        data["Taxable Amount"] = summary["summary_taxable"]
        data["Sub Total"] = summary["summary_taxable"]
        data["Total Tax Amount"] = summary["summary_tax"]
        data["Confidence"]["Taxable Amount"] = 0.99
        data["Confidence"]["Total Tax Amount"] = 0.99
        applied_rules.append("SUMMARY_TABLE_TOTAL_ROW_PRIORITY")

    if summary.get("line_taxable_sum") is not None:
        data["Line Item Taxable Sum"] = summary["line_taxable_sum"]
    if summary.get("line_tax_sum") is not None:
        data["Line Item Tax Sum"] = summary["line_tax_sum"]

    if data["Taxable Amount"] is None:
        subtotal = re.search(
            r"(?:SUB\s*TOTAL|TAXABLE\s*VALUE|TAXABLE\s*AMOUNT|BASIC\s*AMOUNT)[^\d]{0,40}(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+)",
            text,
        )
        if subtotal:
            taxable = float(subtotal.group(1).replace(",", ""))
            data["Taxable Amount"] = taxable
            data["Sub Total"] = taxable
            data["Confidence"]["Taxable Amount"] = 0.9

    for tax in ["CGST", "SGST", "IGST"]:
        for line in lines:
            m = re.search(rf"\b{tax}\b[^\d]{{0,20}}(\d{{1,3}}(?:,\d{{3}})+(?:\.\d{{1,2}})?|\d+\.\d{{1,2}})", line)
            if m:
                data[f"{tax} Amount"] = float(m.group(1).replace(",", ""))
                data["Confidence"][f"{tax} Amount"] = 0.9
                break

    if data.get("Total Tax Amount") is None:
        tax_sum = data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]
        if tax_sum > 0:
            data["Total Tax Amount"] = round(tax_sum, 2)

    final = None
    for line in lines:
        if re.search(r"\b(GRAND TOTAL|TOTAL AMOUNT|AMOUNT PAYABLE|NET PAYABLE|TOTAL)\b", line):
            nums = _line_total_candidates(line)
            if nums:
                final = nums[-1]

    if final is None:
        for i, line in enumerate(lines):
            if "GRAND TOTAL" in line and i + 1 < len(lines):
                nums = _line_total_candidates(lines[i + 1])
                if nums:
                    final = nums[-1]
                    break

    if final is not None:
        data["Final Amount"] = round(final, 2)
        data["Confidence"]["Final Amount"] = 0.9

    if words_total is not None:
        if data.get("Final Amount") is None or not _is_close(data["Final Amount"], words_total, tolerance=0.01):
            data["Final Amount"] = words_total
            data["Confidence"]["Final Amount"] = 0.98
            applied_rules.append("FINAL_VALIDATED_BY_WORDS_ANCHOR")

    if (
        data.get("Taxable Amount") is not None
        and data.get("Final Amount") is not None
        and data["Final Amount"] < data["Taxable Amount"]
    ):
        replacement = _find_larger_total_candidate(lines, data["Taxable Amount"])
        if replacement is not None:
            data["Final Amount"] = round(replacement, 2)
            data["Confidence"]["Final Amount"] = 0.92
            applied_rules.append("REJECT_SMALL_TOTAL_AND_SEARCH_DOWN")

    if (
        summary.get("line_taxable_sum") is not None
        and summary.get("line_tax_sum") is not None
        and data.get("Final Amount") is not None
    ):
        recon_total = round(summary["line_taxable_sum"] + summary["line_tax_sum"], 2)
        data["Math Reconciliation Total"] = recon_total
        data["Math Reconciliation Passed"] = _is_close(recon_total, data["Final Amount"], tolerance=0.01)
        applied_rules.append("MATHEMATICAL_RECONCILIATION")

    data["Is GST Invoice"] = bool(
        data.get("GST Number") or data.get("CGST Amount") or data.get("SGST Amount") or data.get("IGST Amount")
    )

    data["_rules_applied"] = applied_rules
    return run_validation_engine(text, data)



def _coerce_float(value):
    if value in (None, "", "null"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        cleaned = re.sub(r"[^\d.-]", "", cleaned)
        if cleaned:
            try:
                return float(cleaned)
            except ValueError:
                return None
    return None


def _extract_json_object(payload: str) -> Dict:
    if not payload:
        return {}

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", payload, flags=re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else payload

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}

    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return {}


def _extract_with_gemini(text: str) -> Dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {}

    model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    prompt = (
        "Extract GST invoice fields from OCR text and return only a single JSON object. "
        "Use null for missing values. Follow exactly this schema: "
        "Invoice Number, Invoice Date, GST Number, Taxable Amount, CGST Amount, SGST Amount, IGST Amount, Final Amount. "
        "Important: specifically detect parts/part and labour/labor sections; sum their taxable values into Taxable Amount. "
        "Do not add commentary.\n\nOCR TEXT:\n" + text
    )

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }

    req = request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (error.URLError, TimeoutError, json.JSONDecodeError):
        return {}

    candidates = raw.get("candidates") or []
    if not candidates:
        return {}

    parts = (((candidates[0] or {}).get("content") or {}).get("parts")) or []
    content = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
    parsed = _extract_json_object(content)
    if not parsed:
        return {}

    normalized = {
        "Invoice Number": parsed.get("Invoice Number"),
        "Invoice Date": parsed.get("Invoice Date"),
        "GST Number": parsed.get("GST Number"),
        "Taxable Amount": _coerce_float(parsed.get("Taxable Amount")),
        "Sub Total": _coerce_float(parsed.get("Taxable Amount")),
        "CGST Amount": _coerce_float(parsed.get("CGST Amount")) or 0.0,
        "SGST Amount": _coerce_float(parsed.get("SGST Amount")) or 0.0,
        "IGST Amount": _coerce_float(parsed.get("IGST Amount")) or 0.0,
        "Final Amount": _coerce_float(parsed.get("Final Amount")),
        "Is GST Invoice": bool(parsed.get("GST Number")),
        "Confidence": {
            "Invoice Number": 0.85 if parsed.get("Invoice Number") else 0.4,
            "Invoice Date": 0.85 if parsed.get("Invoice Date") else 0.4,
            "GST Number": 0.9 if parsed.get("GST Number") else 0.4,
            "Taxable Amount": 0.85 if _coerce_float(parsed.get("Taxable Amount")) is not None else 0.4,
            "CGST Amount": 0.85 if _coerce_float(parsed.get("CGST Amount")) is not None else 0.4,
            "SGST Amount": 0.85 if _coerce_float(parsed.get("SGST Amount")) is not None else 0.4,
            "IGST Amount": 0.85 if _coerce_float(parsed.get("IGST Amount")) is not None else 0.4,
            "Final Amount": 0.85 if _coerce_float(parsed.get("Final Amount")) is not None else 0.4,
        },
        "_rules_applied": ["AI_GEMINI_EXTRACTION"],
    }
    return normalized


def extract_invoice_fields(text: str) -> dict:
    ai_data = _extract_with_gemini(text)
    if ai_data:
        return run_validation_engine(normalize_text(text), ai_data)
    return _extract_invoice_fields_regex(text)
