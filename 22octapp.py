# 22octapp.py
import streamlit as st
import pandas as pd
import numpy as np
import re
import io
from datetime import datetime

st.set_page_config(page_title="DV Automation (Full)", layout="wide")
st.title("🔎 Full Data Validation Automation (Enterprise) — Final")

st.markdown("""
This app:
- converts Sawtooth skip logic into validation rules (supports AND / OR / parentheses / comparisons),
- generates `Validation_Rules` (Variable | Type | Rule Applied | Description | Derived From),
- runs full checks (Skip, Range, Mandatory, Straightliner, Multi-select, Junk OE, DK/Refused, Combined),
- outputs a formatted `Data_Validation_Report_Final.xlsx` with a Respondent-level violations sheet,
- exposes tuning sliders for straightliner and junk-OE heuristics.
""")

# ------------------ UPLOADS ------------------
st.sidebar.header("Upload files")
raw_file = st.sidebar.file_uploader("Raw Data (Excel/CSV)", type=["xlsx", "xls", "csv"])
skips_file = st.sidebar.file_uploader("Sawtooth Skips (CSV/XLSX)", type=["csv", "xlsx"])
rules_file = st.sidebar.file_uploader("Optional: Validation Rules template (xlsx)", type=["xlsx"])
run_btn = st.sidebar.button("Run Full DV Automation 🚀")

# Threshold controls
st.sidebar.header("Tuning parameters")
straightliner_threshold = st.sidebar.slider("Straightliner threshold (fraction of same answers)", 0.50, 0.98, 0.85, 0.01)
junk_repeat_min = st.sidebar.slider("Junk OE: min repeated characters to flag", 2, 8, 4, 1)
junk_min_length = st.sidebar.slider("Junk OE: minimum OE length to consider (short <-> more strict)", 1, 10, 2, 1)

# ------------------ HELPERS ------------------
def read_any_df(uploaded):
    if uploaded is None:
        return None
    name = uploaded.name.lower()
    try:
        if name.endswith(".xlsx") or name.endswith(".xls"):
            return pd.read_excel(uploaded, engine="openpyxl")
        else:
            return pd.read_csv(uploaded, encoding="ISO-8859-1")
    except Exception:
        uploaded.seek(0)
        try:
            return pd.read_csv(uploaded, encoding="utf-8", errors="replace")
        except Exception:
            uploaded.seek(0)
            return pd.read_excel(uploaded, engine="openpyxl")

def is_multi_select_series(s):
    vals = s.dropna().astype(str).str.strip().str.lower().unique()
    if len(vals)==0:
        return False
    if set(vals).issubset(set(['0','1'])) or set(vals).issubset(set(['checked','unchecked','1','0'])):
        return True
    if len(vals) == 2 and any(v in ('checked','unchecked','1','0','true','false') for v in vals):
        return True
    return False

def detect_junk_oe(value, junk_repeat_min=4, junk_min_length=2):
    if pd.isna(value):
        return False
    s = str(value).strip()
    if s == "":
        return True
    # numeric-only short OE
    if s.isdigit() and len(s) <= 3:
        return True
    # repeated characters (e.g., "aaaaa")
    if re.match(r'^(.)\1{' + str(max(1,junk_repeat_min-1)) + r',}$', s):
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

# ------------------ Skip expression parser (AND/OR support) ------------------
# We'll tokenize and build an AST, then evaluate vectorized against raw_df
token_spec = [
    ('LPAREN',  r'\('),
    ('RPAREN',  r'\)'),
    ('AND',     r'\bAND\b|\band\b'),
    ('OR',      r'\bOR\b|\bor\b'),
    ('NEQ',     r'<>|!='),
    ('GTE',     r'>='),
    ('LTE',     r'<='),
    ('GT',      r'>'),
    ('LT',      r'<'),
    ('EQ',      r'==|='),
    ('NUMBER',  r'\b\d+(\.\d+)?\b'),
    ('IDENT',   r'\b[A-Za-z0-9_\.]+\b'),
    ('QS',      r'\'[^\']*\'|"[^"]*"'),  # quoted string
    ('WS',      r'\s+'),
    ('MISMATCH', r'.'),
]
tok_regex = '|'.join('(?P<%s>%s)' % pair for pair in token_spec)
Token = tuple

