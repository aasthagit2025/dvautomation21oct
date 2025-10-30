# 29octapp.py  (Final Build – Part 1/2)
# KnowledgeExcel – Data Validation Automation
# Includes numeric-safe, space-insensitive skip parser and two-way skip validation

import streamlit as st
import pandas as pd
import numpy as np
import re, io
from datetime import datetime
from typing import List, Tuple

st.set_page_config(page_title="KnowledgeExcel — DV Automation (Final)", layout="wide")
st.title("KnowledgeExcel — Data Validation Automation (Final)")

# ---------- DK defaults ----------
DEFAULT_DK_CODES = [88, 99]
DEFAULT_DK_TOKENS = ["DK", "Refused", "Don't know", "Dont know", "Refuse", "REFUSED"]

# ---------- Parse user DK input ----------
def parse_dk_codes(s: str):
    try:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        return [int(float(p)) for p in parts]
    except Exception:
        return DEFAULT_DK_CODES

def parse_dk_tokens(s: str):
    try:
        return [p.strip() for p in re.split(r',|\;|\|', s) if p.strip()]
    except Exception:
        return DEFAULT_DK_TOKENS

# ---------- Sidebar ----------
st.sidebar.header("Upload")
raw_file = st.sidebar.file_uploader("Raw Data (xlsx/csv)", type=["xlsx", "xls", "csv"])
skips_file = st.sidebar.file_uploader("Sawtooth Skips (CSV/XLSX)", type=["csv", "xlsx"])
run_btn = st.sidebar.button("Run Validation")

st.sidebar.markdown("---")
st.sidebar.header("DK Settings")
dk_codes_input = st.sidebar.text_input("DK numeric codes", ",".join(map(str, DEFAULT_DK_CODES)))
dk_tokens_input = st.sidebar.text_input("DK text tokens", ",".join(DEFAULT_DK_TOKENS))

DK_CODES = parse_dk_codes(dk_codes_input)
DK_TOKENS = parse_dk_tokens(dk_tokens_input)
st.session_state["DK_CODES"] = DK_CODES
st.session_state["DK_TOKENS"] = DK_TOKENS

# ---------- Helper ----------
def read_any_df(uploaded):
    if uploaded is None:
        return None
    bio = io.BytesIO(uploaded.read())
    name = uploaded.name.lower()
    try:
        if name.endswith(("xlsx", "xls")):
            return pd.read_excel(bio, engine="openpyxl")
        else:
            return pd.read_csv(bio, encoding="utf-8-sig")
    except Exception:
        bio.seek(0)
        return pd.read_csv(bio, encoding="ISO-8859-1")

# ---------- Robust Skip Parser ----------
def parse_skip_expression_to_mask(expr, df):
    """
    Fully-robust Sawtooth skip parser:
      • Handles <>, =, AND/OR (case & space-insensitive)
      • Handles Not(...), parentheses
      • Coerces variables to numeric
      • Returns boolean Series
    """
    expr_orig = str(expr)
    try:
        e = expr_orig
        # Normalize spacing (make operators space-insensitive)
        e = re.sub(r'(?i)and', ' AND ', e)
        e = re.sub(r'(?i)or', ' OR ', e)
        e = re.sub(r'(?i)not\s*\(', ' NOT(', e)
        e = re.sub(r'\s+', ' ', e)

        # Replace SQL/Decipher syntax with Python equivalents
        e = e.replace("<>", "!=")
        e = re.sub(r'(?<![!<>=])=(?!=)', '==', e)
        e = re.sub(r'(?i)\bAND\b', '&', e)
        e = re.sub(r'(?i)\bOR\b', '|', e)
        e = re.sub(r'(?i)\bNOT\s*\(', '~(', e)

        # Replace variable names with df references (numeric coercion)
        cols = sorted(df.columns, key=len, reverse=True)
        for col in cols:
            e = re.sub(rf'(?<!\w){re.escape(col)}(?!\w)',
                       f"pd.to_numeric(df[{repr(col)}], errors='coerce')", e)

        mask = eval(e, {"df": df, "pd": pd, "np": np})
        return pd.Series(mask, index=df.index).fillna(False).astype(bool)
    except Exception as err:
        raise ValueError(f"Skip parse failed for '{expr_orig}': {err}")

# ---------- Junk OE ----------
def detect_junk_oe(v, repeat_min=4, min_len=2):
    if pd.isna(v): return False
    s = str(v).strip()
    if s == "" or (s.isdigit() and len(s) <= 3): return True
    if re.match(rf'^(.)\1{{{repeat_min-1},}}$', s): return True
    if len(s) <= min_len: return True
    return False

