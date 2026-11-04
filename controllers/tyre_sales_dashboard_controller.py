"""
tyre_sales_dashboard_controller.py
─────────────────────────────────────────────────────────────────────────────
Dedicated controller for Top 10 Tyre Types by Sales (Horizontal Bar Chart).
Handles:
1. Dynamic filter extraction:
   - Year: 2026, 2025, 2024 (exact same query as Year-wise Sales)
   - Zone: Distinct zones from ZIVF_ZoneMaster_01_TR
   - Region: Distinct regions from ZIVF_RegionMaster_MD_01
   - Customer Category: Distinct CH_TYPE from ZIVF_CustomerMaster_01_MD
   - Construction Type: Distinct CONSTUCTION from ZIVR_SKUMaster_01_TR (user's exact SQL)
2. Permutation Pre-calculation and Bulk Sync into MySQL table `dashboard_tyre_sales_top_10`:
   - Runs fast bulk aggregation from HANA (with memory-safe split queries).
   - Generates and rolls up all dimensional permutations in Python memory.
   - Saves Top 10 tyre types per slice with instant < 5ms retrieval.
3. Fast API endpoint for `/tyre-sales-data` and `/available-years`.
─────────────────────────────────────────────────────────────────────────────
"""

import time
import collections
from datetime import datetime, timezone, timedelta
from flask import request, jsonify

_filters_cache = {}
_CACHE_TTL = 300


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
        print(f"[Tyre Sales] fetchall_dict error: {e}", flush=True)
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
    from helper.sap_analytics_service import get_sap_connection_for_session

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
        print(f"[Tyre Sales] HANA connection note: {e}", flush=True)
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
        try: cursor.close()
        except Exception: pass
    if hana_conn:
        try: hana_conn.close()
        except Exception: pass
    if tunnel:
        try: tunnel.stop()
        except Exception: pass
    if mysql_conn:
        try: mysql_conn.close()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Master Filter Fetching Functions (No Hardcoding)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_master_years(cursor, is_hana, schema, user_db):
    """Last 3 active years from Sales History & Current Sales."""
    from controllers.year_wise_dashboard_controller import fetch_master_years as ym_years
    return ym_years(cursor, is_hana, schema, user_db)


def fetch_master_zones(cursor, is_hana, schema, user_db):
    """Master zones from ZIVF_ZoneMaster_01_TR."""
    from controllers.year_wise_dashboard_controller import fetch_zones as ym_zones
    return ym_zones(cursor, is_hana, schema, user_db)


def fetch_master_regions(cursor, is_hana, schema, user_db):
    """Master regions from RegionMaster."""
    from controllers.year_wise_dashboard_controller import fetch_regions as ym_regions
    return ym_regions(cursor, is_hana, schema, user_db)


def fetch_master_customer_categories(cursor, is_hana, schema, user_db):
    """Customer types/categories from CustomerMaster CH_TYPE."""
    from controllers.year_wise_dashboard_controller import fetch_customer_types as ym_cust
    return ym_cust(cursor, is_hana, schema, user_db)


def fetch_master_construction_types(cursor, is_hana, schema, user_db):
    """User's exact query for Distinct CONSTUCTION from ZIVR_SKUMaster_01_TR."""
    from controllers.sales_by_zone_dashboard_controller import fetch_master_construction_types as sbz_const
    return sbz_const(cursor, is_hana, schema, user_db)


def get_available_years_filters_controller(get_db_connection):
    """
    Controller for GET /available-years.
    Powers the 5 dropdowns in TyreSalesChartDynamic.tsx.
    """
    session_id = request.args.get("session_id", "").strip()
    user_id = request.args.get("user_id")

    cache_key = f"avail_years_filters_{session_id}"
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
        zones = fetch_master_zones(cursor, is_hana, schema, user_db)
        regions = fetch_master_regions(cursor, is_hana, schema, user_db)
        customer_types = fetch_master_customer_categories(cursor, is_hana, schema, user_db)
        construction_types = fetch_master_construction_types(cursor, is_hana, schema, user_db)

        from controllers.sales_by_zone_dashboard_controller import fetch_master_months
        months = fetch_master_months(cursor, is_hana, schema, user_db)

        resp_data = {
            "status": "success",
            "years": years,
            "zones": zones,
            "regions": regions,
            "months": months,
            "customer_types": customer_types,
            "construction_types": construction_types
        }

        _set_cache(cache_key, resp_data)
        return jsonify(resp_data), 200

    except Exception as e:
        print(f"[Available Years Filters] Error: {e}", flush=True)
        return jsonify({
            "status": "error",
            "message": str(e),
            "years": [{"label": "ALL", "value": "ALL"}, {"label": "2026", "value": 2026}, {"label": "2025", "value": 2025}, {"label": "2024", "value": 2024}],
            "zones": [{"label": "ALL", "value": "ALL"}],
            "regions": [{"label": "ALL", "value": "ALL"}],
            "months": [{"label": "ALL", "value": "ALL"}],
            "customer_types": [{"label": "ALL", "value": "ALL"}],
            "construction_types": [{"label": "ALL", "value": "ALL"}]
        }), 200
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel, cursor if 'cursor' in locals() else None)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Table Schema Initialization
# ─────────────────────────────────────────────────────────────────────────────

