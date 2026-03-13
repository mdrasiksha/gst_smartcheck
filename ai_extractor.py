import json
import os
import re
from typing import Dict
from urllib import error, request

from word2number import w2n

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




def get_amount_from_words(text):
    try:
        # Find text after 'Amount in Words:'
        match = re.search(r'Amount in Words:\s*(.*)', text, re.IGNORECASE)
        if not match: return None
        clean_str = match.group(1).split('only')[0] # Remove 'only'
        clean_str = re.sub(r'[^a-zA-Z\s-]', '', clean_str) # Keep only words
        return float(w2n.word_to_num(clean_str))
    except:
        return None


def _extract_master_total_from_words(text: str) -> float | None:
    """Use text anchors like 'Total Invoice Value (In Words)' / 'Amount in Words' as master total."""
    lines = text.split("\n")
    anchor_pattern = re.compile(r"(TOTAL\s+INVOICE\s+VALUE\s*\(IN\s+WORDS\)|AMOUNT\s+IN\s+WORDS|TOTAL\s+IN\s+WORDS)")

    for idx, line in enumerate(lines):
        if not anchor_pattern.search(line):
            continue

        candidates: list[str] = []
        inline = re.split(anchor_pattern, line, maxsplit=1)
        if len(inline) >= 3:
            trailing = inline[-1].strip(" :-")
            if trailing:
                candidates.append(trailing)

        if idx + 1 < len(lines):
            nxt = lines[idx + 1].strip()
            if nxt:
                candidates.append(nxt)

        for candidate in candidates:
            cleaned = re.sub(r"\b(INR|RUPEES?|ONLY|PAISE|PAISA|AND)\b", " ", candidate, flags=re.IGNORECASE)
            cleaned = re.sub(r"[^A-Z\s-]", " ", cleaned.upper())
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if not cleaned:
                continue
            try:
                return round(float(w2n.word_to_num(cleaned)), 2)
            except ValueError:
                continue

    return None


def _extract_round_off(lines: list[str]) -> float | None:
    for line in lines:
        if "ROUND OFF" not in line.upper():
            continue
        matches = re.findall(r"[+-]?\d+(?:,\d{3})*(?:\.\d{1,2})?", line)
        if matches:
            return round(float(matches[-1].replace(",", "")), 2)
    return None


def _sum_tax_components(lines: list[str], label: str) -> float | None:
    non_total_values: list[float] = []
    total_values: list[float] = []

    for line in lines:
        upper = line.upper()
        if label not in upper:
            continue

        values = _line_total_candidates(line)
        if not values:
            continue

        picked = round(values[-1], 2)
        if "TOTAL" in upper:
            total_values.append(picked)
        else:
            non_total_values.append(picked)

    if non_total_values:
        return round(sum(non_total_values), 2)
    if total_values:
        return round(max(total_values), 2)
    return None


def _pick_closest_to_target(values: list[float], target: float, tolerance: float = 5.0) -> float | None:
    if not values:
        return None
    winner = min(values, key=lambda v: abs(v - target))
    if abs(winner - target) <= tolerance:
        return round(winner, 2)
    return None


def _extract_tax_amount_near_label(lines: list[str], label_pattern: str) -> float | None:
    for i, line in enumerate(lines):
        if not re.search(label_pattern, line.upper()):
            continue

        window = lines[i: min(len(lines), i + 4)]
        for candidate_line in window[1:] + [window[0]]:
            nums = _line_total_candidates(candidate_line)
            if nums:
                return round(nums[-1], 2)
    return None


def _extract_tax_amount_from_tax_column(lines: list[str], label: str) -> float | None:
    for i, line in enumerate(lines):
        upper_line = line.upper()
        if label not in upper_line:
            continue

        tax_anchor = None
        if "TAX AMOUNT" in upper_line:
            tax_anchor = upper_line.find("TAX AMOUNT")

        for row in lines[i: min(len(lines), i + 4)]:
            nums = list(re.finditer(r"\b\d+(?:,\d{3})*(?:\.\d{1,2})?\b", row))
            if not nums:
                continue

            if tax_anchor is not None:
                right_side = [m for m in nums if m.start() >= tax_anchor]
                if right_side:
                    return round(float(right_side[-1].group().replace(",", "")), 2)

            return round(float(nums[-1].group().replace(",", "")), 2)
    return None

