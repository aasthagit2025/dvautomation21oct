# app.py
import streamlit as st
import pandas as pd
import pyreadstat
import io
import re
import numpy as np

st.set_page_config(layout="wide")
st.title("📊 Auto Validation Rules + Failed Checks Generator")

# ---------- Uploads ----------
st.markdown("Upload raw survey data (CSV / XLSX / SAV)")
data_file = st.file_uploader("Raw data file", type=["csv", "xlsx", "sav"])

st.markdown("Upload skip rules file (CSV / XLSX) — the condition text will be read from **Check_Type** (fallback: Logic/Condition/ConditionText)")
skip_file = st.file_uploader("Skip rules file (optional)", type=["csv", "xlsx"])

st.markdown("Upload Constructed List export (TXT) — optional (used to suggest list-based rules)")
constructed_txt = st.file_uploader("Constructed list export (optional)", type=["txt"])

# ---------- Helpers ----------
def read_data(file):
    if file is None:
        return None
    name = file.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(file, encoding_errors="ignore")
    elif name.endswith(".xlsx") or name.endswith(".xls"):
        return pd.read_excel(file)
    elif name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(file)
        return df
    else:
        raise ValueError("Unsupported file type")

def detect_id_column(df):
    for candidate in ["RespondentID", "Password", "RespID", "RID", "Sys_RespNum", "sys_RespNum", "sys_respnum"]:
        if candidate in df.columns:
            return candidate
    # fallback: first column likely ID
    return df.columns[0]

def read_skip_df(file):
    if file is None:
        return pd.DataFrame(columns=["SkipFrom", "Check_Type", "Logic", "SkipTo"])
    if file.name.lower().endswith(".csv"):
        return pd.read_csv(file, encoding_errors="ignore")
    else:
        return pd.read_excel(file)

# Condition parsing: supports =, !=, <>, <, >, <=, >=, NOT(...), AND/OR, parentheses basic
def get_condition_mask(cond_text, df):
    """Return boolean Series where condition is TRUE.
       Accepts 'If A=1 and (B>2 or C<>3)' or forms using NOT(...)."""
    if not isinstance(cond_text, str) or cond_text.strip() == "":
        return pd.Series(True, index=df.index)  # empty condition -> everyone
    text = cond_text.strip()
    # Remove leading If (case-insensitive)
    if text.lower().startswith("if"):
        text = text[2:].strip()
    # handle NOT( ... ): Replace Not(expr) with a special token approach by recursion
    # We'll use a simple recursive evaluator by tokenizing OR groups and AND groups,
    # and supporting 'Not(...)' at top-level parts.
    def eval_part(part):
        part = part.strip()
        # NOT wrapper
        m_not = re.match(r'^\s*not\s*\((.*)\)\s*$', part, flags=re.IGNORECASE)
        if m_not:
            return ~get_condition_mask("If " + m_not.group(1), df)
        # Parentheses: delegate to get_condition_mask
        if part.startswith("(") and part.endswith(")"):
            return get_condition_mask("If " + part[1:-1], df)
        # normalize operators
        part = part.replace("<>", "!=")
        # match op
        for op in ["<=", ">=", "!=", "<", ">", "="]:
            if op in part:
                left, right = [p.strip() for p in part.split(op, 1)]
                # if left not in df: false mask
                if left not in df.columns:
                    return pd.Series(False, index=df.index)
                colvals = df[left]
                # try numeric compare
                try:
                    rval = float(right)
                    colnum = pd.to_numeric(colvals, errors="coerce")
                    if op == "<=":
                        return colnum <= rval
                    if op == ">=":
                        return colnum >= rval
                    if op == "<":
                        return colnum < rval
                    if op == ">":
                        return colnum > rval
                    if op == "=":
                        return colnum == rval
                    if op == "!=":
                        return colnum != rval
                except Exception:
                    # string compare (preserve NA literal - treat "NA"/"N/A"/"nan" as strings)
                    rval_s = right.strip().strip('"').strip("'")
                    if op in ("!=", "!="):
                        return colvals.astype(str).str.strip() != rval_s
                    else:
                        return colvals.astype(str).str.strip() == rval_s
        # fallback: unknown -> false
        return pd.Series(False, index=df.index)

    # Split OR groups (top-level)
    or_groups = re.split(r'\s+or\s+', text, flags=re.IGNORECASE)
    mask = pd.Series(False, index=df.index)
    for og in or_groups:
        and_parts = re.split(r'\s+and\s+', og, flags=re.IGNORECASE)
        sub = pd.Series(True, index=df.index)
        for p in and_parts:
            sub &= eval_part(p)
        mask |= sub
    return mask

