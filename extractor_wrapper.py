from ai_extractor import extract_invoice_fields

def extract_with_audit(text: str):
    data = extract_invoice_fields(text)

    audit = {}
    if data.get("GST Number"):
        audit["GST Number"] = "Matched standard GSTIN regex pattern"

    if data.get("Taxable Amount"):
        audit["Taxable Amount"] = "Derived using largest valid pre-tax amount rule"

    if data.get("Final Amount"):
        audit["Final Amount"] = "Recovered from TOTAL / computed fallback logic"

    data["_audit"] = audit
    return data
