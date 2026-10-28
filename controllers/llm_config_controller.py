# """
# LLM Configuration Controller
# ─────────────────────────────
# Admin APIs for managing LLM providers and scenario → provider assignments.
# """

# import re
# import mysql.connector
# from flask import request, jsonify


# # ── Helpers ──────────────────────────────────────────────────────────────────

# def _mask_key(key: str) -> str:
#     """Mask API key for display: show first 6 chars, rest as bullets."""
#     if not key:
#         return ""
#     if len(key) <= 6:
#         return "••••••"
#     return key[:6] + "••••••••"


# def _is_masked(key: str) -> bool:
#     """Check if a key is the masked placeholder (shouldn't be saved)."""
#     if not key:
#         return True
#     return "••" in key


# # ── Known scenarios (for seeding / validation) ───────────────────────────────

# KNOWN_SCENARIOS = [
#     "rag_chat", "analysis", "visualization", "intent_routing", "chat",
#     "insights", "query_branch", "web_search", "agent_planner",
#     "knowledge", "sheet_processing",
# ]


# # ── Table init (auto-create on first use) ────────────────────────────────────
# # The _ensure_tables function has been removed. 
# # Tables must be created manually via SQL script.

# # ═══════════════════════════════════════════════════════════════════════════════
# #  Provider CRUD
# # ═══════════════════════════════════════════════════════════════════════════════

# def get_providers_controller(get_conn):
#     """GET /api/llm/providers — List all providers (keys masked)."""
#     company_id = request.args.get('company_id')
#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor(dictionary=True)
#         if company_id:
#             cursor.execute(
#                 "SELECT id, name, provider_type, api_key, model_name, base_url, is_active, "
#                 "created_at, updated_at, company_id FROM llm_providers "
#                 "WHERE company_id = %s OR company_id IS NULL ORDER BY created_at ASC", (company_id,)
#             )
#         else:
#             cursor.execute(
#                 "SELECT id, name, provider_type, api_key, model_name, base_url, is_active, "
#                 "created_at, updated_at, company_id FROM llm_providers ORDER BY created_at ASC"
#             )
#         rows = cursor.fetchall()
#         cursor.close()

#         # Mask API keys
#         for row in rows:
#             row["api_key_masked"] = _mask_key(row.get("api_key") or "")
#             del row["api_key"]
#             # Convert datetime to string for JSON serialization
#             if row.get("created_at"):
#                 row["created_at"] = str(row["created_at"])
#             if row.get("updated_at"):
#                 row["updated_at"] = str(row["updated_at"])

#         return jsonify({"status": True, "providers": rows})
#     except Exception as e:
#         return jsonify({"status": False, "msg": str(e)}), 500
#     finally:
#         conn.close()


# def create_provider_controller(get_conn):
#     """POST /api/llm/providers — Create a new provider."""
#     data = request.get_json(silent=True) or {}
#     name = (data.get("name") or "").strip()
#     provider_type = (data.get("provider_type") or "").strip()
#     model_name = (data.get("model_name") or "").strip()
#     api_key = (data.get("api_key") or "").strip()
#     base_url = (data.get("base_url") or "").strip()
#     is_active = data.get("is_active", True)
#     company_id = data.get("company_id")

#     if not name or not provider_type or not model_name:
#         return jsonify({"status": False, "msg": "name, provider_type, and model_name are required"}), 400

#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor()
#         cursor.execute(
#             "INSERT INTO llm_providers (name, provider_type, api_key, model_name, base_url, is_active, company_id) "
#             "VALUES (%s, %s, %s, %s, %s, %s, %s)",
#             (name, provider_type, api_key if api_key else None, model_name, base_url, is_active, company_id)
#         )
#         conn.commit()
#         new_id = cursor.lastrowid
#         cursor.close()

      
#         return jsonify({"status": True, "msg": "Provider created", "id": new_id}), 201
#     except Exception as e:
#         return jsonify({"status": False, "msg": str(e)}), 500
#     finally:
#         conn.close()


