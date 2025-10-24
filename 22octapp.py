import streamlit as st
import pandas as pd
import pyreadstat
import io
import csv
import re

st.set_page_config(page_title="Auto Validation Rules + Failed Checks", layout="wide")
st.title("📊 Auto Validation Rules + Failed Checks Generator")

# --- File Uploads ---
raw_file = st.file_uploader("Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx", "sav"])
skip_file = st.file_uploader("Upload skip rules (CSV or XLSX) — optional", type=["csv", "xlsx"])

# --- Helper: Auto-detect delimiter ---
def detect_delimiter(sample_bytes):
    try:
        sample = sample_bytes.decode('utf-8', errors='ignore')
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample)
        return dialect.delimiter
    except Exception:
        return ','

# --- Helper: Load Survey Data Safely ---
def load_survey_data(file):
    if file.name.endswith(".csv"):
        try:
            sample = file.read(4096)
            delimiter = detect_delimiter(sample)
            file.seek(0)
            return pd.read_csv(file, encoding="utf-8", sep=delimiter)
        except UnicodeDecodeError:
            file.seek(0)
            return pd.read_csv(file, encoding="ISO-8859-1", sep=delimiter)
    elif file.name.endswith(".xlsx"):
        return pd.read_excel(file)
    elif file.name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(file)
        return df
    else:
        st.error("Unsupported file format.")
        st.stop()

# --- Helper: Load Skip Rules ---
def load_skips(file):
    if file.name.endswith(".csv"):
        return pd.read_csv(file)
    elif file.name.endswith(".xlsx"):
        return pd.read_excel(file)
    else:
        return pd.DataFrame()

# --- Helper: Generate Auto Validation Rules ---
def generate_rules(df, skips_df):
    rules = []
    for col in df.columns:
        col_lower = col.lower()
        if col_lower in ["respondentid", "password", "sys_respnum"]:
            continue

        col_type = "text"
        unique_vals = df[col].dropna().unique()

        # Detect type
        if df[col].dtype in ["float64", "int64"]:
            col_type = "numeric"
        elif df[col].dtype == "object":
            if all(x in [0, 1, "0", "1", ""] for x in df[col].dropna().unique()):
                col_type = "multi-select"
            elif any(str(x).isdigit() for x in unique_vals):
                col_type = "single-select"
            elif any(re.search(r"[a-zA-Z]", str(x)) for x in unique_vals):
                col_type = "openend"

        # Build rules
        if col_type == "numeric" or col_type == "single-select":
            rules.append({
                "Question": col,
                "Check_Type": "Range;Missing",
                "Condition": "1-5;Value is missing",
                "Source": "Auto (numeric/single)"
            })
        elif col_type == "multi-select":
            rules.append({
                "Question": col,
                "Check_Type": "Multi-Select",
                "Condition": "Only 0/1; At least one selected",
                "Source": "Auto (multi-select)"
            })
        elif col_type == "openend":
            rules.append({
                "Question": col,
                "Check_Type": "Missing;OpenEnd_Junk",
                "Condition": ";MinLen(3)",
                "Source": "Auto (open-end/text)"
            })

        # Add straightliner for rating grids (e.g., Q1_r1, Q1_r2…)
        if re.search(r"_r\d+", col):
            rules.append({
                "Question": col,
                "Check_Type": "Range;Straightliner",
                "Condition": "1-5;Across grid items",
                "Source": "Auto (rating)"
            })

    # --- Integrate Skip Logic ---
    if not skips_df.empty:
        for _, row in skips_df.iterrows():
            try:
                q_from = str(row.get("Skip From", "")).strip()
                q_to = str(row.get("Skip To", "")).strip()
                logic = str(row.get("Logic", "")).strip()
                if q_from and q_to:
                    rules.append({
                        "Question": q_to,
                        "Check_Type": "Skip",
                        "Condition": f"If {logic} then {q_to} should be answered",
                        "Source": "Auto (skip)"
                    })
            except Exception:
                continue

    rules_df = pd.DataFrame(rules)
    return rules_df

# --- Helper: Validate Data (failed checks only) ---
def validate_data(df, rules_df):
    id_col = next((c for c in ["RespondentID", "Password", "sys_respnum"] if c in df.columns), None)
    if not id_col:
        id_col = df.columns[0]

    issues = []

    for _, r in rules_df.iterrows():
        q = r["Question"]
        check = r["Check_Type"]
        cond = r["Condition"]

        if q not in df.columns:
            continue

        # Missing
        if "missing" in check.lower():
            mask = df[q].isna() | (df[q].astype(str).str.strip().isin(["", "NA", "N/A", "None", "none"]))
            for rid in df.loc[mask, id_col]:
                issues.append({id_col: rid, "Question": q, "Check_Type": "Missing", "Issue": "Value is missing"})

        # Range
        if "range" in check.lower() and "-" in cond:
            try:
                parts = cond.split("-")
                low, high = float(parts[0]), float(parts[1].split(";")[0])
                mask = ~df[q].between(low, high)
                for rid in df.loc[mask, id_col]:
                    issues.append({id_col: rid, "Question": q, "Check_Type": "Range", "Issue": f"Value out of range ({low}-{high})"})
            except Exception:
                continue

        # Skip
        if "skip" in check.lower():
            if "if" in cond.lower() and "then" in cond.lower():
                parts = re.split(r'(?i)then', cond)
                if_part = parts[0].replace("if", "").strip()
                try:
                    col, val = re.split(r"[=<>]", if_part)[0].strip(), re.split(r"[=<>]", if_part)[1].strip()
                    if col in df.columns:
                        mask = df[col].astype(str).str.strip() == val
                        blank = df[q].isna() | (df[q].astype(str).str.strip() == "")
                        offenders = df.loc[mask & blank, id_col]
                        for rid in offenders:
                            issues.append({id_col: rid, "Question": q, "Check_Type": "Skip", "Issue": "Blank but should be answered"})
                except Exception:
                    continue

    return pd.DataFrame(issues)

# --- MAIN APP LOGIC ---
if raw_file:
    df = load_survey_data(raw_file)
    skips_df = load_skips(skip_file) if skip_file else pd.DataFrame()

    st.subheader("Step 1️⃣ — Auto Generate Validation Rules")
    rules_df = generate_rules(df, skips_df)
    st.dataframe(rules_df.head(200), use_container_width=True)

    out_rules = io.BytesIO()
    with pd.ExcelWriter(out_rules, engine="openpyxl") as writer:
        rules_df.to_excel(writer, index=False, sheet_name="Validation Rules")

    st.download_button(
        label="📥 Download Validation Rules",
        data=out_rules.getvalue(),
        file_name="validation_rules.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    st.subheader("Step 2️⃣ — Run Validation (Failed Checks Only)")
    if st.button("Run Validation"):
        failed_df = validate_data(df, rules_df)
        st.success(f"Validation complete — {len(failed_df)} failed checks found.")
        st.dataframe(failed_df, use_container_width=True)

        out_fail = io.BytesIO()
        with pd.ExcelWriter(out_fail, engine="openpyxl") as writer:
            failed_df.to_excel(writer, index=False, sheet_name="Failed Checks")

        st.download_button(
            label="📥 Download Failed Checks Report",
            data=out_fail.getvalue(),
            file_name="failed_checks.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
