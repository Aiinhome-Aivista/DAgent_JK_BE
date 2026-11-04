"""
sales_revenue_dashboard_controller.py
─────────────────────────────────────────────────────────────────────────────
Dedicated controller for Sales Revenue (Plan vs Sale).
Reads from and synchronizes MySQL table `sales_revenue_plan_vs_sale`.

Self-contained query functions without external service fallback:
- Actual Sales from ZPVF_SD_CurrentSales_01_TR (Current Year 2026)
- Plan Target from ZIVF_SD_Sales_Plan_Target_01_TR (YMONTH LIKE '2026%')
─────────────────────────────────────────────────────────────────────────────
"""

from datetime import datetime, timezone, timedelta
from flask import request, jsonify

SALES_REVENUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `sales_revenue_plan_vs_sale` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `city_or_zone_name` VARCHAR(100) NOT NULL,
    `plan_value_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `sale_value_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `achiev_value_pct` DECIMAL(8, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_session_zone_city` (`session_id`, `zone_code`, `city_or_zone_name`),
    INDEX `idx_session_zone` (`session_id`, `zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_sr_table_initialized = False


def ensure_sales_revenue_table(conn):
    global _sr_table_initialized
    if _sr_table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(SALES_REVENUE_TABLE_SQL)
        conn.commit()
        cur.close()
        _sr_table_initialized = True
    except Exception as e:
        print(f"[Sales Revenue Table] init note: {e}", flush=True)


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
        print(f"[Sales Revenue] HANA connection note: {e}", flush=True)
        is_hana = False

    return mysql_conn, hana_conn, tunnel, is_hana, sap_schema, effective_session


def _close_connections(mysql_conn, hana_conn, tunnel, cursor=None):
    if cursor:
        try: cursor.close()
        except Exception: pass
    if hana_conn:
        try: hana_conn.close()
        except Exception: pass
    if tunnel:
        try: tunnel.stop()
        except Exception: pass
    if mysql_conn and hasattr(mysql_conn, "is_connected") and mysql_conn.is_connected():
        try: mysql_conn.close()
        except Exception: pass
    elif mysql_conn:
        try: mysql_conn.close()
        except Exception: pass


def get_cached_sales_revenue(mysql_conn, session_id, zone=None):
    ensure_sales_revenue_table(mysql_conn)
    zone_val = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute("""
            SELECT city_or_zone_name, plan_value_cr, sale_value_cr, achiev_value_pct, last_synced_at
            FROM `sales_revenue_plan_vs_sale`
            WHERE session_id = %s AND zone_code = %s
            ORDER BY city_or_zone_name ASC
        """, (str(session_id).strip(), zone_val))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return None

        result = []
        last_sync = None
        for r in rows:
            c_name = r.get("city_or_zone_name") if isinstance(r, dict) else r[0]
            p_val = float(r.get("plan_value_cr") if isinstance(r, dict) else r[1])
            s_val = float(r.get("sale_value_cr") if isinstance(r, dict) else r[2])
            a_pct = float(r.get("achiev_value_pct") if isinstance(r, dict) else r[3])
            last_sync = r.get("last_synced_at") if isinstance(r, dict) else r[4]
            result.append({
                "zone": c_name,
                "city": c_name,
                "city_or_zone_name": c_name,
                "planValueCr": p_val,
                "saleValueCr": s_val,
                "achievValuePct": a_pct
            })

        return {"data": result, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[Sales Revenue] Cache read error: {e}", flush=True)
        return None


def save_sales_revenue_to_table(mysql_conn, session_id, zone, chart_data):
    if not mysql_conn or not session_id or not chart_data:
        return False
    try:
        ensure_sales_revenue_table(mysql_conn)
        zone_val = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')

        cur = mysql_conn.cursor()
        cur.execute(
            "DELETE FROM `sales_revenue_plan_vs_sale` WHERE session_id = %s AND zone_code = %s",
            (str(session_id).strip(), zone_val)
        )

        sql = """
            INSERT INTO `sales_revenue_plan_vs_sale`
                (`session_id`, `zone_code`, `city_or_zone_name`, `plan_value_cr`, `sale_value_cr`, `achiev_value_pct`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                `plan_value_cr` = VALUES(`plan_value_cr`),
                `sale_value_cr` = VALUES(`sale_value_cr`),
                `achiev_value_pct` = VALUES(`achiev_value_pct`),
                `last_synced_at` = VALUES(`last_synced_at`)
        """

        batch = []
        for item in chart_data:
            c_name = str(item.get("city_or_zone_name") or item.get("city") or item.get("zone") or item.get("region") or "").strip()[:100]
            if not c_name:
                continue
            p_val = float(item.get("planValueCr") or 0.0)
            s_val = float(item.get("saleValueCr") or 0.0)
            a_pct = float(item.get("achievValuePct") or 0.0)
            batch.append((str(session_id).strip(), zone_val, c_name, p_val, s_val, a_pct, ist_now, ist_now))

        if batch:
            cur.executemany(sql, batch)
            mysql_conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Sales Revenue] Save error: {e}", flush=True)
        return False


def calculate_sales_revenue_hana(cursor, schema, zone=None):
    from helper.sap_connection import sanitize_and_execute_hana
    if not zone or str(zone).strip().lower() in ('', 'null', 'all'):
        hana_sql = f"""
        WITH
        CUSTOMER_MAP AS
        (
            SELECT DISTINCT
                LPAD(TO_NVARCHAR(C."KUNNR"), 10, '0') AS CUSTOMER_CODE,
                C."BZIRK"
            FROM "{schema}"."ZIVF_CustomerMaster_01_MD" C
            WHERE IFNULL(C."KLABC", '') <> 'X'
        ),
        TERRITORY_MAP AS
        (
            SELECT DISTINCT
                T."BZIRK",
                T."P_ZONE"
            FROM "{schema}"."ZIVF_TerritoryMaster_01_TR" T
        ),
        CURRENT_SALES AS
        (
            SELECT
                LPAD(TRIM(S."dealer_code"), 10, '0') AS CUSTOMER_CODE,
                COALESCE(S."ndp_value", S."taxable_value", S."invoice_value", 0) AS SALES_VALUE
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" S
            WHERE S."distribution_channel" = '10'
              AND IFNULL(S."indicator_cancel", '') NOT IN ('X', 'Y', '1')
        ),
        ACTUAL_BY_ZONE AS
        (
            SELECT
                T."P_ZONE",
                SUM(S.SALES_VALUE) AS ACTUAL_SALES
            FROM CURRENT_SALES S
            INNER JOIN CUSTOMER_MAP C ON S.CUSTOMER_CODE = C.CUSTOMER_CODE
            INNER JOIN TERRITORY_MAP T ON C."BZIRK" = T."BZIRK"
            GROUP BY T."P_ZONE"
        ),
        PLAN_BY_ZONE AS
        (
            SELECT
                T."P_ZONE",
                SUM(IFNULL(P."PLAN_VALUE", 0)) AS ORIGINAL_PLAN,
                SUM(IFNULL(P."REV_PLAN_VAL", 0)) AS REVISED_PLAN,
                COUNT(*) AS TARGET_RECORDS
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" P
            INNER JOIN TERRITORY_MAP T ON TRIM(P."TERR_CODE") = TRIM(T."BZIRK")
            WHERE P."YMONTH" LIKE '2026%'
            GROUP BY T."P_ZONE"
        )
        SELECT
            CASE Z."P_ZONE"
                WHEN 'CZ' THEN 'Central Zone'
                WHEN 'EZ' THEN 'EAST'
                WHEN 'NZ' THEN 'NORTH'
                WHEN 'NP' THEN 'Nepal'
                WHEN 'SZ' THEN 'SOUTH'
                WHEN 'TZ' THEN 'South Zone - II'
                WHEN 'WZ' THEN 'WEST'
                ELSE Z."NAME"
            END AS "zone",
            ROUND(IFNULL(A.ACTUAL_SALES, 0) / 10000000, 2) AS "saleValueCr",
            ROUND(IFNULL(P.ORIGINAL_PLAN, 0) / 10000000, 2) AS "planValueCr",
            ROUND(IFNULL(P.REVISED_PLAN, 0) / 10000000, 2) AS "revisedPlanValueCr",
            ROUND(
                CASE WHEN IFNULL(P.ORIGINAL_PLAN, 0) = 0 THEN 0
                     ELSE IFNULL(A.ACTUAL_SALES, 0) / P.ORIGINAL_PLAN * 100 END, 2
            ) AS "achievValuePct",
            Z."P_ZONE" AS "zoneCode",
            IFNULL(P.TARGET_RECORDS, 0) AS "targetRecords"
        FROM "{schema}"."ZIVF_ZoneMaster_01_TR" Z
        LEFT JOIN ACTUAL_BY_ZONE A ON Z."P_ZONE" = A."P_ZONE"
        LEFT JOIN PLAN_BY_ZONE P ON Z."P_ZONE" = P."P_ZONE"
        ORDER BY "saleValueCr" DESC
        """
        sanitize_and_execute_hana(cursor, schema, hana_sql)
    else:
        # Regional drill-down for selected zone
        hana_sql = f"""
        SELECT 
            COALESCE(NULLIF(TRIM(rm.REGION_NAME), ''), NULLIF(TRIM(tm_plant.NAME), ''), NULLIF(TRIM(tm_bzirk.NAME), ''), 'Other Region') AS "region", 
            ROUND(SUM(COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0)) / 10000000, 2) AS "saleValueCr",
            0 AS "planValueCr",
            0 AS "achievValuePct"
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" cs
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cust 
            ON (LPAD(TRIM(TO_VARCHAR(cs.DEALER_CODE)), 10, '0') = LPAD(TRIM(TO_VARCHAR(cust.KUNNR)), 10, '0') OR LTRIM(TO_VARCHAR(cs.DEALER_CODE), '0') = LTRIM(TO_VARCHAR(cust.KUNNR), '0'))
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cust.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cust.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), '')
        ))
        WHERE (cust.KLABC IS NULL OR UPPER(TRIM(cust.KLABC)) <> 'X')
          AND (cs.INDICATOR_CANCEL IS NULL OR cs.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (cs.DISTRIBUTION_CHANNEL = 10 OR cs.DISTRIBUTION_CHANNEL = '10')
          AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
               OR zm.NAME = ? OR zm.P_ZONE = ?
               OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
        GROUP BY COALESCE(NULLIF(TRIM(rm.REGION_NAME), ''), NULLIF(TRIM(tm_plant.NAME), ''), NULLIF(TRIM(tm_bzirk.NAME), ''), 'Other Region')
        HAVING SUM(COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0)) > 0
        ORDER BY "saleValueCr" DESC
        """
        sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone))

    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def calculate_sales_revenue_mysql(cursor, session_id, zone=None):
    try:
        args = (zone,) if zone and str(zone).strip().lower() != "all" else (None,)
        cursor.callproc('sp_get_sales_revenue_by_zone', args)
        rows = []
        if hasattr(cursor, 'stored_results'):
            for res in cursor.stored_results():
                rows = res.fetchall()
                break
        if not rows and hasattr(cursor, 'fetchall') and cursor.description:
            rows = cursor.fetchall()
        return rows
    except Exception as e:
        print(f"[Sales Revenue] MySQL calc note: {e}", flush=True)
        return []


def get_sales_revenue_data_controller(get_db_connection):
    session_id = request.args.get("session_id")
    zone = request.args.get("zone")
    force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = \
        _get_connection_context(get_db_connection, session_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        # 1. Check dedicated MySQL table `sales_revenue_plan_vs_sale` first
        if not force_refresh:
            cached = get_cached_sales_revenue(mysql_conn, effective_session, zone=zone)
            if cached and cached.get("data"):
                return jsonify({
                    "status": "success",
                    "source": "sales_revenue_plan_vs_sale",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        # 2. Compute dynamically using self-contained query function
        if is_hana:
            cursor = hana_conn.cursor()
            results = calculate_sales_revenue_hana(cursor, schema, zone=zone)
            cursor.close()
        else:
            cursor = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            results = calculate_sales_revenue_mysql(cursor, effective_session, zone=zone)
            cursor.close()

        # 3. Save into `sales_revenue_plan_vs_sale` table
        if results:
            save_sales_revenue_to_table(mysql_conn, effective_session, zone, results)

        return jsonify({
            "status": "success",
            "source": "sales_revenue_plan_vs_sale",
            "data": results or []
        }), 200

    except Exception as e:
        print(f"[Sales Revenue] Controller error: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel)
