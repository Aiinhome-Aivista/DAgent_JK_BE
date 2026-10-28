import re
from model.llm_client import call_llm_chat
from database.prompt_loader import get_prompt

# ── Deterministic guards ───────────────────────────────────────────────────
# Ranking / math phrasing must ALWAYS go to SQL. Relying solely on an LLM
# classifier (with an INSIGHT fallback) was a major source of inconsistency:
# the same question could route to SQL one run and to a 200-row RAG sample the
# next, producing different answers.
_AGG_RE = re.compile(
    r'\b(top|bottom|worst|best|highest|lowest|largest|smallest|most|least|'
    r'rank|ranking|total|sum|count|how many|number of|average|avg|mean|'
    r'median|maximum|minimum|\bmax\b|\bmin\b|per\s|group(?:ed)?\s+by|'
    r'breakdown|distribution|compare|comparison|growth|trend|share|'
    r'percentage|percent|%|month\s*wise|year\s*wise|region\s*wise|zone\s*wise)\b',
    re.I,
)
_INSIGHT_RE = re.compile(
    r'\b(why|reason|explain|describe|opinion|think|feel|sentiment|complaint|'
    r'summari|recommend|suggest|qualitative)\b',
    re.I,
)


def classify_intent(user_query: str, workspace_id: str = None, conn=None) -> str:
    """
    Classify a query into AGGREGATION, INSIGHT, or HYBRID.

    1. AGGREGATION: filtering, grouping, counting, ranking, sorting, math.
    2. INSIGHT: conceptual relationships, reasons, 'why' questions.
    3. HYBRID: structural filtering first, then semantic context.

    Fails CLOSED to AGGREGATION (computable) rather than INSIGHT (sampled RAG),
    so numeric questions are never answered from a partial sample.
    """
    q = user_query or ""

    # Fast path: clear ranking/aggregation wording with no insight wording.
    if _AGG_RE.search(q) and not _INSIGHT_RE.search(q):
        return "AGGREGATION"

    router_prompt = get_prompt(workspace_id, 'intent_routing')

    if not router_prompt or not router_prompt.strip():
        print("[Router] ERROR: No router prompt found in DB for workspace or global fallback. Returning default AGGREGATION.")
        return "AGGREGATION"
    # Append user query to prompt
    router_prompt += f'\n\nUser Query: "{user_query}"\nCategory:'

    messages = [{"role": "user", "content": router_prompt}]
    try:
        response = call_llm_chat(messages, temperature=0.0)
        if response:
            category = response.strip().upper()
            if category in ["AGGREGATION", "INSIGHT", "HYBRID"]:
                return category
    except Exception as e:
        print(f"[Router] Error classifying intent: {e}")

    # Fail closed to AGGREGATION so numeric questions are computed, not sampled.
    return "AGGREGATION"



#####
# import re
# from model.llm_client import call_llm_chat

# _AGG_RE = re.compile(
#     r'\b(top|bottom|worst|best|highest|lowest|largest|smallest|most|least|'
#     r'rank|ranking|total|sum|count|how many|number of|average|avg|mean|'
#     r'median|maximum|minimum|\bmax\b|\bmin\b|per\s|group(?:ed)?\s+by|'
#     r'breakdown|distribution|compare|comparison|growth|trend|share|'
#     r'percentage|percent|%|month\s*wise|year\s*wise|region\s*wise|zone\s*wise)\b',
#     re.I,
# )
# _INSIGHT_RE = re.compile(
#     r'\b(why|reason|explain|describe|opinion|think|feel|sentiment|complaint|'
#     r'summari|recommend|suggest|qualitative)\b',
#     re.I,
# )


# def classify_intent(user_query: str) -> str:
#     """
#     Classify a query into AGGREGATION, INSIGHT, or HYBRID.

#     1. AGGREGATION: filtering, grouping, counting, ranking, sorting, math.
#     2. INSIGHT: conceptual relationships, reasons, 'why' questions.
#     3. HYBRID: structural filtering first, then semantic context.

#     Fails CLOSED to AGGREGATION (computable) rather than INSIGHT (sampled RAG),
#     so numeric questions are never answered from a partial sample.
#     """
#     q = user_query or ""

#     # Fast path: clear ranking/aggregation wording with no insight wording.
#     if _AGG_RE.search(q) and not _INSIGHT_RE.search(q):
#         return "AGGREGATION"

#     router_prompt = f"""
#     You are an AI query router for a data system. Analyze the user's query and classify it into exactly one of three categories:

#     1. AGGREGATION: Use this if the question can be answered entirely using database operations such as filtering, grouping, counting, ranking, joining, set operations, averages, percentages, or window functions (e.g., "Identify percentage of active customers who bought both", "top 10 customers", "total sales").
#     2. INSIGHT: Use this if the query requires finding conceptual relationships, trends, contextual explanations, or reading specific notes (e.g., "Why did region X fail?", "What do customers think about product Y?").
#     3. HYBRID: Use this if the query requires filtering by specific IDs or categories first, and then finding semantic context (e.g., "Summarize the complaints for our top 5 most expensive products").

#     Respond with ONLY the category name: AGGREGATION, INSIGHT, or HYBRID.

#     User Query: "{user_query}"
#     Category:"""

#     messages = [{"role": "user", "content": router_prompt}]
#     try:
#         response = call_llm_chat(messages, temperature=0.0)
#         if response:
#             category = response.strip().upper()
#             if category in ["AGGREGATION", "INSIGHT", "HYBRID"]:
#                 return category
#     except Exception as e:
#         print(f"[Router] Error classifying intent: {e}")

#     return "AGGREGATION"

#####