def _extract_amount_in_words_value(text: str) -> float | None:
    """Extract and parse amount from an 'Amount in Words:' style anchor."""
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        if "AMOUNT IN WORDS" not in line:
            continue

        candidates = []
        inline = re.search(r"AMOUNT\s+IN\s+WORDS\s*[:\-]\s*(.+)$", line)
        if inline and inline.group(1).strip():
            candidates.append(inline.group(1).strip())

        if idx + 1 < len(lines):
            candidates.append(lines[idx + 1].strip())

        for candidate in candidates:
            cleaned = re.sub(r"\b(INR|RUPEES?|ONLY)\b", " ", candidate, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            parsed = _words_to_number(cleaned)
            if parsed is not None:
                return round(parsed, 2)
    return None


def _extract_priority_invoice_number(text: str) -> str | None:
    for pattern in (
        r"\bPI\s*NO\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]*)",
        r"\bESTIMATION\s*NO\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]*)",
    ):
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1).strip(" -:/")
            if not is_non_invoice_identifier(candidate):
                return candidate

    for pattern in (
        r"INVOICE\s+NUMBER\s*[:\-]\s*([A-Z0-9][A-Z0-9\-/]*)",
        r"(?:INVOICE|INV|BILL|DOC|VOUCHER|S\.?NO)\s*(?:NO|NUMBER)?\.?\s*[:\-]?\s*([A-Z0-9\-/]+)",
    ):
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1).strip(" -:/")
            if candidate and re.search(r"\d", candidate) and not is_non_invoice_identifier(candidate):
                return candidate

    return None


def _extract_priority_invoice_date(lines: list[str]) -> str | None:
    date_pattern = r"\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b"
    for line in lines:
        upper = line.upper()
        if "PRICE IS VALID TILL" in upper or "WARRANTY" in upper:
            continue
        if "PI DATE" in upper or "ORDER REF DATE" in upper:
            match = re.search(date_pattern, line)
            if match:
                return match.group()
    return None


def _extract_total_order_value_excluding_tax(lines: list[str]) -> float | None:
    labels = ("TOTAL ORDER VALUE (EXCLUDING TAX)", "TOTAL ORDER VALUE EXCLUDING TAX")
    return _extract_labelled_amount(lines, labels)


def _extract_labelled_amount(lines: list[str], labels: tuple[str, ...]) -> float | None:
    for i, line in enumerate(lines):
        upper_line = line.upper()
        if not any(label in upper_line for label in labels):
            continue

        nums = _line_total_candidates(line)
        if nums:
            return round(nums[-1], 2)

        if i + 1 < len(lines):
            next_nums = _line_total_candidates(lines[i + 1])
            if next_nums:
                return round(next_nums[-1], 2)
    return None


def _extract_priority_cgst_sgst(lines: list[str]) -> tuple[float | None, float | None]:
    cgst_amount = _extract_tax_amount_near_label(lines, r"(?:9\s*%\s*CGST|CGST\s*[:\-]?\s*9\s*%)")
    sgst_amount = _extract_tax_amount_near_label(lines, r"(?:9\s*%\s*SGST|SGST\s*[:\-]?\s*9\s*%)")

    if cgst_amount is None:
        cgst_amount = _extract_tax_amount_from_tax_column(lines, "CGST")
    if sgst_amount is None:
        sgst_amount = _extract_tax_amount_from_tax_column(lines, "SGST")

    return cgst_amount, sgst_amount


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
    igst_row_sum = 0.0
    igst_rows_seen = 0
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

        igst_match = re.search(
            r"^\s*\d{4,8}\s+(\d+(?:,\d{3})*(?:\.\d{1,2})?)\s+\d{1,2}(?:\.\d{1,2})?\s*%?\s+(\d+(?:,\d{3})*(?:\.\d{1,2})?)",
            line,
        )
        if igst_match:
            igst_rows_seen += 1
            igst_row_sum += float(igst_match.group(2).replace(",", ""))

        if "TOTAL" in line:
            total_taxable = amounts[0]
            total_tax = round(amounts[-1], 2)
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
    if igst_rows_seen:
        result["summary_igst_sum"] = round(igst_row_sum, 2)
    return result


def _line_total_candidates(line: str) -> list[float]:
    amounts = []
    for match in re.finditer(r"\b\d+(?:,\d{3})*(?:\.\d{1,2})?\b", line):
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




def _extract_freight_amount(lines: list[str]) -> float | None:
    for line in lines:
        if "FREIGHT" not in line.upper():
            continue

        values = _line_total_candidates(line)
        if values:
            return round(values[-1], 2)
    return None


