# controllers/saved_content_analysis_controller.py

import re
import requests
import json
from flask import request, jsonify
from database.config import MISTRAL_API_KEY, MISTRAL_MODEL
from model.llm_client import call_llm_chat

MISTRAL_API_URL   = "https://api.mistral.ai/v1/chat/completions"
MAX_CHARS_PER_URL = 4000



# ═══════════════════════════════════════════════════════════════
# HELPER 1 — Scrape plain text from a URL
# ═══════════════════════════════════════════════════════════════

def _scrape_url(url: str) -> str:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:MAX_CHARS_PER_URL]
    except Exception as e:
        print(f"[Scraper] Failed to scrape {url}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════
# HELPER 2 — Fetch user's saved results from MySQL
# ═══════════════════════════════════════════════════════════════

def _fetch_saved_results(user_id: str, get_connection_func) -> list:
    conn = cursor = None
    try:
        conn = get_connection_func()
        if not conn:
            return []
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT saved_id, title, url, brief, topic
            FROM saved_web_results
            WHERE user_id = %s
            ORDER BY saved_at DESC
        """, (user_id,))
        return cursor.fetchall()
    except Exception as e:
        print(f"[DB] Failed to fetch saved results: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


# ═══════════════════════════════════════════════════════════════
# HELPER 3 — Call Mistral
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
# HELPER 4 — Build sources context block
# ═══════════════════════════════════════════════════════════════

def _build_sources_context(saved_results: list) -> str:
    parts = []
    for i, row in enumerate(saved_results, 1):
        scraped = _scrape_url(row["url"])
        content = scraped if scraped else row.get("brief", "")
        parts.append(
            f"--- SOURCE {i} ---\n"
            f"Title  : {row['title']}\n"
            f"URL    : {row['url']}\n"
            f"Topic  : {row.get('topic', '')}\n"
            f"Content:\n{content}\n"
        )
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# HELPER 5 — Safe parse chat_history
# Accepts both:
#   [ {"role": "user", "content": "..."} ]   <- correct dict format
#   [ "some string", "another string" ]       <- wrong, skip safely
# ═══════════════════════════════════════════════════════════════

def _parse_chat_history(raw: list) -> str:
    if not raw or not isinstance(raw, list):
        return ""
    lines = []
    for turn in raw[-6:]:  # last 6 turns max
        if isinstance(turn, dict):
            role    = str(turn.get("role", "user")).capitalize()
            content = str(turn.get("content", ""))
            lines.append(f"{role}: {content}")
        # if it's a string or anything else, skip it silently
    if not lines:
        return ""
    return "Previous conversation:\n" + "\n".join(lines) + "\n\n"


# ═══════════════════════════════════════════════════════════════
# CONTROLLER 1 — Detailed description ONLY (no questions)
#
# POST /saved-content/describe
# Body : { "user_id": "abc123" }
#
# Response:
# {
#   "status": "success",
#   "description": "Topic-focused detailed paragraph with (source: URL) refs...",
#   "sources_used": [ {"title": "...", "url": "..."} ]
# }
# ═══════════════════════════════════════════════════════════════

def saved_content_describe_controller(get_connection_func):
    data    = request.json or {}
    user_id = (data.get("user_id") or "").strip()

    if not user_id:
        return jsonify({
            "status": "failed", "statusCode": 400,
            "message": "Field 'user_id' is required."
        }), 400

    saved_results = _fetch_saved_results(user_id, get_connection_func)
    if not saved_results:
        return jsonify({
            "status":       "no_data",
            "statusCode":   200,
            "message":      "No saved results found. Save some web results first.",
            "description":  None,
            "sources_used": []
        }), 200

    sources_context = _build_sources_context(saved_results)

    system_prompt = """
You are a strict research assistant.
You ONLY use the website contents provided — no outside knowledge or training data allowed.
Every sentence must come directly from the provided source content.
Respond in valid JSON only.
"""

    user_prompt = f"""
Below are the ONLY sources you are allowed to use:

{sources_context}

Your task: Write a detailed, topic-focused description (minimum 10-14 sentences).

HOW TO WRITE IT:
- Focus on the TOPICS and CONCEPTS discussed across the sources — not on describing the websites themselves.
- Group related ideas together (e.g., all content about "supervised learning" from any source together).
- For EVERY specific fact, concept, or piece of information you mention, add the source URL
  at the end of that sentence in this exact format: (source: URL)
  Example: "Gradient descent is an optimization algorithm used to minimize loss functions (source: https://example.com)."
- Cover ALL major topics found across ALL sources.
- Do NOT say things like "Source 1 says..." or "This website discusses..." — write as topic paragraphs.
- Do NOT add any questions or suggestions.

Return ONLY this JSON:
{{
  "description": "Your detailed topic-focused description here, with (source: URL) after each fact...",
  "sources_used": [
    {{"title": "...", "url": "..."}}
  ]
}}
"""

    result = _call_mistral(system_prompt, user_prompt)

    if not result:
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    "LLM failed to generate description. Please try again."
        }), 500

    return jsonify({
        "status":        "success",
        "statusCode":    200,
        "user_id":       user_id,
        "total_sources": len(saved_results),
        "description":   result.get("description", ""),
        "sources_used":  result.get("sources_used", [
            {"title": r["title"], "url": r["url"]} for r in saved_results
        ])
    }), 200


# ═══════════════════════════════════════════════════════════════
# CONTROLLER 2 — Chat with saved content
#
# POST /saved-content/chat
#
# CASE A — No question (first open):
#   Body    : { "user_id": "abc123" }
#   Response: { "mode": "suggest", "suggested_questions": ["Q1?","Q2?","Q3?"] }
#
# CASE B — Question provided:
#   Body    : {
#     "user_id": "abc123",
#     "question": "What is X?",
#     "chat_history": [
#       {"role": "user",      "content": "..."},
#       {"role": "assistant", "content": "..."}
#     ]
#   }
#   Response: {
#     "mode": "answer",
#     "answer": "...(source: URL)...",
#     "sources_referenced": [...],
#     "follow_up_questions": ["Q1?","Q2?","Q3?"]
#   }
# ═══════════════════════════════════════════════════════════════

def saved_content_chat_controller(get_connection_func):
    data         = request.json or {}
    user_id      = (data.get("user_id")  or "").strip()
    question     = (data.get("question") or "").strip()
    chat_history = data.get("chat_history", [])

    if not user_id:
        return jsonify({
            "status": "failed", "statusCode": 400,
            "message": "Field 'user_id' is required."
        }), 400

    saved_results = _fetch_saved_results(user_id, get_connection_func)
    if not saved_results:
        return jsonify({
            "status":     "no_data",
            "statusCode": 200,
            "message":    "No saved results found. Save some web results first."
        }), 200

    sources_context = _build_sources_context(saved_results)

    # ─────────────────────────────────────────────
    # CASE A: No question → suggest 3 starter questions
    # ─────────────────────────────────────────────
    if not question:
        system_prompt = """
You are a strict research assistant.
Your ONLY knowledge is the website content provided.
You MUST NOT use outside knowledge or training data.
Respond in valid JSON only.
"""
        user_prompt = f"""
Below are the ONLY sources you are allowed to use:

{sources_context}

Based STRICTLY on the topics and content present in these sources,
generate exactly 3 questions that:
- Are specific to the actual topics discussed in the content
- Can be fully answered from the sources above
- Cover different topics/aspects across the saved content
- Are NOT generic questions like "What is AI?" unless that topic is specifically explained in the sources
- Use natural, human-readable language. DO NOT mention internal system names, folder names, or long raw database table names.

Return ONLY this JSON:
{{
  "suggested_questions": [
    "Specific question 1 based on source content?",
    "Specific question 2 based on source content?",
    "Specific question 3 based on source content?"
  ]
}}
"""
        result = _call_mistral(system_prompt, user_prompt)

        if not result:
            return jsonify({
                "status":     "error",
                "statusCode": 500,
                "message":    "LLM failed to generate suggestions. Please try again."
            }), 500

        return jsonify({
            "status":              "success",
            "statusCode":          200,
            "mode":                "suggest",
            "user_id":             user_id,
            "suggested_questions": result.get("suggested_questions", [])
        }), 200

    # ─────────────────────────────────────────────
    # CASE B: Question provided → answer + 3 follow-ups
    # ─────────────────────────────────────────────

    history_text = _parse_chat_history(chat_history)  # safe parse, no crash

    system_prompt = """
You are a strict research assistant. Your ONLY knowledge source is the website content provided.

RULES YOU MUST NEVER BREAK:
1. Answer ONLY using information present in the provided sources.
2. If the answer is not in the sources, say exactly:
   "I could not find information about this in your saved sources."
3. NEVER use your training data, general knowledge, or any outside information.
4. For EVERY fact or sentence in your answer, cite the exact source URL at the end
   of that sentence in this format: (source: URL)
5. Suggest exactly 3 follow-up questions answerable from the same sources.
6. Respond in valid JSON only.
"""

    user_prompt = f"""
Below are the ONLY sources you are allowed to use:

{sources_context}

{history_text}User's question: "{question}"

Answer in detail using ONLY the source content above.
After every sentence or fact, add (source: URL) referencing which URL that information came from.

Then suggest exactly 3 follow-up questions that:
- Are directly related to the user's question AND the source content
- Can be fully answered from the sources above
- Are specific, not generic
- Use natural, human-readable language. DO NOT mention internal system names, folder names, or long raw database table names.

Return ONLY this JSON:
{{
  "answer": "Detailed answer where every fact ends with (source: URL)...",
  "sources_referenced": [
    {{"title": "...", "url": "..."}}
  ],
  "follow_up_questions": [
    "Follow-up question 1?",
    "Follow-up question 2?",
    "Follow-up question 3?"
  ]
}}

If the question cannot be answered from the sources:
- Set "answer" to: "I could not find information about this in your saved sources."
- Set "sources_referenced" to: []
- Still suggest 3 follow-up questions that ARE answerable from the sources.
"""

    result = _call_mistral(system_prompt, user_prompt)

    if not result:
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    "LLM failed to generate an answer. Please try again."
        }), 500

    return jsonify({
        "status":              "success",
        "statusCode":          200,
        "mode":                "answer",
        "user_id":             user_id,
        "question":            question,
        "answer":              result.get("answer", ""),
        "sources_referenced":  result.get("sources_referenced", []),
        "follow_up_questions": result.get("follow_up_questions", [])
    }), 200