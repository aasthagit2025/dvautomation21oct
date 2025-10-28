# Full DV automation run (visible to user). Generates Data_Validation_Report_Final.xlsx
import pandas as pd
import numpy as np
import os
from datetime import datetime
import ace_tools as tools

# File paths (from uploaded files)
raw_paths = [
    "/mnt/data/raw data.xlsx",
    "/mnt/data/raw data1.csv",
    "/mnt/data/raw data.csv"
]

# Find which raw data file exists and load appropriately
raw_df = None
for p in raw_paths:
    if os.path.exists(p):
        try:
            if p.lower().endswith(".xlsx") or p.lower().endswith(".xls"):
                raw_df = pd.read_excel(p, engine="openpyxl")
            else:
                raw_df = pd.read_csv(p, encoding="ISO-8859-1")
            raw_source = p
            break
        except Exception as e:
            # try alternative encoding for csv
            try:
                raw_df = pd.read_csv(p, encoding="utf-8", errors="replace")
                raw_source = p
                break
            except:
                continue

if raw_df is None:
    raise FileNotFoundError("Couldn't locate or load any raw data file from expected paths.")

# Load Skips and rules and sample report template
skips_path = "/mnt/data/Skips.csv"
rules_path = "/mnt/data/validation_rules_new.xlsx"
template_path = "/mnt/data/validation_report (21).xlsx"

skips_df = pd.read_csv(skips_path, encoding="ISO-8859-1") if os.path.exists(skips_path) else pd.DataFrame()
rules_df = pd.read_excel(rules_path, sheet_name=None) if os.path.exists(rules_path) else {}
template_exists = os.path.exists(template_path)

# --- Identify respondent ID column ---
possible_id_names = ["RESPID","RespondentID","Respondent Id","CASEID","CaseID","caseid","id","ID","case id","Case ID","resp_id"]
id_col = None
for name in possible_id_names:
    if name in raw_df.columns:
        id_col = name
        break
# fallback: first column if still none
if id_col is None:
    id_col = raw_df.columns[0]

# --- Setup constants ---
DK_CODES = [88, 99]
DK_STRINGS = ["DK", "Refused", "Don't know", "Dont know", "Refuse", "REFUSED"]
numeric_valid_min = 0
numeric_valid_max = 99

# Store validation findings
findings = []

# 1) Basic structural checks
# a) Missing values by variable
for col in raw_df.columns:
    missing_count = raw_df[col].isnull().sum()
    if missing_count > 0:
        findings.append({
            "Variable": col,
            "Check_Type": "Missing Values",
            "Description": f"{missing_count} missing responses",
            "Affected_Count": int(missing_count)
        })

# b) Duplicate respondent IDs
dup_mask = raw_df.duplicated(subset=[id_col], keep=False)
dup_count = dup_mask.sum()
if dup_count > 0:
    dup_ids = raw_df.loc[dup_mask, id_col].unique().tolist()
    findings.append({
        "Variable": id_col,
        "Check_Type": "Duplicate IDs",
        "Description": f"{len(dup_ids)} duplicated respondent IDs found (total duplicate rows: {int(dup_count)})",
        "Affected_Count": int(dup_count)
    })

# c) Out-of-range numeric values and invalid DK codes in numeric columns
for col in raw_df.columns:
    # skip ID column
    if col == id_col:
        continue
    series = raw_df[col]
    # Try to coerce to numeric and check for valid range
    coerced = pd.to_numeric(series, errors="coerce")
    # count non-null numeric entries that are out-of-range (excluding DK codes)
    out_of_range_mask = (~coerced.isna()) & ((coerced < numeric_valid_min) | (coerced > numeric_valid_max)) & (~coerced.isin(DK_CODES))
    out_of_range_count = int(out_of_range_mask.sum())
    if out_of_range_count > 0:
        findings.append({
            "Variable": col,
            "Check_Type": "Out-of-Range Numeric",
            "Description": f"{out_of_range_count} numeric values outside {numeric_valid_min}-{numeric_valid_max} (excluding DK codes)",
            "Affected_Count": out_of_range_count
        })
    # unexpected DK codes/texts in text columns
    if series.dtype == object:
        # check for numeric DK strings
        dk_text_mask = series.astype(str).str.strip().str.lower().isin([s.lower() for s in DK_STRINGS])
        dk_text_count = int(dk_text_mask.sum())
        if dk_text_count > 0:
            findings.append({
                "Variable": col,
                "Check_Type": "DK/Refused Text Found",
                "Description": f"{dk_text_count} values containing DK/Refused text tokens",
                "Affected_Count": dk_text_count
            })

