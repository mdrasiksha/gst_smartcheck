from ai_extractor import _line_total_candidates, extract_invoice_fields


def test_line_total_candidates_skip_dot_separated_account_codes():
    line = "A/C Code 9100.310300   Sub A/C Code BA00013072-310300-9100"
    assert _line_total_candidates(line) == []


def test_non_gst_receipt_prefers_actual_total_over_account_code_fragments():
    text = """
    COLLECTION RECEIPT
    POLICY NO. 31030031240100037049  A/C CODE 9100.310300
    TOTAL = 1777.00
    """

    data = extract_invoice_fields(text)

    assert data["Final Amount"] == 1777.0
    assert data["Taxable Amount"] is None
