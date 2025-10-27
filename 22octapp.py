# app.py
import streamlit as st
import pandas as pd
import pyreadstat
import io
import re
from typing import List, Tuple

st.set_page_config(layout="wide", page_title="Auto Validation Rules + Failed Checks Generator")

st.title("📊 Auto Validation Rules + Failed Checks Generator")

# ----------------------
# Helper utilities
# ----------------------
def detect_id_column(df: pd.DataFrame) -> str:
    candidates = ["RespondentID", "Password", "sys_RespNum", "RespID", "RID", "ID"]
    for c in candidates:
        if c in df.columns:
            return c
    # fallback: try first column
    return df.columns[0]

def is_literal_NA(x) -> bool:
    try:
        s = str(x).strip()
        return s.upper() == "NA"
    except Exception:
        return False

def blank_mask_for_col(df: pd.DataFrame, col: str) -> pd.Series:
    # True = blank for purposes of checks. Treat literal "NA" as answered (i.e., NOT blank)
    s = df[col].astype(object)
    is_blank = s.isna() | (s.astype(str).str.strip() == "")
    # remove literal "NA" from blank
    is_na_literal = s.astype(str).str.strip().str.upper() == "NA"
    is_blank = is_blank & (~is_na_literal)
    return is_blank

# parse a condition like "If Q4_r1<=7 and Q5=2 or Q6<>3 then Q4c should be answered"
op_regex = r"(<=|>=|<>|!=|=|<|>)"

def parse_condition_to_mask(cond_text: str, df: pd.DataFrame) -> pd.Series:
    """ Return boolean mask for rows where condition is True. """
    if not cond_text or str(cond_text).strip() == "":
        return pd.Series(True, index=df.index)

    text = str(cond_text).strip()
    # remove leading If/if
    if text.lower().startswith("if"):
        text = text[2:].strip()
    # normalize 'Not(' and 'Not ' usage
    # We'll support OR groups split by ' or ' (case-insensitive)
    or_groups = re.split(r'\s+or\s+', text, flags=re.IGNORECASE)
    mask = pd.Series(False, index=df.index)
    for grp in or_groups:
        and_parts = re.split(r'\s+and\s+', grp, flags=re.IGNORECASE)
        submask = pd.Series(True, index=df.index)
        for part in and_parts:
            part = part.strip()
            # handle Not(...) or Not A
            if part.lower().startswith("not(") and part.endswith(")"):
                inner = part[4:-1].strip()
                inner_mask = parse_condition_to_mask(inner, df)
                submask &= ~inner_mask
                continue
            if part.lower().startswith("not "):
                inner = part[4:].strip()
                inner_mask = parse_condition_to_mask(inner, df)
                submask &= ~inner_mask
                continue

            # replace '<>' with '!=' for convenience
            part = part.replace("<>", "!=")
            # find operator
            m = re.search(op_regex, part)
            if not m:
                # unsupported pattern (treat as False)
                submask &= False
                continue
            op = m.group(1)
            left, right = [p.strip() for p in re.split(op_regex, part, maxsplit=1)[:3:2]]
            # right could be a value or list like (1 or 2) or "1 or 2"
            # handle simple CSV: '1 or 2' is handled via or_groups already; if right has 'or' inside parentheses we skip complex parsing
            # try numeric comparison
            if left not in df.columns:
                submask &= False
                continue
            colvals = df[left]
            # if right looks like numeric and operator is numeric then compare numerically
            try:
                val_num = float(right)
                colnum = pd.to_numeric(colvals, errors="coerce")
                if op == "<=":
                    submask &= colnum <= val_num
                elif op == ">=":
                    submask &= colnum >= val_num
                elif op == "<":
                    submask &= colnum < val_num
                elif op == ">":
                    submask &= colnum > val_num
                elif op in ("=", "=="):
                    submask &= colnum == val_num
                elif op in ("!=",):
                    submask &= colnum != val_num
            except Exception:
                # string comparison (trim)
                right_s = right.strip().strip('"').strip("'")
                if op in ("=", "=="):
                    submask &= colvals.astype(str).str.strip() == right_s
                elif op in ("!=",):
                    submask &= colvals.astype(str).str.strip() != right_s
                else:
                    # can't do numeric op on non-numeric
                    submask &= False
        mask |= submask
    return mask

