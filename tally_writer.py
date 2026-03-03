from __future__ import annotations

from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_tally_date(raw_date: str) -> str:
    """Convert common invoice date formats into Tally's YYYYMMDD format."""
    text = str(raw_date or "").strip()
    if not text:
        return ""

    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            continue

    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8]


def build_tally_voucher_xml(invoice_data: dict) -> str:
    """Map extracted invoice fields into a Tally Sales <VOUCHER> XML payload."""
    invoice_number = str(invoice_data.get("Invoice Number") or invoice_data.get("Number") or "")
    invoice_date = _format_tally_date(invoice_data.get("Invoice Date") or invoice_data.get("Date"))
    gstin = str(invoice_data.get("Vendor GSTIN") or invoice_data.get("GSTIN") or "")

    total_amount = _to_float(invoice_data.get("Final Amount") or invoice_data.get("Total"))
    taxable_amount = _to_float(invoice_data.get("Taxable Amount"))
    cgst_amount = _to_float(invoice_data.get("CGST Amount"))
    sgst_amount = _to_float(invoice_data.get("SGST Amount"))
    igst_amount = _to_float(invoice_data.get("IGST Amount"))

    tax_amount = cgst_amount + sgst_amount + igst_amount
    if taxable_amount <= 0 and total_amount > 0:
        taxable_amount = round(total_amount - tax_amount, 2)

    envelope = Element("ENVELOPE")
    header = SubElement(envelope, "HEADER")
    SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = SubElement(envelope, "BODY")
    import_data = SubElement(body, "IMPORTDATA")
    request_desc = SubElement(import_data, "REQUESTDESC")
    SubElement(request_desc, "REPORTNAME").text = "Vouchers"

    request_data = SubElement(import_data, "REQUESTDATA")
    tally_message = SubElement(request_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})

    voucher = SubElement(
        tally_message,
        "VOUCHER",
        {
            "VCHTYPE": "Sales",
            "ACTION": "Create",
            "OBJVIEW": "Invoice Voucher View",
        },
    )

    SubElement(voucher, "DATE").text = invoice_date
    SubElement(voucher, "VOUCHERTYPENAME").text = "Sales"
    SubElement(voucher, "VOUCHERNUMBER").text = invoice_number
    SubElement(voucher, "PARTYGSTIN").text = gstin
    SubElement(voucher, "NARRATION").text = "Imported by GST SmartCheck"

    party_entry = SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    SubElement(party_entry, "LEDGERNAME").text = "Sundry Debtors"
    SubElement(party_entry, "ISDEEMEDPOSITIVE").text = "Yes"
    SubElement(party_entry, "AMOUNT").text = f"-{total_amount:.2f}"

    sales_entry = SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    SubElement(sales_entry, "LEDGERNAME").text = "Sales"
    SubElement(sales_entry, "ISDEEMEDPOSITIVE").text = "No"
    SubElement(sales_entry, "AMOUNT").text = f"{taxable_amount:.2f}"

    if cgst_amount:
        cgst_entry = SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        SubElement(cgst_entry, "LEDGERNAME").text = "Output CGST"
        SubElement(cgst_entry, "ISDEEMEDPOSITIVE").text = "No"
        SubElement(cgst_entry, "AMOUNT").text = f"{cgst_amount:.2f}"

    if sgst_amount:
        sgst_entry = SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        SubElement(sgst_entry, "LEDGERNAME").text = "Output SGST"
        SubElement(sgst_entry, "ISDEEMEDPOSITIVE").text = "No"
        SubElement(sgst_entry, "AMOUNT").text = f"{sgst_amount:.2f}"

    if igst_amount:
        igst_entry = SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        SubElement(igst_entry, "LEDGERNAME").text = "Output IGST"
        SubElement(igst_entry, "ISDEEMEDPOSITIVE").text = "No"
        SubElement(igst_entry, "AMOUNT").text = f"{igst_amount:.2f}"

    xml_bytes = tostring(envelope, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")
