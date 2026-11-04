"""
sales_by_zone_dashboard_controller.py
─────────────────────────────────────────────────────────────────────────────
Dedicated controller for Sales by Zone (Pie Chart).
Handles:
1. Dynamic filter extraction:
   - Year: 2026, 2025, 2024 (exact same query as Year-wise Sales)
   - Month: 1-12 / JAN-DEC (exact same query from ZITR_DayAttribute_01_TR)
   - Tyre Type: Distinct TYRE_TYPE from ZIVR_SKUMaster_01_TR (user's exact SQL)
   - Construction Type: Distinct CONSTUCTION from ZIVR_SKUMaster_01_TR (user's exact SQL)
2. Permutation Pre-calculation and Bulk Sync into MySQL table `dashboard_sales_by_zone`:
   - Runs fast bulk aggregation from HANA (with memory-safe split queries).
   - Generates and rolls up all dimensional permutations in Python memory.
   - Saves to `dashboard_sales_by_zone` with instant < 5ms retrieval.
3. Fast API endpoint for `/sales-by-zone`:
   - Instant response from MySQL pre-computed table.
   - Dynamic on-demand fallback if slice not yet cached.
─────────────────────────────────────────────────────────────────────────────
"""

import time
import calendar
import collections
from datetime import datetime, timezone, timedelta
from flask import request, jsonify

# In-memory fast cache for filters (5 minutes TTL)
_filters_cache = {}
_CACHE_TTL = 300

ZONE_NAME_MAP = {
    'WZ': 'WEST',
    'EZ': 'EAST',
    'NZ': 'NORTH',
    'SZ': 'SOUTH',
    'TZ': 'South Zone - II',
    'CZ': 'Central Zone',
    'NP': 'Nepal',
    'WEST': 'WEST',
    'EAST': 'EAST',
    'NORTH': 'NORTH',
    'SOUTH': 'SOUTH',
    'SOUTH ZONE - II': 'South Zone - II',
    'CENTRAL ZONE': 'Central Zone',
    'NEPAL': 'Nepal'
}

MONTH_MAP_NUM_TO_LABEL = {
    1: 'JAN', 2: 'FEB', 3: 'MAR', 4: 'APR', 5: 'MAY', 6: 'JUN',
    7: 'JUL', 8: 'AUG', 9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DEC'
}

MONTH_MAP_LABEL_TO_NUM = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
    'JANUARY': 1, 'FEBRUARY': 2, 'MARCH': 3, 'APRIL': 4, 'MAY': 5, 'JUNE': 6,
    'JULY': 7, 'AUGUST': 8, 'SEPTEMBER': 9, 'OCTOBER': 10, 'NOVEMBER': 11, 'DECEMBER': 12
}


def _get_cache(key):
    if key in _filters_cache:
        data, ts = _filters_cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _filters_cache[key] = (data, time.time())


def fetchall_dict(cursor):
    """Safely fetch all rows from cursor as dicts."""
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
    except Exception as e:
        print(f"[Sales by Zone] fetchall_dict error: {e}", flush=True)
        return []


def _val(r, *keys, default=None):
    """Helper to get value from dict row with case-insensitive key lookup."""
    if not isinstance(r, dict):
        return default
    for k in keys:
        if k in r and r[k] is not None:
            return r[k]
        for rk, rv in r.items():
            if rk.lower() == k.lower() and rv is not None:
                return rv
    return default


def _resolve_session(conn, session_id):
    """Auto-resolves first available session_id from workspaces if missing."""
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


def _get_connection_context(get_db_connection, session_id_arg=None, user_id_arg=None):
    """Establishes MySQL & SAP HANA connections."""
    mysql_conn = get_db_connection()
    if not mysql_conn:
        return None, None, None, False, None, None, None

    effective_session = _resolve_session(mysql_conn, session_id_arg)
    from helper.sap_connection import get_sap_connection_for_session

    hana_conn = None
    tunnel = None
    is_hana = False
    sap_schema = "SAP_S4"
    user_db = None

    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, effective_session, user_id=user_id_arg)
        if hana_conn:
            is_hana = True
            sap_schema = schema or "SAP_S4"
    except Exception as e:
        print(f"[Sales by Zone] HANA connection note: {e}", flush=True)
        is_hana = False

    if not is_hana:
        try:
            m_cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            m_cur.execute("""
                SELECT new_user_db FROM external_db_sync_log
                WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """, (effective_session,))
            s_row = m_cur.fetchone()
            if not s_row:
                m_cur.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s LIMIT 1", (effective_session,))
                s_row = m_cur.fetchone()
            if s_row and isinstance(s_row, dict):
                user_db = s_row.get("new_user_db") or s_row.get("workspace_db")
            m_cur.close()
        except Exception:
            pass
        if not user_db:
            user_db = "jk_sales_workspace_db"

    return mysql_conn, hana_conn, tunnel, is_hana, sap_schema, user_db, effective_session