def expand_prefix(prefix: str, cols: List[str]) -> List[str]:
    # if prefix ends with '_' or ends with any non-alphanumeric, treat as prefix
    return [c for c in cols if c.startswith(prefix)]

def expand_range_expr(expr: str, cols: List[str]) -> List[str]:
    # support "Q3_1 to Q3_13" or "Q3_1-Q3_13" or "Q3_1 to Q3_13 should be answered"
    expr = str(expr)
    m = re.search(r'([A-Za-z0-9_]+?\d+)\s*(?:to|-)\s*([A-Za-z0-9_]+?\d+)', expr, flags=re.IGNORECASE)
    if not m:
        # maybe single variable or prefix_
        token = re.split(r'\s', expr.strip())[0]
        if token.endswith("_"):
            return expand_prefix(token, cols)
        return [token] if token in cols else []
    start, end = m.group(1), m.group(2)
    base1 = re.match(r"(.+?)(\d+)$", start)
    base2 = re.match(r"(.+?)(\d+)$", end)
    if base1 and base2 and base1.group(1) == base2.group(1):
        prefix = base1.group(1)
        start_i, end_i = int(base1.group(2)), int(base2.group(2))
        return [f"{prefix}{i}" for i in range(start_i, end_i + 1) if f"{prefix}{i}" in cols]
    return []

# ----------------------
# UI: upload
# ----------------------
st.markdown("Upload raw survey data (CSV / XLSX / SAV)")
raw_file = st.file_uploader("raw data (CSV / XLSX / SAV)", type=["csv", "xlsx", "sav"])
st.markdown("Upload skip rules (CSV or XLSX) — optional (your Sawtooth Skips export)")
skips_file = st.file_uploader("Skips (CSV/XLSX) - OPTIONAL", type=["csv", "xlsx"])
st.markdown("Upload Constructed List export (text) — optional")
constructed_txt = st.file_uploader("Constructed list .txt (optional)", type=["txt"])
st.markdown("Upload pre-made validation rules (Excel) — optional (if you already have rules)")
rules_file = st.file_uploader("Validation rules (xlsx) - OPTIONAL", type=["xlsx"])

if not raw_file:
    st.info("Upload raw data file to continue.")
    st.stop()

# ----------------------
# Load data
# ----------------------
try:
    if raw_file.name.endswith(".csv"):
        # try common encodings fallback
        try:
            df = pd.read_csv(raw_file)
        except Exception:
            raw_file.seek(0)
            df = pd.read_csv(raw_file, encoding="latin1")
    elif raw_file.name.endswith(".xlsx"):
        df = pd.read_excel(raw_file)
    elif raw_file.name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(raw_file)
    else:
        st.error("Unsupported raw data file type")
        st.stop()
except Exception as e:
    st.error(f"Error reading raw file: {e}")
    st.stop()

# keep original column order
cols_order = list(df.columns)

id_col = detect_id_column(df)
st.write(f"Detected ID column: **{id_col}**")

# ----------------------
# Load skip rules (if provided)
# Expecting CSV/XLSX with columns similar to: Skip From, Skip Type, Always Skip, Logic, Skip To
# We'll be flexible: we will search for a column containing 'Logic' or 'Condition' text.
# ----------------------
skips_df = None
if skips_file:
    try:
        if skips_file.name.endswith(".csv"):
            skips_df = pd.read_csv(skips_file)
        else:
            skips_df = pd.read_excel(skips_file)
        st.success("Skip rules loaded.")
    except Exception as e:
        st.error(f"Unable to read skip rules file: {e}")
        skips_df = None

