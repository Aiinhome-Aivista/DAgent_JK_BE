"""
category_sales_dashboard_controller.py
─────────────────────────────────────────────────────────────────────────────
Dedicated controller for Category Sales.
Reads from and synchronizes MySQL table `dashboard_category_sales`.

Category description is directly bound to CAT_CODE from:
"SAP_S4"."ZIVF_CategoryMaster_01_TR" (joined with ZIVR_SKUMaster_01_TR on CAT_CODE)
Self-contained query functions without external service fallback.
─────────────────────────────────────────────────────────────────────────────
"""

from datetime import datetime, timezone, timedelta
from flask import request, jsonify

CATEGORY_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_category_sales` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `category_name` VARCHAR(100) NOT NULL,
    `total_sales_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session_zone` (`session_id`, `zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_cat_table_initialized = False


def ensure_category_sales_table(conn):
    global _cat_table_initialized
    if _cat_table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(CATEGORY_SALES_TABLE_SQL)
        # Purge any previously cached invalid records (e.g. '0', '', 'Z')
        try:
            cur.execute("DELETE FROM `dashboard_category_sales` WHERE category_name IN ('0', '', 'Z') OR category_name IS NULL")
        except Exception:
            pass
        conn.commit()
        cur.close()
        _cat_table_initialized = True
    except Exception as e:
        print(f"[Category Sales Table] init note: {e}", flush=True)


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


def _get_connection_context(get_db_connection, session_id_arg=None, user_id_arg=None):
    mysql_conn = get_db_connection()
    if not mysql_conn:
        return None, None, None, False, None, None

    effective_session = _resolve_session(mysql_conn, session_id_arg)
    from helper.sap_connection import get_sap_connection_for_session

    hana_conn = None
    tunnel = None
    is_hana = False
    sap_schema = "SAP_S4"

    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, effective_session, user_id=user_id_arg)
        if hana_conn:
            is_hana = True
            sap_schema = schema or "SAP_S4"
    except Exception as e:
        print(f"[Category Sales] HANA connection note: {e}", flush=True)
        is_hana = False

    return mysql_conn, hana_conn, tunnel, is_hana, sap_schema, effective_session


def _close_connections(mysql_conn, hana_conn, tunnel, cursor=None):
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
    if mysql_conn and hasattr(mysql_conn, "is_connected") and mysql_conn.is_connected():
        try:
            mysql_conn.close()
        except Exception:
            pass
    elif mysql_conn:
        try:
            mysql_conn.close()
        except Exception:
            pass


