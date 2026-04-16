def validate_invoice(data):
    if not data:
        return "INVALID DATA"

    if data.get("Invoice Type") == "Non-GST Invoice" or data.get("Validation") == "Non GST Invoice":
        return "Non GST Invoice"

    if data.get("Requires Manual Review"):
        return "REQUIRES MANUAL REVIEW"

    final_amount = data.get("Final Amount")
    if not final_amount:
        return "FINAL AMOUNT MISSING"

    if data.get("Is GST Invoice"):
        gst_total = (
            data.get("CGST Amount", 0)
            + data.get("SGST Amount", 0)
            + data.get("IGST Amount", 0)
        )

        if gst_total == 0:
            return "GST DETECTED BUT TAX AMOUNT MISSING"

        taxable = (
            data.get("Sub Total")
            or data.get("Taxable Amount")
            or 0
        )

        expected = round(taxable + gst_total, 2)

        if abs(expected - final_amount) > 2:
            return f"TAX MISMATCH (Expected {expected}, Found {final_amount})"

        return "VALID"

    return "VALID (NON-GST)"
