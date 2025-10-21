import streamlit as st
import pandas as pd
import re
from io import BytesIO

st.set_page_config(page_title="Auto Validation Rules Generator", layout="wide")

st.title("📊 Auto Validation Rules + Failed Checks Generator")

# --- File uploads ---
raw_data_file = st.file_uploader("Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx"])
skip_file = st.file_uploader("Upload skip rules (CSV or XLSX) — optional", type=["csv", "xlsx"])
constructed_file = st.file_uploader("Upload Constructed List export (text file) — optional", type=["txt"])

# --- Helper Functions ---
def detect_variable_type(series):
    """Detect type of variable (rating, single-select, numeric, open-end, multi-select)"""
    unique = series.dropna().unique()
    if series.dtype.kind in "if" and len(unique) > 0:
        max_val = series.max()
        min_val = series.min()
        if 1 <= min_val <= 5 and max_val <= 5:
            return "rating"
        elif set(unique).issubset({0, 1}):
            return "multi-select"
        else:
            return "numeric"
    elif series.dtype == "object":
        if all(isinstance(x, str) and len(x) > 20 for x in series.dropna()):
            return "open-end"
        return "single-select"
    return "unknown"


def read_file(file):
    """Read CSV/XLSX generically"""
    if file is None:
        return None
    try:
        if file.name.endswith(".csv"):
            return pd.read_csv(file)
        elif file.name.endswith(".xlsx"):
            return pd.read_excel(file)
    except Exception as e:
        st.error(f"Error reading {file.name}: {e}")
        return None


def parse_constructed_lists(text):
    """Extract constructed list logic ranges (like ADD(ParentListName(),1,5))"""
    logic_ranges = {}
    matches = re.findall(r"List Name:\s*(\S+).*?\[Logic\]:\s*ADD\(.*?,(\d+),(\d+)\)", text, re.S | re.I)
    for name, start, end in matches:
        logic_ranges[name.strip()] = f"{start}-{end}"
    return logic_ranges


def generate_rules(data, skips=None, constructed_text=None):
    rules = []

    # --- Parse constructed lists if provided ---
    constructed_ranges = {}
    if constructed_text:
        constructed_ranges = parse_constructed_lists(constructed_text)

    # --- Loop through variables in raw data ---
    for col in data.columns:
        var_type = detect_variable_type(data[col])
        check_type = ""
        condition = ""

        # --- Rule logic ---
        if var_type == "rating":
            range_str = constructed_ranges.get(col, "1-5")
            check_type = "Range;Skip"
            condition = f"{range_str};If Segment_7=1 then {col} should be answered"

        elif var_type == "single-select":
            check_type = "Range;Skip"
            condition = f"1-5;If Segment_7=1 then {col} should be answered"

        elif var_type == "multi-select":
            check_type = "Multi-Select"
            condition = "Only 0/1; At least one selected"

        elif var_type == "numeric":
            check_type = "Range"
            condition = "Check for valid numeric range"

        elif var_type == "open-end":
            check_type = "Missing;OpenEnd_Junk"
            condition = "MinLen(3)"

        else:
            check_type = "Missing"
            condition = "Should not be blank"

        # --- Add skip rules if provided ---
        if skips is not None and col in skips["Question"].values:
            skip_cond = skips.loc[skips["Question"] == col, "Condition"].values
            if len(skip_cond) > 0:
                check_type += ";Skip"
                condition += f";{skip_cond[0]}"

        rules.append({
            "Question": col,
            "Check_Type": check_type,
            "Condition": condition
        })

    return pd.DataFrame(rules)


def to_excel(df: pd.DataFrame) -> BytesIO:
    """Convert DataFrame to Excel file in-memory."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Validation Rules")
    output.seek(0)
    return output


# --- Generate rules ---
if raw_data_file:
    data = read_file(raw_data_file)
    skips = read_file(skip_file)
    constructed_text = constructed_file.read().decode("utf-8") if constructed_file else None

    st.success(f"✅ Loaded raw data with {data.shape[1]} variables and {data.shape[0]} records")

    rules_df = generate_rules(data, skips, constructed_text)

    st.subheader("🧾 Preview: Generated Validation Rules (Editable)")
    edited_rules = st.data_editor(rules_df, num_rows="dynamic", use_container_width=True, key="editor")

    # --- Download Section ---
    st.markdown("### 💾 Download Validation Rules")
    excel_data = to_excel(edited_rules)

    st.download_button(
        label="📥 Download Edited Validation Rules (Excel)",
        data=excel_data,
        file_name="validation_rules.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

else:
    st.info("👆 Please upload raw survey data to start.")