def get_cached_category_sales(mysql_conn, session_id, zone=None):
    ensure_category_sales_table(mysql_conn)
    z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute("""
            SELECT category_name, total_sales_cr, last_synced_at
            FROM `dashboard_category_sales`
            WHERE session_id = %s AND zone_code = %s
              AND category_name NOT IN ('0', '', 'Z')
            ORDER BY total_sales_cr DESC
        """, (str(session_id).strip(), z_code))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None

        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        res_list = []
        for r in rows:
            if isinstance(r, dict):
                cname = r.get("category_name")
                cr = float(r.get("total_sales_cr") or 0.0)
            else:
                cname = r[0]
                cr = float(r[1] or 0.0)
            if cname and str(cname).strip() not in ('0', '', 'Z'):
                res_list.append({
                    "category": str(cname).strip(),
                    "total_sales_cr": cr
                })
        return {"data": res_list, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[Category Sales] Cache read error: {e}", flush=True)
        return None


def save_category_sales_to_table(mysql_conn, session_id, zone, data_list):
    if not mysql_conn or not session_id or not data_list:
        return False
    try:
        ensure_category_sales_table(mysql_conn)
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = mysql_conn.cursor()
        cur.execute(
            "DELETE FROM `dashboard_category_sales` WHERE session_id = %s AND zone_code = %s",
            (str(session_id).strip(), z_code)
        )
        sql = """
            INSERT INTO `dashboard_category_sales`
                (`session_id`, `zone_code`, `category_name`, `total_sales_cr`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        batch = []
        for item in data_list:
            if isinstance(item, dict):
                cname = str(item.get("category") or item.get("category_name") or item.get("name") or "").strip()[:100]
                cr = float(item.get("total_sales_cr") or item.get("valCr") or item.get("sales_value") or 0.0)
                if cname and cname not in ('0', '', 'Z'):
                    batch.append((str(session_id).strip(), z_code, cname, cr, ist_now, ist_now))
        if batch:
            cur.executemany(sql, batch)
            mysql_conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Category Sales] Save error: {e}", flush=True)
        return False


def calculate_category_sales_hana(cursor, schema, zone=None, year=None):
    from helper.sap_connection import sanitize_and_execute_hana
    
    # Year filter support: If year is provided (e.g. 2026), filter CurrentSales/SalesHistory
    year_condition = ""
    if year and str(year).strip().lower() != "all" and str(year).strip().isdigit():
        year_condition = f"AND YEAR(s.\"invoice_date\") = {int(year)}"

    hana_sql = f"""
    SELECT 
        cat."CAT_DESC" AS "category", 
        ROUND(SUM(COALESCE(cs."NDP_VALUE", cs."TAXABLE_VALUE", cs."INVOICE_VALUE", 0)) / 10000000, 2) AS "total_sales_cr"
    FROM (
        SELECT s."DEALER_CODE", s."PLANT", s."SKU", s."INVOICE_VALUE", s."TAXABLE_VALUE", s."NDP_VALUE", s."INDICATOR_CANCEL", s."DISTRIBUTION_CHANNEL", s."invoice_date"
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
        WHERE (s."INDICATOR_CANCEL" IS NULL OR s."INDICATOR_CANCEL" NOT IN ('X', 'Y', '1'))
          AND (s."DISTRIBUTION_CHANNEL" = 10 OR s."DISTRIBUTION_CHANNEL" = '10')
          {year_condition}
        UNION ALL
        SELECT s."DEALER_CODE", s."PLANT", s."SKU", s."INVOICE_VALUE", s."TAXABLE_VALUE", s."NDP_VALUE", s."INDICATOR_CANCEL", s."DISTRIBUTION_CHANNEL", s."invoice_date"
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
        WHERE (s."INDICATOR_CANCEL" IS NULL OR s."INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (s."DISTRIBUTION_CHANNEL" = 10 OR s."DISTRIBUTION_CHANNEL" = '10')
          {year_condition}
    ) cs
    INNER JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sku 
        ON (TRIM(cs."SKU") = TRIM(sku."MATNR"))
    INNER JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat 
        ON (TRIM(sku."CAT_CODE") = TRIM(cat."CAT_CODE"))
    LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cust 
        ON (cs."DEALER_CODE" = cust."KUNNR" OR LPAD(TRIM(cs."DEALER_CODE"), 10, '0') = cust."KUNNR" OR LTRIM(cs."DEALER_CODE", '0') = LTRIM(cust."KUNNR", '0'))
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant 
        ON (cs."PLANT" = tm_plant."WERKS" OR cust."VWERK" = tm_plant."WERKS")
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk 
        ON (cust."BZIRK" = tm_bzirk."BZIRK")
    LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
        ON (rm."region_code" = COALESCE(NULLIF(TRIM(tm_plant."REGIO"), ''), NULLIF(TRIM(tm_bzirk."REGIO"), ''), NULLIF(TRIM(TO_VARCHAR(cust."REGIO")), '')))
    LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm 
        ON (zm."P_ZONE" = COALESCE(
            NULLIF(TRIM(tm_plant."P_ZONE"), ''),
            NULLIF(TRIM(tm_bzirk."P_ZONE"), ''),
            NULLIF(TRIM(rm."zone_code"), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(cs."PLANT"), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(cs."PLANT"), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(cs."PLANT"), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(cs."PLANT"), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(cs."PLANT"), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(cs."PLANT"), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(cs."PLANT"), 1, 1) = '7' THEN 'WZ'
                ELSE NULL
            END
        ))
    WHERE (cust."KLABC" IS NULL OR UPPER(TRIM(cust."KLABC")) <> 'X')
      AND cat."CAT_DESC" IS NOT NULL 
      AND TRIM(cat."CAT_DESC") <> '' 
      AND TRIM(cat."CAT_DESC") <> '0'
      AND cat."CAT_CODE" NOT IN ('TR', 'NT', 'AL')
      AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
           OR zm."NAME" = ? OR zm."P_ZONE" = ?
           OR UPPER(TRIM(zm."NAME")) = UPPER(TRIM(?)) OR UPPER(TRIM(zm."P_ZONE")) = UPPER(TRIM(?)))
    GROUP BY cat."CAT_DESC"
    HAVING SUM(COALESCE(cs."NDP_VALUE", cs."TAXABLE_VALUE", cs."INVOICE_VALUE", 0)) > 0
    ORDER BY "total_sales_cr" DESC
    """
    sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def calculate_category_sales_mysql(cursor, session_id, zone=None):
    """Direct self-contained MySQL query function."""
    try:
        args = (zone,) if zone and str(zone).strip().lower() != "all" else (None,)
        cursor.callproc('sp_get_category_sales', args)
        rows = []
        if hasattr(cursor, 'stored_results'):
            for res in cursor.stored_results():
                rows = res.fetchall()
                break
        if not rows and hasattr(cursor, 'fetchall') and cursor.description:
            rows = cursor.fetchall()
        return rows
    except Exception as e:
        print(f"[Category Sales] MySQL calculation note: {e}", flush=True)
        return []


def get_category_sales_controller(get_db_connection):
    session_id = request.args.get("session_id")
    zone = request.args.get("zone")
    year = request.args.get("year") or request.args.get("years")
    force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = \
        _get_connection_context(get_db_connection, session_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        # 1. Check MySQL table `dashboard_category_sales` first if not force_refresh
        if not force_refresh:
            cached = get_cached_category_sales(mysql_conn, effective_session, zone=zone)
            if cached and cached.get("data"):
                return jsonify({
                    "status": "success",
                    "source": "dashboard_category_sales",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        # 2. Compute dynamically using self-contained query function
        if is_hana:
            cursor = hana_conn.cursor()
            results = calculate_category_sales_hana(cursor, schema, zone=zone, year=year)
            cursor.close()
        else:
            cursor = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            results = calculate_category_sales_mysql(cursor, effective_session, zone=zone)
            cursor.close()

        # 3. Save into `dashboard_category_sales` table
        if results:
            save_category_sales_to_table(mysql_conn, effective_session, zone, results)

        return jsonify({
            "status": "success",
            "source": "dashboard_category_sales",
            "data": results or []
        }), 200

    except Exception as e:
        print(f"[Category Sales] Controller error: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel)

