import json

def format_sse(data: dict) -> str:
    """Format a Python dict as an SSE data frame."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def get_friendly_status(stage: str, question: str = "", intent: str = None, row_count: int = None) -> str:
    """
    Generate clean, user-friendly, non-technical status labels based on the query context
    and the current RAG execution stage. Never leaks table names, column names, or raw SQL.
    """
    q_lower = (question or "").lower()

    # Topic detection
    is_sales = any(w in q_lower for w in ["sale", "revenue", "target", "profit", "margin", "growth", "performance", "turnover", "quota"])
    is_customer = any(w in q_lower for w in ["customer", "dealer", "client", "buyer", "distributor", "vendor", "account"])
    is_product = any(w in q_lower for w in ["product", "sku", "category", "item", "material", "brand", "model", "tyre", "tire"])
    is_geo = any(w in q_lower for w in ["region", "zone", "territory", "state", "city", "branch", "area", "country"])
    is_time = any(w in q_lower for w in ["2021", "2022", "2023", "2024", "2025", "2026", "month", "quarter", "q1", "q2", "q3", "q4", "ytd", "daily", "weekly", "yearly", "year"])

    if stage == "start":
        return "Thinking..."

    elif stage == "analyzing":
        if is_sales:
            if is_geo:
                return "Analyzing sales and performance across regions..."
            elif is_time:
                return "Analyzing revenue trends over the selected period..."
            return "Analyzing your sales and performance query..."
        elif is_customer:
            return "Looking into dealer and customer activity..."
        elif is_product:
            return "Reviewing product and category metrics..."
        elif is_geo:
            return "Examining geographical and regional trends..."
        return "Understanding your question and context..."

    elif stage == "retrieving":
        if intent == "AGGREGATION":
            return "Gathering enterprise transactional records..."
        elif intent == "INSIGHT":
            return "Searching knowledge base & documentation..."
        elif intent == "HYBRID":
            return "Correlating enterprise data with knowledge context..."
        return "Connecting to your business data..."

    elif stage == "computing":
        if row_count is not None and row_count > 0:
            if is_sales:
                return "Crunching numbers and calculating financial metrics..."
            return "Processing records and calculating comparisons..."
        return "Synthesizing insights from retrieved data..."

    elif stage == "visualizing":
        return "Preparing charts and visual insights..."

    elif stage == "finalizing":
        return "Drafting your comprehensive answer..."

    return "Processing your request..."
