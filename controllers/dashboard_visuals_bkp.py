import json
from flask import request, jsonify
from model.llm_client import call_llm_chat
from database import config

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
        # Clean json if it contains markdown formatting
        if response.startswith("```json"):
            response = response.replace("```json", "").replace("```", "").strip()
        elif response.startswith("```"):
            response = response.replace("```", "").strip()
            
        metrics = json.loads(response)
        
        # Provide default fallback values if any metric is null or missing
        defaults = {
            "metric_1": {"label": "Metric 1", "value": "N/A", "subtext": "No data available"},
            "metric_2": {"label": "Metric 2", "value": "N/A", "subtext": "No data available"},
            "metric_3": {"label": "Metric 3", "value": "N/A", "subtext": "No data available"},
            "metric_4": {"label": "Metric 4", "value": "N/A", "subtext": "No data available"}
        }
        
        for key in defaults:
            if key not in metrics or not metrics[key] or metrics[key].get("value") is None:
                metrics[key] = defaults[key]
            else:
                # Ensure all required fields exist
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
   Format of each item: {{ "name": "TYRE_TYPE", "value": float }}

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
        # Clean json if it contains markdown formatting
        if response.startswith("```json"):
            response = response.replace("```json", "").replace("```", "").strip()
        elif response.startswith("```"):
            response = response.replace("```", "").strip()
            
        chart_data = json.loads(response)
        return jsonify({"status": "success", "data": chart_data}), 200
    except Exception as e:
        print(f"[Graph] Error extracting chart data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500




# default_dashboard_metrics_controller
def default_dashboard_metrics_controller(get_db_connection):
    """
    Fetches default summary metrics (total sales, top tyre, leading region, YoY growth)
    to populate the dashboard right-side panels when the page initially loads.
    Uses dynamic table mapping based on session_id from external_db_sync_log.
    """
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
        
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    cursor = None
    
    # Fallback default values
    metrics_data = {
        "metric_1": {"label": "Total Sales Revenue", "value": "N/A", "subtext": "No data available"},
        "metric_2": {"label": "Top Performing Tyre", "value": "N/A", "subtext": "No data available"},
        "metric_3": {"label": "Leading Region", "value": "N/A", "subtext": "No data available"},
        "metric_4": {"label": "Year-over-Year Growth", "value": "N/A", "subtext": "No data available"}
    }

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500
            
        cursor = conn.cursor(dictionary=True)

        # 1. Look up the dynamic table name and database from external_db_sync_log
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
        tbl_name = sync_row["table_name"]
        
        # Build the dynamic fully qualified table name `db`.`table`
        table_name = f"`{user_db}`.`{tbl_name}`"
        
        # Fetch all synced tables for this session to dynamically inspect schemas
        # We query the main database (config.MYSQL_CONFIG['database']) before switching to user_db
        cursor.execute(f"""
            SELECT DISTINCT table_name 
            FROM `{config.MYSQL_CONFIG['database']}`.`external_db_sync_log` 
            WHERE session_id = %s 
              AND new_user_db = %s
              AND table_name IS NOT NULL
        """, (session_id, user_db))
        session_tables = [r["table_name"] for r in cursor.fetchall()]

        # Switch to the user's database just to be safe, though fully qualified name works
        cursor.execute(f"USE `{user_db}`")

        table_columns = {}
        for tbl in session_tables:
            try:
                cursor.execute(f"DESCRIBE `{tbl}`")
                cols = cursor.fetchall()
                table_columns[tbl] = {col["Field"].lower(): col["Field"] for col in cols}
            except Exception as e:
                print(f"[Default Metrics] Describe table {tbl} failed: {e}")

        # Find the table that contains invoice_value
        sales_table = None
        for tbl, cols in table_columns.items():
            if "invoice_value" in cols:
                sales_table = tbl
                break

        # Fallback to tbl_name if no table with invoice_value is found
        if not sales_table:
            sales_table = tbl_name

        sales_cols = table_columns.get(sales_table, {})
        has_revenue = "invoice_value" in sales_cols

        if has_revenue:
            real_revenue_col = sales_cols["invoice_value"]
            revenue_expr = f"CAST(REPLACE(TRIM(`{real_revenue_col}`), ',', '') AS DECIMAL(18,2))"
            # For JOIN queries where the sales table has alias 's'
            join_revenue_expr = f"CAST(REPLACE(TRIM(s.`{real_revenue_col}`), ',', '') AS DECIMAL(18,2))"
        else:
            revenue_expr = "0.0"
            join_revenue_expr = "0.0"

        # 1. Total Sales Revenue
        total_revenue = 0.0
        if has_revenue:
            try:
                real_revenue_col = sales_cols["invoice_value"]
                cursor.execute(f"SELECT SUM({revenue_expr}) AS total_sales_revenue FROM `{sales_table}`")
                total_revenue = cursor.fetchone()["total_sales_revenue"] or 0
            except Exception as rev_err:
                print(f"[Default Metrics] Total revenue query failed: {rev_err}")

        # 2. Top Performing Tyre
        top_tyre = "N/A"
        if has_revenue:
            # Case A: tyre_type is in the sales_table
            if "tyre_type" in sales_cols:
                real_tyre_col = sales_cols["tyre_type"]
                try:
                    cursor.execute(f"""
                        SELECT
                            `{real_tyre_col}` AS tyre_type,
                            ROUND(SUM({revenue_expr}), 2) AS revenue
                        FROM `{sales_table}`
                        WHERE `{real_tyre_col}` IS NOT NULL
                          AND `{real_tyre_col}` <> ''
                        GROUP BY `{real_tyre_col}`
                        ORDER BY revenue DESC
                        LIMIT 1
                    """)
                    row = cursor.fetchone()
                    if row:
                        top_tyre = row["tyre_type"]
                except Exception as tyre_err:
                    print(f"[Default Metrics] Top tyre query failed: {tyre_err}")
            # Case B: tyre_type is in another table
            else:
                tyre_table = None
                for tbl, cols in table_columns.items():
                    if "tyre_type" in cols:
                        tyre_table = tbl
                        break
                if tyre_table:
                    # Find join column
                    common_cols = set(sales_cols.keys()) & set(table_columns[tyre_table].keys())
                    join_col = None
                    for pref in ["material", "customer", "plant", "id"]:
                        if pref in common_cols:
                            join_col = pref
                            break
                    if not join_col and common_cols:
                        join_col = list(common_cols)[0]

                    if join_col:
                        real_join_sales = sales_cols[join_col]
                        real_join_tyre = table_columns[tyre_table][join_col]
                        real_tyre_col = table_columns[tyre_table]["tyre_type"]
                        try:
                            cursor.execute(f"""
                                SELECT
                                    t.`{real_tyre_col}` AS tyre_type,
                                    ROUND(SUM({join_revenue_expr}), 2) AS revenue
                                FROM `{sales_table}` s
                                JOIN `{tyre_table}` t 
                                  ON s.`{real_join_sales}` = t.`{real_join_tyre}`
                                WHERE t.`{real_tyre_col}` IS NOT NULL
                                  AND t.`{real_tyre_col}` <> ''
                                GROUP BY t.`{real_tyre_col}`
                                ORDER BY revenue DESC
                                LIMIT 1
                            """)
                            row = cursor.fetchone()
                            if row:
                                top_tyre = row["tyre_type"]
                        except Exception as tyre_err:
                            print(f"[Default Metrics] Joined top tyre query failed: {tyre_err}")

        # 3. Leading Region
        leading_region = "N/A"
        if has_revenue:
            # Case A: region is in the sales_table
            if "region" in sales_cols:
                real_region_col = sales_cols["region"]
                try:
                    cursor.execute(f"""
                        SELECT
                            `{real_region_col}` AS region,
                            ROUND(SUM({revenue_expr}), 2) AS revenue
                        FROM `{sales_table}`
                        WHERE `{real_region_col}` IS NOT NULL
                          AND `{real_region_col}` <> ''
                        GROUP BY `{real_region_col}`
                        ORDER BY revenue DESC
                        LIMIT 1
                    """)
                    row = cursor.fetchone()
                    if row:
                        leading_region = row["region"]
                except Exception as reg_err:
                    print(f"[Default Metrics] Leading region query failed: {reg_err}")
            # Case B: region is in another table
            else:
                region_table = None
                for tbl, cols in table_columns.items():
                    if "region" in cols:
                        region_table = tbl
                        break
                if region_table:
                    # Find join column
                    common_cols = set(sales_cols.keys()) & set(table_columns[region_table].keys())
                    join_col = None
                    for pref in ["customer", "region", "zone", "id"]:
                        if pref in common_cols:
                            join_col = pref
                            break
                    if not join_col and common_cols:
                        join_col = list(common_cols)[0]

                    if join_col:
                        real_join_sales = sales_cols[join_col]
                        real_join_region = table_columns[region_table][join_col]
                        real_region_col = table_columns[region_table]["region"]
                        try:
                            cursor.execute(f"""
                                SELECT
                                    r.`{real_region_col}` AS region,
                                    ROUND(SUM({join_revenue_expr}), 2) AS revenue
                                FROM `{sales_table}` s
                                JOIN `{region_table}` r 
                                  ON s.`{real_join_sales}` = r.`{real_join_region}`
                                WHERE r.`{real_region_col}` IS NOT NULL
                                  AND r.`{real_region_col}` <> ''
                                GROUP BY r.`{real_region_col}`
                                ORDER BY revenue DESC
                                LIMIT 1
                            """)
                            row = cursor.fetchone()
                            if row:
                                leading_region = row["region"]
                        except Exception as reg_err:
                            print(f"[Default Metrics] Joined region query failed: {reg_err}")

        # 4. YoY Growth
        current_year = 0.0
        previous_year = 0.0
        yoy = 0.0
        if has_revenue and "invoice_date" in sales_cols:
            real_date_col = sales_cols["invoice_date"]
            try:
                # Detect column type to decide date parsing strategy
                cursor.execute(f"SHOW COLUMNS FROM `{sales_table}` LIKE %s", (real_date_col,))
                col_info = cursor.fetchone()
                col_type = col_info["Type"].lower() if col_info else "text"

                # If column is already date/datetime, use YEAR() directly
                # If it's text/varchar, parse with STR_TO_DATE
                if "date" in col_type or "timestamp" in col_type:
                    year_expr = f"YEAR(`{real_date_col}`)"
                    date_filter = f"`{real_date_col}` IS NOT NULL"
                else:
                    year_expr = f"YEAR(STR_TO_DATE(`{real_date_col}`, '%d-%m-%Y'))"
                    date_filter = f"`{real_date_col}` IS NOT NULL AND `{real_date_col}` != ''"

                # Current Year Revenue
                cursor.execute(f"""
                    SELECT ROUND(SUM({revenue_expr}), 2) AS revenue
                    FROM `{sales_table}`
                    WHERE {date_filter}
                      AND {year_expr} = YEAR(CURDATE())
                """)
                curr_row = cursor.fetchone()
                current_year = float(curr_row["revenue"] or 0) if curr_row else 0.0

                # Previous Year Revenue
                cursor.execute(f"""
                    SELECT ROUND(SUM({revenue_expr}), 2) AS revenue
                    FROM `{sales_table}`
                    WHERE {date_filter}
                      AND {year_expr} = YEAR(CURDATE()) - 1
                """)
                prev_row = cursor.fetchone()
                previous_year = float(prev_row["revenue"] or 0) if prev_row else 0.0

                if previous_year > 0:
                    yoy = round(((current_year - previous_year) / previous_year) * 100, 2)
            except Exception as yoy_err:
                print(f"[Default Metrics] YoY query failed: {yoy_err}")

        # Format total revenue smartly (Cr or Lacs)
        formatted_revenue = f"₹{float(total_revenue):,.2f}"
        if total_revenue >= 10000000:
            formatted_revenue = f"₹{(total_revenue / 10000000):.2f} Cr"
        elif total_revenue >= 100000:
            formatted_revenue = f"₹{(total_revenue / 100000):.2f} Lac"

        # Format output as dynamic metrics for frontend
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
                "value": f"{yoy}%", 
                "subtext": "Compared to last year"
            }
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
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()






