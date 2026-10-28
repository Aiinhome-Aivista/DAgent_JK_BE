# controllers/sheet_content_controller.py

import re
import json
import requests
import mysql.connector
from flask import request, jsonify
from database.config import MISTRAL_API_KEY, MISTRAL_MODEL
from model.llm_client import call_llm_chat

MISTRAL_API_URL  = "https://api.mistral.ai/v1/chat/completions"
MAX_ROWS_FOR_LLM = 200



# ═══════════════════════════════════════════════════════════════
# HELPER 1 — Validate table exists in sheet_scans (no strict user check)
# ═══════════════════════════════════════════════════════════════

def _validate_table_exists(table_name: str, get_connection_func) -> bool:
    """
    Returns True if this table_name exists in sheet_scans.
    No strict user_id ownership — just confirms the table was created by /sheet/scan.
    """
    conn = cursor = None
    try:
        conn = get_connection_func()
        if not conn:
            return False
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM sheet_scans
            WHERE table_name = %s
            LIMIT 1
        """, (table_name,))
        return cursor.fetchone() is not None
    except Exception as e:
        print(f"[DB] validate_table_exists error: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


# ═══════════════════════════════════════════════════════════════
# HELPER 2 — Read rows from the dynamic sheet table
# ═══════════════════════════════════════════════════════════════

def _fetch_sheet_rows(table_name: str, get_connection_func) -> tuple:
    # Safety: table_name must only contain safe chars
    if not re.match(r'^sheet_[a-zA-Z0-9_]+$', table_name):
        raise ValueError("Invalid table name.")

    conn = cursor = None
    try:
        conn = get_connection_func()
        if not conn:
            return [], []
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT * FROM `{table_name}` LIMIT %s", (MAX_ROWS_FOR_LLM,))
        rows = cursor.fetchall()
        if not rows:
            return [], []
        columns = [c for c in rows[0].keys() if c != "_row_id"]
        clean_rows = [{k: v for k, v in r.items() if k != "_row_id"} for r in rows]
        return columns, clean_rows
    except Exception as e:
        print(f"[DB] Failed to fetch sheet rows: {e}")
        return [], []
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


# ═══════════════════════════════════════════════════════════════
# HELPER 3 — Format sheet data as readable text for LLM
# ═══════════════════════════════════════════════════════════════

def _build_sheet_context(columns: list, rows: list) -> str:
    if not rows:
        return "The sheet has no data."
    lines = [f"Columns: {', '.join(columns)}\n"]
    for i, row in enumerate(rows, 1):
        parts = " | ".join([f"{k}: {v}" for k, v in row.items() if str(v).strip() != ""])
        lines.append(f"Row {i}: {parts}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# HELPER 4 — Call Mistral
# ═══════════════════════════════════════════════════════════════

def _call_mistral(system_prompt: str, user_prompt: str):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt}
    ]
    try:
        content_str = call_llm_chat(messages, json_mode=True, temperature=0.3)
        return json.loads(content_str)
    except Exception as e:
        print(f"[LLM] Error: {e}")
        return None



# ═══════════════════════════════════════════════════════════════
# HELPER 5 — Safe parse chat_history
# ═══════════════════════════════════════════════════════════════

def _parse_chat_history(raw: list) -> str:
    if not raw or not isinstance(raw, list):
        return ""
    lines = []
    for turn in raw[-6:]:
        if isinstance(turn, dict):
            role    = str(turn.get("role", "user")).capitalize()
            content = str(turn.get("content", ""))
            lines.append(f"{role}: {content}")
    if not lines:
        return ""
    return "Previous conversation:\n" + "\n".join(lines) + "\n\n"


# ═══════════════════════════════════════════════════════════════
# SHARED: validate + fetch helper used by both controllers
# ═══════════════════════════════════════════════════════════════

def _get_validated_sheet_data(user_id, table_name, get_connection_func):
    """
    Returns (error_response, columns, rows).
    If error_response is not None, return it immediately.
    """
    if not user_id:
        return ({"status": "failed", "statusCode": 400,
                 "message": "Field 'user_id' is required."}, 400), None, None

    if not table_name:
        return ({"status": "failed", "statusCode": 400,
                 "message": "Field 'table_name' is required. Get it from /sheet/scan response."}, 400), None, None

    # Only check table exists — not strict user ownership
    if not _validate_table_exists(table_name, get_connection_func):
        return ({"status": "failed", "statusCode": 404,
                 "message": f"Table '{table_name}' not found. Please run /sheet/scan first."}, 404), None, None

    try:
        columns, rows = _fetch_sheet_rows(table_name, get_connection_func)
    except ValueError as e:
        return ({"status": "failed", "statusCode": 400, "message": str(e)}, 400), None, None

    if not rows:
        return ({"status": "no_data", "statusCode": 200,
                 "message": "The sheet table is empty.", "description": None}, 200), None, None

    return None, columns, rows


# ═══════════════════════════════════════════════════════════════
# CONTROLLER 1 — Sheet Describe
#
# POST /sheet/describe
# Body: { "user_id": "1", "table_name": "sheet_1_20260306154417" }
# ═══════════════════════════════════════════════════════════════

def sheet_describe_controller(get_connection_func):
    data       = request.json or {}
    user_id    = (data.get("user_id")    or "").strip()
    table_name = (data.get("table_name") or "").strip()

    err, columns, rows = _get_validated_sheet_data(user_id, table_name, get_connection_func)
    if err:
        err_body, status_code = err
        from flask import jsonify
        return jsonify(err_body), status_code

    from flask import jsonify
    sheet_context = _build_sheet_context(columns, rows)

    system_prompt = """