def _close_connections(mysql_conn, hana_conn, tunnel, cursor=None):
    """Safely closes all database connections."""
    if cursor:
        try:
            cursor.close()
        except Exception:
            pass
    if hana_conn:
        try:
            hana_conn.close()
        except Exception:
            pass
    if tunnel:
        try:
            tunnel.stop()
        except Exception:
            pass
    if mysql_conn:
        try:
            mysql_conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fetch Master Filter Options (Years, Months, Construction, Tyre Types)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_master_months(cursor, is_hana, schema, user_db):
    """Exact same query as Year-wise Sales Comparison."""
    months = []
    if is_hana:
        sql = f"""
            SELECT DISTINCT
                MONTH("Date_") AS MONTH_NO,
                CASE MONTH("Date_")
                    WHEN 1  THEN 'JAN'
                    WHEN 2  THEN 'FEB'
                    WHEN 3  THEN 'MAR'
                    WHEN 4  THEN 'APR'
                    WHEN 5  THEN 'MAY'
                    WHEN 6  THEN 'JUN'
                    WHEN 7  THEN 'JUL'
                    WHEN 8  THEN 'AUG'
                    WHEN 9  THEN 'SEP'
                    WHEN 10 THEN 'OCT'
                    WHEN 11 THEN 'NOV'
                    WHEN 12 THEN 'DEC'
                END AS MONTH_NAME
            FROM "{schema}"."ZITR_DayAttribute_01_TR"
            WHERE "Date_" IS NOT NULL
            ORDER BY MONTH_NO ASC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            m_no = _val(r, "month_no")
            m_name = _val(r, "month_name")
            if m_no:
                months.append({"label": str(m_name).strip(), "value": int(m_no)})
    else:
        sql = f"""
            SELECT DISTINCT
                MONTH(billing__doc_date) AS MONTH_NO,
                CASE MONTH(billing__doc_date)
                    WHEN 1  THEN 'JAN'
                    WHEN 2  THEN 'FEB'
                    WHEN 3  THEN 'MAR'
                    WHEN 4  THEN 'APR'
                    WHEN 5  THEN 'MAY'
                    WHEN 6  THEN 'JUN'
                    WHEN 7  THEN 'JUL'
                    WHEN 8  THEN 'AUG'
                    WHEN 9  THEN 'SEP'
                    WHEN 10 THEN 'OCT'
                    WHEN 11 THEN 'NOV'
                    WHEN 12 THEN 'DEC'
                END AS MONTH_NAME
            FROM `{user_db}`.`sales_data`
            WHERE billing__doc_date IS NOT NULL
            ORDER BY MONTH_NO ASC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            m_no = _val(r, "month_no")
            m_name = _val(r, "month_name")
            if m_no:
                months.append({"label": str(m_name).upper().strip(), "value": int(m_no)})

    if not months:
        for i in range(1, 13):
            months.append({"label": MONTH_MAP_NUM_TO_LABEL[i], "value": i})

    return [{"label": "ALL", "value": "ALL"}] + months


def fetch_master_years(cursor, is_hana, schema, user_db):
    """Exact same query as Year-wise Sales Comparison (last 3 active years)."""
    years = []
    if is_hana:
        sql = f"""
            SELECT DISTINCT
                YEAR("invoice_date") AS SALES_YEAR
            FROM (
                SELECT "invoice_date"
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
                UNION ALL
                SELECT "invoice_date"
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
            ) S
            WHERE "invoice_date" >= ADD_YEARS(CURRENT_DATE, -2)
              AND "invoice_date" <= CURRENT_DATE
            ORDER BY SALES_YEAR DESC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            y = _val(r, "sales_year", "year")
            if y:
                years.append({"label": str(y).strip(), "value": int(y)})
    else:
        sql = f"""
            SELECT DISTINCT YEAR(billing__doc_date) AS SALES_YEAR
            FROM `{user_db}`.`sales_data`
            WHERE billing__doc_date IS NOT NULL
            ORDER BY SALES_YEAR DESC
            LIMIT 3
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            y = _val(r, "sales_year", "year")
            if y:
                years.append({"label": str(y).strip(), "value": int(y)})

    if not years:
        years = [{"label": "2026", "value": 2026}, {"label": "2025", "value": 2025}, {"label": "2024", "value": 2024}]

    return [{"label": "ALL", "value": "ALL"}] + years


