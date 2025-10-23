import streamlit as st
import pandas as pd
import io

st.set_page_config(page_title="Auto Validation Rules + Failed Checks Generator", layout="wide")
st.title("📊 Auto Validation Rules + Failed Checks Generator")

# --- FILE UPLOADS ---
raw_file = st.file_uploader("Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx"])
skip_file = st.file_uploader("Upload skip rules (CSV or XLSX) — optional", type=["csv", "xlsx"])
constructed_file = st.file_uploader("Upload Constructed List export (TXT) — optional", type=["txt"])

# --- FUNCTION: DETECT QUESTION TYPE ---
def detect_question_type(series):
    if series.dtype == 'O':
        unique = series.dropna().unique()
        if all(x in [0, 1, '0', '1'] for x in unique):
            return "Multi-Select"
        elif series.str.len().mean() > 20:
            return "Open-End"
        else:
            return "Single-Select"
    else:
        if series.dropna().between(1, 10).all():
            return "Rating"
        else:
            return "Numeric"

# --- FUNCTION: GENERATE RULES ---
def generate_rules(df, skips_df=None):
    rules = []
    for col in df.columns:
        q_type = detect_question_type(df[col])
        check_type, condition, source = "", "", "Auto"

        if q_type == "Rating":
            check_type = "Range;Straightliner"
            max_val = int(df[col].max())
            condition = f"1-{max_val};If answered, check straightlining"
        elif q_type == "Single-Select":
            check_type = "Range;Skip"
            max_val = int(df[col].max())
            condition = f"1-{max_val};If relevant skip logic true then should be answered"
        elif q_type == "Multi-Select":
            check_type = "Missing"
            condition = "At least one selected"
        elif q_type == "Open-End":
            check_type = "Missing;OpenEnd_Junk"
            condition = "MinLen(3)"
        else:
            check_type = "Range"
            max_val = int(df[col].max())
            condition = f"1-{max_val}"

        # Apply skips if available
        if skips_df is not None:
            skip_logic = skips_df[skips_df['Skip From'] == col]
            if not skip_logic.empty:
                skip_text = " + ".join(skip_logic['Logic'].fillna(''))
                if "Skip" not in check_type:
                    check_type += ";Skip"
                condition += f";If {skip_text}"

        rules.append({
            "Question": col,
            "Check_Type": check_type,
            "Condition": condition,
            "Source": source
        })

    return pd.DataFrame(rules)

# --- FUNCTION: VALIDATE DATA (FAILED CHECKS) ---
def run_validation(df, rules_df):
    failed_rows = []

    for _, rule in rules_df.iterrows():
        q = rule["Question"]
        if q not in df.columns:
            continue
        check = rule["Check_Type"]
        cond = rule["Condition"]

        series = df[q]

        # Check 1: Missing
        if "Missing" in check:
            fail_idx = series[series.isna()].index
            for i in fail_idx:
                failed_rows.append((df.loc[i, df.columns[0]], q, "Missing", "Blank but should be answered"))

        # Check 2: Range
        if "Range" in check and "-" in cond:
            try:
                rng = cond.split(";")[0]
                lo, hi = map(int, rng.split("-"))
                fail_idx = series[~series.between(lo, hi, inclusive="both") & series.notna()].index
                for i in fail_idx:
                    failed_rows.append((df.loc[i, df.columns[0]], q, "Range", f"Out of range ({rng})"))
            except:
                pass

    if failed_rows:
        return pd.DataFrame(failed_rows, columns=["RespondentID", "Question", "Check_Type", "Error"])
    else:
        return pd.DataFrame(columns=["RespondentID", "Question", "Check_Type", "Error"])

# --- MAIN LOGIC ---
if raw_file:
    if raw_file.name.endswith(".csv"):
        df = pd.read_csv(raw_file)
    else:
        df = pd.read_excel(raw_file)

    skips_df = None
    if skip_file:
        if skip_file.name.endswith(".csv"):
            skips_df = pd.read_csv(skip_file)
        else:
            skips_df = pd.read_excel(skip_file)

    st.subheader("Step 1: Generated Validation Rules")
    rules_df = generate_rules(df, skips_df)
    st.dataframe(rules_df.head(100), use_container_width=True)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        rules_df.to_excel(writer, index=False, sheet_name='Validation_Rules')
    st.download_button("⬇️ Download Validation Rules", data=buffer.getvalue(),
                       file_name="validation_rules.xlsx", mime="application/vnd.ms-excel")

    st.subheader("Step 2: Failed Checks (Validation Report)")
    report_df = run_validation(df, rules_df)
    st.dataframe(report_df.head(100), use_container_width=True)

    buffer2 = io.BytesIO()
    with pd.ExcelWriter(buffer2, engine='xlsxwriter') as writer:
        report_df.to_excel(writer, index=False, sheet_name='Failed_Checks')
    st.download_button("⬇️ Download Failed Checks Report", data=buffer2.getvalue(),
                       file_name="validation_report.xlsx", mime="application/vnd.ms-excel")