import json
from flask import request, jsonify

def get_actual_column_name(cursor, table_name, expected_name):
    try:
        cursor.execute(f"SHOW COLUMNS FROM {table_name}")
        columns = [row['Field'] for row in cursor.fetchall()]
        
        target = expected_name.lower().replace('_', '').replace(' ', '')
        
        # 1. Exact match
        for col in columns:
            if col == expected_name: return f"`{col}`"
                
        # 2. Case insensitive match
        for col in columns:
            if col.lower() == expected_name.lower(): return f"`{col}`"
                
        # 3. Fuzzy match
        for col in columns:
            if col.lower().replace('_', '').replace(' ', '') == target: return f"`{col}`"
                
    except Exception as e:
        print(f"Error fetching columns for {table_name}: {e}")
        
    return None

def check_table_columns(cursor, table_name, required_columns):
    found_cols = {}
    for col in required_columns:
        actual_col = get_actual_column_name(cursor, table_name, col)
        if not actual_col:
            return None
        found_cols[col] = actual_col
    return found_cols

def get_best_table_for_session(cursor, session_id, required_columns):
    """Finds a table that contains ALL required columns. 
       First checks tables synced in this session, then falls back to any table in the user's database."""
    
    # Get the user_db for this session
    cursor.execute("""
        SELECT new_user_db 
        FROM external_db_sync_log 
        WHERE session_id=%s 
          AND new_user_db IS NOT NULL 
          AND new_user_db != ''
        ORDER BY id DESC LIMIT 1
    """, (session_id,))
    row = cursor.fetchone()
    if not row:
        return None, None, {}
        
    user_db = row["new_user_db"]
    
    # 1. Check tables synced in this session
    cursor.execute("""
        SELECT table_name 
        FROM external_db_sync_log 
        WHERE session_id=%s AND new_user_db=%s AND table_name IS NOT NULL
        ORDER BY id DESC
    """, (session_id, user_db))
    
    synced_tables = [r["table_name"] for r in cursor.fetchall()]
    
    for tbl_name in synced_tables:
        table_name = f"`{user_db}`.`{tbl_name}`"
        found_cols = check_table_columns(cursor, table_name, required_columns)
        if found_cols:
            return table_name, user_db, found_cols
            
    # 2. Fallback: Check ALL tables in the user_db
    try:
        cursor.execute(f"SHOW TABLES FROM `{user_db}`")
        all_tables = [list(r.values())[0] for r in cursor.fetchall()]
        
        for tbl_name in all_tables:
            if tbl_name in synced_tables:
                continue # already checked
            table_name = f"`{user_db}`.`{tbl_name}`"
            found_cols = check_table_columns(cursor, table_name, required_columns)
            if found_cols:
                return table_name, user_db, found_cols
    except Exception as e:
        print(f"Error scanning all tables in {user_db}: {e}")
            
    return None, None, {}