def tokenize(expr):
    for mo in re.finditer(tok_regex, expr):
        kind = mo.lastgroup
        value = mo.group()
        if kind == 'WS':
            continue
        if kind == 'QS':
            # strip quotes
            value = value[1:-1]
            yield ('STRING', value)
        elif kind == 'NUMBER':
            yield ('NUMBER', value)
        elif kind == 'IDENT':
            yield ('IDENT', value)
        elif kind == 'MISMATCH':
            yield ('MISMATCH', value)
        else:
            yield (kind, value)

# Parser (recursive descent)
class Parser:
    def __init__(self, tokens):
        self.tokens = [t for t in tokens]
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return ('EOF', '')

    def pop(self):
        t = self.peek()
        self.pos += 1
        return t

    def parse(self):
        node = self.expr()
        if self.peek()[0] != 'EOF':
            raise ValueError("Unexpected token after expression")
        return node

    # expr := term (OR term)*
    def expr(self):
        node = self.term()
        while self.peek()[0] == 'OR':
            self.pop()
            right = self.term()
            node = ('OR', node, right)
        return node

    # term := factor (AND factor)*
    def term(self):
        node = self.factor()
        while self.peek()[0] == 'AND':
            self.pop()
            right = self.factor()
            node = ('AND', node, right)
        return node

    # factor := comparison | LPAREN expr RPAREN
    def factor(self):
        tok = self.peek()
        if tok[0] == 'LPAREN':
            self.pop()
            node = self.expr()
            if self.peek()[0] != 'RPAREN':
                raise ValueError("Missing closing parenthesis")
            self.pop()
            return node
        else:
            return self.comparison()

    # comparison := IDENT (EQ|NEQ|GT|LT|GTE|LTE) (IDENT|STRING|NUMBER)
    def comparison(self):
        left = self.pop()
        if left[0] != 'IDENT':
            # sometimes sawtooth logic includes full variable::attribute — still accept IDENT-like tokens
            raise ValueError(f"Expected variable identifier, got {left}")
        op = self.pop()
        if op[0] not in {'EQ','NEQ','GT','LT','GTE','LTE'}:
            raise ValueError(f"Expected comparison operator, got {op}")
        right = self.pop()
        if right[0] not in {'IDENT','STRING','NUMBER'}:
            raise ValueError(f"Expected value, got {right}")
        return ('CMP', left[1], op[0], right[1])

def build_mask_from_ast(ast_node, df):
    # returns a boolean Series mask aligned with df.index
    if ast_node[0] == 'OR':
        return build_mask_from_ast(ast_node[1], df) | build_mask_from_ast(ast_node[2], df)
    if ast_node[0] == 'AND':
        return build_mask_from_ast(ast_node[1], df) & build_mask_from_ast(ast_node[2], df)
    if ast_node[0] == 'CMP':
        varname = ast_node[1]
        op = ast_node[2]
        raw_val = ast_node[3]
        # variable could be like "Segment_1" or "Q1", if not present in df, return all False
        if varname not in df.columns:
            # try upper/lower variants
            if varname.upper() in df.columns:
                varname = varname.upper()
            elif varname.lower() in df.columns:
                varname = varname.lower()
            else:
                return pd.Series([False]*len(df), index=df.index)
        left = df[varname].astype(str).str.strip()
        # right value numeric or string?
        # try numeric
        is_num = False
        try:
            rv_num = float(raw_val)
            is_num = True
        except Exception:
            rv_num = raw_val
        if is_num:
            # do numeric comparison where possible
            coerced = pd.to_numeric(left, errors='coerce')
            if op == 'EQ':
                return coerced == rv_num
            if op == 'NEQ':
                return coerced != rv_num
            if op == 'GT':
                return coerced > rv_num
            if op == 'LT':
                return coerced < rv_num
            if op == 'GTE':
                return coerced >= rv_num
            if op == 'LTE':
                return coerced <= rv_num
        else:
            # string comparison (case-insensitive)
            lv = left.str.lower()
            rv = str(rv_num).lower()
            if op == 'EQ':
                return lv == rv
            if op == 'NEQ':
                return lv != rv
            # for other ops, attempt numeric coercion fallback
            coerced = pd.to_numeric(left, errors='coerce')
            try:
                valnum = float(raw_val)
                if op == 'GT':
                    return coerced > valnum
                if op == 'LT':
                    return coerced < valnum
                if op == 'GTE':
                    return coerced >= valnum
                if op == 'LTE':
                    return coerced <= valnum
            except Exception:
                # unsupported, return False mask
                return pd.Series([False]*len(df), index=df.index)
    # default fallback
    return pd.Series([False]*len(df), index=df.index)

