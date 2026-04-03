from ai_extractor import extract_invoice_fields


def test_igst_not_extracted_from_msme_identifier():
    text = """
    Total In Words
    Indian Rupee Seventeen Thousand Six Hundred Seventy-Six Only
    Sub Total 14,980.00
    CGST9 (9%) 1,348.20
    SGST9 (9%) 1,348.20
    Round Off (-) 0.40
    Total ₹17,676.00
    Notes
    SUPPLY MEANT FOR SEZ/SEZ DEVELOPER UNDER BOND WITHOUT PAYMENT OF IGST MSME NO:UDYAM-TN-02-0123580
    """

    data = extract_invoice_fields(text)

    assert data["IGST Amount"] == 0.0
    assert data["CGST Amount"] == 1348.2
    assert data["SGST Amount"] == 1348.2
