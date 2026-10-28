# import requests
# import json 
# import time
# from flask import request, has_request_context
# # pyrefly: ignore [missing-import]
# import google.generativeai as genai
# from database.config import engine

# # Global HTTP Session for connection pooling
# http_session = requests.Session()

# # Simple in-memory cache for LLM configs
# _config_cache = {}
# CACHE_TTL = 300  # 5 minutes

# def clear_llm_cache():
#     """Clear the in-memory LLM configuration cache."""
#     _config_cache.clear()
#     print("[LLM Client] Configuration cache cleared.")


# def get_current_scenario():
#     """Automatically determine the scenario based on the active API endpoint."""
#     if not has_request_context():
#         return "rag_chat"
#     try:
#         route = request.path
#         if "rag_chat" in route or "session-chat" in route or "chat" in route: return "rag_chat"
#         elif "analysis" in route or "analyze" in route: return "analysis"
#         elif "view_info" in route: return "visualization"
#         elif "insight" in route: return "insights"
#         elif "search" in route: return "web_search"
#         elif "agent" in route or "query" in route: return "agent_planner"
#         elif "knowledge" in route: return "knowledge"
#         elif "sheet" in route: return "sheet_processing"
#     except:
#         pass
#     return "rag_chat"


# def get_assigned_llm_config(scenario):
#     """Fetch the LLM configuration for the given scenario from the DB."""
#     now = time.time()
#     if scenario in _config_cache:
#         cached = _config_cache[scenario]
#         if now - cached["timestamp"] < CACHE_TTL:
#             return cached["data"]
            
#     try:
#         from sqlalchemy import text
#         with engine.connect() as conn:
#             query = text("""
#                 SELECT p.provider_type, p.api_key, p.model_name, p.base_url, a.temperature, a.max_tokens, a.timeout
#                 FROM llm_scenario_assignments a
#                 JOIN llm_providers p ON a.provider_id = p.id
#                 WHERE a.scenario = :scenario AND p.is_active = TRUE
#             """)
#             result = conn.execute(query, {"scenario": scenario})
#             row = result.mappings().fetchone()
            
#         config_data = dict(row) if row else None
#         _config_cache[scenario] = {"data": config_data, "timestamp": now}
#         return config_data
#     except Exception as e:
#         print(f"[LLM Client] DB fetch error for scenario '{scenario}': {e}")
#         if scenario in _config_cache:
#             return _config_cache[scenario]["data"]
#         return None


# def _get_fallback_mistral_local_config():
#     """Fetch the active mistral_local provider from the DB for fallback purposes."""
#     try:
#         from sqlalchemy import text
#         with engine.connect() as conn:
#             query = text("""
#                 SELECT api_key, model_name, base_url
#                 FROM llm_providers
#                 WHERE provider_type = 'mistral_local' AND is_active = TRUE
#                 LIMIT 1
#             """)
#             result = conn.execute(query)
#             row = result.mappings().fetchone()
#             return dict(row) if row else None
#     except Exception as e:
#         print(f"[LLM Fallback] DB fetch error for mistral_local fallback: {e}")
#         return None


# def _call_gemini(messages, json_mode, temperature, api_key, model_name, base_url=None, timeout=90):
#     if not api_key or not model_name:
#         raise ValueError("api_key and model_name are required for gemini")
        
#     kwargs = {"api_key": api_key}
#     if base_url:
#         kwargs["client_options"] = {"api_endpoint": base_url}
#     genai.configure(**kwargs)
    
#     system_instruction = None
#     contents = []
#     for msg in messages:
#         role = msg.get("role")
#         content = msg.get("content")
#         if role == "system":
#             system_instruction = content
#         elif role == "user":
#             contents.append({"role": "user", "parts": [content]})
#         elif role in ("assistant", "model"):
#             contents.append({"role": "model", "parts": [content]})
#     generation_config = {}
#     if json_mode:
#         generation_config["response_mime_type"] = "application/json"
#     if temperature is not None:
#         generation_config["temperature"] = temperature
#     model = genai.GenerativeModel(
#         model_name=model_name,
#         system_instruction=system_instruction,
#         generation_config=generation_config
#     )
#     # Gemini client uses its own internal timeout logic, but we can pass it if supported (not natively in simple generate_content)
#     return model.generate_content(contents).text.strip()