def parse_skip_expression_to_mask(expr, df):
    # returns boolean Series mask where expression is TRUE
    try:
        tokens = list(tokenize(expr))
        p = Parser(tokens)
        ast = p.parse()
        mask = build_mask_from_ast(ast, df)
        # ensure boolean dtype
        return mask.fillna(False).astype(bool)
    except Exception:
        # if parsing fails, fallback to simple contains heuristics
        # attempt to parse simple "VAR = VALUE" with regex
        try:
            m = re.match(r'^\s*([A-Za-z0-9_\.]+)\s*(=|==|<>|!=|>|<|>=|<=)\s*(.+)\s*$', expr)
            if m:
                var, op, val = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().strip("'\"")
                if var not in df.columns:
                    return pd.Series([False]*len(df), index=df.index)
                left = df[var].astype(str).str.strip()
                # numeric?
                try:
                    valnum = float(val)
                    coerced = pd.to_numeric(left, errors='coerce')
                    if op in ('=','=='):
                        return coerced == valnum
                    if op in ('!=','<>'):
                        return coerced != valnum
                    if op == '>':
                        return coerced > valnum
                    if op == '<':
                        return coerced < valnum
                    if op == '>=':
                        return coerced >= valnum
                    if op == '<=':
                        return coerced <= valnum
                except Exception:
                    lv = left.str.lower(); rv = val.lower()
                    if op in ('=','=='):
                        return lv == rv
                    if op in ('!=','<>'):
                        return lv != rv
            return pd.Series([False]*len(df), index=df.index)
        except Exception:
            return pd.Series([False]*len(df), index=df.index)

