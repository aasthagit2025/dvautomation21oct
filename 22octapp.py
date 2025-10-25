# app.py
import streamlit as st
import pandas as pd
import pyreadstat
import io
import re
from typing import List

st.set_page_config(page_title="Auto Validation Rules + Failed Checks Generator", layout="wide")
st.title("📊 Auto Validation Rules + Failed Checks Generator")

# --- Upload ---
col1, col2, col3 = st.columns([1, 1, 1])
with col1:
    raw_file = st.file_uploader("Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx", "sav"])
with col2:
    skips_file = st.file_uploader("Upload skip rules (CSV or XLSX) — optional", type=["csv", "xlsx"])
with col3:
    constructed_txt = st.file_uploader("Upload Constructed List export (text) — optional", type=["txt"])

# --- Helpers ---
def read_raw(file):
    if file is None:
        return None
    try:
        if file.name.endswith(".csv"):
            return pd.read_csv(file, encoding_errors="ignore", low_memory=False)
        elif file.name.endswith(".xlsx"):
            return pd.read_excel(file)
        elif file.name.endswith(".sav"):
            df, meta = pyreadstat.read_sav(file)
            return df
    except Exception as e:
        st.error(f"Error reading raw data: {e}")
        return None

def read_skips(file):
    if file is None:
        return None
    try:
        if file.name.endswith(".csv"):
            return pd.read_csv(file, encoding_errors="ignore")
        else:
            return pd.read_excel(file)
    except Exception as e:
        st.error(f"Error reading skips file: {e}")
        return None

def read_constructed_txt(file) -> str:
    if file is None:
        return ""
    try:
        return file.getvalue().decode(errors="ignore")
    except Exception:
        try:
            return file.getvalue().decode('utf-8', errors="ignore")
        except Exception as e:
            st.warning(f"Could not decode constructed text: {e}")
            return ""

def find_next_variable(colname, all_cols):
    """Return next column name after colname in all_cols, or None"""
    if colname not in all_cols:
        return None
    idx = all_cols.index(colname)
    if idx + 1 < len(all_cols):
        return all_cols[idx + 1]
    return None

def is_multiselect_prefix(col, all_cols):
    # if many columns start with this prefix and suffix is underscore+digit OR _rX patterns
    return len([c for c in all_cols if c.startswith(col)]) > 1

def blank_mask_for(col_vals):
    """Return boolean Series for blanks: True if blank (NaN or empty string).
       Treat literal 'NA' or 'N/A' as NOT blank (they count as answers)."""
    s = col_vals
    is_na = s.isna()
    asstr = s.astype(str).str.strip()
    empty_str = asstr == ""
    literal_na = asstr.str.upper().isin(["NA", "N/A"])
    return is_na | empty_str & (~literal_na)

def parse_logic_text(logic_text: str):
    """A simple passthrough parser for Logic column from skip file.
       We'll return the text (we don't deeply transform here)."""
    if pd.isna(logic_text) or str(logic_text).strip() == "":
        return ""
    return str(logic_text).strip()

# Condition mask builder (used during validation)
def get_condition_mask(cond_text: str, df: pd.DataFrame):
    """Parse an IF condition like 'If Q1=1 and Q2>3 or Q3<>2' and return boolean mask.
       We'll support operators: <=, >=, !=, <>, =, <, >
    """
    if not cond_text:
        return pd.Series(True, index=df.index)
    cond_text = cond_text.strip()
    # remove leading If (case-insensitive)
    if cond_text.lower().startswith("if"):
        cond_text = cond_text[2:].strip()
    or_groups = re.split(r'\s+or\s+', cond_text, flags=re.IGNORECASE)
    mask = pd.Series(False, index=df.index)
    for or_g in or_groups:
        and_parts = re.split(r'\s+and\s+', or_g, flags=re.IGNORECASE)
        sub_mask = pd.Series(True, index=df.index)
        for part in and_parts:
            part = part.strip().replace("<>", "!=")
            matched = False
            for op in ["<=", ">=", "!=", "<", ">", "="]:
                if op in part:
                    col, val = [p.strip() for p in part.split(op, 1)]
                    if col not in df.columns:
                        sub_mask &= False
                        matched = True
                        break
                    # numeric comparison?
                    col_vals = df[col]
                    # try numeric val
                    try:
                        val_num = float(val)
                        col_num = pd.to_numeric(col_vals, errors="coerce")
                        if op == "<=":
                            sub_mask &= col_num <= val_num
                        elif op == ">=":
                            sub_mask &= col_num >= val_num
                        elif op == "<":
                            sub_mask &= col_num < val_num
                        elif op == ">":
                            sub_mask &= col_num > val_num
                        elif op == "=":
                            sub_mask &= col_num == val_num
                    except Exception:
                        # string compare
                        if op in ["!=", "<>"]:
                            sub_mask &= col_vals.astype(str).str.strip() != val
                        elif op == "=":
                            sub_mask &= col_vals.astype(str).str.strip() == val
                    matched = True
                    break
            if not matched:
                sub_mask &= False
        mask |= sub_mask
    return mask

