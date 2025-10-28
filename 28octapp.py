# 22octapp.py
import streamlit as st
import pandas as pd
import numpy as np
import re
import io
from datetime import datetime

st.set_page_config(page_title="DV Automation — Final Interactive", layout="wide")
st.title("🔎 Full Data Validation Automation — Interactive Review Flow")

st.markdown(
    "Flow: Generate Validation Rules → Review (or Upload revised Validation Rules.xlsx) → Confirm → Generate Validation Report. "
    "Two download buttons appear at the end: `Validation Rules.xlsx` and `Validation Report.xlsx`."
)

# ---------------- Sidebar: uploads and controls ----------------
st.sidebar.header("Upload files")
raw_file = st.sidebar.file_uploader("Raw Data (Excel or CSV)", type=["xlsx", "xls", "csv"])
skips_file = st.sidebar.file_uploader("Sawtooth Skips (CSV/XLSX)", type=["csv", "xlsx"])
rules_template_file = st.sidebar.file_uploader("Optional: Validation Rules template (xlsx)", type=["xlsx"])
run_btn = st.sidebar.button("Run: Build Validation Rules 🚀")

st.sidebar.markdown("---")
st.sidebar.header("Tuning parameters")
straightliner_threshold = st.sidebar.slider("Straightliner threshold", 0.50, 0.98, 0.85, 0.01)
junk_repeat_min = st.sidebar.slider("Junk OE: min repeated chars", 2, 8, 4, 1)
junk_min_length = st.sidebar.slider("Junk OE: min OE length", 1, 10, 2, 1)

# Check xlsxwriter availability
try:
    import xlsxwriter  # noqa: F401
    XLSXWRITER_AVAILABLE = True
except Exception:
    XLSXWRITER_AVAILABLE = False
    st.sidebar.warning("xlsxwriter not installed — Excel formatting will be basic. Add 'xlsxwriter' to requirements.txt for full formatting.")

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
            # use utf-8-sig to handle BOM
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

# Robust skip expression parser (supports AND/OR/parentheses/comparisons)
# Tokenize/parse approach (keeps previous robust parser)
token_spec = [
    ('LPAREN',  r'\('), ('RPAREN',  r'\)'),
    ('AND',     r'\bAND\b|\band\b'), ('OR', r'\bOR\b|\bor\b'),
    ('NEQ',     r'<>|!='), ('GTE', r'>='), ('LTE', r'<='), ('GT', r'>'), ('LT', r'<'), ('EQ', r'==|='),
    ('NUMBER',  r'\b\d+(\.\d+)?\b'), ('IDENT',   r'\b[A-Za-z0-9_\.]+\b'),
    ('QS',      r'\'[^\']*\'|"[^"]*"'), ('WS', r'\s+'), ('MISMATCH', r'.'),
]
tok_regex = '|'.join('(?P<%s>%s)' % pair for pair in token_spec)

def tokenize(expr):
    for mo in re.finditer(tok_regex, expr):
        kind = mo.lastgroup
        value = mo.group()
        if kind == 'WS':
            continue
        if kind == 'QS':
            yield ('STRING', value[1:-1])
        elif kind == 'NUMBER':
            yield ('NUMBER', value)
        elif kind == 'IDENT':
            yield ('IDENT', value)
        else:
            yield (kind, value)

class Parser:
    def __init__(self, tokens):
        self.tokens = [t for t in tokens]
        self.pos = 0
    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ('EOF','')
    def pop(self):
        t = self.peek(); self.pos += 1; return t
    def parse(self):
        node = self.expr()
        if self.peek()[0] != 'EOF':
            raise ValueError("Unexpected token")
        return node
    def expr(self):
        node = self.term()
        while self.peek()[0] == 'OR':
            self.pop(); right = self.term(); node = ('OR', node, right)
        return node
    def term(self):
        node = self.factor()
        while self.peek()[0] == 'AND':
            self.pop(); right = self.factor(); node = ('AND', node, right)
        return node
    def factor(self):
        tok = self.peek()
        if tok[0] == 'LPAREN':
            self.pop(); node = self.expr()
            if self.peek()[0] != 'RPAREN':
                raise ValueError("Missing )")
            self.pop(); return node
        else:
            return self.comparison()
    def comparison(self):
        left = self.pop()
        if left[0] != 'IDENT':
            raise ValueError("Left must be IDENT")
        op = self.pop()
        if op[0] not in {'EQ','NEQ','GT','LT','GTE','LTE'}:
            raise ValueError("Expected comparison")
        right = self.pop()
        if right[0] not in {'IDENT','STRING','NUMBER'}:
            raise ValueError("Expected value")
        return ('CMP', left[1], op[0], right[1])