# ------------------ RUN VALIDATION ------------------
if run_btn:
    if raw_file is None or skips_file is None:
        st.error("Please upload both Raw Data and Sawtooth Skips file.")
    else:
        with st.spinner("Loading files..."):
            raw_df = read_any_df(raw_file)
            skips_df = read_any_df(skips_file)
            rules_wb = None
            if rules_file:
                try:
                    rules_wb = pd.read_excel(rules_file, sheet_name=None)
                except Exception:
                    rules_wb = None

        st.success(f"Loaded raw data (rows: {raw_df.shape[0]}, cols: {raw_df.shape[1]}) and skips ({skips_df.shape[0]} rows).")

        # respondent id detection
        candidates = ["RESPID","RespondentID","CaseID","caseid","id","ID","Respondent Id","CASEID"]
        id_col = next((c for c in raw_df.columns if c in candidates), raw_df.columns[0])
        st.info(f"Detected respondent ID column: **{id_col}**")

        # DK settings
        DK_CODES = [88, 99]
        DK_STRINGS = ["DK", "Refused", "Don't know", "Dont know", "Refuse", "REFUSED"]
        numeric_min, numeric_max = 0, 99

        # Build Validation Rules from Skips
        validation_rules = []
        skips_lc = {c.lower(): c for c in skips_df.columns}
        # Try to detect standard columns
        from_col = next((skips_lc[c] for c in skips_lc if 'skip from' in c or 'from'==c or 'skip_from' in c), None)
        to_col = next((skips_lc[c] for c in skips_lc if 'skip to' in c or 'to'==c or 'skip_to' in c), None)
        logic_col = next((skips_lc[c] for c in skips_lc if 'logic' in c or 'condition' in c), None)
        question_col = next((skips_lc[c] for c in skips_lc if 'question' in c), None)
        skipif_col = next((skips_lc[c] for c in skips_lc if 'skip if' in c or 'skipif' in c), None)
        condition_col = next((skips_lc[c] for c in skips_lc if 'condition' in c), None)

        # If there's a logic column, convert rows into rules
        if logic_col:
            for _, r in skips_df.iterrows():
                logic = r.get(logic_col, "")
                src = r.get(from_col, "") if from_col else (r.get(question_col, "") if question_col else "")
                tgt = r.get(to_col, "") if to_col else ""
                if pd.notna(logic) and str(logic).strip() != "":
                    rule_text = str(logic).strip()
                    validation_rules.append({
                        "Variable": str(src) if pd.notna(src) else "",
                        "Type": "Skip",
                        "Rule Applied": rule_text,
                        "Description": f"Skip {src} when {rule_text} (Target: {tgt})",
                        "Derived From": "Sawtooth Skip"
                    })
        # Fallback skipif pattern
        if skipif_col and condition_col and question_col:
            for _, r in skips_df.iterrows():
                q = r.get(question_col)
                cond = r.get(condition_col)
                skipif = r.get(skipif_col)
                if pd.notna(q) and pd.notna(cond) and pd.notna(skipif):
                    validation_rules.append({
                        "Variable": q,
                        "Type": "Skip",
                        "Rule Applied": f"{cond} == {skipif}",
                        "Description": f"Skip {q} when {cond} == {skipif}",
                        "Derived From": "Sawtooth Skip"
                    })

        # Auto-infer variable types and base auto-rules
        var_order = list(raw_df.columns)
        var_types = {}
        for var in var_order:
            s = raw_df[var]
            numeric_series = pd.to_numeric(s, errors="coerce")
            num_non_null = numeric_series.notna().sum()
            unique_vals = s.dropna().astype(str).str.strip().unique()
            if is_multi_select_series(s):
                vtype = "Multi-select"
            elif num_non_null / max(1, len(s)) > 0.6 and num_non_null > 5:
                vtype = "Numeric"
            elif s.dropna().astype(str).str.len().mean() > 20:
                vtype = "Open-Ended"
            else:
                vtype = "Categorical"
            var_types[var] = vtype

            if vtype == "Numeric":
                validation_rules.append({
                    "Variable": var,
                    "Type": "Range",
                    "Rule Applied": f"{numeric_min}-{numeric_max}",
                    "Description": f"Numeric values expected between {numeric_min} and {numeric_max}",
                    "Derived From": "Auto"
                })
            if vtype == "Multi-select":
                validation_rules.append({
                    "Variable": var,
                    "Type": "Multi-select",
                    "Rule Applied": "Checked only; Base = respondents with non-missing set",
                    "Description": "Multi-response variable; expect Checked/Unchecked or 0/1",
                    "Derived From": "Auto"
                })
            if vtype == "Open-Ended":
                validation_rules.append({
                    "Variable": var,
                    "Type": "Junk OE",
                    "Rule Applied": "Junk heuristics (repeats, numeric-only short, non-alnum-heavy)",
                    "Description": "Detect possible junk open-ends",
                    "Derived From": "Auto"
                })
            validation_rules.append({
                "Variable": var,
                "Type": "DK/Refused",
                "Rule Applied": f"Numeric codes {DK_CODES}; text tokens {DK_STRINGS}",
                "Description": "Detect DK/Refused usage",
                "Derived From": "Auto"
            })

        # Ingest manual rules template if present
        applied_rules_manual = []
        if rules_wb and isinstance(rules_wb, dict):
            for sheetname, df in rules_wb.items():
                cols_lower = [c.lower() for c in df.columns]
                if 'variable' in cols_lower and 'ruletype' in cols_lower:
                    var_col = df.columns[cols_lower.index('variable')]
                    rule_col = df.columns[cols_lower.index('ruletype')]
                    params_col = None
                    if 'params' in cols_lower:
                        params_col = df.columns[cols_lower.index('params')]
                    for _, r in df.iterrows():
                        var = r[var_col]
                        ruletype = r[rule_col]
                        params = r[params_col] if params_col else ""
                        applied_rules_manual.append({"Variable": var, "RuleType": ruletype, "Params": params})
                        validation_rules.append({
                            "Variable": var,
                            "Type": str(ruletype),
                            "Rule Applied": str(params),
                            "Description": f"Manual rule from template sheet {sheetname}",
                            "Derived From": f"Rules Template: {sheetname}"
                        })

        # Build validation rules df preserving dataset order
        vr_df = pd.DataFrame(validation_rules)
        vr_df['Variable'] = vr_df['Variable'].fillna("").astype(str)
        def var_index(v):
            try:
                return var_order.index(v)
            except ValueError:
                return len(var_order) + 1
        if not vr_df.empty:
            vr_df['__ord'] = vr_df['Variable'].apply(var_index)
            vr_df = vr_df.sort_values(['__ord']).drop(columns='__ord')
        for col in ["Variable", "Type", "Rule Applied", "Description", "Derived From"]:
            if col not in vr_df.columns:
                vr_df[col] = ""
        vr_df = vr_df[["Variable","Type","Rule Applied","Description","Derived From"]]

        st.subheader("Validation Rules (preview)")
        st.dataframe(vr_df.head(200), use_container_width=True)

        # ---------------- APPLY CHECKS ----------------
        detailed_findings = []

        # Duplicate IDs
        dup_mask = raw_df.duplicated(subset=[id_col], keep=False)
        if dup_mask.sum() > 0:
            detailed_findings.append({
                "Variable": id_col,
                "Check_Type": "Duplicate IDs",
                "Description": f"{int(dup_mask.sum())} duplicate rows (IDs duplicated)",
                "Affected_Count": int(dup_mask.sum()),
                "Examples": ";".join(map(str, raw_df.loc[dup_mask, id_col].unique()[:10]))
            })

        # Mandatory from manual rules
        for rr in applied_rules_manual:
            var = rr.get("Variable")
            if var in raw_df.columns and str(rr.get("RuleType","")).strip().lower() == "mandatory":
                miss = int(raw_df[var].isnull().sum())
                if miss > 0:
                    ex = raw_df.loc[raw_df[var].isnull(), id_col].astype(str).head(10).tolist()
                    detailed_findings.append({
                        "Variable": var,
                        "Check_Type": "Mandatory Missing",
                        "Description": f"Manual mandatory rule: {miss} missing responses",
                        "Affected_Count": miss,
                        "Examples": ";".join(ex)
                    })

        # Range checks (use vr_df if available)
        for var in var_order:
            if var not in raw_df.columns:
                continue
            if var in var_types and var_types[var] != "Numeric":
                # still check if explicit Range rule exists in vr_df
                pass
            coerced = pd.to_numeric(raw_df[var], errors='coerce')
            # find range rule in vr_df if present
            var_ranges = vr_df[(vr_df['Variable'] == var) & (vr_df['Type'].str.lower().str.contains("range", na=False))]
            lo, hi = numeric_min, numeric_max
            if not var_ranges.empty:
                ra = str(var_ranges.iloc[0]['Rule Applied'])
                m = re.match(r'^\s*(\d+)\s*[-:]\s*(\d+)\s*$', ra)
                if m:
                    lo, hi = int(m.group(1)), int(m.group(2))
            mask_valid_num = coerced.notna() & (~coerced.isin(DK_CODES))
            mask_out = mask_valid_num & ((coerced < lo) | (coerced > hi))
            out_count = int(mask_out.sum())
            if out_count > 0:
                ex_ids = raw_df.loc[mask_out, id_col].astype(str).head(10).tolist()
                detailed_findings.append({
                    "Variable": var,
                    "Check_Type": "Range Violation",
                    "Description": f"{out_count} numeric values outside {lo}-{hi}",
                    "Affected_Count": out_count,
                    "Examples": ";".join(ex_ids)
                })

        # Skip violations - parse VR skip rules and evaluate vectorized
        skip_rules = vr_df[vr_df['Type'].str.lower().str.contains("skip", na=False)]
        # for each skip rule, parse the rule expression to mask; if mask True and target var present and not null -> violation
        for _, r in skip_rules.iterrows():
            target_var = r['Variable']
            rule_expr = str(r['Rule Applied']).strip()
            if target_var and target_var in raw_df.columns and rule_expr:
                mask = parse_skip_expression_to_mask(rule_expr, raw_df)
                # respondents who should have skipped (mask True) but have non-missing in target_var
                violators = raw_df[mask & raw_df[target_var].notna()]
                if len(violators) > 0:
                    detailed_findings.append({
                        "Variable": target_var,
                        "Check_Type": "Skip Violation",
                        "Description": f"{len(violators)} respondents answered {target_var} though skip condition ({rule_expr}) applies",
                        "Affected_Count": int(len(violators)),
                        "Examples": ";".join(violators[id_col].astype(str).head(10).tolist())
                    })

        # DK/Refused checks for all variables
        for var in var_order:
            if var not in raw_df.columns:
                continue
            s = raw_df[var]
            coerced = pd.to_numeric(s, errors='coerce')
            dk_num = int(coerced.isin(DK_CODES).sum())
            dk_text = int(s.astype(str).str.strip().str.lower().isin([t.lower() for t in DK_STRINGS]).sum())
            total_dk = dk_num + dk_text
            if total_dk > 0:
                ex_ids = raw_df.loc[(coerced.isin(DK_CODES)) | (s.astype(str).str.strip().str.lower().isin([t.lower() for t in DK_STRINGS])), id_col].astype(str).head(10).tolist()
                detailed_findings.append({
                    "Variable": var,
                    "Check_Type": "DK/Refused",
                    "Description": f"{total_dk} DK/Refused occurrences (numeric/text)",
                    "Affected_Count": total_dk,
                    "Examples": ";".join(ex_ids)
                })

        # Multi-select invalid codes
        for var in var_order:
            if var_types.get(var) == "Multi-select":
                s = raw_df[var].dropna().astype(str).str.strip().str.lower()
                allowed = set(['0','1','checked','unchecked','true','false'])
                unique_vals = set(s.unique())
                invalid = [v for v in unique_vals if v not in allowed]
                if invalid:
                    mask_invalid = raw_df[var].astype(str).str.strip().str.lower().isin(invalid)
                    count_invalid = int(mask_invalid.sum())
                    detailed_findings.append({
                        "Variable": var,
                        "Check_Type": "Multi-select Invalid Codes",
                        "Description": f"{count_invalid} invalid multi-select values detected: {invalid}",
                        "Affected_Count": count_invalid,
                        "Examples": ";".join(raw_df.loc[mask_invalid, id_col].astype(str).head(10).tolist())
                    })

        # Junk OE detection
        for var in var_order:
            if var_types.get(var) == "Open-Ended":
                series = raw_df[var]
                junk_mask = series.apply(lambda x: detect_junk_oe(x, junk_repeat_min, junk_min_length))
                junk_count = int(junk_mask.sum())
                if junk_count > 0:
                    detailed_findings.append({
                        "Variable": var,
                        "Check_Type": "Junk OE",
                        "Description": f"{junk_count} open-end responses flagged as junk (heuristics)",
                        "Affected_Count": junk_count,
                        "Examples": ";".join(raw_df.loc[junk_mask, id_col].astype(str).head(10).tolist())
                    })

        # Straightliner detection across prefix groupings
        prefixes = {}
        for var in var_order:
            p = re.split(r'[_\.]', var)[0]
            prefixes.setdefault(p, []).append(var)
        for prefix, cols in prefixes.items():
            if len(cols) >= 3:
                sliners = find_straightliners(raw_df, cols, threshold=straightliner_threshold)
                if sliners:
                    detailed_findings.append({
                        "Variable": prefix,
                        "Check_Type": "Straightliner (Grid)",
                        "Description": f"{len(sliners)} respondents flagged as straightliners across {len(cols)} items (prefix: {prefix})",
                        "Affected_Count": int(len(sliners)),
                        "Examples": ";".join(list(map(str, list(sliners.keys())[:10])))
                    })

        # Combined violations: construct boolean masks for main checks and sum for each respondent
        combined_masks = {}
        # Skip masks: for each skip rule, store mask of violators
        for _, r in skip_rules.iterrows():
            tgt = r['Variable']
            rule_expr = str(r['Rule Applied']).strip()
            if tgt and tgt in raw_df.columns and rule_expr:
                mask = parse_skip_expression_to_mask(rule_expr, raw_df) & raw_df[tgt].notna()
                combined_masks[f"skip_{tgt}"] = mask

        # Range masks for numeric variables
        for var in var_order:
            if var_types.get(var) == "Numeric":
                coerced = pd.to_numeric(raw_df[var], errors='coerce')
                mask = (~coerced.isna()) & (~coerced.isin(DK_CODES)) & ((coerced < numeric_min) | (coerced > numeric_max))
                combined_masks[f"range_{var}"] = mask

        # Junk OE masks
        for var in var_order:
            if var_types.get(var) == "Open-Ended":
                combined_masks[f"junk_{var}"] = raw_df[var].apply(lambda x: detect_junk_oe(x, junk_repeat_min, junk_min_length))

        # DK masks
        for var in var_order:
            mask_text = raw_df[var].astype(str).str.strip().str.lower().isin([t.lower() for t in DK_STRINGS])
            coerced = pd.to_numeric(raw_df[var], errors='coerce')
            mask_num = coerced.isin(DK_CODES)
            combined_masks[f"dk_{var}"] = mask_text | mask_num

        # Multi-select invalid mask
        for var in var_order:
            if var_types.get(var) == "Multi-select":
                s = raw_df[var].astype(str).str.strip().str.lower()
                allowed = set(['0','1','checked','unchecked','true','false'])
                mask_invalid = ~s.isin(list(allowed))
                combined_masks[f"multi_invalid_{var}"] = mask_invalid

        # Build dataframe of masks and compute respondent totals
        if combined_masks:
            masks_df = pd.DataFrame(combined_masks, index=raw_df.index).fillna(False).astype(bool)
            masks_df['total_violations'] = masks_df.sum(axis=1)
            # respondents with >=2 different violation types
            multiple_mask = masks_df['total_violations'] >= 2
            if multiple_mask.sum() > 0:
                detailed_findings.append({
                    "Variable": "Multiple",
                    "Check_Type": "Combined Violations",
                    "Description": f"{int(multiple_mask.sum())} respondents have >=2 different violation types",
                    "Affected_Count": int(multiple_mask.sum()),
                    "Examples": ";".join(raw_df.loc[multiple_mask, id_col].astype(str).head(10).tolist())
                })
        else:
            masks_df = pd.DataFrame(index=raw_df.index)
            masks_df['total_violations'] = 0

        # ---------------- BUILD REPORTS ----------------
        if len(detailed_findings) > 0:
            detailed_df = pd.DataFrame(detailed_findings)
            summary_df = detailed_df.groupby("Check_Type", as_index=False)["Affected_Count"].sum().sort_values("Affected_Count", ascending=False)
        else:
            detailed_df = pd.DataFrame(columns=["Variable","Check_Type","Description","Affected_Count","Examples"])
            summary_df = pd.DataFrame(columns=["Check_Type","Affected_Count"])

        project_info = pd.DataFrame({
            "Report Generated": [datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")],
            "Raw Data Rows": [raw_df.shape[0]],
            "Raw Data Columns": [raw_df.shape[1]],
            "Respondent ID Column": [id_col],
            "Var Count": [len(var_order)]
        })

        # Build Respondent_Violations sheet: counts per violation category for each respondent
        resp_viol = pd.DataFrame({id_col: raw_df[id_col].astype(str)})
        # aggregate selected masks into meaningful columns
        # We'll compute presence (1/0) per high-level category
        resp_viol['Missing'] = raw_df.isnull().sum(axis=1)  # number of missing cells (not a check, but useful)
        # Create high-level flags: Skip, Range, JunkOE, Straightliner, MultiInvalid, DK
        resp_viol['Skip'] = 0
        resp_viol['Range'] = 0
        resp_viol['JunkOE'] = 0
        resp_viol['Straightliner'] = 0
        resp_viol['MultiInvalid'] = 0
        resp_viol['DK'] = 0

        # populate from masks_df where possible
        for colmask in masks_df.columns:
            if colmask.startswith('skip_'):
                resp_viol['Skip'] = resp_viol['Skip'] | masks_df[colmask].astype(int)
            if colmask.startswith('range_'):
                resp_viol['Range'] = resp_viol['Range'] | masks_df[colmask].astype(int)
            if colmask.startswith('junk_'):
                resp_viol['JunkOE'] = resp_viol['JunkOE'] | masks_df[colmask].astype(int)
            if colmask.startswith('multi_invalid_'):
                resp_viol['MultiInvalid'] = resp_viol['MultiInvalid'] | masks_df[colmask].astype(int)
            if colmask.startswith('dk_'):
                resp_viol['DK'] = resp_viol['DK'] | masks_df[colmask].astype(int)

        # Straightliner: use earlier detection via find_straightliners "sliners" variable groups
        # We'll recompute and mark respondents flagged anywhere
        straight_flags = pd.Series(0, index=raw_df.index)
        for prefix, cols in prefixes.items():
            if len(cols) >= 3:
                sdet = find_straightliners(raw_df, cols, threshold=straightliner_threshold)
                for idx in sdet.keys():
                    straight_flags.loc[idx] = 1
        resp_viol['Straightliner'] = straight_flags.astype(int)

        # Total violation count
        resp_viol['TotalViolations'] = resp_viol[['Skip','Range','JunkOE','Straightliner','MultiInvalid','DK']].sum(axis=1)

        # ---------------- EXPORT TO EXCEL with formatting to match sample ----------------
        output_name = "Data_Validation_Report_Final.xlsx"

        def write_workbook_bytes(buf):
            with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
                # order sheets: Detailed Checks, Summary, Validation_Rules, Project Info, Respondent_Violations
                detailed_df.to_excel(writer, sheet_name="Detailed Checks", index=False)
                summary_df.to_excel(writer, sheet_name="Summary", index=False)
                vr_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
                project_info.to_excel(writer, sheet_name="Project Info", index=False)
                resp_viol.to_excel(writer, sheet_name="Respondent_Violations", index=False)

                workbook = writer.book
                # header style: dark blue similar to sample
                header_format = workbook.add_format({'bold': True, 'bg_color': '#305496', 'font_color': 'white', 'border':1})
                border_fmt = workbook.add_format({'border':1})
                sheet_map = {
                    "Detailed Checks": detailed_df,
                    "Summary": summary_df,
                    "Validation_Rules": vr_df,
                    "Project Info": project_info,
                    "Respondent_Violations": resp_viol
                }
                for sheet_name, df_sheet in sheet_map.items():
                    try:
                        ws = writer.sheets[sheet_name]
                        # freeze header row and first column
                        ws.freeze_panes(1, 1)
                        # apply header
                        for col_num, value in enumerate(df_sheet.columns.values):
                            ws.write(0, col_num, value, header_format)
                        # set column widths
                        for i, col in enumerate(df_sheet.columns):
                            try:
                                max_len = max(df_sheet[col].astype(str).map(len).max(), len(str(col))) + 2
                                ws.set_column(i, i, min(60, max_len))
                            except Exception:
                                pass
                    except Exception:
                        pass

        # produce in-memory xlsx
        bio = io.BytesIO()
        write_workbook_bytes(bio)
        bio.seek(0)

        # UI: show summary and download
        st.subheader("✅ DV Summary")
        st.dataframe(summary_df, use_container_width=True)
        st.download_button("📥 Download Data Validation Report", data=bio, file_name=output_name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.success("DV automation completed with AND/OR skip parsing, Excel formatting, Respondent_Violations sheet, and threshold sliders.")