# --- Load inputs ---
df = read_raw(raw_file)
skips_df = read_skips(skips_file)
constructed_text = read_constructed_txt(constructed_txt)

if df is None:
    st.info("Upload raw data to begin.")
    st.stop()

# Identify ID column
id_col = next((c for c in ["RespondentID", "Password", "RespID", "RID", "sys_RespNum"] if c in df.columns), None)
if id_col is None:
    st.warning("No respondent ID column found using common names. Using first column as ID.")
    id_col = df.columns[0]

all_cols = list(df.columns)

# --- Auto-generate validation rules from raw data & skips ---
generated_rules = []  # list of dicts: Question, Check_Type, Condition, Source

# Helper to add rule
def add_rule(q, ctype, condition="", source="Auto"):
    generated_rules.append({
        "Question": q,
        "Check_Type": ctype,
        "Condition": condition,
        "Source": source
    })

# 1) Use skips file to produce skip rules (priority: these appear as Skip rules)
if skips_df is not None:
    # expect columns like Skip From, Skip Type, Always Skip, Logic, Skip To
    # normalize column names (lower)
    skips_df.columns = [c.strip() for c in skips_df.columns]
    # Use a few candidate column headers
    possible_from = [c for c in skips_df.columns if c.lower().startswith("skip from")]
    possible_logic = [c for c in skips_df.columns if c.lower().startswith("logic")]
    possible_to = [c for c in skips_df.columns if c.lower().startswith("skip to")]
    possible_type = [c for c in skips_df.columns if c.lower().startswith("skip type")]
    possible_always = [c for c in skips_df.columns if c.lower().startswith("always skip")]
    for _, row in skips_df.iterrows():
        try:
            skip_from = row[possible_from[0]] if possible_from else row.get("Skip From", "")
            skip_type = row[possible_type[0]] if possible_type else row.get("Skip Type", "")
            always_skip = row[possible_always[0]] if possible_always else row.get("Always Skip", "")
            logic = row[possible_logic[0]] if possible_logic else row.get("Logic", "")
            skip_to = row[possible_to[0]] if possible_to else row.get("Skip To", "")
        except Exception:
            # fallback generic
            skip_from = row.get("Skip From", "")
            skip_type = row.get("Skip Type", "")
            always_skip = row.get("Always Skip", "")
            logic = row.get("Logic", "")
            skip_to = row.get("Skip To", "")

        sf = str(skip_from).strip()
        logic_txt = parse_logic_text(logic)
        st_to = str(skip_to).strip()

        # Determine target variable(s)
        if st_to.lower() in ["next question", "nextquestion", "next"]:
            # map to next var after skip_from among raw data columns
            next_v = find_next_variable(sf, all_cols)
            target_cols = [next_v] if next_v else []
        elif st_to == "" or pd.isna(st_to):
            # no explicit skip-to: assume next
            next_v = find_next_variable(sf, all_cols)
            target_cols = [next_v] if next_v else []
        else:
            target_cols = [st.strip() for st in st_to.split(",")]

        # If Always Skip==1 -> no validation needed (skip rule exists but no check)
        if str(always_skip).strip() in ["1", "True", "true", "YES", "Yes"]:
            # skip generation of checks for target(s) (but still record the mapping for traceability)
            for t in target_cols:
                add_rule(t, "Skip", f"{logic_txt} then {t} should be skipped (Always Skip)", "Skips")
            continue

        # Normal case -> create Skip rule phrasing (we will validate it later)
        for t in target_cols:
            # If skip_type includes 'Pre' or 'Post' we ignore difference for rule creation — both produce Skip rules
            # We create rule text like: If <logic> then <target> should be blank  or should be answered
            # If logic contains negative form like 'Not(' or '<>' we keep as-is.
            # Default to "should be blank" when logic seems to say 'then Next Question should be blank' or includes 'not'
            rule_condition = ""
            if logic_txt:
                # if logic already contains a 'then' clause (rare in the skips export), use as-is, else append then-part using 'should be blank'
                # Attempt heuristics: if logic contains 'then' or 'should be answered' phrases
                lower_logic = logic_txt.lower()
                if "then" in lower_logic or "should" in lower_logic:
                    # take as-is
                    rule_condition = logic_txt
                else:
                    # default: If <logic> then <t> should be blank
                    rule_condition = f"If {logic_txt} then {t} should be blank"
            else:
                # No logic given — nothing to generate
                rule_condition = f"If (unknown) then {t} should be blank"

            add_rule(t, "Skip", rule_condition, "Skips")