# 2) Skip logic validation (using Skips.csv)
# We'll try to interpret common columns: 'Skip From', 'Logic', 'Skip To', 'Skip If', 'Condition', 'Question', 'Target'
skip_cols = [c.lower() for c in skips_df.columns]
# Normalize column names mapping
col_map = {}
for c in skips_df.columns:
    lc = c.lower()
    if "skip from" in lc or lc=="skip from" or "skipfrom" in lc:
        col_map['from'] = c
    if "logic" in lc:
        col_map['logic'] = c
    if "skip to" in lc or "skipto" in lc:
        col_map['to'] = c
    if "skip if" in lc or "skipif" in lc:
        col_map['skipif'] = c
    if "condition" in lc:
        col_map['condition'] = c
    if "question" in lc:
        col_map['question'] = c
    if "target" in lc:
        col_map['target'] = c

# Heuristic: if there's a 'Logic' column containing expressions like "Segment_16<>1" use it
if 'logic' in col_map:
    for _, r in skips_df.iterrows():
        logic = r[col_map['logic']]
        from_q = r[col_map['from']] if 'from' in col_map else r.get('Question', np.nan)
        to_q = r[col_map['to']] if 'to' in col_map else r.get('Skip To', np.nan)
        # Try to parse simple expressions like Variable=Value or Variable<>Value or Variable==Value
        if isinstance(logic, str) and logic.strip():
            expr = logic.strip()
            # support patterns VAR<>VALUE, VAR=VALUE, VAR==VALUE, VAR!=VALUE
            import re
            m = re.match(r"^\s*([A-Za-z0-9_]+)\s*(<>|!=|==|=)\s*([A-Za-z0-9_']+)\s*$", expr)
            if m:
                var, op, val = m.group(1), m.group(2), m.group(3)
                # strip quotes from val if present
                val = val.strip("'\"")
                # Apply to raw_df if var exists
                if var in raw_df.columns:
                    if op in ("=", "=="):
                        violators = raw_df[(raw_df[var].astype(str) == str(val)) & (~raw_df[from_q].isnull())] if from_q in raw_df.columns else pd.DataFrame()
                    elif op in ("<>","!="):
                        violators = raw_df[(raw_df[var].astype(str) != str(val)) & (~raw_df[from_q].isnull())] if from_q in raw_df.columns else pd.DataFrame()
                    else:
                        violators = pd.DataFrame()
                    if not violators.empty:
                        findings.append({
                            "Variable": from_q,
                            "Check_Type": "Skip Violation",
                            "Description": f"{len(violators)} respondents answered {from_q} contrary to skip logic ({expr})",
                            "Affected_Count": int(len(violators))
                        })
# fallback simpler skipif column usage: SkipIf contains value and 'Question' contains question to be skipped when some Condition var equals SkipIf
if 'skipif' in col_map and 'condition' in col_map and 'question' in col_map:
    for _, r in skips_df.iterrows():
        q = r[col_map['question']]
        cond_var = r[col_map['condition']]
        skip_val = r[col_map['skipif']]
        if q in raw_df.columns and cond_var in raw_df.columns:
            violators = raw_df[(raw_df[cond_var].astype(str) == str(skip_val)) & (~raw_df[q].isnull())]
            if not violators.empty:
                findings.append({
                    "Variable": q,
                    "Check_Type": "Skip Violation (SkipIf)",
                    "Description": f"{len(violators)} respondents answered {q} though skip condition {cond_var}={skip_val} applies",
                    "Affected_Count": int(len(violators))
                })

# 3) Routing mandatory checks: If a target question exists and should be asked, but missing values are present - flag
# We'll interpret 'Skip To' entries and ensure if someone didn't skip they should have answer
if 'to' in col_map and 'from' in col_map:
    for _, r in skips_df.iterrows():
        from_q = r[col_map['from']]
        to_q = r[col_map['to']]
        # If both present in data, find cases where from_q not skipped but to_q is null and should have been answered
        if from_q in raw_df.columns and to_q in raw_df.columns:
            mask_should_have_answered = ~raw_df[from_q].isnull()
            missing_in_to = raw_df[mask_should_have_answered & raw_df[to_q].isnull()]
            if not missing_in_to.empty:
                findings.append({
                    "Variable": to_q,
                    "Check_Type": "Routing Missing",
                    "Description": f"{len(missing_in_to)} respondents answered {from_q} but missing {to_q} (routing issue)",
                    "Affected_Count": int(len(missing_in_to))
                })

