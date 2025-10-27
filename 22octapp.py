# app.py
import streamlit as st
import pandas as pd
import pyreadstat
import io
import re

st.set_page_config(page_title="Auto Validation Rules + Failed Checks Generator", layout="wide")
st.title("📊 Auto Validation Rules + Failed Checks Generator")

# --- Upload UI ---
st.markdown("Upload raw survey data (CSV / XLSX / SAV) and validation rules (Excel). Rules format must be:")
st.markdown("`Question | Check_Type | Condition` (multiple check types allowed separated by `;`)")

col1, col2 = st.columns(2)
with col1:
    raw_file = st.file_uploader("Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx", "sav"])
with col2:
    rules_file = st.file_uploader("Upload validation rules (Excel, .xlsx)", type=["xlsx"])

# --- Helpers ---
def read_data(file):
    """Read CSV/XLSX/SAV. For CSV, do not convert literal 'NA' to NaN (keep as string)."""
    if file.name.lower().endswith(".csv"):
        # keep_default_na=False ensures literal NA stays as string "NA"
        return pd.read_csv(file, encoding_errors="ignore", keep_default_na=False)
    elif file.name.lower().endswith(".xlsx"):
        return pd.read_excel(file)
    elif file.name.lower().endswith(".sav"):
        df, meta = pyreadstat.read_sav(file)
        return df
    else:
        raise ValueError("Unsupported file type")

def detect_id_col(df):
    for c in ["RespondentID", "Password", "RespID", "RID", "sys_RespNum", "Respondent"]:
        if c in df.columns:
            return c
    # fallback to first column if nothing matches
    return df.columns[0]

def expand_prefix(prefix, cols):
    # if prefix ends with '_' treat as prefix, else if prefix itself is column that's returned
    if prefix.endswith("_"):
        return [c for c in cols if c.startswith(prefix)]
    # also support prefix like 'Q9_' provided in rules
    if prefix + "" in cols and prefix in cols:
        return [prefix]
    return [c for c in cols if c.startswith(prefix)]

def expand_range_expr(expr, cols):
    """
    Expand expressions like 'Q3_1 to Q3_13' -> list of cols between.
    If expr is single column, return [expr] if present.
    """
    expr = expr.strip()
    m = re.search(r'([A-Za-z0-9_]+?)(\d+)\s+to\s+([A-Za-z0-9_]+?)(\d+)', expr, flags=re.IGNORECASE)
    if m:
        p1, s1, p2, s2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if p1 == p2:
            return [f"{p1}{i}" for i in range(s1, s2+1) if f"{p1}{i}" in cols]
    # if expr like "Q3_1 to Q3_13" with underscore near numbers also handled above
    # fallback: if expr contains 'to' but parsing failed, attempt token split
    if " to " in expr.lower():
        parts = [p.strip() for p in expr.split("to")]
        if len(parts) == 2:
            start, end = parts
            base_start = re.match(r'([A-Za-z0-9_]+?)(\d+)$', start)
            base_end = re.match(r'([A-Za-z0-9_]+?)(\d+)$', end)
            if base_start and base_end and base_start.group(1) == base_end.group(1):
                prefix = base_start.group(1)
                s = int(base_start.group(2)); e = int(base_end.group(2))
                return [f"{prefix}{i}" for i in range(s, e+1) if f"{prefix}{i}" in cols]
    # otherwise, return expr if present as column, or expand prefix if expr endswith '_'
    if expr in cols:
        return [expr]
    if expr.endswith("_"):
        return [c for c in cols if c.startswith(expr)]
    return []