def build_mask_from_ast(ast_node, df):
    if ast_node[0] == 'OR':
        return build_mask_from_ast(ast_node[1], df) | build_mask_from_ast(ast_node[2], df)
    if ast_node[0] == 'AND':
        return build_mask_from_ast(ast_node[1], df) & build_mask_from_ast(ast_node[2], df)
    if ast_node[0] == 'CMP':
        varname = ast_node[1]; op = ast_node[2]; raw_val = ast_node[3]
        if varname not in df.columns:
            # try variants
            if varname.upper() in df.columns:
                varname = varname.upper()
            elif varname.lower() in df.columns:
                varname = varname.lower()
            else:
                return pd.Series(False, index=df.index)
        left = df[varname].astype(str).str.strip()
        is_num = False
        try:
            rv_num = float(raw_val); is_num = True
        except Exception:
            rv_num = raw_val
        if is_num:
            coerced = pd.to_numeric(left, errors='coerce')
            if op == 'EQ': return coerced == rv_num
            if op == 'NEQ': return coerced != rv_num
            if op == 'GT': return coerced > rv_num
            if op == 'LT': return coerced < rv_num
            if op == 'GTE': return coerced >= rv_num
            if op == 'LTE': return coerced <= rv_num
        else:
            lv = left.str.lower(); rv = str(rv_num).lower()
            if op == 'EQ': return lv == rv
            if op == 'NEQ': return lv != rv
            coerced = pd.to_numeric(left, errors='coerce')
            try:
                valnum = float(raw_val)
                if op == 'GT': return coerced > valnum
                if op == 'LT': return coerced < valnum
                if op == 'GTE': return coerced >= valnum
                if op == 'LTE': return coerced <= valnum
            except Exception:
                return pd.Series(False, index=df.index)
    return pd.Series(False, index=df.index)

def parse_skip_expression_to_mask(expr, df):
    try:
        tokens = list(tokenize(expr))
        p = Parser(tokens)
        ast = p.parse()
        return build_mask_from_ast(ast, df).fillna(False).astype(bool)
    except Exception:
        # fallback simple parsing
        try:
            m = re.match(r'^\s*([A-Za-z0-9_\.]+)\s*(=|==|<>|!=|>|<|>=|<=)\s*(.+)\s*$', expr)
            if m:
                var, op, val = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().strip("'\"")
                if var not in df.columns: return pd.Series(False, index=df.index)
                left = df[var].astype(str).str.strip()
                try:
                    valnum = float(val); coerced = pd.to_numeric(left, errors='coerce')
                    if op in ('=','=='): return coerced == valnum
                    if op in ('!=','<>'): return coerced != valnum
                    if op == '>': return coerced > valnum
                    if op == '<': return coerced < valnum
                    if op == '>=': return coerced >= valnum
                    if op == '<=': return coerced <= valnum
                except Exception:
                    lv = left.str.lower(); rv = val.lower()
                    if op in ('=','=='): return lv == rv
                    if op in ('!=','<>'): return lv != rv
            return pd.Series(False, index=df.index)
        except Exception:
            return pd.Series(False, index=df.index)

# ---------------- MAIN RUN FLOW ----------------
if st.session_state.get("confirmed_report_ready") is None:
    st.session_state["confirmed_report_ready"] = False

