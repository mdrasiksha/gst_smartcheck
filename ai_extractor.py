import json
import os
import re
from typing import Dict
from urllib import error, request
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

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
    address_keywords = ["PIN", "TAMIL", "NADU", "CHENNAI", "BANGALORE"]
    if re.fullmatch(r"\d{6}", num_str):
        return any(word in text.upper() for word in address_keywords)

    return False

def is_hsn_code(num_str, line_text):
    """
    Blocks HSN / SAC codes from being treated as money
    """
    cleaned = num_str.replace(",", "")
    integer_part = cleaned.split(".", 1)[0]
    if re.fullmatch(r"\d{4,8}", integer_part):
        return bool(re.search(r"\b(HSN|SAC)\b", line_text.upper()))
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
    upper_line = line.upper()
    identifier_keywords = ("UDYAM", "MSME", "LUT", "ARN", "IFSC", "ACCOUNT", "GSTIN", "A/C", "POLICY", "CHEQUE", "REFERENCE", "CODE", "BANK")
    line_has_identifier_keyword = any(keyword in upper_line for keyword in identifier_keywords)
    for match in re.finditer(r"\b\d+(?:,\d{3})*(?:\.\d{1,2})?\b", line):
        raw_value = match.group()
        normalized_value = raw_value.replace(",", "")
        integer_part = normalized_value.split(".", 1)[0]

        prev_char = line[match.start() - 1] if match.start() > 0 else ""
        next_char = line[match.end()] if match.end() < len(line) else ""

        # Ignore numeric fragments inside dot/hyphen/slash separated identifiers (e.g. 9100.310300, BA0001-9100).
        if prev_char in ".-/" or next_char in ".-/":
            if line_has_identifier_keyword:
                continue
            if prev_char == "." or next_char == ".":
                continue

        suffix = line[match.end(): match.end() + 10].strip().upper()
        if suffix.startswith("NOS") or suffix.startswith("UNITS") or suffix.startswith("PCS") or suffix.startswith("QTY"):
            continue

        context_window = upper_line[max(0, match.start() - 20): match.end() + 20]
        if is_hsn_code(raw_value, context_window):
            continue

        if re.fullmatch(r"\d{4,8}", integer_part):
            prefix_window = upper_line[max(0, match.start() - 12): match.start()]
            if re.search(r"\b(HSN|SAC)\b", context_window) or re.search(r"\b(HSN|SAC)\b\s*[:\-/]*\s*$", prefix_window):
                continue

        if is_address_number(integer_part, context_window):
            continue

        if (
            "." not in normalized_value
            and "," not in raw_value
            and line_has_identifier_keyword
        ):
            continue

        if "." not in normalized_value and "," not in raw_value and len(integer_part) >= 7:
            money_context = upper_line[max(0, match.start() - 20): min(len(upper_line), match.end() + 20)]
            if not re.search(r"\b(TOTAL|AMOUNT|VALUE|PREMIUM|TAX|GST|PAYABLE|NET|SUB)\b", money_context):
                continue

        amounts.append(float(normalized_value))
    return amounts


def _find_larger_total_candidate(lines: list[str], minimum: float) -> float | None:
    for line in reversed(lines):
        if not re.search(r"\b(TOTAL|GRAND TOTAL|AMOUNT PAYABLE|NET PAYABLE)\b", line):
            continue
        for value in reversed(_line_total_candidates(line)):
            if value > minimum:
                return value
    return None


def extract_sections(text: str) -> Dict[str, str]:
    lines = normalize_text(text).split("\n")

    def _find_anchor(start: int, anchors: tuple[str, ...], min_hits: int = 1) -> int | None:
        for idx in range(start, len(lines)):
            upper = lines[idx].upper()
            hits = sum(1 for anchor in anchors if anchor in upper)
            if hits >= min_hits:
                return idx
        return None

    item_start = _find_anchor(0, ("DESCRIPTION", "HSN", "QTY", "RATE", "AMOUNT"), min_hits=2)
    tax_start = _find_anchor(item_start or 0, ("CGST", "SGST", "IGST"), min_hits=1)
    total_start = _find_anchor(
        tax_start or item_start or 0,
        ("GRAND TOTAL", "AMOUNT PAYABLE", "TOTAL"),
        min_hits=1,
    )

    header_end = item_start if item_start is not None else len(lines)
    item_end = tax_start if tax_start is not None else (total_start if total_start is not None else len(lines))
    tax_end = total_start if total_start is not None else len(lines)

    sections = {
        "HEADER": "\n".join(lines[:header_end]).strip(),
        "ITEM_TABLE": "\n".join(lines[item_start:item_end]).strip() if item_start is not None else "",
        "TAX_BLOCK": "\n".join(lines[tax_start:tax_end]).strip() if tax_start is not None else "",
        "TOTAL_BLOCK": "\n".join(lines[total_start:]).strip() if total_start is not None else "",
    }
    return sections


def parse_item_table(section: str) -> Dict:
    items = []
    total = 0.0

    for raw_line in section.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        upper = line.upper()
        if any(stop in upper for stop in ("DESCRIPTION", "SUB TOTAL", "TOTAL TAXABLE", "CGST", "SGST", "IGST", "TOTAL")):
            continue

        amounts = _line_total_candidates(line)
        if not amounts:
            continue

        amount = round(amounts[-1], 2)
        hsn_match = re.search(r"\b\d{4,8}\b", line)
        description = re.sub(r"\b\d+(?:,\d{3})*(?:\.\d{1,2})?\b", " ", line)
        description = re.sub(r"\s+", " ", description).strip(" -:")

        items.append(
            {
                "hsn_code": hsn_match.group(0) if hsn_match else None,
                "description": description or None,
                "amount": amount,
            }
        )
        total += amount

    return {"items": items, "item_total_sum": round(total, 2) if items else None}


def parse_tax_block(section: str) -> Dict[str, float | None]:
    lines = section.split("\n") if section else []
    return {
        "CGST Amount": _sum_tax_components(lines, "CGST"),
        "SGST Amount": _sum_tax_components(lines, "SGST"),
        "IGST Amount": _sum_tax_components(lines, "IGST"),
    }


def parse_total_block(section: str) -> Dict[str, float | None]:
    lines = section.split("\n") if section else []
    taxable = _extract_labelled_amount(lines, ("TAXABLE AMOUNT", "TAXABLE VALUE", "SUB TOTAL", "BASIC AMOUNT"))
    round_off = _extract_round_off(lines)
    final = _extract_labelled_amount(lines, ("GRAND TOTAL", "AMOUNT PAYABLE", "FINAL TOTAL", "NET PAYABLE", "TOTAL"))

    return {
        "Taxable Amount": taxable,
        "Round Off": round_off,
        "Final Amount": final,
    }




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


def _extract_total_using_keywords(lines: list[str], target_total: float) -> float | None:
    keyword_pattern = re.compile(r"\b(TOTAL|AMOUNT\s+PAYABLE|GRAND\s+TOTAL)\b")
    candidates: list[float] = []

    for idx, line in enumerate(lines):
        if not keyword_pattern.search(line.upper()):
            continue

        window = lines[max(0, idx - 1): min(len(lines), idx + 2)]
        for candidate_line in window:
            candidates.extend(_line_total_candidates(candidate_line))

    if not candidates:
        return None

    return round(min(candidates, key=lambda amount: abs(amount - target_total)), 2)


def _is_valid_date(value) -> bool:
    if not value:
        return False
    return bool(
        re.search(
            r"\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b|\b\d{1,2}\s+[A-Z]{3,9}\s+\d{2,4}\b|\b\d{1,2}-[A-Z]{3}-\d{2,4}\b",
            str(value).upper(),
        )
    )