def get_condition_mask(cond_text, df):
    """
    Parse condition text like 'If Q4_r1=10 or Q4_r1=11 and Q2_1=1' and return boolean mask.
    Supports operators: <=, >=, !=, <>, =, <, >
    """
    if not cond_text or str(cond_text).strip() == "":
        return pd.Series(True, index=df.index)
    txt = cond_text.strip()
    if txt.lower().startswith("if"):
        txt = txt[2:].strip()
    # normalize Not(...) to use != logic if necessary - we'll keep simple: support not(...) with regex
    # Split on OR at top level
    or_groups = re.split(r'\s+or\s+', txt, flags=re.IGNORECASE)
    mask = pd.Series(False, index=df.index)
    for or_group in or_groups:
        and_parts = re.split(r'\s+and\s+', or_group, flags=re.IGNORECASE)
        sub = pd.Series(True, index=df.index)
        for part in and_parts:
            part = part.strip()
            part = part.replace("<>", "!=")
            # find operator
            matched = False
            for op in ["<=", ">=", "!=", "<", ">", "="]:
                if op in part:
                    left, right = [p.strip() for p in part.split(op, 1)]
                    # handle Not(...) or Not(X=1) formats roughly: if left starts with Not( then invert
                    invert = False
                    if left.lower().startswith("not(") and left.endswith(")"):
                        inner = left[4:-1]
                        left = inner.strip()
                        invert = True
                    if left not in df.columns:
                        # treat as False for this part
                        sub &= False
                        matched = True
                        break
                    col_vals = df[left]
                    # try numeric compare first
                    try:
                        right_num = float(right)
                        col_num = pd.to_numeric(col_vals, errors="coerce")
                        if op == "<=":
                            part_mask = col_num <= right_num
                        elif op == ">=":
                            part_mask = col_num >= right_num
                        elif op == "<":
                            part_mask = col_num < right_num
                        elif op == ">":
                            part_mask = col_num > right_num
                        elif op == "=":
                            part_mask = col_num == right_num
                        elif op == "!=":
                            part_mask = col_num != right_num
                    except Exception:
                        # string comparison (case-insensitive)
                        if op in ["!=", "="]:
                            if op == "=":
                                part_mask = col_vals.astype(str).str.strip() == right
                            else:
                                part_mask = col_vals.astype(str).str.strip() != right
                        else:
                            # unsupported non-numeric inequality on strings -> False
                            part_mask = pd.Series(False, index=df.index)
                    if invert:
                        part_mask = ~part_mask
                    sub &= part_mask.fillna(False)
                    matched = True
                    break
            if not matched:
                # unknown part -> treat as False
                sub &= False
        mask |= sub
    return mask.fillna(False)

def is_blank_series(s):
    """Blank = NaN or empty string. But literal 'NA' (case-insensitive) is NOT blank."""
    s_str = s.astype(str)
    blank = s.isna() | (s_str.str.strip() == "")
    # If original value was string "NA" (not NaN), blank should be False
    is_na_literal = s_str.str.upper() == "NA"
    blank = blank & (~is_na_literal)
    return blank