if run_btn:
    # 1) validate uploads present
    if raw_file is None or skips_file is None:
        st.error("Please upload raw data and Sawtooth skips files before running.")
    else:
        progress = st.progress(0)
        status = st.empty()
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

        # 2) detect respondent id and remove BOM if any
        status.text("Detecting Respondent ID and filtering system vars...")
        possible_ids = ["RESPID","RespondentID","CaseID","caseid","id","ID","Respondent Id","sys_RespNum"]
        id_col = next((c for c in raw_df.columns if c in possible_ids), raw_df.columns[0])
        id_col = id_col.lstrip("\ufeff")
        # Filter out sys_ variables from working sets
        data_vars = [c for c in raw_df.columns if not str(c).lower().startswith("sys_")]
        progress.progress(20)

        # 3) build Validation Rules from skips (excluding sys_ vars)
        status.text("Building Validation Rules from Sawtooth skips...")
        validation_rules = []
        # heuristics for skip columns
        skips_lc = {c.lower(): c for c in skips_df.columns}
        logic_col = next((skips_lc[c] for c in skips_lc if 'logic' in c or 'condition' in c), None)
        from_col = next((skips_lc[c] for c in skips_lc if 'skip from' in c or c == 'from' or 'question' in c), None)
        to_col = next((skips_lc[c] for c in skips_lc if 'skip to' in c or c == 'to' or 'target' in c), None)

        if logic_col:
            for _, r in skips_df.iterrows():
                logic = r.get(logic_col, "")
                src = r.get(from_col, "") if from_col else ""
                tgt = r.get(to_col, "") if to_col else ""
                if pd.notna(logic) and str(logic).strip() != "":
                    # only create validation rule if src is not a sys_ var
                    src_str = str(src) if pd.notna(src) else ""
                    if not src_str.lower().startswith("sys_"):
                        validation_rules.append({
                            "Variable": src_str,
                            "Type": "Skip",
                            "Rule Applied": str(logic).strip(),
                            "Description": f"Skip {src_str} when {str(logic).strip()} (Target: {tgt})",
                            "Derived From": "Sawtooth Skip"
                        })
        progress.progress(40)

        # 4) auto-derived rules (Range, DK) for non-sys variables
        status.text("Adding auto rules (Range, DK) for non-system variables...")
        DK_CODES = [88, 99]; DK_STRINGS = ["DK","Refused","Don't know","Dont know","Refuse","REFUSED"]
        numeric_min, numeric_max = 0, 99
        for var in data_vars:
            validation_rules.append({
                "Variable": var,
                "Type": "Range",
                "Rule Applied": f"{numeric_min}-{numeric_max}",
                "Description": "Auto numeric range",
                "Derived From": "Auto"
            })
            validation_rules.append({
                "Variable": var,
                "Type": "DK/Refused",
                "Rule Applied": f"Codes {DK_CODES}; Tokens {DK_STRINGS}",
                "Description": "Auto DK/Refused detection",
                "Derived From": "Auto"
            })
        progress.progress(60)

        # 5) if user provided a rules template, append (but still exclude sys_ vars)
        status.text("Ingesting manual rules if provided...")
        applied_manual = []
        if rules_wb and isinstance(rules_wb, dict):
            for sheetname, df in rules_wb.items():
                cols_lower = [c.lower() for c in df.columns]
                if 'variable' in cols_lower and 'ruletype' in cols_lower:
                    var_col = df.columns[cols_lower.index('variable')]
                    rule_col = df.columns[cols_lower.index('ruletype')]
                    params_col = df.columns[cols_lower.index('params')] if 'params' in cols_lower else None
                    for _, rr in df.iterrows():
                        var = str(rr[var_col])
                        ruletype = rr[rule_col]
                        params = rr[params_col] if params_col is not None else ""
                        if not var.lower().startswith("sys_"):
                            applied_manual.append({"Variable": var, "RuleType": ruletype, "Params": params})
                            validation_rules.append({
                                "Variable": var,
                                "Type": str(ruletype),
                                "Rule Applied": str(params),
                                "Description": f"Manual rule from template {sheetname}",
                                "Derived From": f"Rules Template: {sheetname}"
                            })
        progress.progress(75)

        # Build Validation Rules dataframe for preview (preserve data_vars order)
        status.text("Preparing Validation Rules preview...")
        vr_df = pd.DataFrame(validation_rules)
        if vr_df.empty:
            vr_df = pd.DataFrame(columns=["Variable","Type","Rule Applied","Description","Derived From"])
        else:
            vr_df['Variable'] = vr_df['Variable'].fillna("").astype(str)
            def var_index(v):
                try:
                    return data_vars.index(v)
                except ValueError:
                    return len(data_vars) + 1
            vr_df['__ord'] = vr_df['Variable'].apply(var_index)
            vr_df = vr_df.sort_values(['__ord']).drop(columns='__ord')
            for col in ["Variable","Type","Rule Applied","Description","Derived From"]:
                if col not in vr_df.columns:
                    vr_df[col] = ""
            vr_df = vr_df[["Variable","Type","Rule Applied","Description","Derived From"]]
        st.subheader("Validation Rules — Preview (system variables excluded)")
        st.dataframe(vr_df, use_container_width=True)
        progress.progress(85)

        # Offer the user option to upload a revised Validation Rules.xlsx or confirm generated rules
        status.text("Review validation rules. You may upload a revised Validation Rules.xlsx (optional) or Confirm to generate the report.")
        uploaded_rules_override = st.file_uploader("Upload revised Validation Rules.xlsx (optional) — will be used instead of generated rules", type=["xlsx"])
        col1, col2 = st.columns(2)
        with col1:
            confirm_btn = st.button("✅ Confirm & Generate Validation Report")
        with col2:
            edit_btn = st.button("🔁 Re-run rule generation (discard preview and regenerate)")

        # If user uploaded revised rules, read them into vr_df_override
        vr_df_override = None
        if uploaded_rules_override is not None:
            try:
                vr_df_override = pd.read_excel(uploaded_rules_override, sheet_name=0)
                # Ensure required columns present
                expected_cols = ["Variable","Type","Rule Applied","Description","Derived From"]
                for c in expected_cols:
                    if c not in vr_df_override.columns:
                        st.warning(f"Uploaded rules missing column: {c}. The generated preview will be used instead.")
                        vr_df_override = None
                        break
                if vr_df_override is not None:
                    # filter out sys_ vars if any in uploaded
                    vr_df_override = vr_df_override[~vr_df_override['Variable'].astype(str).str.lower().str.startswith("sys_")]
                    st.success("Uploaded validation rules loaded and will be used when you Confirm.")
            except Exception as e:
                st.error("Could not read uploaded Validation Rules.xlsx — please check format. Error: " + str(e))
                vr_df_override = None

        progress.progress(90)

        # If "Re-run rule generation" clicked: redo (simple approach: reload run by setting a message)
        if edit_btn:
            st.info("Re-running rule generation. Press the main 'Run: Build Validation Rules' button again after adjusting inputs if needed.")
            progress.progress(0)
        # If confirm clicked, proceed to generate Validation Report using uploaded rules if provided otherwise generated preview
        if confirm_btn:
            final_vr_df = vr_df_override.copy() if vr_df_override is not None else vr_df.copy()
            # Ensure sys_ not present
            final_vr_df = final_vr_df[~final_vr_df['Variable'].astype(str).str.lower().str.startswith("sys_")].reset_index(drop=True)

            # Save the final rules to an in-memory Excel for download later
            rules_buf = io.BytesIO()
            try:
                engine_choice = "xlsxwriter" if XLSXWRITER_AVAILABLE else "openpyxl"
                with pd.ExcelWriter(rules_buf, engine=engine_choice) as writer:
                    final_vr_df.to_excel(writer, sheet_name="Validation_Rules", index=False)
                    if XLSXWRITER_AVAILABLE:
                        workbook = writer.book
                        worksheet = writer.sheets["Validation_Rules"]
                        header_format = workbook.add_format({'bold': True, 'bg_color': '#305496', 'font_color': 'white', 'border':1})
                        for col_num, value in enumerate(final_vr_df.columns.values):
                            worksheet.write(0, col_num, value, header_format)
                        worksheet.freeze_panes(1,1)
                        for i, col in enumerate(final_vr_df.columns):
                            try:
                                width = max(final_vr_df[col].astype(str).map(len).max(), len(str(col))) + 2
                                worksheet.set_column(i, i, min(60, width))
                            except Exception:
                                pass
                rules_buf.seek(0)
            except Exception as e:
                st.error("Could not prepare Validation Rules download: " + str(e))
                rules_buf = None

            # Now run checks using final_vr_df and raw_df (excluding sys_ vars)
            status.text("Running full validation checks using confirmed rules...")
            detailed_findings = []
            data_df = raw_df.copy()

            # Helper for collecting respondent IDs (limit to 200 IDs in list to keep file sizes reasonable)
            def format_ids(ids_series, max_ids=200):
                return ";".join(map(str, ids_series.astype(str).unique()[:max_ids].tolist()))

            # Duplicate IDs
            dup_mask = data_df.duplicated(subset=[id_col], keep=False)
            if dup_mask.sum() > 0:
                detailed_findings.append({
                    "Variable": id_col,
                    "Check_Type": "Duplicate IDs",
                    "Description": f"{int(dup_mask.sum())} duplicate rows (IDs duplicated)",
                    "Affected_Count": int(dup_mask.sum()),
                    "Respondent_IDs": format_ids(data_df.loc[dup_mask, id_col])
                })

            # Apply each rule from final_vr_df
            for _, rule in final_vr_df.iterrows():
                var = str(rule['Variable'])
                rtype = str(rule['Type']).strip().lower()
                r_applied = str(rule['Rule Applied'])
                # skip if var not in data (or is sys_)
                if var not in data_df.columns:
                    continue
                # Range
                if 'range' in rtype:
                    m = re.match(r'^\s*(\d+)\s*[-:]\s*(\d+)\s*$', r_applied)
                    lo, hi = numeric_min, numeric_max
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
                    # parse expression and build boolean mask where skip applies
                    try:
                        mask = parse_skip_expression_to_mask(r_applied, data_df)
                        violators = data_df[mask & data_df[var].notna()]
                        if len(violators) > 0:
                            detailed_findings.append({
                                "Variable": var,
                                "Check_Type": "Skip Violation",
                                "Description": f"{len(violators)} respondents answered {var} though skip ({r_applied}) applies",
                                "Affected_Count": int(len(violators)),
                                "Respondent_IDs": format_ids(violators[id_col])
                            })
                    except Exception as e:
                        # Skip parsing errors
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
                    mask = s.str.strip().str.lower().isin([t.lower() for t in DK_STRINGS]) | coerced.isin(DK_CODES)
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
                else:
                    # generic fallback checks: range-like numeric check and DK tokens
                    try:
                        coerced = pd.to_numeric(data_df[var], errors='coerce')
                        mask_num_out = (~coerced.isna()) & (~coerced.isin(DK_CODES)) & ((coerced < numeric_min) | (coerced > numeric_max))
                        if mask_num_out.sum() > 0:
                            detailed_findings.append({
                                "Variable": var,
                                "Check_Type": "Range Violation (Fallback)",
                                "Description": f"{int(mask_num_out.sum())} numeric values outside {numeric_min}-{numeric_max}",
                                "Affected_Count": int(mask_num_out.sum()),
                                "Respondent_IDs": format_ids(data_df.loc[mask_num_out, id_col])
                            })
                    except Exception:
                        pass

            # Straightliner detection across prefix groups (only non-sys vars)
            prefixes = {}
            for v in data_vars:
                p = re.split(r'[_\.]', v)[0]
                prefixes.setdefault(p, []).append(v)
            total_straight_flags = {}
            for prefix, cols in prefixes.items():
                if len(cols) >= 3:
                    sliners = find_straightliners(data_df, cols, threshold=straightliner_threshold)
                    if sliners:
                        idxs = list(sliners.keys())
                        detailed_findings.append({
                            "Variable": prefix,
                            "Check_Type": "Straightliner (Grid)",
                            "Description": f"{len(sliners)} respondents flagged as straightliners across {len(cols)} items",
                            "Affected_Count": int(len(sliners)),
                            "Respondent_IDs": format_ids(pd.Series(idxs))
                        })
                        for i in idxs:
                            total_straight_flags[i] = 1
            progress.progress(98)

            # Build final DataFrames for export
            detailed_df = pd.DataFrame(detailed_findings) if len(detailed_findings) > 0 else pd.DataFrame(columns=["Variable","Check_Type","Description","Affected_Count","Respondent_IDs"])
            summary_df = detailed_df.groupby("Check_Type", as_index=False)["Affected_Count"].sum().sort_values("Affected_Count", ascending=False) if not detailed_df.empty else pd.DataFrame(columns=["Check_Type","Affected_Count"])
            project_info = pd.DataFrame({
                "Report Generated":[datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")],
                "Raw Data Rows":[raw_df.shape[0]],
                "Raw Data Columns":[raw_df.shape[1]],
                "Respondent ID":[id_col],
                "Variables Validated":[len(data_vars)]
            })

            # Prepare Validation Report.xlsx (with Respondent_IDs in Detailed Checks)
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
                        sheet_map = {
                            "Detailed Checks": detailed_df,
                            "Summary": summary_df,
                            "Validation_Rules": final_vr_df,
                            "Project Info": project_info
                        }
                        for sheet_name, df_sheet in sheet_map.items():
                            try:
                                ws = writer.sheets[sheet_name]
                                ws.freeze_panes(1,1)
                                for col_num, value in enumerate(df_sheet.columns.values):
                                    ws.write(0, col_num, value, header_fmt)
                                for i, col in enumerate(df_sheet.columns):
                                    try:
                                        width = max(df_sheet[col].astype(str).map(len).max(), len(str(col))) + 2
                                        ws.set_column(i, i, min(60, width))
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                report_buf.seek(0)
            except Exception as e:
                st.error("Could not prepare Validation Report for download: " + str(e))
                report_buf = None

            # At this point both rules_buf and report_buf exist (if no errors). Show two download buttons
            st.success("Validation finished. Download the final files below.")
            if rules_buf is not None:
                st.download_button("📥 Download Validation Rules.xlsx", data=rules_buf, file_name="Validation Rules.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            else:
                st.error("Validation Rules file not available.")

            if report_buf is not None:
                st.download_button("📥 Download Validation Report.xlsx", data=report_buf, file_name="Validation Report.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            else:
                st.error("Validation Report file not available.")

            # mark session state to indicate report ready (useful if you want to show the same again without re-run)
            st.session_state["confirmed_report_ready"] = True
            progress.progress(100)