def _extract_item_amount_sum(lines: list[str]) -> float | None:
    in_item_table = False
    total = 0.0
    row_count = 0

    for line in lines:
        upper = line.upper()
        if "DESCRIPTION" in upper and "AMOUNT" in upper:
            in_item_table = True
            continue

        if not in_item_table:
            continue

        if any(stop in upper for stop in ("SUB TOTAL", "TOTAL TAXABLE", "CGST", "SGST", "IGST", "TOTAL IN WORDS", "AMOUNT IN WORDS")):
            break

        values = _line_total_candidates(line)
        if values:
            total += values[-1]
            row_count += 1

    if row_count:
        return round(total, 2)
    return None


def _validate_tax_math(data: Dict, tolerance: float = 0.01) -> tuple[bool, float, float]:
    taxable = float(data.get("Taxable Amount") or 0)
    cgst = float(data.get("CGST Amount") or 0)
    sgst = float(data.get("SGST Amount") or 0)
    igst = float(data.get("IGST Amount") or 0)
    round_off = float(data.get("Round Off") or 0)
    total_tax = data.get("Total Tax Amount")
    final_amount = data.get("Final Amount")

    if final_amount is None:
        return False, 0.0, 0.0

    tax_value = float(total_tax) if total_tax is not None else (igst if igst > 0 else (cgst + sgst))
    expected = round(taxable + tax_value + round_off, 2)
    actual = round(float(final_amount), 2)
    difference = round(actual - expected, 2)
    return abs(difference) <= tolerance, expected, difference