# tyre_sales_data_controller

def tyre_sales_data_controller(get_db_connection):
    data = request.json or {}
    session_id = data.get("session_id")
    user_id = data.get("user_id")
    question = data.get("question") # currently not used for SQL but required in payload

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    cursor = None
    chart_data = []

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500
            
        cursor = conn.cursor(dictionary=True)

        table_name, user_db, cols = get_best_table_for_session(cursor, session_id, ["tyre_type", "invoice_value"])

        if not table_name:
            return jsonify({"status": "success", "data": chart_data, "message": "Required metrics (tyre_type, invoice_value) not found in any table."}), 200

        cursor.execute(f"USE `{user_db}`")
        
        actual_tyre_type = cols["tyre_type"]
        actual_invoice_value = cols["invoice_value"]

        revenue_expr = f"""
            CAST(
                REPLACE(TRIM({actual_invoice_value}), ',', '')
                AS DECIMAL(18,2)
            )
        """

        # dummyTyreData
        try:
            cursor.execute(f"""
                SELECT
                    {actual_tyre_type} as category,
                    ROUND(SUM({revenue_expr}), 2) AS sales_value
                FROM {table_name}
                WHERE {actual_tyre_type} IS NOT NULL AND {actual_tyre_type} <> ''
                GROUP BY {actual_tyre_type}
                ORDER BY sales_value DESC
                LIMIT 10
            """)
            chart_data = cursor.fetchall()
            error_msg = ""
        except Exception as e:
            print(f"Skipping tyre_sales_data due to error: {e}")
            error_msg = str(e)
            
        visualizations = []
        if chart_data:
            visualizations.append({
                "data": chart_data,
                "seriesKey": "",
                "title": "Top 10 Tyre Types by Sales",
                "type": "horizontal_bar_chart",
                "xKey": "sales_value",
                "yKey": "tyre_type"
            })
            
        return jsonify({
            "status": "success" if not error_msg else "error", 
            "visualizations": visualizations,
            "message": "Data retrieved successfully. Missing metrics means the column doesn't exist in the current table." if not chart_data and not error_msg else "",
            "sql_error": error_msg
        }), 200 if not error_msg else 400

    except Exception as exc:
        print(f"[Tyre Sales Data] Database query failed: {exc}")
        return jsonify({
            "status": "error",
            "message": "Database query failed.",
            "details": str(exc)
        }), 500

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

