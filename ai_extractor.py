import re



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
def extract_invoice_fields(text: str) -> dict:
    applied_rules = []
    text = normalize_text(text)
    print("\n================ DEBUG TEXT START ================\n")
    print(text)
    print("\n================ DEBUG TEXT END ================\n")

    data = {
        "Invoice Number": None,
        "Invoice Date": None,
        "GST Number": None,
        "Taxable Amount": None,
        "Sub Total": None,
        "CGST Amount": 0.0,
        "SGST Amount": 0.0,
        "IGST Amount": 0.0,
        "Final Amount": None,
        "Is GST Invoice": False,
        "Confidence": {},
    }

    lines = text.split("\n")
    final = None  # single source of truth

    # =========================================================
    # HSN / SAC CODE EXTRACTION (NON-DESTRUCTIVE)
    # =========================================================
    hsn_codes = set()

    for line in lines:
        # Common patterns: HSN 8471, HSN CODE: 9983, SAC 998313
        matches = re.findall(r"\b(?:HSN|SAC)\s*(?:CODE)?\s*[:\-]?\s*(\d{4,8})\b", line)
        for m in matches:
            hsn_codes.add(m)

    if hsn_codes:
        data["HSN Codes"] = ", ".join(sorted(hsn_codes))
        data["Confidence"]["HSN Codes"] = 0.90
    else:
        data["HSN Codes"] = None


    # =========================================================
    # GST NUMBER
    # =========================================================
    gst = re.search(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", text)
    if gst:
        data["GST Number"] = gst.group()
        data["Is GST Invoice"] = True
        data["Confidence"]["GST Number"] = 0.95
        applied_rules.append("GST_REGEX_MATCH")

    # =========================================================
    # INVOICE NUMBER
    # =========================================================
    inv = re.search(r"(INVOICE|INV)\s*NO\.?\s*[:\-]?\s*([A-Z0-9\-\/]{6,})", text)
    if inv:
        data["Invoice Number"] = inv.group(2)
        data["Confidence"]["Invoice Number"] = 0.95
        applied_rules.append("INVOICE_NO_STRICT_MATCH")

    else:
        loose_inv = re.search(r"\b([A-Z]{2,5}-[A-Z0-9\-\/]{4,})\b", text)
        if loose_inv:
            candidate = loose_inv.group(1)
            if not is_non_invoice_identifier(candidate):
                data["Invoice Number"] = candidate
                data["Confidence"]["Invoice Number"] = 0.8
    # =========================================================
    # NUMERIC-ONLY INVOICE NUMBER FALLBACK (SAFE)
    # Handles: Invoice No : 1115
    # =========================================================
    if not data.get("Invoice Number"):
        m = re.search(
            r"(INVOICE\s*(NO|NUMBER)?)[^\d]{0,10}(\d{3,10})",
            text
        )
        if m:
            data["Invoice Number"] = m.group(3)
            data["Confidence"]["Invoice Number"] = 0.95

    # =========================================================
    # INVOICE DATE – SAFE, NO GARBAGE
    # =========================================================
    date_patterns = [
        r"\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b",
        r"\b\d{1,2}\s+[A-Z]{3,9}\s+\d{2,4}\b",
        r"\b\d{1,2}-[A-Z]{3}-\d{2,4}\b",
        r"\b[A-Z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b",
    ]

    found_date = None
    for i, line in enumerate(lines):
        if "TOTAL" in line or "%" in line:
            continue
        for pat in date_patterns:
            m = re.search(pat, line)
            if m:
                found_date = m.group()
                break
        if found_date:
            break

        if ("DATED" in line or "DT." in line) and i + 1 < len(lines):
            for pat in date_patterns:
                m = re.search(pat, lines[i + 1])
                if m:
                    found_date = m.group()
                    break
        if found_date:
            break

    if found_date:
        data["Invoice Date"] = found_date
        data["Confidence"]["Invoice Date"] = 0.95

    # =========================================================
    # TAXABLE / SUBTOTAL – KEYWORD
    # =========================================================
    subtotal = re.search(
        r"(SUBTOTAL|SUB TOTAL|TAXABLE VALUE|TAXABLE AMOUNT|BASIC AMOUNT)[^\d]{0,40}(\d+(\.\d{1,2})?)",
        text,
    )
    if subtotal:
        val = float(subtotal.group(2))
        data["Taxable Amount"] = val
        data["Sub Total"] = val
        data["Confidence"]["Taxable Amount"] = 0.95
        applied_rules.append("TAXABLE_KEYWORD_MATCH")

    # =========================================================
    # TAXABLE – LINE ITEM (SaaS / KREDENT / TABLE)
    # =========================================================
    if not data["Taxable Amount"]:
        li = re.search(r"\b\d+\s+.+?\s+(\d+(\.\d{1,2})?)\s+\d{6}\b", text)
        if li:
            val = float(li.group(1))
            data["Taxable Amount"] = val
            data["Sub Total"] = val
            data["Confidence"]["Taxable Amount"] = 0.9

    # =========================================================
    # =========================================================
    # TAXABLE – OCR MERGED FIX (338.14998439)
    # =========================================================
    if not data["Taxable Amount"]:
        merged = re.search(r"\b(\d+\.\d{2})(\d{6})\b", text)
        if merged:
            val = float(merged.group(1))
            data["Taxable Amount"] = val
            data["Sub Total"] = val
            data["Confidence"]["Taxable Amount"] = 0.95

    # =========================================================
    # FINAL GUARANTEED TAXABLE FIX (INDUSTRIAL / ELECTRICAL INVOICES)
    # CRITICAL: DO NOT MOVE, DO NOT MODIFY
    # Only triggers if Taxable Amount is still missing
    # =========================================================
    if data["Taxable Amount"] is None:
        industrial_candidates = []

        for line in lines:
            # Stop at GRAND TOTAL – never read footer
            if "GRAND TOTAL" in line:
                break

            # Skip tax, total, percentage lines
            if any(k in line for k in ["CGST", "SGST", "IGST", "TOTAL", "%"]):
                continue

            # Capture large numbers (real business values)
            nums = re.findall(r"\b\d{4,}\.\d{1,2}\b|\b\d{4,}\b", line)
            for n in nums:
                try:
                    val = float(n)
                    industrial_candidates.append(val)
                except:
                    pass

        if industrial_candidates:
            # Business rule: taxable is always the largest amount before tax
            taxable_val = max(industrial_candidates)
            applied_rules.append("INDUSTRIAL_LARGEST_PRE_TAX")

            # Hard safety: must be realistic business amount
            if taxable_val > 1000:
                data["Taxable Amount"] = taxable_val
                data["Sub Total"] = taxable_val
                data["Confidence"]["Taxable Amount"] = 0.95

    # =========================================================
    # FINAL GUARANTEED TAXABLE FIX (DO NOT MOVE – DO NOT MODIFY)
    # =========================================================
    if data["Taxable Amount"] is None:
        strong_candidates = []

        for line in lines:
            if any(x in line for x in ["CGST", "SGST", "IGST", "TOTAL", "GRAND", "%"]):
                continue

            nums = re.findall(r"(\d{4,}\.\d{1,2})", line)
            for n in nums:
                strong_candidates.append(float(n))

        if strong_candidates:
            taxable_val = max(strong_candidates)

            if taxable_val > 1000:
                data["Taxable Amount"] = taxable_val
                data["Sub Total"] = taxable_val
                data["Confidence"]["Taxable Amount"] = 0.95
    # =========================================================
    # FINAL INDUSTRIAL / ELECTRICAL TAXABLE FIX (NON-DESTRUCTIVE)

    # Blocks HSN, Order IDs, Invoice numbers from being misread as amounts
    # =========================================================
    if data["Taxable Amount"] is None:
        industrial_values = []

        for line in lines:
            line_clean = line.strip()

            # Stop at GRAND TOTAL – footer zone
            if "GRAND TOTAL" in line_clean:
                break

            # Skip non-money lines
            if any(x in line_clean for x in [
                "HSN", "SAC", "INVOICE", "ORDER", "GSTIN", "PAN",
                "CGST", "SGST", "IGST", "%", "QTY", "QUANTITY"
            ]):
                continue

            # Capture only realistic money values (with decimals)
            nums = re.findall(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b", line_clean)

            for n in nums:
                if is_hsn_code(n, line_clean):
                    continue  # skip HSN codes safely

                try:
                    val = float(n.replace(",", ""))

                    if val < 1000000:
                        industrial_values.append(val)
                except:
                    pass

        if industrial_values:
            taxable_val = max(industrial_values)

            # Safety: taxable must be less than final amount
            if not data["Final Amount"] or taxable_val < data["Final Amount"]:
                data["Taxable Amount"] = taxable_val
                data["Sub Total"] = taxable_val
                data["Confidence"]["Taxable Amount"] = 0.95

    # GST SPLIT – WITH %
    # =========================================================
    percent_tax = re.findall(r"\b(CGST|SGST|IGST)\b\s*@?\s*\d+(\.\d+)?%\s+(\d+(\.\d{1,2})?)", text)
    for tax, _, amt, _ in percent_tax:
        data[f"{tax} Amount"] = float(amt)
        data["Confidence"][f"{tax} Amount"] = 0.95

    # =========================================================
    # GST SPLIT – AMOUNT ONLY
    # =========================================================
    simple_tax = re.findall(r"\b(CGST|SGST|IGST)\b(?![^\n]*%)\s*[^\d]{0,10}(\d+(\.\d{1,2})?)", text)
    for tax, amt, _ in simple_tax:
        key = f"{tax} Amount"
        if data[key] == 0:
            data[key] = float(amt)
            data["Confidence"][key] = 0.9
    # =========================================================
    # ADD-ON GST SPLIT SUPPORT
    # Handles: CGST9 (9%) 1,348.20
    # NON-DESTRUCTIVE (runs only if GST amount is still 0)
    # =========================================================
    alt_gst = re.findall(
        r"\b(CGST|SGST|IGST)\s*\d*\s*\(?\d+(\.\d+)?%\)?\s+(\d{1,3}(?:,\d{3})*\.\d{2})",
        text
    )

    for tax, _, amt in alt_gst:
        key = f"{tax} Amount"
        if data.get(key, 0) == 0:
            data[key] = float(amt.replace(",", ""))
            data["Confidence"][key] = 0.95

    # =========================================================
    # SAFETY: PREVENT TAX % FROM BECOMING TAXABLE (WITHOUT KILLING REAL VALUES)
    # =========================================================
    if data["Taxable Amount"] is not None:
        # Only block if it's clearly a tax RATE, not a real amount
        if data["Taxable Amount"] <= 100 and re.search(r"\b(CGST|SGST|IGST)\b", text):
            data["Taxable Amount"] = None
            data["Sub Total"] = None
            data["Confidence"].pop("Taxable Amount", None)

    # =========================================================
    # FINAL AMOUNT – OCR MERGED NUMBER + GRAND TOTAL FIX
    # Handles: 955328GRAND TOTAL
    # =========================================================
    if not data["Final Amount"]:
        merged_total = re.search(
            r"\b(\d{3,}\.\d{1,2}|\d{3,})\s*GRAND\s*TOTAL\b",
            text
        )
        if merged_total:
            data["Final Amount"] = float(merged_total.group(1))
            data["Confidence"]["Final Amount"] = 0.95

    # =========================================================
    # ADD-ON: FINAL AMOUNT OCR MERGE FIX
    # Handles: 955328GRAND TOTAL
    # =========================================================
    if data["Final Amount"] is None:
        merged_total = re.search(
            r"\b(\d{3,}\.\d{1,2}|\d{3,})\s*GRAND\s*TOTAL\b",
            text
        )
        if merged_total:
            data["Final Amount"] = float(merged_total.group(1))
            data["Confidence"]["Final Amount"] = 0.95

    # FINAL AMOUNT – STRICT TOTAL LINES
    # =========================================================
    for line in lines:
        if re.search(r"\b(GRAND TOTAL|TOTAL AMOUNT|TOTAL INVOICE VALUE|AMOUNT PAYABLE|NET PAYABLE|BALANCE AMOUNT|TOTAL)\b", line):
            nums = re.findall(r"\b\d+\.\d{1,2}\b|\b\d+\b", line)
            if nums:
                final = nums[-1]
                break

    # =========================================================
    # MMT / TRAVEL FIX – GRAND TOTAL ON NEXT LINE (SAFE)
    # =========================================================
    if not final:
        for i, line in enumerate(lines):
            if "GRAND TOTAL" in line:
                nums = re.findall(r"\b\d+\.\d{1,2}\b|\b\d+\b", line)
                if nums:
                    final = nums[-1]
                    break
                elif i + 1 < len(lines):
                    next_nums = re.findall(r"\b\d+\.\d{1,2}\b|\b\d+\b", lines[i + 1])
                    if next_nums:
                        final = next_nums[-1]
                        break

    # =========================================================
    # ADD-ON FINAL AMOUNT SUPPORT
    # Handles: Total ₹17,676.00
    # NON-DESTRUCTIVE (runs only if Final Amount missing)
    # =========================================================
    if data["Final Amount"] is None:
        simple_total = re.search(
            r"\bTOTAL\b[^\d]{0,10}(₹)?\s*(\d{1,3}(?:,\d{3})*\.\d{2})",
            text
        )
        if simple_total:
            data["Final Amount"] = float(simple_total.group(2).replace(",", ""))
            data["Confidence"]["Final Amount"] = 0.95

    # =========================================================
    # FALLBACK – LAST LARGE NUMBER (NO PIN CODES)
    # =========================================================
    if not final:
        candidates = re.findall(r"\b\d{3,}\.\d{1,2}\b|\b\d{3,}\b", text)

        clean_candidates = []
        for c in candidates:
            if not is_address_number(c, text):
                clean_candidates.append(c)

        if clean_candidates:
            final = clean_candidates[-1]

    # =========================================================
    # ASSIGN FINAL SAFELY
    # =========================================================
    if final:
        try:
            data["Final Amount"] = float(final)
            data["Confidence"]["Final Amount"] = 0.95
            applied_rules.append("FINAL_FROM_TOTAL_LINE")

        except:
            pass

    # =========================================================
    # OCR CONCAT GUARD (87338.14 KILLER)
    # =========================================================
    if data["Final Amount"] and data["Taxable Amount"]:
        if data["Final Amount"] > data["Taxable Amount"] * 5:
            for line in lines:
                if "TOTAL" in line:
                    nums = re.findall(r"\b\d+\.\d{1,2}\b|\b\d+\b", line)
                    if nums:
                        data["Final Amount"] = float(nums[-1])
                        break

    # =========================================================
    # COMPUTED FALLBACK (ACT / UTILITY)
    # =========================================================
    if (not data["Final Amount"] or data["Final Amount"] == 0) and data["Taxable Amount"]:
        computed = data["Taxable Amount"] + data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]
        if computed > data["Taxable Amount"]:
            data["Final Amount"] = round(computed, 2)
            data["Confidence"]["Final Amount"] = 0.95
            applied_rules.append("FINAL_COMPUTED_FROM_GST")

    # =========================================================
    # GST FLAG
    # =========================================================
    data["Is GST Invoice"] = bool(
        data["GST Number"] or data["CGST Amount"] or data["SGST Amount"] or data["IGST Amount"]
    )

    # =========================================================
    # FINAL BUSINESS SAFETY
    # If GST exists and Final Amount still missing,
    # compute Final = Taxable + GST
    # =========================================================
    if (
            data.get("Final Amount") in [None, 0]
            and data.get("Taxable Amount")
            and (
            data.get("CGST Amount", 0)
            + data.get("SGST Amount", 0)
            + data.get("IGST Amount", 0)
    ) > 0
    ):
        data["Final Amount"] = round(
            data["Taxable Amount"]
            + data["CGST Amount"]
            + data["SGST Amount"]
            + data["IGST Amount"], 2
        )
        data["Confidence"]["Final Amount"] = 0.90

    # =========================================================
    # OVERALL CONFIDENCE
    # =========================================================
    if data["Confidence"]:
        data["Overall Confidence"] = round(sum(data["Confidence"].values()) / len(data["Confidence"]) * 100, 2)
    else:
        data["Overall Confidence"] = 0

    # =========================================================
    # FINAL CONVERGENCE FIX (DO NOT MOVE)
    # =========================================================

    # Recover GST amounts if present in text but not extracted
    if data.get("GST Number"):
        if data["CGST Amount"] == 0:
            m = re.search(
                r"CGST\s*\d*\s*\(?\d+(\.\d+)?%\)?\s+(\d{1,3}(?:,\d{3})*\.\d{2})",
                text
            )
            if m:
                data["CGST Amount"] = float(m.group(2).replace(",", ""))
                data["Confidence"]["CGST Amount"] = 0.95

        if data["SGST Amount"] == 0:
            m = re.search(
                r"SGST\s*\d*\s*\(?\d+(\.\d+)?%\)?\s+(\d{1,3}(?:,\d{3})*\.\d{2})",
                text
            )
            if m:
                data["SGST Amount"] = float(m.group(2).replace(",", ""))
                data["Confidence"]["SGST Amount"] = 0.95

    # Recover Final Amount from simple TOTAL line (Total ₹17,676.00)
    if data.get("Final Amount") in [None, 0]:
        m = re.search(
            r"\bTOTAL\b[^\d]{0,10}(₹)?\s*(\d{1,3}(?:,\d{3})*\.\d{2})",
            text
        )
        if m:
            data["Final Amount"] = float(m.group(2).replace(",", ""))
            data["Confidence"]["Final Amount"] = 0.95

    # Absolute accounting fallback (GST applied)
    if (
        data.get("Final Amount") in [None, 0]
        and data.get("Taxable Amount")
        and (data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]) > 0
    ):
        data["Final Amount"] = round(
            data["Taxable Amount"]
            + data["CGST Amount"]
            + data["SGST Amount"]
            + data["IGST Amount"], 2
        )
        data["Confidence"]["Final Amount"] = 0.90

    # =========================================================
    # FINAL HARD GUARANTEE (DO NOT MOVE)
    # Ensures Final Amount is NEVER lost by later fallbacks
    # =========================================================
    if data.get("Final Amount") in [None, 0]:
        money_vals = []

        for m in re.findall(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b", text):
            try:
                money_vals.append(float(m.replace(",", "")))
            except:
                pass

        # Take the maximum realistic amount as Final Amount
        if money_vals:
            data["Final Amount"] = max(money_vals)
            data["Confidence"]["Final Amount"] = 0.90

    print(
        data["Taxable Amount"],
        data["CGST Amount"],
        data["SGST Amount"],
        data["Final Amount"]
    )

    # =========================================================
    # FINAL INVOICE DETAILS RECOVERY (SAFE & NON-DESTRUCTIVE)
    # =========================================================

    # 1. Recover Taxable Amount from Sub Total
    if data.get("Taxable Amount") is None and data.get("Sub Total"):
        data["Taxable Amount"] = data["Sub Total"]
        data["Confidence"]["Taxable Amount"] = 0.90

    # 2. Recover GST amounts if GST invoice but amounts missing
    if data.get("GST Number"):
        if data["CGST Amount"] == 0:
            m = re.search(r"CGST[^\d]*(\d{1,3}(?:,\d{3})*\.\d{2})", text)
            if m:
                data["CGST Amount"] = float(m.group(1).replace(",", ""))
                data["Confidence"]["CGST Amount"] = 0.90

        if data["SGST Amount"] == 0:
            m = re.search(r"SGST[^\d]*(\d{1,3}(?:,\d{3})*\.\d{2})", text)
            if m:
                data["SGST Amount"] = float(m.group(1).replace(",", ""))
                data["Confidence"]["SGST Amount"] = 0.90

        if data["IGST Amount"] == 0:
            m = re.search(r"IGST[^\d]*(\d{1,3}(?:,\d{3})*\.\d{2})", text)
            if m:
                data["IGST Amount"] = float(m.group(1).replace(",", ""))
                data["Confidence"]["IGST Amount"] = 0.90

    # 3. Recover Final Amount from TOTAL line
    if data.get("Final Amount") in [None, 0]:
        m = re.search(r"\bTOTAL\b[^\d]*(\d{1,3}(?:,\d{3})*\.\d{2})", text)
        if m:
            data["Final Amount"] = float(m.group(1).replace(",", ""))
            data["Confidence"]["Final Amount"] = 0.90

    # 4. Absolute fallback – compute final if GST present
    if (
        data.get("Final Amount") in [None, 0]
        and data.get("Taxable Amount")
        and (data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]) > 0
    ):
        data["Final Amount"] = round(
            data["Taxable Amount"]
            + data["CGST Amount"]
            + data["SGST Amount"]
            + data["IGST Amount"], 2
        )
        data["Confidence"]["Final Amount"] = 0.85

    # 5. Last-resort safety: take largest monetary value
    if data.get("Final Amount") in [None, 0]:
        amounts = []
        for v in re.findall(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b", text):
            try:
                amounts.append(float(v.replace(",", "")))
            except:
                pass
        if amounts:
            data["Final Amount"] = max(amounts)
            data["Confidence"]["Final Amount"] = 0.80

    # =========================================================
    # FINAL GST RECOVERY (FORMAT: CGST9 (9%) 1,348.20)
    # Non-destructive: runs only if GST amount is still 0
    # =========================================================
    if data.get("GST Number"):
        if data["CGST Amount"] == 0:
            m = re.search(
                r"CGST\s*\d*\s*\(?\d+(\.\d+)?%\)?\s+(\d{1,3}(?:,\d{3})*\.\d{2})",
                text
            )
            if m:
                data["CGST Amount"] = float(m.group(2).replace(",", ""))
                data["Confidence"]["CGST Amount"] = 0.95

        if data["SGST Amount"] == 0:
            m = re.search(
                r"SGST\s*\d*\s*\(?\d+(\.\d+)?%\)?\s+(\d{1,3}(?:,\d{3})*\.\d{2})",
                text
            )
            if m:
                data["SGST Amount"] = float(m.group(2).replace(",", ""))
                data["Confidence"]["SGST Amount"] = 0.95

    # =========================================================
    # FINAL AUTHORITATIVE FIX (DO NOT MOVE)
    # =========================================================

    # 1. Recover GST amounts (comma-free, normalized text)
    if data.get("GST Number"):
        if data["CGST Amount"] == 0:
            m = re.search(r"CGST\s*\d*\s*\(?\d+(\.\d+)?%\)?\s+(\d+\.\d{2})", text)
            if m:
                data["CGST Amount"] = float(m.group(2))
                data["Confidence"]["CGST Amount"] = 0.95

        if data["SGST Amount"] == 0:
            m = re.search(r"SGST\s*\d*\s*\(?\d+(\.\d+)?%\)?\s+(\d+\.\d{2})", text)
            if m:
                data["SGST Amount"] = float(m.group(2))
                data["Confidence"]["SGST Amount"] = 0.95

    # 2. Recover Final Amount from TOTAL (normalized text)
    if data.get("Final Amount") in [None, 0]:
        m = re.search(r"\bTOTAL\b[^\d]*(\d+\.\d{2})", text)
        if m:
            data["Final Amount"] = float(m.group(1))
            data["Confidence"]["Final Amount"] = 0.95

    # 3. Accounting truth fallback (GST applied)
    if (
        data.get("Final Amount") in [None, 0]
        and data.get("Taxable Amount")
        and (data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]) > 0
    ):
        data["Final Amount"] = round(
            data["Taxable Amount"]
            + data["CGST Amount"]
            + data["SGST Amount"]
            + data["IGST Amount"], 2
        )
        data["Confidence"]["Final Amount"] = 0.95
    # =========================================================
    # FINAL GST OVERRIDE (AUTHORITATIVE)
    # If GST exists and Final == Taxable, recompute Final
    # =========================================================
    if (
        data.get("Taxable Amount") is not None
        and (data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]) > 0
        and data.get("Final Amount") == data.get("Taxable Amount")
    ):
        data["Final Amount"] = round(
            data["Taxable Amount"]
            + data["CGST Amount"]
            + data["SGST Amount"]
            + data["IGST Amount"], 2
        )
        data["Confidence"]["Final Amount"] = 0.95

    # =========================================================
    # FINAL TAXABLE AMOUNT CORRECTION (HSN SAFE)
    # =========================================================
    if data.get("Taxable Amount") and data["Taxable Amount"] > 1_000_000:
        # Likely picked an HSN or code by mistake
        money_vals = []

        for m in re.findall(r"\b\d+\.\d{2}\b", text):
            try:
                money_vals.append(float(m))
            except:
                pass

        if money_vals:
            data["Taxable Amount"] = max(money_vals)
            data["Sub Total"] = data["Taxable Amount"]
            data["Confidence"]["Taxable Amount"] = 0.95
    # =========================================================
    # FINAL TAXABLE CORRECTION (GST APPLIED CASE)
    # If taxable equals final but GST exists, recompute taxable
    # =========================================================
    if (
        data.get("Final Amount") is not None
        and data.get("Taxable Amount") == data.get("Final Amount")
        and (data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]) > 0
    ):
        data["Taxable Amount"] = round(
            data["Final Amount"]
            - (data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]),
            2
        )
        data["Sub Total"] = data["Taxable Amount"]
        data["Confidence"]["Taxable Amount"] = 0.95
    # =========================================================
    # FINAL SERVICE / B2C INVOICE FIX (HSN SAFE)
    # =========================================================
    if (
        data.get("Final Amount") is not None
        and (data["CGST Amount"] + data["SGST Amount"] + data["IGST Amount"]) == 0
        and data.get("Taxable Amount")
        and data["Taxable Amount"] > data["Final Amount"] * 10
    ):
        # Likely HSN/SAC picked as taxable in B2C invoice
        data["Taxable Amount"] = data["Final Amount"]
        data["Sub Total"] = data["Final Amount"]
        data["Confidence"]["Taxable Amount"] = 0.95
    # =========================================================
    # FINAL TAXABLE RECOVERY (SINGLE LINE-ITEM INVOICE)
    # =========================================================
    if data.get("Taxable Amount") is None:
        money_vals = []

        for m in re.findall(r"\b\d+\.\d{2}\b", text):
            try:
                val = float(m)
                # Ignore GST lines and very small values
                if val >= 1000:
                    money_vals.append(val)
            except:
                pass

        if money_vals:
            # Taxable is the largest non-tax amount before GST
            taxable_val = max(money_vals)

            # Safety: must be less than final amount if final exists
            if not data.get("Final Amount") or taxable_val < data["Final Amount"]:
                data["Taxable Amount"] = taxable_val
                data["Sub Total"] = taxable_val
                data["Confidence"]["Taxable Amount"] = 0.95

    # =========================================================
    # FINAL GST AMOUNT RECOVERY (INDUSTRIAL INVOICE SAFE)
    # =========================================================
    if (
        data.get("Taxable Amount")
        and data["CGST Amount"] == 0
        and data["SGST Amount"] == 0
    ):
        # Look for standalone GST values (e.g. 720.00 720.00)
        gst_vals = []

        for m in re.findall(r"\b\d+\.\d{2}\b", text):
            try:
                val = float(m)
                # GST is usually 5%–18% of taxable
                if 0.05 * data["Taxable Amount"] <= val <= 0.20 * data["Taxable Amount"]:
                    gst_vals.append(val)
            except:
                pass

        if len(gst_vals) >= 2:
            # Assume equal CGST & SGST
            gst_vals.sort()
            data["CGST Amount"] = gst_vals[-1]
            data["SGST Amount"] = gst_vals[-1]
            data["Confidence"]["CGST Amount"] = 0.95
            data["Confidence"]["SGST Amount"] = 0.95

    data["_rules_applied"] = applied_rules

    return data






