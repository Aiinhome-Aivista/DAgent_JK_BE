import re
import json
import traceback
import pandas as pd
import json, traceback
from datetime import datetime 
from difflib import SequenceMatcher
import google.generativeai as genai
from database.config import MAX_SAMPLE_VALUES, MODEL_NAME




# ---------- Helper Functions ----------
def looks_like_date(val: str) -> bool:
    date_formats = ["%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"]
    for fmt in date_formats:
        try:
            datetime.strptime(val, fmt)
            return True
        except Exception:
            continue
    return False

def map_dtype(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series.dropna()):
        return "INTEGER"
    if pd.api.types.is_float_dtype(series.dropna()):
        return "DECIMAL"
    if pd.api.types.is_bool_dtype(series.dropna()):
        return "BOOLEAN"
    if pd.api.types.is_datetime64_any_dtype(series.dropna()):
        return "DATETIME"

    sample_vals = series.dropna().astype(str).head(50).tolist()
    if all(looks_like_date(v) for v in sample_vals):
        return "DATE"
    if all(v.isdigit() for v in sample_vals):
        return "INTEGER"

    try:
        [float(v) for v in sample_vals]
        return "DECIMAL"
    except Exception:
        pass

    if any(ord(ch) > 127 for v in sample_vals for ch in v):
        return "NVARCHAR"

    return "VARCHAR"

def read_file_to_df(path: str):
    if path.lower().endswith(".csv"):
        return pd.read_csv(path, dtype=str, low_memory=False)
    elif path.lower().endswith((".xls", ".xlsx")):
        return pd.read_excel(path, dtype=str)
    return None

def sample_values(series: pd.Series, n: int = MAX_SAMPLE_VALUES):
    vals = series.dropna().astype(str).tolist()
    return vals[:n]

def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(s[start : end + 1])
            except Exception:
                pass
        return {"raw_text": s}

def call_gemini(prompt: str):
    try:
        # Create the model object
        model = genai.GenerativeModel(MODEL_NAME)
        
        # Generate content
        response = model.generate_content(prompt)
        
        # Return text output
        return getattr(response, "text", str(response))
    except Exception as e:
        return f"__GEMINI_CALL_FAILED__ {str(e)}\n{traceback.format_exc()}"

def compute_technical_metadata(df: pd.DataFrame):
    res = {}
    for col in df.columns:
        series = df[col]
        col_meta = {
            "column_name": col,
            "pandas_dtype": str(series.dtype),
            "inferred_sql_type": map_dtype(series),
            "non_null_count": int(series.count()),
            "null_count": int(series.isnull().sum()),
            "unique_count": int(series.nunique(dropna=True)),
            "sample_values": sample_values(series),
        }
        try:
            numeric = pd.to_numeric(series.dropna(), errors="coerce")
            if numeric.dropna().size > 0:
                col_meta.update(
                    {
                        "min": float(numeric.min()),
                        "max": float(numeric.max()),
                        "mean": float(numeric.mean()),
                        "median": float(numeric.median()),
                    }
                )
        except Exception:
            pass
        res[col] = col_meta
    return res

def make_context_prompt(col, samples, tech_meta, filename):
    return f"""
You are an expert data analyst. Analyze the column '{col}' in file '{filename}'.

Sample values: {json.dumps(samples)}
Technical metadata: {json.dumps(tech_meta)}

Respond ONLY with JSON:
{{
  "column_name": "{col}",
  "inferred_entity": string,
  "inferred_type": string,
  "confidence": number,
  "explanation": string,
  "detected_format_regex": string|null,
  "anomalies": [string],
  "suggested_normalization": [string]
}}
"""

def make_compare_prompt(col, tech_meta, context_meta, filename):
    return f"""
Compare technical vs contextual metadata for column '{col}' in file '{filename}'.

Technical metadata: {json.dumps(tech_meta)}
Contextual metadata: {json.dumps(context_meta)}

Respond ONLY with JSON:
{{
  "column_name": "{col}",
  "technical_summary": string,
  "contextual_summary": string,
  "differences": [string],
  "final_recommendation": {{
      "use": "technical_metadata"|"contextual_metadata",
      "reason": string
  }},
  "which_is_more_accurate": {{
      "selected": "technical_metadata"|"contextual_metadata",
      "confidence": number
  }}
}}
"""

def name_similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def clean_gemini_response(response_text):
    cleaned = re.sub(r"^```(json)?|```$", "", response_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"insights": [cleaned]}