# ----------------------
# Parse constructed list file (optional) — best-effort; we add rules for constructed lists that restrict ranges
# ----------------------
constructed_logic = None
if constructed_txt:
    try:
        txt = constructed_txt.read().decode("utf-8", errors="ignore")
        constructed_logic = txt
        st.success("Constructed list text loaded.")
    except Exception:
        try:
            constructed_logic = constructed_txt.read().decode("latin1", errors="ignore")
            st.success("Constructed list (latin1) loaded.")
        except Exception as e:
            st.warning(f"Could not decode constructed list file: {e}")
            constructed_logic = None

# ----------------------
# Generate auto rules if rules_file not provided
# ----------------------
if rules_file:
    try:
        rules_df = pd.read_excel(rules_file)
        st.success("Using uploaded validation rules.")
    except Exception as e:
        st.error(f"Could not read validation rules file: {e}")
        st.stop()
else:
    # Auto-generate rules
    generated = []
    cols = cols_order

    # function to detect if a group of columns forms a multi-select (0/1)
    def group_is_multiselect(prefix: str) -> bool:
        rel = [c for c in cols if c.startswith(prefix)]
        if len(rel) <= 1:
            return False
        # check values mostly 0/1 or NaN
        vals = pd.concat([df[c].dropna().astype(str).str.strip() for c in rel], axis=0)
        unique_vals = set(vals.unique()) - set(["", "nan", "None"])
        # allow '0','1' and occasionally other markers; but if most are 0/1 -> treat as multiselect
        # We'll treat as multiselect if unique_vals subset of {'0','1'} or at least 70% are 0/1
        if not unique_vals:
            return True
        if unique_vals.issubset({"0", "1", "0.0", "1.0"}):
            return True
        counts = vals.value_counts(normalize=True)
        top_sum = sum(counts.get(x, 0) for x in ["0", "1", "0.0", "1.0"])
        return top_sum >= 0.7

    # track prefixes already used
    used_prefixes = set()

    for c in cols:
        # skip duplicate generation for group members
        # detection: names like 'Q2_1', 'Q2_2' -> prefix 'Q2_'
        m = re.match(r"^(.+?_)\d+$", c)
        if m:
            prefix = m.group(1)
            if prefix in used_prefixes:
                continue
            # determine group columns
            group_cols = [col for col in cols if col.startswith(prefix)]
            if group_is_multiselect(prefix):
                # Multi-select rule: only 0/1 and at least one selected
                generated.append({
                    "Question": prefix.rstrip("_"),
                    "Check_Type": "Multi-Select",
                    "Condition": "Only 0/1; At least one selected",
                    "Source": "Auto (multiselect detected)"
                })
                used_prefixes.add(prefix)
                # For each member, also no individual range required
                continue
            else:
                # not multiselect -> fall through to treat as rating items if numeric
                # mark used_prefixes so Straightliner will be created below
                used_prefixes.add(prefix)
                # continue to create per-item rules below

        # detect rating/question groups by suffix patterns like _r1 or r1
        if c.endswith("_r1") or re.search(r"_r\d+$", c):
            # we'll create Range and Straightliner as group - only once per prefix
            m2 = re.match(r"^(.+?_r)\d+$", c)
            # better: detect prefix before numeric suffix, e.g., ITQ1_r1 -> prefix ITQ1_
            p = re.sub(r"\d+$", "", c)
            p = re.sub(r"_r\d+$", "_r", c)  # fallback
            # find base prefix: take chars up to last underscore preceding the numeric index
            m3 = re.match(r"^(.+?_)\w+\d*$", c)
            # simpler: find group of columns that share base before trailing index
            # We'll use a general rule: treat group prefix = text until last '_' that precedes digits
            m_last = re.match(r"^(.+_)\w*\d*$", c)
            # Instead use pattern: take everything up to the last digit block
            base_prefix = re.sub(r"\d+$", "", c)
            base_prefix = re.sub(r"_r\d+$", "_r", base_prefix)
            # Collect related columns by trying typical patterns
            related = [col for col in cols if col.startswith(base_prefix)]
            # if few related, fallback to pattern up to last underscore
            if len(related) <= 1:
                cut = c.rfind("_")
                if cut > 0:
                    base_pref2 = c[:cut+1]
                    related = [col for col in cols if col.startswith(base_pref2)]
                    base_prefix = base_pref2
            # Create Range check using observed min/max (excluding literal 'NA' and DK codes if present)
            # Try to detect integer-like values and a sensible min/max
            numeric_vals = pd.to_numeric(df[c].replace(r'^\s*$', pd.NA, regex=True).replace(
                to_replace=r'^\s*NA\s*$', value=pd.NA, regex=True), errors="coerce").dropna()
            if not numeric_vals.empty:
                minv = int(numeric_vals.min())
                maxv = int(numeric_vals.max())
                # clamp to reasonable range: if exclusively 1..5 then use 1-5; else use min-max
                generated.append({
                    "Question": c,
                    "Check_Type": "Range",
                    "Condition": f"{minv}-{maxv}",
                    "Source": "Auto (rating)"
                })
            else:
                # treat as open-end or single-select if values are strings
                # create Missing check for single-select textual
                generated.append({
                    "Question": c,
                    "Check_Type": "Missing",
                    "Condition": "",
                    "Source": "Auto (single-select/text)"
                })
            # Straightliner: create one per base_prefix (if >1 related)
            if len(related) > 1:
                related_str = ",".join(related)
                generated.append({
                    "Question": base_prefix.rstrip("_"),
                    "Check_Type": "Straightliner",
                    "Condition": f"Flag if same response across items: {related_str}",
                    "Source": "Auto (straightliner)"
                })
            continue

        # If column looks numeric (most values numeric) create Range with observed min/max
        numeric_series = pd.to_numeric(df[c].replace(r'^\s*$', pd.NA, regex=True).replace(
            to_replace=r'^\s*NA\s*$', value=pd.NA, regex=True), errors="coerce")
        numeric_nonnull = numeric_series.dropna()
        if len(numeric_nonnull) > 0 and (numeric_nonnull.apply(float.is_integer).mean() > 0.5 or numeric_nonnull.nunique() > 10):
            # Use min-max observed (excluding DK/Refused 88/99 if present?)
            # If 88 and 99 are present and max>20, it's likely DK codes; still include them as valid per your request.
            minv = int(numeric_nonnull.min())
            maxv = int(numeric_nonnull.max())
            generated.append({
                "Question": c,
                "Check_Type": "Range",
                "Condition": f"{minv}-{maxv}",
                "Source": "Auto (numeric)"
            })
            # also Missing for non-multiselect numeric single-selects
            generated.append({
                "Question": c,
                "Check_Type": "Missing",
                "Condition": "",
                "Source": "Auto (numeric)"
            })
            continue

        # detect likely open-end/text (string-heavy)
        text_frac = df[c].astype(str).apply(lambda x: len(str(x).strip())>0).mean()
        if text_frac > 0 and df[c].astype(str).str.len().median() > 15:
            # open-end
            generated.append({
                "Question": c,
                "Check_Type": "OpenEnd_Junk",
                "Condition": "MinLen(3)",
                "Source": "Auto (open-end/text)"
            })
            continue

        # default: Missing check
        generated.append({
            "Question": c,
            "Check_Type": "Missing",
            "Condition": "",
            "Source": "Auto (single-select)"
        })

    rules_df = pd.DataFrame(generated)[["Question", "Check_Type", "Condition", "Source"]]