# --- Main flow ---
if raw_file and rules_file:
    try:
        df = read_data(raw_file)
    except Exception as e:
        st.error(f"Error reading data file: {e}")
        st.stop()

    try:
        rules_df = pd.read_excel(rules_file, dtype=str).fillna("")
    except Exception as e:
        st.error(f"Error reading rules Excel: {e}")
        st.stop()

    id_col = detect_id_col(df)
    st.info(f"Detected Respondent ID column: `{id_col}` (change in code if wrong)")

    # Show uploaded rules preview and allow small edits
    st.markdown("### Preview / edit uploaded rules (you can modify then re-run)")
    st.dataframe(rules_df.head(200))

    # Option to save generated rules (we'll generate baseline rules from uploaded file)
    if st.button("Generate validation rules and run validation"):
        # Build generated rules (we use rules_df as authoritative; if automation needed later, we can auto-gen)
        gen_rules = []
        # Normalize rules_df columns
        rules_df.columns = [c.strip() for c in rules_df.columns]
        for _, row in rules_df.iterrows():
            q = str(row.get("Question", "")).strip()
            ctype = str(row.get("Check_Type", "")).strip()
            cond = str(row.get("Condition", "")).strip()
            if q == "":
                continue
            gen_rules.append({"Question": q, "Check_Type": ctype, "Condition": cond, "Source": "UserRules"})

        gen_rules_df = pd.DataFrame(gen_rules)

        # Run validation based on gen_rules_df
        report = []
        cols = df.columns.tolist()

        for _, r in gen_rules_df.iterrows():
            q = r["Question"].strip()
            check_types = [c.strip() for c in r["Check_Type"].split(";") if c.strip()]
            conditions = [c.strip() for c in r["Condition"].split(";")] if r["Condition"] else []

            # derive related columns
            if q in cols:
                related_cols = [q]
            else:
                # prefix style: if endswith '_' treat as prefix
                if q.endswith("_"):
                    related_cols = [c for c in cols if c.startswith(q)]
                else:
                    # also if user wrote like 'Q9_' but without trailing underscore
                    related_cols = [c for c in cols if c.startswith(q)]
                if not related_cols:
                    # if nothing matches, still keep q as target (to report missing variable)
                    related_cols = [q]

            # If there's a skip in check_types, evaluate skip first to get mask of those who SHOULD answer
            skip_mask = None
            if any(ct.lower() == "skip" for ct in check_types):
                # find index of first skip in check_types to match condition position
                try:
                    skip_idx = [i for i,ct in enumerate(check_types) if ct.lower()=="skip"][0]
                    skip_cond = conditions[skip_idx] if skip_idx < len(conditions) else ""
                    if "then" not in skip_cond.lower():
                        # invalid skip format -> report
                        report.append({id_col: None, "Question": q, "Check_Type": "Skip", "Issue": f"Invalid skip condition (missing 'then'): {skip_cond}"})
                        skip_mask = None
                    else:
                        if_part, then_part = re.split(r'(?i)then', skip_cond, maxsplit=1)
                        skip_mask = get_condition_mask(if_part, df)

                        # parse then_part to get target columns to which skip applies
                        then_part = then_part.strip()
                        # extracts first token (like Q3_1 or Q3_ or 'Q3_1 to Q3_13')
                        # Try to find range pattern 'X to Y' in then_part
                        target_cols = []
                        # if 'to' in then_part, attempt expand_range_expr
                        if re.search(r'\bto\b', then_part, flags=re.IGNORECASE):
                            target_cols = expand_range_expr(then_part, cols)
                        else:
                            # take the first token (alphanumeric/underscore and optional trailing '_')
                            m = re.search(r'([A-Za-z0-9_]+_?\d*_|[A-Za-z0-9_]+)', then_part)
                            if m:
                                token = m.group(0).strip()
                                if token.endswith("_"):
                                    target_cols = [c for c in cols if c.startswith(token)]
                                elif token in cols:
                                    target_cols = [token]
                                else:
                                    # maybe token is like Q3_1 (exists) or Q3_1 to Q3_13 handled above
                                    if token in cols:
                                        target_cols = [token]
                        # if none found, fallback: if q itself expands to multiple cols treat them
                        if not target_cols and q.endswith("_"):
                            target_cols = [c for c in cols if c.startswith(q)]
                        # If still empty, try to extract any column-like tokens
                        if not target_cols:
                            tokens = re.findall(r'[A-Za-z0-9_]+', then_part)
                            for t in tokens:
                                if t in cols:
                                    target_cols.append(t)
                        # Now apply the skip logic checks for each target column found
                        for tcol in target_cols:
                            if tcol not in cols:
                                report.append({id_col: None, "Question": tcol, "Check_Type": "Skip", "Issue": "Target variable not found in dataset"})
                                continue
                            blank_mask = is_blank_series(df[tcol])
                            answered_mask = ~blank_mask
                            should_be_blank = "blank" in then_part.lower()
                            if should_be_blank:
                                # If condition true -> should be blank; offenders are condition true AND answered
                                offenders = df.loc[skip_mask & answered_mask, id_col]
                                for rid in offenders:
                                    report.append({id_col: rid, "Question": tcol, "Check_Type": "Skip", "Issue": "Answered but should be blank"})
                            else:
                                # should be answered -> condition true AND blank => offender
                                offenders = df.loc[skip_mask & blank_mask, id_col]
                                for rid in offenders:
                                    report.append({id_col: rid, "Question": tcol, "Check_Type": "Skip", "Issue": "Blank but should be answered"})
                except Exception as e:
                    report.append({id_col: None, "Question": q, "Check_Type": "Skip", "Issue": f"Error parsing skip: {e}"})

            # rows_to_check: only check range/missing/etc for respondents who SHOULD answer
            rows_to_check = skip_mask if skip_mask is not None else pd.Series(True, index=df.index)

            # Now evaluate other checks
            for idx, ct in enumerate(check_types):
                ct_lower = ct.lower()
                cond = conditions[idx] if idx < len(conditions) else ""
                if ct_lower == "skip":
                    continue
                if ct_lower == "range":
                    # cond expected like 1-5 or "1-11" or "1 to 5"
                    try:
                        # normalize
                        condn = cond.replace("to", "-").strip()
                        if "-" not in condn:
                            raise ValueError("Range condition missing '-' or 'to'")
                        minv, maxv = [float(x.strip()) for x in condn.split("-", 1)]
                        for col in related_cols:
                            if col not in cols:
                                report.append({id_col: None, "Question": col, "Check_Type": "Range", "Issue": "Variable not found in data"})
                                continue
                            colnum = pd.to_numeric(df[col], errors="coerce")
                            valid = colnum.between(minv, maxv)
                            # those rows_to_check & (~valid) are offenders. BUT exclude rows that are blank (missing) — missing is checked separately
                            mask_off = rows_to_check & (~valid) & (~colnum.isna())
                            offenders = df.loc[mask_off, id_col]
                            for rid in offenders:
                                report.append({id_col: rid, "Question": col, "Check_Type": "Range", "Issue": f"Value out of range ({minv}-{maxv})"})
                    except Exception as e:
                        report.append({id_col: None, "Question": q, "Check_Type": "Range", "Issue": f"Invalid range condition ({cond}) - {e}"})

                elif ct_lower == "missing":
                    for col in related_cols:
                        if col not in cols:
                            report.append({id_col: None, "Question": col, "Check_Type": "Missing", "Issue": "Variable not found in data"})
                            continue
                        blank = is_blank_series(df[col])
                        offenders = df.loc[rows_to_check & blank, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": col, "Check_Type": "Missing", "Issue": "Value is missing"})

                elif ct_lower == "straightliner":
                    # related_cols might be a prefix: if only one item and endswith '_' expand
                    rc = related_cols
                    if len(rc) == 1 and rc[0].endswith("_"):
                        rc = [c for c in cols if c.startswith(rc[0])]
                    # if only 1 col but target is prefix-like (e.g., Q9_ ) expand
                    if len(rc) == 1 and rc[0] in cols and any(col.startswith(rc[0]) for col in cols):
                        rc = [c for c in cols if c.startswith(rc[0])]
                    if len(rc) > 1:
                        same = df[rc].nunique(axis=1) == 1
                        offenders = df.loc[rows_to_check & same, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": ",".join(rc), "Check_Type": "Straightliner", "Issue": "Same response across all items"})

                elif ct_lower == "multi-select":
                    # expected that q is prefix e.g., Q2_ and related_cols will be expanded
                    if q.endswith("_"):
                        mcols = [c for c in cols if c.startswith(q)]
                    else:
                        mcols = [c for c in cols if c.startswith(q)]
                    if not mcols:
                        report.append({id_col: None, "Question": q, "Check_Type": "Multi-Select", "Issue": "Multi-select columns not found"})
                        continue
                    # check allowed values 0/1
                    for col in mcols:
                        # treat non-numeric values as offenders
                        bad = ~df[col].isin([0, 1, "0", "1"]) & (~is_blank_series(df[col]))
                        offenders = df.loc[rows_to_check & bad, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": col, "Check_Type": "Multi-Select", "Issue": "Invalid value (not 0/1)"})
                    # check at least one selected per respondent
                    try:
                        summed = df[mcols].replace({"": 0, "NA": 0, "na": 0}).fillna(0).astype(float).sum(axis=1)
                        no_selection = summed == 0
                        offenders = df.loc[rows_to_check & no_selection, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": q, "Check_Type": "Multi-Select", "Issue": "No options selected"})
                    except Exception:
                        # fallback: if cannot cast to numeric, check string '1' occurrences
                        one_selected = df[mcols].astype(str).apply(lambda row: row.eq("1").any(), axis=1)
                        offenders = df.loc[rows_to_check & ~one_selected, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": q, "Check_Type": "Multi-Select", "Issue": "No options selected"})

                elif ct_lower == "openend_junk":
                    for col in related_cols:
                        if col not in cols:
                            report.append({id_col: None, "Question": col, "Check_Type": "OpenEnd_Junk", "Issue": "Variable not found"})
                            continue
                        # treat literal "NA" and non-blank as valid answered
                        s = df[col].astype(str)
                        # treat as junk if length < 3 (excluding 'NA' literal)
                        junk_mask = (~s.str.upper().eq("NA")) & (s.str.strip().str.len() < 3)
                        offenders = df.loc[rows_to_check & junk_mask, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": col, "Check_Type": "OpenEnd_Junk", "Issue": "Open-end looks like junk/low-effort"})

                elif ct_lower == "duplicate":
                    for col in related_cols:
                        if col not in cols:
                            continue
                        dupes = df.loc[rows_to_check & df.duplicated(subset=[col], keep=False), id_col]
                        for rid in dupes:
                            report.append({id_col: rid, "Question": col, "Check_Type": "Duplicate", "Issue": "Duplicate value found"})

                else:
                    # Unknown check type -> record it in generated rules but don't crash
                    report.append({id_col: None, "Question": q, "Check_Type": ct, "Issue": "Unknown check type (skipped)"})

        # Convert outputs to DataFrames
        failed_df = pd.DataFrame(report)
        # Keep columns deterministic order
        if not failed_df.empty:
            cols_order = [id_col, "Question", "Check_Type", "Issue"]
            for c in cols_order:
                if c not in failed_df.columns:
                    failed_df[c] = None
            failed_df = failed_df[cols_order]

        # Show generated rules and failed checks
        st.markdown("### Generated Validation Rules")
        st.dataframe(gen_rules_df.reset_index(drop=True))

        st.markdown("### Failed Checks (only failing rows)")
        if failed_df.empty:
            st.success("No failures detected ✅")
            st.dataframe(failed_df)
        else:
            st.error(f"{len(failed_df)} failing rows found")
            st.dataframe(failed_df)

        # Provide downloads: combined workbook with two sheets: Generated Rules + Failed Checks
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            gen_rules_df.to_excel(writer, index=False, sheet_name="Validation Rules")
            failed_df.to_excel(writer, index=False, sheet_name="Failed Checks")
        out.seek(0)
        st.download_button("📥 Download Rules + Failed Checks (Excel)", data=out.getvalue(),
                           file_name="validation_rules_and_failures.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