# ---------- Straightliner ----------
def find_straightliners(df, cols, thr=0.85):
    res = {}
    if len(cols) < 2: return res
    m = df[cols].astype(str).fillna("")
    for i, r in m.iterrows():
        vals = [x for x in r.values if x != ""]
        if len(vals) < 2: continue
        top = pd.Series(vals).mode()[0]
        frac = (pd.Series(vals) == top).mean()
        if frac >= thr: res[i] = {"frac": frac}
    return res

# ---------- Two-way skip validator ----------
def check_skip_violations(var, expr, df, id_col):
    out = []
    try:
        mask = parse_skip_expression_to_mask(expr, df)
        ans = df[var].astype(str).fillna("").str.strip()
        blank = ans.eq("") | ans.str.lower().isin(["na","n/a","nan","none"])
        # Violation 1 – answered when should skip
        v1 = df[mask & ~blank]
        # Violation 2 – skipped when should answer
        v2 = df[~mask & blank]
        if len(v1)>0:
            out.append(("Skip Violation (Answered when should Skip)",
                        len(v1), ";".join(v1[id_col].astype(str).tolist()[:200])))
        if len(v2)>0:
            out.append(("Skip Violation (Skipped when should Answer)",
                        len(v2), ";".join(v2[id_col].astype(str).tolist()[:200])))
    except Exception as e:
        out.append(("Skip Parsing Error",0,f"Could not parse: {expr} | {e}"))
    return out

# ---------- END OF PART 1 ----------
# 29octapp.py  (Final Build – Part 2/2)
# ---------------------------------------------------------------
def format_ids(series, n=200):
    return ";".join(map(str, series.astype(str).unique()[:n]))

def group_variables(cols):
    groups = {}
    for c in cols:
        m = re.match(r"^(.*?)(_?\d+|R\d+)?$", c, flags=re.I)
        if m:
            prefix = re.sub(r"[_Rr\d]+$", "", m.group(1))
        else:
            prefix = c
        groups.setdefault(prefix, []).append(c)
    return groups

# ---------- RUN ----------
if run_btn:
    if not raw_file or not skips_file:
        st.error("Please upload both Raw Data and Skips files.")
        st.stop()

    df = read_any_df(raw_file)
    skips = read_any_df(skips_file)
    id_col = next((c for c in df.columns if str(c).lower() in
                   ["respondentid","resp_id","id","sys_respnum"]), df.columns[0])

    st.info(f"Respondent ID column → **{id_col}**")
    data_vars = [c for c in df.columns if not str(c).lower().startswith("sys_")]
    rules, findings = [], []

    # ---------- build rules from skips ----------
    st.write("Building skip rules from Sawtooth Skips…")
    lc = {c.lower(): c for c in skips.columns}
    from_col = lc.get("skip from") or list(skips.columns)[0]
    logic_col = lc.get("logic") or lc.get("condition") or None
    to_col = lc.get("skip to") or lc.get("target") or None

    if logic_col:
        for _, r in skips.iterrows():
            src = str(r.get(from_col, "")).strip()
            expr = str(r.get(logic_col, "")).strip()
            tgt = str(r.get(to_col, "")).strip()
            if not src or not expr: 
                continue
            desc = f"Skip {src} when {expr} (Target {tgt})"
            rules.append({"Variable":src,"Type":"Skip","Rule Applied":expr,"Description":desc})

    # ---------- validate ----------
    st.write("Running validations…")
    for rule in rules:
        var, expr = rule["Variable"], rule["Rule Applied"]
        if var not in df.columns: 
            continue
        for ctype, count, ids in check_skip_violations(var, expr, df, id_col):
            findings.append({"Variable":var,"Check_Type":ctype,
                             "Description":rule["Description"],
                             "Affected_Count":count,"Respondent_IDs":ids})

    # ---------- summary ----------
    if findings:
        rep = pd.DataFrame(findings)
        st.subheader("Skip Validation Results – Preview")
        st.dataframe(rep.head(200), use_container_width=True)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
            rep.to_excel(w, "Detailed Checks", index=False)
            rep.groupby("Check_Type", as_index=False)["Affected_Count"].sum()\
               .to_excel(w, "Summary", index=False)
            pd.DataFrame({"Generated": [datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")],
                          "Respondent ID": [id_col],
                          "Rows":[df.shape[0]],"Cols":[df.shape[1]]})\
               .to_excel(w, "Project Info", index=False)
        buf.seek(0)
        st.download_button("📥 Download Validation Report.xlsx",
            data=buf, file_name="Validation Report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.success("Validation Report ready for download.")
    else:
        st.warning("No skip violations found with current data.")
# ---------------------------------------------------------------
