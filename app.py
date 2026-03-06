import streamlit as st
import os
import zipfile
import tempfile

from main import process_invoice
from license_manager import is_license_valid
from confidence_utils import confidence_label
from batch_excel_writer import write_batch_summary


# -------------------------------
# Page config (must be first Streamlit call)
# -------------------------------
st.set_page_config(page_title="Invoice Automation", layout="centered")
st.markdown("""
<style>
    .block-container {
        padding-top: 2rem;
    }
    div[data-testid="metric-container"] {
        background-color: #111827;
        padding: 15px;
        border-radius: 12px;
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)


# -------------------------------
# License Check (CRITICAL)
# -------------------------------
valid, message = is_license_valid()

if not valid:
    st.error(f"❌ {message}")
    st.stop()

if "expire" in message.lower():
    st.warning(f"⚠️ {message}")


# -------------------------------
# App folders
# -------------------------------
os.makedirs("samples", exist_ok=True)
os.makedirs("output", exist_ok=True)


# -------------------------------
# UI Header
# -------------------------------
st.title("GST SmartCheck")
st.caption("GST Invoice Validation & Excel Automation" "Version: v1.0.0" )


# -------------------------------
# Sidebar
# -------------------------------
with st.sidebar:
    st.header("ℹ️ About")
    st.write("""
    **GST Invoice Automation Tool**

    Designed for:
    - Chartered Accountants
    - Audit Firms
    - Accounts Teams

    Features:
    - Accurate GST extraction
    - Smart tax validation
    - Excel-ready output
    - Batch invoice processing
    """)
    st.markdown("---")
    st.write("📧 Support: support@yourcompany.com")


# -------------------------------
# Upload Inputs
# -------------------------------
uploaded_file = st.file_uploader(
    "📄 Drag & Drop GST Invoice PDF here",
    type=["pdf"]
)

uploaded_zip = st.file_uploader(
    "📦 Upload ZIP (Multiple GST Invoices)",
    type=["zip"]
)


# =====================================================
# ZIP / BATCH MODE
# =====================================================
if uploaded_zip:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "batch.zip")

        with open(zip_path, "wb") as f:
            f.write(uploaded_zip.getbuffer())

        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)

        results = []

        for root, _, files in os.walk(tmp):
            for file in files:
                if file.lower().endswith(".pdf"):
                    pdf = os.path.join(root, file)
                    out = os.path.join("output", file.replace(".pdf", ".xlsx"))

                    try:
                        data, status = process_invoice(pdf, out, source_file_name=file)

                        results.append({
                            "Invoice": file,
                            "Status": status,
                            "Final Amount": data.get("Final Amount"),
                            "Rules Applied": ", ".join(data.get("_rules_applied", []))
                        })

                    except Exception as e:
                        results.append({
                            "Invoice": file,
                            "Status": "FAILED",
                            "Final Amount": None,
                            "Rules Applied": str(e)
                        })

        st.info(f"📄 Total invoices processed: {len(results)}")

        st.subheader("📦 Batch Processing Summary")

        if results:
            st.table(results)

            batch_output = os.path.join("output", "batch_summary.xlsx")
            write_batch_summary(results, batch_output)

            with open(batch_output, "rb") as f:
                batch_excel_bytes = f.read()

            st.download_button(
                label="⬇ Download Batch Summary (Excel)",
                data=batch_excel_bytes,
                file_name="batch_summary.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="download_batch_summary"
            )
        else:
            st.warning("⚠️ No PDF invoices found inside the ZIP file.")


# =====================================================
# SINGLE INVOICE MODE
# =====================================================
if uploaded_file:
    pdf_path = os.path.join("samples", uploaded_file.name)

    with open(pdf_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    base_name = os.path.splitext(uploaded_file.name)[0]
    output_file = os.path.join("output", base_name + ".xlsx")

    try:
        progress = st.progress(0)
        st.info("🔍 Reading invoice...")
        progress.progress(30)

        data, status = process_invoice(pdf_path, output_file, source_file_name=uploaded_file.name)

        progress.progress(80)
        st.info("📊 Generating Excel report...")
        progress.progress(100)

        # ===============================
        # LOAD EXCEL FILE INTO MEMORY (FIX FOR DOWNLOAD)
        # ===============================
        excel_bytes = None
        if os.path.exists(output_file):
            with open(output_file, "rb") as f:
                excel_bytes = f.read()

        # ===============================
        # PREPARE CONFIDENCE DATA (SAFE)
        # ===============================
        confidence_rows = []

        if "Confidence" in data:
            for field, score in data["Confidence"].items():
                confidence_rows.append({
                    "Field": field,
                    "Confidence": f"{int(score * 100)}%",
                    "Status": confidence_label(score)
                })

        # ===============================
        # SUMMARY CARDS (TOP VIEW)
        # ===============================
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.metric("📄 Invoice No", data.get("Invoice Number", "-"))

        with col2:
            st.metric("💰 Taxable (₹)", f"{data.get('Taxable Amount', 0):,.2f}")

        with col3:
            gst_total = (
                    data.get("CGST Amount", 0)
                    + data.get("SGST Amount", 0)
                    + data.get("IGST Amount", 0)
            )
            st.metric("🧾 GST (₹)", f"{gst_total:,.2f}")

        with col4:
            st.metric("✅ Final (₹)", f"{data.get('Final Amount', 0):,.2f}")

        # -------------------------------
        # Extracted Data
        # -------------------------------

        tab1, tab2, tab3 = st.tabs([
            "📄 Invoice Details",
            "📊 Confidence",
            "⬇ Export"
        ])



        display_rows = []
        for k, v in data.items():
            if k not in ["Confidence", "_rules_applied", "_audit"]:
                display_rows.append({"Field": k, "Value": v})

        with tab1:
            with st.expander("📄 View Extracted Invoice Data", expanded=True):
                st.table(display_rows)

        # -------------------------------
        # Confidence Levels
        # -------------------------------
        with tab2:
            st.subheader("📊 Confidence Levels")

            if confidence_rows:
                st.table(confidence_rows)
            else:
                st.info("No confidence data available.")

        with tab3:
            st.markdown("### 📦 Export Invoice")
            st.caption("Export only after reviewing validation status")

            if excel_bytes:
                st.download_button(
                    label="⬇ Download Excel",
                    data=excel_bytes,
                    file_name=base_name + ".xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"download_single_{base_name}"
                )
            else:
                st.error("Excel file not ready for download.")

        # -------------------------------
        # ===============================
        # STATUS BADGE
        # ===============================
        st.subheader("🧾 Validation Status")

        if "VALID" in status:
            st.success("✅ Invoice is VALID – Safe to export")
        elif "MISSING" in status:
            st.warning("⚠️ Review Needed – Missing values detected")
        elif "MISMATCH" in status:
            st.error("❌ Error – Tax mismatch detected")
        else:
            st.info(status)

        # ===============================
        # ACTION GUIDANCE
        # ===============================
        if "VALID" in status:
            st.info("➡️ You can safely export this invoice for GST filing.")
        elif "MISMATCH" in status:
            st.info("➡️ Please verify invoice calculation before filing.")
        else:
            st.info("➡️ Review highlighted fields before export.")

        # -------------------------------
        # Time Saved Indicator
        # -------------------------------
        if data.get("Taxable Amount"):
            st.success("⏱ Estimated time saved: ~15 minutes compared to manual GST entry")

        # -------------------------------
        # GST Compliance Summary
        # -------------------------------
        st.markdown("""
        ### ✅ GST Compliance Summary
        • Invoice processed successfully  
        • GST values validated using business rules  
        • Ready for filing, audit, or accounting export
        """)

        # -------------------------------
        # Download Excel
        # -------------------------------

    except Exception as e:
        st.error("❌ Failed to process invoice. Please upload a clear digital GST invoice.")
        st.exception(e)


# -------------------------------
# Footer
# -------------------------------
st.markdown("---")
st.markdown(
    """
    <div style="text-align:center; color: gray; font-size: 13px;">
        © 2026 <b>Invoice Automation</b> • Built for CA Firms • All Rights Reserved
    </div>
    """,
    unsafe_allow_html=True
)