def _is_non_gst_invoice(data: Dict) -> bool:
    gst_number = data.get("GST Number")
    cgst = float(data.get("CGST Amount") or 0.0)
    sgst = float(data.get("SGST Amount") or 0.0)
    igst = float(data.get("IGST Amount") or 0.0)
    return not gst_number and cgst == 0.0 and sgst == 0.0 and igst == 0.0


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

    words_target = validated.get("Amount in Words Parsed")
    if words_target is not None and validated.get("Final Amount") is None:
        validated["Final Amount"] = round(float(words_target), 2)

    if _is_non_gst_invoice(validated):
        validated["Is GST Invoice"] = False
        validated["Invoice Type"] = "Non-GST Invoice"
        validated["CGST Amount"] = 0.0
        validated["SGST Amount"] = 0.0
        validated["IGST Amount"] = 0.0
        validated["Total Tax Amount"] = 0.0
        if validated.get("Final Amount") is not None:
            validated["Taxable Amount"] = round(float(validated["Final Amount"]), 2)
            validated["Sub Total"] = validated["Taxable Amount"]
    else:
        validated["Is GST Invoice"] = True
        validated.setdefault("Invoice Type", "GST Invoice")

    is_valid_math, expected, math_difference = _validate_tax_math(validated, tolerance=0.01)
    if not is_valid_math:
        validated = _retry_with_aggressive_patterns(text, validated)
        words_target = validated.get("Amount in Words Parsed")
        if words_target is not None and validated.get("Final Amount") is None:
            validated["Final Amount"] = round(float(words_target), 2)
        is_valid_math, expected, math_difference = _validate_tax_math(validated, tolerance=0.01)


    # Smart tax aggregation: when IGST is zero, trust CGST+SGST against words anchor target.
    if words_target is not None and float(validated.get("IGST Amount") or 0.0) == 0.0:
        recalculated = round(
            float(validated.get("Taxable Amount") or 0.0)
            + float(validated.get("CGST Amount") or 0.0)
            + float(validated.get("SGST Amount") or 0.0),
            2,
        )
        words_difference = round(float(words_target) - recalculated, 2)
        if abs(words_difference) <= 1.0:
            is_valid_math = True
            expected = recalculated
            math_difference = words_difference
            validated["Final Amount"] = round(float(words_target), 2)
            validated.setdefault("_rules_applied", []).append("CGST_SGST_WORDS_TOTAL_VALIDATION")

    if validated.get("Invoice Type") == "Non-GST Invoice":
        validated["Validation"] = "Non GST Invoice"
    else:
        validated["Validation"] = "Verified" if is_valid_math else "Math Mismatch"
    validated["Requires Manual Review"] = bool((validated.get("Final Amount") in (None, 0)) or not is_valid_math)

    validated["Math Expected Total"] = expected
    validated["Math Difference"] = math_difference
    validated["Step B - Tax Math Match"] = is_valid_math

    words_total = validated.get("Amount in Words Parsed")
    final_amount = validated.get("Final Amount")
    if words_total is not None and final_amount is not None:
        validated["Step A - Words Match"] = _is_close(float(words_total), float(final_amount), tolerance=0.01)
        validated["Words vs Total Difference"] = round(float(final_amount) - float(words_total), 2)

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
        "Round Off": 0.0,
        "Final Amount": None,
        "Is GST Invoice": False,
        "Confidence": {},
        "Source File Name": "invoice.pdf",
    }

    lines = text.split("\n")

    priority_invoice = _extract_priority_invoice_number(text)
    if priority_invoice:
        data["Invoice Number"] = priority_invoice
        data["Confidence"]["Invoice Number"] = 0.99
        applied_rules.append("INVOICE_NUMBER_LABEL_PRIORITY")

    gst = re.search(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", text)
    if gst and validate_gstin_checksum(gst.group()):
        data["GST Number"] = gst.group()
        data["Confidence"]["GST Number"] = 0.95

    inv_patterns = [
        r"(?:INVOICE|INV|BILL|DOC|VOUCHER|S\.?NO)\s*(?:NO|NUMBER)?\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]*\d(?:[A-Z0-9\-/]*))",
        r"\b(\d{3,8}/\d{2}-\d{2})\b",
    ]
    if not data.get("Invoice Number"):
        for pattern in inv_patterns:
            inv = re.search(pattern, text)
            if inv:
                candidate = inv.group(1).strip(" -:/")
                if re.search(r"\d", candidate) and not is_non_invoice_identifier(candidate):
                    data["Invoice Number"] = candidate
                    data["Confidence"]["Invoice Number"] = 0.95
                    applied_rules.append("INVOICE_NO_WITH_SUFFIX")
                    break

    priority_date = _extract_priority_invoice_date(lines)
    if priority_date:
        data["Invoice Date"] = priority_date
        data["Confidence"]["Invoice Date"] = 0.99
        applied_rules.append("PI_OR_ORDER_REF_DATE_PRIORITY")

    date_patterns = [
        r"\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b",
        r"\b\d{1,2}\s+[A-Z]{3,9}\s+\d{2,4}\b",
        r"\b\d{1,2}-[A-Z]{3}-\d{2,4}\b",
    ]
    if not data.get("Invoice Date"):
        for line in lines:
            upper = line.upper()
            if "PRICE IS VALID TILL" in upper or "WARRANTY" in upper:
                continue
            for pat in date_patterns:
                m = re.search(pat, line)
                if m:
                    data["Invoice Date"] = m.group()
                    data["Confidence"]["Invoice Date"] = 0.95
                    break
            if data["Invoice Date"]:
                break

    words_total = _extract_master_total_from_words(text)
    if words_total is None:
        words_total = get_amount_from_words(text)
    if words_total is None:
        words_total = _extract_amount_in_words_value(text)
    if words_total is None:
        words_total = _extract_amount_chargeable_in_words(text)
    if words_total is not None:
        data["Amount in Words Parsed"] = round(words_total, 2)
        data["Confidence"]["Amount in Words Parsed"] = 0.98
        applied_rules.append("MASTER_TOTAL_FROM_WORDS")

    summary = _extract_tax_summary_details(text)
    has_summary_taxable = summary.get("summary_taxable") is not None
    has_summary_igst = summary.get("summary_igst_sum") is not None

    primary_order_taxable = _extract_total_order_value_excluding_tax(lines)
    if primary_order_taxable is not None:
        data["Taxable Amount"] = primary_order_taxable
        data["Sub Total"] = primary_order_taxable
        data["Confidence"]["Taxable Amount"] = 0.99
        applied_rules.append("TOTAL_ORDER_VALUE_EXCLUDING_TAX_PRIORITY")

    if has_summary_taxable and data.get("Taxable Amount") is None:
        data["Taxable Amount"] = summary["summary_taxable"]
        data["Sub Total"] = summary["summary_taxable"]
        data["Confidence"]["Taxable Amount"] = 0.99
        applied_rules.append("SUMMARY_TABLE_TOTAL_ROW_PRIORITY")

    if summary.get("summary_tax") is not None:
        data["Total Tax Amount"] = summary["summary_tax"]
        data["Confidence"]["Total Tax Amount"] = 0.97

    if has_summary_igst:
        data["IGST Amount"] = summary["summary_igst_sum"]
        data["Total Tax Amount"] = summary["summary_igst_sum"]
        data["Confidence"]["IGST Amount"] = 0.99
        data["Confidence"]["Total Tax Amount"] = 0.99
        applied_rules.append("IGST_SUM_FROM_HSN_SAC_TABLE")

    if summary.get("line_taxable_sum") is not None:
        data["Line Item Taxable Sum"] = summary["line_taxable_sum"]
    if summary.get("line_tax_sum") is not None:
        data["Line Item Tax Sum"] = summary["line_tax_sum"]

    net_amount = _extract_labelled_amount(lines, ("NET AMOUNT",))
    if net_amount is not None and not has_summary_taxable:
        data["Taxable Amount"] = round(net_amount, 2)
        data["Sub Total"] = round(net_amount, 2)
        data["Confidence"]["Taxable Amount"] = max(data["Confidence"].get("Taxable Amount", 0.0), 0.99)
        applied_rules.append("NET_AMOUNT_PRIORITY")

    if data["Taxable Amount"] is None:
        subtotal = re.search(
            r"(?:SUB\s*TOTAL|TAXABLE\s*VALUE|TAXABLE\s*AMOUNT|BASIC\s*AMOUNT|NET\s*AMOUNT)[^\d]{0,40}(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+)",
            text,
        )
        if subtotal:
            taxable = float(subtotal.group(1).replace(",", ""))
            data["Taxable Amount"] = taxable
            data["Sub Total"] = taxable
            data["Confidence"]["Taxable Amount"] = 0.9

    if data["Taxable Amount"] is None and words_total is not None:
        target_taxable = words_total * 0.8475
        all_values = []
        for line in lines:
            all_values.extend(_line_total_candidates(line))
        approx_taxable = _pick_closest_to_target(all_values, target_taxable, tolerance=max(8.0, words_total * 0.02))
        if approx_taxable is not None:
            data["Taxable Amount"] = approx_taxable
            data["Sub Total"] = approx_taxable
            data["Confidence"]["Taxable Amount"] = 0.86
            applied_rules.append("TAXABLE_FROM_MASTER_TOTAL_RATIO")

    # Freight inclusion rule: taxable must include item amount + freight charges if present.
    freight_amount = _extract_freight_amount(lines)
    item_amount_sum = _extract_item_amount_sum(lines)
    if freight_amount is not None:
        data["Freight Charges"] = freight_amount
        if item_amount_sum is not None:
            combined_taxable = round(item_amount_sum, 2)
            if data.get("Taxable Amount") is None or abs(float(data.get("Taxable Amount") or 0.0) - combined_taxable) > 1.0:
                data["Taxable Amount"] = combined_taxable
                data["Sub Total"] = combined_taxable
                data["Confidence"]["Taxable Amount"] = max(data["Confidence"].get("Taxable Amount", 0.0), 0.96)
                applied_rules.append("TAXABLE_INCLUDES_FREIGHT_FROM_ITEMS")

    summed_cgst = _sum_tax_components(lines, "CGST")
    summed_sgst = _sum_tax_components(lines, "SGST")
    if summed_cgst is not None:
        data["CGST Amount"] = summed_cgst
        data["Confidence"]["CGST Amount"] = 0.97
        applied_rules.append("CGST_MULTI_LINE_SUM")
    if summed_sgst is not None:
        data["SGST Amount"] = summed_sgst
        data["Confidence"]["SGST Amount"] = 0.97
        applied_rules.append("SGST_MULTI_LINE_SUM")

    for tax in ["CGST", "SGST", "IGST"]:
        if tax in {"CGST", "SGST"} and data.get(f"{tax} Amount"):
            continue
        if tax == "IGST" and has_summary_igst:
            continue
        for line in lines:
            m = re.search(rf"\b{tax}\b[^\d]{{0,20}}(\d{{1,3}}(?:,\d{{3}})+(?:\.\d{{1,2}})?|\d+\.\d{{1,2}})", line)
            if m:
                data[f"{tax} Amount"] = round(float(m.group(1).replace(",", "")), 2)
                data["Confidence"][f"{tax} Amount"] = 0.9
                break

    if data.get("CGST Amount") and data.get("SGST Amount"):
        data["Total Tax Amount"] = round(float(data["CGST Amount"]) + float(data["SGST Amount"]), 2)
        data["Confidence"]["Total Tax Amount"] = max(data["Confidence"].get("Total Tax Amount", 0.0), 0.95)
        applied_rules.append("TOTAL_TAX_FROM_9_PERCENT_COMPONENTS")
    elif data.get("Total Tax Amount") is None:
        tax_sum = data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]
        if tax_sum > 0:
            data["Total Tax Amount"] = round(tax_sum, 2)

    round_off = _extract_round_off(lines)
    if round_off is not None:
        data["Round Off"] = round_off
        data["Confidence"]["Round Off"] = 0.95
        applied_rules.append("ROUND_OFF_CAPTURED")

    final = _extract_labelled_amount(lines, ("GRAND TOTAL",))
    if final is None:
        final = _extract_labelled_amount(lines, ("TOTAL AMOUNT",))
    if final is not None:
        applied_rules.append("TOTAL_AMOUNT_LABEL_PRIORITY")

    for line in reversed(lines):
        if re.search(r"\b(GRAND TOTAL|TOTAL AMOUNT|AMOUNT PAYABLE|NET PAYABLE|TOTAL)\b", line):
            nums = _line_total_candidates(line)
            if nums:
                final = nums[-1]
                break

    if final is None:
        for i, line in enumerate(lines):
            if "GRAND TOTAL" in line and i + 1 < len(lines):
                nums = _line_total_candidates(lines[i + 1])
                if nums:
                    final = nums[-1]
                    break

    if final is None:
        m = re.search(r"TOTAL\s*[:\-]?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)", text)
        if m:
            final = float(m.group(1).replace(",", ""))
            applied_rules.append("TOTAL_GENERIC_FALLBACK")

    if words_total is not None:
        data["Final Amount"] = round(words_total, 2)
        data["Confidence"]["Final Amount"] = 0.98
        applied_rules.append("FINAL_FROM_AMOUNT_IN_WORDS_ANCHOR")
    elif final is not None:
        data["Final Amount"] = round(final, 2)
        data["Confidence"]["Final Amount"] = 0.9

    if data.get("Taxable Amount") is None and data.get("Final Amount") is not None and data.get("Total Tax Amount") is not None:
        derived_taxable = round(float(data["Final Amount"]) - float(data["Total Tax Amount"]) - float(data.get("Round Off") or 0), 2)
        if derived_taxable > 0:
            data["Taxable Amount"] = derived_taxable
            data["Sub Total"] = derived_taxable
            data["Confidence"]["Taxable Amount"] = max(data["Confidence"].get("Taxable Amount", 0.0), 0.94)
            applied_rules.append("TAXABLE_DERIVED_FROM_MASTER_TOTAL_MINUS_TAX")

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

    if data.get("Taxable Amount") is not None and data.get("CGST Amount") is not None and data.get("SGST Amount") is not None and data.get("Final Amount") is not None:
        computed_total = round(float(data["Taxable Amount"]) + float(data["CGST Amount"]) + float(data["SGST Amount"]) + float(data.get("Round Off") or 0), 2)
        has_multiline_gst = "CGST_MULTI_LINE_SUM" in applied_rules or "SGST_MULTI_LINE_SUM" in applied_rules
        if not has_multiline_gst and not _is_close(computed_total, float(data["Final Amount"]), tolerance=0.01):
            fallback_cgst = _extract_tax_amount_from_tax_column(lines, "CGST")
            fallback_sgst = _extract_tax_amount_from_tax_column(lines, "SGST")
            if fallback_cgst is not None:
                data["CGST Amount"] = fallback_cgst
                data["Confidence"]["CGST Amount"] = max(data["Confidence"].get("CGST Amount", 0.0), 0.85)
            if fallback_sgst is not None:
                data["SGST Amount"] = fallback_sgst
                data["Confidence"]["SGST Amount"] = max(data["Confidence"].get("SGST Amount", 0.0), 0.85)
            if fallback_cgst is not None or fallback_sgst is not None:
                applied_rules.append("GST_FROM_TAX_AMOUNT_COLUMN_FALLBACK")

    for amount_key in ("Taxable Amount", "Sub Total", "CGST Amount", "SGST Amount", "IGST Amount", "Total Tax Amount", "Round Off", "Final Amount"):
        if data.get(amount_key) is not None:
            data[amount_key] = round(float(data[amount_key]), 2)

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
        "Priority rules: use PI No or Estimation No as Invoice Number; use PI Date or Order Ref Date as Invoice Date; "
        "ignore dates near 'Price is valid till' or 'Warranty'. "
        "For taxable amount, prioritize 'Total Order Value (Excluding Tax)'. "
        "Sum GST from main items and secondary charges in CGST/SGST/IGST. "
        "Capture final Grand Total from the bottom section. "
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
