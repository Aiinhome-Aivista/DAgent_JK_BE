import os
import io
import json
import traceback
import concurrent.futures
from sqlalchemy import text
from functools import lru_cache
from flask import request, jsonify
from concurrent.futures import ThreadPoolExecutor

# --- New Imports for Unstructured Data ---
from docx import Document
from pypdf import PdfReader
from database.config import MYSQL_CONFIG, UPLOAD_FOLDER, engine
from helper.global_helper import (
    compute_technical_metadata,
    make_compare_prompt,
    make_context_prompt,
    name_similarity,
    read_file_to_df,
    safe_json_loads,
)
from model.llm_client import call_llm

# -------------------------
# Cached LLM Calls
# -------------------------
@lru_cache(maxsize=1000)
def cached_llm_call(prompt):
    return call_llm(prompt)

# -------------------------
# New Helper: Extract Text from Unstructured Files
# -------------------------
def extract_text_content(file_path, filename):
    """Extracts text from PDF or DOCX files."""
    ext = os.path.splitext(filename)[1].lower()
    text_content = ""
    try:
        if ext == '.pdf':
            reader = PdfReader(file_path)
            for page in reader.pages:
                text_content += page.extract_text() + "\n"
        elif ext in ['.docx', '.doc']:
            doc = Document(file_path)
            for para in doc.paragraphs:
                text_content += para.text + "\n"
        elif ext == '.txt':
            with open(file_path, 'r', encoding='utf-8') as f:
                text_content = f.read()
        return text_content.strip()
    except Exception as e:
        print(f"Error extracting text from {filename}: {e}")
        return None

# -------------------------
# New Helper: Unstructured Analysis Prompt
# -------------------------
def analyze_unstructured_document(filename, text_content):
    """Generates a prompt for summarizing and extracting entities from text."""
    # Truncate text if too long to avoid token limits (adjust as needed)
    truncated_text = text_content[:15000] 
    
    prompt = f"""
    Analyze the following document content from the file "{filename}".
    
    Return a valid JSON object with the following keys:
    1."summary":
        - A detailed multi-sentence summary (6–10 sentences).
        - Cover purpose, context, important findings, critical events, and conclusions.
        - Make it human-readable and highly informative.
    2."entities": [
      {{ "name": "string", "type": "Person|Organization|Location|Other" }}],
    3. "key_topics": A list of the main topics or themes.
    4. "sentiment": The overall sentiment (Positive, Neutral, Negative).
    5. "document_type": Classify the document (e.g., Invoice, Contract, Resume, Report).
    
    Text Content:
    {truncated_text}
    """
    
    raw_response = cached_llm_call(prompt)
    return safe_json_loads(raw_response)

