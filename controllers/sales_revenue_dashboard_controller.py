"""
sales_revenue_dashboard_controller.py
─────────────────────────────────────────────────────────────────────────────
Dedicated controller for Sales Revenue (Plan vs Sale & Achievement).
Reads from and synchronizes MySQL cache table `sales_revenue_plan_vs_sale`.

Hierarchical Drilldown:
- Level 1: Zone (All Zones for unrestricted, Assigned Zone for restricted)
- Level 2: Region (Drilldown into selected Zone, filtered by assigned region if restricted)
- Level 3: Territory (Drilldown into selected Region)

Plan Target from ZIVF_SD_Sales_Plan_Target_01_TR (YMONTH LIKE '2026%')
Actual Sales from ZPVF_SD_CurrentSales_01_TR + ZPVF_SD_SalesHistory_01_TR
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
        print(f"[Sales Revenue] Table ensure error: {e}", flush=True)


ZONE_NAME_MAP = {
    'CZ': 'Central Zone', 'CENTRAL ZONE': 'Central Zone',
    'EZ': 'East Zone', 'EAST': 'East Zone', 'EAST ZONE': 'East Zone',
    'NZ': 'North Zone', 'NORTH': 'North Zone', 'NORTH ZONE': 'North Zone',
    'SZ': 'South Zone I', 'SOUTH': 'South Zone I', 'SOUTH ZONE': 'South Zone I', 'SOUTH ZONE I': 'South Zone I',
    'TZ': 'South Zone II', 'SOUTH ZONE - II': 'South Zone II', 'SOUTH ZONE II': 'South Zone II',
    'WZ': 'West Zone', 'WEST': 'West Zone', 'WEST ZONE': 'West Zone',
    'NP': 'Nepal Zone', 'NEPAL': 'Nepal Zone', 'NEPAL ZONE': 'Nepal Zone'
}

ZONE_CODE_MAP = {
    'CZ': 'CZ', 'CENTRAL ZONE': 'CZ',
    'EZ': 'EZ', 'EAST': 'EZ', 'EAST ZONE': 'EZ',
    'NZ': 'NZ', 'NORTH': 'NZ', 'NORTH ZONE': 'NZ',
    'SZ': 'SZ', 'SOUTH': 'SZ', 'SOUTH ZONE': 'SZ', 'SOUTH ZONE I': 'SZ',
    'TZ': 'TZ', 'SOUTH ZONE - II': 'TZ', 'SOUTH ZONE II': 'TZ',
    'WZ': 'WZ', 'WEST': 'WZ', 'WEST ZONE': 'WZ',
    'NP': 'NP', 'NEPAL': 'NP', 'NEPAL ZONE': 'NP'
}


def _normalize_zone_code(val):
    if not val:
        return None
    s = str(val).strip().upper()
    if s in ("", "ALL", "NONE", "NULL"):
        return None
    return ZONE_CODE_MAP.get(s, s)


def _normalize_zone_name(val):
    if not val:
        return None
    s = str(val).strip().upper()
    if s in ("", "ALL", "NONE", "NULL"):
        return None
    return ZONE_NAME_MAP.get(s, val)


def _get_user_hierarchy_scope(mysql_conn, user_id):
    """
    Returns (assigned_zone_code, assigned_zone_name, assigned_region, assigned_territory, is_restricted)
    If user is National Head (Rank 1) or has ALL/empty boundaries, is_restricted is False.
    """
    if not user_id or not mysql_conn:
        return None, None, None, None, False

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

        if not row:
            return None, None, None, None, False

        rank_id = int(row.get("rank_id") or 4)
        z = str(row.get("zone_code") or "").strip()
        r = str(row.get("region_code") or "").strip()
        t = str(row.get("territory_code") or "").strip()

        # Unrestricted if rank 1 or zone is 'ALL' or blank
        if rank_id == 1 or not z or z.upper() in ("ALL", "NONE", "NULL", ""):
            return None, None, None, None, False

        assigned_zc = _normalize_zone_code(z)
        assigned_zn = _normalize_zone_name(z)
        assigned_r = r if r and r.upper() not in ("ALL", "NONE", "NULL", "") else None
        assigned_t = t if t and t.upper() not in ("ALL", "NONE", "NULL", "") else None

        return assigned_zc, assigned_zn, assigned_r, assigned_t, True
    except Exception as e:
        print(f"[Sales Revenue] Hierarchy check error: {e}", flush=True)
        return None, None, None, None, False


def _resolve_session(mysql_conn, session_id):
    if session_id and str(session_id).strip() and str(session_id).strip().lower() != "null":
        return str(session_id).strip()
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute("SELECT session_id FROM session_tracking ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row:
            return row["session_id"] if isinstance(row, dict) else row[0]
    except Exception:
        pass
    return "active_session"


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


def get_cached_sales_revenue(mysql_conn, session_id, zone=None):
    if not mysql_conn or not session_id:
        return None
    try:
        ensure_sales_revenue_table(mysql_conn)
        norm_zone_code = _normalize_zone_code(zone)
        norm_zone_name = _normalize_zone_name(zone)
        zone_val = norm_zone_code if norm_zone_code else "ALL"

        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        if zone_val == "ALL":
            cur.execute(
                """
                SELECT city_or_zone_name, plan_value_cr, sale_value_cr, achiev_value_pct, last_synced_at
                FROM sales_revenue_plan_vs_sale
                WHERE session_id = %s AND zone_code = 'ALL'
                ORDER BY sale_value_cr DESC
                """,
                (str(session_id).strip(),)
            )
        else:
            cur.execute(
                """
                SELECT city_or_zone_name, plan_value_cr, sale_value_cr, achiev_value_pct, last_synced_at
                FROM sales_revenue_plan_vs_sale
                WHERE session_id = %s AND (
                    zone_code = %s
                    OR (zone_code = 'ALL' AND (UPPER(city_or_zone_name) = UPPER(%s) OR UPPER(city_or_zone_name) = UPPER(%s)))
                )
                ORDER BY sale_value_cr DESC
                """,
                (str(session_id).strip(), zone_val, norm_zone_name or zone_val, norm_zone_code or zone_val)
            )
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
            s_val = float(item.get("saleValueCr") or item.get("saleV") or 0.0)
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
    """
    Executes accurate, CTE-based SAP HANA queries for Zone, Region, and Territory levels.
    Calculates Actual Sales from CurrentSales + SalesHistory,
    and Target Plan from ZIVF_SD_Sales_Plan_Target_01_TR.
    """
    from helper.sap_connection import sanitize_and_execute_hana
    from helper.sap_connection import detect_sales_dates

    det_year, _, _ = detect_sales_dates(cursor, schema)
    curr_year = det_year or datetime.now().year
    start_year_date = f"{curr_year}-01-01"
    plan_year_like = f"{curr_year}%"

    norm_zone_code = _normalize_zone_code(zone)
    norm_zone_name = _normalize_zone_name(zone)
    target_reg = str(region).strip() if region and str(region).strip().upper() not in ('', 'ALL', 'NONE', 'NULL') else None
    target_terr = str(territory).strip() if territory and str(territory).strip().upper() not in ('', 'ALL', 'NONE', 'NULL') else None

    # Base CTEs shared across all 3 levels
    common_ctes = f"""
    WITH
    CUSTOMER_MAP AS
    (
        SELECT DISTINCT
            LPAD(TO_NVARCHAR(C."KUNNR"), 10, '0') AS CUSTOMER_CODE,
            TRIM(C."BZIRK") AS BZIRK
        FROM "{schema}"."ZIVF_CustomerMaster_01_MD" C
        WHERE IFNULL(C."KLABC", '') <> 'X'
    ),
    TERRITORY_MAP AS
    (
        SELECT DISTINCT
            TRIM(T."BZIRK") AS BZIRK,
            TRIM(T."NAME") AS TERRITORY_NAME,
            TRIM(T."REGIO") AS REGIO,
            TRIM(T."P_ZONE") AS P_ZONE
        FROM "{schema}"."ZIVF_TerritoryMaster_01_TR" T
    ),
    REGION_MAP AS
    (
        SELECT DISTINCT
            TRIM(R."region_code") AS REGION_CODE,
            TRIM(R."region_name") AS REGION_NAME,
            TRIM(R."zone_code") AS ZONE_CODE
        FROM "{schema}"."ZIVF_RegionMaster_MD_01" R
    ),
    CURRENT_SALES AS
    (
        SELECT
            LPAD(TRIM(S."dealer_code"), 10, '0') AS CUSTOMER_CODE,
            COALESCE(S."ndp_value", 0) AS SALES_VALUE
        FROM (
            SELECT "dealer_code", "ndp_value", "distribution_channel", "indicator_cancel", "invoice_date", "invoice_number"
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
            WHERE "distribution_channel" = '10'
              AND IFNULL("indicator_cancel", '') NOT IN ('X', 'Y', '1')
              AND "invoice_date" >= '{start_year_date}'
            UNION ALL
            SELECT cs."dealer_code", cs."ndp_value", cs."distribution_channel", cs."indicator_cancel", cs."invoice_date", cs."invoice_number"
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" cs
            WHERE cs."distribution_channel" = '10'
              AND IFNULL(cs."indicator_cancel", '') NOT IN ('X', 'Y', '1')
              AND cs."invoice_date" >= '{start_year_date}'
              AND NOT EXISTS (
                  SELECT 1 FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" sh 
                  WHERE sh."invoice_number" = cs."invoice_number"
              )
        ) S
    )
    """

    # LEVEL 1: Zone Level
    if level == 'zone':
        zone_where = ""
        if norm_zone_code:
            zone_where = f"WHERE Z.\"P_ZONE\" = '{norm_zone_code}'"

        hana_sql = f"""
        {common_ctes},
        ACTUAL_BY_ZONE AS
        (
            SELECT
                COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, '')) AS P_ZONE,
                SUM(S.SALES_VALUE) AS ACTUAL_SALES
            FROM CURRENT_SALES S
            INNER JOIN CUSTOMER_MAP C ON S.CUSTOMER_CODE = C.CUSTOMER_CODE
            INNER JOIN TERRITORY_MAP T ON C.BZIRK = T.BZIRK
            LEFT JOIN REGION_MAP RM ON (T.REGIO = RM.REGION_CODE OR (T.P_ZONE = RM.ZONE_CODE AND T.REGIO = RM.REGION_NAME))
            GROUP BY COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, ''))
        ),
        PLAN_BY_ZONE AS
        (
            SELECT
                COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, '')) AS P_ZONE,
                SUM(IFNULL(P."PLAN_VALUE", 0)) AS ORIGINAL_PLAN,
                SUM(IFNULL(P."REV_PLAN_VAL", 0)) AS REVISED_PLAN,
                COUNT(*) AS TARGET_RECORDS
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" P
            INNER JOIN TERRITORY_MAP T ON TRIM(P."TERR_CODE") = T.BZIRK
            LEFT JOIN REGION_MAP RM ON (T.REGIO = RM.REGION_CODE OR (T.P_ZONE = RM.ZONE_CODE AND T.REGIO = RM.REGION_NAME))
            WHERE P."YMONTH" LIKE '{plan_year_like}'
            GROUP BY COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, ''))
        )
        SELECT
            CASE Z."P_ZONE"
                WHEN 'CZ' THEN 'Central Zone'
                WHEN 'EZ' THEN 'East Zone'
                WHEN 'NZ' THEN 'North Zone'
                WHEN 'NP' THEN 'Nepal Zone'
                WHEN 'SZ' THEN 'South Zone I'
                WHEN 'TZ' THEN 'South Zone II'
                WHEN 'WZ' THEN 'West Zone'
                ELSE Z."NAME"
            END AS "zone",
            CASE Z."P_ZONE"
                WHEN 'CZ' THEN 'Central Zone'
                WHEN 'EZ' THEN 'East Zone'
                WHEN 'NZ' THEN 'North Zone'
                WHEN 'NP' THEN 'Nepal Zone'
                WHEN 'SZ' THEN 'South Zone I'
                WHEN 'TZ' THEN 'South Zone II'
                WHEN 'WZ' THEN 'West Zone'
                ELSE Z."NAME"
            END AS "city",
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
        LEFT JOIN ACTUAL_BY_ZONE A ON Z."P_ZONE" = A.P_ZONE
        LEFT JOIN PLAN_BY_ZONE P ON Z."P_ZONE" = P.P_ZONE
        {zone_where}
        ORDER BY "saleValueCr" DESC
        """
        sanitize_and_execute_hana(cursor, schema, hana_sql)

    # LEVEL 3: Territory Level (Drilldown into selected Region)
    elif level == 'territory':
        where_clauses = []
        if norm_zone_code:
            where_clauses.append(f"UPPER(TRIM(COALESCE(A.P_ZONE, P.P_ZONE))) = '{norm_zone_code}'")
        if target_reg:
            where_clauses.append(f"""(
                UPPER(TRIM(COALESCE(A.REGION_NAME, P.REGION_NAME))) = UPPER(TRIM('{target_reg}'))
                OR UPPER(TRIM(COALESCE(A.REG_CODE, P.REG_CODE))) = UPPER(TRIM('{target_reg}'))
            )""")
        if target_terr:
            where_clauses.append(f"""(
                UPPER(TRIM(COALESCE(A.TERR_CODE, P.TERR_CODE))) = UPPER(TRIM('{target_terr}'))
                OR UPPER(TRIM(COALESCE(A.TERRITORY_NAME, P.TERRITORY_NAME))) = UPPER(TRIM('{target_terr}'))
            )""")

        filter_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        hana_sql = f"""
        {common_ctes},
        ACTUAL_BY_TERRITORY AS
        (
            SELECT
                T.BZIRK AS TERR_CODE,
                COALESCE(NULLIF(T.TERRITORY_NAME, ''), T.BZIRK) AS TERRITORY_NAME,
                COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region') AS REGION_NAME,
                COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')) AS REG_CODE,
                COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, '')) AS P_ZONE,
                SUM(S.SALES_VALUE) AS ACTUAL_SALES
            FROM CURRENT_SALES S
            INNER JOIN CUSTOMER_MAP C ON S.CUSTOMER_CODE = C.CUSTOMER_CODE
            INNER JOIN TERRITORY_MAP T ON C.BZIRK = T.BZIRK
            LEFT JOIN REGION_MAP RM ON (T.REGIO = RM.REGION_CODE OR (T.P_ZONE = RM.ZONE_CODE AND T.REGIO = RM.REGION_NAME))
            GROUP BY T.BZIRK, COALESCE(NULLIF(T.TERRITORY_NAME, ''), T.BZIRK), 
                     COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region'),
                     COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')),
                     COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, ''))
        ),
        PLAN_BY_TERRITORY AS
        (
            SELECT
                T.BZIRK AS TERR_CODE,
                COALESCE(NULLIF(T.TERRITORY_NAME, ''), T.BZIRK) AS TERRITORY_NAME,
                COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region') AS REGION_NAME,
                COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')) AS REG_CODE,
                COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, '')) AS P_ZONE,
                SUM(IFNULL(P."PLAN_VALUE", 0)) AS ORIGINAL_PLAN
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" P
            INNER JOIN TERRITORY_MAP T ON TRIM(P."TERR_CODE") = T.BZIRK
            LEFT JOIN REGION_MAP RM ON (T.REGIO = RM.REGION_CODE OR (T.P_ZONE = RM.ZONE_CODE AND T.REGIO = RM.REGION_NAME))
            WHERE P."YMONTH" LIKE '{plan_year_like}'
            GROUP BY T.BZIRK, COALESCE(NULLIF(T.TERRITORY_NAME, ''), T.BZIRK), 
                     COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region'),
                     COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')),
                     COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, ''))
        )
        SELECT
            COALESCE(A.TERRITORY_NAME, P.TERRITORY_NAME, A.TERR_CODE, P.TERR_CODE) AS "city",
            COALESCE(A.TERRITORY_NAME, P.TERRITORY_NAME, A.TERR_CODE, P.TERR_CODE) AS "territory",
            COALESCE(A.TERR_CODE, P.TERR_CODE) AS "territoryCode",
            COALESCE(A.REGION_NAME, P.REGION_NAME) AS "region",
            COALESCE(A.P_ZONE, P.P_ZONE) AS "zoneCode",
            ROUND(IFNULL(A.ACTUAL_SALES, 0) / 10000000, 2) AS "saleValueCr",
            ROUND(IFNULL(P.ORIGINAL_PLAN, 0) / 10000000, 2) AS "planValueCr",
            ROUND(
                CASE WHEN IFNULL(P.ORIGINAL_PLAN, 0) = 0 THEN 0
                     ELSE IFNULL(A.ACTUAL_SALES, 0) / P.ORIGINAL_PLAN * 100
                END, 2
            ) AS "achievValuePct"
        FROM ACTUAL_BY_TERRITORY A
        FULL OUTER JOIN PLAN_BY_TERRITORY P 
            ON (A.TERR_CODE = P.TERR_CODE)
        {filter_sql}
        ORDER BY "saleValueCr" DESC
        """
        sanitize_and_execute_hana(cursor, schema, hana_sql)

    # LEVEL 2: Region Level (Drilldown into selected Zone)
    else:
        where_clauses = []
        if norm_zone_code:
            where_clauses.append(f"UPPER(TRIM(COALESCE(A.P_ZONE, P.P_ZONE))) = '{norm_zone_code}'")
        if target_reg:
            where_clauses.append(f"""(
                UPPER(TRIM(COALESCE(A.REGION_NAME, P.REGION_NAME))) = UPPER(TRIM('{target_reg}'))
                OR UPPER(TRIM(COALESCE(A.REG_CODE, P.REG_CODE))) = UPPER(TRIM('{target_reg}'))
            )""")

        filter_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        hana_sql = f"""
        {common_ctes},
        ACTUAL_BY_REGION AS
        (
            SELECT
                COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region') AS REGION_NAME,
                COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')) AS REG_CODE,
                COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, '')) AS P_ZONE,
                SUM(S.SALES_VALUE) AS ACTUAL_SALES
            FROM CURRENT_SALES S
            INNER JOIN CUSTOMER_MAP C ON S.CUSTOMER_CODE = C.CUSTOMER_CODE
            INNER JOIN TERRITORY_MAP T ON C.BZIRK = T.BZIRK
            LEFT JOIN REGION_MAP RM ON (T.REGIO = RM.REGION_CODE OR (T.P_ZONE = RM.ZONE_CODE AND T.REGIO = RM.REGION_NAME))
            GROUP BY COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region'),
                     COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')),
                     COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, ''))
        ),
        PLAN_BY_REGION AS
        (
            SELECT
                COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region') AS REGION_NAME,
                COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')) AS REG_CODE,
                COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, '')) AS P_ZONE,
                SUM(IFNULL(P."PLAN_VALUE", 0)) AS ORIGINAL_PLAN
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" P
            INNER JOIN TERRITORY_MAP T ON TRIM(P."TERR_CODE") = T.BZIRK
            LEFT JOIN REGION_MAP RM ON (T.REGIO = RM.REGION_CODE OR (T.P_ZONE = RM.ZONE_CODE AND T.REGIO = RM.REGION_NAME))
            WHERE P."YMONTH" LIKE '{plan_year_like}'
            GROUP BY COALESCE(NULLIF(RM.REGION_NAME, ''), NULLIF(T.REGIO, ''), 'Other Region'),
                     COALESCE(NULLIF(RM.REGION_CODE, ''), NULLIF(T.REGIO, '')),
                     COALESCE(NULLIF(T.P_ZONE, ''), NULLIF(RM.ZONE_CODE, ''))
        )
        SELECT
            COALESCE(A.REGION_NAME, P.REGION_NAME) AS "region",
            COALESCE(A.REGION_NAME, P.REGION_NAME) AS "city",
            COALESCE(A.REG_CODE, P.REG_CODE) AS "regionCode",
            COALESCE(A.P_ZONE, P.P_ZONE) AS "zoneCode",
            ROUND(IFNULL(A.ACTUAL_SALES, 0) / 10000000, 2) AS "saleValueCr",
            ROUND(IFNULL(P.ORIGINAL_PLAN, 0) / 10000000, 2) AS "planValueCr",
            ROUND(
                CASE WHEN IFNULL(P.ORIGINAL_PLAN, 0) = 0 THEN 0
                     ELSE IFNULL(A.ACTUAL_SALES, 0) / P.ORIGINAL_PLAN * 100
                END, 2
            ) AS "achievValuePct"
        FROM ACTUAL_BY_REGION A
        FULL OUTER JOIN PLAN_BY_REGION P 
            ON (UPPER(TRIM(A.REGION_NAME)) = UPPER(TRIM(P.REGION_NAME)) AND UPPER(TRIM(A.P_ZONE)) = UPPER(TRIM(P.P_ZONE)))
        {filter_sql}
        ORDER BY "saleValueCr" DESC
        """
        sanitize_and_execute_hana(cursor, schema, hana_sql)

    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


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

    # User authorization check
    assigned_zc, assigned_zn, assigned_r, assigned_t, is_restricted = \
        _get_user_hierarchy_scope(mysql_conn, user_id)

    # Determine effective drilldown level
    eff_level = str(level).strip().lower()
    if eff_level not in ("zone", "region", "territory"):
        eff_level = "zone"

    if is_restricted:
        # Restricted user (e.g. Mehebub): locked to assigned zone
        target_zone = assigned_zn or assigned_zc
        target_reg = assigned_r if assigned_r else (region if region and region.upper() not in ("ALL", "NONE", "NULL", "") else None)
        target_terr = assigned_t if assigned_t else (territory if territory and territory.upper() not in ("ALL", "NONE", "NULL", "") else None)
    else:
        # Unrestricted user (Admin/National Head): completely open
        target_zone = zone if zone and str(zone).strip().lower() not in ("all", "null", "none", "") else None
        target_reg = region if region and str(region).strip().upper() not in ("ALL", "NONE", "NULL", "") else None
        target_terr = territory if territory and str(territory).strip().upper() not in ("ALL", "NONE", "NULL", "") else None

    norm_zone_code = _normalize_zone_code(target_zone)
    if eff_level == "zone":
        norm_reg = "ALL"
        norm_terr = "ALL"
    elif eff_level == "region":
        norm_reg = str(target_reg).strip().upper() if target_reg else "ALL"
        norm_terr = "ALL"
    else:
        norm_reg = str(target_reg).strip().upper() if target_reg else "ALL"
        norm_terr = str(target_terr).strip().upper() if target_terr else "ALL"

    cache_ctx = f"sr_lvl_{eff_level}_z_{norm_zone_code or 'ALL'}_r_{norm_reg}_t_{norm_terr}"

    try:
        from helper.dashboard_cache_service import get_cached_component_data, set_cached_component_data

        # 1. Check cache first
        if not force_refresh:
            cached_res = get_cached_component_data(mysql_conn, effective_session, "sales_revenue", cache_ctx)
            if cached_res and cached_res.get("data"):
                return jsonify({
                    "status": "success",
                    "source": "dashboard_cached_metrics",
                    "level": eff_level,
                    "data": cached_res["data"],
                    "_cached": True,
                    "_last_synced_at": cached_res.get("_last_synced_at")
                }), 200

            # Hierarchy cache fallback: If a restricted user or specific zone is requested at zone-level,
            # check if the all-zones cache exists! If it does, extract the matching zone row so numbers are 100% identical!
            if eff_level == "zone" and norm_zone_code:
                all_zones_cache = get_cached_component_data(mysql_conn, effective_session, "sales_revenue", "sr_lvl_zone_z_ALL_r_ALL_t_ALL")
                if all_zones_cache and all_zones_cache.get("data"):
                    target_zn = (_normalize_zone_name(norm_zone_code) or "").upper()
                    filtered_zone_data = [
                        item for item in all_zones_cache["data"]
                        if str(item.get("zoneCode") or "").strip().upper() == norm_zone_code
                        or str(item.get("zone") or item.get("city") or "").strip().upper() in (norm_zone_code, target_zn)
                    ]
                    if filtered_zone_data:
                        return jsonify({
                            "status": "success",
                            "source": "dashboard_cached_metrics_derived",
                            "level": "zone",
                            "data": filtered_zone_data,
                            "_cached": True,
                            "_last_synced_at": all_zones_cache.get("_last_synced_at")
                        }), 200

            # Hierarchy cache fallback: If a restricted user requests a specific region at region-level,
            # check if the all-regions cache for this zone exists! If it does, extract the matching region row so numbers are 100% identical!
            if eff_level == "region" and norm_zone_code and norm_reg != "ALL":
                all_regions_cache = get_cached_component_data(mysql_conn, effective_session, "sales_revenue", f"sr_lvl_region_z_{norm_zone_code}_r_ALL_t_ALL")
                if all_regions_cache and all_regions_cache.get("data"):
                    filtered_reg_data = [
                        item for item in all_regions_cache["data"]
                        if str(item.get("region") or item.get("city") or "").strip().upper() == norm_reg
                        or str(item.get("regionCode") or "").strip().upper() == norm_reg
                    ]
                    if filtered_reg_data:
                        return jsonify({
                            "status": "success",
                            "source": "dashboard_cached_metrics_derived",
                            "level": "region",
                            "data": filtered_reg_data,
                            "_cached": True,
                            "_last_synced_at": all_regions_cache.get("_last_synced_at")
                        }), 200

            # Fallback for zone-level cache in sales_revenue_plan_vs_sale table
            if eff_level == "zone":
                cached = get_cached_sales_revenue(mysql_conn, effective_session, zone=target_zone)
                if cached and cached.get("data"):
                    return jsonify({
                        "status": "success",
                        "source": "sales_revenue_plan_vs_sale",
                        "level": "zone",
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
                level=eff_level,
                zone=target_zone,
                region=target_reg,
                territory=target_terr
            )
            cursor.close()
        else:
            results = []

        # 3. Save into cache
        if results:
            try:
                set_cached_component_data(mysql_conn, effective_session, "sales_revenue", results, cache_ctx)
                if eff_level == "zone" and not norm_zone_code:
                    save_sales_revenue_to_table(mysql_conn, effective_session, "ALL", results)
                    for item in results:
                        zc = str(item.get("zoneCode") or "").strip().upper()
                        if zc:
                            sub_ctx = f"sr_lvl_zone_z_{zc}_r_ALL_t_ALL"
                            set_cached_component_data(mysql_conn, effective_session, "sales_revenue", [item], sub_ctx)
                elif eff_level == "region" and norm_zone_code and norm_reg == "ALL":
                    for item in results:
                        rc = str(item.get("region") or item.get("city") or "").strip().upper()
                        if rc:
                            sub_ctx = f"sr_lvl_region_z_{norm_zone_code}_r_{rc}_t_ALL"
                            set_cached_component_data(mysql_conn, effective_session, "sales_revenue", [item], sub_ctx)
            except Exception as ce:
                print(f"[Sales Revenue] Cache set note: {ce}", flush=True)

            if eff_level == "zone" and not is_restricted and not target_zone:
                save_sales_revenue_to_table(mysql_conn, effective_session, target_zone or "ALL", results)

        return jsonify({
            "status": "success",
            "source": "live_query",
            "level": eff_level,
            "data": results or []
        }), 200

    except Exception as e:
        print(f"[Sales Revenue] Controller error: {e}", flush=True)
        # Graceful fallback: return empty list or cached table rather than crashing
        cached = get_cached_sales_revenue(mysql_conn, effective_session, zone=target_zone)
        if cached and cached.get("data"):
            return jsonify({
                "status": "success",
                "source": "sales_revenue_plan_vs_sale",
                "level": eff_level,
                "data": cached["data"],
                "_cached": True,
                "_last_synced_at": cached.get("_last_synced_at")
            }), 200

        return jsonify({"status": "error", "message": str(e), "data": []}), 200
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel)