def is_literal_na_string(x):
    if pd.isna(x):
        return False
    s = str(x).strip().lower()
    return s in {"na", "n/a", "nan"}

def blank_mask_for_column(df, col):
    """Define blank = actual NaN or empty string ONLY.
       Literal 'NA' or 'N/A' are treated as valid answered values (not blank)."""
    series = df[col]
    # actual NaN
    m_nan = series.isna()
    # empty string after stripping
    m_empty = series.astype(str).str.strip() == ""
    # but treat literal NA strings as NOT blank
    m_literal_na = series.astype(str).str.strip().str.lower().isin({"na", "n/a", "nan"})
    return (m_nan | m_empty) & (~m_literal_na)

# ---------- Main UI actions ----------
if data_file is None:
    st.info("Upload raw data to get started.")
    st.stop()

# read data
df = read_data(data_file)
id_col = detect_id_column(df)

# read skips
skip_df = read_skip_df(skip_file) if skip_file else pd.DataFrame()
# normalize skip text column: primary 'Check_Type', fallback 'Logic'/'Condition'/'ConditionText'
skip_condition_col = None
for c in ["Check_Type", "Logic", "Condition", "ConditionText", "CheckType"]:
    if c in skip_df.columns:
        skip_condition_col = c
        break

# read constructed text (optional) for additional rules suggestions
constructed_text = None
if constructed_txt is not None:
    try:
        constructed_text = constructed_txt.read().decode("utf-8")
    except Exception:
        try:
            constructed_text = constructed_txt.read().decode("latin1")
        except Exception:
            constructed_text = constructed_txt.read()  # may be str already

# ---------- Auto-generate rules ----------
st.header("Generated validation rules (editable)")
generated = []

# helper to find multi-select prefix groups
def prefixes_from_cols(cols):
    # find prefixes that appear with several numeric suffixes like Q2_1, Q2_2 etc.
    prefixes = {}
    for c in cols:
        m = re.match(r"^([A-Za-z0-9_]+?)(_r?\d+|_\d+)$", c)
        if m:
            prefix = m.group(1) + "_"
            prefixes.setdefault(prefix, []).append(c)
    # also support prefix with trailing underscore Q3_ etc.
    # return prefixes appearing >=2
    return {p: v for p, v in prefixes.items() if len(v) >= 2}

multi_prefixes = prefixes_from_cols(df.columns)