# def _call_mistral_cloud(messages, json_mode, temperature, api_key, model_name, base_url=None, timeout=90):
#     if not api_key or not model_name or not base_url:
#         raise ValueError("api_key, model_name, and base_url are required for mistral_cloud")
        
#     url = base_url if base_url.endswith("/chat/completions") else f"{base_url.rstrip('/')}/chat/completions"
        
#     headers = {
#         "Authorization": f"Bearer {api_key}",
#         "Content-Type": "application/json"
#     }
#     payload = {
#         "model": model_name,
#         "messages": messages,
#         "temperature": temperature
#     }
#     if json_mode:
#         payload["response_format"] = {"type": "json_object"}
#     res = http_session.post(url, json=payload, headers=headers, timeout=timeout)
#     res.raise_for_status()
#     return res.json()["choices"][0]["message"]["content"].strip()


# def _call_mistral_local(messages, json_mode, temperature, api_key, model_name, base_url, timeout=600):
#     if not model_name or not base_url:
#         raise ValueError("model_name and base_url are required for mistral_local")
        
#     start_time = time.time()
#     payload = {
#         "model": model_name,
#         "messages": messages,
#         "stream": False,
#         "options": {"temperature": temperature}
#     }
#     if json_mode:
#         payload["format"] = "json"
#     target_url = f"{base_url.rstrip('/')}/api/chat"
#     res = http_session.post(target_url, json=payload, timeout=timeout)
#     res.raise_for_status()
#     print(f"[LLM Client] Local Mistral responded in {time.time() - start_time:.2f} seconds")
#     return res.json()["message"]["content"].strip()


# def _call_openai(messages, json_mode, temperature, api_key, model_name, base_url, timeout=90):
#     if not api_key or not model_name or not base_url:
#         raise ValueError("api_key, model_name, and base_url are required for openai/openrouter")

#     headers = {
#         "Authorization": f"Bearer {api_key}",
#         "Content-Type": "application/json",
#         "HTTP-Referer": "https://traverseai.com",  # Optional, required by some providers like OpenRouter
#         "X-Title": "D-Agent"
#     }
#     payload = {
#         "model": model_name,
#         "messages": messages,
#         "temperature": temperature
#     }
#     if json_mode:
#         payload["response_format"] = {"type": "json_object"}
        
#     target_url = base_url if base_url.endswith("/chat/completions") else f"{base_url.rstrip('/')}/chat/completions"
        
#     res = http_session.post(target_url, json=payload, headers=headers, timeout=timeout)
#     res.raise_for_status()
#     return res.json()["choices"][0]["message"]["content"].strip()


# PROVIDER_HANDLERS = {
#     "gemini": _call_gemini,
#     "mistral_cloud": _call_mistral_cloud,
#     "mistral_local": _call_mistral_local,
#     "openai": _call_openai,
#     "openrouter": _call_openai,
# }


# def call_llm_chat(messages: list, json_mode: bool = False, temperature: float = 0.3, scenario: str = None) -> str:
#     # --- DEBUG LOG FOR ALL LLM CALLS ---
#     system_msg = next((m["content"] for m in messages if m.get("role") == "system"), None)
#     if system_msg:
#         print(f"\n{'='*60}\n[LLM CLIENT] SYSTEM PROMPT BEING SENT:\n{'-'*60}\n{system_msg}\n{'='*60}\n")
#     # -----------------------------------

#     if scenario is None:
#         scenario = get_current_scenario()
#     config = get_assigned_llm_config(scenario)

#     if not config:
#         print(f"[LLM Error] No DB config found for scenario '{scenario}'. Attempting mistral_local fallback.")
#         fallback_config = _get_fallback_mistral_local_config()
#         if not fallback_config:
#             err_msg = f"No DB config found for scenario '{scenario}' and no active mistral_local provider available in DB."
#             return json.dumps({"error": err_msg}) if json_mode else f"[LLM Error] {err_msg}"
        