You are a strict data analyst.
Your ONLY knowledge source is the spreadsheet data provided.
You MUST NOT use any outside knowledge, training data, or assumptions.
Every sentence must be directly based on the provided data rows.
Respond in valid JSON only.
"""

    user_prompt = f"""
Below is the COMPLETE data from the user's Google Sheet:

{sheet_context}

Write a detailed, topic-focused description (minimum 10-14 sentences) that covers:
- What kind of data this sheet contains overall
- Key topics, patterns, trends, and insights found across the rows and columns
- Notable values, ranges, categories, or outliers present in the data
- Relationships between columns if visible from the data
- Any significant or interesting data points

STRICT RULES:
- Every fact or insight must cite its exact source like this: (source: Row N, column "ColumnName")
  Example: "The highest base pay is 167411.2 (source: Row 1, column "BasePay")."
- Group related insights by topic — do NOT describe row by row.
- Do NOT include any questions or suggestions.
- Do NOT use outside knowledge — only what is in the data above.

Return ONLY this JSON:
{{
  "description": "Your detailed topic-focused description with (source: Row N, column ColumnName) citations..."
}}
"""

    result = _call_mistral(system_prompt, user_prompt)

    if not result:
        return jsonify({"status": "error", "statusCode": 500,
                        "message": "LLM failed to generate description. Please try again."}), 500

    return jsonify({
        "status":      "success",
        "statusCode":  200,
        "user_id":     user_id,
        "table_name":  table_name,
        "columns":     columns,
        "row_count":   len(rows),
        "description": result.get("description", "")
    }), 200


# ═══════════════════════════════════════════════════════════════
# CONTROLLER 2 — Sheet Chat
#
# POST /sheet/chat
#
# CASE A — No question:
#   Body: { "user_id": "1", "table_name": "sheet_1_20260306154417" }
#   Response: { "mode": "suggest", "suggested_questions": [...] }
#
# CASE B — Question provided:
#   Body: { "user_id": "1", "table_name": "...", "question": "...",
#           "chat_history": [...] }
#   Response: { "mode": "answer", "answer": "...", "follow_up_questions": [...] }
# ═══════════════════════════════════════════════════════════════

def sheet_chat_controller(get_connection_func):
    from flask import jsonify

    data         = request.json or {}
    user_id      = (data.get("user_id")    or "").strip()
    table_name   = (data.get("table_name") or "").strip()
    question     = (data.get("question")   or "").strip()
    chat_history = data.get("chat_history", [])

    err, columns, rows = _get_validated_sheet_data(user_id, table_name, get_connection_func)
    if err:
        err_body, status_code = err
        return jsonify(err_body), status_code

    sheet_context = _build_sheet_context(columns, rows)

    # ─────────────────────────────────────────────
    # CASE A: No question → suggest 3 starter questions
    # ─────────────────────────────────────────────
    if not question:
        system_prompt = """
