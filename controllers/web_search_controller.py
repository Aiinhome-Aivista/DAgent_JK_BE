# controllers/web_search_controller.py

import uuid
import requests
import json
import mysql.connector
from datetime import datetime
from flask import request, jsonify
from database.config import MYSQL_CONFIG, MISTRAL_API_KEY, MISTRAL_MODEL
from model.llm_client import call_llm_chat

# ─────────────────────────────────────────────

# STEP 1 — LLM-Powered Web Search Controller
# Endpoint: POST /search
# Body: { "topic": "machine learning", "user_id": "abc123" (optional) }
# ─────────────────────────────────────────────

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
BRAVE_SEARCH_API_URL = "https://api.search.brave.com/res/v1/web/search"

# ── Optional: set your Brave Search API key in config.py or .env ──
# If you don't have Brave Search, the controller falls back to LLM-only results.
import os
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")  # Set this in your .env


def _call_brave_search(topic: str, count: int = 8) -> list[dict]:
    """
    Calls Brave Search API and returns a list of {title, url, description} dicts.
    Falls back to empty list if key is missing or request fails.
    """
    if not BRAVE_API_KEY:
        return []

    try:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": BRAVE_API_KEY
        }
        params = {"q": topic, "count": count, "text_decorations": False}
        resp = requests.get(BRAVE_SEARCH_API_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title":       item.get("title", ""),
                "url":         item.get("url", ""),
                "description": item.get("description", "")
            })
        return results

    except Exception as e:
        print(f"[BraveSearch] Error: {e}")
        return []


def _call_llm_for_web_results(topic: str, raw_results: list[dict]) -> list[dict]:
    """
    Sends topic + raw search snippets to Mistral.
    Mistral returns a cleaned JSON list with title, url, and a short brief per site.
    If raw_results is empty, Mistral generates results from its own knowledge.
    """
    if raw_results:
        # Give the LLM real search data to summarise
        formatted = "\n".join(
            f"{i+1}. Title: {r['title']}\n   URL: {r['url']}\n   Snippet: {r['description']}"
            for i, r in enumerate(raw_results)
        )
        user_prompt = f"""
The user searched for the topic: "{topic}"

Here are raw search results fetched from the web:
{formatted}

Your task:
- Return a JSON array of objects, one per result.
- Each object must have exactly these keys:
    "title"  : the page title (clean it up if needed)
    "url"    : the full URL
    "brief"  : a 2-3 sentence plain-English summary of what the page is about and why it is useful for the topic

Output ONLY the JSON array, no extra text.
"""
    else:
        # Fallback: LLM generates results from knowledge
        user_prompt = f"""
The user wants to learn about the topic: "{topic}"

Since no live search results are available, use your knowledge to recommend 6-8 highly relevant,
real websites (with real URLs) that would be genuinely useful for this topic.

Return a JSON array of objects with exactly these keys:
    "title"  : a descriptive page/site title
    "url"    : the full, real URL  
    "brief"  : a 2-3 sentence explanation of what the site covers and why it is helpful

Output ONLY the JSON array, no extra text.
"""

    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful research assistant. "
                "You always respond with strictly valid JSON arrays only — no markdown, no explanation."
            )
        },
        {"role": "user", "content": user_prompt}
    ]

    try:
        content_str = call_llm_chat(messages, json_mode=True, temperature=0.3)

        # The model may wrap the array in {"results": [...]} — handle both
        parsed = json.loads(content_str)
        if isinstance(parsed, list):
            return parsed
        # Try common wrapper keys
        for key in ("results", "websites", "links", "data"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        # Last resort: return whatever values are lists
        for v in parsed.values():
            if isinstance(v, list):
                return v
        return []

    except Exception as e:
        print(f"[LLM] Error generating web results: {e}")
        return []



def web_search_controller(get_connection_func):
    """
    POST /search
    Body JSON:
        topic    (str, required) — the topic to search
        user_id  (str, optional) — for logging / linking to saved results
    
    Returns:
        {
          "status": "success",
          "search_id": "<uuid>",
          "topic": "...",
          "results": [
            { "title": "...", "url": "...", "brief": "..." },
            ...
          ]
        }
    """
    data = request.json or {}
    topic   = (data.get("topic") or "").strip()
    user_id = (data.get("user_id") or "").strip() or None

    # ── Validation ──
    if not topic:
        return jsonify({
            "status": "failed",
            "statusCode": 400,
            "message": "Field 'topic' is required."
        }), 400

    if len(topic) > 300:
        return jsonify({
            "status": "failed",
            "statusCode": 400,
            "message": "Topic must be 300 characters or fewer."
        }), 400

    search_id = str(uuid.uuid4())

    # ── Step 1: Web Search (Brave) ──
    raw_results = _call_brave_search(topic, count=8)

    # ── Step 2: LLM Enhancement / Fallback ──
    enriched_results = _call_llm_for_web_results(topic, raw_results)

    if not enriched_results:
        return jsonify({
            "status": "failed",
            "statusCode": 500,
            "message": "LLM failed to generate results. Please try again."
        }), 500

    # ── Step 3: Persist search record in MySQL ──
    conn = cursor = None
    try:
        conn = get_connection_func()
        if conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO web_searches (search_id, user_id, topic, result_count, created_at)
                VALUES (%s, %s, %s, %s, NOW())
            """, (search_id, user_id, topic, len(enriched_results)))
            conn.commit()
    except Exception as e:
        # Non-fatal — still return results even if DB logging fails
        print(f"[DB] Failed to log search: {e}")
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()

    return jsonify({
        "status":    "success",
        "statusCode": 200,
        "search_id": search_id,
        "topic":     topic,
        "source":    "web+llm" if raw_results else "llm_only",
        "results":   enriched_results
    }), 200
    