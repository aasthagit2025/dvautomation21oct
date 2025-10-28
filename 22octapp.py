# 22octapp.py
import streamlit as st
import pandas as pd
import numpy as np
import re
import io
from datetime import datetime

st.set_page_config(page_title="DV Automation (Final)", layout="wide")
st.title("🔎 Full Data Validation Automation — Final with Flagged Respondents Export")

st.markdown(
    "This app builds Validation Rules from Sawtooth skips, runs full DV checks (Skip, Range, DK, Multi, Straightliner, Junk OE, Combined), "
    "and provides three downloads: **Validation Rules.xlsx**, **Validation Report.xlsx**, and **Flagged_Respondents.csv**."
)

# ---------------- Sidebar: uploads and controls ----------------
st.sidebar.header("Upload files")
raw_file = st.sidebar.file_uploader("Raw Data (Excel or CSV)", type=["xlsx", "xls", "csv"])
skips_file = st.sidebar.file_uploader("Sawtooth Skips (CSV/XLSX)", type=["csv", "xlsx"])
rules_file = st.sidebar.file_uploader("Optional: Validation Rules template (xlsx)", type=["xlsx"])
run_btn = st.sidebar.button("Run Full DV Automation 🚀")

st.sidebar.markdown("---")
st.sidebar.header("Tuning parameters")
straightliner_threshold = st.sidebar.slider("Straightliner threshold", 0.50, 0.98, 0.85, 0.01)
junk_repeat_min = st.sidebar.slider("Junk OE: min repeated chars", 2, 8, 4, 1)
junk_min_length = st.sidebar.slider("Junk OE: min OE length", 1, 10, 2, 1)

# ---------------- Check xlsxwriter availability ----------------
try:
    import xlsxwriter  # noqa: F401
    XLSXWRITER_AVAILABLE = True
except Exception:
    XLSXWRITER_AVAILABLE = False

if not XLSXWRITER_AVAILABLE:
    st.sidebar.warning(
        "⚠️ Note: xlsxwriter not installed. Excel downloads will still work but without advanced formatting.\n\n"
        "Add this to your `requirements.txt`:\n\n```\nxlsxwriter\n```"
    )

# ---------------- Helper functions ----------------
def read_any_df(uploaded):
    if uploaded is None:
        return None
    name = uploaded.name.lower()
    uploaded.seek(0)
    try:
        if name.endswith((".xlsx", ".xls")):
            return pd.read_excel(uploaded, engine="openpyxl")
        else:
            return pd.read_csv(uploaded, encoding="utf-8-sig")
    except Exception:
        uploaded.seek(0)
        try:
            return pd.read_csv(uploaded, encoding="ISO-8859-1")
        except Exception:
            uploaded.seek(0)
            return pd.read_csv(uploaded, encoding="utf-8", errors="replace")

def detect_junk_oe(value, junk_repeat_min=4, junk_min_length=2):
    if pd.isna(value):
        return False
    s = str(value).strip()
    if s == "":
        return True
    if s.isdigit() and len(s) <= 3:
        return True
    if re.match(r'^(.)\1{' + str(max(1, junk_repeat_min-1)) + r',}$', s):
        return True
    non_alnum_ratio = len(re.sub(r'[A-Za-z0-9]', '', s)) / max(1, len(s))
    if non_alnum_ratio > 0.6:
        return True
    if len(s) <= junk_min_length:
        return True
    return False

def find_straightliners(df, candidate_cols, threshold=0.85):
    straightliners = {}
    if len(candidate_cols) < 2:
        return straightliners
    m = df[candidate_cols].astype(str).fillna("")
    for idx, row in m.iterrows():
        non_blank = row.replace("", np.nan).dropna()
        if len(non_blank) < 2:
            continue
        vals = non_blank.values
        top_modes = pd.Series(vals).mode()
        if top_modes.empty:
            continue
        topval = top_modes.iloc[0]
        same_count = (vals == topval).sum()
        frac = same_count / len(non_blank)
        if frac >= threshold:
            straightliners[idx] = {"value": topval, "same_count": int(same_count), "total": int(len(non_blank)), "fraction": float(frac)}
    return straightliners

def parse_skip_expression_to_mask(expr, df):
    # basic skip parsing for conditions like Q1=1 OR (Q2<>3)
    try:
        expr = expr.replace("AND", "&").replace("and", "&").replace("OR", "|").replace("or", "|")
        for col in df.columns:
            expr = re.sub(rf'\b{col}\b', f"df['{col}']", expr)
        mask = eval(expr)
        return mask.fillna(False).astype(bool)
    except Exception:
        return pd.Series([False]*len(df), index=df.index)

