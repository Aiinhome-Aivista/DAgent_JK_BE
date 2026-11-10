"""
top_bottom_performance_controller.py
─────────────────────────────────────────────────────────────────────────────
Dedicated controller for Side-by-Side Top 10 and Bottom 10 Sales Performance.
Features:
1. Dynamic Dimensions:
   - Customers (Customer Master Name)
   - Dealers (Z001 Trade / Dealer network)
   - Territories (Territory Master Name)
   - Regions (Region Master Name)
   - Materials (SKU Master MAKTX / Title)
2. Rolling Days Window (2, 3, 4, 5, 6 days anchored to latest invoice_date)
3. Pre-calculation & Bulk Sync into MySQL table `dashboard_top_bottom_performance`
   for instant <5ms response times.
4. Fast API endpoint for POST /top-bottom-performance.
─────────────────────────────────────────────────────────────────────────────
"""

import time
from datetime import datetime, timezone, timedelta
from flask import request, jsonify
from utils.logger_service import log_system_error

TOP_BOTTOM_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_top_bottom_performance` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `dimension` VARCHAR(50) NOT NULL,
    `days_window` INT NOT NULL DEFAULT 2,
    `ranking_type` VARCHAR(10) NOT NULL,
    `rank_no` INT NOT NULL DEFAULT 1,
    `entity_name` VARCHAR(255) NOT NULL,
    `sales_value` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_tb_lookup` (`session_id`, `dimension`, `days_window`, `ranking_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_tb_table_initialized = False


def ensure_top_bottom_table(conn):
    """Ensures `dashboard_top_bottom_performance` exists."""
    global _tb_table_initialized
    if _tb_table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(TOP_BOTTOM_TABLE_SQL)
        conn.commit()
        cur.close()
        _tb_table_initialized = True
    except Exception as e:
        print(f"[Top/Bottom Table] init note: {e}", flush=True)


def _resolve_session(conn, session_id):
    if session_id and str(session_id).strip() not in ("", "null", "None"):
        return str(session_id).strip()
    try:
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("SELECT session_id FROM workspaces WHERE session_id IS NOT NULL AND session_id != '' ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row and isinstance(row, dict) and row.get("session_id"):
            return row["session_id"]
    except Exception:
        pass
    return "42d4f4e5-e66d-4c2a-9ad4-ea637e90fc91"


def _fetchall_dict(cursor):
    try:
        rows = cursor.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return rows
        if hasattr(cursor, 'description') and cursor.description:
            colnames = [d[0] for d in cursor.description]
            return [dict(zip(colnames, r)) for r in rows]
        return rows
    except Exception:
        return []


def _compute_top_bottom_slice_hana(cursor, schema, dimension: str, days: int):
    """
    Executes aggregation on SAP HANA views for a specific dimension & days window.
    Returns (top_10, bottom_10) tuples with {entity_name, sales_value, rank}.
    """
    sales_view = "ZPVF_SD_CurrentSales_01_TR"
    cust_view = "ZIVF_CustomerMaster_01_MD"
    terr_view = "ZIVF_TerritoryMaster_01_TR"
    reg_view = "ZIVF_RegionMaster_MD_01"
    sku_view = "ZIVR_SKUMaster_01_TR"

    day_offset = max(1, days) - 1

    joins = ""
    extra_filter = ""
    group_by = ""
    select_expr = ""

    if dimension == "Customers":
        entity_expr = f'COALESCE(NULLIF(TRIM(cm."NAME1"), \'\'), NULLIF(TRIM(cm."BNAME"), \'\'), TRIM(TO_VARCHAR(s."dealer_code")))'
        joins = f"""
            JOIN "{schema}"."{cust_view}" cm ON (
                s."dealer_code" = cm."KUNNR"
                OR LPAD(TRIM(TO_VARCHAR(s."dealer_code")), 10, '0') = cm."KUNNR"
                OR LTRIM(TO_VARCHAR(s."dealer_code"), '0') = LTRIM(cm."KUNNR", '0')
            )
        """
        extra_filter = 'AND (cm."KLABC" IS NULL OR cm."KLABC" <> \'X\')'
        select_expr = f'{entity_expr} AS entity_name'
        group_by = entity_expr
    elif dimension == "Dealers":
        entity_expr = f'COALESCE(NULLIF(TRIM(cm."NAME1"), \'\'), NULLIF(TRIM(cm."BNAME"), \'\'), TRIM(TO_VARCHAR(s."dealer_code")))'
        joins = f"""
            JOIN "{schema}"."{cust_view}" cm ON (
                s."dealer_code" = cm."KUNNR"
                OR LPAD(TRIM(TO_VARCHAR(s."dealer_code")), 10, '0') = cm."KUNNR"
                OR LTRIM(TO_VARCHAR(s."dealer_code"), '0') = LTRIM(cm."KUNNR", '0')
            )
        """
        extra_filter = 'AND (TRIM(cm."KTOKD") = \'Z001\' OR UPPER(TRIM(cm."CH_TYPE")) = \'DL\') AND (cm."KLABC" IS NULL OR cm."KLABC" <> \'X\')'
        select_expr = f'{entity_expr} AS entity_name'
        group_by = entity_expr
    elif dimension == "Territories":
        entity_expr = f'COALESCE(NULLIF(TRIM(tm."NAME"), \'\'), NULLIF(TRIM(tm."BZIRK"), \'\'), \'Unmapped Territory\')'
        joins = f"""
            JOIN "{schema}"."{cust_view}" cm ON (
                s."dealer_code" = cm."KUNNR"
                OR LPAD(TRIM(TO_VARCHAR(s."dealer_code")), 10, '0') = cm."KUNNR"
                OR LTRIM(TO_VARCHAR(s."dealer_code"), '0') = LTRIM(cm."KUNNR", '0')
            )
            JOIN "{schema}"."{terr_view}" tm ON (cm."BZIRK" = tm."BZIRK" OR TRIM(cm."BZIRK") = TRIM(tm."BZIRK"))
        """
        extra_filter = 'AND (cm."KLABC" IS NULL OR cm."KLABC" <> \'X\')'
        select_expr = f'{entity_expr} AS entity_name'
        group_by = entity_expr
    elif dimension == "Regions":
        entity_expr = f'COALESCE(NULLIF(TRIM(rm."region_name"), \'\'), NULLIF(TRIM(rm."region_code"), \'\'), \'Unmapped Region\')'
        joins = f"""
            JOIN "{schema}"."{cust_view}" cm ON (
                s."dealer_code" = cm."KUNNR"
                OR LPAD(TRIM(TO_VARCHAR(s."dealer_code")), 10, '0') = cm."KUNNR"
                OR LTRIM(TO_VARCHAR(s."dealer_code"), '0') = LTRIM(cm."KUNNR", '0')
            )
            JOIN "{schema}"."{terr_view}" tm ON (cm."BZIRK" = tm."BZIRK" OR TRIM(cm."BZIRK") = TRIM(tm."BZIRK"))
            JOIN "{schema}"."{reg_view}" rm ON (tm."REGIO" = rm."region_code" OR TRIM(tm."REGIO") = TRIM(rm."region_code"))
        """
        extra_filter = 'AND (cm."KLABC" IS NULL OR cm."KLABC" <> \'X\')'
        select_expr = f'{entity_expr} AS entity_name'
        group_by = entity_expr
    elif dimension in ("Tyres", "Tyre Types"):
        cat_view = "ZIVF_CategoryMaster_01_TR"
        entity_expr = f'COALESCE(NULLIF(TRIM(cat."CAT_DESC"), \'\'), NULLIF(TRIM(cat."SHORT_DESC"), \'\'), \'Unknown Tyre\')'
        joins = f"""
            JOIN "{schema}"."{sku_view}" sk ON TRIM(s."SKU") = TRIM(sk."MATNR")
            LEFT JOIN "{schema}"."{cat_view}" cat ON TRIM(sk."CAT_CODE") = TRIM(cat."CAT_CODE")
        """
        select_expr = f'{entity_expr} AS entity_name'
        group_by = entity_expr
    elif dimension == "Materials":
        entity_expr = f'COALESCE(NULLIF(TRIM(sk."MAKTX"), \'\'), NULLIF(TRIM(sk."PROD_TITLE"), \'\'), TRIM(s."SKU"))'
        joins = f"""
            JOIN "{schema}"."{sku_view}" sk ON TRIM(s."SKU") = TRIM(sk."MATNR")
        """
        select_expr = f'{entity_expr} AS entity_name'
        group_by = entity_expr
    else:
        return [], []

    sql = f"""
        SELECT 
            {select_expr},
            ROUND(SUM(COALESCE(s."invoice_value", 0)), 2) AS sales_value
        FROM "{schema}"."{sales_view}" s
        {joins}
        CROSS JOIN (
            SELECT MAX(s_max."invoice_date") AS max_dt 
            FROM "{schema}"."{sales_view}" s_max
            WHERE s_max."invoice_date" IS NOT NULL
              AND (s_max."distribution_channel" = '10' OR s_max."distribution_channel" = 10)
              AND (s_max."indicator_cancel" IS NULL OR s_max."indicator_cancel" NOT IN ('X', 'Y', '1'))
        ) md
        WHERE s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -{day_offset}) AND md.max_dt
          AND (s."distribution_channel" = '10' OR s."distribution_channel" = 10)
          AND (s."indicator_cancel" IS NULL OR s."indicator_cancel" NOT IN ('X', 'Y', '1'))
          {extra_filter}
        GROUP BY {group_by}
        HAVING SUM(COALESCE(s."invoice_value", 0)) > 0
        ORDER BY 2 DESC
    """

    cursor.execute(sql)
    rows = _fetchall_dict(cursor)
    if not rows:
        return [], []

    clean_rows = []
    for r in rows:
        name = r.get("entity_name") or r.get("ENTITY_NAME") or "Unknown"
        val = float(r.get("sales_value") or r.get("SALES_VALUE") or 0.0)
        clean_rows.append({"name": str(name).strip(), "sales_value": round(val, 2)})

    top_10 = [{"rank": i + 1, "name": item["name"], "sales_value": item["sales_value"]} for i, item in enumerate(clean_rows[:10])]
    
    # Bottom 10: lowest positive sales ascending (rank 1 = lowest)
    bottom_sorted = sorted(clean_rows, key=lambda x: x["sales_value"])[:10]
    bottom_10 = [{"rank": i + 1, "name": item["name"], "sales_value": item["sales_value"]} for i, item in enumerate(bottom_sorted)]

    return top_10, bottom_10


def _compute_top_bottom_slice_mysql(cursor, user_db: str, dimension: str, days: int):
    """Fallback query on local MySQL workspace tables if HANA is unavailable."""
    day_offset = max(1, days) - 1
    sales_tbl = f"`{user_db}`.`sales_fact`"
    cust_tbl = f"`{user_db}`.`customer_master`"
    terr_tbl = f"`{user_db}`.`territory_master`"
    reg_tbl = f"`{user_db}`.`region_master`"
    sku_tbl = f"`{user_db}`.`sku_master`"

    joins = ""
    extra_filter = ""
    group_by = ""
    select_expr = ""

    if dimension == "Customers":
        cust_name_expr = "COALESCE(NULLIF(TRIM(cm.NAME1), ''), CAST(sd.dealer_code AS CHAR))"
        joins = f"JOIN {cust_tbl} cm ON (sd.dealer_code = cm.KUNNR OR TRIM(LEADING '0' FROM CAST(sd.dealer_code AS CHAR)) = TRIM(LEADING '0' FROM CAST(cm.KUNNR AS CHAR)))"
        extra_filter = "AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')"
        select_expr = f"{cust_name_expr} AS entity_name"
        group_by = f"TRIM(LEADING '0' FROM CAST(sd.dealer_code AS CHAR)), {cust_name_expr}"
    elif dimension == "Dealers":
        cust_name_expr = "COALESCE(NULLIF(TRIM(cm.NAME1), ''), CAST(sd.dealer_code AS CHAR))"
        joins = f"JOIN {cust_tbl} cm ON (sd.dealer_code = cm.KUNNR OR TRIM(LEADING '0' FROM CAST(sd.dealer_code AS CHAR)) = TRIM(LEADING '0' FROM CAST(cm.KUNNR AS CHAR)))"
        extra_filter = "AND (TRIM(cm.KTOKD) = 'Z001' OR UPPER(TRIM(cm.CH_TYPE)) = 'DL') AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')"
        select_expr = f"{cust_name_expr} AS entity_name"
        group_by = f"TRIM(LEADING '0' FROM CAST(sd.dealer_code AS CHAR)), {cust_name_expr}"
    elif dimension == "Territories":
        entity_expr = "COALESCE(NULLIF(TRIM(tm.NAME), ''), NULLIF(TRIM(cm.BZIRK), ''), 'Unmapped Territory')"
        joins = f"""
            JOIN {cust_tbl} cm ON (sd.dealer_code = cm.KUNNR OR TRIM(LEADING '0' FROM CAST(sd.dealer_code AS CHAR)) = TRIM(LEADING '0' FROM CAST(cm.KUNNR AS CHAR)))
            JOIN {terr_tbl} tm ON cm.BZIRK = tm.BZIRK
        """
        extra_filter = "AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')"
        select_expr = f"{entity_expr} AS entity_name"
        group_by = entity_expr
    elif dimension == "Regions":
        entity_expr = "COALESCE(NULLIF(TRIM(rm.region_name), ''), NULLIF(TRIM(rm.region_code), ''), 'Unmapped Region')"
        joins = f"""
            JOIN {cust_tbl} cm ON (sd.dealer_code = cm.KUNNR OR TRIM(LEADING '0' FROM CAST(sd.dealer_code AS CHAR)) = TRIM(LEADING '0' FROM CAST(cm.KUNNR AS CHAR)))
            JOIN {terr_tbl} tm ON cm.BZIRK = tm.BZIRK
            JOIN {reg_tbl} rm ON tm.REGIO = rm.region_code
        """
        extra_filter = "AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')"
        select_expr = f"{entity_expr} AS entity_name"
        group_by = entity_expr
    elif dimension in ("Tyres", "Tyre Types"):
        entity_expr = "COALESCE(NULLIF(TRIM(sk.tyre_type), ''), 'Unknown Tyre')"
        joins = f"JOIN {sku_tbl} sk ON sd.SKU = sk.MATNR"
        select_expr = f"{entity_expr} AS entity_name"
        group_by = entity_expr
    elif dimension == "Materials":
        entity_expr = "COALESCE(NULLIF(TRIM(sk.MAKTX), ''), NULLIF(TRIM(sk.PROD_TITLE), ''), CAST(sd.SKU AS CHAR))"
        joins = f"JOIN {sku_tbl} sk ON sd.SKU = sk.MATNR"
        select_expr = f"{entity_expr} AS entity_name"
        group_by = entity_expr
    else:
        return [], []

    sql = f"""
        SELECT 
            {select_expr},
            SUM(sd.invoice_value) AS sales_value
        FROM {sales_tbl} sd
        {joins}
        CROSS JOIN (SELECT MAX(sd_max.invoice_date) AS max_dt FROM {sales_tbl} sd_max) md
        WHERE sd.invoice_date BETWEEN DATE_SUB(md.max_dt, INTERVAL {day_offset} DAY) AND md.max_dt
          AND CAST(sd.distribution_channel AS CHAR) = '10'
          AND (sd.indicator_cancel IS NULL OR sd.indicator_cancel NOT IN ('X', 'Y', '1'))
          {extra_filter}
        GROUP BY {group_by}
        HAVING SUM(sd.invoice_value) > 0
        ORDER BY sales_value DESC
    """

    cursor.execute(sql)
    rows = _fetchall_dict(cursor)
    if not rows:
        return [], []

    clean_rows = []
    for r in rows:
        name = r.get("entity_name") or "Unknown"
        val = float(r.get("sales_value") or 0.0)
        clean_rows.append({"name": str(name).strip(), "sales_value": round(val, 2)})

    top_10 = [{"rank": i + 1, "name": item["name"], "sales_value": item["sales_value"]} for i, item in enumerate(clean_rows[:10])]
    bottom_sorted = sorted(clean_rows, key=lambda x: x["sales_value"])[:10]
    bottom_10 = [{"rank": i + 1, "name": item["name"], "sales_value": item["sales_value"]} for i, item in enumerate(bottom_sorted)]

    return top_10, bottom_10
    bottom_10 = [{"rank": i + 1, "name": item["name"], "sales_value": item["sales_value"]} for i, item in enumerate(bottom_sorted)]

    return top_10, bottom_10


# ─────────────────────────────────────────────────────────────────────────────
# Bulk Sync Function (Called by Scheduler & On-Demand)
# ─────────────────────────────────────────────────────────────────────────────

SUPPORTED_DIMENSIONS = ["Customers", "Tyres", "Territories", "Regions", "Materials"]
SUPPORTED_DAYS = [2, 3, 4, 5, 6]


def bulk_sync_top_bottom_performance(mysql_conn, session_id: str, user_id=None):
    """
    Pre-calculates Top 10 and Bottom 10 across all supported Dimensions
    and Rolling Days Windows (2, 3, 4, 5, 6 days) and bulk inserts into
    MySQL table `dashboard_top_bottom_performance`.
    """
    if not mysql_conn or not session_id:
        return False

    ensure_top_bottom_table(mysql_conn)
    effective_session = _resolve_session(mysql_conn, session_id)

    from helper.sap_connection import get_sap_connection_for_session
    hana_conn = None
    tunnel = None
    is_hana = False
    sap_schema = "SAP_S4"
    user_db = None

    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, effective_session, user_id=user_id)
        if hana_conn:
            is_hana = True
            sap_schema = schema or "SAP_S4"
    except Exception as e:
        print(f"[Bulk Sync Top/Bottom] HANA conn note: {e}", flush=True)
        is_hana = False

    if not is_hana:
        try:
            m_cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            m_cur.execute("SELECT new_user_db FROM external_db_sync_log WHERE session_id=%s ORDER BY id DESC LIMIT 1", (effective_session,))
            s_row = m_cur.fetchone()
            if not s_row:
                m_cur.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s LIMIT 1", (effective_session,))
                s_row = m_cur.fetchone()
            if s_row:
                user_db = s_row.get("new_user_db") or s_row.get("workspace_db")
            m_cur.close()
        except Exception:
            pass

    records_to_insert = []
    source_cursor = hana_conn.cursor() if is_hana else (mysql_conn.cursor() if user_db else None)

    if not source_cursor:
        print("[Bulk Sync Top/Bottom] No data source available", flush=True)
        return False

    try:
        t0 = time.time()
        print(f"[Bulk Sync Top/Bottom] Starting sync for session={effective_session}, is_hana={is_hana}, schema={sap_schema}...", flush=True)
        for dim in SUPPORTED_DIMENSIONS:
            for d in SUPPORTED_DAYS:
                try:
                    if is_hana:
                        top_10, bottom_10 = _compute_top_bottom_slice_hana(source_cursor, sap_schema, dim, d)
                    else:
                        top_10, bottom_10 = _compute_top_bottom_slice_mysql(source_cursor, user_db, dim, d)

                    print(f"[Bulk Sync Top/Bottom] Slice {dim} ({d} days): {len(top_10)} top, {len(bottom_10)} bottom", flush=True)

                    for item in top_10:
                        records_to_insert.append((
                            effective_session, dim, d, "TOP", item["rank"], item["name"], item["sales_value"]
                        ))
                    for item in bottom_10:
                        records_to_insert.append((
                            effective_session, dim, d, "BOTTOM", item["rank"], item["name"], item["sales_value"]
                        ))
                except Exception as slice_err:
                    print(f"[Bulk Sync Top/Bottom] Error on {dim} ({d} days): {slice_err}", flush=True)
                    try:
                        log_system_error(session_id=effective_session, module_name="top_bottom_performance", error_msg=f"{dim} ({d} days): {slice_err}")
                    except Exception:
                        pass

        print(f"[Bulk Sync Top/Bottom] Total records collected across all slices: {len(records_to_insert)}", flush=True)
        if records_to_insert:
            m_cur = mysql_conn.cursor()
            # Clear old records for this session
            m_cur.execute("DELETE FROM `dashboard_top_bottom_performance` WHERE session_id = %s", (effective_session,))
            insert_sql = """
                INSERT INTO `dashboard_top_bottom_performance`
                (`session_id`, `dimension`, `days_window`, `ranking_type`, `rank_no`, `entity_name`, `sales_value`)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            m_cur.executemany(insert_sql, records_to_insert)
            mysql_conn.commit()
            m_cur.close()
            print(f"[Bulk Sync Top/Bottom] ✅ Saved {len(records_to_insert)} records in {round(time.time() - t0, 2)}s", flush=True)
            return True
        else:
            print(f"[Bulk Sync Top/Bottom] ⚠️ No records collected across 25 permutations!", flush=True)
    except Exception as e:
        print(f"[Bulk Sync Top/Bottom] Main execution error: {e}", flush=True)
        try:
            log_system_error(session_id=effective_session, module_name="top_bottom_performance", error_msg=f"Bulk sync: {e}")
        except Exception:
            pass
        return False
    finally:
        try:
            if is_hana and source_cursor:
                source_cursor.close()
            if hana_conn:
                hana_conn.close()
            if tunnel:
                tunnel.stop()
        except Exception:
            pass

    return False


# ─────────────────────────────────────────────────────────────────────────────
# API Controller Handler (POST /top-bottom-performance)
# ─────────────────────────────────────────────────────────────────────────────

def get_top_bottom_performance_controller(get_db_connection):
    """
    Handles POST /top-bottom-performance
    Payload:
    {
        "session_id": "...",
        "user_id": "...",
        "dimension": "Customers" | "Dealers" | "Territories" | "Regions" | "Materials",
        "days": 2 | 3 | 4 | 5 | 6,
        "force_refresh": false
    }
    """
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    user_id = body.get("user_id")
    dimension = body.get("dimension") or "Customers"
    days = int(body.get("days") or 2)
    force_refresh = bool(body.get("force_refresh", False))

    if dimension not in SUPPORTED_DIMENSIONS:
        dimension = "Customers"
    if days not in SUPPORTED_DAYS:
        days = 2

    mysql_conn = get_db_connection()
    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    ensure_top_bottom_table(mysql_conn)
    effective_session = _resolve_session(mysql_conn, session_id)

    try:
        # 1. Check pre-calculated cache table first (< 5ms response)
        if not force_refresh:
            cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            cur.execute("""
                SELECT ranking_type, rank_no, entity_name, sales_value
                FROM `dashboard_top_bottom_performance`
                WHERE session_id = %s AND dimension = %s AND days_window = %s
                ORDER BY rank_no ASC
            """, (effective_session, dimension, days))
            cached_rows = cur.fetchall()
            cur.close()

            if cached_rows:
                top_10 = []
                bottom_10 = []
                for r in cached_rows:
                    rtype = str(r.get("ranking_type", "")).upper()
                    item = {
                        "rank": int(r.get("rank_no", 1)),
                        "name": r.get("entity_name", "Unknown"),
                        "sales_value": float(r.get("sales_value", 0.0))
                    }
                    if rtype == "TOP":
                        top_10.append(item)
                    elif rtype == "BOTTOM":
                        bottom_10.append(item)

                if top_10 or bottom_10:
                    return jsonify({
                        "status": "success",
                        "source": "pre_calculated_cache",
                        "dimension": dimension,
                        "days": days,
                        "top_10": top_10,
                        "bottom_10": bottom_10
                    }), 200

        # 2. If not cached or force_refresh, calculate on the fly
        from helper.sap_connection import get_sap_connection_for_session
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, effective_session, user_id=user_id)
        
        top_10, bottom_10 = [], []
        if hana_conn:
            try:
                h_cur = hana_conn.cursor()
                top_10, bottom_10 = _compute_top_bottom_slice_hana(h_cur, schema or "SAP_S4", dimension, days)
                h_cur.close()
            finally:
                try: hana_conn.close()
                except Exception: pass
                if tunnel:
                    try: tunnel.stop()
                    except Exception: pass
        else:
            # Fallback to MySQL workspace
            m_cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            m_cur.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s LIMIT 1", (effective_session,))
            row = m_cur.fetchone()
            m_cur.close()
            if row and row.get("workspace_db"):
                src_cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
                top_10, bottom_10 = _compute_top_bottom_slice_mysql(src_cur, row["workspace_db"], dimension, days)
                src_cur.close()

        # Cache newly computed results into table
        if top_10 or bottom_10:
            try:
                c_cur = mysql_conn.cursor()
                c_cur.execute("""
                    DELETE FROM `dashboard_top_bottom_performance`
                    WHERE session_id = %s AND dimension = %s AND days_window = %s
                """, (effective_session, dimension, days))
                ins_sql = """
                    INSERT INTO `dashboard_top_bottom_performance`
                    (`session_id`, `dimension`, `days_window`, `ranking_type`, `rank_no`, `entity_name`, `sales_value`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                to_save = []
                for item in top_10:
                    to_save.append((effective_session, dimension, days, "TOP", item["rank"], item["name"], item["sales_value"]))
                for item in bottom_10:
                    to_save.append((effective_session, dimension, days, "BOTTOM", item["rank"], item["name"], item["sales_value"]))
                c_cur.executemany(ins_sql, to_save)
                mysql_conn.commit()
                c_cur.close()
            except Exception as save_err:
                print(f"[Top/Bottom Cache Save] note: {save_err}", flush=True)

        return jsonify({
            "status": "success",
            "source": "live_computed",
            "dimension": dimension,
            "days": days,
            "top_10": top_10,
            "bottom_10": bottom_10
        }), 200

    except Exception as e:
        print(f"[Top/Bottom Controller] Error: {e}", flush=True)
        try:
            log_system_error(session_id=effective_session, module_name="top_bottom_performance", error_msg=str(e))
        except Exception:
            pass
        return jsonify({"status": "error", "message": str(e), "top_10": [], "bottom_10": []}), 500
    finally:
        try: mysql_conn.close()
        except Exception: pass