#filter for sales by zone chart
def dashboard_filters_controller(get_db_connection):
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    cursor = None
    
    # Initialize empty dynamic filters
    filters_data = {
        "categories": [],
        "constructions": [],
        "tyreTypes": []
    }

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "success", **filters_data}), 200
            
        cursor = conn.cursor(dictionary=True)
        
        # Find user_db for session
        cursor.execute("""
            SELECT new_user_db 
            FROM external_db_sync_log 
            WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
            ORDER BY id DESC LIMIT 1
        """, (session_id,))
        row = cursor.fetchone()
        
        # Fallback: if session not found, use most recent synced db
        if not row:
            cursor.execute("""
                SELECT new_user_db 
                FROM external_db_sync_log 
                WHERE new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """)
            row = cursor.fetchone()
        
        table_name = None
        actual_cat = None
        actual_const = None
        actual_tyre = None

        if row:
            user_db = row["new_user_db"]
            cursor.execute(f"USE `{user_db}`")
            cursor.execute("SHOW TABLES")
            tables = [list(r.values())[0] for r in cursor.fetchall()]
            
            # Find the first existing table that has at least one relevant column
            for tbl in tables:
                t_name = f"`{user_db}`.`{tbl}`"
                cat = get_actual_column_name(cursor, t_name, "product_category") or \
                      get_actual_column_name(cursor, t_name, "category") or \
                      get_actual_column_name(cursor, t_name, "product_type")
                      
                const = get_actual_column_name(cursor, t_name, "construction_type") or \
                        get_actual_column_name(cursor, t_name, "construction")
                        
                tyre = get_actual_column_name(cursor, t_name, "tyre_type")
                
                if cat or const or tyre:
                    # Check if table has data
                    cursor.execute(f"SELECT 1 FROM {t_name} LIMIT 1")
                    if cursor.fetchone():
                        table_name = t_name
                        actual_cat = cat
                        actual_const = const
                        actual_tyre = tyre
                        break

            if actual_cat:
                cursor.execute(f"SELECT DISTINCT {actual_cat} as val FROM {table_name} WHERE {actual_cat} IS NOT NULL AND {actual_cat} != ''")
                res = [r["val"] for r in cursor.fetchall() if r["val"]]
                if res: filters_data["categories"] = res
                
            if actual_const:
                cursor.execute(f"SELECT DISTINCT {actual_const} as val FROM {table_name} WHERE {actual_const} IS NOT NULL AND {actual_const} != ''")
                res = [r["val"] for r in cursor.fetchall() if r["val"]]
                if res: filters_data["constructions"] = res
                
            if actual_tyre:
                cursor.execute(f"SELECT DISTINCT {actual_tyre} as val FROM {table_name} WHERE {actual_tyre} IS NOT NULL AND {actual_tyre} != ''")
                res = [r["val"] for r in cursor.fetchall() if r["val"]]
                if res: filters_data["tyreTypes"] = res
                
        # Debug info
        debug_info = {
            "table_found": table_name,
            "actual_cat": actual_cat if table_name else None,
            "actual_const": actual_const if table_name else None,
            "actual_tyre": actual_tyre if table_name else None,
        }
        if table_name:
            cursor.execute(f"SHOW COLUMNS FROM {table_name}")
            debug_info["all_columns"] = [r['Field'] for r in cursor.fetchall()]

        return jsonify({
            "status": "success",
            **filters_data
        }), 200

    except Exception as exc:
        print(f"[Dashboard Filters] query failed: {exc}")
        return jsonify({
            "status": "success",
            **filters_data
        }), 200
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