def _tax_rate_is_consistent(taxable: float | None, tax_amount: float | None, tolerance: float = 0.015) -> bool:
    if taxable is None or taxable <= 0 or tax_amount is None:
        return False
    observed_rate = tax_amount / taxable
    valid_rates = (0.025, 0.05, 0.06, 0.09, 0.12, 0.14, 0.18, 0.28)
    return any(abs(observed_rate - valid_rate) <= tolerance for valid_rate in valid_rates)


def calculate_field_confidence(data: dict, text: str) -> dict:
    invoice_number = str(data.get("Invoice Number") or "").strip()
    gstin = str(data.get("GSTIN") or data.get("GST Number") or "").strip().upper()
    vendor_name = str(data.get("Vendor Name") or "").strip()
    taxable = _coerce_float(data.get("Taxable Amount"))
    cgst = _coerce_float(data.get("CGST Amount")) or 0.0
    sgst = _coerce_float(data.get("SGST Amount")) or 0.0
    igst = _coerce_float(data.get("IGST Amount")) or 0.0
    final_amount = _coerce_float(data.get("Final Amount"))

    tax_total = cgst + sgst + igst
    expected_total = round((taxable or 0.0) + tax_total, 2) if taxable is not None else None
    calc_match = expected_total is not None and final_amount is not None and abs(expected_total - final_amount) <= 1.0

    invoice_pattern = bool(re.search(r"\b(?:INV|INVOICE)[\s\-_/]*[A-Z0-9]*\d+[A-Z0-9\-/]*\b", invoice_number, flags=re.IGNORECASE))
    invoice_clean_len = len(re.sub(r"[^A-Z0-9]", "", invoice_number.upper()))
    if invoice_pattern and invoice_clean_len >= 5:
        invoice_conf = 0.92
    elif invoice_clean_len >= 4 and re.search(r"\d", invoice_number):
        invoice_conf = 0.7
    else:
        invoice_conf = 0.45

    gstin_conf = 0.95 if (gstin and validate_gstin_checksum(gstin)) else 0.3

    taxable_conf = 0.4
    if taxable is not None and taxable > 0:
        taxable_conf = 0.8
        if calc_match:
            taxable_conf = 0.95

    final_conf = 0.4
    if final_amount is not None and final_amount > 0:
        final_conf = 0.8
        if calc_match:
            final_conf = 0.95

    tax_confidence = {}
    for tax_key, tax_amount in (("CGST Amount", cgst), ("SGST Amount", sgst), ("IGST Amount", igst)):
        if tax_amount in {5.0, 12.0, 18.0}:
            tax_confidence[tax_key] = 0.2
        elif _tax_rate_is_consistent(taxable, tax_amount):
            tax_confidence[tax_key] = 0.9
        elif tax_amount > 0:
            tax_confidence[tax_key] = 0.6
        else:
            tax_confidence[tax_key] = 0.5

    vendor_alpha = len(vendor_name) > 3 and bool(re.search(r"[A-Z]{3,}", vendor_name.upper())) and not bool(
        re.search(r"[^A-Z0-9&().,\- /\s]", vendor_name.upper())
    )
    vendor_conf = 0.8 if vendor_alpha else 0.3
    date_conf = 0.9 if _is_valid_date(data.get("Invoice Date")) else 0.4

    field_confidence = {
        "Invoice Number": invoice_conf,
        "Vendor Name": vendor_conf,
        "GSTIN": gstin_conf,
        "Taxable Amount": taxable_conf,
        "CGST Amount": tax_confidence["CGST Amount"],
        "SGST Amount": tax_confidence["SGST Amount"],
        "IGST Amount": tax_confidence["IGST Amount"],
        "Final Amount": final_conf,
        "Invoice Date": date_conf,
    }

    if not calc_match:
        for key in ("CGST Amount", "SGST Amount", "IGST Amount", "Final Amount"):
            field_confidence[key] = min(field_confidence[key], 0.4)

    return field_confidence


