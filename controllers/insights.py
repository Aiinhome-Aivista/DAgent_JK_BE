import os
import json
import traceback
import pandas as pd
import pypdf
import mysql.connector
import google.generativeai as genai
from flask import request, jsonify
from helper.global_helper import clean_gemini_response
from database.config import ACTIVE_LLM, MODEL_NAME, UPLOAD_FOLDER, MYSQL_CONFIG
from model.llm_client import call_llm

def insights_controller():
    try:
        #  Check session name
        session_name = request.form.get("session_name")
        if not session_name:
            return jsonify({"error": "Session name missing"}), 400

        #  Check uploaded files
        if "files" not in request.files:
            return jsonify({"error": "No files uploaded"}), 400

        uploaded_files = request.files.getlist("files")
        dfs = {}

        for f in uploaded_files:
            file_path = os.path.join(UPLOAD_FOLDER, f.filename)
            f.save(file_path) 
            
            file_extension = f.filename.split('.')[-1].lower()

            #  Load file into DataFrame
            if file_extension == "csv":
                dfs[f.filename] = pd.read_csv(file_path)
            elif file_extension in ["xlsx", "xls"]:
                dfs[f.filename] = pd.read_excel(file_path)
            
            # --- PDF SUPPORT LOGIC ---
            elif file_extension == "pdf":
                try:
                    with open(file_path, 'rb') as pdf_file:
                        reader = pypdf.PdfReader(pdf_file)
                        full_text = []
                        for page in reader.pages:
                            full_text.append(page.extract_text() or "") 
                    
                    document_text = "\n".join(full_text)
                    
                    if document_text.strip():
                        dfs[f.filename] = pd.DataFrame([{"document_text": document_text}])
                    else:
                        print(f"Warning: PDF {f.filename} contains no readable text.")
                        os.remove(file_path)
                        continue

                except Exception as pdf_e:
                    os.remove(file_path)
                    return jsonify({"error": f"Failed to process PDF {f.filename}: {str(pdf_e)}"}), 400
            
            else:
                os.remove(file_path)
                return jsonify({"error": f"Unsupported file format: {f.filename}. Only CSV/Excel/PDF supported."}), 400

            os.remove(file_path)  # Clean up temp file

        #  Combine all data into text for the LLM
        tables_text = ""
        for fname, df in dfs.items():
            if 'document_text' in df.columns and len(df.columns) == 1:
                 # === FIX: TRUNCATE TEXT TO AVOID 400 ERROR ===
                 # Limit to 10,000 characters (approx 2.5k tokens) to be safe
                 raw_text = df['document_text'].iloc[0]
                 truncated_text = raw_text[:10000] 
                 
                 tables_text += f"\n### Document: {fname}\nTEXT CONTENT (Truncated to first 10k chars):\n{truncated_text}\n"
            else:
                 # For CSV/Excel, use the tabular head
                 tables_text += f"\n### Table: {fname}\n{df.head(20).to_string(index=False)}\n"


        #  Send to LLM
        prompt = f"""
You are a professional data analyst.

I will provide you with **tables (CSV/Excel samples)** and **documents (PDF text summaries)**.
Analyze all the data sources to find meaningful connections, patterns, and insights.

TASK:
1. Analyze all the data sources (tables and documents) and detect important patterns or trends.
2. Summarize any notable insights about:
   - Key entities (e.g., customers, products, locations) if present in tables.
   - Quantities, counts, or other numerical patterns from tables.
   - Dates, sequences, or peaks if any are available.
   - **Main topics, context, or key findings extracted from the text documents.**
3. Return all insights as a JSON object with a single key "insights", which is a list of clear, grammatically correct sentences.

Example format:
{{
  "insights": [
    "The majority of orders are concentrated in region X, according to the Sales table.",
    "The attached policy document highlights mandatory annual compliance training.",
    "Product Y is the most frequently sold item in Q3."
  ]
}}

Only return valid JSON. Do not include code fences or explanations.

Data Sources:
{tables_text}
"""

        print(f" Using LLM mode: {ACTIVE_LLM}")

        response_text = call_llm(prompt)
        insights = clean_gemini_response(response_text)


        # Store insights in MySQL (graph table)
        try:
            conn = mysql.connector.connect(**MYSQL_CONFIG)
            cursor = conn.cursor()
            sql = "UPDATE graph SET insights = %s WHERE session_name = %s"
            # Ensure insights is a valid dict before dumping
            if isinstance(insights, str):
                 # Fallback if clean_gemini_response returned raw string
                 final_json = json.dumps({"insights": [insights]})
            else:
                 final_json = json.dumps(insights)
                 
            cursor.execute(sql, (final_json, session_name))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"MySQL update failed: {e}")

        # Return JSON response
        return jsonify(insights)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500