import xml.etree.ElementTree as ET
from datetime import datetime
import os

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from ai_extractor import validate_gstin_checksum


EXCEL_COLUMNS = [
    "Source File Name",
    "Vendor Name",
    "Vendor GSTIN",
    "Invoice Number",
    "Invoice Date",
    "Taxable Amount",
    "CGST Amount",
    "SGST Amount",
    "IGST Amount",
    "Total Tax",
    "Final Amount",
    "Invoice Type",
    "Validation Status",
    "Confidence Score",
    "Rules Applied",
]

NUMERIC_COLUMNS = ["Taxable Amount", "CGST Amount", "SGST Amount", "IGST Amount", "Total Tax", "Final Amount", "Confidence Score"]
CURRENCY_COLUMNS = ["Taxable Amount", "CGST Amount", "SGST Amount", "IGST Amount", "Total Tax", "Final Amount"]

def _first_available(data, keys, default=None):
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _extract_confidence_score(data):
    confidence_data = data.get("Confidence")
    if isinstance(confidence_data, dict) and confidence_data:
        return round(sum(confidence_data.values()) / len(confidence_data) * 100, 2)
    return _first_available(data, ["Confidence Score", "Confidence"], default=None)


def _normalize_date(value):
    if value in (None, ""):
        return None

    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d")


def _prepare_row(data, status, source_file_name=None):
    gstin = str(_first_available(data, ["GST Number", "GSTIN", "Vendor GSTIN"], default="") or "").upper() or None
    taxable_value = _first_available(data, ["Taxable Amount", "Taxable Value"])
    cgst = _first_available(data, ["CGST Amount", "CGST"], default=0) or 0
    sgst = _first_available(data, ["SGST Amount", "SGST"], default=0) or 0
    igst = _first_available(data, ["IGST Amount", "IGST"], default=0) or 0
    final_amount = _first_available(data, ["Final Amount", "Total"])

    if not gstin and float(cgst or 0) == 0 and float(sgst or 0) == 0 and float(igst or 0) == 0:
        data["Invoice Type"] = "Non GST Invoice"
        if final_amount not in (None, ""):
            data["Taxable Amount"] = final_amount
            taxable_value = final_amount

    total_tax = _first_available(data, ["Total Tax Amount", "Total Tax"], default=None)
    if total_tax in (None, ""):
        total_tax = (float(cgst or 0) + float(sgst or 0) + float(igst or 0))

    row = {
        "Source File Name": os.path.basename(source_file_name or _first_available(data, ["Source File Name", "File Name", "Filename"], default="") or "") or None,
        "Vendor Name": _first_available(data, ["Vendor Name", "Supplier Name", "Party Name"]),
        "Vendor GSTIN": gstin,
        "Invoice Number": _first_available(data, ["Invoice Number", "Invoice No"]),
        "Invoice Date": _normalize_date(_first_available(data, ["Invoice Date", "Date"])),
        "Taxable Amount": taxable_value,
        "CGST Amount": cgst,
        "SGST Amount": sgst,
        "IGST Amount": igst,
        "Total Tax": total_tax,
        "Final Amount": final_amount,
        "Invoice Type": _first_available(data, ["Invoice Type"], default="GST Invoice"),
        "Validation Status": status,
        "Confidence Score": _extract_confidence_score(data),
        "Rules Applied": ", ".join(data.get("_rules_applied", [])) if isinstance(data.get("_rules_applied"), list) else _first_available(data, ["Rules Applied"], default=None),
    }

    frame = pd.DataFrame([row], columns=EXCEL_COLUMNS)

    for col in NUMERIC_COLUMNS:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    for col in CURRENCY_COLUMNS:
        frame[col] = frame[col].round(2)

    return frame


