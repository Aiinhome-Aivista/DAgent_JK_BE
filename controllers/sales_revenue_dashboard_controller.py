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


ZONE_MAP = {
    'CZ': 'CZ', 'CENTRAL ZONE': 'CZ',
    'EZ': 'EZ', 'EAST': 'EZ', 'EAST ZONE': 'EZ',
    'NZ': 'NZ', 'NORTH': 'NZ', 'NORTH ZONE': 'NZ',
    'SZ': 'SZ', 'SOUTH': 'SZ', 'SOUTH ZONE': 'SZ', 'SOUTH ZONE I': 'SZ',
    'TZ': 'TZ', 'SOUTH ZONE - II': 'TZ', 'SOUTH ZONE II': 'TZ',
    'WZ': 'WZ', 'WEST': 'WZ', 'WEST ZONE': 'WZ',
    'NP': 'NP', 'NEPAL': 'NP', 'NEPAL ZONE': 'NP'
}

def _normalize_zone(val):
    if not val:
        return None
    s = str(val).strip().upper()
    if s in ("", "ALL", "NONE", "NULL"):
        return None
    return ZONE_MAP.get(s, s)

def _get_user_scope(mysql_conn, user_id):
    scope = {"rank_id": 1, "zone_code": None, "region_code": None, "territory_code": None}
    if not user_id or not mysql_conn:
        return scope
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute("""
            SELECT rank_id, zone_code, region_code, territory_code
            FROM user_hierarchy_access
            WHERE user_id = %s
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            scope["rank_id"] = int(row.get("rank_id") or 4)
            z = str(row.get("zone_code") or "").strip()
            r = str(row.get("region_code") or "").strip()
            t = str(row.get("territory_code") or "").strip()
            if z and z.upper() not in ("ALL", "", "NONE", "NULL"):
                scope["zone_code"] = _normalize_zone(z)
            if r and r.upper() not in ("ALL", "", "NONE", "NULL"):
                scope["region_code"] = r
            if t and t.upper() not in ("ALL", "", "NONE", "NULL"):
                scope["territory_code"] = t
    except Exception as e:
        print(f"[Sales Revenue] User scope lookup note: {e}", flush=True)
    return scope


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


def calculate_sales_revenue_hana(cursor, schema, level='zone', zone=None, region=None, territory=None):
    from helper.sap_connection import sanitize_and_execute_hana
    from helper.sap_connection import detect_sales_dates
    det_year, _, _ = detect_sales_dates(cursor, schema)
    curr_year = det_year or datetime.now().year
    start_year_date = f"{curr_year}-01-01"
    plan_year_like = f"{curr_year}%"

    norm_zone = _normalize_zone(zone) if zone else None
    target_region = str(region).strip() if region and str(region).strip().upper() not in ('', 'ALL', 'NONE', 'NULL') else None
    target_territory = str(territory).strip() if territory and str(territory).strip().upper() not in ('', 'ALL', 'NONE', 'NULL') else None

    # Base CTEs
    base_ctes = f"""
        WITH
        CUSTOMER_MAP AS
        (
            SELECT DISTINCT
                LPAD(TO_NVARCHAR(C."KUNNR"), 10, '0') AS CUSTOMER_CODE,
                TRIM(C."BZIRK") AS "BZIRK",
                TRIM(TO_VARCHAR(C."REGIO")) AS "CUST_REGIO"
            FROM "{schema}"."ZIVF_CustomerMaster_01_MD" C
            WHERE IFNULL(C."KLABC", '') <> 'X'
        ),
        TERRITORY_MAP AS
        (
            SELECT DISTINCT
                TRIM(T."BZIRK") AS "BZIRK",
                TRIM(T."P_ZONE") AS "P_ZONE",
                TRIM(T."REGIO") AS "REGIO",
                TRIM(T."NAME") AS "TERRITORY_NAME",
                TRIM(T."WERKS") AS "WERKS"
            FROM "{schema}"."ZIVF_TerritoryMaster_01_TR" T
            WHERE T."BZIRK" IS NOT NULL AND T."BZIRK" <> ''
        ),
        REGION_MAP AS
        (
            SELECT DISTINCT
                TRIM(R."region_code") AS "REGION_CODE",
                TRIM(R."region_name") AS "REGION_NAME",
                TRIM(R."zone_code") AS "ZONE_CODE"
            FROM "{schema}"."ZIVF_RegionMaster_MD_01" R
            WHERE R."region_code" IS NOT NULL AND R."region_code" <> ''
        ),
        CURRENT_SALES AS
        (
            SELECT
                LPAD(TRIM(S."dealer_code"), 10, '0') AS CUSTOMER_CODE,
                COALESCE(S."ndp_value", 0) AS SALES_VALUE,
                S."plant" AS PLANT
            FROM (
                SELECT "dealer_code", "ndp_value", "plant", "distribution_channel", "indicator_cancel", "invoice_date", "invoice_number"
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
                WHERE "distribution_channel" = '10'
                  AND IFNULL("indicator_cancel", '') NOT IN ('X', 'Y', '1')
                  AND "invoice_date" >= '{start_year_date}'
                UNION ALL
                SELECT cs."dealer_code", cs."ndp_value", cs."plant", cs."distribution_channel", cs."indicator_cancel", cs."invoice_date", cs."invoice_number"
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" cs
                WHERE cs."distribution_channel" = '10'
                  AND IFNULL(cs."indicator_cancel", '') NOT IN ('X', 'Y', '1')
                  AND cs."invoice_date" >= '{start_year_date}'
                  AND NOT EXISTS (
                      SELECT 1 FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" sh 
                      WHERE sh."invoice_number" = cs."invoice_number"
                  )
            ) S
        ),
        SALES_WITH_GEO AS
        (
            SELECT
                S.SALES_VALUE,
                COALESCE(NULLIF(T."P_ZONE", ''), NULLIF(RM."ZONE_CODE", ''), 'UNKNOWN') AS "ZONE_CODE",
                COALESCE(NULLIF(RM."REGION_NAME", ''), NULLIF(T."REGIO", ''), 'UNKNOWN') AS "REGION_NAME",
                COALESCE(NULLIF(RM."REGION_CODE", ''), NULLIF(T."REGIO", ''), 'UNKNOWN') AS "REGION_CODE",
                COALESCE(NULLIF(T."BZIRK", ''), 'UNKNOWN') AS "TERR_CODE",
                COALESCE(NULLIF(T."TERRITORY_NAME", ''), NULLIF(T."BZIRK", ''), 'UNKNOWN') AS "TERRITORY_NAME"
            FROM CURRENT_SALES S
            LEFT JOIN CUSTOMER_MAP C ON S.CUSTOMER_CODE = C.CUSTOMER_CODE
            LEFT JOIN TERRITORY_MAP T ON (C."BZIRK" = T."BZIRK" OR (C."BZIRK" IS NULL AND S.PLANT = T."WERKS"))
            LEFT JOIN REGION_MAP RM ON (T."REGIO" = RM."REGION_CODE" OR (T."P_ZONE" = RM."ZONE_CODE" AND T."REGIO" = RM."REGION_NAME"))
        ),
        PLAN_WITH_GEO AS
        (
            SELECT
                IFNULL(P."PLAN_VALUE", 0) AS PLAN_VALUE,
                IFNULL(P."REV_PLAN_VAL", 0) AS REV_PLAN_VAL,
                COALESCE(NULLIF(T."P_ZONE", ''), NULLIF(RM."ZONE_CODE", ''), 'UNKNOWN') AS "ZONE_CODE",
                COALESCE(NULLIF(RM."REGION_NAME", ''), NULLIF(T."REGIO", ''), 'UNKNOWN') AS "REGION_NAME",
                COALESCE(NULLIF(RM."REGION_CODE", ''), NULLIF(T."REGIO", ''), 'UNKNOWN') AS "REGION_CODE",
                TRIM(P."TERR_CODE") AS "TERR_CODE",
                COALESCE(NULLIF(T."TERRITORY_NAME", ''), TRIM(P."TERR_CODE"), 'UNKNOWN') AS "TERRITORY_NAME"
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" P
            LEFT JOIN TERRITORY_MAP T ON TRIM(P."TERR_CODE") = T."BZIRK"
            LEFT JOIN REGION_MAP RM ON (T."REGIO" = RM."REGION_CODE" OR (T."P_ZONE" = RM."ZONE_CODE" AND T."REGIO" = RM."REGION_NAME"))
            WHERE P."YMONTH" LIKE '{plan_year_like}'
        )
    """

    if level == 'territory' and norm_zone:
        reg_cond = ""
        if target_region:
            reg_cond = f"AND (RM.\"REGION_CODE\" = '{target_region}' OR UPPER(RM.\"REGION_NAME\") = UPPER('{target_region}') OR T.\"REGIO\" = '{target_region}')"
        terr_cond = ""
        if target_territory:
            terr_cond = f"AND (T.\"BZIRK\" = '{target_territory}' OR UPPER(T.\"TERRITORY_NAME\") = UPPER('{target_territory}'))"

        hana_sql = f"""
        {base_ctes},
        ACTUAL_BY_TERRITORY AS
        (
            SELECT
                "TERR_CODE",
                "TERRITORY_NAME",
                "REGION_NAME",
                "REGION_CODE",
                "ZONE_CODE",
                SUM(SALES_VALUE) AS ACTUAL_SALES
            FROM SALES_WITH_GEO
            WHERE "ZONE_CODE" = '{norm_zone}'
              {f"AND ('{target_region}' IS NULL OR \"REGION_CODE\" = '{target_region}' OR UPPER(\"REGION_NAME\") = UPPER('{target_region}'))" if target_region else ""}
            GROUP BY "TERR_CODE", "TERRITORY_NAME", "REGION_NAME", "REGION_CODE", "ZONE_CODE"
        ),
        PLAN_BY_TERRITORY AS
        (
            SELECT
                "TERR_CODE",
                "TERRITORY_NAME",
                "REGION_NAME",
                "REGION_CODE",
                "ZONE_CODE",
                SUM(PLAN_VALUE) AS ORIGINAL_PLAN,
                SUM(REV_PLAN_VAL) AS REVISED_PLAN,
                COUNT(*) AS TARGET_RECORDS
            FROM PLAN_WITH_GEO
            WHERE "ZONE_CODE" = '{norm_zone}'
              {f"AND ('{target_region}' IS NULL OR \"REGION_CODE\" = '{target_region}' OR UPPER(\"REGION_NAME\") = UPPER('{target_region}'))" if target_region else ""}
            GROUP BY "TERR_CODE", "TERRITORY_NAME", "REGION_NAME", "REGION_CODE", "ZONE_CODE"
        )
        SELECT
            COALESCE(NULLIF(TRIM(T."TERRITORY_NAME"), ''), T."BZIRK") AS "territory",
            COALESCE(NULLIF(TRIM(T."TERRITORY_NAME"), ''), T."BZIRK") AS "city",
            T."BZIRK" AS "territoryCode",
            '{target_region or ""}' AS "region",
            '{norm_zone}' AS "zoneCode",
            ROUND(IFNULL(A.ACTUAL_SALES, 0) / 10000000, 2) AS "saleValueCr",
            ROUND(IFNULL(P.ORIGINAL_PLAN, 0) / 10000000, 2) AS "planValueCr",
            ROUND(IFNULL(P.REVISED_PLAN, 0) / 10000000, 2) AS "revisedPlanValueCr",
            ROUND(
                CASE WHEN IFNULL(P.ORIGINAL_PLAN, 0) = 0 THEN 0
                     ELSE IFNULL(A.ACTUAL_SALES, 0) / P.ORIGINAL_PLAN * 100 END, 2
            ) AS "achievValuePct",
            IFNULL(P.TARGET_RECORDS, 0) AS "targetRecords",
            'territory' AS "level"
        FROM (
            SELECT DISTINCT T."BZIRK", T."TERRITORY_NAME", T."P_ZONE", T."REGIO"
            FROM TERRITORY_MAP T
            LEFT JOIN REGION_MAP RM ON (T."REGIO" = RM."REGION_CODE" OR (T."P_ZONE" = RM."ZONE_CODE" AND T."REGIO" = RM."REGION_NAME"))
            WHERE T."P_ZONE" = '{norm_zone}'
              {reg_cond}
        ) T
        LEFT JOIN ACTUAL_BY_TERRITORY A ON T."BZIRK" = A."TERR_CODE"
        LEFT JOIN PLAN_BY_TERRITORY P ON T."BZIRK" = P."TERR_CODE"
        WHERE (IFNULL(A.ACTUAL_SALES, 0) > 0 OR IFNULL(P.ORIGINAL_PLAN, 0) > 0)
          {terr_cond}
        ORDER BY "saleValueCr" DESC
        """

    elif level == 'region' and norm_zone:
        reg_cond = ""
        if target_region:
            reg_cond = f"AND (RM.\"REGION_CODE\" = '{target_region}' OR UPPER(RM.\"REGION_NAME\") = UPPER('{target_region}'))"

        hana_sql = f"""
        {base_ctes},
        ACTUAL_BY_REGION AS
        (
            SELECT
                "REGION_NAME",
                "REGION_CODE",
                "ZONE_CODE",
                SUM(SALES_VALUE) AS ACTUAL_SALES
            FROM SALES_WITH_GEO
            WHERE "ZONE_CODE" = '{norm_zone}'
            GROUP BY "REGION_NAME", "REGION_CODE", "ZONE_CODE"
        ),
        PLAN_BY_REGION AS
        (
            SELECT
                "REGION_NAME",
                "REGION_CODE",
                "ZONE_CODE",
                SUM(PLAN_VALUE) AS ORIGINAL_PLAN,
                SUM(REV_PLAN_VAL) AS REVISED_PLAN,
                COUNT(*) AS TARGET_RECORDS
            FROM PLAN_WITH_GEO
            WHERE "ZONE_CODE" = '{norm_zone}'
            GROUP BY "REGION_NAME", "REGION_CODE", "ZONE_CODE"
        )
        SELECT
            RM."REGION_NAME" AS "region",
            RM."REGION_NAME" AS "city",
            RM."REGION_CODE" AS "regionCode",
            RM."ZONE_CODE" AS "zoneCode",
            ROUND(IFNULL(A.ACTUAL_SALES, 0) / 10000000, 2) AS "saleValueCr",
            ROUND(IFNULL(P.ORIGINAL_PLAN, 0) / 10000000, 2) AS "planValueCr",
            ROUND(IFNULL(P.REVISED_PLAN, 0) / 10000000, 2) AS "revisedPlanValueCr",
            ROUND(
                CASE WHEN IFNULL(P.ORIGINAL_PLAN, 0) = 0 THEN 0
                     ELSE IFNULL(A.ACTUAL_SALES, 0) / P.ORIGINAL_PLAN * 100 END, 2
            ) AS "achievValuePct",
            IFNULL(P.TARGET_RECORDS, 0) AS "targetRecords",
            'region' AS "level"
        FROM REGION_MAP RM
        LEFT JOIN ACTUAL_BY_REGION A ON (RM."REGION_CODE" = A."REGION_CODE" OR UPPER(RM."REGION_NAME") = UPPER(A."REGION_NAME"))
        LEFT JOIN PLAN_BY_REGION P ON (RM."REGION_CODE" = P."REGION_CODE" OR UPPER(RM."REGION_NAME") = UPPER(P."REGION_NAME"))
        WHERE RM."ZONE_CODE" = '{norm_zone}'
          {reg_cond}
          AND (IFNULL(A.ACTUAL_SALES, 0) > 0 OR IFNULL(P.ORIGINAL_PLAN, 0) > 0)
        ORDER BY "saleValueCr" DESC
        """

    else:
        zone_where = ""
        if norm_zone:
            zone_where = f"WHERE Z.\"P_ZONE\" = '{norm_zone}'"

        hana_sql = f"""
        {base_ctes},
        ACTUAL_BY_ZONE AS
        (
            SELECT
                "ZONE_CODE",
                SUM(SALES_VALUE) AS ACTUAL_SALES
            FROM SALES_WITH_GEO
            WHERE "ZONE_CODE" != 'UNKNOWN'
            GROUP BY "ZONE_CODE"
        ),
        PLAN_BY_ZONE AS
        (
            SELECT
                "ZONE_CODE",
                SUM(PLAN_VALUE) AS ORIGINAL_PLAN,
                SUM(REV_PLAN_VAL) AS REVISED_PLAN,
                COUNT(*) AS TARGET_RECORDS
            FROM PLAN_WITH_GEO
            WHERE "ZONE_CODE" != 'UNKNOWN'
            GROUP BY "ZONE_CODE"
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
            CASE Z."P_ZONE"
                WHEN 'CZ' THEN 'Central Zone'
                WHEN 'EZ' THEN 'EAST'
                WHEN 'NZ' THEN 'NORTH'
                WHEN 'NP' THEN 'Nepal'
                WHEN 'SZ' THEN 'SOUTH'
                WHEN 'TZ' THEN 'South Zone - II'
                WHEN 'WZ' THEN 'WEST'
                ELSE Z."NAME"
            END AS "city",
            Z."P_ZONE" AS "zoneCode",
            ROUND(IFNULL(A.ACTUAL_SALES, 0) / 10000000, 2) AS "saleValueCr",
            ROUND(IFNULL(P.ORIGINAL_PLAN, 0) / 10000000, 2) AS "planValueCr",
            ROUND(IFNULL(P.REVISED_PLAN, 0) / 10000000, 2) AS "revisedPlanValueCr",
            ROUND(
                CASE WHEN IFNULL(P.ORIGINAL_PLAN, 0) = 0 THEN 0
                     ELSE IFNULL(A.ACTUAL_SALES, 0) / P.ORIGINAL_PLAN * 100 END, 2
            ) AS "achievValuePct",
            IFNULL(P.TARGET_RECORDS, 0) AS "targetRecords",
            'zone' AS "level"
        FROM "{schema}"."ZIVF_ZoneMaster_01_TR" Z
        LEFT JOIN ACTUAL_BY_ZONE A ON Z."P_ZONE" = A."ZONE_CODE"
        LEFT JOIN PLAN_BY_ZONE P ON Z."P_ZONE" = P."ZONE_CODE"
        {zone_where}
        ORDER BY "saleValueCr" DESC
        """

    sanitize_and_execute_hana(cursor, schema, hana_sql)
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
    req_json = request.json if request.is_json and request.json else {}
    session_id = request.args.get("session_id") or req_json.get("session_id")
    level = request.args.get("level") or req_json.get("level") or "zone"
    zone = request.args.get("zone") or req_json.get("zone")
    region = request.args.get("region") or req_json.get("region")
    territory = request.args.get("territory") or req_json.get("territory")
    user_id = request.args.get("user_id") or req_json.get("user_id")
    force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1") or bool(req_json.get("force_refresh", False))

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = \
        _get_connection_context(get_db_connection, session_id, user_id_arg=user_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    # Resolve user authorization hierarchy bounds
    user_scope = _get_user_scope(mysql_conn, user_id)

    # Restrict Zone
    if user_scope.get("zone_code"):
        effective_zone = user_scope["zone_code"]
    else:
        effective_zone = _normalize_zone(zone)

    # Restrict Region
    if user_scope.get("region_code"):
        effective_region = user_scope["region_code"]
    else:
        effective_region = str(region).strip() if region and str(region).strip().upper() not in ("", "ALL", "NONE", "NULL") else None

    # Restrict Territory
    if user_scope.get("territory_code"):
        effective_territory = user_scope["territory_code"]
    else:
        effective_territory = str(territory).strip() if territory and str(territory).strip().upper() not in ("", "ALL", "NONE", "NULL") else None

    # Determine effective drilldown level
    effective_level = str(level).strip().lower()
    if effective_level not in ("zone", "region", "territory"):
        effective_level = "zone"

    # Auto-adjust level if needed:
    if effective_level == "territory" and not effective_region:
        effective_level = "region"
    if effective_level == "region" and not effective_zone:
        effective_level = "zone"

    cache_context = f"sr_lvl_{effective_level}_z_{effective_zone or 'ALL'}_r_{effective_region or 'ALL'}_t_{effective_territory or 'ALL'}_u{user_id or 'anon'}"

    try:
        from helper.dashboard_cache_service import get_cached_component_data, set_cached_component_data

        # 1. Check cache first
        if not force_refresh:
            cached_res = get_cached_component_data(mysql_conn, effective_session, "sales_revenue", cache_context)
            if cached_res and cached_res.get("data"):
                has_valid_data = any(float(x.get("planValueCr") or 0) > 0 or float(x.get("saleValueCr") or 0) > 0 for x in cached_res["data"])
                if has_valid_data:
                    return jsonify({
                        "status": "success",
                        "source": "dashboard_cached_metrics",
                        "level": effective_level,
                        "zone": effective_zone,
                        "region": effective_region,
                        "territory": effective_territory,
                        "data": cached_res["data"],
                        "_cached": True,
                        "_last_synced_at": cached_res.get("_last_synced_at")
                    }), 200

            # Fallback for zone-level cache in sales_revenue_plan_vs_sale
            if effective_level == "zone":
                cached = get_cached_sales_revenue(mysql_conn, effective_session, zone=effective_zone)
                if cached and cached.get("data"):
                    has_valid_data = any(float(x.get("planValueCr") or 0) > 0 or float(x.get("saleValueCr") or 0) > 0 for x in cached["data"])
                    if has_valid_data:
                        return jsonify({
                            "status": "success",
                            "source": "sales_revenue_plan_vs_sale",
                            "level": "zone",
                            "zone": effective_zone,
                            "data": cached["data"],
                            "_cached": True,
                            "_last_synced_at": cached.get("_last_synced_at")
                        }), 200

        # 2. Compute dynamically using self-contained query function
        if is_hana:
            cursor = hana_conn.cursor()
            results = calculate_sales_revenue_hana(
                cursor,
                schema,
                level=effective_level,
                zone=effective_zone,
                region=effective_region,
                territory=effective_territory
            )
            cursor.close()
        else:
            cursor = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            results = calculate_sales_revenue_mysql(cursor, effective_session, zone=effective_zone)
            cursor.close()

        # 3. Save into cache
        if results:
            try:
                set_cached_component_data(mysql_conn, effective_session, "sales_revenue", cache_context, results, row_count=len(results))
            except Exception as ce:
                print(f"[Sales Revenue] Cache set note: {ce}", flush=True)

            if effective_level == "zone":
                save_sales_revenue_to_table(mysql_conn, effective_session, effective_zone or "ALL", results)

        return jsonify({
            "status": "success",
            "source": "live_query",
            "level": effective_level,
            "zone": effective_zone,
            "region": effective_region,
            "territory": effective_territory,
            "data": results or []
        }), 200

    except Exception as e:
        print(f"[Sales Revenue] Controller error: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel)