#         provider = "mistral_local"
#         api_key = fallback_config.get("api_key")
#         model_name = fallback_config.get("model_name")
#         base_url = fallback_config.get("base_url")
#         timeout_val = 600
#         print(f"[LLM Client] DB Routing Active -> Scenario: {scenario} | Provider: {provider} | Model: {model_name} (FALLBACK)")
#     else:
#         provider = config['provider_type']
#         api_key = config['api_key']
#         model_name = config['model_name']
#         base_url = config['base_url']
#         if config['temperature'] is not None:
#             temperature = config['temperature']
#         timeout_val = config.get('timeout')
#         if timeout_val is None:
#             timeout_val = 600 if provider == "mistral_local" else 90
#         print(f"[LLM Client] DB Routing Active -> Scenario: {scenario} | Provider: {provider} | Model: {model_name}")

#     try:
#         # 3. Execute with whichever provider was selected, with retry logic
#         max_retries = 2
#         last_err = None
        
#         for attempt in range(max_retries + 1):
#             try:
#                 handler = PROVIDER_HANDLERS.get(provider.strip().lower())
#                 if handler:
#                     return handler(messages, json_mode, temperature, api_key, model_name, base_url, timeout=timeout_val)
#                 else:
#                     print(f"[LLM Client] Unknown provider '{provider}'")
#                     return json.dumps({"error": "Invalid config"}) if json_mode else "[LLM Error] Invalid config"
#             except Exception as loop_e:
#                 last_err = loop_e
#                 if provider == "mistral_local" or attempt == max_retries:
#                     raise last_err
#                 print(f"[LLM Client] Attempt {attempt + 1} failed for {provider}: {loop_e}. Retrying...")
#                 time.sleep(1)

#     except Exception as e:
#         print(f"[LLM Error] Provider '{provider}' failed after retries: {e}")
#         if provider != "mistral_local":
#             print("[LLM Error] Attempting ultimate fallback to mistral_local from DB...")
#             try:
#                 fallback_config = _get_fallback_mistral_local_config()
#                 if fallback_config:
#                     return _call_mistral_local(messages, json_mode, temperature, fallback_config.get("api_key"), fallback_config.get("model_name"), fallback_config.get("base_url"))
#                 else:
#                     raise ValueError("No active mistral_local provider found in DB")
#             except Exception as fallback_err:
#                 print(f"[LLM Error] Both primary and local fallback failed. Error: {fallback_err}")
#                 if json_mode:
#                     return json.dumps({"error": "LLM failed completely", "details": str(fallback_err)})
#                 return f"[LLM Error] Both primary and local fallback failed. Error: {str(fallback_err)}"
        
#         if json_mode:
#             return json.dumps({"error": "LLM failed completely", "details": str(e)})
#         return f"[LLM Error] LLM failed completely. Error: {str(e)}"


# def call_llm(prompt: str, scenario: str = None) -> str:
#     return call_llm_chat([{"role": "user", "content": prompt}], json_mode=False, scenario=scenario)




import requests
import json 
import time
from flask import request, has_request_context
# pyrefly: ignore [missing-import]
import google.generativeai as genai
from database.config import engine

# Global HTTP Session for connection pooling
http_session = requests.Session()

# Simple in-memory cache for LLM configs
_config_cache = {}
CACHE_TTL = 300  # 5 minutes

def clear_llm_cache():
    """Clear the in-memory LLM configuration cache."""
    _config_cache.clear()
    print("[LLM Client] Configuration cache cleared.")


def get_current_scenario():
    """Automatically determine the scenario based on the active API endpoint."""
    if not has_request_context():
        return "rag_chat"
    try:
        route = request.path
        if "rag_chat" in route or "session-chat" in route or "chat" in route: return "rag_chat"
        elif "analysis" in route or "analyze" in route: return "analysis"
        elif "view_info" in route: return "visualization"
        elif "insight" in route: return "insights"
        elif "search" in route: return "web_search"
        elif "agent" in route or "query" in route: return "agent_planner"
        elif "knowledge" in route: return "knowledge"
        elif "sheet" in route: return "sheet_processing"
    except:
        pass
    return "rag_chat"