def write_to_excel(data, status, output_path, source_file_name=None):
    df = _prepare_row(data, status, source_file_name=source_file_name)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)

    wb = load_workbook(output_path)
    ws = wb.active

    header_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
    verified_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    mismatch_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    header_font = Font(bold=True)
    center_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"), top=Side(style="thin"), bottom=Side(style="thin")
    )

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    validation_col = EXCEL_COLUMNS.index("Validation Status") + 1
    gst_col = EXCEL_COLUMNS.index("Vendor GSTIN") + 1
    currency_col_indexes = [EXCEL_COLUMNS.index(col) + 1 for col in CURRENCY_COLUMNS]

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            cell.border = thin_border

        for col_idx in currency_col_indexes:
            row[col_idx - 1].number_format = "0.00"

        validation_cell = row[validation_col - 1]
        validation_value = str(validation_cell.value or "").strip().lower()
        validation_cell.fill = verified_fill if validation_value in {"verified", "success", "non gst invoice"} else mismatch_fill
        validation_cell.font = Font(bold=True)

        gst_cell = row[gst_col - 1]
        if gst_cell.value:
            gst_cell.value = str(gst_cell.value).upper()
            if not validate_gstin_checksum(str(gst_cell.value)):
                gst_cell.fill = mismatch_fill

    min_widths = {
        "Source File Name": 28,
        "Vendor Name": 22,
        "Vendor GSTIN": 18,
        "Invoice Number": 14,
        "Invoice Date": 14,
        "Taxable Amount": 14,
        "CGST Amount": 12,
        "SGST Amount": 12,
        "IGST Amount": 12,
        "Total Tax": 12,
        "Final Amount": 14,
        "Invoice Type": 16,
        "Validation Status": 24,
        "Confidence Score": 16,
        "Rules Applied": 28,
    }

    for col_idx in range(1, ws.max_column + 1):
        max_length = 0
        header = str(ws.cell(row=1, column=col_idx).value or "")
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        for row_idx in range(1, ws.max_row + 1):
            cell_value = ws.cell(row=row_idx, column=col_idx).value
            if cell_value is not None:
                max_length = max(max_length, len(str(cell_value)))
        ws.column_dimensions[col_letter].width = max(max_length + 3, min_widths.get(header, 12))

    try:
        wb.save(output_path)
    finally:
        wb.close()

    return bool(
        output_path
        and output_path.lower().endswith(".xlsx")
        and os.path.exists(output_path)
        and os.path.getsize(output_path) > 0
    )


def generate_tally_xml(data):
    row = _prepare_row(data, status=data.get("Validation Status", "Success")).iloc[0].to_dict()

    voucher_date = ""
    if row.get("Invoice Date"):
        voucher_date = pd.to_datetime(row["Invoice Date"]).strftime("%Y%m%d")

    taxable = float(row.get("Taxable Amount") or 0)
    cgst = float(row.get("CGST Amount") or 0)
    sgst = float(row.get("SGST Amount") or 0)
    igst = float(row.get("IGST Amount") or 0)
    total = float(row.get("Final Amount") or (taxable + cgst + sgst + igst))
    total_tax = round(cgst + sgst + igst, 2)

    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    request_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(request_desc, "REPORTNAME").text = "Vouchers"

    request_data = ET.SubElement(import_data, "REQUESTDATA")
    tally_message = ET.SubElement(request_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})

    voucher = ET.SubElement(
        tally_message,
        "VOUCHER",
        {"VCHTYPE": "Sales", "ACTION": "Create", "OBJVIEW": "Invoice Voucher View"},
    )

    ET.SubElement(voucher, "DATE").text = voucher_date
    ET.SubElement(voucher, "VOUCHERTYPENAME").text = "Sales"
    ET.SubElement(voucher, "PARTYGSTIN").text = str(row.get("Vendor GSTIN") or "")
    ET.SubElement(voucher, "NARRATION").text = f"GST SmartCheck import for invoice {row.get('Invoice Number') or ''}".strip()

    party_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    ET.SubElement(party_entry, "LEDGERNAME").text = "Sundry Debtors"
    ET.SubElement(party_entry, "ISDEEMEDPOSITIVE").text = "Yes"
    ET.SubElement(party_entry, "AMOUNT").text = f"-{total:.2f}"

    sales_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    ET.SubElement(sales_entry, "LEDGERNAME").text = "Sales"
    ET.SubElement(sales_entry, "ISDEEMEDPOSITIVE").text = "No"
    ET.SubElement(sales_entry, "AMOUNT").text = f"{taxable:.2f}"

    if total_tax > 0:
        tax_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(tax_entry, "LEDGERNAME").text = "Output GST"
        ET.SubElement(tax_entry, "ISDEEMEDPOSITIVE").text = "No"
        ET.SubElement(tax_entry, "AMOUNT").text = f"{total_tax:.2f}"

    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True).decode("utf-8")