# 2) Auto-detect variable-level rules from raw data
# We'll inspect each column in raw order and add appropriate rules if not already generated
existing_questions = set([r["Question"] for r in generated_rules if r["Question"]])

for col in all_cols:
    if col in existing_questions:
        # still add other checks for the same variable (we will allow multiple check types on same question)
        pass

    # Decide variable type heuristics
    series = df[col]
    unique_non_blank = series[~blank_mask_for(series)].dropna().unique()
    # try numeric
    numeric_vals = pd.to_numeric(series.dropna(), errors="coerce")
    num_nonnan = numeric_vals.notna().sum()
    # count unique non-null values
    unique_count = len(pd.Series(unique_non_blank))
    # detect multiselect groups: many columns share same prefix before last underscore and suffix numeric
    # Candidate prefix: part before last '_' + digit
    m = re.match(r"^(.+?)(_r?\d+|_\d+)$", col)
    if m:
        prefix = m.group(1) + "_"
    else:
        # try without trailing index
        prefix = re.sub(r"(_\d+)$", "_", col) if re.search(r"_\d+$", col) else col

    # Detect if this column is part of a multi-select group
    # We consider multiselect if there exist >=2 columns that start with a base prefix like 'Q2_'
    base_candidates = []
    # generate possible prefixes: up to last underscore
    if "_" in col:
        base_candidates.append(col.rsplit("_", 1)[0] + "_")
    if col.endswith("_r1") or "_r" in col:
        base_candidates.append(re.sub(r"(_r?\d+)$", "_", col))
    base_candidates = [b for b in base_candidates if b != col]

    is_multiselect = False
    ms_prefix = None
    for b in base_candidates:
        matches = [c for c in all_cols if c.startswith(b)]
        if len(matches) >= 2:
            is_multiselect = True
            ms_prefix = b
            break

    # Add rules depending on heuristics (ensure duplicates of same check type for same question are avoided)
    def has_rule(q, ctype):
        return any(r for r in generated_rules if r["Question"] == q and r["Check_Type"].lower() == ctype.lower())

    # If multiselect prefix discovered, create a single Multi-Select rule on prefix (only once)
    if is_multiselect and ms_prefix:
        qname = ms_prefix.rstrip("_")
        # create rule for prefix if not existing (so it will cover all ms columns)
        if not has_rule(qname, "Multi-Select"):
            add_rule(qname, "Multi-Select", "Only 0/1; At least one selected", "Auto (multiselect detected)")
        # Also add per-subcolumn 0/1 check
        for sub in [c for c in all_cols if c.startswith(ms_prefix)]:
            if not has_rule(sub, "Multi-Select"):
                add_rule(sub, "Multi-Select", "Only 0/1", "Auto (multiselect detected)")
        continue

    # If numeric and unique small range typical for rating (2..20), add Range; Straightliner
    numeric_non_na = pd.to_numeric(series.dropna(), errors="coerce").dropna()
    uniq_numeric_vals = pd.Series(numeric_non_na.unique()).dropna().astype(float) if not numeric_non_na.empty else pd.Series([])
    if len(uniq_numeric_vals) >= 2 and len(uniq_numeric_vals) <= 20 and numeric_non_na.notna().sum() > 0:
        # compute min/max excluding weird sentinel values
        minv = float(uniq_numeric_vals.min())
        maxv = float(uniq_numeric_vals.max())
        # Add Range rule
        if not has_rule(col, "Range"):
            add_rule(col, "Range", f"{int(minv)}-{int(maxv) if maxv.is_integer() else maxv}", "Auto (rating)")
        # Add Straightliner at prefix level (group of similarly named items)
        # choose a grouping prefix (strip trailing _r or trailing digits)
        gprefix = re.sub(r'(_r?\d+)$', '', col)
        gprefix = re.sub(r'(_\d+)$', '', gprefix)
        # Only add group straightliner if there are multiple in that group
        group_candidates = [c for c in all_cols if c.startswith(gprefix) and c != col]
        if group_candidates:
            grp_key = gprefix + "_group"
            # We'll create a straightliner rule per gprefix (unique name)
            if not any(r for r in generated_rules if r["Question"] == gprefix and r["Check_Type"].lower() == "straightliner"):
                add_rule(gprefix, "Straightliner", f"group {gprefix}", "Auto (rating)")
        continue

    # If object/string: detect open-end text by long average length or many unique values
    if series.dtype == object or series.dtype.name == "string":
        avg_len = series.astype(str).map(len).mean() if len(series) > 0 else 0
        nunique = series.dropna().astype(str).map(lambda x: x.strip()).nunique()
        # open-end heuristics
        if avg_len > 20 or nunique > 20:
            if not has_rule(col, "OpenEnd_Junk"):
                add_rule(col, "OpenEnd_Junk", "MinLen(3)", "Auto (open-end/text)")
            # also check missing for open-end
            if not has_rule(col, "Missing"):
                add_rule(col, "Missing", "", "Auto (open-end/text)")
            continue
        # else single-select (categorical)
        if not has_rule(col, "Missing"):
            add_rule(col, "Missing", "", "Auto (single-select)")

    # fallback: if nothing else, attempt missing
    if not has_rule(col, "Missing"):
        add_rule(col, "Missing", "", "Auto (default)")