def get_assigned_llm_config(scenario):
    """Fetch the LLM configuration for the given scenario from the DB."""
    now = time.time()
    if scenario in _config_cache:
        cached = _config_cache[scenario]
        if now - cached["timestamp"] < CACHE_TTL:
            return cached["data"]
            
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            query = text("""
                SELECT p.provider_type, p.api_key, p.model_name, p.base_url, a.temperature, a.max_tokens, a.timeout
                FROM llm_scenario_assignments a
                JOIN llm_providers p ON a.provider_id = p.id
                WHERE a.scenario = :scenario AND p.is_active = TRUE
            """)
            result = conn.execute(query, {"scenario": scenario})
            row = result.mappings().fetchone()
            
        config_data = dict(row) if row else None
        _config_cache[scenario] = {"data": config_data, "timestamp": now}
        return config_data
    except Exception as e:
        print(f"[LLM Client] DB fetch error for scenario '{scenario}': {e}")
        if scenario in _config_cache:
            return _config_cache[scenario]["data"]
        return None


def _get_fallback_mistral_local_config():
    """Fetch the active mistral_local provider from the DB for fallback purposes."""
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            query = text("""
                SELECT api_key, model_name, base_url
                FROM llm_providers
                WHERE provider_type = 'mistral_local' AND is_active = TRUE
                LIMIT 1
            """)
            result = conn.execute(query)
            row = result.mappings().fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[LLM Fallback] DB fetch error for mistral_local fallback: {e}")
        return None


def _call_gemini(messages, json_mode, temperature, api_key, model_name, base_url=None, timeout=90):
    if not api_key or not model_name:
        raise ValueError("api_key and model_name are required for gemini")
        
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["client_options"] = {"api_endpoint": base_url}
    genai.configure(**kwargs)
    
    system_instruction = None
    contents = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            system_instruction = content
        elif role == "user":
            contents.append({"role": "user", "parts": [content]})
        elif role in ("assistant", "model"):
            contents.append({"role": "model", "parts": [content]})
    generation_config = {}
    if json_mode:
        generation_config["response_mime_type"] = "application/json"
    if temperature is not None:
        generation_config["temperature"] = temperature
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=system_instruction,
        generation_config=generation_config
    )
    # Gemini client uses its own internal timeout logic, but we can pass it if supported (not natively in simple generate_content)
    return model.generate_content(contents).text.strip()


def _call_mistral_cloud(messages, json_mode, temperature, api_key, model_name, base_url=None, timeout=90):
    if not api_key or not model_name or not base_url:
        raise ValueError("api_key, model_name, and base_url are required for mistral_cloud")
        
    url = base_url if base_url.endswith("/chat/completions") else f"{base_url.rstrip('/')}/chat/completions"
        
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    res = http_session.post(url, json=payload, headers=headers, timeout=timeout)
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


def _call_local(messages, json_mode, temperature, api_key, model_name, base_url, timeout=600):
    if not model_name or not base_url:
        raise ValueError("model_name and base_url are required for mistral_local")
        
    start_time = time.time()
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature}
    }
    if json_mode:
        payload["format"] = "json"
    target_url = f"{base_url.rstrip('/')}/api/chat"
    res = http_session.post(target_url, json=payload, timeout=timeout)
    res.raise_for_status()
    print(f"[LLM Client] Local Mistral responded in {time.time() - start_time:.2f} seconds")
    return res.json()["message"]["content"].strip()


def _call_openai(messages, json_mode, temperature, api_key, model_name, base_url, timeout=90):
    if not api_key or not model_name or not base_url:
        raise ValueError("api_key, model_name, and base_url are required for openai/openrouter")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://traverseai.com",  # Optional, required by some providers like OpenRouter
        "X-Title": "D-Agent"
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
        
    target_url = base_url if base_url.endswith("/chat/completions") else f"{base_url.rstrip('/')}/chat/completions"
        
    res = http_session.post(target_url, json=payload, headers=headers, timeout=timeout)
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


PROVIDER_HANDLERS = {
    "gemini": _call_gemini,
    "mistral_cloud": _call_mistral_cloud,
    "local": _call_local,
    "openai": _call_openai,
    "openrouter": _call_openai,
}