# def update_provider_controller(get_conn, provider_id):
#     """PUT /api/llm/providers/<id> — Update a provider."""
#     data = request.get_json(silent=True) or {}

#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor(dictionary=True)

#         # Fetch existing
#         cursor.execute("SELECT * FROM llm_providers WHERE id = %s", (provider_id,))
#         existing = cursor.fetchone()
#         if not existing:
#             cursor.close()
#             return jsonify({"status": False, "msg": "Provider not found"}), 404

#         # Build update fields
#         name = (data.get("name") or existing["name"]).strip()
#         provider_type = (data.get("provider_type") or existing["provider_type"]).strip()
#         model_name = (data.get("model_name") or existing["model_name"]).strip()
#         base_url = data.get("base_url", existing["base_url"] or "").strip()
#         is_active = data.get("is_active", existing["is_active"])

#         # API key: only update if a new non-masked key is provided
#         new_key = (data.get("api_key") or "").strip()
#         if new_key and not _is_masked(new_key):
#             api_key = new_key
#         else:
#             api_key = existing["api_key"]

#         cursor.execute(
#             "UPDATE llm_providers SET name=%s, provider_type=%s, api_key=%s, "
#             "model_name=%s, base_url=%s, is_active=%s WHERE id=%s",
#             (name, provider_type, api_key, model_name, base_url, is_active, provider_id)
#         )
#         conn.commit()
#         cursor.close()
#         from model.llm_client import clear_llm_cache
#         clear_llm_cache()
      
#         return jsonify({"status": True, "msg": "Provider updated"})
#     except Exception as e:
#         return jsonify({"status": False, "msg": str(e)}), 500
#     finally:
#         conn.close()


# def delete_provider_controller(get_conn, provider_id):
#     """DELETE /api/llm/providers/<id> — Delete a provider (assignments auto-nullify via FK)."""
#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor()
#         cursor.execute("DELETE FROM llm_providers WHERE id = %s", (provider_id,))
#         conn.commit()
#         affected = cursor.rowcount
#         cursor.close()

#         if affected == 0:
#             return jsonify({"status": False, "msg": "Provider not found"}), 404

      
#         return jsonify({"status": True, "msg": "Provider deleted"})
#     except Exception as e:
#         return jsonify({"status": False, "msg": str(e)}), 500
#     finally:
#         conn.close()


# def test_provider_controller(get_conn, provider_id):
#     """POST /api/llm/providers/<id>/test — Quick connectivity test."""
#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor(dictionary=True)
#         cursor.execute("SELECT * FROM llm_providers WHERE id = %s", (provider_id,))
#         provider = cursor.fetchone()
#         cursor.close()

#         if not provider:
#             return jsonify({"status": False, "msg": "Provider not found"})

#         ptype = provider["provider_type"]
#         test_messages = [{"role": "user", "content": "Say 'OK' in one word."}]

#         from model.llm_client import (
#             _call_gemini, _call_mistral_cloud, _call_mistral_local, _call_openai
#         )

#         if ptype == "gemini":
#             result = _call_gemini(test_messages, False, 0.1, provider["api_key"], provider["model_name"], provider.get("base_url"), timeout=60)
#         elif ptype == "mistral_cloud":
#             result = _call_mistral_cloud(test_messages, False, 0.1, provider["api_key"], provider["model_name"], provider.get("base_url"), timeout=60)
#         elif ptype == "mistral_local":
#             result = _call_mistral_local(test_messages, False, 0.1, provider.get("api_key"), provider["model_name"], provider["base_url"], timeout=600)
#         elif ptype == "openai":
#             result = _call_openai(test_messages, False, 0.1, provider["api_key"], provider["model_name"], provider["base_url"], timeout=60)
#         elif ptype.strip().lower() == "openrouter":
#             result = _call_openai(
#                 test_messages,
#                 False,
#                 0.1,
#                 provider["api_key"],
#                 provider["model_name"],
#                 provider.get("base_url"),
#                 timeout=60
#             )
#         else:
#             return jsonify({"status": False, "msg": f"Unknown provider type: {ptype}"})