def calculate_confidence(data: Dict, text: str) -> Dict:
    """Calculate field-level + weighted overall confidence and return compact metrics."""
    field_confidence = calculate_field_confidence(data, text)
    weights = {
        "Final Amount": 2,
        "CGST Amount": 2,
        "SGST Amount": 2,
        "IGST Amount": 2,
    }
    total_weight = 0
    weighted_sum = 0.0
    for field, score in field_confidence.items():
        weight = weights.get(field, 1)
        total_weight += weight
        weighted_sum += score * weight

    overall_confidence = round(weighted_sum / total_weight, 4) if total_weight else 0.0
    low_confidence_fields = [field for field, score in field_confidence.items() if score < 0.6]
    return {
        "Overall Confidence": overall_confidence,
        "Field Confidence": field_confidence,
        "Low Confidence Fields": low_confidence_fields,
        "Validation": data.get("Validation"),
        "Math Difference": data.get("Math Difference"),
    }


def _is_non_gst_invoice(data: Dict) -> bool:
    gst_number = data.get("GST Number")
    cgst = float(data.get("CGST Amount") or 0.0)
    sgst = float(data.get("SGST Amount") or 0.0)
    igst = float(data.get("IGST Amount") or 0.0)
    return not gst_number and cgst == 0.0 and sgst == 0.0 and igst == 0.0