def call_llm_chat(messages: list, json_mode: bool = False, temperature: float = 0.3, scenario: str = None) -> str:
    # --- DEBUG LOG FOR ALL LLM CALLS ---
    system_msg = next((m["content"] for m in messages if m.get("role") == "system"), None)
    if system_msg:
        print(f"\n{'='*60}\n[LLM CLIENT] SYSTEM PROMPT BEING SENT:\n{'-'*60}\n{system_msg}\n{'='*60}\n")
    # -----------------------------------

    if scenario is None:
        scenario = get_current_scenario()
    config = get_assigned_llm_config(scenario)

    if not config:
        print(f"[LLM Error] No DB config found for scenario '{scenario}'. Attempting mistral_local fallback.")
        fallback_config = _get_fallback_mistral_local_config()
        if not fallback_config:
            err_msg = f"No DB config found for scenario '{scenario}' and no active mistral_local provider available in DB."
            return json.dumps({"error": err_msg}) if json_mode else f"[LLM Error] {err_msg}"
        
        provider = "local"
        api_key = fallback_config.get("api_key")
        model_name = fallback_config.get("model_name")
        base_url = fallback_config.get("base_url")
        timeout_val = 600
        print(f"[LLM Client] DB Routing Active -> Scenario: {scenario} | Provider: {provider} | Model: {model_name} (FALLBACK)")
    else:
        provider = config['provider_type']
        api_key = config['api_key']
        model_name = config['model_name']
        base_url = config['base_url']
        if config['temperature'] is not None:
            temperature = config['temperature']
        timeout_val = config.get('timeout')
        if timeout_val is None:
            timeout_val = 600 if provider == "local" else 90
        print(f"[LLM Client] DB Routing Active -> Scenario: {scenario} | Provider: {provider} | Model: {model_name}")

    try:
        # 3. Execute with whichever provider was selected, with retry logic
        max_retries = 1
        last_err = None
        
        for attempt in range(max_retries + 1):
            try:
                handler = PROVIDER_HANDLERS.get(provider.strip().lower())
                if handler:
                    return handler(messages, json_mode, temperature, api_key, model_name, base_url, timeout=timeout_val)
                else:
                    print(f"[LLM Client] Unknown provider '{provider}'")
                    return json.dumps({"error": "Invalid config"}) if json_mode else "[LLM Error] Invalid config"
            except Exception as loop_e:
                last_err = loop_e
                if provider == "local" or attempt == max_retries:
                    raise last_err
                print(f"[LLM Client] Attempt {attempt + 1} failed for {provider}: {loop_e}. Retrying...")
                time.sleep(1)

    except Exception as e:
        print(f"[LLM Error] Provider '{provider}' failed after retries: {e}")
        try:
            from utils.logger_service import log_system_error
            err_type = "timeout" if "timeout" in str(e).lower() else "llm_error"
            log_system_error(None, f"llm_client_{provider}", f"Provider '{provider}' failed: {e}", error_type=err_type)
        except Exception:
            pass

        if provider != "mistral_local":
            print("[LLM Error] Attempting ultimate fallback to mistral_local from DB...")
            try:
                fallback_config = _get_fallback_mistral_local_config()
                if fallback_config:
                    return _call_local(messages, json_mode, temperature, fallback_config.get("api_key"), fallback_config.get("model_name"), fallback_config.get("base_url"))
                else:
                    raise ValueError("No active mistral_local provider found in DB")
            except Exception as fallback_err:
                print(f"[LLM Error] Both primary and local fallback failed. Error: {fallback_err}")
                try:
                    from utils.logger_service import log_system_error
                    log_system_error(None, "llm_client_fallback", f"Both primary '{provider}' and fallback failed: {fallback_err}", error_type="fatal_llm_error")
                except Exception:
                    pass
                if json_mode:
                    return json.dumps({"error": "LLM failed completely", "details": str(fallback_err)})
                return f"[LLM Error] Both primary and local fallback failed. Error: {str(fallback_err)}"
        
        if json_mode:
            return json.dumps({"error": "LLM failed completely", "details": str(e)})
        return f"[LLM Error] LLM failed completely. Error: {str(e)}"


def call_llm(prompt: str, scenario: str = None) -> str:
    return call_llm_chat([{"role": "user", "content": prompt}], json_mode=False, scenario=scenario)