#         return jsonify({"status": True, "msg": "Connection successful", "response": result[:200]})

#     except Exception as e:
#         err_str = str(e).lower()
#         short_msg = "Test Failed"

#         print(f"[LLM TEST] Provider: {provider_id}")
#         print(f"[LLM TEST] Error: {repr(e)}")
        
#         if "401" in err_str or "unauthorized" in err_str:
#             short_msg = "Invalid API Key"
#         elif "402" in err_str or "payment required" in err_str:
#             short_msg = "Insufficient OpenRouter credits"
#         elif "403" in err_str or "forbidden" in err_str:
#             short_msg = "Access denied"
#         elif "429" in err_str or "quota" in err_str:
#             short_msg = "Rate limit or quota exceeded"
#         elif "404" in err_str or "not found" in err_str:
#             if "model" in err_str:
#                 short_msg = "Invalid Model Name."
#             else:
#                 short_msg = "The provider API URL is incorrect."
#         elif "timeout" in err_str:
#             short_msg = "Server took too long to respond."
#         elif "connection refused" in err_str or "failed to establish" in err_str:
#             short_msg = "The server is offline or unreachable."
        
#         return jsonify({"status": False, "msg": short_msg})
#     finally:
#         conn.close()


# # ═══════════════════════════════════════════════════════════════════════════════
# #  Scenario Assignments
# # ═══════════════════════════════════════════════════════════════════════════════

# def get_assignments_controller(get_conn):
#     """GET /api/llm/assignments — Get all scenario → provider mappings."""
#     company_id = request.args.get('company_id')
#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor(dictionary=True)
        
#         if company_id == 'all':
#             query = """
#                 SELECT sa.scenario, sa.provider_id, sa.temperature, sa.max_tokens, sa.timeout, sa.company_id, p.name AS provider_name, p.provider_type
#                 FROM llm_scenario_assignments sa
#                 LEFT JOIN llm_providers p ON sa.provider_id = p.id
#                 ORDER BY sa.company_id, sa.scenario
#             """
#             cursor.execute(query)
#         elif company_id:
#             query = """
#                 SELECT sa.scenario, sa.provider_id, sa.temperature, sa.max_tokens, sa.timeout, sa.company_id, p.name AS provider_name, p.provider_type
#                 FROM llm_scenario_assignments sa
#                 LEFT JOIN llm_providers p ON sa.provider_id = p.id
#                 WHERE sa.company_id = %s
#                 ORDER BY sa.scenario
#             """
#             cursor.execute(query, (company_id,))
#         else:
#             query = """
#                 SELECT sa.scenario, sa.provider_id, sa.temperature, sa.max_tokens, sa.timeout, sa.company_id, p.name AS provider_name, p.provider_type
#                 FROM llm_scenario_assignments sa
#                 LEFT JOIN llm_providers p ON sa.provider_id = p.id
#                 WHERE sa.company_id IS NULL
#                 ORDER BY sa.scenario
#             """
#             cursor.execute(query)
            
#         rows = cursor.fetchall()
#         cursor.close()
#         return jsonify({"status": True, "assignments": rows})
#     except Exception as e:
#         return jsonify({"status": False, "msg": str(e)}), 500
#     finally:
#         conn.close()


# def update_assignments_controller(get_conn):
#     """
#     PUT /api/llm/assignments — Bulk-update scenario assignments.
#     Body: { "assignments": [ { "scenario": "rag_chat", "provider_id": 1, "temperature": 0.3, "max_tokens": 4096 }, ... ], "company_id": 123 }
#     """
#     data = request.get_json(silent=True) or {}
#     assignments = data.get("assignments", [])
#     company_id = data.get("company_id")

