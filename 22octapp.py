import streamlit as st
import pandas as pd
import pyreadstat
import io
import csv
import re

st.set_page_config(page_title="Auto Validation Rules + Failed Checks", layout="wide")
st.title("📊 Auto Validation Rules + Failed Checks Generator")

# --- File Uploads ---
raw_file = st.file_uploader("Upload raw survey data (CSV / XLSX / SAV)", type=["csv", "xlsx", "sav"])
skip_file = st.file_uploader("Upload skip rules (CSV or XLSX) — optional", type=["csv", "xlsx"])

# --- Helper: Auto-detect delimiter ---
def detect_delimiter(sample_bytes):
    try:
        sample = sample_bytes.decode('utf-8', errors='ignore')
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample)
        return dialect.delimiter
    except Exception:
        return ','

# --- Helper: Load Survey Data Safely ---
def load_survey_data(file):
    if file.name.endswith(".csv"):
        try:
            sample = file.read(4096)
            delimiter = detect_delimiter(sample)
            file.seek(0)
            return pd.read_csv(file, encoding="utf-8", sep=delimiter)
        except UnicodeDecodeError:
            file.seek(0)
            return pd.read_csv(file, encoding="ISO-8859-1", sep=delimiter)
    elif file.name.endswith(".xlsx"):
        return pd.read_excel(file)
    elif file.name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(file)
        return df
    else:
        st.error("Unsupported file format.")
        st.stop()

# --- Helper: Load Skip Rules ---
def load_skips