def fetch_master_tyre_types(cursor, is_hana, schema, user_db):
    """Category master descriptions with code as value."""
    tyre_types = []
    if is_hana:
        sql = f"""
            SELECT DISTINCT
                TRIM(cat."CAT_CODE") AS CAT_CODE,
                TRIM(cat."CAT_DESC") AS CAT_DESC,
                cat."SEQ_NO"
            FROM "{schema}"."ZIVF_CategoryMaster_01_TR" cat
            WHERE cat."CAT_DESC" IS NOT NULL
              AND TRIM(cat."CAT_DESC") <> ''
              AND cat."CAT_CODE" NOT IN ('TR', 'NT', 'AL')
            ORDER BY cat."SEQ_NO" ASC, CAT_DESC ASC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            cd = _val(r, "cat_desc")
            cc = _val(r, "cat_code")
            if cd and cc:
                tyre_types.append({"label": str(cd).strip(), "value": str(cc).strip()})
    else:
        try:
            sql = f"""
                SELECT DISTINCT TRIM(tyre_type) as tyre_type
                FROM `{user_db}`.`sales_data`
                WHERE tyre_type IS NOT NULL AND tyre_type <> ''
                ORDER BY tyre_type
            """
            cursor.execute(sql)
            for r in fetchall_dict(cursor):
                tt = _val(r, "tyre_type")
                if tt and str(tt).strip():
                    val_str = str(tt).strip()
                    tyre_types.append({"label": val_str, "value": val_str})
        except Exception:
            pass

    return [{"label": "ALL", "value": "ALL"}] + tyre_types


def fetch_master_construction_types(cursor, is_hana, schema, user_db):
    """User's exact query for Distinct CONSTUCTION from ZIVR_SKUMaster_01_TR."""
    constructions = []
    if is_hana:
        sql = f"""
            SELECT DISTINCT
                TRIM(M."CONSTUCTION") AS CONSTRUCTION_TYPE
            FROM (
                SELECT "SKU"
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
                WHERE "distribution_channel" = '10'

                UNION

                SELECT "SKU"
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
                WHERE "distribution_channel" = '10'
            ) S
            INNER JOIN "{schema}"."ZIVR_SKUMaster_01_TR" M
                ON TRIM(S."SKU") = TRIM(M."MATNR")
            WHERE M."CONSTUCTION" IS NOT NULL
              AND TRIM(M."CONSTUCTION") <> ''
            ORDER BY CONSTRUCTION_TYPE
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            ct = _val(r, "construction_type")
            if ct and str(ct).strip():
                val_str = str(ct).strip()
                constructions.append({"label": val_str, "value": val_str})
    else:
        try:
            sql = f"""
                SELECT DISTINCT TRIM(constuction) as construction_type
                FROM `{user_db}`.`sales_data`
                WHERE constuction IS NOT NULL AND constuction <> ''
                ORDER BY construction_type
            """
            cursor.execute(sql)
            for r in fetchall_dict(cursor):
                ct = _val(r, "construction_type")
                if ct and str(ct).strip():
                    val_str = str(ct).strip()
                    constructions.append({"label": val_str, "value": val_str})
        except Exception:
            pass

    return [{"label": "ALL", "value": "ALL"}] + constructions


def get_sales_by_zone_filters_controller(get_db_connection):
    """
    Returns dynamic filters for Sales by Zone (and dashboard-filters endpoint).
    Returns: { status, categories, constructions, tyreTypes, years, months }
    """
    session_id = request.args.get("session_id", "").strip()
    user_id = request.args.get("user_id")

    cache_key = f"sbz_filters_{session_id}"
    cached = _get_cache(cache_key)
    if cached:
        return jsonify(cached), 200

    mysql_conn, hana_conn, tunnel, is_hana, schema, user_db, effective_session = \
        _get_connection_context(get_db_connection, session_id, user_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Failed to connect to database"}), 500

    try:
        cursor = hana_conn.cursor() if is_hana else mysql_conn.cursor()

        years = fetch_master_years(cursor, is_hana, schema, user_db)
        months = fetch_master_months(cursor, is_hana, schema, user_db)
        tyre_types = fetch_master_tyre_types(cursor, is_hana, schema, user_db)
        constructions = fetch_master_construction_types(cursor, is_hana, schema, user_db)

        # Categories for general dashboard compatibility
        categories = [{"label": "ALL", "value": "ALL"}]

        resp_data = {
            "status": "success",
            "categories": categories,
            "constructions": constructions,
            "tyreTypes": tyre_types,
            "years": years,
            "months": months
        }

        _set_cache(cache_key, resp_data)
        return jsonify(resp_data), 200

    except Exception as e:
        print(f"[Sales by Zone Filters] Error: {e}", flush=True)
        return jsonify({
            "status": "error",
            "message": str(e),
            "years": [{"label": "ALL", "value": "ALL"}, {"label": "2026", "value": 2026}, {"label": "2025", "value": 2025}, {"label": "2024", "value": 2024}],
            "months": [{"label": "ALL", "value": "ALL"}],
            "constructions": [{"label": "ALL", "value": "ALL"}],
            "tyreTypes": [{"label": "ALL", "value": "ALL"}]
        }), 200
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel, cursor if 'cursor' in locals() else None)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Table Schema Initialization
# ─────────────────────────────────────────────────────────────────────────────

SALES_BY_ZONE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_sales_by_zone` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `year_val` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `month_val` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `construction_type` VARCHAR(100) NOT NULL DEFAULT 'ALL',
    `tyre_type` VARCHAR(100) NOT NULL DEFAULT 'ALL',
    `zone_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `zone_name` VARCHAR(100) NOT NULL,
    `sales_value` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `percentage` DECIMAL(7, 4) NOT NULL DEFAULT 0.0000,
    `quantity` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_sbz_lookup` (`session_id`, `year_val`, `month_val`, `construction_type`, `tyre_type`),
    INDEX `idx_sbz_zone` (`zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_table_initialized = False

def ensure_sales_by_zone_table(conn):
    """Ensures `dashboard_sales_by_zone` table exists with all required permutation columns."""
    global _table_initialized
    if _table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(SALES_BY_ZONE_TABLE_SQL)
        # Verify / alter columns if upgrading older table version
        for col_def in [
            ("year_val", "VARCHAR(20) NOT NULL DEFAULT 'ALL'"),
            ("month_val", "VARCHAR(20) NOT NULL DEFAULT 'ALL'"),
            ("construction_type", "VARCHAR(100) NOT NULL DEFAULT 'ALL'"),
            ("tyre_type", "VARCHAR(100) NOT NULL DEFAULT 'ALL'"),
            ("zone_code", "VARCHAR(50) NOT NULL DEFAULT 'ALL'"),
            ("zone_name", "VARCHAR(100) NOT NULL DEFAULT ''"),
            ("sales_value", "DECIMAL(18, 2) NOT NULL DEFAULT 0.00"),
            ("percentage", "DECIMAL(7, 4) NOT NULL DEFAULT 0.0000"),
            ("quantity", "DECIMAL(18, 2) NOT NULL DEFAULT 0.00")
        ]:
            try:
                cur.execute(f"ALTER TABLE `dashboard_sales_by_zone` ADD COLUMN `{col_def[0]}` {col_def[1]}")
            except Exception:
                pass

        try:
            cur.execute("ALTER TABLE `dashboard_sales_by_zone` ADD INDEX `idx_sbz_lookup` (`session_id`, `year_val`, `month_val`, `construction_type`, `tyre_type`)")
        except Exception:
            pass

        conn.commit()
        cur.close()
        try:
            cur.execute("DELETE FROM `dashboard_sales_by_zone` WHERE zone_code = 'UNKNOWN' OR zone_name = 'UNKNOWN'")
            conn.commit()
        except Exception:
            pass
        _table_initialized = True
    except Exception as e:
        print(f"[Sales by Zone Table] init: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Bulk Sync Permutations (For Scheduler & Sync Now Button)
# ─────────────────────────────────────────────────────────────────────────────

def bulk_sync_sales_by_zone_permutations(mysql_conn, session_id: str, user_id=None):
    """
    Pre-calculates all Sales by Zone slices across (Year, Month, Construction, Tyre Type)
    and stores them cleanly in MySQL table `dashboard_sales_by_zone`.
    Uses memory-safe split queries to avoid HANA memory limits, and in-memory aggregation.
    """
    if not mysql_conn or not session_id:
        return False

    from helper.dashboard_cache_service import get_now_ist
    ist_now = get_now_ist()
    ensure_sales_by_zone_table(mysql_conn)

    effective_session = session_id
    from helper.sap_connection import get_sap_connection_for_session
    hana_conn = None
    tunnel = None
    is_hana = False
    sap_schema = "SAP_S4"
    user_db = None

    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id=user_id)
        if hana_conn:
            is_hana = True
            sap_schema = schema or "SAP_S4"
    except Exception as e:
        print(f"[Bulk Sync Sales by Zone] HANA note: {e}", flush=True)
        is_hana = False

    if not is_hana:
        try:
            m_cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            m_cur.execute("""
                SELECT new_user_db FROM external_db_sync_log
                WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            s_row = m_cur.fetchone()
            if not s_row:
                m_cur.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s LIMIT 1", (session_id,))
                s_row = m_cur.fetchone()
            if s_row and isinstance(s_row, dict):
                user_db = s_row.get("new_user_db") or s_row.get("workspace_db")
            m_cur.close()
        except Exception:
            pass
        if not user_db:
            user_db = "jk_sales_workspace_db"

    cursor = None
    raw_rows = []

    try:
        if is_hana:
            cursor = hana_conn.cursor()

            # Execute 2 fast separate queries for CurrentSales and SalesHistory (>= 2024)
            # This completely avoids HANA UNION memory limits!
            base_query_template = """
                SELECT
                    zm."P_ZONE" AS ZONE_CODE,
                    COALESCE(NULLIF(TRIM(zm."NAME"), ''), zm."P_ZONE") AS ZONE_NAME,
                    YEAR(s."invoice_date") AS SALES_YEAR,
                    MONTH(s."invoice_date") AS MONTH_NO,
                    COALESCE(NULLIF(TRIM(sku."CONSTUCTION"), ''), 'UNKNOWN') AS CONSTRUCTION_TYPE,
                    COALESCE(NULLIF(TRIM(cat."CAT_CODE"), ''), NULLIF(TRIM(sku."TYRE_TYPE"), ''), 'UNKNOWN') AS TYRE_TYPE,
                    COALESCE(NULLIF(TRIM(cat."CAT_DESC"), ''), 'UNKNOWN') AS TYRE_DESC,
                    ROUND(SUM(COALESCE(s."invoice_value", s."ndp_value", s."taxable_value", 0)), 2) AS TOTAL_SALES,
                    ROUND(SUM(COALESCE(s."Tyre_Quantity", 0)), 2) AS TOTAL_QUANTITY
                FROM "{schema}"."{table_name}" s
                INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
                    ON (s."dealer_code" = cm."KUNNR" OR LPAD(TRIM(s."dealer_code"), 10, '0') = cm."KUNNR" OR LTRIM(s."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
                LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant 
                    ON (s."plant" = tm_plant."WERKS" OR cm."VWERK" = tm_plant."WERKS")
                LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk 
                    ON (cm."BZIRK" = tm_bzirk."BZIRK")
                LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
                    ON (rm."region_code" = COALESCE(NULLIF(TRIM(tm_plant."REGIO"), ''), NULLIF(TRIM(tm_bzirk."REGIO"), ''), NULLIF(TRIM(TO_VARCHAR(cm."REGIO")), '')))
                INNER JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm 
                    ON (zm."P_ZONE" = COALESCE(
                        NULLIF(TRIM(tm_plant."P_ZONE"), ''),
                        NULLIF(TRIM(tm_bzirk."P_ZONE"), ''),
                        NULLIF(TRIM(rm."zone_code"), ''),
                        CASE 
                            WHEN SUBSTRING(TO_VARCHAR(s."plant"), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                            WHEN SUBSTRING(TO_VARCHAR(s."plant"), 1, 1) = '3' THEN 'NZ'
                            WHEN SUBSTRING(TO_VARCHAR(s."plant"), 1, 1) = '1' THEN 'EZ'
                            WHEN SUBSTRING(TO_VARCHAR(s."plant"), 1, 1) = '8' THEN 'NP'
                            WHEN SUBSTRING(TO_VARCHAR(s."plant"), 1, 2) IN ('51', '52') THEN 'TZ'
                            WHEN SUBSTRING(TO_VARCHAR(s."plant"), 1, 1) = '5' THEN 'SZ'
                            WHEN SUBSTRING(TO_VARCHAR(s."plant"), 1, 1) = '7' THEN 'WZ'
                            ELSE NULL
                        END
                    ))
                LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sku 
                    ON (TRIM(s."SKU") = TRIM(sku."MATNR"))
                LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat 
                    ON (TRIM(sku."CAT_CODE") = TRIM(cat."CAT_CODE"))
                WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
                  AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
                  AND IFNULL(cm."KLABC", '') <> 'X'
                  AND s."invoice_date" IS NOT NULL
                  AND zm."P_ZONE" IS NOT NULL AND TRIM(zm."P_ZONE") <> ''
                  {extra_filter}
                GROUP BY 
                    zm."P_ZONE",
                    COALESCE(NULLIF(TRIM(zm."NAME"), ''), zm."P_ZONE"),
                    YEAR(s."invoice_date"),
                    MONTH(s."invoice_date"),
                    COALESCE(NULLIF(TRIM(sku."CONSTUCTION"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(cat."CAT_CODE"), ''), NULLIF(TRIM(sku."TYRE_TYPE"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(cat."CAT_DESC"), ''), 'UNKNOWN')
            """

            # 1. CurrentSales (2026 active)
            sql_curr = base_query_template.format(
                schema=sap_schema,
                table_name="ZPVF_SD_CurrentSales_01_TR",
                extra_filter="AND s.\"invoice_date\" >= '2024-01-01'"
            )
            try:
                cursor.execute(sql_curr)
                raw_rows.extend(fetchall_dict(cursor))
            except Exception as e:
                print(f"[Bulk Sync Sales by Zone] CurrentSales query warning: {e}", flush=True)

            # 2. SalesHistory (2024 - 2025)
            sql_hist = base_query_template.format(
                schema=sap_schema,
                table_name="ZPVF_SD_SalesHistory_01_TR",
                extra_filter="AND s.\"invoice_date\" >= '2024-01-01'"
            )
            try:
                cursor.execute(sql_hist)
                raw_rows.extend(fetchall_dict(cursor))
            except Exception as e:
                print(f"[Bulk Sync Sales by Zone] SalesHistory query warning: {e}", flush=True)

        else:
            cursor = mysql_conn.cursor()
            mysql_sql = f"""
                SELECT
                    COALESCE(NULLIF(TRIM(rm.zone), ''), 'UNKNOWN') AS ZONE_CODE,
                    COALESCE(NULLIF(TRIM(rm.zone), ''), 'UNKNOWN') AS ZONE_NAME,
                    YEAR(s.billing__doc_date) AS SALES_YEAR,
                    MONTH(s.billing__doc_date) AS MONTH_NO,
                    COALESCE(NULLIF(TRIM(s.constuction), ''), 'UNKNOWN') AS CONSTRUCTION_TYPE,
                    COALESCE(NULLIF(TRIM(s.tyre_type), ''), 'UNKNOWN') AS TYRE_TYPE,
                    ROUND(SUM(COALESCE(s.net__value, 0)), 2) AS TOTAL_SALES,
                    ROUND(SUM(COALESCE(s.billing_qty_in_sku, 0)), 2) AS TOTAL_QUANTITY
                FROM `{user_db}`.`sales_data` s
                JOIN `{user_db}`.`customer_master` cm ON (s.customer = cm.kunnr)
                LEFT JOIN `{user_db}`.`region_master` rm ON (rm.region = cm.sales_office)
                WHERE (s.distribution__channel = '10' OR s.distribution__channel = 10)
                  AND s.billing__doc_date IS NOT NULL
                  AND s.billing__doc_date >= '2024-01-01'
                  AND IFNULL(cm.klabc, '') <> 'X'
                GROUP BY
                    COALESCE(NULLIF(TRIM(rm.zone), ''), 'UNKNOWN'),
                    YEAR(s.billing__doc_date),
                    MONTH(s.billing__doc_date),
                    COALESCE(NULLIF(TRIM(s.constuction), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(s.tyre_type), ''), 'UNKNOWN')
            """
            cursor.execute(mysql_sql)
            raw_rows = fetchall_dict(cursor)

    except Exception as e:
        print(f"[Bulk Sync Sales by Zone] Extraction error: {e}", flush=True)
        return False
    finally:
        if is_hana:
            if cursor:
                try: cursor.close()
                except Exception: pass
            if hana_conn:
                try: hana_conn.close()
                except Exception: pass
            if tunnel:
                try: tunnel.stop()
                except Exception: pass

    if not raw_rows:
        print("[Bulk Sync Sales by Zone] No raw sales records found to sync.", flush=True)
        return False

    print(f"[Bulk Sync Sales by Zone] Fetched {len(raw_rows)} base aggregates. Computing dimensional rollups in memory...", flush=True)

    # In-memory aggregation:
    # slice_key = (year_val, month_val, construction_type, tyre_type)
    # slice_zones[slice_key][(zone_code, zone_name)] = {'sales': float, 'qty': float}
    # slice_totals[slice_key] = float (total sales for slice to calculate percentage)
    slice_zones = collections.defaultdict(lambda: collections.defaultdict(lambda: {'sales': 0.0, 'qty': 0.0}))
    slice_totals = collections.defaultdict(float)

    for r in raw_rows:
        zc = _val(r, "zone_code")
        if not zc or str(zc).strip().upper() == "UNKNOWN":
            continue
        zn = _val(r, "zone_name", default=zc)
        zc = str(zc).strip().upper() if zc else "UNKNOWN"
        zn = str(zn).strip() if zn else zc
        zn = ZONE_NAME_MAP.get(zc, ZONE_NAME_MAP.get(zn.upper(), zn))

        s_yr = _val(r, "sales_year")
        m_no = _val(r, "month_no")
        c_tp = _val(r, "construction_type", default="UNKNOWN")
        t_tp = _val(r, "tyre_type", default="UNKNOWN")
        t_desc = _val(r, "tyre_desc", default=t_tp)
        t_desc_str = str(t_desc).strip().upper() if t_desc else "UNKNOWN"

        sales = float(_val(r, "total_sales", default=0.0) or 0.0)
        qty = float(_val(r, "total_quantity", default=0.0) or 0.0)

        y_str = str(s_yr).strip() if s_yr else "UNKNOWN"
        m_str = str(m_no).strip() if m_no else "UNKNOWN"
        c_str = str(c_tp).strip().upper() if c_tp else "UNKNOWN"
        t_str = str(t_tp).strip().upper() if t_tp else "UNKNOWN"

        # Expand permutations across 4 dimensions:
        # Year: specific year, ALL
        # Month: specific month number, ALL
        # Construction: specific construction, ALL
        # Tyre Type: specific tyre type, ALL
        for y_dim in (y_str, "ALL"):
            for m_dim in (m_str, "ALL"):
                for c_dim in (c_str, "ALL"):
                    for t_dim in set((t_str, t_desc_str, "ALL")):
                        k = (y_dim, m_dim, c_dim, t_dim)
                        slice_zones[k][(zc, zn)]['sales'] += sales
                        slice_zones[k][(zc, zn)]['qty'] += qty
                        slice_totals[k] += sales

    # Build bulk insert batch
    batch_rows = []
    for (y_val, m_val, c_val, t_val), zones_dict in slice_zones.items():
        total_s = slice_totals[(y_val, m_val, c_val, t_val)]
        for (zc, zn), vals in zones_dict.items():
            sv = round(vals['sales'], 2)
            qty = round(vals['qty'], 2)
            pct = round((sv / total_s) * 100, 4) if total_s > 0 else 0.0000

            batch_rows.append((
                effective_session,
                y_val,
                m_val,
                c_val,
                t_val,
                zc,
                zn,
                sv,
                pct,
                qty,
                ist_now,
                ist_now
            ))

    print(f"[Bulk Sync Sales by Zone] Generated {len(batch_rows)} permutation rows. Writing to MySQL...", flush=True)

    # Fresh clean write: DELETE existing rows for session, then INSERT
    m_cur = None
    try:
        m_cur = mysql_conn.cursor()
        m_cur.execute("DELETE FROM `dashboard_sales_by_zone` WHERE session_id = %s", (effective_session,))

        insert_sql = """
            INSERT INTO `dashboard_sales_by_zone`
                (`session_id`, `year_val`, `month_val`, `construction_type`, `tyre_type`,
                 `zone_code`, `zone_name`, `sales_value`, `percentage`, `quantity`,
                 `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        chunk_size = 5000
        for i in range(0, len(batch_rows), chunk_size):
            chunk = batch_rows[i:i + chunk_size]
            m_cur.executemany(insert_sql, chunk)

        mysql_conn.commit()
        print(f"[Bulk Sync Sales by Zone] ✅ Successfully committed {len(batch_rows)} rows to `dashboard_sales_by_zone`.", flush=True)
        return True

    except Exception as e:
        print(f"[Bulk Sync Sales by Zone] MySQL insert error: {e}", flush=True)
        try:
            mysql_conn.rollback()
        except Exception:
            pass
        return False
    finally:
        if m_cur:
            try: m_cur.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 4. Data Retrieval Controller for /sales-by-zone
# ─────────────────────────────────────────────────────────────────────────────

def normalize_filter_val(val):
    if val is None:
        return "ALL"
    if isinstance(val, list):
        if not val or len(val) == 0:
            return "ALL"
        val = val[0]
    s = str(val).strip()
    if not s or s.lower() in ("all", "none", "null"):
        return "ALL"
    return s


def normalize_month_val(val):
    raw = normalize_filter_val(val)
    if raw == "ALL":
        return "ALL"
    if raw.isdigit():
        return str(int(raw))
    up = raw.upper()
    if up in MONTH_MAP_LABEL_TO_NUM:
        return str(MONTH_MAP_LABEL_TO_NUM[up])
    return "ALL"


def get_sales_by_zone_data_controller(get_db_connection):
    """
    Main controller for POST /sales-by-zone.
    Reads pre-calculated slice from `dashboard_sales_by_zone` in < 5ms.
    """
    data = request.json or {}
    session_id = data.get("session_id")
    user_id = data.get("user_id")

    # Extract 4 filter dimensions
    year_raw = data.get("years") or data.get("year") or data.get("selected_years")
    month_raw = data.get("months") or data.get("month") or data.get("selected_months")
    const_raw = data.get("construction_type") or data.get("construction")
    tyre_raw = data.get("tyre_type") or data.get("vehicle_type") or data.get("tyreType") or data.get("tyre_types")

    selected_year = normalize_filter_val(year_raw)
    selected_month = normalize_month_val(month_raw)
    selected_const = normalize_filter_val(const_raw).upper()
    selected_tyre = normalize_filter_val(tyre_raw).upper()

    # Zone filter (optional single zone)
    raw_zones = data.get("selected_zones") or data.get("zones") or data.get("zone")
    zone_filter = normalize_filter_val(raw_zones)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Failed to connect to database."}), 500

    effective_session = _resolve_session(conn, session_id)
    ensure_sales_by_zone_table(conn)

    try:
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()

        # Direct indexed lookup from pre-calculated MySQL table
        query_sql = """
            SELECT
                zone_code,
                zone_name,
                sales_value,
                percentage,
                quantity,
                last_synced_at
            FROM `dashboard_sales_by_zone`
            WHERE session_id = %s
              AND year_val = %s
              AND month_val = %s
              AND construction_type = %s
              AND tyre_type = %s
              AND zone_code != 'UNKNOWN'
            ORDER BY sales_value DESC
        """
        cur.execute(query_sql, (effective_session, selected_year, selected_month, selected_const, selected_tyre))
        rows = cur.fetchall()

        if not rows:
            # Fallback if specific construction/tyre had different casing or space
            query_sql_fuzzy = """
                SELECT
                    zone_code,
                    zone_name,
                    sales_value,
                    percentage,
                    quantity,
                    last_synced_at
                FROM `dashboard_sales_by_zone`
                WHERE session_id = %s
                  AND year_val = %s
                  AND month_val = %s
                  AND UPPER(TRIM(construction_type)) = %s
                  AND UPPER(TRIM(tyre_type)) = %s
                ORDER BY sales_value DESC
            """
            cur.execute(query_sql_fuzzy, (effective_session, selected_year, selected_month, selected_const, selected_tyre))
            rows = cur.fetchall()

        cur.close()

        if rows:
            last_synced = None
            chart_data = []

            for r in rows:
                zc = _val(r, "zone_code")
                if not zc or str(zc).strip().upper() == "UNKNOWN":
                    continue
                zn = _val(r, "zone_name", default=zc)
                sv = float(_val(r, "sales_value", default=0.0) or 0.0)
                pct = float(_val(r, "percentage", default=0.0) or 0.0)
                qty = float(_val(r, "quantity", default=0.0) or 0.0)
                sync_dt = _val(r, "last_synced_at")
                if sync_dt and not last_synced:
                    last_synced = str(sync_dt)

                zn_clean = ZONE_NAME_MAP.get(str(zc).upper(), zn)

                # If a specific zone was selected, filter by it
                if zone_filter != "ALL":
                    if str(zc).upper() != zone_filter.upper() and str(zn).upper() != zone_filter.upper():
                        continue

                chart_data.append({
                    "zone": zc,
                    "name": zn_clean,
                    "sales_value": sv,
                    "value": sv,
                    "percentage": round(pct, 1),
                    "quantity": qty
                })

            # If specific zone filter reduced items, recalculate percentage
            if zone_filter != "ALL" and chart_data:
                filtered_total = sum(d["value"] for d in chart_data)
                for d in chart_data:
                    d["percentage"] = round((d["value"] / filtered_total) * 100, 1) if filtered_total > 0 else 0.0

            # Filter to positive values for clean pie chart display
            display_data = [d for d in chart_data if d["value"] > 0] or chart_data

            return jsonify({
                "status": "success",
                "source": "dashboard_sales_by_zone",
                "visualizations": [{
                    "data": display_data,
                    "seriesKey": "",
                    "title": "Sales by Zone",
                    "type": "pie_chart",
                    "xKey": "name",
                    "yKey": "value"
                }],
                "data": display_data,
                "message": "Data retrieved successfully.",
                "sql_error": "",
                "_cached": True,
                "_last_synced_at": last_synced
            }), 200

        # If no rows found in MySQL (e.g. before initial scheduler sync has run):
        # Trigger an on-demand slice calculation or fallback to dashboard_visuals
        from controllers.dashboard_visuals import sales_by_zone_data_controller as legacy_sales_by_zone
        return legacy_sales_by_zone(get_db_connection)

    except Exception as e:
        print(f"[Sales by Zone Controller] Error: {e}", flush=True)
        return jsonify({
            "status": "error",
            "message": str(e),
            "visualizations": [],
            "data": []
        }), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