def _retry_with_aggressive_patterns(text: str, data: Dict) -> Dict:
    retry_data = dict(data)

    total_priority_patterns = [
        r"\bGRAND\s*TOTAL\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})",
        r"\bAMOUNT\s*PAYABLE\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})",
        r"\bNET\s*PAYABLE\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})",
        r"\bTOTAL\s*₹?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})",
        r"\bTOTAL\s*AMOUNT\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})",
    ]
    taxable_patterns = [
        r"(?:TAXABLE\s*VALUE|TAXABLE\s*AMOUNT|SUB\s*TOTAL|BASIC\s*AMOUNT)[^\d]{0,20}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
    ]

    if retry_data.get("Final Amount") in (None, 0):
        for pattern in total_priority_patterns:
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
    validated.setdefault("_rules_applied", [])
    lines = text.split("\n")

    # Stage 1: Primary label-based extraction
    label_map = {
        "Taxable Amount": ("TAXABLE AMOUNT", "TAXABLE VALUE", "SUB TOTAL", "BASIC AMOUNT"),
        "CGST Amount": ("CGST",),
        "SGST Amount": ("SGST",),
        "IGST Amount": ("IGST",),
        "Final Amount": ("TOTAL", "GRAND TOTAL", "AMOUNT PAYABLE"),
    }

    for key, labels in label_map.items():
        if validated.get(key) in (None, 0, 0.0):
            amount = _extract_labelled_amount(lines, labels)
            if amount is not None:
                validated[key] = amount
                validated["Confidence"][key] = max(validated["Confidence"].get(key, 0.0), 0.88)

    if not validated.get("Invoice Number"):
        invoice_match = re.search(r"\bINVOICE\s*(?:NO|NUMBER)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]*)", text)
        if invoice_match:
            validated["Invoice Number"] = invoice_match.group(1).strip(" -:/")
            validated["_invoice_number_label_match"] = True

    if not validated.get("Invoice Date"):
        date_match = re.search(r"\bINVOICE\s*DATE\s*[:\-]?\s*(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})", text)
        if date_match:
            validated["Invoice Date"] = date_match.group(1)

    if not validated.get("GST Number"):
        gst_match = re.search(r"\bGSTIN\s*[:\-]?\s*(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9])\b", text)
        if gst_match:
            validated["GST Number"] = gst_match.group(1)

    sections = extract_sections(text)
    item_details = parse_item_table(sections.get("ITEM_TABLE", ""))
    tax_details = parse_tax_block(sections.get("TAX_BLOCK", ""))
    combined_total_section = "\n".join(part for part in (sections.get("TAX_BLOCK", ""), sections.get("TOTAL_BLOCK", "")) if part)
    total_details = parse_total_block(combined_total_section)

    for tax_key in ("CGST Amount", "SGST Amount", "IGST Amount"):
        if validated.get(tax_key) in (None, 0, 0.0) and tax_details.get(tax_key) is not None:
            validated[tax_key] = tax_details[tax_key]
            validated["Confidence"][tax_key] = max(validated["Confidence"].get(tax_key, 0.0), 0.88)

    if validated.get("Taxable Amount") in (None, 0, 0.0) and total_details.get("Taxable Amount") is not None:
        validated["Taxable Amount"] = total_details["Taxable Amount"]
        validated["Sub Total"] = total_details["Taxable Amount"]
        validated["Confidence"]["Taxable Amount"] = max(validated["Confidence"].get("Taxable Amount", 0.0), 0.88)

    if total_details.get("Round Off") is not None:
        validated["Round Off"] = total_details["Round Off"]

    if validated.get("Final Amount") in (None, 0, 0.0) and total_details.get("Final Amount") is not None:
        validated["Final Amount"] = total_details["Final Amount"]

    if validated.get("Taxable Amount") in (None, 0, 0.0) and item_details.get("item_total_sum") is not None:
        validated["Taxable Amount"] = item_details["item_total_sum"]
        validated["Sub Total"] = item_details["item_total_sum"]

    # Stage 2: Math validation (taxable + cgst + sgst + roundoff ~= total)
    taxable = float(validated.get("Taxable Amount") or 0.0)
    cgst = float(validated.get("CGST Amount") or 0.0)
    sgst = float(validated.get("SGST Amount") or 0.0)
    round_off = float(validated.get("Round Off") or 0.0)
    current_total = float(validated.get("Final Amount") or 0.0)
    expected_total = round(taxable + cgst + sgst + round_off, 2)
    math_difference = round(current_total - expected_total, 2)
    is_valid_math = abs(math_difference) <= 1.0
    validated.setdefault("_rules_applied", []).append("MATH_VALIDATION")

    # Stage 3: Fallback extraction when mismatch > 1 rupee
    if not is_valid_math:
        tax_component = float(validated.get("IGST Amount") or 0.0)
        if tax_component == 0.0:
            tax_component = cgst + sgst
        fallback_target = round(taxable + tax_component, 2)
        fallback_total = _extract_total_using_keywords(lines, fallback_target)
        if fallback_total is not None:
            validated["Final Amount"] = fallback_total
            current_total = fallback_total
            math_difference = round(current_total - expected_total, 2)
            is_valid_math = abs(math_difference) <= 1.0
            validated.setdefault("_rules_applied", []).append("TOTAL_BLOCK_PRIORITY")

    words_total = validated.get("Amount in Words Parsed")
    words_match = False
    if words_total is not None and validated.get("Final Amount") is not None:
        words_match = _is_close(float(words_total), float(validated["Final Amount"]), tolerance=1.0)
        if words_match:
            validated.setdefault("_rules_applied", []).append("WORDS_TOTAL_MATCH")

    for key in ("Taxable Amount", "CGST Amount", "SGST Amount", "IGST Amount", "Final Amount"):
        validated["Confidence"].setdefault(key, 0.4 if validated.get(key) is None else 0.7)

    if _is_non_gst_invoice(validated):
        validated["Is GST Invoice"] = False
        validated["Invoice Type"] = "Non-GST Invoice"
        validated["Validation"] = "Non GST Invoice"
    else:
        validated["Is GST Invoice"] = True
        validated.setdefault("Invoice Type", "GST Invoice")
        validated["Validation"] = "Verified" if is_valid_math else "Math Mismatch"

    validated["Requires Manual Review"] = bool((validated.get("Final Amount") in (None, 0)) or not is_valid_math)
    validated["Math Expected Total"] = expected_total
    validated["Math Difference"] = math_difference
    validated["Step B - Tax Math Match"] = is_valid_math
    validated["Step A - Words Match"] = words_match

    # Stage 4: Confidence scoring
    confidence_summary = calculate_confidence(validated, text)
    validated["Field Confidence"] = confidence_summary["Field Confidence"]
    validated["Confidence"] = dict(confidence_summary["Field Confidence"])
    validated["_low_confidence_fields"] = confidence_summary["Low Confidence Fields"]
    validated["Overall Confidence"] = confidence_summary["Overall Confidence"]

    # Stage 5: Rules tracking
    required_rules = ["TOTAL_BLOCK_PRIORITY", "MATH_VALIDATION", "WORDS_TOTAL_MATCH"]
    existing_rules = validated.setdefault("_rules_applied", [])
    for rule in required_rules:
        if rule not in existing_rules:
            existing_rules.append(rule)

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
        "_invoice_number_label_match": False,
    }

    lines = text.split("\n")

    priority_invoice = _extract_priority_invoice_number(text)
    if priority_invoice:
        data["Invoice Number"] = priority_invoice
        data["Confidence"]["Invoice Number"] = 0.99
        data["_invoice_number_label_match"] = True
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
                    data["_invoice_number_label_match"] = True
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

    total_priority_patterns = [
        ("GRAND_TOTAL_PRIORITY", r"\bGRAND\s*TOTAL\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})"),
        ("AMOUNT_PAYABLE_PRIORITY", r"\bAMOUNT\s*PAYABLE\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})"),
        ("NET_PAYABLE_PRIORITY", r"\bNET\s*PAYABLE\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})"),
        ("TOTAL_RUPEE_PRIORITY", r"\bTOTAL\s*₹?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})"),
        ("TOTAL_AMOUNT_PRIORITY", r"\bTOTAL\s*AMOUNT\b[^\d]{0,25}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})"),
    ]

    final = None
    for rule_name, pattern in total_priority_patterns:
        matched = re.search(pattern, text)
        if matched:
            final = float(matched.group(1).replace(",", ""))
            applied_rules.append(rule_name)
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


