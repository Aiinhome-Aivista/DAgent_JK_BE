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
from database.config import MYSQL_CONFIG

ACCOUNT_CATEGORY_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_account_category_sales` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `region_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `category_title` VARCHAR(100) NOT NULL,
    `sales_value_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session_zone` (`session_id`, `zone_code`),
    INDEX `idx_session_zone_reg` (`session_id`, `zone_code`, `region_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_table_initialized = False


def _use_main_db(conn):
    try:
        main_db = MYSQL_CONFIG.get("database", "dagent_v2")
        cur = conn.cursor()
        cur.execute(f"USE `{main_db}`")
        cur.close()
    except Exception:
        pass


def ensure_account_category_sales_table(conn):
    global _table_initialized
    if _table_initialized or not conn:
        return
    try:
        _use_main_db(conn)
        cur = conn.cursor()
        cur.execute(ACCOUNT_CATEGORY_SALES_TABLE_SQL)
        # Check if region_code column exists, if not add it
        try:
            cur.execute("SELECT region_code FROM `dashboard_account_category_sales` LIMIT 1")
            cur.fetchall()
        except Exception:
            try:
                cur.execute("ALTER TABLE `dashboard_account_category_sales` ADD COLUMN `region_code` VARCHAR(50) NOT NULL DEFAULT 'ALL' AFTER `zone_code`")
                cur.execute("ALTER TABLE `dashboard_account_category_sales` ADD INDEX `idx_session_zone_reg` (`session_id`, `zone_code`, `region_code`)")
            except Exception:
                pass
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
        cur.fetchall()
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
    if mysql_conn:
        try:
            mysql_conn.close()
        except Exception:
            pass


def get_cached_account_category_sales(mysql_conn, session_id, zone=None, region=None):
    _use_main_db(mysql_conn)
    ensure_account_category_sales_table(mysql_conn)
    z_code = str(zone).strip() if zone and str(zone).strip().lower() not in ("all", "null", "none", "") else "ALL"
    r_code = str(region).strip() if region and str(region).strip().lower() not in ("all", "null", "none", "") else "ALL"
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute("""
            SELECT category_title, sales_value_cr, last_synced_at
            FROM `dashboard_account_category_sales`
            WHERE session_id = %s AND zone_code = %s AND region_code = %s
              AND category_title IS NOT NULL
              AND TRIM(category_title) <> ''
              AND sales_value_cr > 0
              AND LOWER(TRIM(category_title)) NOT IN ('account category', 'unknown', '0', 'null', 'none')
            ORDER BY sales_value_cr DESC
        """, (str(session_id).strip(), z_code, r_code))
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


def save_account_category_sales_to_table(mysql_conn, session_id, zone, data_list, region=None):
    if not mysql_conn or not session_id or not data_list:
        return False
    try:
        _use_main_db(mysql_conn)
        ensure_account_category_sales_table(mysql_conn)
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        z_code = str(zone).strip() if zone and str(zone).strip().lower() not in ("all", "null", "none", "") else "ALL"
        r_code = str(region).strip() if region and str(region).strip().lower() not in ("all", "null", "none", "") else "ALL"
        cur = mysql_conn.cursor()
        cur.execute(
            "DELETE FROM `dashboard_account_category_sales` WHERE session_id = %s AND zone_code = %s AND region_code = %s",
            (str(session_id).strip(), z_code, r_code)
        )
        sql = """
            INSERT INTO `dashboard_account_category_sales`
                (`session_id`, `zone_code`, `region_code`, `category_title`, `sales_value_cr`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                if cr <= 0:
                    continue
                batch.append((str(session_id).strip(), z_code, r_code, ct, cr, ist_now, ist_now))
        if batch:
            cur.executemany(sql, batch)
            mysql_conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Account Category Sales] Save error: {e}", flush=True)
        return False


def calculate_account_category_sales_hana(cursor, schema, zone=None, region=None):
    from helper.sap_connection import sanitize_and_execute_hana, detect_sales_dates, get_rolling_sales_boundary
    det_year, _, _ = detect_sales_dates(cursor, schema)
    curr_year = det_year or datetime.now().year
    start_year_date = f"{curr_year}-01-01"
    boundary_date = get_rolling_sales_boundary(cursor, schema)

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
            COALESCE(cs.NDP_VALUE, 0) AS invoice_value
        FROM (
            SELECT "DEALER_CODE", "NDP_VALUE", "PLANT", "INDICATOR_CANCEL", "DISTRIBUTION_CHANNEL", "invoice_date", "invoice_number"
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
            WHERE ("DISTRIBUTION_CHANNEL" = 10 OR "DISTRIBUTION_CHANNEL" = '10')
              AND ("INDICATOR_CANCEL" IS NULL OR "INDICATOR_CANCEL" NOT IN ('X', 'Y', '1'))
              AND "invoice_date" >= '{start_year_date}'
              AND "invoice_date" < '{boundary_date}'
            UNION ALL
            SELECT cs_raw."DEALER_CODE", cs_raw."NDP_VALUE", cs_raw."PLANT", cs_raw."INDICATOR_CANCEL", cs_raw."DISTRIBUTION_CHANNEL", cs_raw."invoice_date", cs_raw."invoice_number"
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" cs_raw
            WHERE (cs_raw."DISTRIBUTION_CHANNEL" = 10 OR cs_raw."DISTRIBUTION_CHANNEL" = '10')
              AND (cs_raw."INDICATOR_CANCEL" IS NULL OR cs_raw."INDICATOR_CANCEL" NOT IN ('X', 'Y', '1'))
              AND cs_raw."invoice_date" >= '{start_year_date}'
              AND cs_raw."invoice_date" >= '{boundary_date}'
        ) cs
        INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (
            LPAD(TRIM(TO_VARCHAR(cs.DEALER_CODE)), 10, '0') = LPAD(TRIM(TO_VARCHAR(cm.KUNNR)), 10, '0') 
            OR LTRIM(TO_VARCHAR(cs.DEALER_CODE), '0') = LTRIM(TO_VARCHAR(cm.KUNNR), '0')
        )
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm ON (TRIM(cm.BZIRK) = TRIM(tm.BZIRK))
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (TRIM(tm.REGIO) = TRIM(rm.region_code))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(NULLIF(TRIM(rm.zone_code), ''), NULLIF(TRIM(tm.P_ZONE), '')))
        WHERE (cs.INDICATOR_CANCEL IS NULL OR cs.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (cs.DISTRIBUTION_CHANNEL = 10 OR cs.DISTRIBUTION_CHANNEL = '10')
          AND cm.CH_TYPE IS NOT NULL 
          AND TRIM(cm.CH_TYPE) <> ''
          AND (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
               OR zm.NAME = ? OR zm.P_ZONE = ?
               OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
          AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All'
               OR rm."region_name" = ? OR rm."region_code" = ?
               OR UPPER(TRIM(rm."region_name")) = UPPER(TRIM(?)) OR UPPER(TRIM(rm."region_code")) = UPPER(TRIM(?))
               OR tm."REGIO" = ? OR UPPER(TRIM(tm."REGIO")) = UPPER(TRIM(?)))
    ),
    cat_sales AS (
        SELECT 
            title,
            ROUND(SUM(invoice_value) / 10000000, 2) AS "valCr"
        FROM filtered_data
        GROUP BY title
        HAVING SUM(invoice_value) > 0
    )
    SELECT 
        m.title AS "title",
        s."valCr" AS "valCr",
        '₹' || TO_VARCHAR(s."valCr") || ' Cr' AS "value"
    FROM master_cats m
    INNER JOIN cat_sales s ON (m.title = s.title)
    WHERE s."valCr" > 0
    ORDER BY "valCr" DESC, m.title ASC
    """
    params = (
        zone, zone, zone, zone, zone, zone, zone, zone,
        region, region, region, region, region, region, region, region, region, region
    )
    sanitize_and_execute_hana(cursor, schema, hana_sql, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def calculate_account_category_sales_mysql(cursor, session_id, zone=None, region=None):
    """Direct self-contained MySQL query function."""
    try:
        from helper.sap_connection import _prepare_mysql_workspace
        _prepare_mysql_workspace(cursor, session_id)

        if region and str(region).strip().lower() not in ("all", "null", "none", ""):
            reg_val = str(region).strip()
            z_val = str(zone).strip() if zone and str(zone).strip().lower() not in ("all", "null", "none", "") else None
            sql = """
                SELECT 
                    CASE 
                        WHEN cm.BRAND_SHOP = 'Y' THEN 'Brand Shops'
                        ELSE COALESCE(ag.account_group_name, 'Other')
                    END AS title,
                    ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2) AS valCr,
                    CONCAT('₹', ROUND(SUM(sd.Invoice_Value_INR) / 10000000, 2), ' Cr') AS value
                FROM sales_data sd
                JOIN customer_master cm ON sd.customer = cm.KUNNR
                LEFT JOIN account_group_master ag ON cm.acc_grp = ag.KTOKD
                LEFT JOIN territory_master tm ON cm.territory = tm.territory_code
                LEFT JOIN region_master rm ON tm.region_code = rm.region
                WHERE (%s IS NULL OR rm.zone = %s)
                  AND (rm.region = %s OR rm.region_name = %s OR UPPER(TRIM(rm.region_name)) = UPPER(TRIM(%s)))
                GROUP BY title
                ORDER BY valCr DESC;
            """
            cursor.execute(sql, (z_val, z_val, reg_val, reg_val, reg_val))
            if hasattr(cursor, 'fetchall'):
                return cursor.fetchall()
            return []

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
    finally:
        try:
            main_db = MYSQL_CONFIG.get("database", "dagent_v2")
            cursor.execute(f"USE `{main_db}`")
        except Exception:
            pass


def get_account_category_sales_controller(get_db_connection):
    session_id = request.args.get("session_id")
    zone = request.args.get("zone")
    region = request.args.get("region")
    force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")

    if request.is_json and request.get_json():
        body = request.get_json()
        session_id = session_id or body.get("session_id")
        zone = zone or body.get("zone")
        region = region or body.get("region")
        if not force_refresh:
            force_refresh = str(body.get("force_refresh", "")).lower() in ("true", "1")

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = \
        _get_connection_context(get_db_connection, session_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        # 1. Check MySQL table `dashboard_account_category_sales` first if not force_refresh
        if not force_refresh:
            cached = get_cached_account_category_sales(mysql_conn, effective_session, zone=zone, region=region)
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
            results = calculate_account_category_sales_hana(cursor, schema, zone=zone, region=region)
            cursor.close()
        else:
            cursor = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            results = calculate_account_category_sales_mysql(cursor, effective_session, zone=zone, region=region)
            cursor.close()

        # 3. Save into `dashboard_account_category_sales` table
        if results:
            save_account_category_sales_to_table(mysql_conn, effective_session, zone, results, region=region)

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