# Convert generated_rules list to DataFrame and preserve data order for Questions based on raw data
rules_df = pd.DataFrame(generated_rules)

# reorder rules so that questions present in raw data appear in raw data order
def sort_key_row(r):
    q = r["Question"]
    if q in all_cols:
        return all_cols.index(q)
    # if group/prefix, try to find first matching column index
    matches = [i for i, c in enumerate(all_cols) if c.startswith(q)]
    return matches[0] if matches else 99999

rules_df["__sort_key"] = rules_df.apply(sort_key_row, axis=1)
rules_df = rules_df.sort_values(["__sort_key", "Question"]).drop(columns="__sort_key").reset_index(drop=True)

st.write("### Preview: Generated Validation Rules (first 200 rows)")
st.dataframe(rules_df.head(200))

# --- Allow edit & download of generated rules ---
st.markdown("---")
st.header("Review / Edit generated rules")
st.info("You can modify Check_Type (semicolons allowed for multiple), Condition, or Question. When ready, press 'Save rules and run validation' to run checks on the data.")
edited = st.experimental_data_editor(rules_df, num_rows="dynamic", width='stretch')

# Download generated rules excel
buf_rules = io.BytesIO()
with pd.ExcelWriter(buf_rules, engine="openpyxl") as writer:
    edited.to_excel(writer, index=False, sheet_name="Validation Rules")