# Walk columns in data order and create rules
for col in df.columns:
    # skip ID column from rules generation
    if col == id_col:
        continue

    # determine related cols: if prefix present use all with prefix; else single
    related = [col]
    # if this column looks like a prefix root (endswith underscore) or matches a prefix base
    for pref, members in multi_prefixes.items():
        if col.startswith(pref) or col in members:
            related = sorted(members)
            break

    # detect question type heuristics
    sample_nonnull = df[related].dropna().astype(str).apply(lambda s: s.str.strip()).stack().reset_index(drop=True)
    # if prefix group or many columns -> multi-select
    if len(related) > 1:
        qtype = "Multi-Select"
    else:
        # single column heuristics
        unique_vals = pd.Series(df[col].dropna().astype(str).str.strip().unique())
        # numeric-like with few unique values (<=10) -> single-select/rating
        numeric_vals = pd.to_numeric(df[col], errors="coerce")
        if numeric_vals.notna().sum() > 0 and len(unique_vals) <= 10:
            qtype = "Rating/Single-Select"
        else:
            # long text -> Open-end
            avg_len = df[col].astype(str).str.len().mean()
            if avg_len > 20:
                qtype = "Open-End"
            else:
                qtype = "Single-Select"

    # build suggested checks for this col
    checks = []
    conditions_list = []

    # If multi-select: add Multi-Select rule(s)
    if len(related) > 1:
        checks.append("Multi-Select")
        conditions_list.append("Only 0/1; At least one selected")
    else:
        # Rating/single-select: add Range if numeric and small range
        if qtype in ("Rating/Single-Select", "Single-Select", "Rating/Single-Select"):
            # attempt to infer valid integer range from the unique numeric values
            vals = pd.to_numeric(df[col], errors="coerce").dropna().unique()
            if len(vals) > 0 and all((vals.astype(float) == np.floor(vals)).tolist()):
                vals_sorted = np.sort(vals.astype(int))
                minv, maxv = int(vals_sorted.min()), int(vals_sorted.max())
                # only create range if reasonable (max-min <= 10)
                if maxv - minv <= 20:
                    checks.append("Range")
                    conditions_list.append(f"{minv}-{maxv}")
            # Missing should be checked for single-select/rating
            checks.append("Missing")
            conditions_list.append("")
        elif qtype == "Open-End":
            checks.append("Missing")
            conditions_list.append("")
            checks.append("OpenEnd_Junk")
            conditions_list.append("MinLen(3)")

    # Straightliner for rating groups: if these are part of a row/column group, suggest Straightliner
    # if there are multiple related columns belonging to same question family, suggest Straightliner
    prefix_root = None
    if len(related) > 1:
        # add Straightliner only once per group (attached to prefix name)
        checks.append("Straightliner")
        conditions_list.append("Group(%s_prefix?)" % related[0].split("_")[0])

    # Now find skip rules from skip_df referencing this column
    # We treat skip condition text coming from skip_condition_col if present
    if not skip_df.empty and skip_condition_col:
        # search skip_df rows where the 'Skip To' or 'SkipTo' or 'Skip To' or any text contains this column name
        possible_target_cols = []
        for c in ["Skip To", "SkipTo", "Skip_To", "SkipToVar", "SkipToQuestion", "SkipToCol", "Skip To "]:
            if c in skip_df.columns:
                possible_target_cols.append(c)
        # also try "Skip From" or "Question" columns to identify target
        if len(possible_target_cols) == 0:
            # fallback: check anywhere in row texts
            for idx, r in skip_df.iterrows():
                row_join = " ".join([str(x) for x in r.tolist() if pd.notna(x)])
                # if column name appears in skip row text, treat as relevant
                if re.search(r'\b' + re.escape(col) + r'\b', row_join):
                    # take the skip condition text from skip_condition_col
                    cond_text = str(r[skip_condition_col])
                    # we will append a Skip check referencing this condition
                    checks.append("Skip")
                    conditions_list.append(cond_text)
                    # we stop after first matching skip for simplicity
                    break
        else:
            # use explicit Skip To columns
            for idx, r in skip_df.iterrows():
                for tcol in possible_target_cols:
                    try:
                        tgt = str(r[tcol])
                    except Exception:
                        tgt = ""
                    if tgt and re.search(r'\b' + re.escape(col) + r'\b', tgt):
                        cond_text = str(r.get(skip_condition_col, ""))
                        checks.append("Skip")
                        conditions_list.append(cond_text)
                        break
                else:
                    continue
                break

    # Save generated rule entries
    # for multi-related columns, attach rule to the prefix (use prefix without numeric suffix)
    rule_question = related[0] if len(related) == 1 else related[0].rsplit("_", 1)[0] + "_"
    if len(checks) == 0:
        # at least put Missing as fallback
        checks = ["Missing"]
        conditions_list = [""]
    # dedupe preserve order
    seen = set()
    checks_u, conds_u = [], []
    for ck, cd in zip(checks, conditions_list):
        if ck not in seen:
            checks_u.append(ck)
            conds_u.append(cd)
            seen.add(ck)
    generated.append({"Question": rule_question, "Check_Type": ";".join(checks_u), "Condition": ";".join([c for c in conds_u if c is not None]), "Source": "Auto (data-driven)"})

# Convert to DataFrame and present editable
rules_df = pd.DataFrame(generated)

# show rules (user can edit)
try:
    edited = st.data_editor(rules_df, num_rows="dynamic", use_container_width=True)
except AttributeError:
    edited = st.experimental_data_editor(rules_df, num_rows="dynamic", use_container_width=True)


st.markdown("### Preview / Run Validation")
run = st.button("Run validation with current rules")

# ---------- Validation Engine ----------
def parse_rule_row_to_checks(row):
    """Return list of tuples (check_type, condition)"""
    q = str(row["Question"])
    cts = [x.strip() for x in str(row.get("Check_Type", "")).split(";") if x.strip()]
    conds = [x.strip() for x in str(row.get("Condition", "")).split(";")] if "Condition" in row else ["" for _ in cts]
    # align lengths
    if len(conds) < len(cts):
        conds += [""] * (len(cts) - len(conds))
    return list(zip(cts, conds))