def sales_by_zone_data_controller(get_db_connection):
    data = request.json or {}
    session_id = data.get("session_id")
    
    # Try different possible keys for product category just in case
    product_type = data.get("product_type") or data.get("category") or data.get("product_category")
    construction_type = data.get("construction_type") or data.get("construction")
    tyre_type = data.get("tyre_type")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    conn = None
    cursor = None
    chart_data = []

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Failed to connect to database."}), 500
            
        cursor = conn.cursor(dictionary=True)

        # Try to find user_db for this session_id, fallback to latest entry
        cursor.execute("""
            SELECT new_user_db 
            FROM external_db_sync_log 
            WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
            ORDER BY id DESC LIMIT 1
        """, (session_id,))
        row = cursor.fetchone()
        
        # If session not found, use most recent synced db
        if not row:
            cursor.execute("""
                SELECT new_user_db 
                FROM external_db_sync_log 
                WHERE new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """)
            row = cursor.fetchone()
        
        table_name = None
        actual_zone = None
        actual_invoice_value = None

        if row:
            user_db = row["new_user_db"]
            cursor.execute(f"SHOW TABLES FROM `{user_db}`")
            tables = [list(r.values())[0] for r in cursor.fetchall()]
            
            # Find the table with zone+invoice_value AND most rows (most complete data)
            best_table = None
            best_zone = None
            best_inv = None
            best_row_count = -1

            for tbl in tables:
                t_name = f"`{user_db}`.`{tbl}`"
                # Try multiple variants for zone and invoice value
                zone_col = (get_actual_column_name(cursor, t_name, "Zone") or
                            get_actual_column_name(cursor, t_name, "zone"))
                inv_col = (get_actual_column_name(cursor, t_name, "Invoice_Value") or
                           get_actual_column_name(cursor, t_name, "invoice_value") or
                           get_actual_column_name(cursor, t_name, "Taxable_Value") or
                           get_actual_column_name(cursor, t_name, "taxable_value"))
                
                if zone_col and inv_col:
                    try:
                        cursor.execute(f"SELECT COUNT(*) as cnt FROM {t_name}")
                        cnt = cursor.fetchone()['cnt']
                        if cnt > best_row_count:
                            best_row_count = cnt
                            best_table = t_name
                            best_zone = zone_col
                            best_inv = inv_col
                    except Exception:
                        pass

            table_name = best_table
            actual_zone = best_zone
            actual_invoice_value = best_inv

        if not table_name:
            return jsonify({"status": "success", "data": chart_data, "message": "Required metrics (zone, invoice_value) not found in any table."}), 200

        where_clauses = [f"{actual_zone} IS NOT NULL", f"{actual_zone} <> ''"]
        params = []

        if product_type and product_type.lower() != 'all':
            actual_cat = (get_actual_column_name(cursor, table_name, "CATEGORY") or
                         get_actual_column_name(cursor, table_name, "product_category") or
                         get_actual_column_name(cursor, table_name, "category"))
            if actual_cat:
                where_clauses.append(f"{actual_cat} = %s")
                params.append(product_type)
                
        if construction_type and construction_type.lower() != 'all':
            actual_const = (get_actual_column_name(cursor, table_name, "CONSTRUCTION") or
                           get_actual_column_name(cursor, table_name, "construction_type") or
                           get_actual_column_name(cursor, table_name, "construction"))
            if actual_const:
                where_clauses.append(f"{actual_const} = %s")
                params.append(construction_type)

        if tyre_type and tyre_type.lower() != 'all':
            actual_tyre = (get_actual_column_name(cursor, table_name, "TYRE_TYPE") or
                          get_actual_column_name(cursor, table_name, "tyre_type"))
            if actual_tyre:
                where_clauses.append(f"{actual_tyre} = %s")
                params.append(tyre_type)

        where_sql = " AND ".join(where_clauses)

        try:
            query = f"""
                SELECT
                    {actual_zone} as zone,
                    ROUND(SUM({actual_invoice_value}), 2) AS sales_value
                FROM {table_name}
                WHERE {where_sql}
                GROUP BY {actual_zone}
                ORDER BY sales_value DESC
            """
            cursor.execute(query, tuple(params))
            results = cursor.fetchall()
            
            total_sales = sum(float(r['sales_value']) for r in results if r['sales_value'])
            
            for r in results:
                r['name'] = r['zone']
                r['value'] = float(r['sales_value']) if r['sales_value'] else 0.0
                if total_sales > 0:
                    r['percentage'] = round((r['value'] / total_sales) * 100, 1)
                else:
                    r['percentage'] = 0.0
                    
            chart_data = results
            error_msg = ""
        except Exception as e:
            print(f"Skipping sales_by_zone due to error: {e}")
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
        return jsonify({
            "status": "error",
            "message": "Database query failed.",
            "details": str(exc)
        }), 500

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

            