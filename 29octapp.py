from pathlib import Path
content = r'''# 29octapp.py
"""
Final Data Validation Streamlit App (Updated with robust skip parser and two-way skip validation)
- Supports: <>, =, AND/OR, NOT(...), parentheses, multi-variable expressions
- "Next Question only" skip auto-adjust (uses raw data sequence)
- Two-way skip validation:
    * Answered when should skip
    * Skipped when should answer
- DK_CODES persisted in session_state to avoid NameError
- Multi-select completeness, Range, DK/Refused, Junk OE, Straightliner checks
- Validation Rules and Validation Report downloads
"""

import streamlit as st
import pandas as pd
import numpy as np
import re
import io
from datetime import datetime
from typing import List, Tuple

# ---------------- Configuration (editable) ----------------
RESPONDENT_ID_CANDIDATES = ["RESPID","RespondentID","CaseID","caseid","id","ID","Respondent Id","sys_RespNum"]
DEFAULT_DK_CODES = [88, 99]
DEFAULT_DK_TOKENS = ["DK","Refused","Don't know","Dont know","Refuse","REFUSED"]

# ---------------- Page ----------------
st.set_page_config(page_title="KnowledgeExcel — DV Automation (29octapp)", layout="wide")
st.title("KnowledgeExcel — Data Validation Automation (29octapp)")
st.markdown(
    "Flow: Upload Raw Data + Sawtooth Skips (Skips.csv format) → Run → Download Validation Rules → (Optional) Upload revised rules → Confirm → Download Validation Report."
)

# ---------------- Sidebar uploads & controls ----------------
st.sidebar.header("Upload files (Skips.csv: Skip From | Skip Type | Skip To | Logic | Comment)")
raw_file = st.sidebar.file_uploader("Raw Data (Excel or CSV)", type=["xlsx","xls","csv"])
skips_file = st.sidebar.file_uploader("Sawtooth Skips (CSV/XLSX) - use provided Skips.csv format", type=["csv","xlsx"])
rules_template_file = st.sidebar.file_uploader("Optional: Validation Rules template (xlsx)", type=["xlsx"])

run_btn = st.sidebar.button("Run Full DV Automation: Build Validation Rules")
st.sidebar.markdown("---")
st.sidebar.header("Tuning parameters")
straightliner_threshold = st.sidebar.slider("Straightliner threshold", 0.50, 0.98, 0.85, 0.01)
junk_repeat_min = st.sidebar.slider("Junk OE: min repeated chars", 2, 8, 4, 1)
junk_min_length = st.sidebar.slider("Junk OE: min OE length", 1, 10, 2, 1)

st.sidebar.markdown("---")
st.sidebar.header("DK / Refused (editable)")
dk_codes_input = st.sidebar.text_input("DK numeric codes (comma separated)", value=",".join(map(str, DEFAULT_DK_CODES)))
dk_tokens_input = st.sidebar.text_input("DK text tokens (comma separated)", value=",".join(DEFAULT_DK_TOKENS))

# ---------------- parse DK inputs & persist to session_state ----------------
def parse_dk_codes(s: str) -> List[int]:
    try:
        parts = [p.strip() for p in s.split(",") if p.strip()!='']
        return [int(float(p)) for p in parts]
    except Exception:
        return DEFAULT_DK_CODES

def parse_dk_tokens(s: str) -> List[str]:
    try:
        parts = [p.strip() for p in re.split(r',|\;|\|', s) if p.strip()!='']
        return parts
    except Exception:
        return DEFAULT_DK_TOKENS

# compute DK lists and save in session_state so they persist across reruns
try:
    DK_CODES = parse_dk_codes(dk_codes_input)
    DK_TOKENS = parse_dk_tokens(dk_tokens_input)
except Exception:
    DK_CODES = DEFAULT_DK_CODES
    DK_TOKENS = DEFAULT_DK_TOKENS

st.session_state["DK_CODES"] = DK_CODES
st.session_state["DK_TOKENS"] = DK_TOKENS

# ---------------- xlsxwriter availability ----------------
try:
    import xlsxwriter  # noqa: F401
    XLSXWRITER_AVAILABLE = True
except Exception:
    XLSXWRITER_AVAILABLE = False
    st.sidebar.warning("xlsxwriter not installed — Excel formatting will be basic. Add 'xlsxwriter' to requirements.txt for full formatting.")

# ---------------- Helper utilities ----------------
@st.cache_data(show_spinner=False)
def read_any_df_cached(uploaded_bytes: bytes, name: str):
    bio = io.BytesIO(uploaded_bytes)
    n = name.lower()
    try:
        if n.endswith((".xlsx", ".xls")):
            return pd.read_excel(bio, engine="openpyxl")
        else:
            return pd.read_csv(bio, encoding="utf-8-sig")
    except Exception:
        bio.seek(0)
        try:
            return pd.read_csv(bio, encoding="ISO-8859-1")
        except Exception:
            bio.seek(0)
            return pd.read_csv(bio, encoding="utf-8", errors="replace")

def read_any_df(uploaded):
    if uploaded is None:
        return None
    uploaded.seek(0)
    return read_any_df_cached(uploaded.read(), uploaded.name)

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

def group_variables(vars_list: List[str]) -> dict:
    groups = {}
    for v in vars_list:
        if re.search(r'_[0-9]+$', v):
            prefix = re.sub(r'_[0-9]+$','', v)
            groups.setdefault(prefix, {"vars": [], "group_type": None}).get("vars").append(v)
        elif re.search(r'R\d+$', v, re.IGNORECASE):
            prefix = re.sub(r'R\d+$','', v)
            groups.setdefault(prefix, {"vars": [], "group_type": None}).get("vars").append(v)
        elif re.search(r'[A-Za-z]$', v) and len(v) > 2:
            prefix = v[:-1]
            groups.setdefault(prefix, {"vars": [], "group_type": None}).get("vars").append(v)
        else:
            groups.setdefault(v, {"vars": [], "group_type": None}).get("vars").append(v)
    for prefix, info in groups.items():
        vars_in_group = info["vars"]
        if any(re.search(r'_[0-9]+$', vv) for vv in vars_in_group) and len(vars_in_group) > 1:
            info["group_type"] = f"Multi-Select Block ({prefix}, {len(vars_in_group)} items)"
        elif (any(re.search(r'R\d+$', vv, re.IGNORECASE) for vv in vars_in_group) or any(re.search(r'[A-Za-z]$', vv) for vv in vars_in_group)) and len(vars_in_group) > 1:
            info["group_type"] = f"Rating Grid ({prefix}, {len(vars_in_group)} items)"
        else:
            info["group_type"] = "Standalone"
    return groups

def detect_variable_type_and_stats(series: pd.Series) -> Tuple[str, dict]:
    s = series.dropna()
    stats = {"n": len(series), "non_missing": len(s)}
    if len(s) == 0:
        return "Empty", stats
    as_str = s.astype(str).str.strip()
    has_alpha = as_str.str.contains(r'[A-Za-z]', regex=True).mean()
    coerced = pd.to_numeric(s, errors='coerce')
    numeric_prop = coerced.notna().mean()
    avg_len = as_str.str.len().mean()
    unique_numeric = pd.Series(coerced.dropna().unique()).tolist()
    stats.update({"numeric_prop": numeric_prop, "has_alpha_prop": has_alpha, "avg_len": avg_len, "unique_numeric_vals": unique_numeric, "sample_text_vals": as_str.unique()[:10].tolist()})
    if (has_alpha > 0.6) or (numeric_prop < 0.3 and avg_len > 10) or (avg_len > 30):
        return "Open-Ended", stats
    if numeric_prop >= 0.6 and avg_len < 15:
        return "Numeric", stats
    return "Categorical", stats

# ---------------- robust skip parser ----------------
def parse_skip_expression_to_mask(expr, df):
    """
    Safely parse skip expressions like:
      'OMQ2_r1<>1 OR OMQ2_r2=1 AND OMQ3<>99'
      'Not(PCQ5x1_r1=1 Or PCQ5x1_r1=2 Or PCQ5x1_r1=3)'
    Supports <> -> !=, = -> ==, AND/OR, NOT(), parentheses, multi-variable expressions.
    Returns a boolean Series or raises an Exception on parse failure.
    """
    expr_orig = str(expr)
    try:
        expr2 = str(expr_orig)

        # Normalize common tokens
        # Handle NOT(...) or Not(...) -> convert to Python ~(...)
        expr2 = re.sub(r'\bNot\s*\(', ' ~(', expr2, flags=re.IGNORECASE)
        # Replace SQL-like operators with Python equivalents
        expr2 = expr2.replace("<>", "!=")
        # Replace lone '=' with '==' unless part of !=, <=, >=
        expr2 = re.sub(r'(?<![!<>=])=(?!=)', '==', expr2)
        # Logical words to python bitwise operators for pandas Series
        expr2 = re.sub(r'\bAND\b', '&', expr2, flags=re.IGNORECASE)
        expr2 = re.sub(r'\bOR\b', '|', expr2, flags=re.IGNORECASE)

        # Replace variable tokens with df['col'] reference, careful with names that may contain special chars
        cols = list(df.columns)
        # Sort by length descending to avoid partial replacements
        cols_sorted = sorted(cols, key=lambda x: -len(x))
        for col in cols_sorted:
            # only replace whole-word occurrences
            expr2 = re.sub(rf'(?<!\w){re.escape(col)}(?!\w)', f"df[{repr(col)}]", expr2)

        # Evaluate safely in a restricted namespace
        mask = eval(expr2, {"df": df, "np": np, "pd": pd})
        # Ensure result is boolean Series
        if isinstance(mask, (pd.Series, np.ndarray, list)):
            mask_ser = pd.Series(mask, index=df.index)
            return mask_ser.fillna(False).astype(bool)
        else:
            raise ValueError(f"Parsed expression did not return a boolean Series: {expr2}")
    except Exception as e:
        # Re-raise the exception so caller can log Skip Parsing Error in report
        raise ValueError(f"Skip parse failed for '{expr_orig}': {e}")

# ---------------- session storage ----------------
if "rules_buf" not in st.session_state:
    st.session_state["rules_buf"] = None
if "report_buf" not in st.session_state:
    st.session_state["report_buf"] = None
if "final_vr_df" not in st.session_state:
    st.session_state["final_vr_df"] = None
if "detailed_df_preview" not in st.session_state:
    st.session_state["detailed_df_preview"] = None
if "rules_generated_time" not in st.session_state:
    st.session_state["rules_generated_time"] = None

# ---------------- Run: Build Validation Rules ----------------
if run_btn:
    if raw_file is None or skips_file is None:
        st.error("Please upload both Raw Data and Sawtooth Skips files.")
    else:
        status = st.empty()
        progress = st.progress(0)
        status.text("Loading files...")
        raw_df = read_any_df(raw_file)
        skips_df = read_any_df(skips_file)
        rules_wb = None
        if rules_template_file:
            try:
                rules_wb = pd.read_excel(rules_template_file, sheet_name=None)
            except Exception:
                rules_wb = None
        progress.progress(10)

        # respondent id
        id_col = next((c for c in raw_df.columns if c in RESPONDENT_ID_CANDIDATES), raw_df.columns[0])
        id_col = id_col.lstrip("\ufeff")
        data_vars = [c for c in raw_df.columns if not str(c).lower().startswith("sys_")]

        status.text("Grouping variables and detecting variable types...")
        groups = group_variables(data_vars)
        var_types = {}
        var_stats = {}
        for var in data_vars:
            vtype, stats = detect_variable_type_and_stats(raw_df[var])
            var_types[var] = vtype
            var_stats[var] = stats
        progress.progress(35)

        # Build rules from skips (Skips.csv expected format)
        validation_rules = []
        # normalize column names for skips file to known names
        skips_cols = {c.strip().lower(): c for c in skips_df.columns}
        # map columns
        skip_from_col = skips_cols.get("skip from") or skips_cols.get("question") or skips_cols.get("from") or list(skips_df.columns)[0]
        skip_type_col = skips_cols.get("skip type") or skips_cols.get("type") or None
        skip_to_col = skips_cols.get("skip to") or skips_cols.get("target") or skips_cols.get("to") or None
        logic_col = skips_cols.get("logic") or skips_cols.get("condition") or None
        comment_col = skips_cols.get("comment") or skips_cols.get("notes") or None

        status.text("Building rules from Sawtooth Skips (auto-fix targets if 'Next Question')...")
        if logic_col:
            for _, r in skips_df.iterrows():
                logic = r.get(logic_col, "")
                src = r.get(skip_from_col, "") if skip_from_col else ""
                tgt = r.get(skip_to_col, "") if skip_to_col else ""
                if pd.isna(logic) or str(logic).strip() == "":
                    continue
                src_str = str(src).strip() if pd.notna(src) else ""
                tgt_str = str(tgt).strip() if pd.notna(tgt) else ""
                if src_str.lower().startswith("sys_"):
                    continue
                # map src case-insensitive to raw_df column if possible
                lower_map = {c.lower(): c for c in raw_df.columns}
                if src_str.lower() in lower_map:
                    src_str = lower_map[src_str.lower()]
                # ONLY apply auto-adjust when tgt_str explicitly indicates "Next Question"
                tgt_lower = str(tgt_str).strip().lower()
                if tgt_lower in ["next", "nextquestion", "next question"]:
                    # find next variable after src_str in raw_df columns (skip system vars)
                    cols = list(raw_df.columns)
                    lower_map_cols = {c.lower(): c for c in cols}
                    if src_str not in cols and src_str.lower() in lower_map_cols:
                        src_str = lower_map_cols[src_str.lower()]
                    if src_str in cols:
                        pos = cols.index(src_str)
                        new_tgt = ""
                        for c in cols[pos + 1:]:
                            if not str(c).lower().startswith("sys_"):
                                new_tgt = c
                                break
                        if new_tgt:
                            description = f"Skip {src_str} when {logic} (Target 'Next Question' auto-updated to {new_tgt})"
                            tgt_str = new_tgt
                        else:
                            description = f"Skip {src_str} when {logic} (No next variable found; please review manually)"
                    else:
                        description = f"Skip {src_str} when {logic} (Source not found in raw data; unable to update target)"
                else:
                    # leave blank or text page targets unchanged
                    if (tgt_str is None) or (tgt_str == "") or (tgt_str not in raw_df.columns):
                        description = f"Skip {src_str} when {logic} (Target {tgt_str} not in data; left as-is)"
                    else:
                        description = f"Skip {src_str} when {logic} (Target: {tgt_str})"

                if src_str in data_vars:
                    validation_rules.append({
                        "Variable": src_str,
                        "Variable_Type": var_types.get(src_str, ""),
                        "Group_Type": groups.get(re.sub(r'_[0-9]+$','',src_str), {}).get("group_type","Standalone"),
                        "Type": "Skip",
                        "Rule Applied": str(logic).strip(),
                        "Description": description,
                        "Derived From": "Sawtooth Skip"
                    })
        progress.progress(55)

        status.text("Generating smart auto-rules for variables...")
        # smart auto-rules
        for var in data_vars:
            vtype = var_types.get(var, "Categorical")
            stats = var_stats.get(var, {})
            prefix = re.sub(r'_[0-9]+$','', var) if re.search(r'_[0-9]+$', var) else (re.sub(r'R\d+$','',var) if re.search(r'R\d+$',var, re.IGNORECASE) else (var[:-1] if re.search(r'[A-Za-z]$', var) else var))
            group_info = groups.get(prefix, {"vars":[var], "group_type":"Standalone"})
            group_type = group_info.get("group_type", "Standalone")

            if vtype == "Open-Ended":
                validation_rules.append({
                    "Variable": var,
                    "Variable_Type": "Open-Ended",
                    "Group_Type": group_type,
                    "Type": "Junk OE",
                    "Rule Applied": "Junk-OE heuristics",
                    "Description": "Open-ended: only junk OE detection applied",
                    "Derived From": "Auto"
                })
                continue

            # Multi-select block
            if re.search(r'_[0-9]+$', var) and group_type.startswith("Multi-Select"):
                members = group_info.get("vars", [var])
                desc = f"Multi-select completeness & validity (group {prefix}, {len(members)} items). Values must be 0/1; no all-missing or all-0 respondent rows."
                validation_rules.append({
                    "Variable": var,
                    "Variable_Type": "Multi-Select",
                    "Group_Type": group_type,
                    "Type": "Multi-Select",
                    "Rule Applied": "Values must be 0/1; no all-missing/all-0 respondent rows",
                    "Description": desc,
                    "Derived From": "Auto"
                })
                series = raw_df[var].dropna().astype(str).str.strip()
                present_tokens = [t for t in DK_TOKENS if any(series.str.lower() == t.lower())]
                present_codes = []
                try:
                    numeric_present = pd.to_numeric(raw_df[var], errors='coerce').dropna().astype(int).unique().tolist()
                    for code in DK_CODES:
                        if code in numeric_present:
                            present_codes.append(code)
                except Exception:
                    pass
                if present_tokens or present_codes:
                    validation_rules.append({
                        "Variable": var,
                        "Variable_Type": "Multi-Select",
                        "Group_Type": group_type,
                        "Type": "DK/Refused",
                        "Rule Applied": f"Codes {present_codes}; Tokens {present_tokens}",
                        "Description": "DK/Refused tokens/codes detected in multi-select (added only because present in data)",
                        "Derived From": "Auto"
                    })
                continue

            # Numeric values -> Range
            series = raw_df[var]
            coerced = pd.to_numeric(series, errors='coerce')
            numeric_vals = coerced.dropna().unique().tolist()
            if len(numeric_vals) > 0:
                if len(numeric_vals) == 1:
                    lo = hi = int(np.nanmin(coerced.dropna()))
                else:
                    lo = int(np.nanmin(coerced.dropna()))
                    hi = int(np.nanmax(coerced.dropna()))
                validation_rules.append({
                    "Variable": var,
                    "Variable_Type": "Numeric",
                    "Group_Type": group_type,
                    "Type": "Range",
                    "Rule Applied": f"{lo}-{hi}",
                    "Description": f"Numeric values expected between {lo} and {hi} based on data",
                    "Derived From": "Auto"
                })
                present_codes = [c for c in DK_CODES if c in [int(x) for x in numeric_vals if float(x).is_integer()]]
                series_text = series.dropna().astype(str).str.strip()
                present_tokens = [t for t in DK_TOKENS if any(series_text.str.lower() == t.lower())]
                if present_codes or present_tokens:
                    validation_rules.append({
                        "Variable": var,
                        "Variable_Type": "Numeric",
                        "Group_Type": group_type,
                        "Type": "DK/Refused",
                        "Rule Applied": f"Codes {present_codes}; Tokens {present_tokens}",
                        "Description": "DK/Refused tokens/codes detected in data (added only because present)",
                        "Derived From": "Auto"
                    })
                continue
            else:
                # categorical
                series_text = series.dropna().astype(str).str.strip()
                present_tokens = [t for t in DK_TOKENS if any(series_text.str.lower() == t.lower())]
                if present_tokens:
                    validation_rules.append({
                        "Variable": var,
                        "Variable_Type": "Categorical",
                        "Group_Type": group_type,
                        "Type": "DK/Refused",
                        "Rule Applied": f"Tokens {present_tokens}",
                        "Description": "DK/Refused tokens detected in categorical data (added only because present)",
                        "Derived From": "Auto"
                    })
                else:
                    validation_rules.append({
                        "Variable": var,
                        "Variable_Type": "Categorical",
                        "Group_Type": group_type,
                        "Type": "None",
                        "Rule Applied": "",
                        "Description": "No automated rule (categorical with no DK tokens found)",
                        "Derived From": "Auto"
                    })
        progress.progress(90)

        # Build VR df, persist as Excel bytes
        vr_df = pd.DataFrame(validation_rules)
        def var_index(v):
            try:
                return data_vars.index(v)
            except Exception:
                return len(data_vars) + 1
        if not vr_df.empty:
            vr_df['__ord'] = vr_df['Variable'].apply(var_index)
            vr_df = vr_df.sort_values(['__ord']).drop(columns='__ord')
        else:
            vr_df = pd.DataFrame(columns=["Variable","Variable_Type","Group_Type","Type","Rule Applied","Description","Derived From"])

        try:
            rules_buf = io.BytesIO()
            engine_choice = "xlsxwriter" if XLSXWRITER_AVAILABLE else "openpyxl"
            with pd.ExcelWriter(rules_buf, engine=engine_choice) as writer:
                vr_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
                if XLSXWRITER_AVAILABLE:
                    workbook = writer.book
                    worksheet = writer.sheets["Validation_Rules"]
                    header_format = workbook.add_format({'bold': True, 'bg_color': '#305496', 'font_color': 'white', 'border':1})
                    for col_num, value in enumerate(vr_df.columns.values):
                        worksheet.write(0, col_num, value, header_format)
                    worksheet.freeze_panes(1,1)
                    for i, col in enumerate(vr_df.columns):
                        try:
                            width = max(vr_df[col].astype(str).map(len).max(), len(str(col))) + 2
                            worksheet.set_column(i, i, min(80, width))
                        except Exception:
                            pass
            rules_buf.seek(0)
            st.session_state["rules_buf"] = rules_buf.getvalue()
            st.session_state["final_vr_df"] = vr_df.copy()
            st.session_state["rules_generated_time"] = datetime.utcnow().isoformat()
            st.subheader("Validation Rules — Preview")
            st.dataframe(vr_df, use_container_width=True)
            st.download_button("📥 Download Validation Rules.xlsx (Generated)", data=io.BytesIO(st.session_state["rules_buf"]), file_name="Validation Rules.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            st.success("Validation rules generated and saved in session. You may download, review, and optionally upload a revised Validation Rules.xlsx before confirming.")
        except Exception as e:
            st.error("Failed to prepare Validation Rules file: " + str(e))
            st.session_state["rules_buf"] = None
            st.session_state["final_vr_df"] = vr_df.copy()

# ---------------- Option: Upload revised rules to override ----------------
uploaded_rules_override = st.file_uploader("Upload revised Validation Rules.xlsx (optional)", type=["xlsx"])
if uploaded_rules_override is not None:
    try:
        vr_override_df = pd.read_excel(uploaded_rules_override, sheet_name=0)
        expected_cols = ["Variable","Variable_Type","Group_Type","Type","Rule Applied","Description","Derived From"]
        if not all(c in vr_override_df.columns for c in expected_cols):
            st.error(f"Uploaded rules missing required columns. Expected: {expected_cols}")
        else:
            # remove sys_ variables
            vr_override_df = vr_override_df[~vr_override_df['Variable'].astype(str).str.lower().str.startswith("sys_")].reset_index(drop=True)
            buf_override = io.BytesIO()
            engine_choice = "xlsxwriter" if XLSXWRITER_AVAILABLE else "openpyxl"
            with pd.ExcelWriter(buf_override, engine=engine_choice) as writer:
                vr_override_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
                if XLSXWRITER_AVAILABLE:
                    workbook = writer.book
                    worksheet = writer.sheets["Validation_Rules"]
                    header_format = workbook.add_format({'bold': True, 'bg_color': '#305496', 'font_color': 'white', 'border':1})
                    for col_num, value in enumerate(vr_override_df.columns.values):
                        worksheet.write(0, col_num, value, header_format)
                    worksheet.freeze_panes(1,1)
            buf_override.seek(0)
            st.session_state["rules_buf"] = buf_override.getvalue()
            st.session_state["final_vr_df"] = vr_override_df.copy()
            st.success("Uploaded Validation Rules will be used when you Confirm.")
            st.download_button("📥 Download Uploaded Validation Rules.xlsx", data=io.BytesIO(st.session_state["rules_buf"]), file_name="Validation Rules.xlsx", mime="application/vnd.openxmlformats-officedocument-spreadsheetml.sheet")
    except Exception as e:
        st.error("Could not read uploaded Validation Rules.xlsx: " + str(e))

# ---------------- Confirm & Generate Validation Report ----------------
confirm_btn = st.button("✅ Confirm & Generate Validation Report")
if confirm_btn:
    st.session_state["_force_generate"] = True

if st.session_state.get("_force_generate"):
    final_vr_df = st.session_state.get("final_vr_df")
    if final_vr_df is None:
        st.error("No Validation Rules available. Run 'Run Full DV Automation' first.")
    elif raw_file is None or skips_file is None:
        st.error("Raw data or skips file missing in current session. Re-run generation.")
    else:
        # ensure DK persisted to local variables (avoid NameError)
        DK_CODES = st.session_state.get("DK_CODES", DEFAULT_DK_CODES)
        DK_TOKENS = st.session_state.get("DK_TOKENS", DEFAULT_DK_TOKENS)

        raw_df = read_any_df(raw_file)
        data_vars = [c for c in raw_df.columns if not str(c).lower().startswith("sys_")]
        id_col = next((c for c in raw_df.columns if c in RESPONDENT_ID_CANDIDATES), raw_df.columns[0])
        id_col = id_col.lstrip("\ufeff")

        status = st.empty()
        progress = st.progress(0)
        status.text("Running validation checks using confirmed rules...")
        progress.progress(5)

        detailed_findings = []
        data_df = raw_df.copy()

        def format_ids(ids_series, max_ids=200):
            return ";".join(map(str, ids_series.astype(str).unique()[:max_ids].tolist()))

        # duplicate IDs
        dup_mask = data_df.duplicated(subset=[id_col], keep=False)
        if dup_mask.sum() > 0:
            detailed_findings.append({
                "Variable": id_col,
                "Check_Type": "Duplicate IDs",
                "Description": f"{int(dup_mask.sum())} duplicate rows (IDs duplicated)",
                "Affected_Count": int(dup_mask.sum()),
                "Respondent_IDs": format_ids(data_df.loc[dup_mask, id_col])
            })
        progress.progress(15)

        # multi-select group checks
        groups = group_variables(data_vars)
        for prefix, info in groups.items():
            if not info["group_type"].startswith("Multi-Select"):
                continue
            members = [m for m in info["vars"] if m in data_df.columns]
            if not members:
                continue
            block = data_df[members]
            all_missing_mask = block.isnull().all(axis=1)
            all_missing_mask = all_missing_mask | (block.applymap(lambda x: str(x).strip()=='').all(axis=1))
            def is_zeroish(x):
                try:
                    if pd.isna(x): return False
                    s = str(x).strip().lower()
                    return s in ['0','0.0','0.00','false','unchecked']
                except Exception:
                    return False
            all_zero_mask = block.applymap(is_zeroish).all(axis=1)
            allowed = set(['0','1','checked','unchecked','true','false','0.0','1.0'])
            invalid_mask = block.fillna('').astype(str).applymap(lambda x: x.strip().lower() not in allowed).any(axis=1)
            if all_missing_mask.sum() > 0:
                detailed_findings.append({
                    "Variable": prefix,
                    "Check_Type": "Multi-Select Completeness - All Missing",
                    "Description": f"{int(all_missing_mask.sum())} respondents with all values missing in multi-select group ({len(members)} items)",
                    "Affected_Count": int(all_missing_mask.sum()),
                    "Respondent_IDs": format_ids(data_df.loc[all_missing_mask, id_col])
                })
            if all_zero_mask.sum() > 0:
                detailed_findings.append({
                    "Variable": prefix,
                    "Check_Type": "Multi-Select Completeness - All Zero",
                    "Description": f"{int(all_zero_mask.sum())} respondents with all 0s in multi-select group ({len(members)} items)",
                    "Affected_Count": int(all_zero_mask.sum()),
                    "Respondent_IDs": format_ids(data_df.loc[all_zero_mask, id_col])
                })
            if invalid_mask.sum() > 0:
                detailed_findings.append({
                    "Variable": prefix,
                    "Check_Type": "Multi-Select Invalid Values",
                    "Description": f"{int(invalid_mask.sum())} respondents with invalid multi-select codes (not 0/1/checked/unchecked)",
                    "Affected_Count": int(invalid_mask.sum()),
                    "Respondent_IDs": format_ids(data_df.loc[invalid_mask, id_col])
                })
        progress.progress(35)

        # apply rules
        for _, rule in final_vr_df.iterrows():
            var = str(rule['Variable'])
            rtype = str(rule['Type']).strip().lower()
            r_applied = str(rule['Rule Applied'])
            if var not in data_df.columns:
                continue
            # Range
            if 'range' in rtype:
                m = re.match(r'^\s*(\d+)\s*[-:]\s*(\d+)\s*$', r_applied)
                lo, hi = 0, 999999
                if m:
                    lo, hi = int(m.group(1)), int(m.group(2))
                coerced = pd.to_numeric(data_df[var], errors='coerce')
                mask_out = (~coerced.isna()) & (~coerced.isin(DK_CODES)) & ((coerced < lo) | (coerced > hi))
                if mask_out.sum() > 0:
                    detailed_findings.append({
                        "Variable": var,
                        "Check_Type": "Range Violation",
                        "Description": f"{int(mask_out.sum())} values outside {lo}-{hi}",
                        "Affected_Count": int(mask_out.sum()),
                        "Respondent_IDs": format_ids(data_df.loc[mask_out, id_col])
                    })
            # Skip
elif 'skip' in rtype:
    try:
        mask = parse_skip_expression_to_mask(r_applied, data_df).astype(bool)
        ans_series = data_df[var].astype(str).str.strip()
        blank_mask = ans_series.isna() | ans_series.eq('') |   ans_series.str.lower().isin(['na','n/a','nan','none'])
        # Violation 1: should skip but answered
        violators_answered = data_df[mask & ~blank_mask]
        # Violation 2: should answer but skipped
        violators_skipped = data_df[(~mask) & blank_mask]

        if len(violators_answered) > 0:
            detailed_findings.append({
                "Variable": var,
                "Check_Type": "Skip Violation (Answered when should Skip)",
                "Description": f"{len(violators_answered)} respondents answered {var} though skip condition '{r_applied}' applies",
                "Affected_Count": int(len(violators_answered)),
                "Respondent_IDs": format_ids(violators_answered[id_col])
            })

        if len(violators_skipped) > 0:
            detailed_findings.append({
                "Variable": var,
                "Check_Type": "Skip Violation (Skipped when should Answer)",
                "Description": f"{len(violators_skipped)} respondents skipped {var} though skip condition '{r_applied}' was False",
                "Affected_Count": int(len(violators_skipped)),
                "Respondent_IDs": format_ids(violators_skipped[id_col])
            })
    except Exception as e:
        detailed_findings.append({
            "Variable": var,
            "Check_Type": "Skip Parsing Error",
            "Description": f"Could not parse skip rule: {r_applied}. Error: {e}",
            "Affected_Count": 0,
            "Respondent_IDs": ""
        })
            
            # DK/Refused
            elif 'dk' in rtype or 'ref' in rtype:
                s = data_df[var].astype(str)
                coerced = pd.to_numeric(data_df[var], errors='coerce')
                mask = s.str.strip().str.lower().isin([t.lower() for t in DK_TOKENS]) | coerced.isin(DK_CODES)
                if mask.sum() > 0:
                    detailed_findings.append({
                        "Variable": var,
                        "Check_Type": "DK/Refused",
                        "Description": f"{int(mask.sum())} DK/Refused occurrences",
                        "Affected_Count": int(mask.sum()),
                        "Respondent_IDs": format_ids(data_df.loc[mask, id_col])
                    })
            # Junk OE
            elif 'junk' in rtype or 'open' in rtype or 'oe' in rtype:
                series = data_df[var]
                mask = series.apply(lambda x: detect_junk_oe(x, junk_repeat_min, junk_min_length))
                if mask.sum() > 0:
                    detailed_findings.append({
                        "Variable": var,
                        "Check_Type": "Junk OE",
                        "Description": f"{int(mask.sum())} open-end responses flagged as junk",
                        "Affected_Count": int(mask.sum()),
                        "Respondent_IDs": format_ids(data_df.loc[mask, id_col])
                    })
            # Multi-Select & None handled above

        progress.progress(70)

        # straightliner (rating grids)
        prefixes = {}
        for v in data_vars:
            p = re.split(r'[_\.]', v)[0]
            prefixes.setdefault(p, []).append(v)
        for prefix, cols in prefixes.items():
            gi = groups.get(prefix, {})
            if gi and gi.get("group_type","").startswith("Rating Grid"):
                sliners = find_straightliners(data_df, gi.get("vars", cols), threshold=straightliner_threshold)
                if sliners:
                    idxs = list(sliners.keys())
                    detailed_findings.append({
                        "Variable": prefix,
                        "Check_Type": "Straightliner (Grid)",
                        "Description": f"{len(sliners)} respondents flagged as straightliners across {len(gi.get('vars', cols))} items",
                        "Affected_Count": int(len(sliners)),
                        "Respondent_IDs": format_ids(pd.Series(idxs))
                    })
        progress.progress(90)

        # build and save report
        detailed_df = pd.DataFrame(detailed_findings) if detailed_findings else pd.DataFrame(columns=["Variable","Check_Type","Description","Affected_Count","Respondent_IDs"])
        summary_df = detailed_df.groupby("Check_Type", as_index=False)["Affected_Count"].sum().sort_values("Affected_Count", ascending=False) if not detailed_df.empty else pd.DataFrame(columns=["Check_Type","Affected_Count"])
        project_info = pd.DataFrame({
            "Report Generated":[datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")],
            "Raw Data Rows":[raw_df.shape[0]],
            "Raw Data Columns":[raw_df.shape[1]],
            "Respondent ID":[id_col],
            "Variables Validated":[len(data_vars)]
        })

        # ensure rules_buf exists
        if st.session_state.get("rules_buf") is None and final_vr_df is not None:
            try:
                buf_r = io.BytesIO()
                engine_choice = "xlsxwriter" if XLSXWRITER_AVAILABLE else "openpyxl"
                with pd.ExcelWriter(buf_r, engine=engine_choice) as writer:
                    final_vr_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
                buf_r.seek(0)
                st.session_state["rules_buf"] = buf_r.getvalue()
            except Exception:
                pass

        report_buf = io.BytesIO()
        try:
            engine_choice = "xlsxwriter" if XLSXWRITER_AVAILABLE else "openpyxl"
            with pd.ExcelWriter(report_buf, engine=engine_choice) as writer:
                detailed_df.to_excel(writer, sheet_name="Detailed Checks", index=False)
                summary_df.to_excel(writer, sheet_name="Summary", index=False)
                final_vr_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
                project_info.to_excel(writer, sheet_name="Project Info", index=False)
                if XLSXWRITER_AVAILABLE:
                    workbook = writer.book
                    header_fmt = workbook.add_format({'bold': True, 'bg_color': '#305496', 'font_color': 'white', 'border':1})
                    sheet_map = {"Detailed Checks": detailed_df, "Summary": summary_df, "Validation_Rules": final_vr_df, "Project Info": project_info}
                    for sheet_name, df_sheet in sheet_map.items():
                        try:
                            ws = writer.sheets[sheet_name]
                            ws.freeze_panes(1,1)
                            for col_num, value in enumerate(df_sheet.columns.values):
                                ws.write(0, col_num, value, header_fmt)
                            for i, col in enumerate(df_sheet.columns):
                                try:
                                    width = max(df_sheet[col].astype(str).map(len).max(), len(str(col))) + 2
                                except Exception:
                                    width = len(str(col)) + 2
                                try:
                                    ws.set_column(i, i, min(80, width))
                                except Exception:
                                    pass
                        except Exception:
                            pass
            report_buf.seek(0)
            st.session_state["report_buf"] = report_buf.getvalue()
            st.session_state["detailed_df_preview"] = detailed_df.copy()
            st.success("Validation Report generated and saved in session.")
        except Exception as e:
            st.error("Could not prepare Validation Report: " + str(e))
            st.session_state["report_buf"] = None

# ---------------- Preview + Downloads ----------------
if st.session_state.get("detailed_df_preview") is not None:
    st.subheader("Detailed Checks — Preview (first 200 rows)")
    try:
        st.dataframe(st.session_state["detailed_df_preview"].head(200), use_container_width=True)
    except Exception:
        st.write(st.session_state["detailed_df_preview"].head(200))
    cols = st.columns(2)
    with cols[0]:
        if st.session_state.get("rules_buf") is not None:
            st.download_button("📥 Download Validation Rules.xlsx", data=io.BytesIO(st.session_state["rules_buf"]), file_name="Validation Rules.xlsx", mime="application/vnd.openxmlformats-officedocument-spreadsheetml.sheet")
        else:
            st.info("Validation Rules file not available for download.")
    with cols[1]:
        if st.session_state.get("report_buf") is not None:
            st.download_button("📥 Download Validation Report.xlsx", data=io.BytesIO(st.session_state["report_buf"]), file_name="Validation Report.xlsx", mime="application/vnd.openxmlformats-officedocument-spreadsheetml.sheet")
        else:
            st.info("Validation Report file not available for download yet")

# EOF
'''
out_path = Path("/mnt/data/29octapp.py")
out_path.write_text(content)
out_path