You are a strict data analyst.
Your ONLY knowledge is the spreadsheet data provided.
You MUST NOT use outside knowledge or assumptions.
Respond in valid JSON only.
"""
        user_prompt = f"""
Below is the COMPLETE spreadsheet data:

{sheet_context}

Based STRICTLY on the data above, generate exactly 3 questions that:
- Are specific to the actual column names and values present in the data
- Can be fully and accurately answered from the data rows above
- Cover different aspects or columns of the sheet
- Are meaningful and analytical (comparisons, totals, patterns, outliers, etc.)
- Use natural, human-readable language. DO NOT mention internal system names, folder names, or long raw database table names.

Return ONLY this JSON:
{{
  "suggested_questions": [
    "Specific analytical question 1?",
    "Specific analytical question 2?",
    "Specific analytical question 3?"
  ]
}}
"""
        result = _call_mistral(system_prompt, user_prompt)
        if not result:
            return jsonify({"status": "error", "statusCode": 500,
                            "message": "LLM failed to generate suggestions."}), 500

        return jsonify({
            "status":              "success",
            "statusCode":          200,
            "mode":                "suggest",
            "user_id":             user_id,
            "table_name":          table_name,
            "suggested_questions": result.get("suggested_questions", [])
        }), 200

    # ─────────────────────────────────────────────
    # CASE B: Question provided → answer + 3 follow-ups
    # ─────────────────────────────────────────────

    history_text = _parse_chat_history(chat_history)

    system_prompt = """
You are a strict data analyst. Your ONLY knowledge source is the spreadsheet data provided.

RULES YOU MUST NEVER BREAK:
1. Answer ONLY using data present in the provided rows and columns.
2. If the answer is not in the data, say exactly:
   "I could not find this information in your sheet data."
3. NEVER use training data, general knowledge, or outside information.
4. Cite the exact source of every fact: (source: Row N, column "ColumnName")
5. Suggest exactly 3 follow-up questions answerable from the same data.
6. Respond in valid JSON only.
"""

    user_prompt = f"""
Below is the COMPLETE spreadsheet data (your ONLY source):

{sheet_context}

{history_text}User's question: "{question}"

Answer in detail using ONLY the data above.
After every specific fact or value, add the citation: (source: Row N, column "ColumnName")

Then suggest exactly 3 follow-up questions that:
- Relate directly to the user's question AND the actual sheet columns/data
- Can be fully answered from the data above
- Are specific and analytical
- Use natural, human-readable language. DO NOT mention internal system names, folder names, or long raw database table names.

Return ONLY this JSON:
{{
  "answer": "Detailed answer with (source: Row N, column ColumnName) after each fact...",
  "follow_up_questions": [
    "Follow-up question 1?",
    "Follow-up question 2?",
    "Follow-up question 3?"
  ]
}}

If the question cannot be answered from the data:
- Set "answer" to: "I could not find this information in your sheet data."
- Still provide 3 follow-up questions answerable from the data.
"""

    result = _call_mistral(system_prompt, user_prompt)
    if not result:
        return jsonify({"status": "error", "statusCode": 500,
                        "message": "LLM failed to generate an answer."}), 500

    return jsonify({
        "status":              "success",
        "statusCode":          200,
        "mode":                "answer",
        "user_id":             user_id,
        "table_name":          table_name,
        "question":            question,
        "answer":              result.get("answer", ""),
        "follow_up_questions": result.get("follow_up_questions", [])
    }), 200