# -------------------------
# Updated Controller
# -------------------------
def analyze_controller():
    try:
        session_name = request.form.get("session_name")
        if not session_name:
            return jsonify({"error": "Missing session_name"}), 400

        if "files" not in request.files:
            return jsonify({"error": "No files uploaded"}), 400

        files = request.files.getlist("files")
        
        # Initialize Result Structures
        results = {
            "files": {},               # Structured Metadata
            "unstructured_analysis": {}, # Unstructured Insights
            "relationships": {},       # SQL Joins (Structured Only)
            "token_counts": {}
        }
        
        dfs = {} # To hold structured dataframes
        unstructured_texts = {} # To hold raw text from PDFs/Docs

        total_input_tokens = 0
        total_output_tokens = 0

        # -------------------------
        # Step 1: Save, Classify & Read Files
        # -------------------------
        for f in files:
            safe_filename = f"{session_name}_{f.filename}"
            file_path = os.path.join(UPLOAD_FOLDER, safe_filename)
            f.save(file_path)
            
            ext = os.path.splitext(f.filename)[1].lower()

            # --- PATH A: STRUCTURED DATA ---
            if ext in ['.csv', '.xlsx', '.xls']:
                df = read_file_to_df(file_path)
                if df is None:
                    results["files"][f.filename] = {"error": "unsupported file type or empty"}
                    continue

                dfs[f.filename] = df
                results["files"][f.filename] = {"metadata": {}, "comparison": {}}

                # Approx token count for CSV
                text_data = " ".join(df.astype(str).fillna("").values.flatten())
                word_count = len(text_data.split())
                total_input_tokens += word_count
                results["files"][f.filename]["word_count"] = word_count

            # --- PATH B: UNSTRUCTURED DATA ---
            elif ext in ['.pdf', '.docx', '.doc', '.txt']:
                raw_text = extract_text_content(file_path, f.filename)
                if not raw_text:
                    results["unstructured_analysis"][f.filename] = {"error": "Could not extract text"}
                    continue
                
                unstructured_texts[f.filename] = raw_text
                
                # Approx token count for Text
                word_count = len(raw_text.split())
                total_input_tokens += word_count
                # Placeholder for results
                results["unstructured_analysis"][f.filename] = {}

        # -------------------------
        # Step 2: Parallel Processing (Columns & Documents)
        # -------------------------
        
        # Define Worker for Structured Columns
        def process_column(fname, col, df):
            nonlocal total_output_tokens
            try:
                tmeta = compute_technical_metadata(df).get(col, {})
                samples = tmeta.get("sample_values", [])

                ctx_raw = cached_llm_call(make_context_prompt(col, samples, tmeta, fname))
                cmp_raw = cached_llm_call(
                    make_compare_prompt(col, tmeta, safe_json_loads(ctx_raw), fname)
                )

                total_output_tokens += len(str(ctx_raw).split()) + len(str(cmp_raw).split())

                ctx_json = safe_json_loads(ctx_raw)
                cmp_json = safe_json_loads(cmp_raw)

                return "structured", fname, col, {
                    "metadata": {
                        "column_name": col,
                        "technical_metadata": tmeta,
                        "contextual_metadata": ctx_json,
                        "differences": cmp_json.get("differences", []),
                        "final_recommendation": cmp_json.get("final_recommendation", {}),
                        "which_is_more_accurate": cmp_json.get("which_is_more_accurate", {}),
                    },
                    "comparison": {
                        "column_name": col,
                        "confidence": ctx_json.get("confidence"),
                        "contextual_summary": cmp_json.get("contextual_summary", ""),
                        "technical_summary": cmp_json.get("technical_summary", ""),
                        "user_input": cmp_json.get("user_input", ""),
                        "differences": cmp_json.get("differences", []),
                        "final_recommendation": cmp_json.get("final_recommendation", {}),
                        "which_is_more_accurate": cmp_json.get("which_is_more_accurate", {}),
                    },
                }
            except Exception as e:
                return "structured", fname, col, {"error": str(e)}

        # Define Worker for Unstructured Docs
        def process_doc(fname, text_content):
            nonlocal total_output_tokens
            try:
                analysis_json = analyze_unstructured_document(fname, text_content)
                total_output_tokens += len(str(analysis_json).split())
                return "unstructured", fname, None, analysis_json
            except Exception as e:
                return "unstructured", fname, None, {"error": str(e)}

        futures = []
        max_threads = min(8, os.cpu_count() or 4)
        
        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            # 2a. Submit Structured Tasks
            for fname, df in dfs.items():
                for col in df.columns:
                    futures.append(executor.submit(process_column, fname, col, df))
            
            # 2b. Submit Unstructured Tasks
            for fname, txt in unstructured_texts.items():
                futures.append(executor.submit(process_doc, fname, txt))

            # 2c. Collect Results
            for future in concurrent.futures.as_completed(futures):
                type_tag, fname, col, result = future.result()
                
                if type_tag == "structured":
                    if "error" not in result:
                        results["files"][fname]["metadata"][col] = result["metadata"]
                        results["files"][fname]["comparison"][col] = result["comparison"]
                    else:
                        results["files"][fname]["metadata"][col] = {"error": result["error"]}
                
                elif type_tag == "unstructured":
                    results["unstructured_analysis"][fname] = result

        # -------------------------
        # Step 3: Detect column relationships (Structured Only)
        # -------------------------
        candidates = []
        file_list = list(dfs.items()) # Only iterate through structured DataFrames

        for i in range(len(file_list)):
            fn1, df1 = file_list[i]
            for j in range(i + 1, len(file_list)):
                fn2, df2 = file_list[j]

                for c1 in df1.columns:
                    for c2 in df2.columns:
                        sim = name_similarity(c1, c2)
                        if sim > 0.75:  # name match threshold
                            try:
                                left_values = set(df1[c1].dropna().astype(str))
                                right_values = set(df2[c2].dropna().astype(str))
                                intersection = left_values & right_values
                                
                                left_total = len(left_values)
                                right_total = len(right_values)
                                inter_count = len(intersection)
                                left_ratio = inter_count / max(1, left_total)
                                right_ratio = inter_count / max(1, right_total)

                                # --- Dynamic Join Type Detection ---
                                if inter_count == 0: join_type = "CROSS JOIN"
                                elif left_ratio > 0.8 and right_ratio > 0.8: join_type = "INNER JOIN"
                                elif left_ratio > 0.6 and right_ratio < 0.4: join_type = "LEFT JOIN"
                                elif right_ratio > 0.6 and left_ratio < 0.4: join_type = "RIGHT JOIN"
                                elif 0.4 < left_ratio < 0.8 and 0.4 < right_ratio < 0.8: join_type = "FULL OUTER JOIN"
                                else: join_type = "INNER JOIN" # Default fallback

                                # Self Join Check
                                if fn1 == fn2 and c1 != c2 and sim > 0.85:
                                    join_type = "SELF JOIN"

                                candidates.append({
                                    "file_a": fn1, "col_a": c1,
                                    "file_b": fn2, "col_b": c2,
                                    "name_similarity": round(sim, 3),
                                    "join_type": join_type,
                                    "left_overlap_ratio": round(left_ratio, 3),
                                    "right_overlap_ratio": round(right_ratio, 3),
                                    "common_values": list(intersection)[:5],
                                })

                            except Exception as join_err:
                                candidates.append({
                                    "file_a": fn1, "col_a": c1, "file_b": fn2, "col_b": c2,
                                    "error": str(join_err)
                                })

        results["relationships"]["candidates"] = candidates

        # -------------------------
        # Step 4: Token statistics
        # -------------------------
        results["token_counts"] = {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
        }

        # -------------------------
        # Step 5: Save results in DB
        # -------------------------
        json_response = json.dumps(results)
        with engine.begin() as conn:
            # Check if session exists
            existing = conn.execute(
                text("SELECT id FROM `analyze` WHERE session_name = :sn"),
                {"sn": session_name},
            ).fetchone()

            sql_params = {
                "sn": session_name, 
                "resp": json_response, 
                "in_tok": total_input_tokens, 
                "out_tok": total_output_tokens
            }

            if existing:
                conn.execute(
                    text("""
                        UPDATE `analyze`
                        SET response = :resp, status = 'updated', processing_status = 'Completed', 
                            error_message = NULL, input_token = :in_tok, output_token = :out_tok
                        WHERE session_name = :sn
                    """), sql_params
                )
            else:
                conn.execute(
                    text("""
                        INSERT INTO `analyze` (session_name, response, status, error_message, processing_status, input_token, output_token)
                        VALUES (:sn, :resp, 'inserted', NULL, 'Completed', :in_tok, :out_tok)
                    """), sql_params
                )

        return jsonify({
            "message": "Files analyzed successfully.",
            "structured_files": list(dfs.keys()),
            "unstructured_files": list(unstructured_texts.keys()),
            "results": results,
        })

    except Exception as e:
        # Error Handling & DB Log
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO `analyze` (session_name, response, status, error_message, processing_status)
                        VALUES (:sn, NULL, 'failed', :err, 'Failed')
                        ON DUPLICATE KEY UPDATE
                            status='failed', error_message=:err, processing_status='Failed'
                    """),
                    {"sn": session_name or "unknown", "err": str(e)},
                )
        except Exception as db_err:
            print("Database error while saving error:", db_err)

        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
