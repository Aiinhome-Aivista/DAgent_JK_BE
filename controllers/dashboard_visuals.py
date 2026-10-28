import json
import re
import calendar
from datetime import datetime, timedelta
from flask import request, jsonify
from model.llm_client import call_llm_chat
from database import config
from database.hana_metadata_service import HANA_DB_TYPES, TARGET_SCHEMA, get_hana_connection_from_credential


# ─────────────────────────────────────────────────────────────────────────────
# DB / Cursor Helpers for Cross-DB Compatibility (MySQL & SAP HANA)
# ─────────────────────────────────────────────────────────────────────────────

def get_sap_connection_for_session(mysql_conn, session_id, user_id=None):
    """
    Checks if session_id (or user_id) has SAP HANA credentials in database_credential
    or sap_hana_view_metadata.
    If yes, establishes a live SAP HANA connection and returns (hana_conn, tunnel, effective_schema).
    If not an SAP session or connection fails, returns (None, None, None).
    """
    if not session_id and not user_id:
        return None, None, None

    try:
        try:
            cur = mysql_conn.cursor(dictionary=True)
        except TypeError:
            cur = mysql_conn.cursor()

        row = None

        # 1. Look up database_credential by session_id with SAP type
        if session_id:
            cur.execute("""
                SELECT credential, db_type, connection_id
                FROM database_credential
                WHERE session_id = %s
                  AND LOWER(TRIM(db_type)) IN ('hana', 'sap_hana', 'saphana', 'sap', 'sap_datasphere')
                ORDER BY created_at DESC LIMIT 1
            """, (session_id,))
            row = cur.fetchone()

        # 2. Look up via sap_hana_view_metadata
        if not row and session_id:
            cur.execute("""
                SELECT connection_id, schema_name
                FROM sap_hana_view_metadata
                WHERE session_id = %s
                LIMIT 1
            """, (session_id,))
            meta_row = cur.fetchone()
            if meta_row and meta_row.get("connection_id"):
                cid = meta_row["connection_id"]
                cur.execute("""
                    SELECT credential, db_type, connection_id
                    FROM database_credential
                    WHERE connection_id = %s
                    LIMIT 1
                """, (cid,))
                row = cur.fetchone()

        # 3. Look up by user_id if still not found
        if not row and user_id:
            cur.execute("""
                SELECT credential, db_type, connection_id
                FROM database_credential
                WHERE user_id = %s
                  AND LOWER(TRIM(db_type)) IN ('hana', 'sap_hana', 'saphana', 'sap', 'sap_datasphere')
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,))
            row = cur.fetchone()

        if not row:
            return None, None, None

        cred_raw = row.get("credential")
        if isinstance(cred_raw, str):
            external_db = json.loads(cred_raw)
        else:
            external_db = cred_raw or {}

        if not external_db:
            return None, None, None

        hana_conn, tunnel, effective_schema = get_hana_connection_from_credential(external_db)
        # Determine actual schema: SAP S/4HANA views live in 'SAP_S4'.
        # If user/credential has 'SAP_S4#AI_BI_EXT', strip the '#...' suffix or use 'SAP_S4'.
        schema = "SAP_S4"
        raw_schema = effective_schema or external_db.get("schema") or external_db.get("database")
        if raw_schema:
            clean_s = str(raw_schema).split("#")[0].strip()
            schema = clean_s if clean_s else "SAP_S4"
        else:
            schema = TARGET_SCHEMA or "SAP_S4"
        return hana_conn, tunnel, schema

    except Exception as e:
        print(f"[SAP-CONN] Live HANA connection failed for session {session_id}: {e}")
        return None, None, None


def get_sap_table_columns(mysql_conn, hana_conn, schema, session_id):
    """
    Returns a dict mapping {table_name: {clean_col_name: actual_col_name}}
    First tries MySQL sap_hana_view_metadata cache, then falls back to live SYS.VIEW_COLUMNS.
    """
    table_columns = {}
    if mysql_conn and session_id:
        try:
            try:
                cur = mysql_conn.cursor(dictionary=True)
            except TypeError:
                cur = mysql_conn.cursor()
            cur.execute("""
                SELECT view_name, columns_detail
                FROM sap_hana_view_metadata
                WHERE session_id = %s AND columns_detail IS NOT NULL
            """, (session_id,))
            rows = cur.fetchall()
            for r in rows:
                v_name = r.get("view_name")
                cols_det = r.get("columns_detail")
                if isinstance(cols_det, str):
                    try:
                        cols_det = json.loads(cols_det)
                    except Exception:
                        cols_det = []
                if v_name and isinstance(cols_det, list):
                    cols_dict = {}
                    for c in cols_det:
                        c_name = c.get("name") if isinstance(c, dict) else str(c)
                        if c_name:
                            clean = str(c_name).lower().replace(' ', '').replace('_', '')
                            cols_dict[clean] = c_name
                    table_columns[v_name] = cols_dict
        except Exception as e:
            print(f"[SAP-COLS] Error reading metadata cache: {e}")

    # Fallback to querying SYS.VIEW_COLUMNS live from HANA
    if not table_columns and hana_conn:
        try:
            h_cur = hana_conn.cursor()
            h_cur.execute("""
                SELECT VIEW_NAME, COLUMN_NAME
                FROM SYS.VIEW_COLUMNS
                WHERE SCHEMA_NAME = ?
                ORDER BY VIEW_NAME, POSITION
            """, (schema,))
            for r in h_cur.fetchall():
                v_name, c_name = r[0], r[1]
                if v_name not in table_columns:
                    table_columns[v_name] = {}
                clean = str(c_name).lower().replace(' ', '').replace('_', '')
                table_columns[v_name][clean] = c_name

            h_cur.execute("""
                SELECT TABLE_NAME, COLUMN_NAME
                FROM SYS.TABLE_COLUMNS
                WHERE SCHEMA_NAME = ?
                ORDER BY TABLE_NAME, POSITION
            """, (schema,))
            for r in h_cur.fetchall():
                t_name, c_name = r[0], r[1]
                if t_name not in table_columns:
                    table_columns[t_name] = {}
                clean = str(c_name).lower().replace(' ', '').replace('_', '')
                table_columns[t_name][clean] = c_name
            h_cur.close()
        except Exception as e:
            print(f"[SAP-COLS] Error fetching columns from HANA catalog: {e}")

    return table_columns


def execute_query(cursor, query, params=None, is_hana=False):
    """Executes a query converting %s placeholders to ? for SAP HANA (hdbcli)."""
    params_tuple = tuple(params) if params else ()
    if is_hana:
        hana_query = query.replace("%s", "?")
        cursor.execute(hana_query, params_tuple)
    else:
        cursor.execute(query, params_tuple)


def fetchone_dict(cursor):
    """Fetches a single row as a lowercase-keyed dict."""
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return {k.lower(): v for k, v in row.items()}
    cols = [desc[0].lower() for desc in cursor.description] if cursor.description else []
    return dict(zip(cols, row))


def fetchall_dict(cursor):
    """Fetches all rows as lowercase-keyed dicts."""
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [{k.lower(): v for k, v in r.items()} for r in rows]
    cols = [desc[0].lower() for desc in cursor.description] if cursor.description else []
    return [dict(zip(cols, r)) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Chat Context Extraction Controllers (LLM-based)
# ─────────────────────────────────────────────────────────────────────────────

def graph_metrics_controller():
    """
    Extracts the 4 dashboard metrics dynamically from the chat context.
    Expects JSON: { "question": "...", "answer": "..." }
    """
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
        
    question = data.get("question", "")
    answer = data.get("answer", "")
    
    defaults = {
        "metric_1": {"label": "Metric 1", "value": "N/A", "subtext": "No data available"},
        "metric_2": {"label": "Metric 2", "value": "N/A", "subtext": "No data available"},
        "metric_3": {"label": "Metric 3", "value": "N/A", "subtext": "No data available"},
        "metric_4": {"label": "Metric 4", "value": "N/A", "subtext": "No data available"}
    }

    if not answer.strip():
        return jsonify({"status": "success", "data": defaults}), 200
    
    prompt = f"""
You are a data extraction assistant for a sales dashboard. 
Based on the user's question and the system's answer below, extract up to 4 key numerical highlights or metrics dynamically.

Instead of hardcoding specific metrics, find ANY 4 meaningful metrics from the text (e.g., "Average Bulk Invoice Value", "Average Retail Invoice Value", "Volume Discount", etc.).

Question: "{question}"
Answer: "{answer}"

Return EXACTLY a valid JSON object in this format, using dynamic labels for whatever metrics you find:
{{
  "metric_1": {{"label": "Name of Metric 1", "value": "...", "subtext": "..."}},
  "metric_2": {{"label": "Name of Metric 2", "value": "...", "subtext": "..."}},
  "metric_3": {{"label": "Name of Metric 3", "value": "...", "subtext": "..."}},
  "metric_4": {{"label": "Name of Metric 4", "value": "...", "subtext": "..."}}
}}

If you cannot find 4 metrics, return null for the remaining metric fields (e.g., "metric_3": null).
"""
    messages = [{"role": "user", "content": prompt}]
    
    try:
        response = call_llm_chat(messages, temperature=0.0)
        if response.startswith("```json"):
            response = response.replace("```json", "").replace("```", "").strip()
        elif response.startswith("```"):
            response = response.replace("```", "").strip()
            
        metrics = json.loads(response)
        
        for key in defaults:
            if key not in metrics or not metrics[key] or metrics[key].get("value") is None:
                metrics[key] = defaults[key]
            else:
                if not metrics[key].get("label"): metrics[key]["label"] = defaults[key]["label"]
                if not metrics[key].get("subtext"): metrics[key]["subtext"] = ""
                
        return jsonify({"status": "success", "data": metrics}), 200
    except Exception as e:
        print(f"[Graph] Error extracting metrics: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def extract_graph_data_controller():
    """
    Extracts numerical data points from the chat context (question + answer)
    and formats them as structured chart data for frontend visualization.
    Expects JSON: { "question": "...", "answer": "..." }
    """
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
        
    question = data.get("question", "")
    answer = data.get("answer", "")
    
    prompt = f"""
You are a data visualization assistant.
Based on the user's question and the system's answer below, extract any numerical or sales data mentioned and format it as structured JSON.
You must categorize the extracted data into one of the following chart data formats based on what is being compared in the text:

1. "dummyYoYData": Use this if the data compares Year-over-Year sales (specifically 2025 vs 2026) for different regions or cities.
   Format of each item: {{ "name": "REGION_NAME", "y2026": float, "y2025": float }}
   
2. "dummyYearComparisonData": Use this if the data compares monthly sales across multiple years (2022 to 2026).
   Format of each item: {{ "month": "MONTH_NAME", "y2022": float, "y2023": float, "y2024": float, "y2025": float, "y2026": float }}
   
3. "dummyZoneData": Use this if the data shows sales across different geographic zones (e.g., Central, North, East, South, West).
   Format of each item: {{ "name": "ZONE_NAME", "value": float }}
   
4. "dummyTyreData": Use this if the data shows sales for different tyre categories (e.g., TRUCK, CAR, LCV, SCV, etc.).
   Format of each item: {{ "name": "VEHICLE _TYPE", "value": float }}

Important Instructions:
- Only populate the array that matches the data category discussed in the Q&A context. 
- For any of the 4 arrays that are NOT applicable or do not have any data mentioned in the text, return them as empty lists `[]`.
- Include the "COLORS" key exactly as shown below.
- Parse all numeric values as clean floats/integers.

Question: "{question}"
Answer: "{answer}"

Return EXACTLY a valid JSON object in this format, with no markdown formatting or extra text:
{{
  "dummyYoYData": [ ... ],
  "dummyYearComparisonData": [ ... ],
  "dummyZoneData": [ ... ],
  "COLORS": ["#0088FE", "#00C49F", "#FFBB28", "#FF8042", "#8884D8", "#E06666", "#93C47D"],
  "dummyTyreData": [ ... ]
}}
"""
    messages = [{"role": "user", "content": prompt}]
    
    try:
        response = call_llm_chat(messages, temperature=0.0)
        if response.startswith("```json"):
            response = response.replace("```json", "").replace("```", "").strip()
        elif response.startswith("```"):
            response = response.replace("```", "").strip()
            
        chart_data = json.loads(response)
        return jsonify({"status": "success", "data": chart_data}), 200
    except Exception as e:
        print(f"[Graph] Error extracting chart data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# 2. Schema Inspection & Query Context Resolver
# ─────────────────────────────────────────────────────────────────────────────

def get_actual_column_name(cursor, table_name, expected_name, is_hana=False):
    """Finds matching column name in table or view."""
    try:
        if is_hana:
            safe_tbl = table_name.replace('"', '')
            schema = safe_tbl.split(".")[0] if "." in safe_tbl else TARGET_SCHEMA
            tbl = safe_tbl.split(".")[1] if "." in safe_tbl else safe_tbl
            cursor.execute("""
                SELECT COLUMN_NAME FROM SYS.VIEW_COLUMNS WHERE SCHEMA_NAME=? AND VIEW_NAME=?
                UNION
                SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE SCHEMA_NAME=? AND TABLE_NAME=?
            """, (schema, tbl, schema, tbl))
            columns = [r[0] for r in cursor.fetchall()]
        else:
            cursor.execute(f"SHOW COLUMNS FROM {table_name}")
            rows = cursor.fetchall()
            columns = [row['Field'] if isinstance(row, dict) else row[0] for row in rows]
        
        candidates = expected_name if isinstance(expected_name, (list, tuple)) else [expected_name]
        
        for cand in candidates:
            # 1. Exact match
            for col in columns:
                if col == cand:
                    return f'"{col}"' if is_hana else f"`{col}`"
                    
            # 2. Case insensitive match
            for col in columns:
                if col.lower() == cand.lower():
                    return f'"{col}"' if is_hana else f"`{col}`"
                    
            # 3. Fuzzy match
            target = cand.lower().replace('_', '').replace(' ', '')
            for col in columns:
                if col.lower().replace('_', '').replace(' ', '') == target:
                    return f'"{col}"' if is_hana else f"`{col}`"
                
    except Exception as e:
        print(f"Error fetching columns for {table_name}: {e}")
        
    return None


def check_table_columns(cursor, table_name, required_columns, is_hana=False):
    found_cols = {}
    for col in required_columns:
        actual_col = get_actual_column_name(cursor, table_name, col, is_hana=is_hana)
        if not actual_col:
            return None
        found_cols[col] = actual_col
    return found_cols


def get_query_context_for_session(cursor, session_id, required_metrics, optional_dims, 
                                  is_hana=False, hana_schema=None, hana_table_columns=None, 
                                  mysql_conn=None, selected_years=None):
    """
    Builds the dynamic FROM and JOIN clauses and resolves column expressions
    for both SAP HANA (direct calculation views) and MySQL (synced tables).
    """
    table_columns = {}
    user_db = None

    if is_hana:
        raw_schema = hana_schema or TARGET_SCHEMA or "SAP_S4"
        if raw_schema and "#" in str(raw_schema):
            user_db = str(raw_schema).split("#")[0].strip() or "SAP_S4"
        else:
            user_db = str(raw_schema).strip() if raw_schema else "SAP_S4"
        table_columns = hana_table_columns or {}
        if not table_columns and mysql_conn:
            table_columns = get_sap_table_columns(mysql_conn, cursor, user_db, session_id)
    else:
        # Fetch MySQL tables synced for this session
        cursor.execute("""
            SELECT DISTINCT new_user_db, table_name 
            FROM external_db_sync_log 
            WHERE session_id=%s 
              AND new_user_db IS NOT NULL 
              AND table_name IS NOT NULL
        """, (session_id,))
        rows = cursor.fetchall()
        
        if not rows:
            cursor.execute("""
                SELECT DISTINCT new_user_db, table_name 
                FROM external_db_sync_log 
                WHERE new_user_db IS NOT NULL AND new_user_db != '' AND table_name IS NOT NULL
                ORDER BY id DESC LIMIT 10
            """)
            rows = cursor.fetchall()

        if not rows:
            return None, None, {}

        user_db = rows[0]["new_user_db"]
        synced_tables = [r["table_name"] for r in rows]
        
        try:
            cursor.execute(f"SHOW TABLES FROM `{user_db}`")
            db_tables = [list(r.values())[0] if isinstance(r, dict) else r[0] for r in cursor.fetchall()]
            all_tables = list(dict.fromkeys(synced_tables + db_tables))
        except Exception:
            all_tables = synced_tables

        for tbl in all_tables:
            try:
                cursor.execute(f"DESCRIBE `{user_db}`.`{tbl}`")
                cols = cursor.fetchall()
                table_columns[tbl] = {
                    (c["Field"] if isinstance(c, dict) else c[0]).lower().replace(' ', '').replace('_', ''): 
                    (c["Field"] if isinstance(c, dict) else c[0]) 
                    for c in cols
                }
            except Exception:
                pass

    if not table_columns:
        return None, None, {}

    def check_match(cols_dict, opts_list):
        if not cols_dict:
            return None
        for opt in opts_list:
            clean_opt = opt.lower().replace(' ', '').replace('_', '')
            if clean_opt in cols_dict:
                return cols_dict[clean_opt]
        return None

    def find_tbl(candidates):
        for cand in candidates:
            clean_cand = cand.lower().replace('_', '').replace('-', '')
            for tbl in table_columns.keys():
                clean_tbl = tbl.lower().replace('_', '').replace('-', '')
                if clean_cand in clean_tbl:
                    return tbl
        return None

    # Identify tables / SAP Calculation Views
    curr_sales_tbl = find_tbl(["zpvfsdcurrentsales", "currentsales", "current_sales"])
    hist_sales_tbl = find_tbl(["zpvfsdsaleshistory", "saleshistory", "sales_history"])
    sales_data_tbl = find_tbl(["salesdata", "sales_data"])
    cust_tbl = find_tbl(["zivfcustomermaster", "customermaster", "customer"])
    terr_tbl = find_tbl(["zivfterritorymaster", "territorymaster", "territory"])
    reg_tbl = find_tbl(["zivfregionmaster", "regionmaster", "region"])
    zone_tbl = find_tbl(["zivfzonemaster", "zonemaster", "zone"])
    sku_tbl = find_tbl(["zivrskumaster", "skumaster", "materialmaster", "material", "sku"])
    cat_tbl = find_tbl(["zivfcategorymaster", "categorymaster", "category"])
    tyre_tbl = find_tbl(["tyretypemaster", "tyretype"])
    canc_tbl = find_tbl(["zpvfsdcancelledsalesinvoice", "cancelledsalesinvoice", "cancelled_sales_invoice", "cancelled"])

    def q_name(n):
        return f'"{n}"' if is_hana else f"`{n}`"

    def q_tbl(t):
        if is_hana:
            # SAP S/4HANA CDS views always live in schema SAP_S4
            if any(str(t).lower().startswith(p) for p in ("zivf", "zivr", "zitr", "zpvf")):
                return f'"SAP_S4"."{t}"'
            return f'"{user_db}"."{t}"'
        return f"`{user_db}`.`{t}`"

    def q_col(alias, c):
        return f'{alias}."{c}"' if is_hana else f'{alias}.`{c}`'

    can_filter = ""
    can_null = ""
    # In SAP HANA, do not join ZPVF_SD_CancelledSalesInvoice_01_TR on large datasets
    # because fact views already have indicator_cancel column and a multi-column join across 30M+ rows
    # exhausts HANA transient memory (GLOBAL_ALLOCATION_LIMIT).
    if canc_tbl and not is_hana:
        s_inv = check_match(table_columns.get(curr_sales_tbl or hist_sales_tbl or sales_data_tbl, {}), ["invoice_number", "invoicenumber", "vbeln"]) or "invoice_number"
        s_plant = check_match(table_columns.get(curr_sales_tbl or hist_sales_tbl or sales_data_tbl, {}), ["plant", "werks"]) or "plant"
        s_fkart = check_match(table_columns.get(curr_sales_tbl or hist_sales_tbl or sales_data_tbl, {}), ["fkart", "billing_type"]) or "fkart"
        
        c_inv = check_match(table_columns.get(canc_tbl, {}), ["invoice_number", "invoicenumber", "vbeln"]) or "invoice_number"
        c_plant = check_match(table_columns.get(canc_tbl, {}), ["plant", "werks"]) or "plant"
        c_fkart = check_match(table_columns.get(canc_tbl, {}), ["fkart", "billing_type"]) or "fkart"
        c_dist = check_match(table_columns.get(canc_tbl, {}), ["distribution_channel", "vtweg"]) or "distribution_channel"

        can_filter = f"LEFT JOIN {q_tbl(canc_tbl)} can ON s.{q_name(s_inv)} = can.{q_name(c_inv)} AND s.{q_name(s_plant)} = can.{q_name(c_plant)} AND s.{q_name(s_fkart)} = can.{q_name(c_fkart)}"
        can_null = f"AND can.{q_name(c_inv)} IS NULL"

    fact_from = None
    fact_cols = {}

    def build_aligned_union(curr_t, hist_t, c_curr_expr, c_hist_expr):
        curr_map = table_columns.get(curr_t, {})
        hist_map = table_columns.get(hist_t, {})
        common_cols = []
        for clean_k, c_n in curr_map.items():
            if clean_k in hist_map:
                common_cols.append((c_n, hist_map[clean_k]))
        if not common_cols:
            return f"(SELECT * FROM {q_tbl(curr_t)} {c_curr_expr} UNION ALL SELECT * FROM {q_tbl(hist_t)} {c_hist_expr})"
        c_proj = ", ".join([f'c.{q_name(c[0])} AS {q_name(c[0])}' for c in common_cols])
        h_proj = ", ".join([f'h.{q_name(c[1])} AS {q_name(c[0])}' for c in common_cols])
        return f"(SELECT {c_proj} FROM {q_tbl(curr_t)} c {c_curr_expr} UNION ALL SELECT {h_proj} FROM {q_tbl(hist_t)} h {c_hist_expr})"

    # Build cancellation & distribution channel conditions on fact tables
    cancel_expr_curr = ""
    cancel_expr_hist = ""
    if curr_sales_tbl:
        c_curr = check_match(table_columns[curr_sales_tbl], ["indicator_cancel", "cancel", "fksto"])
        d_curr = check_match(table_columns[curr_sales_tbl], ["distribution_channel", "vtweg"])
        curr_conds = []
        if c_curr: curr_conds.append(f"({q_name(c_curr)} IS NULL OR {q_name(c_curr)} NOT IN ('X', 'Y', '1'))")
        if d_curr:
            curr_conds.append(f"(TRIM(CAST({q_name(d_curr)} AS NVARCHAR(20))) IN ('10', '010', '1'))" if is_hana else f"({q_name(d_curr)} = 10 OR {q_name(d_curr)} = '10')")
        cancel_expr_curr = ("WHERE " + " AND ".join(curr_conds)) if curr_conds else ""

    if hist_sales_tbl:
        c_hist = check_match(table_columns[hist_sales_tbl], ["indicator_cancel", "cancel", "fksto"])
        d_hist = check_match(table_columns[hist_sales_tbl], ["distribution_channel", "vtweg"])
        hist_conds = []
        if c_hist: hist_conds.append(f"({q_name(c_hist)} IS NULL OR {q_name(c_hist)} NOT IN ('X', 'Y', '1'))")
        if d_hist:
            hist_conds.append(f"(TRIM(CAST({q_name(d_hist)} AS NVARCHAR(20))) IN ('10', '010', '1'))" if is_hana else f"({q_name(d_hist)} = 10 OR {q_name(d_hist)} = '10')")
        cancel_expr_hist = ("WHERE " + " AND ".join(hist_conds)) if hist_conds else ""

    # Parse years filter to pick the right table efficiently in SAP HANA
    valid_years = []
    if selected_years:
        for y in selected_years:
            if not y or str(y).strip().lower() == "all": continue
            try: valid_years.append(int(y))
            except ValueError: pass

    if is_hana and curr_sales_tbl and hist_sales_tbl:
        # In SAP HANA, ZPVF_SD_SalesHistory_01_TR has >30M rows.
        # Avoid unconstrained union if user selects current year (>=2026) or default dashboard view
        if valid_years and all(y >= 2026 for y in valid_years):
            fact_from = f"(SELECT s.* FROM {q_tbl(curr_sales_tbl)} s {can_filter} {cancel_expr_curr.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
            fact_cols = table_columns.get(curr_sales_tbl, {})
        elif valid_years and all(y <= 2025 for y in valid_years):
            fact_from = f"(SELECT s.* FROM {q_tbl(hist_sales_tbl)} s {can_filter} {cancel_expr_hist.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
            fact_cols = table_columns.get(hist_sales_tbl, {})
        elif not valid_years:
            # Default dashboard view: use current sales for fast, memory-safe load
            fact_from = f"(SELECT s.* FROM {q_tbl(curr_sales_tbl)} s {can_filter} {cancel_expr_curr.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
            fact_cols = table_columns.get(curr_sales_tbl, {})
        else:
            aligned_u = build_aligned_union(curr_sales_tbl, hist_sales_tbl, cancel_expr_curr, cancel_expr_hist)
            fact_from = f"(SELECT s.* FROM {aligned_u} s {can_filter} WHERE 1=1 {can_null}) s"
            fact_cols = table_columns.get(curr_sales_tbl) or table_columns.get(hist_sales_tbl) or {}
    elif curr_sales_tbl and hist_sales_tbl:
        aligned_u = build_aligned_union(curr_sales_tbl, hist_sales_tbl, cancel_expr_curr, cancel_expr_hist)
        fact_from = f"(SELECT s.* FROM {aligned_u} s {can_filter} WHERE 1=1 {can_null}) s"
        fact_cols = table_columns.get(curr_sales_tbl) or table_columns.get(hist_sales_tbl) or {}
    elif curr_sales_tbl:
        fact_from = f"(SELECT s.* FROM {q_tbl(curr_sales_tbl)} s {can_filter} {cancel_expr_curr.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
        fact_cols = table_columns.get(curr_sales_tbl, {})
    elif hist_sales_tbl:
        fact_from = f"(SELECT s.* FROM {q_tbl(hist_sales_tbl)} s {can_filter} {cancel_expr_hist.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
        fact_cols = table_columns.get(hist_sales_tbl, {})
    elif sales_data_tbl:
        fact_from = f"{q_tbl(sales_data_tbl)} s"
        fact_cols = table_columns.get(sales_data_tbl, {})
    else:
        req_keys = list(required_metrics.keys())
        first_req = req_keys[0] if req_keys else None
        found_fact = None
        if first_req:
            for tbl, cols in table_columns.items():
                if check_match(cols, required_metrics[first_req]):
                    found_fact = tbl
                    if any("invoice" in k for k in cols.keys()):
                        break
        if not found_fact and table_columns:
            found_fact = list(table_columns.keys())[0]
        if found_fact:
            fact_from = f"{q_tbl(found_fact)} s"
            fact_cols = table_columns.get(found_fact, {})

    if not fact_from or not fact_cols:
        return None, None, {}

    # Build Joins
    joins = []

    # 1. Customer Master `cm`
    if cust_tbl:
        s_cust = check_match(fact_cols, ["dealercode", "dealer_code", "customer", "kunnr"])
        c_cust = check_match(table_columns[cust_tbl], ["kunnr", "dealercode", "customer"])
        if s_cust and c_cust:
            joins.append(f"LEFT JOIN {q_tbl(cust_tbl)} cm ON s.{q_name(s_cust)} = cm.{q_name(c_cust)}")

    # 2. Territory Master `tm`
    if terr_tbl:
        s_plant = check_match(fact_cols, ["plant", "werks", "vwerk"])
        t_plant = check_match(table_columns[terr_tbl], ["werks", "plant"])
        if s_plant and t_plant:
            joins.append(f"LEFT JOIN {q_tbl(terr_tbl)} tm ON s.{q_name(s_plant)} = tm.{q_name(t_plant)}")
        elif cust_tbl:
            c_terr = check_match(table_columns[cust_tbl], ["bzirk", "territory", "territory_code"])
            t_terr = check_match(table_columns[terr_tbl], ["bzirk", "territory_code", "territory"])
            if c_terr and t_terr:
                joins.append(f"LEFT JOIN {q_tbl(terr_tbl)} tm ON cm.{q_name(c_terr)} = tm.{q_name(t_terr)}")

    # 3. Region Master `rm`
    if reg_tbl:
        if terr_tbl and any("tm" in j for j in joins):
            t_reg = check_match(table_columns[terr_tbl], ["regio", "region_code", "region", "area"])
            r_reg = check_match(table_columns[reg_tbl], ["region_code", "region", "regio"])
            if t_reg and r_reg:
                joins.append(f"LEFT JOIN {q_tbl(reg_tbl)} rm ON tm.{q_name(t_reg)} = rm.{q_name(r_reg)}")
        elif cust_tbl and any("cm" in j for j in joins):
            c_reg = check_match(table_columns[cust_tbl], ["regio", "region_code", "region"])
            r_reg = check_match(table_columns[reg_tbl], ["region_code", "region", "regio"])
            if c_reg and r_reg:
                joins.append(f"LEFT JOIN {q_tbl(reg_tbl)} rm ON cm.{q_name(c_reg)} = rm.{q_name(r_reg)}")

    # 4. Zone Master `zm`
    if zone_tbl:
        if terr_tbl and any("tm" in j for j in joins):
            t_zone = check_match(table_columns[terr_tbl], ["p_zone", "zone_code", "zone"])
            z_zone = check_match(table_columns[zone_tbl], ["p_zone", "zone_code", "zone"])
            if t_zone and z_zone:
                joins.append(f"LEFT JOIN {q_tbl(zone_tbl)} zm ON tm.{q_name(t_zone)} = zm.{q_name(z_zone)}")
        elif reg_tbl and any("rm" in j for j in joins):
            r_zone = check_match(table_columns[reg_tbl], ["zone_code", "zone", "p_zone"])
            z_zone = check_match(table_columns[zone_tbl], ["p_zone", "zone_code", "zone"])
            if r_zone and z_zone:
                joins.append(f"LEFT JOIN {q_tbl(zone_tbl)} zm ON rm.{q_name(r_zone)} = zm.{q_name(z_zone)}")
        elif cust_tbl and any("cm" in j for j in joins):
            c_zone = check_match(table_columns[cust_tbl], ["p_zone", "zone_code", "zone"])
            z_zone = check_match(table_columns[zone_tbl], ["p_zone", "zone_code", "zone"])
            if c_zone and z_zone:
                joins.append(f"LEFT JOIN {q_tbl(zone_tbl)} zm ON cm.{q_name(c_zone)} = zm.{q_name(z_zone)}")

    # 5. SKU Master `sm`
    if sku_tbl:
        s_mat = check_match(fact_cols, ["sku", "material", "matnr"])
        sm_mat = check_match(table_columns[sku_tbl], ["matnr", "sku", "material"])
        if s_mat and sm_mat:
            joins.append(f"LEFT JOIN {q_tbl(sku_tbl)} sm ON s.{q_name(s_mat)} = sm.{q_name(sm_mat)}")

    # 6. Category Master `catm`
    if cat_tbl and sku_tbl and any("sm" in j for j in joins):
        sm_cat = check_match(table_columns[sku_tbl], ["cat_code", "category", "catg", "prd_cat"])
        catm_cat = check_match(table_columns[cat_tbl], ["cat_code", "category_code", "category"])
        if sm_cat and catm_cat:
            joins.append(f"LEFT JOIN {q_tbl(cat_tbl)} catm ON sm.{q_name(sm_cat)} = catm.{q_name(catm_cat)}")

    # 7. Tyre Type Master `ttm`
    if tyre_tbl and sku_tbl and any("sm" in j for j in joins):
        sm_tyre = check_match(table_columns[sku_tbl], ["tyre_type", "tyretype"])
        ttm_tyre = check_match(table_columns[tyre_tbl], ["tyre_type_code", "tyre_type"])
        if sm_tyre and ttm_tyre:
            joins.append(f"LEFT JOIN {q_tbl(tyre_tbl)} ttm ON sm.{q_name(sm_tyre)} = ttm.{q_name(ttm_tyre)}")

    alias_map = [
        ("s", fact_cols),
        ("ttm", table_columns.get(tyre_tbl, {}) if tyre_tbl and any("ttm" in j for j in joins) else {}),
        ("catm", table_columns.get(cat_tbl, {}) if cat_tbl and any("catm" in j for j in joins) else {}),
        ("zm", table_columns.get(zone_tbl, {}) if zone_tbl and any("zm" in j for j in joins) else {}),
        ("rm", table_columns.get(reg_tbl, {}) if reg_tbl and any("rm" in j for j in joins) else {}),
        ("tm", table_columns.get(terr_tbl, {}) if terr_tbl and any("tm" in j for j in joins) else {}),
        ("sm", table_columns.get(sku_tbl, {}) if sku_tbl and any("sm" in j for j in joins) else {}),
        ("cm", table_columns.get(cust_tbl, {}) if cust_tbl and any("cm" in j for j in joins) else {}),
    ]

    resolved_cols = {}
    all_metrics = {**required_metrics, **optional_dims}

    for req_key, opts in all_metrics.items():
        found_expr = None
        for alias, cols in alias_map:
            if not cols:
                continue
            matched = check_match(cols, opts)
            if matched:
                found_expr = q_col(alias, matched)
                break
        resolved_cols[req_key] = found_expr

    for req_key in required_metrics.keys():
        if not resolved_cols.get(req_key):
            return None, None, {}

    from_clause = fact_from
    if joins:
        unique_joins = list(dict.fromkeys(joins))
        from_clause += " " + " ".join(unique_joins)

    return from_clause, user_db, resolved_cols


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tyre Sales Data Controller (Top 10 Tyre Types Chart)
# ─────────────────────────────────────────────────────────────────────────────

def tyre_sales_data_controller(get_db_connection):
    data = request.json or {}
    session_id = data.get("session_id")
    user_id = data.get("user_id")

    def make_list(val):
        if val is None:
            return []
        if isinstance(val, list):
            return [v for v in val if v]
        return [val] if val else []

    selected_years = make_list(data.get("selected_years") or data.get("years") or data.get("year"))
    selected_customer_categories = make_list(data.get("selected_customer_categories") or data.get("customer_categories") or data.get("customer_category") or data.get("selected_customer_types") or data.get("customer_types") or data.get("customer_type"))
    selected_zones = make_list(data.get("selected_zones") or data.get("zones") or data.get("Zone") or data.get("zone"))
    selected_regions = make_list(data.get("selected_regions") or data.get("regions") or data.get("Region") or data.get("region"))
    selected_constructions = make_list(data.get("selected_constructions") or data.get("construction_types") or data.get("construction_type") or data.get("constructions") or data.get("construction"))

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    hana_conn = None
    tunnel = None
    cursor = None
    chart_data = []

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500

        # Try live SAP HANA connection first
        hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, session_id, user_id=user_id)
        is_hana = bool(hana_conn)

        if is_hana:
            cursor = hana_conn.cursor()
            sap_table_cols = get_sap_table_columns(conn, hana_conn, sap_schema, session_id)
        else:
            try:
                cursor = conn.cursor(dictionary=True)
            except TypeError:
                cursor = conn.cursor()
            sap_schema = None
            sap_table_cols = None

        def has_valid_filters(filters):
            return any(f and str(f).strip().lower() != "all" for f in filters)

        required_metrics = {
            "vehicle_type": ["cat_desc", "category_name", "category", "tyre_type_description", "tyre_type_desc", "tyre_type_name", "vehicle_type", "tyre_type", "maktx"],
            "invoice_value": ["invoice_value", "ndp_value", "taxable_value", "revenue", "Invoice_Value_INR"]
        }
        optional_dims = {}
        if has_valid_filters(selected_years):
            optional_dims["invoice_date"] = ["invoice_date", "date", "billing__doc_date", "fkdat"]
        if has_valid_filters(selected_customer_categories):
            optional_dims["customer_category"] = ["ch_type", "ktokd", "customer_type", "customer_category", "cust_type", "type"]
        if has_valid_filters(selected_zones):
            optional_dims["zone"] = ["name", "p_zone", "Zone", "zone"]
        if has_valid_filters(selected_regions):
            optional_dims["region"] = ["region_name", "region_code", "Region", "region", "regio"]
        if has_valid_filters(selected_constructions):
            optional_dims["construction_type"] = ["constuction", "construction_type", "construction"]

        from_clause, user_db, cols = get_query_context_for_session(
            cursor, session_id, required_metrics, optional_dims,
            is_hana=is_hana, hana_schema=sap_schema, hana_table_columns=sap_table_cols,
            mysql_conn=conn, selected_years=selected_years
        )

        if not from_clause:
            return jsonify({"status": "success", "data": chart_data, "message": "Required metrics (vehicle_type, invoice_value) not found."}), 200

        if not is_hana:
            cursor.execute(f"USE `{user_db}`")
        
        actual_vehicle_type = cols["vehicle_type"]
        actual_invoice_value = cols["invoice_value"]
        actual_invoice_date = cols.get("invoice_date")
        actual_customer_category = cols.get("customer_category")
        actual_zone = cols.get("zone")
        actual_region = cols.get("region")
        actual_construction = cols.get("construction_type")

        revenue_expr = f"CAST(REPLACE(TRIM({actual_invoice_value}), ',', '') AS DECIMAL(18,2))"
        where_clauses = [f"{actual_vehicle_type} IS NOT NULL", f"{actual_vehicle_type} <> ''"]
        params = []

        # Years filter
        if actual_invoice_date and selected_years:
            valid_years = []
            for y in selected_years:
                if str(y).strip().lower() == "all": continue
                try: valid_years.append(int(y))
                except ValueError: pass
            if valid_years:
                placeholders = ",".join(["%s"] * len(valid_years))
                where_clauses.append(f"YEAR({actual_invoice_date}) IN ({placeholders})")
                params.extend(valid_years)

        # Customer Category Filter
        if actual_customer_category and selected_customer_categories:
            valid_categories = [str(c).strip() for c in selected_customer_categories if c and str(c).strip().lower() != "all"]
            if valid_categories:
                placeholders = ",".join(["%s"] * len(valid_categories))
                where_clauses.append(f"{actual_customer_category} IN ({placeholders})")
                params.extend(valid_categories)

        # Zone Filter
        if actual_zone and selected_zones:
            valid_zones = [str(z).strip() for z in selected_zones if z and str(z).strip().lower() != "all"]
            if valid_zones:
                placeholders = ",".join(["%s"] * len(valid_zones))
                where_clauses.append(f"{actual_zone} IN ({placeholders})")
                params.extend(valid_zones)

        # Region Filter
        if actual_region and selected_regions:
            valid_regions = [str(r).strip() for r in selected_regions if r and str(r).strip().lower() != "all"]
            if valid_regions:
                placeholders = ",".join(["%s"] * len(valid_regions))
                where_clauses.append(f"{actual_region} IN ({placeholders})")
                params.extend(valid_regions)

        # Construction Filter
        if actual_construction and selected_constructions:
            label_to_id = {
                "bias": "1", "radial": "2", "tubeless bias": "3", "tubeless radial": "4",
                "radial (tube type)": "5", "radial (tubeless)": "6", "solid / otr": "7"
            }
            valid_constructions = []
            for c in selected_constructions:
                if not c or str(c).strip().lower() == "all": continue
                s_c = str(c).strip()
                valid_constructions.append(label_to_id.get(s_c.lower(), s_c))
            if valid_constructions:
                placeholders = ",".join(["%s"] * len(valid_constructions))
                where_clauses.append(f"{actual_construction} IN ({placeholders})")
                params.extend(valid_constructions)

        where_sql = " AND ".join(where_clauses)
        error_msg = ""

        try:
            query = f"""
                SELECT
                    {actual_vehicle_type} as category,
                    ROUND(SUM({revenue_expr}), 2) AS sales_value
                FROM {from_clause}
                WHERE {where_sql}
                GROUP BY {actual_vehicle_type}
                ORDER BY sales_value DESC
                LIMIT 10
            """
            execute_query(cursor, query, params, is_hana=is_hana)
            chart_data = fetchall_dict(cursor)
            for r in chart_data:
                if "category" in r and "vehicle_type" not in r:
                    r["vehicle_type"] = r["category"]
                elif "vehicle_type" in r and "category" not in r:
                    r["category"] = r["vehicle_type"]
                r["sales_value"] = float(r.get("sales_value") or 0.0)
        except Exception as e:
            print(f"[Tyre Sales Data] Execution error: {e}")
            if is_hana and ("cannot allocate enough memory" in str(e).lower() or "allocation failed" in str(e).lower()):
                try:
                    print("[Tyre Sales Data] Retrying with lightweight current sales view due to HANA memory limit...")
                    lean_from = f"(SELECT * FROM \"SAP_S4\".\"ZPVF_SD_CurrentSales_01_TR\" WHERE (distribution_channel=10 OR distribution_channel='10') AND (indicator_cancel IS NULL OR indicator_cancel NOT IN ('X', 'Y', '1'))) s"
                    retry_query = re.sub(r'FROM\s+\(.*?\)\s+s', f'FROM {lean_from}', query, count=1, flags=re.DOTALL)
                    execute_query(cursor, retry_query, params, is_hana=is_hana)
                    chart_data = fetchall_dict(cursor)
                    for r in chart_data:
                        if "category" in r and "vehicle_type" not in r:
                            r["vehicle_type"] = r["category"]
                        elif "vehicle_type" in r and "category" not in r:
                            r["category"] = r["vehicle_type"]
                        r["sales_value"] = float(r.get("sales_value") or 0.0)
                    error_msg = ""
                except Exception as retry_e:
                    error_msg = str(e)
            else:
                error_msg = str(e)

        visualizations = []
        if chart_data:
            visualizations.append({
                "data": chart_data,
                "seriesKey": "",
                "title": "Top 10 Tyre Types by Sales",
                "type": "horizontal_bar_chart",
                "xKey": "sales_value",
                "yKey": "vehicle_type"
            })

        return jsonify({
            "status": "success" if not error_msg else "error",
            "visualizations": visualizations,
            "data": chart_data,
            "message": "Data retrieved successfully." if not error_msg else "",
            "sql_error": error_msg
        }), 200 if not error_msg else 400

    except Exception as exc:
        print(f"[Tyre Sales Data] Database query failed: {exc}")
        return jsonify({"status": "error", "message": "Database query failed.", "details": str(exc)}), 500

    finally:
        if cursor and is_hana:
            try: cursor.close()
            except Exception: pass
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass
        if cursor and not is_hana:
            try: cursor.close()
            except Exception: pass
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 4. Sales by Zone Controller (Pie Chart)
# ─────────────────────────────────────────────────────────────────────────────

def sales_by_zone_data_controller(get_db_connection):
    data = request.json or {}
    session_id = data.get("session_id")
    user_id = data.get("user_id")

    product_type = data.get("product_type") or data.get("category") or data.get("product_category")
    construction_type = data.get("construction_type") or data.get("construction")
    vehicle_type = data.get("vehicle_type") or data.get("tyre_type") or data.get("tyreType") or data.get("tyre_types")

    def make_list(val):
        if val is None:
            return []
        if isinstance(val, list):
            return [v for v in val if v]
        return [val] if val else []

    selected_years = make_list(data.get("selected_years") or data.get("years") or data.get("year"))
    selected_months = make_list(data.get("selected_months") or data.get("months") or data.get("month"))
    selected_zones = make_list(data.get("selected_zones") or data.get("zones") or data.get("zone") or data.get("Zone"))

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    hana_conn = None
    tunnel = None
    cursor = None
    chart_data = []

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500

        hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, session_id, user_id=user_id)
        is_hana = bool(hana_conn)

        if is_hana:
            cursor = hana_conn.cursor()
            sap_table_cols = get_sap_table_columns(conn, hana_conn, sap_schema, session_id)
        else:
            try:
                cursor = conn.cursor(dictionary=True)
            except TypeError:
                cursor = conn.cursor()
            sap_schema = None
            sap_table_cols = None

        def has_valid_filters(filters):
            return any(f and str(f).strip().lower() != "all" for f in filters)

        required_metrics = {
            "zone": ["name", "p_zone", "Zone", "zone"],
            "invoice_value": ["invoice_value", "ndp_value", "Invoice_Value", "taxable_value", "Invoice_Value_INR"]
        }
        optional_dims = {}
        if has_valid_filters(selected_years) or has_valid_filters(selected_months):
            optional_dims["invoice_date"] = ["invoice_date", "date", "billing__doc_date", "fkdat"]
        if product_type and str(product_type).lower() != 'all':
            optional_dims["customer_category"] = ["cat_code", "cat_desc", "category", "product_category", "catg", "prd_cat"]
        if vehicle_type and str(vehicle_type).lower() != 'all':
            optional_dims["vehicle_type"] = ["cat_code", "cat_desc", "tyre_type", "vehicle_type", "tyre_type_name", "maktx"]
        if construction_type and str(construction_type).lower() != 'all':
            optional_dims["construction_type"] = ["constuction", "CONSTRUCTION", "construction_type", "construction"]

        from_clause, user_db, cols = get_query_context_for_session(
            cursor, session_id, required_metrics, optional_dims,
            is_hana=is_hana, hana_schema=sap_schema, hana_table_columns=sap_table_cols,
            mysql_conn=conn, selected_years=selected_years
        )

        if not from_clause:
            return jsonify({"status": "success", "data": chart_data, "message": "Required metrics (zone, invoice_value) not found."}), 200

        if not is_hana:
            cursor.execute(f"USE `{user_db}`")

        actual_zone = cols["zone"]
        actual_invoice_value = cols["invoice_value"]
        revenue_expr = f"CAST(REPLACE(TRIM({actual_invoice_value}), ',', '') AS DECIMAL(18,2))"

        actual_invoice_date = cols.get("invoice_date")
        actual_cat = cols.get("customer_category")
        actual_const = cols.get("construction_type")
        actual_tyre = cols.get("vehicle_type")

        where_clauses = [f"{actual_zone} IS NOT NULL", f"{actual_zone} <> ''"]
        if "cm" in from_clause:
            klabc_col = 'cm."KLABC"' if is_hana else 'cm.`klabc`'
            where_clauses.append(f"({klabc_col} IS NULL OR UPPER(TRIM({klabc_col})) <> 'X')")
        params = []

        if product_type and str(product_type).lower() != 'all' and actual_cat:
            if "catm" in from_clause:
                desc_col = 'catm."CAT_DESC"' if is_hana else 'catm.`cat_desc`'
                code_col = 'catm."CAT_CODE"' if is_hana else 'catm.`cat_code`'
                where_clauses.append(f"({actual_cat} = %s OR {desc_col} = %s OR {code_col} = %s)")
                params.extend([product_type, product_type, product_type])
            else:
                where_clauses.append(f"{actual_cat} = %s")
                params.append(product_type)

        if construction_type and str(construction_type).lower() != 'all' and actual_const:
            label_to_id = {
                "bias": "1", "radial": "2", "tubeless bias": "3", "tubeless radial": "4",
                "radial (tube type)": "5", "radial (tubeless)": "6", "solid / otr": "7"
            }
            c_val = str(construction_type).strip()
            c_val = label_to_id.get(c_val.lower(), c_val)
            where_clauses.append(f"{actual_const} = %s")
            params.append(c_val)

        if vehicle_type and str(vehicle_type).lower() != 'all' and actual_tyre:
            if "catm" in from_clause:
                desc_col = 'catm."CAT_DESC"' if is_hana else 'catm.`cat_desc`'
                code_col = 'catm."CAT_CODE"' if is_hana else 'catm.`cat_code`'
                where_clauses.append(f"({actual_tyre} = %s OR {desc_col} = %s OR {code_col} = %s)")
                params.extend([vehicle_type, vehicle_type, vehicle_type])
            else:
                where_clauses.append(f"{actual_tyre} = %s")
                params.append(vehicle_type)

        # Years filter
        if actual_invoice_date and selected_years:
            valid_years = []
            for y in selected_years:
                if str(y).strip().lower() == "all": continue
                try: valid_years.append(int(y))
                except ValueError: pass
            if valid_years:
                placeholders = ",".join(["%s"] * len(valid_years))
                where_clauses.append(f"YEAR({actual_invoice_date}) IN ({placeholders})")
                params.extend(valid_years)

        # Months filter
        if actual_invoice_date and selected_months:
            month_to_num = {calendar.month_name[i].lower(): i for i in range(1, 13)}
            valid_month_nums = []
            for m in selected_months:
                if not m or str(m).strip().lower() == "all": continue
                sm = str(m).strip().lower()
                if sm in month_to_num: valid_month_nums.append(month_to_num[sm])
                elif sm.isdigit(): valid_month_nums.append(int(sm))
            if valid_month_nums:
                placeholders = ",".join(["%s"] * len(valid_month_nums))
                where_clauses.append(f"MONTH({actual_invoice_date}) IN ({placeholders})")
                params.extend(valid_month_nums)

        # Zones filter
        if actual_zone and selected_zones:
            valid_zones = [str(z).strip() for z in selected_zones if z and str(z).strip().lower() != "all"]
            if valid_zones:
                placeholders = ",".join(["%s"] * len(valid_zones))
                where_clauses.append(f"{actual_zone} IN ({placeholders})")
                params.extend(valid_zones)

        where_sql = " AND ".join(where_clauses)
        error_msg = ""

        try:
            query = f"""
                SELECT
                    {actual_zone} as zone,
                    ROUND(SUM({revenue_expr}), 2) AS sales_value
                FROM {from_clause}
                WHERE {where_sql}
                GROUP BY {actual_zone}
                ORDER BY sales_value DESC
            """
            execute_query(cursor, query, params, is_hana=is_hana)
            results = fetchall_dict(cursor)

            total_sales = sum(float(r.get('sales_value') or 0.0) for r in results)
            zone_map = {
                'WZ': 'West Zone', 'EZ': 'East Zone', 'NZ': 'North Zone',
                'SZ': 'South Zone I', 'TZ': 'South Zone II', 'CZ': 'Central Zone', 'NP': 'Nepal'
            }

            for r in results:
                raw_zone = str(r.get('zone') or '').strip()
                r['name'] = zone_map.get(raw_zone.upper(), raw_zone)
                r['value'] = float(r.get('sales_value') or 0.0)
                r['percentage'] = round((r['value'] / total_sales) * 100, 1) if total_sales > 0 else 0.0

            chart_data = results
        except Exception as e:
            print(f"[Sales by Zone] Execution error: {e}")
            if is_hana and ("cannot allocate enough memory" in str(e).lower() or "allocation failed" in str(e).lower()):
                try:
                    print("[Sales by Zone] Retrying with lightweight current sales view due to HANA memory limit...")
                    lean_from = f"(SELECT * FROM \"SAP_S4\".\"ZPVF_SD_CurrentSales_01_TR\" WHERE (distribution_channel=10 OR distribution_channel='10') AND (indicator_cancel IS NULL OR indicator_cancel NOT IN ('X', 'Y', '1'))) s"
                    retry_query = re.sub(r'FROM\s+\(.*?\)\s+s', f'FROM {lean_from}', query, count=1, flags=re.DOTALL)
                    execute_query(cursor, retry_query, params, is_hana=is_hana)
                    results = fetchall_dict(cursor)
                    total_sales = sum(float(r.get('sales_value') or 0.0) for r in results)
                    zone_map = {
                        'WZ': 'West Zone', 'EZ': 'East Zone', 'NZ': 'North Zone',
                        'SZ': 'South Zone I', 'TZ': 'South Zone II', 'CZ': 'Central Zone', 'NP': 'Nepal'
                    }
                    for r in results:
                        raw_zone = str(r.get('zone') or '').strip()
                        r['name'] = zone_map.get(raw_zone.upper(), raw_zone)
                        r['value'] = float(r.get('sales_value') or 0.0)
                        r['percentage'] = round((r['value'] / total_sales) * 100, 1) if total_sales > 0 else 0.0
                    chart_data = results
                    error_msg = ""
                except Exception as retry_e:
                    error_msg = str(e)
            else:
                error_msg = str(e)

        visualizations = []
        if chart_data:
            visualizations.append({
                "data": chart_data,
                "seriesKey": "",
                "title": "Sales by Zone",
                "type": "pie_chart",
                "xKey": "name",
                "yKey": "value"
            })

        return jsonify({
            "status": "success" if not error_msg else "error",
            "visualizations": visualizations,
            "data": chart_data,
            "message": "Data retrieved successfully." if not error_msg else "",
            "sql_error": error_msg
        }), 200 if not error_msg else 400

    except Exception as exc:
        print(f"[Sales by Zone] Database query failed: {exc}")
        return jsonify({"status": "error", "message": "Database query failed.", "details": str(exc)}), 500

    finally:
        if cursor and is_hana:
            try: cursor.close()
            except Exception: pass
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass
        if cursor and not is_hana:
            try: cursor.close()
            except Exception: pass
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. Year-Wise Sales Comparison Controller (Bar Chart)
# ─────────────────────────────────────────────────────────────────────────────

def year_wise_sales_comparison_controller(get_db_connection):
    """
    Fetches Year-wise Sales Comparison data grouped by month and year.
    Returns:
    {
      "seriesKey": "year",
      "title": "Year-wise Sales Comparison",
      "type": "bar_chart",
      "xKey": "month",
      "yKey": "sales_value",
      "visualization": [ { "month": "January", "sales_value": ..., "year": "2026" }, ... ]
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id", "")
    user_id = data.get("user_id")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    def make_list(val):
        if val is None:
            return []
        if isinstance(val, list):
            return [v for v in val if v]
        return [val] if val else []

    selected_years = make_list(data.get("selected_years") or data.get("years") or data.get("year"))
    selected_zones = make_list(data.get("selected_zones") or data.get("zones") or data.get("Zone") or data.get("zone"))
    selected_regions = make_list(data.get("selected_regions") or data.get("regions") or data.get("Region") or data.get("region"))
    selected_months = make_list(data.get("selected_months") or data.get("months") or data.get("Month") or data.get("month"))
    selected_customer_types = make_list(data.get("selected_customer_types") or data.get("customer_types") or data.get("customer_type"))

    conn = None
    hana_conn = None
    tunnel = None
    cursor = None
    visualization_data = []

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500

        hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, session_id, user_id=user_id)
        is_hana = bool(hana_conn)

        if is_hana:
            cursor = hana_conn.cursor()
            sap_table_cols = get_sap_table_columns(conn, hana_conn, sap_schema, session_id)
        else:
            try:
                cursor = conn.cursor(dictionary=True)
            except TypeError:
                cursor = conn.cursor()
            sap_schema = None
            sap_table_cols = None

        def has_valid_filters(filters):
            return any(f and str(f).strip().lower() != "all" for f in filters)

        required_metrics = {
            "invoice_value": ["invoice_value", "ndp_value", "Invoice_Value", "taxable_value", "Invoice_Value_INR"],
            "invoice_date": ["invoice_date", "date", "billing__doc_date", "fkdat"]
        }
        optional_dims = {}
        if has_valid_filters(selected_zones):
            optional_dims["zone"] = ["name", "p_zone", "Zone", "zone"]
        if has_valid_filters(selected_regions):
            optional_dims["region"] = ["region_name", "region_code", "Region", "region", "regio"]
        if has_valid_filters(selected_customer_types):
            optional_dims["customer_type"] = ["ch_type", "ktokd", "customer_type", "customer_category", "cust_type", "type"]

        from_clause, user_db, cols = get_query_context_for_session(
            cursor, session_id, required_metrics, optional_dims,
            is_hana=is_hana, hana_schema=sap_schema, hana_table_columns=sap_table_cols,
            mysql_conn=conn, selected_years=selected_years
        )

        if not from_clause:
            return jsonify({
                "seriesKey": "year",
                "title": "Year-wise Sales Comparison",
                "type": "bar_chart",
                "xKey": "month",
                "yKey": "sales_value",
                "visualization": []
            }), 200

        if not is_hana:
            cursor.execute(f"USE `{user_db}`")

        actual_invoice_date_expr = cols["invoice_date"]
        actual_invoice_value_expr = cols["invoice_value"]
        actual_zone = cols.get("zone")
        actual_region = cols.get("region")
        actual_customer_type_expr = cols.get("customer_type")

        revenue_expr = f"CAST(REPLACE(TRIM({actual_invoice_value_expr}), ',', '') AS DECIMAL(18,2))"
        where_clauses = [f"{actual_invoice_date_expr} IS NOT NULL"]
        params = []

        # Years filter
        if selected_years:
            valid_years = []
            for y in selected_years:
                if str(y).strip().lower() == "all": continue
                try: valid_years.append(int(y))
                except ValueError: pass
            if valid_years:
                placeholders = ",".join(["%s"] * len(valid_years))
                where_clauses.append(f"YEAR({actual_invoice_date_expr}) IN ({placeholders})")
                params.extend(valid_years)

        # Months filter
        if selected_months:
            month_to_num = {calendar.month_name[i].lower(): i for i in range(1, 13)}
            valid_month_nums = []
            for m in selected_months:
                if not m or str(m).strip().lower() == "all": continue
                sm = str(m).strip().lower()
                if sm in month_to_num: valid_month_nums.append(month_to_num[sm])
                elif sm.isdigit(): valid_month_nums.append(int(sm))
            if valid_month_nums:
                placeholders = ",".join(["%s"] * len(valid_month_nums))
                where_clauses.append(f"MONTH({actual_invoice_date_expr}) IN ({placeholders})")
                params.extend(valid_month_nums)

        # Zone Filter
        if actual_zone and selected_zones:
            valid_zones = [str(z).strip() for z in selected_zones if z and str(z).strip().lower() != "all"]
            if valid_zones:
                placeholders = ",".join(["%s"] * len(valid_zones))
                where_clauses.append(f"{actual_zone} IN ({placeholders})")
                params.extend(valid_zones)

        # Region Filter
        if actual_region and selected_regions:
            valid_regions = [str(r).strip() for r in selected_regions if r and str(r).strip().lower() != "all"]
            if valid_regions:
                placeholders = ",".join(["%s"] * len(valid_regions))
                where_clauses.append(f"{actual_region} IN ({placeholders})")
                params.extend(valid_regions)

        # Customer Type Filter
        if actual_customer_type_expr and selected_customer_types:
            valid_types = [str(c).strip().upper() for c in selected_customer_types if c and str(c).strip().lower() != "all"]
            if valid_types:
                placeholders = ",".join(["%s"] * len(valid_types))
                where_clauses.append(f"UPPER({actual_customer_type_expr}) IN ({placeholders})")
                params.extend(valid_types)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        month_expr = f"MONTH({actual_invoice_date_expr})"
        year_expr = f"YEAR({actual_invoice_date_expr})"

        query = f"""
            SELECT 
                {month_expr} AS month_num,
                {year_expr} AS year,
                ROUND(SUM({revenue_expr}), 2) AS total_sales
            FROM {from_clause}
            {where_sql}
            GROUP BY {year_expr}, {month_expr}
            ORDER BY year ASC, month_num ASC
        """

        execute_query(cursor, query, params, is_hana=is_hana)
        results = fetchall_dict(cursor)

        for row in results:
            m_num = int(row.get("month_num") or 1)
            m_name = calendar.month_name[m_num] if (1 <= m_num <= 12) else str(row.get("month_name") or "")
            visualization_data.append({
                "month": m_name,
                "sales_value": float(row.get("total_sales") or 0.0),
                "year": str(row.get("year"))
            })

        return jsonify({
            "seriesKey": "year",
            "title": "Year-wise Sales Comparison",
            "type": "bar_chart",
            "xKey": "month",
            "yKey": "sales_value",
            "visualization": visualization_data
        }), 200

    except Exception as exc:
        print(f"[Year-wise Sales Comparison] Database query failed: {exc}")
        return jsonify({"status": "error", "message": "Database query failed.", "details": str(exc)}), 500

    finally:
        if cursor and is_hana:
            try: cursor.close()
            except Exception: pass
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass
        if cursor and not is_hana:
            try: cursor.close()
            except Exception: pass
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 6. Available Years & Filters Metadata Controller
# ─────────────────────────────────────────────────────────────────────────────

def available_years_controller(get_db_connection):
    """
    Fetches distinct years, zones, regions, months, customer types, and construction types
    available for the active session (from SAP HANA views or MySQL synced tables).
    """
    session_id = request.args.get("session_id", "").strip()
    user_id = request.args.get("user_id")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    years = []
    zones = []
    regions = []
    months = []
    customer_types = []
    construction_types = []

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500

        hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, session_id, user_id=user_id)
        is_hana = bool(hana_conn)

        if is_hana:
            cursor = hana_conn.cursor()
            table_columns = get_sap_table_columns(conn, hana_conn, sap_schema, session_id)
            user_db = sap_schema
            all_tables = list(table_columns.keys())
        else:
            try:
                cursor = conn.cursor(dictionary=True)
            except TypeError:
                cursor = conn.cursor()

            cursor.execute("""
                SELECT new_user_db 
                FROM external_db_sync_log 
                WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            sync_row = cursor.fetchone()

            if not sync_row:
                cursor.execute("""
                    SELECT new_user_db 
                    FROM external_db_sync_log 
                    WHERE new_user_db IS NOT NULL AND new_user_db != ''
                    ORDER BY id DESC LIMIT 1
                """)
                sync_row = cursor.fetchone()

            if not sync_row:
                return jsonify({
                    "status": "success", "years": [], "zones": [], "regions": [],
                    "months": [], "customer_types": [], "construction_types": []
                }), 200

            user_db = sync_row["new_user_db"]
            cursor.execute(f"USE `{user_db}`")
            cursor.execute("SHOW TABLES")
            all_tables = [list(r.values())[0] if isinstance(r, dict) else r[0] for r in cursor.fetchall()]

        # Helper to format qualified table name
        def q_tbl(t): return f'"{user_db}"."{t}"' if is_hana else f"`{user_db}`.`{t}`"

        all_years = set()
        month_set = set()

        # 1. Fetch Years and Months from sales views / tables
        for tbl in all_tables:
            tbl_lower = tbl.lower()
            if any(k in tbl_lower for k in ["dayattribute", "calendar", "target", "budget", "plan"]):
                continue
            if not any(k in tbl_lower for k in ["sales", "currentsales", "saleshistory", "invoice"]):
                continue

            t_name = q_tbl(tbl)
            date_col = get_actual_column_name(cursor, t_name, ["invoice_date", "date", "billing__doc_date", "fkdat"], is_hana=is_hana)
            if date_col:
                try:
                    q_yr = f"SELECT DISTINCT YEAR({date_col}) AS yr FROM {t_name} WHERE {date_col} IS NOT NULL"
                    execute_query(cursor, q_yr, is_hana=is_hana)
                    for r in fetchall_dict(cursor):
                        if r.get("yr") and 2000 <= int(r["yr"]) <= 2100:
                            all_years.add(int(r["yr"]))

                    q_m = f"SELECT DISTINCT MONTH({date_col}) AS m_num FROM {t_name} WHERE {date_col} IS NOT NULL"
                    execute_query(cursor, q_m, is_hana=is_hana)
                    for r in fetchall_dict(cursor):
                        if r.get("m_num"):
                            m_idx = int(r["m_num"])
                            if 1 <= m_idx <= 12:
                                month_set.add(m_idx)
                except Exception as e:
                    print(f"[Available Years] Date query error for {tbl}: {e}")

        if all_years:
            years = sorted(list(all_years))
        if month_set:
            months = [calendar.month_name[m] for m in sorted(list(month_set))]

        # 2. Fetch Zones
        for tbl in all_tables:
            t_name = q_tbl(tbl)
            actual_zone = get_actual_column_name(cursor, t_name, ["name", "p_zone", "zone", "zone_name"], is_hana=is_hana)
            if actual_zone and any(z in tbl.lower() for z in ["zone", "customermaster", "customer"]):
                try:
                    q_z = f"SELECT DISTINCT {actual_zone} as val FROM {t_name} WHERE {actual_zone} IS NOT NULL AND {actual_zone} <> '' ORDER BY val ASC"
                    execute_query(cursor, q_z, is_hana=is_hana)
                    zones = [r["val"] for r in fetchall_dict(cursor) if r.get("val")]
                    if zones: break
                except Exception as e:
                    print(f"[Available Years] Zone error: {e}")

        # 3. Fetch Regions (Prioritize RegionMaster and text names over numeric codes)
        sorted_region_tables = sorted(all_tables, key=lambda t: 0 if "regionmaster" in t.lower() else (1 if "region" in t.lower() else 2))
        for tbl in sorted_region_tables:
            t_name = q_tbl(tbl)
            actual_region = get_actual_column_name(cursor, t_name, ["region_name", "region", "name", "region_code", "regio"], is_hana=is_hana)
            if actual_region and any(r in tbl.lower() for r in ["region", "territory"]):
                try:
                    q_r = f"SELECT DISTINCT {actual_region} as val FROM {t_name} WHERE {actual_region} IS NOT NULL AND {actual_region} <> '' ORDER BY val ASC"
                    execute_query(cursor, q_r, is_hana=is_hana)
                    fetched_regs = [r["val"] for r in fetchall_dict(cursor) if r.get("val")]
                    has_alpha = any(re.search(r'[a-zA-Z]', str(x)) for x in fetched_regs)
                    if fetched_regs and (has_alpha or not regions):
                        regions = fetched_regs
                        if has_alpha:
                            break
                except Exception as e:
                    print(f"[Available Years] Region error: {e}")

        # 4. Fetch Customer Types
        for tbl in all_tables:
            t_name = q_tbl(tbl)
            actual_customer_type = get_actual_column_name(cursor, t_name, ["ch_type", "ktokd", "customer_type", "customer_category", "cust_type", "type"], is_hana=is_hana)
            if actual_customer_type and any(c in tbl.lower() for c in ["customer", "customermaster"]):
                try:
                    q_c = f"SELECT DISTINCT {actual_customer_type} as val FROM {t_name} WHERE {actual_customer_type} IS NOT NULL AND {actual_customer_type} <> '' ORDER BY val ASC"
                    execute_query(cursor, q_c, is_hana=is_hana)
                    customer_types = [r["val"] for r in fetchall_dict(cursor) if r.get("val")]
                    if customer_types: break
                except Exception as e:
                    print(f"[Available Years] Customer types error: {e}")

        # 5. Fetch Construction Types
        construction_labels = {
            1: "Bias", 2: "Radial", 3: "Tubeless Bias", 4: "Tubeless Radial",
            5: "Radial (Tube Type)", 6: "Radial (Tubeless)", 7: "Solid / OTR"
        }
        for tbl in all_tables:
            t_name = q_tbl(tbl)
            actual_construction_type = get_actual_column_name(cursor, t_name, ["constuction", "construction_type", "construction"], is_hana=is_hana)
            if actual_construction_type and any(s in tbl.lower() for s in ["sku", "skumaster", "material", "construction"]):
                try:
                    q_con = f"SELECT DISTINCT {actual_construction_type} as val FROM {t_name} WHERE {actual_construction_type} IS NOT NULL AND {actual_construction_type} <> '' ORDER BY val ASC"
                    execute_query(cursor, q_con, is_hana=is_hana)
                    for r in fetchall_dict(cursor):
                        raw_val = r["val"]
                        try:
                            val_int = int(raw_val)
                            lbl = construction_labels.get(val_int, f"Type {raw_val}")
                        except (ValueError, TypeError):
                            lbl = str(raw_val)
                        construction_types.append({"value": raw_val, "label": lbl})
                    if construction_types: break
                except Exception as e:
                    print(f"[Available Years] Construction types error: {e}")

        return jsonify({
            "status": "success",
            "years": years,
            "zones": zones,
            "regions": regions,
            "months": months,
            "customer_types": customer_types,
            "construction_types": construction_types
        }), 200

    except Exception as exc:
        print(f"[Available Years] Database query failed: {exc}")
        return jsonify({"status": "error", "message": "Database query failed.", "details": str(exc)}), 500

    finally:
        if cursor and is_hana:
            try: cursor.close()
            except Exception: pass
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass
        if cursor and not is_hana:
            try: cursor.close()
            except Exception: pass
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 7. Dashboard Filters Controller (Categories, Constructions, TyreTypes)
# ─────────────────────────────────────────────────────────────────────────────

def dashboard_filters_controller(get_db_connection):
    session_id = request.args.get("session_id", "").strip()
    user_id = request.args.get("user_id")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    filters_data = {
        "categories": [],
        "constructions": [],
        "tyreTypes": [],
        "years": [],
        "months": []
    }

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "success", **filters_data}), 200

        hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, session_id, user_id=user_id)
        is_hana = bool(hana_conn)

        if is_hana:
            cursor = hana_conn.cursor()
            table_columns = get_sap_table_columns(conn, hana_conn, sap_schema, session_id)
            user_db = sap_schema
            tables = list(table_columns.keys())
        else:
            try:
                cursor = conn.cursor(dictionary=True)
            except TypeError:
                cursor = conn.cursor()

            cursor.execute("""
                SELECT new_user_db 
                FROM external_db_sync_log 
                WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            row = cursor.fetchone()

            if not row:
                cursor.execute("""
                    SELECT new_user_db 
                    FROM external_db_sync_log 
                    WHERE new_user_db IS NOT NULL AND new_user_db != ''
                    ORDER BY id DESC LIMIT 1
                """)
                row = cursor.fetchone()

            if not row:
                return jsonify({"status": "success", **filters_data}), 200

            user_db = row["new_user_db"]
            cursor.execute(f"USE `{user_db}`")
            cursor.execute("SHOW TABLES")
            tables = [list(r.values())[0] if isinstance(r, dict) else r[0] for r in cursor.fetchall()]

        def q_tbl(t): return f'"{user_db}"."{t}"' if is_hana else f"`{user_db}`.`{t}`"

        # 1. Categories
        for tbl in tables:
            if "category" in tbl.lower():
                t_name = q_tbl(tbl)
                code_col = get_actual_column_name(cursor, t_name, ["cat_code", "category_code", "code"], is_hana=is_hana)
                desc_col = get_actual_column_name(cursor, t_name, ["cat_desc", "category_name", "category"], is_hana=is_hana)
                if code_col and desc_col:
                    try:
                        q_cat = f"SELECT DISTINCT {code_col} as val_code, {desc_col} as val_label FROM {t_name} WHERE {desc_col} IS NOT NULL AND {desc_col} != '' ORDER BY val_label ASC"
                        execute_query(cursor, q_cat, is_hana=is_hana)
                        res = [{"value": r["val_code"], "label": r["val_label"]} for r in fetchall_dict(cursor) if r.get("val_label")]
                        if res:
                            filters_data["categories"] = res
                            break
                    except Exception as e:
                        print(f"[Filters] Categories error for {tbl}: {e}")

        if not filters_data["categories"]:
            for tbl in tables:
                t_name = q_tbl(tbl)
                cat = get_actual_column_name(cursor, t_name, ["cat_desc", "catg", "prd_cat", "product_category", "category", "product_type"], is_hana=is_hana)
                if cat:
                    try:
                        q_cat = f"SELECT DISTINCT {cat} as val FROM {t_name} WHERE {cat} IS NOT NULL AND {cat} != '' ORDER BY val ASC"
                        execute_query(cursor, q_cat, is_hana=is_hana)
                        res = [{"value": r["val"], "label": r["val"]} for r in fetchall_dict(cursor) if r.get("val")]
                        if res:
                            filters_data["categories"] = res
                            break
                    except Exception as e:
                        print(f"[Filters] Categories fallback error: {e}")

        # 2. Constructions
        construction_labels = {
            1: "Bias", 2: "Radial", 3: "Tubeless Bias", 4: "Tubeless Radial",
            5: "Radial (Tube Type)", 6: "Radial (Tubeless)", 7: "Solid / OTR"
        }
        for tbl in tables:
            t_name = q_tbl(tbl)
            const = get_actual_column_name(cursor, t_name, ["constuction", "construction_type", "construction"], is_hana=is_hana)
            if const:
                try:
                    q_con = f"SELECT DISTINCT {const} as val FROM {t_name} WHERE {const} IS NOT NULL AND {const} != '' ORDER BY val ASC"
                    execute_query(cursor, q_con, is_hana=is_hana)
                    res = []
                    for r in fetchall_dict(cursor):
                        raw_val = r["val"]
                        try:
                            val_int = int(raw_val)
                            lbl = construction_labels.get(val_int, f"Type {raw_val}")
                        except (ValueError, TypeError):
                            lbl = str(raw_val)
                        res.append({"value": raw_val, "label": lbl})
                    if res:
                        filters_data["constructions"] = res
                        break
                except Exception as e:
                    print(f"[Filters] Constructions error: {e}")

        # 3. Tyre Types
        for tbl in tables:
            if "category" in tbl.lower():
                t_name = q_tbl(tbl)
                code_col = get_actual_column_name(cursor, t_name, ["cat_code", "category_code", "code"], is_hana=is_hana)
                desc_col = get_actual_column_name(cursor, t_name, ["cat_desc", "category_name", "category"], is_hana=is_hana)
                if desc_col:
                    try:
                        q_tyre = f"SELECT DISTINCT {code_col or desc_col} as val_code, {desc_col} as val_label FROM {t_name} WHERE {desc_col} IS NOT NULL AND {desc_col} != '' ORDER BY val_label ASC"
                        execute_query(cursor, q_tyre, is_hana=is_hana)
                        res = [{"value": r["val_code"], "label": r["val_label"]} for r in fetchall_dict(cursor) if r.get("val_label")]
                        if res:
                            filters_data["tyreTypes"] = res
                            break
                    except Exception as e:
                        print(f"[Filters] Tyre types error: {e}")

        if not filters_data["tyreTypes"]:
            for tbl in tables:
                t_name = q_tbl(tbl)
                tyre = get_actual_column_name(cursor, t_name, ["tyre_type", "vehicle_type", "tyre_type_name"], is_hana=is_hana)
                if tyre:
                    try:
                        q_tyre = f"SELECT DISTINCT {tyre} as val FROM {t_name} WHERE {tyre} IS NOT NULL AND {tyre} != '' ORDER BY val ASC"
                        execute_query(cursor, q_tyre, is_hana=is_hana)
                        res = [{"value": r["val"], "label": r["val"]} for r in fetchall_dict(cursor) if r.get("val")]
                        if res:
                            filters_data["tyreTypes"] = res
                            break
                    except Exception as e:
                        print(f"[Filters] Tyre fallback error: {e}")

        # 4. Years and Months
        all_years = set()
        month_set = set()
        for tbl in tables:
            tbl_lower = tbl.lower()
            if any(k in tbl_lower for k in ["dayattribute", "calendar", "target", "budget", "plan"]):
                continue
            if not any(k in tbl_lower for k in ["sales", "invoice", "currentsales", "saleshistory"]):
                continue

            t_name = q_tbl(tbl)
            date_col = get_actual_column_name(cursor, t_name, ["invoice_date", "date", "billing__doc_date", "fkdat"], is_hana=is_hana)
            if date_col:
                try:
                    q_y = f"SELECT DISTINCT YEAR({date_col}) as yr FROM {t_name} WHERE {date_col} IS NOT NULL"
                    execute_query(cursor, q_y, is_hana=is_hana)
                    for r in fetchall_dict(cursor):
                        if r.get("yr") and 2000 <= int(r["yr"]) <= 2100:
                            all_years.add(int(r["yr"]))

                    q_m = f"SELECT DISTINCT MONTH({date_col}) as m_num FROM {t_name} WHERE {date_col} IS NOT NULL"
                    execute_query(cursor, q_m, is_hana=is_hana)
                    for r in fetchall_dict(cursor):
                        if r.get("m_num"):
                            m_idx = int(r["m_num"])
                            if 1 <= m_idx <= 12:
                                month_set.add(m_idx)
                except Exception as e:
                    print(f"[Filters] Date error: {e}")

        if all_years:
            filters_data["years"] = sorted(list(all_years))
        if month_set:
            filters_data["months"] = [calendar.month_name[m] for m in sorted(list(month_set))]

        return jsonify({"status": "success", **filters_data}), 200

    except Exception as exc:
        print(f"[Dashboard Filters] query failed: {exc}")
        return jsonify({"status": "success", **filters_data}), 200

    finally:
        if cursor and is_hana:
            try: cursor.close()
            except Exception: pass
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass
        if cursor and not is_hana:
            try: cursor.close()
            except Exception: pass
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 8. Default Dashboard Metrics Controller (16 Executive KPI Metrics)
# ─────────────────────────────────────────────────────────────────────────────

def default_dashboard_metrics_controller(get_db_connection):
    """
    Fetches the 16 executive summary metrics for the right-side dashboard panels.
    Supports live SAP HANA calculation views and MySQL synced databases.
    """
    data = request.json or {}
    session_id = data.get("session_id", "").strip()
    user_id = data.get("user_id")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    metrics_data = {
        "metric_1": {"label": "Total Sales Revenue", "value": "N/A", "subtext": "No data available"},
        "metric_2": {"label": "Top Performing Tyre", "value": "N/A", "subtext": "No data available"},
        "metric_3": {"label": "Leading Region", "value": "N/A", "subtext": "No data available"},
        "metric_4": {"label": "Year-over-Year Growth", "value": "N/A", "subtext": "No data available"},
        "metric_5": {"label": "Achievement", "value": "N/A", "subtext": "Current achievement"},
        "metric_6": {"label": "Sales Target", "value": "N/A", "subtext": "Target sales"},
        "metric_7": {"label": "Sales Actual", "value": "N/A", "subtext": "Actual sales"},
        "metric_8": {"label": "SAS IN", "value": "N/A", "subtext": "SAS IN value"},
        "metric_9": {"label": "SAS Variance", "value": "N/A", "subtext": "Variance"},
        "metric_10": {"label": "Billing Scope", "value": "N/A", "subtext": "Billing scope"},
        "metric_11": {"label": "Dealer Spread", "value": "N/A", "subtext": "Dealer spread"},
        "metric_12": {"label": "Overdue", "value": "N/A", "subtext": "Overdue percentage"},
        "metric_13": {"label": "Exposure", "value": "N/A", "subtext": "Exposure percentage"},
        "metric_14": {"label": "Rotation", "value": "N/A", "subtext": "Rotation metric"},
        "metric_15": {"label": "New Dealer", "value": "N/A", "subtext": "New dealers"},
        "metric_16": {"label": "Attrition", "value": "N/A", "subtext": "Attrition count"}
    }

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500

        hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, session_id, user_id=user_id)
        is_hana = bool(hana_conn)

        table_columns = {}
        user_db = None

        if is_hana:
            cursor = hana_conn.cursor()
            user_db = sap_schema
            table_columns = get_sap_table_columns(conn, hana_conn, user_db, session_id)
        else:
            try:
                cursor = conn.cursor(dictionary=True)
            except TypeError:
                cursor = conn.cursor()

            cursor.execute("""
                SELECT new_user_db, table_name 
                FROM external_db_sync_log 
                WHERE session_id=%s 
                  AND new_user_db IS NOT NULL 
                  AND new_user_db != ''
                  AND table_name IS NOT NULL
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            sync_row = cursor.fetchone()

            if not sync_row:
                print(f"[Default Metrics] No dynamic table found for session {session_id}")
                return jsonify({"status": "success", "data": metrics_data}), 200

            user_db = sync_row["new_user_db"]
            cursor.execute(f"USE `{user_db}`")
            cursor.execute("SHOW TABLES")
            db_tables = [list(r.values())[0] if isinstance(r, dict) else r[0] for r in cursor.fetchall()]

            for tbl in db_tables:
                try:
                    cursor.execute(f"DESCRIBE `{tbl}`")
                    cols = cursor.fetchall()
                    table_columns[tbl] = {
                        (c["Field"] if isinstance(c, dict) else c[0]).lower().replace(' ', '').replace('_', ''): 
                        (c["Field"] if isinstance(c, dict) else c[0]) 
                        for c in cols
                    }
                except Exception as e:
                    print(f"[Default Metrics] Describe table {tbl} failed: {e}")

        def check_col(cols_dict, opts_list):
            if not cols_dict:
                return None
            for opt in opts_list:
                clean_opt = opt.lower().replace(' ', '').replace('_', '')
                if clean_opt in cols_dict:
                    return cols_dict[clean_opt]
            return None

        def find_table(candidates):
            for cand in candidates:
                clean_cand = cand.lower().replace('_', '').replace('-', '')
                for tbl in table_columns.keys():
                    clean_tbl = tbl.lower().replace('_', '').replace('-', '')
                    if clean_cand in clean_tbl:
                        return tbl
            return None

        def q_tbl(t): return f'"{user_db}"."{t}"' if is_hana else f"`{user_db}`.`{t}`"
        def q_col(alias, c): return f'{alias}."{c}"' if is_hana else f'{alias}.`{c}`'
        def q_name(n): return f'"{n}"' if is_hana else f"`{n}`"

        # Identify SAP & Legacy Tables
        curr_sales_tbl = find_table(["zpvfsdcurrentsales", "currentsales", "current_sales"])
        hist_sales_tbl = find_table(["zpvfsdsaleshistory", "saleshistory", "sales_history"])
        sales_data_tbl = find_table(["salesdata", "sales_data"])
        cust_tbl = find_table(["zivfcustomermaster", "customermaster", "customer"])
        terr_tbl = find_table(["zivfterritorymaster", "territorymaster", "territory"])
        reg_tbl = find_table(["zivfregionmaster", "regionmaster", "region"])
        zone_tbl = find_table(["zivfzonemaster", "zonemaster", "zone"])
        sku_tbl = find_table(["zivrskumaster", "skumaster", "materialmaster", "material", "sku"])
        cat_tbl = find_table(["zivfcategorymaster", "categorymaster", "category"])
        tyre_tbl = find_table(["tyretypemaster", "tyretype"])
        day_attr_tbl = find_table(["zitrdayattribute", "dayattribute", "day_attribute"])
        target_tbl = find_table(["zivfsdsalesplantarget", "salestarget", "sales_target", "target", "budget", "sales_plan"])
        canc_tbl = find_table(["zpvfsdcancelledsalesinvoice", "cancelledsalesinvoice", "cancelled_sales_invoice", "cancelled"])

        can_filter = ""
        can_null = ""
        # On SAP HANA, skip joining ZPVF_SD_CancelledSalesInvoice_01_TR on default metrics
        # as indicator_cancel is already on fact views and 30M row string join causes GLOBAL_ALLOCATION_LIMIT
        if canc_tbl and not is_hana:
            s_inv = check_col(table_columns.get(curr_sales_tbl or hist_sales_tbl or sales_data_tbl, {}), ["invoice_number", "invoicenumber", "vbeln"]) or "invoice_number"
            s_plant = check_col(table_columns.get(curr_sales_tbl or hist_sales_tbl or sales_data_tbl, {}), ["plant", "werks"]) or "plant"
            s_fkart = check_col(table_columns.get(curr_sales_tbl or hist_sales_tbl or sales_data_tbl, {}), ["fkart", "billing_type"]) or "fkart"
            c_inv = check_col(table_columns.get(canc_tbl, {}), ["invoice_number", "invoicenumber", "vbeln"]) or "invoice_number"
            c_plant = check_col(table_columns.get(canc_tbl, {}), ["plant", "werks"]) or "plant"
            c_fkart = check_col(table_columns.get(canc_tbl, {}), ["fkart", "billing_type"]) or "fkart"
            c_dist = check_col(table_columns.get(canc_tbl, {}), ["distribution_channel", "vtweg"]) or "distribution_channel"

            can_filter = f"LEFT JOIN {q_tbl(canc_tbl)} can ON s.{q_name(s_inv)} = can.{q_name(c_inv)} AND s.{q_name(s_plant)} = can.{q_name(c_plant)} AND s.{q_name(s_fkart)} = can.{q_name(c_fkart)}"
            can_null = f"AND can.{q_name(c_inv)} IS NULL"

        def build_aligned_union(curr_t, hist_t, c_curr_expr, c_hist_expr):
            curr_map = table_columns.get(curr_t, {})
            hist_map = table_columns.get(hist_t, {})
            common_cols = []
            for clean_k, c_n in curr_map.items():
                if clean_k in hist_map:
                    common_cols.append((c_n, hist_map[clean_k]))
            if not common_cols:
                return f"(SELECT * FROM {q_tbl(curr_t)} {c_curr_expr} UNION ALL SELECT * FROM {q_tbl(hist_t)} {c_hist_expr})"
            c_proj = ", ".join([f'c.{q_name(c[0])} AS {q_name(c[0])}' for c in common_cols])
            h_proj = ", ".join([f'h.{q_name(c[1])} AS {q_name(c[0])}' for c in common_cols])
            return f"(SELECT {c_proj} FROM {q_tbl(curr_t)} c {c_curr_expr} UNION ALL SELECT {h_proj} FROM {q_tbl(hist_t)} h {c_hist_expr})"

        cancel_expr_curr = ""
        cancel_expr_hist = ""
        if curr_sales_tbl:
            c_curr = check_col(table_columns[curr_sales_tbl], ["indicator_cancel", "cancel", "fksto"])
            d_curr = check_col(table_columns[curr_sales_tbl], ["distribution_channel", "vtweg"])
            curr_conds = []
            if c_curr: curr_conds.append(f"({q_name(c_curr)} IS NULL OR {q_name(c_curr)} NOT IN ('X', 'Y', '1'))")
            if d_curr:
                curr_conds.append(f"(TRIM(CAST({q_name(d_curr)} AS NVARCHAR(20))) IN ('10', '010', '1'))" if is_hana else f"({q_name(d_curr)} = 10 OR {q_name(d_curr)} = '10')")
            cancel_expr_curr = ("WHERE " + " AND ".join(curr_conds)) if curr_conds else ""

        if hist_sales_tbl:
            c_hist = check_col(table_columns[hist_sales_tbl], ["indicator_cancel", "cancel", "fksto"])
            d_hist = check_col(table_columns[hist_sales_tbl], ["distribution_channel", "vtweg"])
            hist_conds = []
            if c_hist: hist_conds.append(f"({q_name(c_hist)} IS NULL OR {q_name(c_hist)} NOT IN ('X', 'Y', '1'))")
            if d_hist:
                hist_conds.append(f"(TRIM(CAST({q_name(d_hist)} AS NVARCHAR(20))) IN ('10', '010', '1'))" if is_hana else f"({q_name(d_hist)} = 10 OR {q_name(d_hist)} = '10')")
            cancel_expr_hist = ("WHERE " + " AND ".join(hist_conds)) if hist_conds else ""

        if is_hana and curr_sales_tbl:
            sales_from = f"(SELECT s.* FROM {q_tbl(curr_sales_tbl)} s {can_filter} {cancel_expr_curr.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
            sales_cols = table_columns.get(curr_sales_tbl, {})
            primary_sales_tbl = curr_sales_tbl
        elif curr_sales_tbl and hist_sales_tbl:
            aligned_u = build_aligned_union(curr_sales_tbl, hist_sales_tbl, cancel_expr_curr, cancel_expr_hist)
            sales_from = f"(SELECT s.* FROM {aligned_u} s {can_filter} WHERE 1=1 {can_null}) s"
            sales_cols = table_columns.get(curr_sales_tbl) or table_columns.get(hist_sales_tbl) or {}
            primary_sales_tbl = curr_sales_tbl
        elif curr_sales_tbl:
            sales_from = f"(SELECT s.* FROM {q_tbl(curr_sales_tbl)} s {can_filter} {cancel_expr_curr.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
            sales_cols = table_columns.get(curr_sales_tbl, {})
            primary_sales_tbl = curr_sales_tbl
        elif hist_sales_tbl:
            sales_from = f"(SELECT s.* FROM {q_tbl(hist_sales_tbl)} s {can_filter} {cancel_expr_hist.replace('WHERE', 'WHERE 1=1 AND')} {can_null}) s"
            sales_cols = table_columns.get(hist_sales_tbl, {})
            primary_sales_tbl = hist_sales_tbl
        elif sales_data_tbl:
            sales_from = f"{q_tbl(sales_data_tbl)} s"
            sales_cols = table_columns.get(sales_data_tbl, {})
            primary_sales_tbl = sales_data_tbl
        else:
            found_tbl = list(table_columns.keys())[0] if table_columns else "sales"
            for tbl, cols in table_columns.items():
                if check_col(cols, ["invoice_value", "ndp_value", "invoice_value_inr", "taxable_value"]):
                    found_tbl = tbl
                    break
            sales_from = f"{q_tbl(found_tbl)} s"
            sales_cols = table_columns.get(found_tbl, {})
            primary_sales_tbl = found_tbl

        real_revenue_col = check_col(sales_cols, ["invoice_value", "ndp_value", "invoice_value_inr", "taxable_value", "taxable_value_inr", "ndp_inr"])
        has_revenue = bool(real_revenue_col)

        if has_revenue:
            if is_hana:
                join_revenue_expr = f"COALESCE(s.{q_name(real_revenue_col)}, 0)"
            else:
                join_revenue_expr = f"COALESCE(CAST(REPLACE(TRIM(s.{q_name(real_revenue_col)}), ',', '') AS DECIMAL(18,2)), 0)"
        else:
            join_revenue_expr = "0.0"

        # 1. Total Sales Revenue
        total_revenue = 0.0
        if has_revenue:
            try:
                execute_query(cursor, f"SELECT SUM({join_revenue_expr}) AS total_sales_revenue FROM {sales_from}", is_hana=is_hana)
                row = fetchone_dict(cursor)
                total_revenue = float(row["total_sales_revenue"] or 0) if row else 0.0
            except Exception as rev_err:
                print(f"[Default Metrics] Total revenue query failed: {rev_err}")

        # 2. Top Performing Tyre
        top_tyre = "N/A"
        if has_revenue:
            # Join SKU Master with Category Master
            if sku_tbl and cat_tbl:
                sales_mat = check_col(sales_cols, ["sku", "material", "matnr"])
                sku_mat = check_col(table_columns[sku_tbl], ["matnr", "sku", "material"])
                sku_cat = check_col(table_columns[sku_tbl], ["cat_code", "catcode", "category_code", "category"])
                cat_code = check_col(table_columns[cat_tbl], ["cat_code", "catcode", "category_code"])
                cat_desc = check_col(table_columns[cat_tbl], ["cat_desc", "catdesc", "category_desc", "category_name", "cat_name"])

                if sales_mat and sku_mat and sku_cat and cat_code and cat_desc:
                    try:
                        q_t = f"""
                            SELECT
                                cat.{q_name(cat_desc)} AS vehicle_type,
                                ROUND(SUM({join_revenue_expr}), 2) AS revenue
                            FROM {sales_from}
                            JOIN {q_tbl(sku_tbl)} sm ON s.{q_name(sales_mat)} = sm.{q_name(sku_mat)}
                            JOIN {q_tbl(cat_tbl)} cat ON sm.{q_name(sku_cat)} = cat.{q_name(cat_code)}
                            WHERE cat.{q_name(cat_desc)} IS NOT NULL AND cat.{q_name(cat_desc)} <> ''
                            GROUP BY cat.{q_name(cat_desc)}
                            ORDER BY revenue DESC
                            LIMIT 1
                        """
                        execute_query(cursor, q_t, is_hana=is_hana)
                        row = fetchone_dict(cursor)
                        if row and row.get("vehicle_type"):
                            top_tyre = row["vehicle_type"]
                    except Exception as tyre_err:
                        print(f"[Default Metrics] Category master join query failed: {tyre_err}")

            # Fallback direct category master query
            if top_tyre == "N/A" and cat_tbl:
                sales_mat = check_col(sales_cols, ["sku", "material", "matnr"])
                cat_mat = check_col(table_columns[cat_tbl], ["matnr", "sku", "material"])
                cat_desc = check_col(table_columns[cat_tbl], ["cat_desc", "catdesc", "category_desc", "category_name"])
                if sales_mat and cat_mat and cat_desc:
                    try:
                        q_t2 = f"""
                            SELECT
                                cat.{q_name(cat_desc)} AS vehicle_type,
                                ROUND(SUM({join_revenue_expr}), 2) AS revenue
                            FROM {sales_from}
                            JOIN {q_tbl(cat_tbl)} cat ON s.{q_name(sales_mat)} = cat.{q_name(cat_mat)}
                            WHERE cat.{q_name(cat_desc)} IS NOT NULL AND cat.{q_name(cat_desc)} <> ''
                            GROUP BY cat.{q_name(cat_desc)}
                            ORDER BY revenue DESC
                            LIMIT 1
                        """
                        execute_query(cursor, q_t2, is_hana=is_hana)
                        row = fetchone_dict(cursor)
                        if row and row.get("vehicle_type"):
                            top_tyre = row["vehicle_type"]
                    except Exception as tyre_err:
                        print(f"[Default Metrics] Direct category master query failed: {tyre_err}")

            # Fallback SKU tyre_type
            if top_tyre == "N/A" and sku_tbl:
                sales_mat = check_col(sales_cols, ["sku", "material", "matnr"])
                sku_mat = check_col(table_columns[sku_tbl], ["matnr", "sku", "material"])
                sku_tyre = check_col(table_columns[sku_tbl], ["tyre_type", "tyretype", "vehicle_type", "maktx"])
                if sales_mat and sku_mat and sku_tyre:
                    try:
                        q_t3 = f"""
                            SELECT
                                sm.{q_name(sku_tyre)} AS vehicle_type,
                                ROUND(SUM({join_revenue_expr}), 2) AS revenue
                            FROM {sales_from}
                            JOIN {q_tbl(sku_tbl)} sm ON s.{q_name(sales_mat)} = sm.{q_name(sku_mat)}
                            WHERE sm.{q_name(sku_tyre)} IS NOT NULL AND sm.{q_name(sku_tyre)} NOT IN ('', '0', '-')
                            GROUP BY sm.{q_name(sku_tyre)}
                            ORDER BY revenue DESC
                            LIMIT 1
                        """
                        execute_query(cursor, q_t3, is_hana=is_hana)
                        row = fetchone_dict(cursor)
                        if row and row.get("vehicle_type"):
                            top_tyre = row["vehicle_type"]
                    except Exception as tyre_err:
                        print(f"[Default Metrics] SKU tyre_type query failed: {tyre_err}")

        # 3. Leading Region
        leading_region = "N/A"
        if has_revenue:
            # Plant -> Territory -> Region
            if terr_tbl and reg_tbl:
                sales_plant = check_col(sales_cols, ["plant", "werks"])
                terr_werks = check_col(table_columns[terr_tbl], ["werks", "plant"])
                terr_area = check_col(table_columns[terr_tbl], ["area", "region_code", "region", "regio"])
                r_code = check_col(table_columns[reg_tbl], ["region_code", "region", "regio"])
                r_name = check_col(table_columns[reg_tbl], ["region_name", "region", "name"])

                if sales_plant and terr_werks and terr_area and r_code and r_name:
                    try:
                        q_r2 = f"""
                            SELECT
                                r.{q_name(r_name)} AS region_name,
                                ROUND(SUM({join_revenue_expr}), 2) AS revenue
                            FROM {sales_from}
                            JOIN {q_tbl(terr_tbl)} t ON s.{q_name(sales_plant)} = t.{q_name(terr_werks)}
                            JOIN {q_tbl(reg_tbl)} r ON t.{q_name(terr_area)} = r.{q_name(r_code)}
                            WHERE r.{q_name(r_name)} IS NOT NULL AND r.{q_name(r_name)} <> ''
                            GROUP BY r.{q_name(r_name)}
                            ORDER BY revenue DESC
                            LIMIT 1
                        """
                        execute_query(cursor, q_r2, is_hana=is_hana)
                        row = fetchone_dict(cursor)
                        if row and row.get("region_name"):
                            leading_region = row["region_name"]
                    except Exception as reg_err:
                        print(f"[Default Metrics] Plant territory region query failed: {reg_err}")

            # Customer -> Territory -> Region
            if leading_region == "N/A" and cust_tbl and reg_tbl:
                sales_cust = check_col(sales_cols, ["dealercode", "dealer_code", "customer", "kunnr"])
                cust_id = check_col(table_columns[cust_tbl], ["kunnr", "dealercode", "customer"])
                r_name = check_col(table_columns[reg_tbl], ["region_name", "region", "name"])

                if terr_tbl:
                    cust_terr = check_col(table_columns[cust_tbl], ["bzirk", "territory", "territory_code"])
                    terr_code = check_col(table_columns[terr_tbl], ["bzirk", "territory_code", "territory"])
                    terr_reg = check_col(table_columns[terr_tbl], ["regio", "region_code", "region", "area"])
                    r_code = check_col(table_columns[reg_tbl], ["region_code", "region", "regio"])

                    if sales_cust and cust_id and cust_terr and terr_code and terr_reg and r_code and r_name:
                        try:
                            q_r3 = f"""
                                SELECT
                                    r.{q_name(r_name)} AS region_name,
                                    ROUND(SUM({join_revenue_expr}), 2) AS revenue
                                FROM {sales_from}
                                JOIN {q_tbl(cust_tbl)} c ON s.{q_name(sales_cust)} = c.{q_name(cust_id)}
                                JOIN {q_tbl(terr_tbl)} t ON c.{q_name(cust_terr)} = t.{q_name(terr_code)}
                                JOIN {q_tbl(reg_tbl)} r ON t.{q_name(terr_reg)} = r.{q_name(r_code)}
                                WHERE r.{q_name(r_name)} IS NOT NULL AND r.{q_name(r_name)} <> ''
                                GROUP BY r.{q_name(r_name)}
                                ORDER BY revenue DESC
                                LIMIT 1
                            """
                            execute_query(cursor, q_r3, is_hana=is_hana)
                            row = fetchone_dict(cursor)
                            if row and row.get("region_name"):
                                leading_region = row["region_name"]
                        except Exception as reg_err:
                            print(f"[Default Metrics] Joined territory region query failed: {reg_err}")

        # 4. YoY Growth
        current_year = 0.0
        previous_year = 0.0
        yoy = 0.0
        yoy_subtext = "No data available"
        real_date_col = check_col(sales_cols, ["invoice_date", "date", "billing__doc_date", "billing_doc_date", "fkdat"])

        if has_revenue and real_date_col:
            try:
                year_expr = f"YEAR(s.{q_name(real_date_col)})"
                date_filter = f"s.{q_name(real_date_col)} IS NOT NULL"

                q_yoy = f"""
                    SELECT {year_expr} AS yr, ROUND(SUM({join_revenue_expr}), 2) AS revenue
                    FROM {sales_from}
                    WHERE {date_filter} AND {year_expr} IS NOT NULL AND {year_expr} > 2000
                    GROUP BY {year_expr}
                    ORDER BY yr DESC
                """
                execute_query(cursor, q_yoy, is_hana=is_hana)
                year_rows = fetchall_dict(cursor)

                if year_rows and len(year_rows) > 0:
                    current_year_row = year_rows[0]
                    current_year = float(current_year_row.get("revenue") or 0.0)
                    current_year_num = int(current_year_row["yr"])

                    if len(year_rows) > 1:
                        prev_year_row = year_rows[1]
                        previous_year = float(prev_year_row.get("revenue") or 0.0)
                        prev_year_num = int(prev_year_row["yr"])

                        if previous_year > 0:
                            yoy = round(((current_year - previous_year) / previous_year) * 100, 2)
                            if current_year_num - prev_year_num == 1:
                                yoy_subtext = "Compared to last year"
                            else:
                                yoy_subtext = f"Compared to {prev_year_num}"
                        else:
                            yoy_subtext = f"{current_year_num} sales only"
                    else:
                        yoy_subtext = f"{current_year_num} sales only"
            except Exception as yoy_err:
                print(f"[Default Metrics] YoY query failed: {yoy_err}")

        # 5. Billing Scope (Distinct active customers billed)
        billing_scope = "N/A"
        real_cust_col_sales = check_col(sales_cols, ["dealercode", "dealer_code", "customer", "kunnr"])
        if real_cust_col_sales:
            try:
                execute_query(cursor, f"SELECT COUNT(DISTINCT s.{q_name(real_cust_col_sales)}) AS cust_count FROM {sales_from}", is_hana=is_hana)
                row = fetchone_dict(cursor)
                if row and row.get("cust_count") is not None:
                    billing_scope = str(row["cust_count"])
            except Exception as e:
                print(f"[Default Metrics] Billing scope query failed: {e}")

        # 6. Dealer Spread (Active dealers with CH_TYPE='DL' or KTOKD='0001')
        dealer_spread = "N/A"
        if real_cust_col_sales and cust_tbl:
            cust_id_col = check_col(table_columns[cust_tbl], ["kunnr", "dealercode", "customer"])
            dealer_type_col = check_col(table_columns[cust_tbl], ["ch_type", "ktokd", "acc_grp", "customer_type"])
            if cust_id_col:
                try:
                    if dealer_type_col:
                        q_ds = f"""
                            SELECT COUNT(DISTINCT s.{q_name(real_cust_col_sales)}) AS dealer_count
                            FROM {sales_from}
                            JOIN {q_tbl(cust_tbl)} c ON s.{q_name(real_cust_col_sales)} = c.{q_name(cust_id_col)}
                            WHERE c.{q_name(dealer_type_col)} IN ('DL', '0001', 'Z001')
                        """
                    else:
                        q_ds = f"SELECT COUNT(DISTINCT s.{q_name(real_cust_col_sales)}) AS dealer_count FROM {sales_from}"
                    execute_query(cursor, q_ds, is_hana=is_hana)
                    row = fetchone_dict(cursor)
                    if row and row.get("dealer_count") is not None:
                        dealer_spread = str(row["dealer_count"])
                except Exception as e:
                    print(f"[Default Metrics] Dealer spread query failed: {e}")

        # 7. Credit Exposure and Outstanding
        exposure_val = "N/A"
        overdue_val = "N/A"
        if cust_tbl:
            exp_col = check_col(table_columns[cust_tbl], ["exposure"])
            out_col = check_col(table_columns[cust_tbl], ["outstand", "outstanding"])
            if exp_col:
                try:
                    if is_hana:
                        execute_query(cursor, f"SELECT SUM(COALESCE({q_name(exp_col)}, 0)) AS val FROM {q_tbl(cust_tbl)} WHERE {q_name(exp_col)} IS NOT NULL", is_hana=is_hana)
                    else:
                        execute_query(cursor, f"SELECT SUM(CAST(REPLACE(TRIM({q_name(exp_col)}), ',', '') AS DECIMAL(18,2))) AS val FROM {q_tbl(cust_tbl)} WHERE {q_name(exp_col)} IS NOT NULL AND {q_name(exp_col)} != ''", is_hana=is_hana)
                    row = fetchone_dict(cursor)
                    if row and row.get("val") is not None:
                        v = float(row["val"])
                        if v >= 10000000: exposure_val = f"₹{(v / 10000000):.2f} Cr"
                        elif v >= 100000: exposure_val = f"₹{(v / 100000):.2f} Lac"
                        else: exposure_val = f"₹{v:,.2f}"
                except Exception as e:
                    print(f"[Default Metrics] Exposure query failed: {e}")

            if out_col:
                try:
                    if is_hana:
                        execute_query(cursor, f"SELECT SUM(COALESCE({q_name(out_col)}, 0)) AS val FROM {q_tbl(cust_tbl)} WHERE {q_name(out_col)} IS NOT NULL", is_hana=is_hana)
                    else:
                        execute_query(cursor, f"SELECT SUM(CAST(REPLACE(TRIM({q_name(out_col)}), ',', '') AS DECIMAL(18,2))) AS val FROM {q_tbl(cust_tbl)} WHERE {q_name(out_col)} IS NOT NULL AND {q_name(out_col)} != ''", is_hana=is_hana)
                    row = fetchone_dict(cursor)
                    if row and row.get("val") is not None:
                        v = float(row["val"])
                        if v >= 10000000: overdue_val = f"₹{(v / 10000000):.2f} Cr"
                        elif v >= 100000: overdue_val = f"₹{(v / 100000):.2f} Lac"
                        else: overdue_val = f"₹{v:,.2f}"
                except Exception as e:
                    print(f"[Default Metrics] Outstanding query failed: {e}")

        # 8. SAS IN & SAS Variance
        sas_in_val = "N/A"
        sas_variance_val = "N/A"
        if cust_tbl:
            sas_col = check_col(table_columns[cust_tbl], ["open_sas", "sas_curr", "dmbtr_sas"])
            if sas_col:
                try:
                    if is_hana:
                        execute_query(cursor, f"SELECT SUM(COALESCE({q_name(sas_col)}, 0)) AS val FROM {q_tbl(cust_tbl)} WHERE {q_name(sas_col)} IS NOT NULL", is_hana=is_hana)
                    else:
                        execute_query(cursor, f"SELECT SUM(CAST(REPLACE(TRIM({q_name(sas_col)}), ',', '') AS DECIMAL(18,2))) AS val FROM {q_tbl(cust_tbl)} WHERE {q_name(sas_col)} IS NOT NULL AND {q_name(sas_col)} != ''", is_hana=is_hana)
                    row = fetchone_dict(cursor)
                    if row and row.get("val") is not None:
                        s_num = float(row["val"])
                        if s_num >= 10000000: sas_in_val = f"₹{(s_num / 10000000):.2f} Cr"
                        elif s_num >= 100000: sas_in_val = f"₹{(s_num / 100000):.2f} Lac"
                        else: sas_in_val = f"₹{s_num:,.2f}"
                        if total_revenue > 0:
                            s_var = ((total_revenue - s_num) / total_revenue) * 100
                            sas_variance_val = f"{s_var:+.1f}%"
                except Exception as e:
                    print(f"[Default Metrics] SAS query failed: {e}")

        # 9. Attrition
        attrition = "N/A"
        if cust_tbl:
            att_col = check_col(table_columns[cust_tbl], ["attration", "attrition", "lm_nobill", "l2m_nobill", "l3m_nobill"])
            if att_col:
                try:
                    q_att_cust = f"SELECT COUNT(*) AS att_count FROM {q_tbl(cust_tbl)} WHERE {q_name(att_col)} IS NOT NULL AND UPPER(TRIM(CAST({q_name(att_col)} AS NVARCHAR(10)))) IN ('Y', '1', 'X', 'TRUE')" if is_hana else f"SELECT COUNT(*) AS att_count FROM {q_tbl(cust_tbl)} WHERE {q_name(att_col)} IS NOT NULL AND {q_name(att_col)} IN ('Y', '1', 'X', 'TRUE')"
                    execute_query(cursor, q_att_cust, is_hana=is_hana)
                    row = fetchone_dict(cursor)
                    if row and row.get("att_count") is not None and int(row["att_count"]) > 0:
                        attrition = str(row["att_count"])
                except Exception as e:
                    print(f"[Default Metrics] Customer Master attrition failed: {e}")

        if attrition == "N/A" and real_cust_col_sales and real_date_col:
            try:
                execute_query(cursor, f"SELECT MAX(s.{q_name(real_date_col)}) AS max_d FROM {sales_from}", is_hana=is_hana)
                max_d_row = fetchone_dict(cursor)
                latest_date = max_d_row.get("max_d") if max_d_row else None
                if latest_date:
                    if isinstance(latest_date, str):
                        latest_dt = datetime.strptime(latest_date[:10], '%Y-%m-%d')
                    else:
                        latest_dt = latest_date
                    curr_month_start = latest_dt.replace(day=1)
                    prev_month_start = (curr_month_start - timedelta(days=1)).replace(day=1)
                    
                    p_start = prev_month_start.strftime('%Y-%m-%d')
                    c_start = curr_month_start.strftime('%Y-%m-%d')
                    date_expr = f"s.{q_name(real_date_col)}"
                    q_att = f"""
                        SELECT COUNT(DISTINCT prev_cust) AS attrition_count
                        FROM (
                            SELECT DISTINCT s.{q_name(real_cust_col_sales)} AS prev_cust
                            FROM {sales_from}
                            WHERE {date_expr} >= %s AND {date_expr} < %s
                        ) p
                        WHERE prev_cust NOT IN (
                            SELECT DISTINCT s.{q_name(real_cust_col_sales)}
                            FROM {sales_from}
                            WHERE {date_expr} >= %s
                        )
                    """
                    execute_query(cursor, q_att, (p_start, c_start, c_start), is_hana=is_hana)
                    row = fetchone_dict(cursor)
                    if row and row.get("attrition_count") is not None:
                        attrition = str(row["attrition_count"])
            except Exception as e:
                print(f"[Default Metrics] Attrition query failed: {e}")

        # 10. Sales Target and Achievement
        sales_target_val = "N/A"
        achievement_val = "N/A"
        if target_tbl:
            try:
                target_col = check_col(table_columns[target_tbl], ["plan_value", "rev_plan_val", "plan_ho_val", "rev_plan", "org_plan", "value", "target", "target_value", "sales_target"])
                if target_col:
                    if is_hana:
                        execute_query(cursor, f"SELECT SUM(COALESCE({q_name(target_col)}, 0)) AS total_target FROM {q_tbl(target_tbl)}", is_hana=is_hana)
                    else:
                        execute_query(cursor, f"SELECT SUM(CAST(REPLACE(TRIM({q_name(target_col)}), ',', '') AS DECIMAL(18,2))) AS total_target FROM {q_tbl(target_tbl)}", is_hana=is_hana)
                    t_row = fetchone_dict(cursor)
                    if t_row and t_row.get("total_target") is not None:
                        t_val = float(t_row["total_target"])
                        if t_val > 0:
                            if t_val >= 10000000: sales_target_val = f"₹{(t_val / 10000000):.2f} Cr"
                            elif t_val >= 100000: sales_target_val = f"₹{(t_val / 100000):.2f} Lac"
                            else: sales_target_val = f"₹{t_val:,.2f}"

                            if total_revenue > 0:
                                ach = (total_revenue / t_val) * 100
                                achievement_val = f"{ach:.2f}%"
            except Exception as e:
                print(f"[Default Metrics] Sales target query failed: {e}")

        # 11. New Dealer
        new_dealer_val = "N/A"
        if cust_tbl:
            erdat_col = check_col(table_columns[cust_tbl], ["erdat", "created_date", "fs_bill_dt", "first_bill_date"])
            if erdat_col:
                try:
                    execute_query(cursor, f"SELECT COUNT(*) AS nd_count FROM {q_tbl(cust_tbl)} WHERE {q_name(erdat_col)} IS NOT NULL", is_hana=is_hana)
                    row = fetchone_dict(cursor)
                    if row and row.get("nd_count") is not None:
                        new_dealer_val = str(row["nd_count"])
                except Exception as e:
                    print(f"[Default Metrics] New dealer query failed: {e}")

        # Format total revenue
        formatted_revenue = f"₹{float(total_revenue):,.2f}"
        if total_revenue >= 10000000:
            formatted_revenue = f"₹{(total_revenue / 10000000):.2f} Cr"
        elif total_revenue >= 100000:
            formatted_revenue = f"₹{(total_revenue / 100000):.2f} Lac"

        metrics_data = {
            "metric_1": {
                "label": "Total Sales Revenue", 
                "value": formatted_revenue, 
                "subtext": "Lifetime revenue"
            },
            "metric_2": {
                "label": "Top Performing Tyre", 
                "value": str(top_tyre), 
                "subtext": "Highest revenue generator"
            },
            "metric_3": {
                "label": "Leading Region", 
                "value": str(leading_region), 
                "subtext": "Most profitable territory"
            },
            "metric_4": {
                "label": "Year-over-Year Growth", 
                "value": f"{yoy:+0.1f}%" if previous_year > 0 else ("0.0%" if current_year > 0 else "N/A"), 
                "subtext": yoy_subtext
            },
            "metric_5": {"label": "Achievement", "value": achievement_val, "subtext": "Current achievement"},
            "metric_6": {"label": "Sales Target", "value": sales_target_val, "subtext": "Target sales"},
            "metric_7": {"label": "Sales Actual", "value": formatted_revenue, "subtext": "Actual sales"},
            "metric_8": {"label": "SAS IN", "value": sas_in_val, "subtext": "SAS IN value"},
            "metric_9": {"label": "SAS Variance", "value": sas_variance_val, "subtext": "Variance"},
            "metric_10": {"label": "Billing Scope", "value": billing_scope, "subtext": "Customers billed"},
            "metric_11": {"label": "Dealer Spread", "value": dealer_spread, "subtext": "Active dealers"},
            "metric_12": {"label": "Overdue", "value": overdue_val, "subtext": "Outstanding balance"},
            "metric_13": {"label": "Exposure", "value": exposure_val, "subtext": "Credit exposure"},
            "metric_14": {"label": "Rotation", "value": "N/A", "subtext": "Rotation metric"},
            "metric_15": {"label": "New Dealer", "value": new_dealer_val, "subtext": "New dealers"},
            "metric_16": {"label": "Attrition", "value": attrition, "subtext": "Inactive from last month"}
        }

        return jsonify({"status": "success", "data": metrics_data}), 200

    except Exception as exc:
        print(f"[Default Metrics] Database query failed: {exc}")
        return jsonify({
            "status": "error",
            "message": "Database query failed.",
            "details": str(exc)
        }), 500

    finally:
        if cursor and is_hana:
            try: cursor.close()
            except Exception: pass
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass
        if cursor and not is_hana:
            try: cursor.close()
            except Exception: pass
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass