"""
account_category_sales_dashboard_controller.py
─────────────────────────────────────────────────────────────────────────────
Dedicated controller for Actual Sales by Account Category (Cr).
Reads from and synchronizes MySQL table `dashboard_account_category_sales`.

Customer categories are dynamically bound to CH_TYPE from:
SELECT DISTINCT "CH_TYPE" AS CUSTOMER_TYPE
FROM "SAP_S4"."ZIVF_CustomerMaster_01_MD"
WHERE "CH_TYPE" IS NOT NULL
  AND "KLABC" <> 'X'
ORDER BY "CH_TYPE";
(Values: DB, DL, ED, FL, GT, IN, OD, OT)
Self-contained query functions without external service fallback.
─────────────────────────────────────────────────────────────────────────────
"""

import time
from datetime import datetime, timezone, timedelta
from flask import request, jsonify

ACCOUNT_CATEGORY_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_account_category_sales` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `category_title` VARCHAR(100) NOT NULL,
    `sales_value_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session_zone` (`session_id`, `zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_table_initialized = False


def ensure_account_category_sales_table(conn):
    global _table_initialized
    if _table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(ACCOUNT_CATEGORY_SALES_TABLE_SQL)
        # Purge any empty or placeholder records
        cur.execute("""
            DELETE FROM `dashboard_account_category_sales` 
            WHERE category_title = '' OR category_title IS NULL 
               OR LOWER(TRIM(category_title)) IN ('account category', 'unknown', '0', 'null', 'none')
        """)
        conn.commit()
        cur.close()
        _table_initialized = True
    except Exception as e:
        print(f"[Account Category Sales] Table init note: {e}", flush=True)


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
        print(f"[Account Category Sales] HANA connection note: {e}", flush=True)
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


def get_cached_account_category_sales(mysql_conn, session_id, zone=None):
    ensure_account_category_sales_table(mysql_conn)
    z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute("""
            SELECT category_title, sales_value_cr, last_synced_at
            FROM `dashboard_account_category_sales`
            WHERE session_id = %s AND zone_code = %s
              AND category_title IS NOT NULL
              AND TRIM(category_title) <> ''
              AND LOWER(TRIM(category_title)) NOT IN ('account category', 'unknown', '0', 'null', 'none')
            ORDER BY sales_value_cr DESC
        """, (str(session_id).strip(), z_code))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None

        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        res_list = []
        for r in rows:
            if isinstance(r, dict):
                ct, cr = r.get("category_title"), float(r.get("sales_value_cr") or 0)
            else:
                ct, cr = r[0], float(r[1] or 0)
            res_list.append({
                "title": ct,
                "valCr": cr,
                "value": f"₹{cr} Cr"
            })
        return {"data": res_list, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[Account Category Sales] Cache read error: {e}", flush=True)
        return None


def save_account_category_sales_to_table(mysql_conn, session_id, zone, data_list):
    if not mysql_conn or not session_id or not data_list:
        return False
    try:
        ensure_account_category_sales_table(mysql_conn)
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = mysql_conn.cursor()
        cur.execute(
            "DELETE FROM `dashboard_account_category_sales` WHERE session_id = %s AND zone_code = %s",
            (str(session_id).strip(), z_code)
        )
        sql = """
            INSERT INTO `dashboard_account_category_sales`
                (`session_id`, `zone_code`, `category_title`, `sales_value_cr`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        batch = []
        for item in data_list:
            if isinstance(item, dict):
                ct = str(item.get("title") or item.get("TITLE") or item.get("label") or "").strip()[:100]
                if not ct or ct.lower() in ("account category", "unknown", "0", "null", "none"):
                    continue
                cr = float(item.get("valCr") or item.get("sales_value_cr") or 0.0)
                if not cr and item.get("value"):
                    val_str = str(item.get("value")).replace("₹", "").replace("Cr", "").strip()
                    try:
                        cr = float(val_str)
                    except Exception:
                        pass
                batch.append((str(session_id).strip(), z_code, ct, cr, ist_now, ist_now))
        if batch:
            cur.executemany(sql, batch)
            mysql_conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Account Category Sales] Save error: {e}", flush=True)
        return False


def calculate_account_category_sales_hana(cursor, schema, zone=None):
    from helper.sap_connection import sanitize_and_execute_hana
    hana_sql = f"""
    WITH master_cats AS (
        SELECT DISTINCT UPPER(TRIM("CH_TYPE")) AS title
        FROM "{schema}"."ZIVF_CustomerMaster_01_MD"
        WHERE "CH_TYPE" IS NOT NULL 
          AND TRIM("CH_TYPE") <> ''
          AND ("KLABC" IS NULL OR UPPER(TRIM("KLABC")) <> 'X')
    ),
    filtered_data AS (
        SELECT 
            UPPER(TRIM(cm.CH_TYPE)) AS title,
            COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0) AS invoice_value
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" cs
        INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (
            LPAD(TRIM(TO_VARCHAR(cs.DEALER_CODE)), 10, '0') = LPAD(TRIM(TO_VARCHAR(cm.KUNNR)), 10, '0') 
            OR LTRIM(TO_VARCHAR(cs.DEALER_CODE), '0') = LTRIM(TO_VARCHAR(cm.KUNNR), '0')
        )
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cm.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (
            rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), ''))
        )
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '7' THEN 'WZ'
                ELSE 'CZ'
            END
        ))
        WHERE (cs.INDICATOR_CANCEL IS NULL OR cs.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (cs.DISTRIBUTION_CHANNEL = 10 OR cs.DISTRIBUTION_CHANNEL = '10')
          AND cm.CH_TYPE IS NOT NULL 
          AND TRIM(cm.CH_TYPE) <> ''
          AND (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
               OR zm.NAME = ? OR zm.P_ZONE = ?
               OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
    ),
    cat_sales AS (
        SELECT 
            title,
            ROUND(SUM(invoice_value) / 10000000, 2) AS "valCr"
        FROM filtered_data
        GROUP BY title
    )
    SELECT 
        m.title AS "title",
        COALESCE(s."valCr", 0.0) AS "valCr",
        '₹' || TO_VARCHAR(COALESCE(s."valCr", 0.0)) || ' Cr' AS "value"
    FROM master_cats m
    LEFT JOIN cat_sales s ON (m.title = s.title)
    ORDER BY "valCr" DESC, m.title ASC
    """
    sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def calculate_account_category_sales_mysql(cursor, session_id, zone=None):
    """Direct self-contained MySQL query function."""
    try:
        args = (zone,) if zone and str(zone).strip().lower() != "all" else (None,)
        cursor.callproc('sp_get_sales_by_account_category', args)
        rows = []
        if hasattr(cursor, 'stored_results'):
            for res in cursor.stored_results():
                rows = res.fetchall()
                break
        if not rows and hasattr(cursor, 'fetchall') and cursor.description:
            rows = cursor.fetchall()
        return rows
    except Exception as e:
        print(f"[Account Category Sales] MySQL calculation note: {e}", flush=True)
        return []


def get_account_category_sales_controller(get_db_connection):
    session_id = request.args.get("session_id")
    zone = request.args.get("zone")
    force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = \
        _get_connection_context(get_db_connection, session_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        # 1. Check MySQL table `dashboard_account_category_sales` first if not force_refresh
        if not force_refresh:
            cached = get_cached_account_category_sales(mysql_conn, effective_session, zone=zone)
            if cached and cached.get("data"):
                return jsonify({
                    "status": "success",
                    "source": "dashboard_account_category_sales",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        # 2. Compute dynamically using self-contained controller query function
        if is_hana:
            cursor = hana_conn.cursor()
            results = calculate_account_category_sales_hana(cursor, schema, zone=zone)
            cursor.close()
        else:
            cursor = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            results = calculate_account_category_sales_mysql(cursor, effective_session, zone=zone)
            cursor.close()

        # 3. Save into `dashboard_account_category_sales` table
        if results:
            save_account_category_sales_to_table(mysql_conn, effective_session, zone, results)

        return jsonify({
            "status": "success",
            "source": "dashboard_account_category_sales",
            "data": results or []
        }), 200

    except Exception as e:
        print(f"[Account Category Sales] Controller error: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel)