# Show generated/loaded rules for review & editing
st.markdown("### Review / Edit generated rules")
st.markdown("You can modify Check_Type (use semicolons for multiple), Condition, or Question. When ready, press 'Save rules and run validation' to run checks on the data.")
# ensure using current streamlit config - experimental_data_editor wants width param 'stretch' or 'content'
edited = st.experimental_data_editor(rules_df, num_rows="dynamic", width="stretch")
st.download_button("📥 Download Validation Rules (Excel)", data=(
    lambda df: (
        io.BytesIO(
            pd.ExcelWriter(io.BytesIO(), engine="openpyxl").book.save
            # we'll create proper file when button clicked below
        )
    )
), help="Use 'Save rules and run validation' to generate the file", disabled=True)

if st.button("Save rules and run validation"):
    # Use edited rules
    rules_df = edited.copy()

    # Save rules to downloadable Excel
    out_rules = io.BytesIO()
    with pd.ExcelWriter(out_rules, engine="openpyxl") as writer:
        rules_df.to_excel(writer, index=False, sheet_name="Validation Rules")
    st.download_button("Download Final Validation Rules", data=out_rules.getvalue(),
                       file_name="Validation_Rules.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---------- run validation ----------
    report = []
    skip_pass_ids = set()

    # Helper: get related columns for a question token
    def get_related_columns(question_token: str) -> List[str]:
        # if token matches a prefix pattern like Q3_ or Q3_ (without trailing digits), expand
        token = str(question_token)
        if token.endswith("_") or token.endswith("X") or token.endswith("r") or token.endswith("r_"):
            return [c for c in cols_order if c.startswith(token)]
        # if token not found but token + '_' prefix exists
        if token in df.columns:
            return [token]
        # try treating token as prefix with underscore
        if token + "_" in " ".join(cols_order):
            return [c for c in cols_order if c.startswith(token + "_")]
        # try prefix exact match
        return [token] if token in df.columns else []

    # iterate through rules and evaluate checks
    for idx, row in rules_df.iterrows():
        q = str(row["Question"]).strip()
        checks = [c.strip() for c in str(row["Check_Type"]).split(";") if str(c).strip() != ""]
        conds = [c.strip() for c in str(row.get("Condition", "")).split(";")]

        # for skip rules that target a range of variables or prefix, we'll handle inside skip branch
        # Determine related columns
        related_cols = []
        if q in df.columns:
            related_cols = [q]
        else:
            # try prefix expansions
            if q.endswith("_"):
                related_cols = [c for c in cols_order if c.startswith(q)]
            else:
                # also accept tokens like Q3_ (without underscore in rules)
                related_cols = [c for c in cols_order if c.startswith(q + "_") or c.startswith(q)]
                # keep only existent columns
                related_cols = [c for c in related_cols if c in df.columns]
        if not related_cols:
            # if rule references an aggregated prefix (like Q9_), still allow straightliner detection
            # but if nothing found, register missing question
            report.append({id_col: None, "Question": q, "Check_Type": ";".join(checks), "Issue": "Question not found in dataset"})
            continue

        # Evaluate Skip checks first to generate masks that determine which rows should be checked
        overall_mask_should_answer = pd.Series(True, index=df.index)  # default: everyone
        for i, chk in enumerate(checks):
            chk_lower = chk.lower()
            cond = conds[i] if i < len(conds) else ""
            if chk_lower == "skip":
                # expecting cond like "If A=1 then QX should be answered" or "... should be blank"
                if "then" not in cond.lower():
                    report.append({id_col: None, "Question": q, "Check_Type": "Skip", "Issue": f"Invalid skip condition ({cond})"})
                    continue
                if_part, then_part = re.split(r'(?i)then', cond, maxsplit=1)
                if_mask = parse_condition_to_mask(if_part, df)
                then_part = then_part.strip()
                should_be_blank = "blank" in then_part.lower()
                # determine targets listed in then_part (could be a single variable or range like Q3_1 to Q3_13 or prefix)
                # We'll extract tokens from then_part (first token(s) before words 'should' or 'to' or 'be')
                then_tokens = re.split(r'\s+should\b|\s+to\b|\s+be\b', then_part, flags=re.IGNORECASE)[0].strip()
                # handle cases like "Q3_1 to Q3_13" or "Q3_"
                target_cols = []
                if " to " in then_tokens.lower() or "-" in then_tokens:
                    target_cols = expand_range_expr(then_tokens, cols_order)
                else:
                    # split by comma in case multiple
                    for tok in re.split(r'[,\s]+', then_tokens):
                        tok = tok.strip()
                        if not tok:
                            continue
                        if tok.endswith("_"):
                            target_cols += [c for c in cols_order if c.startswith(tok)]
                        elif tok in df.columns:
                            target_cols.append(tok)
                        else:
                            # maybe token like Next Question -> map to next variable in raw file order
                            if tok.lower().startswith("next"):
                                # for skip conditions like "If OMQ2_r1<>1 then Next Question should be blank"
                                # map to the variable immediately after the "Skip From" or after the variable mentioned in if_part? We'll map based on the 'Skip From' if possible
                                # Try to find variable from if_part left side
                                mcol = re.split(r'\s*(<=|>=|<>|!=|=|<|>)\s*', if_part.strip())[0].strip()
                                if mcol in cols_order:
                                    pos = cols_order.index(mcol)
                                    if pos + 1 < len(cols_order):
                                        target_cols.append(cols_order[pos+1])
                            # otherwise ignore unknown tokens
                    # also if empty, fallback to related columns
                if not target_cols:
                    target_cols = related_cols

                for tcol in target_cols:
                    if tcol not in df.columns:
                        report.append({id_col: None, "Question": tcol, "Check_Type": "Skip", "Issue": "Skip target not found in dataset"})
                        continue
                    blank_mask = blank_mask_for_col(df, tcol)
                    not_blank_mask = ~blank_mask
                    # Respondents for which IF is true -> they SHOULD answer (if should_be_blank False), or SHOULD be blank (if should_be_blank True)
                    if should_be_blank:
                        # If IF true -> should be blank -> so those with not-blank are offenders
                        offenders = df.loc[if_mask & not_blank_mask, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": tcol, "Check_Type": "Skip", "Issue": "Answered but should be blank"})
                        # respondents where IF true & blank -> skip pass (exclude from further checks on that tcol)
                        # Mark them as excluded for subsequent checks on that target
                        # We'll use overall_mask_should_answer per target when applying other checks
                    else:
                        # IF true -> SHOULD answer -> those with blank are offenders
                        offenders = df.loc[if_mask & blank_mask, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": tcol, "Check_Type": "Skip", "Issue": "Blank but should be answered"})
                        # For subsequent checks for this tcol, only apply checks to rows where IF true.
                        # We'll set overall_mask_should_answer to IF mask for subsequent checks on these related cols
                        overall_mask_should_answer = overall_mask_should_answer & if_mask

        # Now evaluate other checks only on rows determined above (overall_mask_should_answer)
        for i, chk in enumerate(checks):
            chk_lower = chk.lower()
            cond = conds[i] if i < len(conds) else ""
            if chk_lower == "skip":
                continue
            if chk_lower == "range":
                # condition like '1-5' or '1 to 5'
                cstr = str(cond).replace("to", "-")
                if "-" not in cstr:
                    # invalid format
                    for rc in related_cols:
                        report.append({id_col: None, "Question": rc, "Check_Type": "Range", "Issue": f"Invalid range condition ({cond})"})
                    continue
                try:
                    minv, maxv = [float(x.strip()) for x in cstr.split("-", 1)]
                    for rc in related_cols:
                        # create mask of rows that should be checked for this variable
                        rows_check = overall_mask_should_answer.copy()
                        # exclude respondents that should be skipped because they satisfied some skip that said target should be blank (we didn't track per-target skip-pass set; this is a reasonable approach)
                        colvals = pd.to_numeric(df[rc], errors="coerce")
                        # Consider blank as separate issue; range check should only flag non-blank values outside range
                        non_blank = ~blank_mask_for_col(df, rc)
                        out_of_range = non_blank & (~colvals.between(minv, maxv))
                        offenders = df.loc[rows_check & out_of_range, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": rc, "Check_Type": "Range", "Issue": f"Value out of range ({int(minv)}-{int(maxv)})"})
                except Exception:
                    for rc in related_cols:
                        report.append({id_col: None, "Question": rc, "Check_Type": "Range", "Issue": f"Invalid range condition ({cond})"})

            elif chk_lower == "missing":
                for rc in related_cols:
                    rows_check = overall_mask_should_answer.copy()
                    blankm = blank_mask_for_col(df, rc)
                    offenders = df.loc[rows_check & blankm, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": rc, "Check_Type": "Missing", "Issue": "Value is missing"})

            elif chk_lower == "straightliner":
                # If rule's Question is a prefix pattern, expand and compare
                # For naming, rule might be "Q9_" or "Q9" etc.
                qtoken = q
                if qtoken.endswith("_"):
                    related = [c for c in cols_order if c.startswith(qtoken)]
                else:
                    # try to find group by looking for columns that start with q + '_' or q + 'r'
                    related = [c for c in cols_order if c.startswith(qtoken + "_") or c.startswith(qtoken)]
                    # if no multi items, fallback: try to find columns sharing prefix up to last underscore
                    if len(related) <= 1:
                        cut = qtoken.rfind("_")
                        if cut > 0:
                            related = [c for c in cols_order if c.startswith(qtoken[:cut+1])]
                if len(related) <= 1:
                    # nothing to do
                    continue
                same_resp = df[related].nunique(axis=1) == 1
                offenders = df.loc[overall_mask_should_answer & same_resp, id_col]
                for rid in offenders:
                    report.append({id_col: rid, "Question": ",".join(related), "Check_Type": "Straightliner", "Issue": "Same response across all items"})

            elif chk_lower == "multi-select":
                # related_cols may refer to prefix; pick group by prefix
                # e.g., Question = "Q2_" or "Q2"
                qtoken = q
                if qtoken.endswith("_"):
                    related = [c for c in cols_order if c.startswith(qtoken)]
                else:
                    related = [c for c in cols_order if c.startswith(qtoken + "_") or c.startswith(qtoken)]
                if not related:
                    continue
                # check only values 0/1
                for col in related:
                    invalid = ~df[col].isin([0, 1, "0", "1", 0.0, 1.0])
                    offenders = df.loc[overall_mask_should_answer & invalid, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": col, "Check_Type": "Multi-Select", "Issue": "Invalid value (not 0/1)"})
                # at least one option selected
                summed = df[related].fillna(0)
                # coerce non-numeric to 0 for sum
                try:
                    summed = summed.astype(float).sum(axis=1)
                except Exception:
                    # fallback: treat any '1' string as 1
                    summed = summed.apply(lambda row: sum(1 for v in row if str(v).strip() == "1"), axis=1)
                offenders = df.loc[overall_mask_should_answer & (summed == 0), id_col]
                for rid in offenders:
                    report.append({id_col: rid, "Question": q, "Check_Type": "Multi-Select", "Issue": "No options selected"})

            elif chk_lower == "openend_junk":
                for rc in related_cols:
                    junk = df[rc].astype(str).str.len() < 3
                    offenders = df.loc[overall_mask_should_answer & junk, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": rc, "Check_Type": "OpenEnd_Junk", "Issue": "Open-end looks like junk/low-effort"})

            elif chk_lower == "duplicate":
                for rc in related_cols:
                    dupes = df.loc[overall_mask_should_answer & df.duplicated(subset=[rc], keep=False), id_col]
                    for rid in dupes:
                        report.append({id_col: rid, "Question": rc, "Check_Type": "Duplicate", "Issue": "Duplicate value found"})

            else:
                # unknown check type - skip
                continue

    # Build report DataFrame and allow download
    report_df = pd.DataFrame(report)
    if report_df.empty:
        st.success("Validation completed! No issues found.")
    else:
        st.error(f"Validation completed! Found {len(report_df)} issues.")
        st.dataframe(report_df)

    # Download failed checks excel
    out_fail = io.BytesIO()
    with pd.ExcelWriter(out_fail, engine="openpyxl") as writer:
        report_df.to_excel(writer, index=False, sheet_name="Failed Checks")
    st.download_button("Download Failed Checks (Excel)", data=out_fail.getvalue(),
                       file_name="Failed_Checks.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.balloons()
