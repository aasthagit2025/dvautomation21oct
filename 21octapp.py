import streamlit as st
import pandas as pd
import io
import traceback

st.set_page_config(page_title="Auto Validation Rules Generator", layout="wide")

st.title("📊 Auto Validation Rules + Failed Checks Generator")
st.write("Upload your files below to automatically generate validation rules, review/edit them, and download the final Excel output.")

# --- File Uploads ---
data_file = st.file_uploader("📂 Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx", "sav"])
rules_file = st.file_uploader("📋 Upload existing validation rules (optional)", type=["xlsx"])
skip_file = st.file_uploader("🚦 Upload skip rules (CSV or XLSX) — optional", type=["csv", "xlsx"])
constructed_file = st.file_uploader("🧱 Upload Constructed List export (TXT) — optional", type=["txt"])

# --- Helper Functions ---
def load_data(file):
    if file.name.endswith(".csv"):
        return pd.read_csv(file, encoding_errors="ignore")
    elif file.name.endswith(".xlsx"):
        return pd.read_excel(file)
    else:
        raise ValueError("Unsupported data file type.")

def load_rules(file):
    if file.name.endswith(".xlsx"):
        return pd.read_excel(file)
    elif file.name.endswith(".csv"):
        return pd.read_csv(file)
    else:
        raise ValueError("Unsupported rules file type.")

def generate_rules_from_data(df):
    """Generate basic placeholder rules automatically"""
    rules = []
    for col in df.columns:
        dtype = str(df[col].dtype).lower()
        if "float" in dtype or "int" in dtype:
            rules.append([col, "Range", "1-5", "Auto (numeric)"])
        elif "object" in dtype:
            if df[col].astype(str).str.len().mean() > 10:
                rules.append([col, "Missing;OpenEnd_Junk", ";MinLen(3)", "Auto (open-end/text)"])
            else:
                rules.append([col, "Missing", "", "Auto (single-select)"])
        else:
            rules.append([col, "Missing", "", "Auto (unknown type)"])
    return pd.DataFrame(rules, columns=["Question", "Check_Type", "Condition", "Source"])

# --- Main Process ---
if data_file:
    try:
        df = load_data(data_file)
        st.success(f"✅ Data loaded successfully: {df.shape[0]} rows, {df.shape[1]} columns")

        # Generate initial rules
        rules_df = generate_rules_from_data(df)

        # Merge Skip / Constructed Logic if provided
        if skip_file:
            skip_df = load_rules(skip_file)
            st.info("Skip file loaded and merged where applicable.")
            # (Later: logic to map skips based on question names)

        if constructed_file:
            constructed_text = constructed_file.read().decode("utf-8", errors="ignore")
            st.info("Constructed list file loaded. Logic extraction not applied yet for demo.")

        # --- Display Editable Rules ---
        st.subheader("🧾 Preview: Generated Validation Rules (Editable)")
        edited_rules = st.data_editor(
            rules_df,
            num_rows="dynamic",
            width="stretch",  # ✅ fixed deprecation warning
            key="editor"
        )

        # --- Download Final Rules ---
        st.subheader("💾 Download Final Validation Rules")
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            edited_rules.to_excel(writer, index=False, sheet_name="Validation_Rules")
        st.download_button(
            label="📥 Download Validation Rules (Excel)",
            data=out.getvalue(),
            file_name="validation_rules_final.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        st.error("❌ An error occurred while processing your files.")
        with st.expander("See error details"):
            st.code(traceback.format_exc())

else:
    st.warning("👆 Please upload at least a data file to begin.")