def _extract_with_gpt(text: str, existing_data: Dict | None = None, retries: int = 2) -> Dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return {}

    schema = {
        "Invoice Number": None,
        "Invoice Date": None,
        "GST Number": None,
        "Taxable Amount": None,
        "CGST Amount": None,
        "SGST Amount": None,
        "IGST Amount": None,
        "Final Amount": None,
    }
    validation_rules = [
        "Return strict JSON object only with schema keys, no markdown.",
        "Handle OCR noise such as O/0, I/1, S/5 confusion.",
        "Do not treat percentage rates (e.g., 18%) as tax amounts.",
        "Final Amount should approximately equal Taxable + CGST + SGST + IGST.",
        "Use null for missing values.",
    ]
    client = OpenAI(api_key=api_key)
    content = {
        "schema": schema,
        "validation_rules": validation_rules,
        "existing_data": existing_data or {},
        "ocr_text": text[:8000],
    }

    for _ in range(max(1, retries + 1)):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract invoice fields from OCR text using the provided schema and rules. "
                            "Return strict JSON only."
                        ),
                    },
                    {"role": "user", "content": json.dumps(content, ensure_ascii=False)},
                ],
                timeout=8,
            )
            payload = response.choices[0].message.content or ""
            parsed = _extract_json_object(payload)
            if parsed:
                return {
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
                    "_rules_applied": ["AI_GPT4O_MINI_EXTRACTION"],
                }
        except Exception:
            continue
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

    # Keep Gemini fast/fail-fast in bulk paths.
    gemini_timeout_seconds = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "8"))
    try:
        with request.urlopen(req, timeout=gemini_timeout_seconds) as resp:
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
    # Optional fast-mode gate: disable Gemini unless explicitly enabled.
    if os.getenv("ENABLE_GEMINI", "false").strip().lower() in {"1", "true", "yes", "on"}:
        ai_data = _extract_with_gemini(text)
        if ai_data:
            return run_validation_engine(normalize_text(text), ai_data)
    return _extract_invoice_fields_regex(text)


def extract_invoice_fields_gpt(text: str, existing_data: Dict | None = None, retries: int = 2) -> dict:
    ai_data = _extract_with_gpt(text, existing_data=existing_data, retries=retries)
    if ai_data:
        return run_validation_engine(normalize_text(text), ai_data)
    return {}