#     if not assignments:
#         return jsonify({"status": False, "msg": "No assignments provided"}), 400

#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor(dictionary=True)
#         for a in assignments:
#             scenario = a.get("scenario", "").strip()
#             provider_id = a.get("provider_id")  # can be None
#             temperature = a.get("temperature", 0.3)
#             raw_tokens = a.get("max_tokens")
#             max_tokens = int(raw_tokens) if raw_tokens else 4096
            
#             raw_timeout = a.get("timeout")
#             timeout_val = int(raw_timeout) if raw_timeout else None

#             if not scenario:
#                 continue
                
#             if company_id:
#                 cursor.execute("SELECT id FROM llm_scenario_assignments WHERE scenario = %s AND company_id = %s", (scenario, company_id))
#             else:
#                 cursor.execute("SELECT id FROM llm_scenario_assignments WHERE scenario = %s AND company_id IS NULL", (scenario,))
                
#             row = cursor.fetchone()
            
#             if row:
#                 cursor.execute(
#                     "UPDATE llm_scenario_assignments SET provider_id = %s, temperature = %s, max_tokens = %s, timeout = %s WHERE id = %s",
#                     (provider_id, temperature, max_tokens, timeout_val, row['id'])
#                 )
#             else:
#                 cursor.execute(
#                     "INSERT INTO llm_scenario_assignments (scenario, provider_id, temperature, max_tokens, timeout, company_id) VALUES (%s, %s, %s, %s, %s, %s)",
#                     (scenario, provider_id, temperature, max_tokens, timeout_val, company_id)
#                 )
#         conn.commit()
#         cursor.close()
#         from model.llm_client import clear_llm_cache
#         clear_llm_cache()
      
#         return jsonify({"status": True, "msg": "Assignments updated"})
#     except Exception as e:
#         return jsonify({"status": False, "msg": str(e)}), 500
#     finally:
#         conn.close()

# def delete_assignments_controller(get_conn):
#     """
#     DELETE /api/llm/assignments?company_id=123
#     """
#     company_id = request.args.get("company_id")
#     if not company_id:
#         return jsonify({"status": False, "msg": "company_id is required"}), 400

#     conn = get_conn()
#     if not conn:
#         return jsonify({"status": False, "msg": "DB connection failed"}), 500
#     try:
#         cursor = conn.cursor()
#         cursor.execute("DELETE FROM llm_scenario_assignments WHERE company_id = %s", (company_id,))
#         conn.commit()
#         cursor.close()
        
#         from model.llm_client import clear_llm_cache
#         clear_llm_cache()
      
#         return jsonify({"status": True, "msg": "Company configuration deleted successfully"})
#     except Exception as e:
#         return jsonify({"status": False, "msg": str(e)}), 500
#     finally:
#         conn.close()


"""
LLM Configuration Controller
─────────────────────────────
Admin APIs for managing LLM providers and scenario → provider assignments.
"""

import re
import mysql.connector
from flask import request, jsonify


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    """Mask API key for display: show first 6 chars, rest as bullets."""
    if not key:
        return ""
    if len(key) <= 6:
        return "••••••"
    return key[:6] + "••••••••"


def _is_masked(key: str) -> bool:
    """Check if a key is the masked placeholder (shouldn't be saved)."""
    if not key:
        return True
    return "••" in key


# ── Known scenarios (for seeding / validation) ───────────────────────────────

KNOWN_SCENARIOS = [
    "rag_chat", "analysis", "visualization", "intent_routing", "chat",
    "insights", "query_branch", "web_search", "agent_planner",
    "knowledge", "sheet_processing",
]


# ── Table init (auto-create on first use) ────────────────────────────────────
# The _ensure_tables function has been removed. 
# Tables must be created manually via SQL script.

# ═══════════════════════════════════════════════════════════════════════════════
#  Provider CRUD
# ═══════════════════════════════════════════════════════════════════════════════

