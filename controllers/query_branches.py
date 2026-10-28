import json
from model.llm_client import call_llm_chat

def execute_aggregation(user_query: str, get_connection_func, schema_info: str = "SQL Database") -> str:
    """
    Handles AGGREGATION intent by converting natural language to SQL/AQL,
    executing it directly on the database engine, and returning formatted results.
    """
    print("⚡ Bypassing VectorDB. Routing to Structured Database (SQL/AQL)...")
    
    # 1. Generate SQL/AQL using LLM
    generation_prompt = f"""
    You are a database expert. Generate a valid SQL query to answer the user's question based on the following schema information.
    
    Schema/Context: {schema_info}
    
    User Query: "{user_query}"
    
    CRITICAL DATE RULE: The 'Month' column (or similar periods) are formatted as YYYYMM (e.g., 202601 for January 2026). If the user mentions a month (e.g., "January 2026"), you MUST format it as YYYYMM in the SQL WHERE clause (e.g., `Month` = 202601).
    
    Return ONLY the raw SQL query, with no markdown formatting or explanation.
    """
    
    sql_query = ""
    try:
        response = call_llm_chat([{"role": "user", "content": generation_prompt}], temperature=0.0)
        sql_query = response.replace("```sql", "").replace("```", "").strip() if response else ""
    except Exception as e:
        print(f"[Aggregation] Error generating query: {e}")
        return f"Error generating query: {str(e)}"
    
    if not sql_query:
        return "Failed to generate structured query."

    # 2. Execute directly on the database
    conn = cur = None
    raw_data = []
    try:
        conn = get_connection_func()
        cur = conn.cursor(dictionary=True)
        cur.execute(sql_query)
        raw_data = cur.fetchall()
    except Exception as e:
        print(f"[Aggregation] Error executing query '{sql_query}': {e}")
        return f"Database error: {str(e)}"
    finally:
        if cur: cur.close()
        if conn: conn.close()

    # 3. Present structured result back
    format_prompt = f"""
    Format the following raw database results into a natural, easy-to-read summary answering the user's question.
    
    User Query: "{user_query}"
    Raw Data: {json.dumps(raw_data, default=str)}
    
    CRITICAL DATE RULE: If the Raw Data contains YYYYMM dates (e.g., 202601), convert them to readable month names (e.g., "January 2026") in your answer.
    
    Answer:
    """
    try:
        response = call_llm_chat([{"role": "user", "content": format_prompt}], temperature=0.2)
        return response.strip() if response else str(raw_data)
    except Exception as e:
        print(f"[Aggregation] Error formatting response: {e}")
        return str(raw_data)


def execute_hybrid(user_query: str, get_connection_func, schema_info: str = "SQL Database") -> list:
    """
    Handles HYBRID intent. 
    1. AQL/SQL First: get a targeted list of entities.
    2. Returns the entities to be used as metadata filters in the standard RAG pipeline.
    """
    print("🧠 Bypassing standard retrieve. Routing to HYBRID (AQL First -> Then RAG)...")
    
    generation_prompt = f"""
    You are a database expert. Generate a valid SQL query that returns ONLY a list of IDs or Names relevant to filtering the user's question.
    
    Schema/Context: {schema_info}
    
    User Query: "{user_query}"
    
    CRITICAL DATE RULE: The 'Month' column (or similar periods) are formatted as YYYYMM (e.g., 202601 for January 2026). If the user mentions a month (e.g., "January 2026"), you MUST format it as YYYYMM in the SQL WHERE clause.
    
    Return ONLY the raw SQL query, with no markdown formatting or explanation. Ensure it selects a single identifier column.
    CRITICAL: You MUST fully qualify the table name with the database name provided in the Schema/Context, in the format `database_name`.`table_name`.
    """
    
    sql_query = ""
    try:
        response = call_llm_chat([{"role": "user", "content": generation_prompt}], temperature=0.0)
        sql_query = response.replace("```sql", "").replace("```", "").strip() if response else ""
    except Exception as e:
        print(f"[Hybrid] Error generating filter query: {e}")
        return []

    conn = cur = None
    entity_list = []
    try:
        conn = get_connection_func()
        cur = conn.cursor()
        cur.execute(sql_query)
        rows = cur.fetchall()
        if rows:
            entity_list = [str(r[0]) for r in rows]
    except Exception as e:
        print(f"[Hybrid] Error executing filter query '{sql_query}': {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()
        
    return entity_list
