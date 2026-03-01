import pandas as pd
import xml.etree.ElementTree as ET


def write_batch_summary(results, output_path):
    df = pd.DataFrame(results)
    df.to_excel(output_path, index=False)


def generate_tally_sales_xml(invoice_data: dict, output_path: str) -> str:
    """
    Generate Tally ERP 9 / Tally Prime compatible Sales voucher XML.

    Expected invoice_data keys include:
      - GSTIN
      - Date (DD-MM-YYYY or similar)
      - Total
      - Tax
    """
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
        {
            "VCHTYPE": "Sales",
            "ACTION": "Create",
            "OBJVIEW": "Invoice Voucher View",
        },
    )

    ET.SubElement(voucher, "DATE").text = str(invoice_data.get("Date", "")).replace("-", "")
    ET.SubElement(voucher, "VOUCHERTYPENAME").text = "Sales"
    ET.SubElement(voucher, "PARTYGSTIN").text = str(invoice_data.get("GSTIN", ""))
    ET.SubElement(voucher, "NARRATION").text = "Imported by GST SmartCheck"

    total = float(invoice_data.get("Total", 0) or 0)
    tax = float(invoice_data.get("Tax", 0) or 0)
    taxable = round(total - tax, 2)

    party_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    ET.SubElement(party_entry, "LEDGERNAME").text = "Sundry Debtors"
    ET.SubElement(party_entry, "ISDEEMEDPOSITIVE").text = "Yes"
    ET.SubElement(party_entry, "AMOUNT").text = f"-{total:.2f}"

    sales_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    ET.SubElement(sales_entry, "LEDGERNAME").text = "Sales"
    ET.SubElement(sales_entry, "ISDEEMEDPOSITIVE").text = "No"
    ET.SubElement(sales_entry, "AMOUNT").text = f"{taxable:.2f}"

    if tax > 0:
        tax_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(tax_entry, "LEDGERNAME").text = "Output GST"
        ET.SubElement(tax_entry, "ISDEEMEDPOSITIVE").text = "No"
        ET.SubElement(tax_entry, "AMOUNT").text = f"{tax:.2f}"

    tree = ET.ElementTree(envelope)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path
