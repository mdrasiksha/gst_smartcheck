import xml.etree.ElementTree as ET
from datetime import datetime
import os

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from ai_extractor import validate_gstin_checksum


EXCEL_COLUMNS = [
    "Invoice No",
    "Date",
    "GSTIN",
    "Taxable Value",
    "CGST",
    "SGST",
    "IGST",
    "Total",
    "Validation",
    "Validation Status",
    "Confidence Score",
    "Source File Name",
]

NUMERIC_COLUMNS = ["Taxable Value", "CGST", "SGST", "IGST", "Total", "Confidence Score"]


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


def _prepare_row(data, status):
    row = {
        "Invoice No": _first_available(data, ["Invoice Number", "Invoice No"]),
        "Date": _normalize_date(_first_available(data, ["Invoice Date", "Date"])),
        "GSTIN": str(_first_available(data, ["GST Number", "GSTIN"], default="") or "").upper() or None,
        "Taxable Value": _first_available(data, ["Taxable Amount", "Taxable Value"]),
        "CGST": _first_available(data, ["CGST Amount", "CGST"]),
        "SGST": _first_available(data, ["SGST Amount", "SGST"]),
        "IGST": _first_available(data, ["IGST Amount", "IGST"]),
        "Total": _first_available(data, ["Final Amount", "Total"]),
        "Validation": data.get("Validation", "Math Mismatch"),
        "Validation Status": status,
        "Confidence Score": _extract_confidence_score(data),
        "Source File Name": _first_available(data, ["Source File Name", "File Name", "Filename"]),
    }

    frame = pd.DataFrame([row], columns=EXCEL_COLUMNS)

    for col in NUMERIC_COLUMNS:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    return frame


def write_to_excel(data, status, output_path):
    df = _prepare_row(data, status)
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

    validation_col = EXCEL_COLUMNS.index("Validation") + 1
    gst_col = EXCEL_COLUMNS.index("GSTIN") + 1

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            cell.border = thin_border

        validation_cell = row[validation_col - 1]
        validation_value = str(validation_cell.value or "").strip().lower()
        validation_cell.fill = verified_fill if validation_value == "verified" else mismatch_fill
        validation_cell.font = Font(bold=True)

        gst_cell = row[gst_col - 1]
        if gst_cell.value:
            gst_cell.value = str(gst_cell.value).upper()
            if not validate_gstin_checksum(str(gst_cell.value)):
                gst_cell.fill = mismatch_fill

    for col_idx in range(1, ws.max_column + 1):
        max_length = 0
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        for row_idx in range(1, ws.max_row + 1):
            cell_value = ws.cell(row=row_idx, column=col_idx).value
            if cell_value is not None:
                max_length = max(max_length, len(str(cell_value)))
        ws.column_dimensions[col_letter].width = max_length + 3

    wb.save(output_path)
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
    if row.get("Date"):
        voucher_date = pd.to_datetime(row["Date"]).strftime("%Y%m%d")

    taxable = float(row.get("Taxable Value") or 0)
    cgst = float(row.get("CGST") or 0)
    sgst = float(row.get("SGST") or 0)
    igst = float(row.get("IGST") or 0)
    total = float(row.get("Total") or (taxable + cgst + sgst + igst))
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
    ET.SubElement(voucher, "PARTYGSTIN").text = str(row.get("GSTIN") or "")
    ET.SubElement(voucher, "NARRATION").text = f"GST SmartCheck import for invoice {row.get('Invoice No') or ''}".strip()

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