st.download_button("📥 Download generated validation rules (Excel)", data=buf_rules.getvalue(), file_name="validation_rules_generated.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# --- Run validation on edited rules when user clicks ---
if st.button("Save rules and run validation"):
    rules_run = edited.copy()
    # Normalize check types & conditions into lists
    report = []
    skip_pass_ids = set()

    for _, rule in rules_run.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip() for c in str(rule.get("Check_Type", "")).split(";") if c.strip()]
        conditions = [c.strip() for c in str(rule.get("Condition", "")).split(";")]

        # For group/prefix names (like 'Q3_') we will expand during checks when needed.
        # For Multi-Select prefix entries (like Q2_) users may have left Question as 'Q2_'.
        # Build related_cols for checks: if q matches a real column use it, else expand prefix
        related_cols = [q] if q in df.columns else [c for c in df.columns if c.startswith(q)]
        if len(related_cols) == 0:
            # nothing to check (skip generation), but record dataset-level issue
            report.append({id_col: None, "Question": q, "Check_Type": ";".join(check_types), "Issue": "Question not found in dataset"})
            continue

        # Evaluate Skip rules first if present to compute mask of "should answer"
        skip_mask = None
        if any(ct.lower() == "skip" for ct in check_types):
            # find index and corresponding condition
            try:
                skip_idx = [i for i,ct in enumerate(check_types) if ct.lower() == "skip"][0]
                cond_text = conditions[skip_idx] if skip_idx < len(conditions) else ""
                # expect format like: If <logic> then <target> should be answered/blank OR logic may be present in skips source
                # split 'then' out (we'll only use the if-part to compute mask)
                if "then" in cond_text.lower():
                    if_part, then_part = re.split(r'(?i)then', cond_text, maxsplit=1)
                else:
                    if_part = cond_text
                    then_part = ""
                if_mask = get_condition_mask(if_part, df)
                # If the then_part contains the target and indicates 'should be blank' or 'should be answered'
                # we will validate accordingly below. For now, store if_mask as skip_mask meaning "condition true"
                skip_mask = if_mask
                # Determine then-target columns (from then_part) if provided; otherwise assume related_cols or next question mapping
                then_q_token = None
                if then_part:
                    # first token likely the target variable
                    then_q_token = then_part.strip().split()[0]
                    # if token equals 'Next' or 'Next Question' - handled where we validate target
                # We'll handle target checks below according to then_part text
            except Exception:
                skip_mask = None

        # Rows to check for non-skip checks: only those respondents who "should answer"
        rows_to_check = skip_mask if skip_mask is not None else pd.Series(True, index=df.index)

        # Now loop through check types (skip already processed above)
        for i, ct in enumerate(check_types):
            ct_lower = ct.lower()
            cond = conditions[i] if i < len(conditions) else ""
            if ct_lower == "skip":
                # Validate skip rule - we must determine target(s) and whether should be blank or answered
                # Identify then part previously parsed
                if "then" in cond.lower():
                    if_part, then_part = re.split(r'(?i)then', cond, maxsplit=1)
                else:
                    if_part, then_part = cond, ""
                then_part = then_part.strip()
                # parse target(s)
                if then_part == "":
                    # no explicit then-part -> assume related_cols
                    target_cols = related_cols
                    should_be_blank = True
                else:
                    # decide target token
                    first_tok = then_part.split()[0].strip()
                    if first_tok.lower() in ["next", "nextquestion", "nextquestion,"]:
                        # map to next var after skip-from if possible (we try to extract skip-from from if_part)
                        # attempt to find a variable name in if_part as skip-from; fallback to related_cols
                        m = re.search(r'([A-Za-z0-9_]+)', if_part)
                        if m:
                            sf = m.group(1)
                            nx = find_next_variable(sf, all_cols)
                            target_cols = [nx] if nx else related_cols
                        else:
                            target_cols = related_cols
                    else:
                        # explicit name or range 'Q3_1 to Q3_13' or 'Q3_1 to Q3_13'
                        if "to" in then_part.lower():
                            # attempt range expansion like 'Q3_1 to Q3_13' or 'Q3_1 to Q3_5'
                            rng = re.findall(r'([A-Za-z0-9_]+(?:\d+)?)\s*to\s*([A-Za-z0-9_]+(?:\d+)?)', then_part, flags=re.IGNORECASE)
                            if rng:
                                start, end = rng[0]
                                # try to generate sequence by numeric suffix
                                m1 = re.match(r'(.+?)(\d+)$', start)
                                m2 = re.match(r'(.+?)(\d+)$', end)
                                if m1 and m2 and m1.group(1) == m2.group(1):
                                    prefix = m1.group(1)
                                    startn = int(m1.group(2)); endn = int(m2.group(2))
                                    target_cols = [f"{prefix}{k}" for k in range(startn, endn+1) if f"{prefix}{k}" in df.columns]
                                else:
                                    # fallback: find all cols starting with start prefix
                                    target_cols = [c for c in all_cols if c.startswith(start)]
                            else:
                                target_cols = [first_tok] if first_tok in df.columns else related_cols
                        elif first_tok.endswith("_"):
                            target_cols = [c for c in all_cols if c.startswith(first_tok)]
                        elif first_tok in df.columns:
                            target_cols = [first_tok]
                        else:
                            target_cols = related_cols

                    should_be_blank = "blank" in then_part.lower()
                # Now check each target col for offenders
                for tcol in target_cols:
                    if tcol not in df.columns:
                        report.append({id_col: None, "Question": tcol, "Check_Type": "Skip", "Issue": "Target variable not found"})
                        continue
                    bmask = blank_mask_for(df[tcol])
                    if should_be_blank:
                        # offender if condition FALSE? be careful: logic indicates condition true => should be blank, so offender is condition true AND not blank
                        offenders = df.loc[skip_mask & ~bmask, id_col] if skip_mask is not None else df.loc[~bmask, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": tcol, "Check_Type": "Skip", "Issue": "Answered but should be blank"})
                    else:
                        # should be answered: condition true & blank -> offender
                        offenders = df.loc[skip_mask & bmask, id_col] if skip_mask is not None else df.loc[bmask, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": tcol, "Check_Type": "Skip", "Issue": "Blank but should be answered"})
                continue

            # Non-skip checks: apply only to rows_to_check (respondents who should answer)
            if ct_lower == "range":
                # condition expected as "min-max" or "1-5" or "1 to 5"
                try:
                    cond_clean = cond.replace("to", "-")
                    if "-" not in cond_clean:
                        raise ValueError("Invalid range")
                    min_val, max_val = [float(x.strip()) for x in cond_clean.split("-", 1)]
                    for col_check in related_cols:
                        colnum = pd.to_numeric(df[col_check], errors="coerce")
                        valid_mask = colnum.between(min_val, max_val)
                        offenders = df.loc[rows_to_check & (~valid_mask | colnum.isna()), id_col]
                        # But if skip_mask was set (i.e., some respondents should be skipped), then only those in rows_to_check True are considered (already used)
                        for rid in offenders:
                            # If blank (NaN) but skip logic made them should be answered, they'll show as Blank via Skip not Range; keep Range for out-of-range numeric values
                            # We'll report out-of-range unless value is blank (handled by Missing/Skip)
                            if pd.isna(df.loc[df[id_col]==rid, col_check].values[0]):
                                # if it's NaN we'll not double-report here (Missing check will handle). Skip
                                continue
                            report.append({id_col: rid, "Question": col_check, "Check_Type": "Range", "Issue": f"Value out of range ({int(min_val)}-{int(max_val)})"})
                except Exception:
                    report.append({id_col: None, "Question": q, "Check_Type": "Range", "Issue": f"Invalid range condition ({cond})"})
            elif ct_lower == "missing":
                for col_check in related_cols:
                    bmask = blank_mask_for(df[col_check])
                    offenders = df.loc[rows_to_check & bmask, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": col_check, "Check_Type": "Missing", "Issue": "Value is missing"})
            elif ct_lower == "straightliner":
                # If q is prefix group, expand; else try to expand similarly-named variables
                grp_cols = related_cols
                if len(grp_cols) == 1:
                    # expand by prefix heuristics
                    prefix = related_cols[0]
                    grp_cols = [c for c in all_cols if c.startswith(prefix)]
                if len(grp_cols) > 1:
                    same_resp = df[grp_cols].nunique(axis=1) == 1
                    offenders = df.loc[rows_to_check & same_resp, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": ",".join(grp_cols), "Check_Type": "Straightliner", "Issue": "Same response across all items"})
            elif ct_lower == "multi-select":
                # related_cols must be prefix or single col; expand
                ms_cols = related_cols if any(c in df.columns for c in related_cols) else [c for c in all_cols if c.startswith(q)]
                if len(ms_cols) == 0:
                    report.append({id_col: None, "Question": q, "Check_Type": "Multi-Select", "Issue": "No multi-select columns found for prefix"})
                    continue
                # 1) ensure values are only 0/1 for each option
                for mc in ms_cols:
                    offenders = df.loc[~df[mc].isin([0, 1, "0", "1", 0.0, 1.0]) & (~df[mc].isna()), id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": mc, "Check_Type": "Multi-Select", "Issue": "Invalid value (not 0/1)"})
                # 2) at least one option selected across options
                summed = None
                try:
                    summed = df[ms_cols].fillna(0).astype(float).sum(axis=1)
                except Exception:
                    # fallback: try convert each col individually ignoring non-numeric
                    conv = []
                    for mc in ms_cols:
                        conv.append(pd.to_numeric(df[mc], errors="coerce").fillna(0))
                    if conv:
                        summed = pd.concat(conv, axis=1).sum(axis=1)
                if summed is not None:
                    offenders = df.loc[rows_to_check & (summed == 0), id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": q, "Check_Type": "Multi-Select", "Issue": "No options selected"})
            elif ct_lower == "openend_junk":
                for col_check in related_cols:
                    # treat 'NA' as answered, so only true blank or short strings flagged
                    s = df[col_check].astype(str).fillna("")
                    # consider blank handled by Missing/Skip; here we flag low-effort non-blank text
                    low_effort = (s.str.strip().str.len() < 3) & (s.str.strip() != "") & (~s.str.upper().isin(["NA","N/A"]))
                    offenders = df.loc[rows_to_check & low_effort, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": col_check, "Check_Type": "OpenEnd_Junk", "Issue": "Open-end looks like junk/low-effort"})
            elif ct_lower == "duplicate":
                for col_check in related_cols:
                    dupes = df.loc[rows_to_check & df.duplicated(subset=[col_check], keep=False), id_col]
                    for rid in dupes:
                        report.append({id_col: rid, "Question": col_check, "Check_Type": "Duplicate", "Issue": "Duplicate value found"})
            else:
                # other check types not implemented - flag as note
                report.append({id_col: None, "Question": q, "Check_Type": ct, "Issue": "Check type not implemented in engine"})

    # Final report DataFrame
    report_df = pd.DataFrame(report)
    if report_df.empty:
        st.success("Validation completed — no failed checks found.")
    else:
        st.error(f"Validation completed — {len(report_df)} failed checks found.")
    st.dataframe(report_df)

    # allow download of failed checks only (Excel)
    out_fail = io.BytesIO()
    with pd.ExcelWriter(out_fail, engine="openpyxl") as writer:
        # ensure there's at least one visible sheet
        if report_df.empty:
            pd.DataFrame([{"Info":"No failed checks"}]).to_excel(writer, index=False, sheet_name="Validation Report")
        else:
            report_df.to_excel(writer, index=False, sheet_name="Validation Report")
    st.download_button("📥 Download validation (failed checks) report", data=out_fail.getvalue(), file_name="validation_failed_checks.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # Also allow download of the edited rules used for validation
    out_rules_final = io.BytesIO()
    with pd.ExcelWriter(out_rules_final, engine="openpyxl") as writer:
        rules_run.to_excel(writer, index=False, sheet_name="Validation Rules Used")
    st.download_button("📥 Download edited rules used (Excel)", data=out_rules_final.getvalue(), file_name="validation_rules_used.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