def get_providers_controller(get_conn):
    """GET /api/llm/providers — List all providers (keys masked)."""
    company_id = request.args.get('company_id')
    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        if company_id:
            cursor.execute(
                "SELECT id, name, provider_type, api_key, model_name, base_url, is_active, "
                "created_at, updated_at, company_id FROM llm_providers "
                "WHERE company_id = %s OR company_id IS NULL ORDER BY created_at ASC", (company_id,)
            )
        else:
            cursor.execute(
                "SELECT id, name, provider_type, api_key, model_name, base_url, is_active, "
                "created_at, updated_at, company_id FROM llm_providers ORDER BY created_at ASC"
            )
        rows = cursor.fetchall()
        cursor.close()

        # Mask API keys
        for row in rows:
            row["api_key_masked"] = _mask_key(row.get("api_key") or "")
            del row["api_key"]
            # Convert datetime to string for JSON serialization
            if row.get("created_at"):
                row["created_at"] = str(row["created_at"])
            if row.get("updated_at"):
                row["updated_at"] = str(row["updated_at"])

        return jsonify({"status": True, "providers": rows})
    except Exception as e:
        return jsonify({"status": False, "msg": str(e)}), 500
    finally:
        conn.close()


def create_provider_controller(get_conn):
    """POST /api/llm/providers — Create a new provider."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    provider_type = (data.get("provider_type") or "").strip()
    model_name = (data.get("model_name") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    is_active = data.get("is_active", True)
    company_id = data.get("company_id")

    if not name or not provider_type or not model_name:
        return jsonify({"status": False, "msg": "name, provider_type, and model_name are required"}), 400

    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO llm_providers (name, provider_type, api_key, model_name, base_url, is_active, company_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (name, provider_type, api_key if api_key else None, model_name, base_url, is_active, company_id)
        )
        conn.commit()
        new_id = cursor.lastrowid
        cursor.close()

      
        return jsonify({"status": True, "msg": "Provider created", "id": new_id}), 201
    except Exception as e:
        return jsonify({"status": False, "msg": str(e)}), 500
    finally:
        conn.close()


def update_provider_controller(get_conn, provider_id):
    """PUT /api/llm/providers/<id> — Update a provider."""
    data = request.get_json(silent=True) or {}

    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor(dictionary=True)

        # Fetch existing
        cursor.execute("SELECT * FROM llm_providers WHERE id = %s", (provider_id,))
        existing = cursor.fetchone()
        if not existing:
            cursor.close()
            return jsonify({"status": False, "msg": "Provider not found"}), 404

        # Build update fields
        name = (data.get("name") or existing["name"]).strip()
        provider_type = (data.get("provider_type") or existing["provider_type"]).strip()
        model_name = (data.get("model_name") or existing["model_name"]).strip()
        base_url = data.get("base_url", existing["base_url"] or "").strip()
        is_active = data.get("is_active", existing["is_active"])

        # API key: only update if a new non-masked key is provided
        new_key = (data.get("api_key") or "").strip()
        if new_key and not _is_masked(new_key):
            api_key = new_key
        else:
            api_key = existing["api_key"]

        cursor.execute(
            "UPDATE llm_providers SET name=%s, provider_type=%s, api_key=%s, "
            "model_name=%s, base_url=%s, is_active=%s WHERE id=%s",
            (name, provider_type, api_key, model_name, base_url, is_active, provider_id)
        )
        conn.commit()
        cursor.close()
        from model.llm_client import clear_llm_cache
        clear_llm_cache()
      
        return jsonify({"status": True, "msg": "Provider updated"})
    except Exception as e:
        return jsonify({"status": False, "msg": str(e)}), 500
    finally:
        conn.close()


def delete_provider_controller(get_conn, provider_id):
    """DELETE /api/llm/providers/<id> — Delete a provider (assignments auto-nullify via FK)."""
    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM llm_providers WHERE id = %s", (provider_id,))
        conn.commit()
        affected = cursor.rowcount
        cursor.close()

        if affected == 0:
            return jsonify({"status": False, "msg": "Provider not found"}), 404

      
        return jsonify({"status": True, "msg": "Provider deleted"})
    except Exception as e:
        return jsonify({"status": False, "msg": str(e)}), 500
    finally:
        conn.close()


def test_provider_controller(get_conn, provider_id):
    """POST /api/llm/providers/<id>/test — Quick connectivity test."""
    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM llm_providers WHERE id = %s", (provider_id,))
        provider = cursor.fetchone()
        cursor.close()

        if not provider:
            return jsonify({"status": False, "msg": "Provider not found"})

        ptype = provider["provider_type"]
        test_messages = [{"role": "user", "content": "Say 'OK' in one word."}]

        from model.llm_client import (
            _call_gemini, _call_mistral_cloud, _call_local, _call_openai
        )

        if ptype == "gemini":
            result = _call_gemini(test_messages, False, 0.1, provider["api_key"], provider["model_name"], provider.get("base_url"), timeout=60)
        elif ptype == "mistral_cloud":
            result = _call_mistral_cloud(test_messages, False, 0.1, provider["api_key"], provider["model_name"], provider.get("base_url"), timeout=60)
        elif ptype == "local":
            result = _call_local(test_messages, False, 0.1, provider.get("api_key"), provider["model_name"], provider["base_url"], timeout=600)
        elif ptype == "openai":
            result = _call_openai(test_messages, False, 0.1, provider["api_key"], provider["model_name"], provider["base_url"], timeout=60)
        elif ptype.strip().lower() == "openrouter":
            result = _call_openai(
                test_messages,
                False,
                0.1,
                provider["api_key"],
                provider["model_name"],
                provider.get("base_url"),
                timeout=60
            )
        else:
            return jsonify({"status": False, "msg": f"Unknown provider type: {ptype}"})

        return jsonify({"status": True, "msg": "Connection successful", "response": result[:200]})

    except Exception as e:
        err_str = str(e).lower()
        short_msg = "Test Failed"

        print(f"[LLM TEST] Provider: {provider_id}")
        print(f"[LLM TEST] Error: {repr(e)}")
        
        if "401" in err_str or "unauthorized" in err_str:
            short_msg = "Invalid API Key"
        elif "402" in err_str or "payment required" in err_str:
            short_msg = "Insufficient OpenRouter credits"
        elif "403" in err_str or "forbidden" in err_str:
            short_msg = "Access denied"
        elif "429" in err_str or "quota" in err_str:
            short_msg = "Rate limit or quota exceeded"
        elif "404" in err_str or "not found" in err_str:
            if "model" in err_str:
                short_msg = "Invalid Model Name."
            else:
                short_msg = "The provider API URL is incorrect."
        elif "timeout" in err_str:
            short_msg = "Server took too long to respond."
        elif "connection refused" in err_str or "failed to establish" in err_str:
            short_msg = "The server is offline or unreachable."
        
        return jsonify({"status": False, "msg": short_msg})
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  Scenario Assignments
# ═══════════════════════════════════════════════════════════════════════════════

def get_assignments_controller(get_conn):
    """GET /api/llm/assignments — Get all scenario → provider mappings."""
    company_id = request.args.get('company_id')
    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        
        if company_id == 'all':
            query = """
                SELECT sa.scenario, sa.provider_id, sa.temperature, sa.max_tokens, sa.timeout, sa.company_id, p.name AS provider_name, p.provider_type
                FROM llm_scenario_assignments sa
                LEFT JOIN llm_providers p ON sa.provider_id = p.id
                ORDER BY sa.company_id, sa.scenario
            """
            cursor.execute(query)
        elif company_id:
            query = """
                SELECT sa.scenario, sa.provider_id, sa.temperature, sa.max_tokens, sa.timeout, sa.company_id, p.name AS provider_name, p.provider_type
                FROM llm_scenario_assignments sa
                LEFT JOIN llm_providers p ON sa.provider_id = p.id
                WHERE sa.company_id = %s
                ORDER BY sa.scenario
            """
            cursor.execute(query, (company_id,))
        else:
            query = """
                SELECT sa.scenario, sa.provider_id, sa.temperature, sa.max_tokens, sa.timeout, sa.company_id, p.name AS provider_name, p.provider_type
                FROM llm_scenario_assignments sa
                LEFT JOIN llm_providers p ON sa.provider_id = p.id
                WHERE sa.company_id IS NULL
                ORDER BY sa.scenario
            """
            cursor.execute(query)
            
        rows = cursor.fetchall()
        cursor.close()
        return jsonify({"status": True, "assignments": rows})
    except Exception as e:
        return jsonify({"status": False, "msg": str(e)}), 500
    finally:
        conn.close()


def update_assignments_controller(get_conn):
    """
    PUT /api/llm/assignments — Bulk-update scenario assignments.
    Body: { "assignments": [ { "scenario": "rag_chat", "provider_id": 1, "temperature": 0.3, "max_tokens": 4096 }, ... ], "company_id": 123 }
    """
    data = request.get_json(silent=True) or {}
    assignments = data.get("assignments", [])
    company_id = data.get("company_id")

    if not assignments:
        return jsonify({"status": False, "msg": "No assignments provided"}), 400

    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor(dictionary=True)
        for a in assignments:
            scenario = a.get("scenario", "").strip()
            provider_id = a.get("provider_id")  # can be None
            temperature = a.get("temperature", 0.3)
            raw_tokens = a.get("max_tokens")
            max_tokens = int(raw_tokens) if raw_tokens else 4096
            
            raw_timeout = a.get("timeout")
            timeout_val = int(raw_timeout) if raw_timeout else None

            if not scenario:
                continue
                
            if company_id:
                cursor.execute("SELECT id FROM llm_scenario_assignments WHERE scenario = %s AND company_id = %s", (scenario, company_id))
            else:
                cursor.execute("SELECT id FROM llm_scenario_assignments WHERE scenario = %s AND company_id IS NULL", (scenario,))
                
            row = cursor.fetchone()
            
            if row:
                cursor.execute(
                    "UPDATE llm_scenario_assignments SET provider_id = %s, temperature = %s, max_tokens = %s, timeout = %s WHERE id = %s",
                    (provider_id, temperature, max_tokens, timeout_val, row['id'])
                )
            else:
                cursor.execute(
                    "INSERT INTO llm_scenario_assignments (scenario, provider_id, temperature, max_tokens, timeout, company_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (scenario, provider_id, temperature, max_tokens, timeout_val, company_id)
                )
        conn.commit()
        cursor.close()
        from model.llm_client import clear_llm_cache
        clear_llm_cache()
      
        return jsonify({"status": True, "msg": "Assignments updated"})
    except Exception as e:
        return jsonify({"status": False, "msg": str(e)}), 500
    finally:
        conn.close()

def delete_assignments_controller(get_conn):
    """
    DELETE /api/llm/assignments?company_id=123
    """
    company_id = request.args.get("company_id")
    if not company_id:
        return jsonify({"status": False, "msg": "company_id is required"}), 400

    conn = get_conn()
    if not conn:
        return jsonify({"status": False, "msg": "DB connection failed"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM llm_scenario_assignments WHERE company_id = %s", (company_id,))
        conn.commit()
        cursor.close()
        
        from model.llm_client import clear_llm_cache
        clear_llm_cache()
      
        return jsonify({"status": True, "msg": "Company configuration deleted successfully"})
    except Exception as e:
        return jsonify({"status": False, "msg": str(e)}), 500
    finally:
        conn.close()

