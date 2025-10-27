import streamlit as st
import pandas as pd
import numpy as np
import pyreadstat
import io
import re

st.set_page_config(page_title="Survey Validation Tool", layout="wide")

st.title("📊 Automated Survey Validation & Skip Logic Tool")

# -------------------- FILE UPLOAD SECTION --------------------
raw_file = st.file_uploader("Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx", "sav"])
skip_file = st.file_uploader("Upload Skip Rules file (CSV / XLSX)", type=["csv", "xlsx"])

# -------------------- LOAD DATA --------------------
if raw_file:
    if raw_file.name.endswith(".csv"):
        df = pd.read_csv(raw_file, encoding_errors="ignore")
    elif raw_file.name.endswith(".xlsx"):
        df = pd.read_excel(raw_file)
    elif raw_file.name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(raw_file)
    else:
        st.error("Unsupported file type!")
        st.stop()

    df.columns = [str(c).strip() for c in df.columns]

    id_col = next((c for c in ["RespondentID", "Password", "RespID", "RID", "Sys_RespNum"] if c in df.columns), None)
    if not id_col:
        id_col = df.columns[0]

    st.success(f"✅ Data loaded successfully with {df.shape[0]} respondents and {df.shape[1]} variables.")
    st.info(f"Respondent ID column used: **{id_col}**")

    # -------------------- LOAD SKIP RULES --------------------
    skip_rules = pd.DataFrame()
    if skip_file:
        if skip_file.name.endswith(".csv"):
            skip_rules = pd.read_csv(skip_file)
        elif skip_file.name.endswith(".xlsx"):
            skip_rules = pd.read_excel(skip_file)
        skip_rules.columns = [str(c).strip() for c in skip_rules.columns]
        st.success(f"✅ {len(skip_rules)} skip rules loaded.")

    # -------------------- AUTO RULE GENERATION --------------------
    st.subheader("🔧 Auto-generating validation rules...")
    rules = []

    def detect_question_type(col, data):
        """Detects question type heuristically based on values."""
        vals = data[col].dropna().unique()
        if len(vals) == 0:
            return "missing"
        if all(str(v).strip().lower() in ["0", "1"] for v in vals):
            return "multi"
        try:
            numeric_vals = pd.to_numeric(vals, errors="coerce")
            if np.nanmax(numeric_vals) <= 10:
                return "rating"
            return "numeric"
        except Exception:
            return "text"

    for col in df.columns:
        q_type = detect_question_type(col, df)
        if q_type == "rating":
            max_val = int(df[col].dropna().astype(float).max())
            rules.append([col, "Range;Straightliner", f"1-{max_val};Same response across grid", "Auto (rating)"])
        elif q_type == "multi":
            rules.append([col, "Multi-Select", "Only 0/1; At least one selected", "Auto (multi-select)"])
        elif q_type == "numeric":
            max_val = int(df[col].dropna().astype(float).max())
            rules.append([col, "Range", f"1-{max_val}", "Auto (numeric)"])
        elif q_type == "text":
            rules.append([col, "Missing;OpenEnd_Junk", ";MinLen(3)", "Auto (open-end/text)"])
        else:
            rules.append([col, "Missing", "", "Auto (single-select)"])

    rules_df = pd.DataFrame(rules, columns=["Question", "Check_Type", "Condition", "Source"])

    # -------------------- MERGE SKIP RULES --------------------
    if not skip_rules.empty:
        for _, row in skip_rules.iterrows():
            q_from = str(row.get("Skip From", "")).strip()
            logic = str(row.get("Logic", "")).strip()
            q_to = str(row.get("Skip To", "")).strip()
            if q_from in df.columns and q_to in df.columns:
                condition = f"If {logic} then {q_to} should be blank"
                rules_df = pd.concat([rules_df, pd.DataFrame([[q_to, "Skip", condition, "From Skip File"]], columns=rules_df.columns)], ignore_index=True)

    st.dataframe(rules_df, use_container_width=True)
    st.download_button("📥 Download Generated Validation Rules", rules_df.to_csv(index=False).encode("utf-8"), "validation_rules.csv", "text/csv")

    # -------------------- VALIDATION CHECKS --------------------
    st.subheader("🔍 Run Validation Checks (Only Failed Rows)")

    if st.button("Run Validation Now"):
        report = []

        def within_range(val, cond):
            try:
                if any(str(v) in ["88", "99", "DK", "Refused"] for v in [val]):
                    return True  # DK/Refused are valid
                low, high = re.findall(r"\d+", cond)
                low, high = int(low), int(high)
                return low <= float(val) <= high
            except:
                return True

        for _, rule in rules_df.iterrows():
            q = rule["Question"]
            ctype = rule["Check_Type"].lower()
            cond = str(rule["Condition"])
            if q not in df.columns:
                continue

            if "range" in ctype:
                for rid, val in zip(df[id_col], df[q]):
                    if pd.notna(val) and not within_range(val, cond):
                        report.append([rid, q, "Range", f"Out of range ({cond})"])
            if "missing" in ctype:
                for rid, val in zip(df[id_col], df[q]):
                    if pd.isna(val) or str(val).strip().lower() in ["", "na", "none"]:
                        report.append([rid, q, "Missing", "Value missing"])
            if "openend_junk" in ctype:
                for rid, val in zip(df[id_col], df[q]):
                    if isinstance(val, str) and len(val.strip()) < 3:
                        report.append([rid, q, "OpenEnd_Junk", "Too short / invalid text"])
            if "straightliner" in ctype and "_" in q:
                prefix = q.split("_")[0]
                grid_cols = [c for c in df.columns if c.startswith(prefix + "_")]
                same_resp = df[grid_cols].nunique(axis=1) == 1
                offenders = df.loc[same_resp, id_col]
                for rid in offenders:
                    report.append([rid, prefix, "Straightliner", "Same response across grid"])
            if "skip" in ctype and "then" in cond.lower():
                try:
                    if_part, then_part = re.split(r"(?i)then", cond)
                    target = re.findall(r"\b\w+\b", then_part)[0]
                    if target in df.columns:
                        skip_mask = df.eval(if_part.replace("=", "=="))
                        blank_mask = df[target].isna() | (df[target].astype(str).str.strip() == "")
                        offenders = df.loc[skip_mask & ~blank_mask, id_col]
                        for rid in offenders:
                            report.append([rid, target, "Skip", "Should be blank as per logic"])
                except:
                    continue

        if not report:
            st.success("✅ No validation issues found!")
        else:
            report_df = pd.DataFrame(report, columns=[id_col, "Question", "Check_Type", "Issue"])
            st.error(f"⚠️ Found {len(report_df)} failed checks!")
            st.dataframe(report_df, use_container_width=True)

            out = io.BytesIO()
            with pd.ExcelWriter(out, engine="openpyxl") as writer:
                report_df.to_excel(writer, index=False, sheet_name="Failed Checks")
            st.download_button(
                "📥 Download Validation Report",
                data=out.getvalue(),
                file_name="validation_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