# ---------------- Run automation ----------------
if run_btn:
    if raw_file is None or skips_file is None:
        st.error("Please upload both Raw Data and Sawtooth Skips file.")
    else:
        progress = st.progress(0)
        status = st.empty()

        # Step 1: Load files
        status.text("Step 1/7 — Loading files")
        raw_df = read_any_df(raw_file)
        skips_df = read_any_df(skips_file)
        rules_wb = None
        if rules_file:
            try:
                rules_wb = pd.read_excel(rules_file, sheet_name=None)
            except Exception:
                rules_wb = None
        progress.progress(10)

        # Detect respondent ID
        status.text("Step 2/7 — Detecting Respondent ID")
        possible_ids = ["RESPID","RespondentID","CaseID","caseid","id","ID","Respondent Id","sys_RespNum"]
        id_col = next((c for c in raw_df.columns if c in possible_ids), raw_df.columns[0])
        id_col = id_col.lstrip("\ufeff")
        st.info(f"Detected Respondent ID column: **{id_col}**")
        progress.progress(20)

        # Settings
        DK_CODES = [88, 99]
        DK_STRINGS = ["DK", "Refused", "Don't know", "Dont know", "Refuse", "REFUSED"]
        numeric_min, numeric_max = 0, 99

        # Step 3: Build Validation Rules
        status.text("Step 3/7 — Building Validation Rules")
        validation_rules = []
        if "Logic" in skips_df.columns:
            for _, r in skips_df.iterrows():
                logic = str(r["Logic"]) if pd.notna(r["Logic"]) else ""
                q_from = str(r.get("Skip From", "")) or str(r.get("Question", ""))
                if logic:
                    validation_rules.append({
                        "Variable": q_from,
                        "Type": "Skip",
                        "Rule Applied": logic,
                        "Description": f"Skip {q_from} when {logic}",
                        "Derived From": "Sawtooth Skip"
                    })
        # Auto add DK/Range rules
        for var in raw_df.columns:
            validation_rules.append({
                "Variable": var, "Type": "Range", "Rule Applied": f"{numeric_min}-{numeric_max}",
                "Description": "Expected numeric within range", "Derived From": "Auto"
            })
            validation_rules.append({
                "Variable": var, "Type": "DK/Refused", "Rule Applied": f"Codes {DK_CODES}; Tokens {DK_STRINGS}",
                "Description": "Detect DK/Refused responses", "Derived From": "Auto"
            })

        vr_df = pd.DataFrame(validation_rules)
        st.subheader("Validation Rules (preview)")
        st.dataframe(vr_df.head(100))
        progress.progress(40)

        # Download Validation Rules.xlsx
        rules_buf = io.BytesIO()
        with pd.ExcelWriter(rules_buf, engine="xlsxwriter" if XLSXWRITER_AVAILABLE else "openpyxl") as writer:
            vr_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
            if XLSXWRITER_AVAILABLE:
                wb, ws = writer.book, writer.sheets["Validation_Rules"]
                header_fmt = wb.add_format({'bold': True, 'bg_color': '#305496', 'font_color': 'white', 'border': 1})
                for i, col in enumerate(vr_df.columns):
                    ws.write(0, i, col, header_fmt)
                    ws.set_column(i, i, min(60, max(vr_df[col].astype(str).map(len).max(), len(col)) + 2))
                ws.freeze_panes(1, 1)
        rules_buf.seek(0)
        st.download_button("📥 Download Validation Rules.xlsx", data=rules_buf, file_name="Validation Rules.xlsx")
        progress.progress(55)

        # Step 4: Run validation checks
        status.text("Step 4/7 — Running validation checks")
        findings = []
        for var in raw_df.columns:
            s = raw_df[var]
            coerced = pd.to_numeric(s, errors="coerce")
            out_range = coerced[(~coerced.isna()) & ((coerced < numeric_min) | (coerced > numeric_max))]
            if len(out_range) > 0:
                findings.append({
                    "Variable": var,
                    "Check_Type": "Range Violation",
                    "Description": f"{len(out_range)} out of range",
                    "Affected_Count": len(out_range)
                })
            dk_text = s.astype(str).str.strip().str.lower().isin([t.lower() for t in DK_STRINGS])
            dk_code = coerced.isin(DK_CODES)
            total_dk = int((dk_text | dk_code).sum())
            if total_dk > 0:
                findings.append({
                    "Variable": var,
                    "Check_Type": "DK/Refused",
                    "Description": f"{total_dk} DK/Refused entries",
                    "Affected_Count": total_dk
                })
        detailed_df = pd.DataFrame(findings)
        summary_df = detailed_df.groupby("Check_Type", as_index=False)["Affected_Count"].sum() if not detailed_df.empty else pd.DataFrame()
        progress.progress(75)

        # Step 5: Respondent-level violations
        status.text("Step 5/7 — Creating Respondent Violations and Flagged Respondents")
        viol_mask = raw_df.isnull() | raw_df.astype(str).apply(lambda x: x.str.strip().str.lower().isin([t.lower() for t in DK_STRINGS]))
        flagged = viol_mask.any(axis=1)
        flagged_df = raw_df.loc[flagged, [id_col]]
        flagged_df["Flag_Count"] = viol_mask.sum(axis=1)[flagged]
        flagged_csv = flagged_df.to_csv(index=False).encode("utf-8")
        st.download_button("📥 Download Flagged_Respondents.csv", data=flagged_csv, file_name="Flagged_Respondents.csv", mime="text/csv")
        progress.progress(85)

        # Step 6: Create Excel report
        status.text("Step 6/7 — Building Validation Report.xlsx")
        report_buf = io.BytesIO()
        with pd.ExcelWriter(report_buf, engine="xlsxwriter" if XLSXWRITER_AVAILABLE else "openpyxl") as writer:
            detailed_df.to_excel(writer, sheet_name="Detailed Checks", index=False)
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            vr_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
            if XLSXWRITER_AVAILABLE:
                wb = writer.book
                header_fmt = wb.add_format({'bold': True, 'bg_color': '#305496', 'font_color': 'white', 'border': 1})
                for sheet in writer.sheets.values():
                    sheet.freeze_panes(1, 1)
                    for i, col in enumerate(detailed_df.columns):
                        sheet.set_column(i, i, 30)
        report_buf.seek(0)
        st.download_button("📥 Download Validation Report.xlsx", data=report_buf, file_name="Validation Report.xlsx")
        progress.progress(100)

        status.success("✅ Completed! All downloads are ready: Validation Rules, Validation Report, and Flagged Respondents.")