def expand_question_to_cols(q, df_columns):
    # If q ends with '_' treat as prefix
    if q.endswith("_"):
        return [c for c in df_columns if c.startswith(q)]
    # if q exactly exists return single
    if q in df_columns:
        return [q]
    # try range pattern like Q3_1 to Q3_13 or Q3_1 to Q3_3
    m = re.search(r'(\w+\d+)\s+to\s+(\w+\d+)', q, flags=re.IGNORECASE)
    if m:
        return expand_range_cols(m.group(1), m.group(2), df_columns)
    # fallback: return any columns that start with q as prefix
    return [c for c in df_columns if c.startswith(q)]

def expand_range_cols(start, end, cols):
    m1 = re.match(r'([A-Za-z_]+?)(\d+)$', start)
    m2 = re.match(r'([A-Za-z_]+?)(\d+)$', end)
    if m1 and m2 and m1.group(1) == m2.group(1):
        pref = m1.group(1)
        s = int(m1.group(2)); e = int(m2.group(2))
        return [f"{pref}{i}" for i in range(s, e+1) if f"{pref}{i}" in cols]
    return [start] if start in cols else []

if run:
    report = []
    rules_to_apply = edited.copy()
    # For each rule, apply checks
    for _, r in rules_to_apply.iterrows():
        q_expr = str(r["Question"])
        checks = parse_rule_row_to_checks(r)
        target_cols = expand_question_to_cols(q_expr, df.columns)
        if len(target_cols) == 0:
            # record dataset level missing variable
            report.append({id_col: None, "Question": q_expr, "Check_Type": "Rule", "Issue": "Question/Prefix not found in dataset"})
            continue

        # Determine skip mask if Skip present in rule
        skip_checks = [c for c in checks if c[0].lower() == "skip"]
        should_answer_mask = None  # True for rows who should answer
        if skip_checks:
            # consider only first skip rule (if multiple, they can be combined, but we handle sequentially)
            skip_cond_text = skip_checks[0][1]
            # normalize then/if parsing
            if re.search(r'\bthen\b', skip_cond_text, flags=re.IGNORECASE):
                if_part, then_part = re.split(r'(?i)then', skip_cond_text, maxsplit=1)
            else:
                # if no 'then' assume whole cond_text is "If <cond> then <q> should be answered" style; try to find If <cond>
                if_part = skip_cond_text
                then_part = ""
            # Determine if 'then' requests answered or blank by checking text in then_part
            should_be_answered = True
            if then_part and re.search(r'\bblank\b', then_part, flags=re.IGNORECASE):
                should_be_answered = False
            # compute if_part mask
            mask_if = get_condition_mask(if_part, df)
            if should_be_answered:
                should_answer_mask = mask_if.copy()
            else:
                should_answer_mask = ~mask_if

            # now check skip-specific offenders for each target col
            for col in target_cols:
                bm = blank_mask_for_column(df, col)
                # offenders: should answer but blank
                offenders = df.loc[should_answer_mask & bm, id_col]
                for rid in offenders:
                    report.append({id_col: rid, "Question": col, "Check_Type": "Skip", "Issue": "Blank but should be answered"})
                # offenders: should NOT answer but answered
                not_answer_mask = ~should_answer_mask
                answered_mask = ~blank_mask_for_column(df, col)
                offenders2 = df.loc[not_answer_mask & answered_mask, id_col]
                for rid in offenders2:
                    report.append({id_col: rid, "Question": col, "Check_Type": "Skip", "Issue": "Answered but should be blank"})

        # Decide rows to evaluate for other checks: only those who should answer if skip present,
        # otherwise all rows.
        rows_mask = should_answer_mask if should_answer_mask is not None else pd.Series(True, index=df.index)

        # apply other checks
        for ck, cond in checks:
            ck = ck.strip()
            if ck.lower() == "skip":
                continue
            if ck.lower() == "range":
                # cond like "1-5" or "1-3"
                for col in target_cols:
                    try:
                        if "-" in cond:
                            lo, hi = cond.split("-", 1)
                        elif "to" in cond:
                            lo, hi = cond.split("to", 1)
                        else:
                            raise ValueError("Invalid range format")
                        lof = float(lo); hif = float(hi)
                        colnum = pd.to_numeric(df[col], errors="coerce")
                        valid = colnum.between(lof, hif)
                        offenders = df.loc[rows_mask & ~valid & ~colnum.isna(), id_col]
                        # we also want to treat blank as Missing (handled by Missing check), so skip here
                        for rid in offenders:
                            report.append({id_col: rid, "Question": col, "Check_Type": "Range", "Issue": f"Value out of range ({lof}-{hif})"})
                    except Exception:
                        report.append({id_col: None, "Question": col, "Check_Type": "Range", "Issue": f"Invalid range condition ({cond})"})
            elif ck.lower() == "missing":
                for col in target_cols:
                    bm = blank_mask_for_column(df, col)
                    offenders = df.loc[rows_mask & bm, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": col, "Check_Type": "Missing", "Issue": "Value is missing"})
            elif ck.lower() == "multi-select":
                # target is prefix; ensure we have related columns
                related_cols = target_cols if len(target_cols) > 1 else [c for c in df.columns if c.startswith(q_expr)]
                if len(related_cols) == 0:
                    report.append({id_col: None, "Question": q_expr, "Check_Type": "Multi-Select", "Issue": "No related multi-select columns found"})
                else:
                    # check only 0/1 values and at least one selected for rows_mask
                    for col in related_cols:
                        offenders = df.loc[rows_mask & (~df[col].isin([0, 1]) & ~df[col].astype(str).str.strip().isin({"0","1"})), id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": col, "Check_Type": "Multi-Select", "Issue": "Invalid value (not 0/1)"})
                    # at-least-one: sum across cols after treating blanks as 0
                    summed = df[related_cols].fillna(0)
                    # ensure numeric
                    try:
                        summed = summed.astype(float).sum(axis=1)
                        offenders = df.loc[rows_mask & (summed == 0), id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": q_expr, "Check_Type": "Multi-Select", "Issue": "No options selected"})
                    except Exception:
                        # if cannot cast to float, try string-based non-empty
                        summed_nonempty = (df[related_cols].astype(str).apply(lambda x: x.str.strip() != "").any(axis=1))
                        offenders = df.loc[rows_mask & (~summed_nonempty), id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": q_expr, "Check_Type": "Multi-Select", "Issue": "No options selected"})
            elif ck.lower() == "openend_junk":
                for col in target_cols:
                    # treat NA literal as answered (not junk)
                    is_junk = ~(df[col].astype(str).str.strip().str.lower().isin({"na","n/a","nan"})) & (df[col].astype(str).str.len() < 3)
                    offenders = df.loc[rows_mask & is_junk, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": col, "Check_Type": "OpenEnd_Junk", "Issue": "Open-end looks like junk/low-effort"})
            elif ck.lower() == "straightliner":
                # expand the prefix if needed
                if len(target_cols) == 1:
                    pref = target_cols[0]
                    related_cols = [c for c in df.columns if c.startswith(pref.rstrip("_"))]
                else:
                    related_cols = target_cols
                if len(related_cols) > 1:
                    same_resp = df[related_cols].nunique(axis=1) == 1
                    offenders = df.loc[rows_mask & same_resp, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": ",".join(related_cols), "Check_Type": "Straightliner", "Issue": "Same response across all items"})
            elif ck.lower() == "duplicate":
                for col in target_cols:
                    dupes = df.loc[rows_mask & df.duplicated(subset=[col], keep=False), id_col]
                    for rid in dupes:
                        report.append({id_col: rid, "Question": col, "Check_Type": "Duplicate", "Issue": "Duplicate value found"})
            else:
                # unknown check: ignore or record
                pass

    # final report
    report_df = pd.DataFrame(report)
    if report_df.empty:
        st.success("No failed checks found.")
    else:
        st.error(f"Failed checks: {len(report_df)}")
    st.dataframe(report_df)

    # Download generated rules (edited) and failed checks
    out_buf = io.BytesIO()
    with pd.ExcelWriter(out_buf, engine="openpyxl") as writer:
        edited.to_excel(writer, index=False, sheet_name="Generated_Rules")
        report_df.to_excel(writer, index=False, sheet_name="Failed_Checks")
    st.download_button("📥 Download rules + failed checks (Excel)", data=out_buf.getvalue(),
                       file_name="validation_rules_and_failed_checks.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.info("You can edit the generated rules above and click **Run validation** when ready.")
