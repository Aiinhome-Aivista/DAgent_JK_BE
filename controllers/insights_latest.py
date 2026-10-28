import os
import json
import traceback
import pandas as pd
import mysql.connector
# from google import genai
import google.generativeai as genai
from flask import  request, jsonify
from helper.global_helper import clean_gemini_response
from database.config import ACTIVE_LLM, MODEL_NAME, UPLOAD_FOLDER, MYSQL_CONFIG
from model.llm_client import call_llm

# ------------------- Gemini Setup -------------------
# Configure Gemini (only once)
# genai.configure(api_key=GEMINI_API_KEY)
# model = genai.GenerativeModel(MODEL_NAME)


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

            #  Load file into DataFrame
            if f.filename.endswith(".csv"):
                dfs[f.filename] = pd.read_csv(file_path)
            elif f.filename.endswith(".xlsx"):
                dfs[f.filename] = pd.read_excel(file_path)
            else:
                return jsonify({"error": f"Unsupported file format: {f.filename}"}), 400

            os.remove(file_path)  # Clean up temp file

        #  Combine all tables into text
        tables_text = ""
        for fname, df in dfs.items():
            tables_text += f"\n### File: {fname}\n{df.head(20).to_string(index=False)}\n"

        #  Send to Gemini
        prompt = f"""
You are a professional data analyst.

I will provide you with tables from one or more CSV/Excel files.
The tables may have arbitrary columns and data.

TASK:
1. Analyze all the tables and detect important patterns or trends.
2. Summarize any notable insights about:
   - Key entities (e.g., customers, products, locations) if present.
   - Quantities, counts, or other numerical patterns.
   - Dates, sequences, or peaks if any are available.
3. Return all insights as a JSON object with a single key "insights", which is a list of clear, grammatically correct sentences.

Example format:
{{
  "insights": [
    "The majority of orders are concentrated in region X.",
    "Customer ABC placed the highest number of transactions.",
    "Product Y is the most frequently sold item."
  ]
}}

Only return valid JSON. Do not include code fences or explanations.

Tables:
{tables_text}
"""


        # response = model.generate_content(prompt)
        # insights = clean_gemini_response(response.text)
        print(f"Using LLM mode: {ACTIVE_LLM}")

        response_text = call_llm(prompt)
        insights = clean_gemini_response(response_text)


        # Store insights in MySQL (graph table)
        try:
            conn = mysql.connector.connect(**MYSQL_CONFIG)
            cursor = conn.cursor()
            sql = "UPDATE graph SET insights = %s WHERE session_name = %s"
            cursor.execute(sql, (json.dumps(insights), session_name))
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