# 4) DK/Refused consistency: ensure DK codes 88/99 not used where not allowed
# If rules_df contains a sheet 'NoDK' or similar, use it; else check across all variables for presence of DK codes as numeric
for col in raw_df.columns:
    if col == id_col:
        continue
    series = raw_df[col]
    coerced = pd.to_numeric(series, errors="coerce")
    dk_numeric_count = int(coerced.isin(DK_CODES).sum())
    if dk_numeric_count > 0:
        findings.append({
            "Variable": col,
            "Check_Type": "DK/Refused Numeric Found",
            "Description": f"{dk_numeric_count} numeric DK/Refused codes ({DK_CODES}) present",
            "Affected_Count": dk_numeric_count
        })

# 5) Apply any specific validation rules from validation_rules_new.xlsx if available
# Expecting sheet 'Rules' or tabs with variable/value specs
applied_rules = []
if isinstance(rules_df, dict):
    for sheet, df in rules_df.items():
        # simple format: Variable | RuleType | Params | Note
        if set(['Variable','RuleType']).issubset(df.columns.str.capitalize()):
            df_cols = [c.lower() for c in df.columns]
            var_col = [c for c in df.columns if c.lower()=='variable'][0]
            rtype_col = [c for c in df.columns if c.lower()=='ruletype'][0]
            params_col = None
            if 'params' in df_cols:
                params_col = [c for c in df.columns if c.lower()=='params'][0]
            for _, rr in df.iterrows():
                var = rr[var_col]
                rtype = rr[rtype_col]
                params = rr[params_col] if params_col else None
                applied_rules.append({"Variable": var, "RuleType": rtype, "Params": params})
                # Example: RuleType could be 'Mandatory' or 'Range' or 'AllowedValues'
                if rtype and isinstance(rtype, str):
                    rt = rtype.strip().lower()
                    if rt == 'mandatory' and var in raw_df.columns:
                        missing = raw_df[var].isnull().sum()
                        if missing>0:
                            findings.append({
                                "Variable": var,
                                "Check_Type": "Mandatory Missing",
                                "Description": f"Rule: Mandatory - {missing} missing responses",
                                "Affected_Count": int(missing)
                            })
                    if rt == 'range' and var in raw_df.columns and params:
                        # expect params like "0-5"
                        try:
                            lo, hi = map(float, str(params).split('-'))
                            coerced = pd.to_numeric(raw_df[var], errors="coerce")
                            invalid = coerced[(~coerced.isna()) & ((coerced < lo) | (coerced > hi))]
                            if len(invalid)>0:
                                findings.append({
                                    "Variable": var,
                                    "Check_Type": "Range Violation (Rule)",
                                    "Description": f"{len(invalid)} values outside specified range {params}",
                                    "Affected_Count": int(len(invalid))
                                })
                        except:
                            pass

# --- Build DataFrames for report ---
if findings:
    detailed_df = pd.DataFrame(findings)
else:
    detailed_df = pd.DataFrame(columns=["Variable","Check_Type","Description","Affected_Count"])

summary_df = detailed_df.groupby("Check_Type", as_index=False)["Affected_Count"].sum().sort_values(by="Affected_Count", ascending=False)

# Also include a quick project info sheet
project_info = {
    "Report Generated": [datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")],
    "Raw Data Source": [raw_source],
    "Rows in Raw Data": [int(raw_df.shape[0])],
    "Columns in Raw Data": [int(raw_df.shape[1])],
    "Respondent ID Column": [id_col],
    "DK Codes (numeric)": [str(DK_CODES)],
    "DK Text Tokens": [", ".join(DK_STRINGS)]
}
project_info_df = pd.DataFrame(project_info)

# --- Export to Excel using template-like sheetnames ---
output_file = "/mnt/data/Data_Validation_Report_Final.xlsx"
with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
    # If template exists, copy template's sheet names as guidance (we'll still write our content)
    if template_exists:
        # write summary first
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        detailed_df.to_excel(writer, sheet_name="Detailed Checks", index=False)
        project_info_df.to_excel(writer, sheet_name="Project Info", index=False)
        # Also write applied rules if any
        if applied_rules:
            pd.DataFrame(applied_rules).to_excel(writer, sheet_name="Applied Rules", index=False)
    else:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        detailed_df.to_excel(writer, sheet_name="Detailed Checks", index=False)
        project_info_df.to_excel(writer, sheet_name="Project Info", index=False)
        if applied_rules:
            pd.DataFrame(applied_rules).to_excel(writer, sheet_name="Applied Rules", index=False)

# Display summary to user
tools.display_dataframe_to_user("DV Summary", summary_df)

# Provide download path
output_file

