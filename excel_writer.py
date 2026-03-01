import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side


def write_to_excel(data, status, output_path):
    rows = []

    for k, v in data.items():
        if k != "Confidence":
            rows.append({"Field": k, "Value": v})

    rows.append({"Field": "Validation Status", "Value": status})

    df = pd.DataFrame(rows)

    # Write using pandas
    df.to_excel(output_path, index=False)

    # -----------------------------
    # STYLING SECTION (CA GRADE)
    # -----------------------------
    wb = load_workbook(output_path)
    ws = wb.active

    header_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
    header_font = Font(bold=True)
    center_align = Alignment(horizontal="center", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")

    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    # Style header
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    # Style all rows
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = left_align
            cell.border = thin_border

            # Highlight important fields
            # Highlight important fields
            if cell.column == 1 and cell.value in [
                "Final Amount",
                "Validation Status",
                "GST Number",
                "HSN Codes"
            ]:
                cell.font = Font(bold=True)

    # Auto column width
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        ws.column_dimensions[col_letter].width = max_length + 5

    # Highlight Validation Status row
    for row in ws.iter_rows():
        if row[0].value == "Validation Status":
            for cell in row:
                cell.fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
                cell.font = Font(bold=True)

    wb.save(output_path)