TYRE_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_tyre_sales_top_10` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `year_val` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `zone_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `region_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `customer_category` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `construction_type` VARCHAR(100) NOT NULL DEFAULT 'ALL',
    `rank_no` INT NOT NULL DEFAULT 1,
    `tyre_type` VARCHAR(100) NOT NULL,
    `sales_value` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_tyre_lookup` (`session_id`, `year_val`, `zone_code`, `region_code`, `customer_category`, `construction_type`),
    INDEX `idx_tyre_type` (`tyre_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_tyre_table_initialized = False

def ensure_tyre_sales_table(conn):
    """Ensures `dashboard_tyre_sales_top_10` exists with all permutation columns."""
    global _tyre_table_initialized
    if _tyre_table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(TYRE_SALES_TABLE_SQL)
        # Check / add missing columns for upgrades
        for col_def in [
            ("year_val", "VARCHAR(20) NOT NULL DEFAULT 'ALL'"),
            ("zone_code", "VARCHAR(50) NOT NULL DEFAULT 'ALL'"),
            ("region_code", "VARCHAR(50) NOT NULL DEFAULT 'ALL'"),
            ("customer_category", "VARCHAR(50) NOT NULL DEFAULT 'ALL'"),
            ("construction_type", "VARCHAR(100) NOT NULL DEFAULT 'ALL'"),
            ("rank_no", "INT NOT NULL DEFAULT 1"),
            ("tyre_type", "VARCHAR(100) NOT NULL"),
            ("sales_value", "DECIMAL(18, 2) NOT NULL DEFAULT 0.00")
        ]:
            try:
                cur.execute(f"ALTER TABLE `dashboard_tyre_sales_top_10` ADD COLUMN `{col_def[0]}` {col_def[1]}")
            except Exception:
                pass

        try:
            cur.execute("ALTER TABLE `dashboard_tyre_sales_top_10` ADD INDEX `idx_tyre_lookup` (`session_id`, `year_val`, `zone_code`, `region_code`, `customer_category`, `construction_type`)")
        except Exception:
            pass

        conn.commit()
        cur.close()
        _tyre_table_initialized = True
    except Exception as e:
        print(f"[Tyre Sales Table] init: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Bulk Sync Permutations (For Scheduler & Sync Now Button)
# ─────────────────────────────────────────────────────────────────────────────

def bulk_sync_tyre_sales_permutations(mysql_conn, session_id: str, user_id=None):
    """
    Pre-calculates all Top 10 Tyre Types slices across
    (Year, Zone, Region, Customer Category, Construction Type)
    and stores them cleanly in MySQL table `dashboard_tyre_sales_top_10`.
    Uses memory-safe split queries to avoid HANA memory limits, and in-memory aggregation.
    """
    if not mysql_conn or not session_id:
        return False

    from helper.dashboard_cache_service import get_now_ist
    ist_now = get_now_ist()
    ensure_tyre_sales_table(mysql_conn)

    effective_session = session_id
    from helper.sap_analytics_service import get_sap_connection_for_session
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
        print(f"[Bulk Sync Tyre Sales] HANA note: {e}", flush=True)
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
            # Memory-safe and eliminates HANA UNION memory limits!
            base_query_template = """
                SELECT
                    COALESCE(NULLIF(TRIM(zm."P_ZONE"), ''), NULLIF(TRIM(tm."P_ZONE"), ''), NULLIF(TRIM(rm."zone_code"), ''), 'UNKNOWN') AS ZONE_CODE,
                    COALESCE(NULLIF(TRIM(rm."region_code"), ''), NULLIF(TRIM(tm."REGIO"), ''), NULLIF(TRIM(cm."BZIRK"), ''), 'UNKNOWN') AS REGION_CODE,
                    COALESCE(NULLIF(TRIM(rm."region_name"), ''), '') AS REGION_NAME,
                    COALESCE(NULLIF(TRIM(cm."CH_TYPE"), ''), 'UNKNOWN') AS CUSTOMER_CATEGORY,
                    COALESCE(NULLIF(TRIM(sku."CONSTUCTION"), ''), 'UNKNOWN') AS CONSTRUCTION_TYPE,
                    COALESCE(NULLIF(TRIM(sku."TYRE_TYPE"), ''), 'UNKNOWN') AS TYRE_TYPE,
                    YEAR(s."invoice_date") AS SALES_YEAR,
                    ROUND(SUM(COALESCE(s."invoice_value", s."ndp_value", s."taxable_value", 0)), 2) AS TOTAL_SALES
                FROM "{schema}"."{table_name}" s
                INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
                    ON (s."dealer_code" = cm."KUNNR" OR LPAD(TRIM(s."dealer_code"), 10, '0') = cm."KUNNR" OR LTRIM(s."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
                LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm 
                    ON (cm."BZIRK" = tm."BZIRK" OR TRIM(cm."BZIRK") = TRIM(tm."BZIRK"))
                LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
                    ON (tm."REGIO" = rm."region_code" OR TRIM(tm."REGIO") = TRIM(rm."region_code"))
                LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm 
                    ON (COALESCE(tm."P_ZONE", rm."zone_code") = zm."P_ZONE")
                LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sku 
                    ON (TRIM(s."SKU") = TRIM(sku."MATNR"))
                WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
                  AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
                  AND IFNULL(cm."KLABC", '') <> 'X'
                  AND s."invoice_date" IS NOT NULL
                  {extra_filter}
                GROUP BY 
                    COALESCE(NULLIF(TRIM(zm."P_ZONE"), ''), NULLIF(TRIM(tm."P_ZONE"), ''), NULLIF(TRIM(rm."zone_code"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm."region_code"), ''), NULLIF(TRIM(tm."REGIO"), ''), NULLIF(TRIM(cm."BZIRK"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm."region_name"), ''), ''),
                    COALESCE(NULLIF(TRIM(cm."CH_TYPE"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(sku."CONSTUCTION"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(sku."TYRE_TYPE"), ''), 'UNKNOWN'),
                    YEAR(s."invoice_date")
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
                print(f"[Bulk Sync Tyre Sales] CurrentSales warning: {e}", flush=True)

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
                print(f"[Bulk Sync Tyre Sales] SalesHistory warning: {e}", flush=True)

        else:
            cursor = mysql_conn.cursor()
            mysql_sql = f"""
                SELECT
                    COALESCE(NULLIF(TRIM(rm.zone), ''), 'UNKNOWN') AS ZONE_CODE,
                    COALESCE(NULLIF(TRIM(rm.region), ''), NULLIF(TRIM(cm.sales_office), ''), 'UNKNOWN') AS REGION_CODE,
                    COALESCE(NULLIF(TRIM(rm.region_name), ''), '') AS REGION_NAME,
                    COALESCE(NULLIF(TRIM(cm.ch_type), ''), 'UNKNOWN') AS CUSTOMER_CATEGORY,
                    COALESCE(NULLIF(TRIM(s.constuction), ''), 'UNKNOWN') AS CONSTRUCTION_TYPE,
                    COALESCE(NULLIF(TRIM(s.tyre_type), ''), 'UNKNOWN') AS TYRE_TYPE,
                    YEAR(s.billing__doc_date) AS SALES_YEAR,
                    ROUND(SUM(COALESCE(s.net__value, 0)), 2) AS TOTAL_SALES
                FROM `{user_db}`.`sales_data` s
                JOIN `{user_db}`.`customer_master` cm ON (s.customer = cm.kunnr)
                LEFT JOIN `{user_db}`.`region_master` rm ON (rm.region = cm.sales_office)
                WHERE (s.distribution__channel = '10' OR s.distribution__channel = 10)
                  AND s.billing__doc_date IS NOT NULL
                  AND s.billing__doc_date >= '2024-01-01'
                  AND IFNULL(cm.klabc, '') <> 'X'
                GROUP BY
                    COALESCE(NULLIF(TRIM(rm.zone), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm.region), ''), NULLIF(TRIM(cm.sales_office), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm.region_name), ''), ''),
                    COALESCE(NULLIF(TRIM(cm.ch_type), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(s.constuction), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(s.tyre_type), ''), 'UNKNOWN'),
                    YEAR(s.billing__doc_date)
            """
            cursor.execute(mysql_sql)
            raw_rows = fetchall_dict(cursor)

    except Exception as e:
        print(f"[Bulk Sync Tyre Sales] Extraction error: {e}", flush=True)
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
        print("[Bulk Sync Tyre Sales] No raw sales records found to sync.", flush=True)
        return False

    print(f"[Bulk Sync Tyre Sales] Fetched {len(raw_rows)} base aggregates. Computing Top 10 rollups in memory...", flush=True)

    # In-memory aggregation:
    # slice_key = (year_val, zone_code, region_code, customer_category, construction_type)
    # slice_tyres[slice_key][tyre_type] = float (total sales)
    slice_tyres = collections.defaultdict(lambda: collections.defaultdict(float))

    for r in raw_rows:
        zc = _val(r, "zone_code", default="UNKNOWN")
        rc = _val(r, "region_code", default="UNKNOWN")
        rn = _val(r, "region_name", default="")
        cc = _val(r, "customer_category", default="UNKNOWN")
        ct = _val(r, "construction_type", default="UNKNOWN")
        tt = _val(r, "tyre_type", default="UNKNOWN")
        sy = _val(r, "sales_year")

        sales = float(_val(r, "total_sales", default=0.0) or 0.0)

        zc_str = str(zc).strip().upper() if zc else "UNKNOWN"
        rc_str = str(rc).strip().upper() if rc else "UNKNOWN"
        rn_str = str(rn).strip().upper() if rn else ""
        cc_str = str(cc).strip().upper() if cc else "UNKNOWN"
        ct_str = str(ct).strip().upper() if ct else "UNKNOWN"
        tt_str = str(tt).strip().upper() if tt else "UNKNOWN"
        sy_str = str(sy).strip() if sy else "UNKNOWN"

        # Generate permutations:
        # Year: specific year, ALL
        # Zone: specific zone, ALL
        # Region: specific code, specific name (if distinct), ALL
        # Customer Category: specific category, ALL
        # Construction Type: specific construction, ALL
        reg_options = [rc_str, "ALL"]
        if rn_str and rn_str != rc_str and rn_str != "UNKNOWN":
            reg_options.insert(1, rn_str)

        for y_dim in (sy_str, "ALL"):
            for z_dim in (zc_str, "ALL"):
                for r_dim in reg_options:
                    for c_dim in (cc_str, "ALL"):
                        for const_dim in (ct_str, "ALL"):
                            k = (y_dim, z_dim, r_dim, c_dim, const_dim)
                            slice_tyres[k][tt_str] += sales

    # Sort each slice and pick Top 10
    batch_rows = []
    for (y_val, z_val, r_val, c_val, const_val), tyre_dict in slice_tyres.items():
        # Sort descending by sales value
        sorted_tyres = sorted(tyre_dict.items(), key=lambda x: x[1], reverse=True)[:10]
        for rank_idx, (tyre_name, tyre_sales) in enumerate(sorted_tyres, start=1):
            batch_rows.append((
                effective_session,
                y_val,
                z_val,
                r_val,
                c_val,
                const_val,
                rank_idx,
                tyre_name,
                round(tyre_sales, 2),
                ist_now,
                ist_now
            ))

    print(f"[Bulk Sync Tyre Sales] Generated {len(batch_rows)} Top 10 permutation rows. Writing to MySQL...", flush=True)

    # Fresh clean write: DELETE existing rows for session, then INSERT
    m_cur = None
    try:
        m_cur = mysql_conn.cursor()
        m_cur.execute("DELETE FROM `dashboard_tyre_sales_top_10` WHERE session_id = %s", (effective_session,))

        insert_sql = """
            INSERT INTO `dashboard_tyre_sales_top_10`
                (`session_id`, `year_val`, `zone_code`, `region_code`, `customer_category`, `construction_type`,
                 `rank_no`, `tyre_type`, `sales_value`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        chunk_size = 5000
        for i in range(0, len(batch_rows), chunk_size):
            chunk = batch_rows[i:i + chunk_size]
            m_cur.executemany(insert_sql, chunk)

        mysql_conn.commit()
        print(f"[Bulk Sync Tyre Sales] ✅ Successfully committed {len(batch_rows)} rows to `dashboard_tyre_sales_top_10`.", flush=True)
        return True

    except Exception as e:
        print(f"[Bulk Sync Tyre Sales] MySQL insert error: {e}", flush=True)
        try: mysql_conn.rollback()
        except Exception: pass
        return False
    finally:
        if m_cur:
            try: m_cur.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# 4. Data Retrieval Controller for /tyre-sales-data
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


def get_tyre_sales_data_controller(get_db_connection):
    """
    Main controller for POST /tyre-sales-data.
    Reads pre-calculated Top 10 from `dashboard_tyre_sales_top_10` in < 5ms.
    """
    data = request.json or {}
    session_id = data.get("session_id")
    user_id = data.get("user_id")

    # Extract 5 filter dimensions
    year_raw = data.get("year") or data.get("years") or data.get("selected_years")
    zone_raw = data.get("zone") or data.get("zones") or data.get("selected_zones")
    region_raw = data.get("region") or data.get("regions") or data.get("selected_regions")
    cust_raw = data.get("customer_category") or data.get("customer_categories") or data.get("selected_customer_categories") or data.get("customer_type") or data.get("customer_types")
    const_raw = data.get("construction_type") or data.get("constructions") or data.get("selected_constructions")

    selected_year = normalize_filter_val(year_raw)
    selected_zone = normalize_filter_val(zone_raw).upper()
    selected_region = normalize_filter_val(region_raw).upper()
    selected_cust = normalize_filter_val(cust_raw).upper()
    selected_const = normalize_filter_val(const_raw).upper()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Failed to connect to database."}), 500

    effective_session = _resolve_session(conn, session_id)
    ensure_tyre_sales_table(conn)

    try:
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()

        # Direct indexed lookup from pre-calculated MySQL table
        query_sql = """
            SELECT
                rank_no,
                tyre_type,
                sales_value,
                last_synced_at
            FROM `dashboard_tyre_sales_top_10`
            WHERE session_id = %s
              AND year_val = %s
              AND zone_code = %s
              AND region_code = %s
              AND customer_category = %s
              AND construction_type = %s
            ORDER BY sales_value DESC
            LIMIT 10
        """
        cur.execute(query_sql, (effective_session, selected_year, selected_zone, selected_region, selected_cust, selected_const))
        rows = cur.fetchall()

        if not rows:
            # Fallback if region was stored as name or code
            query_sql_fuzzy = """
                SELECT
                    rank_no,
                    tyre_type,
                    sales_value,
                    last_synced_at
                FROM `dashboard_tyre_sales_top_10`
                WHERE session_id = %s
                  AND year_val = %s
                  AND (zone_code = %s OR UPPER(TRIM(zone_code)) = %s)
                  AND (region_code = %s OR UPPER(TRIM(region_code)) = %s)
                  AND (customer_category = %s OR UPPER(TRIM(customer_category)) = %s)
                  AND (construction_type = %s OR UPPER(TRIM(construction_type)) = %s)
                ORDER BY sales_value DESC
                LIMIT 10
            """
            cur.execute(query_sql_fuzzy, (
                effective_session, selected_year,
                selected_zone, selected_zone,
                selected_region, selected_region,
                selected_cust, selected_cust,
                selected_const, selected_const
            ))
            rows = cur.fetchall()

        cur.close()

        if rows:
            last_synced = None
            chart_data = []

            for r in rows:
                tt = _val(r, "tyre_type", default="Unknown")
                sv = float(_val(r, "sales_value", default=0.0) or 0.0)
                sync_dt = _val(r, "last_synced_at")
                if sync_dt and not last_synced:
                    last_synced = str(sync_dt)

                plot_val = sv / 10000000.0 if sv >= 100000 else sv

                chart_data.append({
                    "vehicle_type": tt,
                    "tyre_type": tt,
                    "category": tt,
                    "name": tt,
                    "sales_value": sv,
                    "value": round(plot_val, 2)
                })

            return jsonify({
                "status": "success",
                "source": "dashboard_tyre_sales_top_10",
                "visualizations": [{
                    "data": chart_data,
                    "seriesKey": "",
                    "title": "Top 10 Tyre Types by Sales",
                    "type": "horizontal_bar_chart",
                    "xKey": "sales_value",
                    "yKey": "vehicle_type"
                }],
                "data": chart_data,
                "message": "Data retrieved successfully.",
                "sql_error": "",
                "_cached": True,
                "_last_synced_at": last_synced
            }), 200

        # Fallback to legacy on-demand calculation if pre-calculated cache not yet populated
        from controllers.dashboard_visuals import tyre_sales_data_controller as legacy_tyre_sales
        return legacy_tyre_sales(get_db_connection)

    except Exception as e:
        print(f"[Tyre Sales Controller] Error: {e}", flush=True)
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

