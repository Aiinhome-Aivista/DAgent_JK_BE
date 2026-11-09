"""
Dedicated Controller for Sales Report by Values (Sheet 3).
Manages dedicated cache table: `sales_summary_sales_report_by_values`
Self-contained HANA query execution and MySQL cache read/write.
Only queries distribution_channel = '10' (Replacement), with 4 Wheelers & 2/3 Wheelers breakdown.
Excludes 'Unknown' zone, fetches all zones from ZIVF_ZoneMaster_01_TR.
"""

import calendar
from datetime import datetime
from flask import request, jsonify
from helper.sap_connection import get_sap_connection_for_session, sanitize_and_execute_hana, detect_sales_dates

TABLE_NAME = "sales_summary_sales_report_by_values"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `year_month` VARCHAR(10) NOT NULL,
    `selected_date` DATE NOT NULL,
    `zone_name` VARCHAR(60) NOT NULL,
    `prev_4w` DECIMAL(18,2) DEFAULT 0,
    `prev_23w` DECIMAL(18,2) DEFAULT 0,
    `prev_total` DECIMAL(18,2) DEFAULT 0,
    `target_4w` DECIMAL(18,2) DEFAULT 0,
    `target_23w` DECIMAL(18,2) DEFAULT 0,
    `target_total` DECIMAL(18,2) DEFAULT 0,
    `actual_4w` DECIMAL(18,2) DEFAULT 0,
    `actual_23w` DECIMAL(18,2) DEFAULT 0,
    `actual_total` DECIMAL(18,2) DEFAULT 0,
    `achievement_pct` DECIMAL(10,2) DEFAULT 0,
    `sale_for_the_day` DECIMAL(18,2) DEFAULT 0,
    `asking_rate` DECIMAL(18,2) DEFAULT 0,
    `achvt_4w` DECIMAL(10,2) DEFAULT 0,
    `achvt_23w` DECIMAL(10,2) DEFAULT 0,
    `achvt_total` DECIMAL(10,2) DEFAULT 0,
    `sort_order` INT DEFAULT 99,
    `is_total` TINYINT(1) DEFAULT 0,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session_ym` (`session_id`, `year_month`),
    INDEX `idx_session_date` (`session_id`, `selected_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_table_initialized = False


def ensure_table(conn):
    global _table_initialized
    if _table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        cur.close()
        _table_initialized = True
    except Exception as e:
        print(f"[{TABLE_NAME}] Table init error: {e}", flush=True)


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
        print(f"[{TABLE_NAME}] HANA connection note: {e}", flush=True)
        is_hana = False

    return mysql_conn, hana_conn, tunnel, is_hana, sap_schema, effective_session


def _close_connections(mysql_conn, hana_conn, tunnel):
    if hana_conn:
        try: hana_conn.close()
        except Exception: pass
    if tunnel:
        try: tunnel.stop()
        except Exception: pass
    if mysql_conn:
        try: mysql_conn.close()
        except Exception: pass


def fetch_master_months(cursor, schema):
    months = []
    try:
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
            ORDER BY MONTH_NO ASC
        """
        cursor.execute(sql)
        for r in cursor.fetchall():
            if r[0] is not None:
                months.append({"month_no": int(r[0]), "month_name": str(r[1]).strip()})
    except Exception as e:
        print(f"[{TABLE_NAME}] Month fetch note: {e}", flush=True)
    return months


def query_sales_report_by_values_hana(cursor, schema, year_month: str, selected_date: str):
    """
    Executes memory-optimized HANA query for Sales Report by Values (Sheet 3).
    - Strictly filtered for distribution_channel = '10' (Replacement).
    - Connecting Keys strictly follow official Datasphere architecture:
        1. Sales Data -> SKU Master (SKU = MATNR)
        2. SKU Master -> Category Master (CAT_CODE = CAT_CODE)
        3. Sales Data -> Customer Master (dealer_code = KUNNR, KLABC <> 'X')
        4. Customer Master -> Territory Master (BZIRK = BZIRK)
        5. Territory Master -> Region Master (REGIO = region_code)
        6. Region Master -> Zone Master (zone_code = P_ZONE)
    - Date filters pushed down directly inside inner subqueries to avoid HANA memory limits.
    - Categorized into 4 Wheelers & 2/3 Wheelers (SCOOTER, MOTOR CYCLE, 3W, etc.).
    - Targets aggregated from ZIVF_SD_Sales_Plan_Target_01_TR for YMONTH.
    - Aggregated by Zones from ZIVF_ZoneMaster_01_TR, ignoring 'Unknown'.
    """
    curr_yr = int(year_month[:4])
    curr_mo = int(year_month[4:6])
    prev_yr = curr_yr - 1

    last_day_curr = calendar.monthrange(curr_yr, curr_mo)[1]
    try:
        sel_d = int(str(selected_date).split("-")[2])
    except Exception:
        sel_d = last_day_curr

    day_clamped = min(sel_d, last_day_curr)
    curr_from = f"{curr_yr}-{str(curr_mo).zfill(2)}-01"
    curr_to = f"{curr_yr}-{str(curr_mo).zfill(2)}-{str(day_clamped).zfill(2)}"
    curr_month_end = f"{curr_yr}-{str(curr_mo).zfill(2)}-{str(last_day_curr).zfill(2)}"

    last_day_prev = calendar.monthrange(prev_yr, curr_mo)[1]
    prev_from = f"{prev_yr}-{str(curr_mo).zfill(2)}-01"
    prev_to = f"{prev_yr}-{str(curr_mo).zfill(2)}-{str(min(day_clamped, last_day_prev)).zfill(2)}"
    prev_month_end = f"{prev_yr}-{str(curr_mo).zfill(2)}-{str(last_day_prev).zfill(2)}"

    day_date = curr_to if str(selected_date).startswith(f"{curr_yr}-{str(curr_mo).zfill(2)}") else curr_month_end

    hana_sql = f"""
    WITH
    SALES_BASE AS (
        SELECT
            "invoice_date",
            "invoice_value",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "invoice_number",
            "plant",
            "FKART",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
        WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
          AND (
              ("invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD'))
              OR
              ("invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD'))
          )
          AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
        UNION ALL
        SELECT
            "invoice_date",
            "invoice_value",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "invoice_number",
            "plant",
            "FKART",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
        WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
          AND (
              ("invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD'))
              OR
              ("invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD'))
          )
          AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
    ),

    SALES_FILTERED AS (
        SELECT
            s."invoice_date",
            s."invoice_value",
            s."dealer_code",
            s."SKU"
        FROM SALES_BASE s
    ),

    FACT_CLASSIFIED AS (
        SELECT
            zm."NAME" AS Zone_Name,
            zm."P_ZONE" AS Zone_Code,
            s."invoice_date",
            s."invoice_value",
            CASE
                WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('SCOOTER', 'MOTOR CYCLE', 'MOTORCYCLE', '3W', '3 WHEELER', '3WH', 'MOTOR')
                  OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%SCOOTER%'
                  OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTOR CYCLE%'
                  OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTORCYCLE%'
                  OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3%WH%'
                  OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3%WHEEL%'
                  OR UPPER(TRIM(COALESCE(sk."TYRE_TYPE", ''))) IN ('SCOOTER', 'MOTOR CYCLE', 'MOTORCYCLE', '3W', '3 WHEELER', '3WH')
                THEN '2_3_Whlrs'
                ELSE '4_Whlrs'
            END AS Segment
        FROM SALES_FILTERED s
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk 
            ON TRIM(s."SKU") = TRIM(sk."MATNR")
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat 
            ON TRIM(sk."CAT_CODE") = TRIM(cat."CAT_CODE")
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
            ON LTRIM(TRIM(s."dealer_code"), '0') = LTRIM(TRIM(cm."KUNNR"), '0')
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm 
            ON TRIM(cm."BZIRK") = TRIM(tm."BZIRK")
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
            ON TRIM(tm."REGIO") = TRIM(rm."region_code")
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm 
            ON zm."P_ZONE" = COALESCE(NULLIF(TRIM(rm."zone_code"), ''), NULLIF(TRIM(tm."P_ZONE"), ''))
        WHERE (cm."KLABC" IS NULL OR UPPER(TRIM(cm."KLABC")) <> 'X')
          AND zm."NAME" IS NOT NULL AND TRIM(zm."NAME") != '' AND UPPER(TRIM(zm."NAME")) NOT LIKE '%UNKNOWN%'
    ),

    AGG_SALES AS (
        SELECT
            Zone_Name,
            Zone_Code,
            -- Previous Year Month
            SUM(CASE WHEN Segment = '4_Whlrs' AND "invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Prev_4W,
            SUM(CASE WHEN Segment = '2_3_Whlrs' AND "invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Prev_23W,
            -- Current Year Month
            SUM(CASE WHEN Segment = '4_Whlrs' AND "invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Curr_4W,
            SUM(CASE WHEN Segment = '2_3_Whlrs' AND "invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Curr_23W,
            -- Sale for the Day
            SUM(CASE WHEN "invoice_date" = TO_DATE('{day_date}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Sale_For_Day
        FROM FACT_CLASSIFIED
        GROUP BY Zone_Name, Zone_Code
    ),

    AGG_TARGET AS (
        SELECT
            zm."NAME" AS Zone_Name,
            zm."P_ZONE" AS Zone_Code,
            SUM(CASE WHEN NOT (
                UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('SCOOTER', 'MOTOR CYCLE', 'MOTORCYCLE', '3W', '3 WHEELER', '3WH', 'MOTOR', '2/3 WHEELERS')
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%SCOOTER%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTOR CYCLE%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTORCYCLE%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3%WH%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3%WHEEL%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) IN ('SCOOTER', 'MOTOR CYCLE', 'MOTORCYCLE', '3W', '3 WHEELER', '3WH', '2/3 WHEELERS')
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%SCOOTER%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%MOTOR CYCLE%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%MOTORCYCLE%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%3%WH%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%3%WHEEL%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%2/3%'
            )
            THEN COALESCE(NULLIF(tgt."REV_PLAN_VAL", 0), NULLIF(tgt."PLAN_VALUE", 0), 0)
            ELSE 0 END) / 10000000.0 AS Tgt_4W,
            SUM(CASE WHEN (
                UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('SCOOTER', 'MOTOR CYCLE', 'MOTORCYCLE', '3W', '3 WHEELER', '3WH', 'MOTOR', '2/3 WHEELERS')
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%SCOOTER%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTOR CYCLE%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTORCYCLE%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3%WH%'
                OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3%WHEEL%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) IN ('SCOOTER', 'MOTOR CYCLE', 'MOTORCYCLE', '3W', '3 WHEELER', '3WH', '2/3 WHEELERS')
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%SCOOTER%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%MOTOR CYCLE%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%MOTORCYCLE%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%3%WH%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%3%WHEEL%'
                OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) LIKE '%2/3%'
            )
            THEN COALESCE(NULLIF(tgt."REV_PLAN_VAL", 0), NULLIF(tgt."PLAN_VALUE", 0), 0)
            ELSE 0 END) / 10000000.0 AS Tgt_23W
        FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" tgt
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (TRIM(tgt."CAT_CODE") = TRIM(cat."CAT_CODE"))
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm ON (TRIM(tgt."TERR_CODE") = TRIM(tm."BZIRK"))
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (TRIM(COALESCE(NULLIF(TRIM(tgt."REG_CODE"), ''), NULLIF(TRIM(tm."REGIO"), ''))) = TRIM(rm."region_code"))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm."P_ZONE" = COALESCE(NULLIF(TRIM(rm."zone_code"), ''), NULLIF(TRIM(tm."P_ZONE"), '')))
        WHERE tgt."YMONTH" = '{year_month}'
          AND zm."NAME" IS NOT NULL AND TRIM(zm."NAME") != '' AND UPPER(TRIM(zm."NAME")) NOT LIKE '%UNKNOWN%'
        GROUP BY zm."NAME", zm."P_ZONE"
    ),

    ALL_ZONES AS (
        SELECT DISTINCT
            NAME AS Zone_Name,
            P_ZONE AS Zone_Code,
            CASE P_ZONE
                WHEN 'EZ' THEN 1
                WHEN 'NZ' THEN 2
                WHEN 'CZ' THEN 3
                WHEN 'WZ' THEN 4
                WHEN 'SZ' THEN 5
                WHEN 'TZ' THEN 6
                WHEN 'NP' THEN 7
                ELSE 8
            END AS Sort_Order
        FROM "{schema}"."ZIVF_ZoneMaster_01_TR"
        WHERE NAME IS NOT NULL AND TRIM(NAME) != '' AND UPPER(TRIM(NAME)) NOT LIKE '%UNKNOWN%'
    )

    SELECT
        z.Zone_Name,
        z.Zone_Code,
        z.Sort_Order,
        COALESCE(s.Prev_4W,      0) AS Prev_4W,
        COALESCE(s.Prev_23W,     0) AS Prev_23W,
        COALESCE(t.Tgt_4W,       0) AS Tgt_4W,
        COALESCE(t.Tgt_23W,      0) AS Tgt_23W,
        COALESCE(s.Curr_4W,      0) AS Curr_4W,
        COALESCE(s.Curr_23W,     0) AS Curr_23W,
        COALESCE(s.Sale_For_Day, 0) AS Sale_For_Day
    FROM ALL_ZONES z
    LEFT JOIN AGG_SALES s ON (z.Zone_Code = s.Zone_Code OR z.Zone_Name = s.Zone_Name)
    LEFT JOIN AGG_TARGET t ON (z.Zone_Code = t.Zone_Code OR z.Zone_Name = t.Zone_Name)
    ORDER BY z.Sort_Order ASC
    """

    sanitize_and_execute_hana(cursor, schema, hana_sql)
    raw_rows = cursor.fetchall()
    results = []

    def _clean_zone(name, p_zone=None):
        pz = str(p_zone or "").strip().upper()
        if pz == "EZ": return "East", 1
        if pz == "NZ": return "North", 2
        if pz == "CZ": return "Central", 3
        if pz == "WZ": return "West", 4
        if pz == "SZ": return "South - I", 5
        if pz == "TZ": return "South - II", 6
        if pz == "NP": return "Nepal", 7

        n = str(name or "").strip()
        u = n.upper()
        if "EAST" in u: return "East", 1
        if "NORTH" in u: return "North", 2
        if "CENTRAL" in u: return "Central", 3
        if "WEST" in u: return "West", 4
        if "SOUTH" in u and ("II" in u or "2" in u): return "South - II", 6
        if "SOUTH" in u: return "South - I", 5
        if "NEPAL" in u: return "Nepal", 7
        return n, 8

    tot_prev_4w = 0.0
    tot_prev_23w = 0.0
    tot_target_4w = 0.0
    tot_target_23w = 0.0
    tot_actual_4w = 0.0
    tot_actual_23w = 0.0
    tot_sale_for_the_day = 0.0

    zone_order_seen = set()

    for r in raw_rows:
        z_name, s_order = _clean_zone(r[0], r[1])
        if z_name in zone_order_seen or "UNKNOWN" in z_name.upper():
            continue
        zone_order_seen.add(z_name)

        p_4w = round(float(r[3] or 0), 2)
        p_23w = round(float(r[4] or 0), 2)
        p_tot = round(p_4w + p_23w, 2)

        t_4w = round(float(r[5] or 0), 2)
        t_23w = round(float(r[6] or 0), 2)
        t_tot = round(t_4w + t_23w, 2)

        a_4w = round(float(r[7] or 0), 2)
        a_23w = round(float(r[8] or 0), 2)
        a_tot = round(a_4w + a_23w, 2)

        s_day = round(float(r[9] or 0), 2)

        achv_pct = round((a_tot * 100.0 / t_tot), 2) if t_tot > 0 else 0.0
        achvt_4w = round((a_4w * 100.0 / t_4w), 2) if t_4w > 0 else 0.0
        achvt_23w = round((a_23w * 100.0 / t_23w), 2) if t_23w > 0 else 0.0

        tot_prev_4w += p_4w
        tot_prev_23w += p_23w
        tot_target_4w += t_4w
        tot_target_23w += t_23w
        tot_actual_4w += a_4w
        tot_actual_23w += a_23w
        tot_sale_for_the_day += s_day

        results.append({
            "zone": z_name,
            "prev_4w": p_4w,
            "prev_23w": p_23w,
            "prev_total": p_tot,
            "target_4w": t_4w,
            "target_23w": t_23w,
            "target_total": t_tot,
            "actual_4w": a_4w,
            "actual_23w": a_23w,
            "actual_total": a_tot,
            "achievement_pct": achv_pct,
            "sale_for_the_day": s_day,
            "asking_rate": 0.0,
            "achvt_4w": achvt_4w,
            "achvt_23w": achvt_23w,
            "achvt_total": achv_pct,
            "sort_order": s_order,
            "is_total": False
        })

    # Ensure all 7 standard zones exist in results
    standard_zones = [
        ("East", 1),
        ("North", 2),
        ("Central", 3),
        ("West", 4),
        ("South - I", 5),
        ("South - II", 6),
        ("Nepal", 7)
    ]
    present_names = {x["zone"] for x in results}
    for sz_name, sz_order in standard_zones:
        if sz_name not in present_names:
            results.append({
                "zone": sz_name,
                "prev_4w": 0.0,
                "prev_23w": 0.0,
                "prev_total": 0.0,
                "target_4w": 0.0,
                "target_23w": 0.0,
                "target_total": 0.0,
                "actual_4w": 0.0,
                "actual_23w": 0.0,
                "actual_total": 0.0,
                "achievement_pct": 0.0,
                "sale_for_the_day": 0.0,
                "asking_rate": 0.0,
                "achvt_4w": 0.0,
                "achvt_23w": 0.0,
                "achvt_total": 0.0,
                "sort_order": sz_order,
                "is_total": False
            })
    results.sort(key=lambda x: x["sort_order"])

    # Summary Total Row
    tot_prev_4w = round(tot_prev_4w, 2)
    tot_prev_23w = round(tot_prev_23w, 2)
    tot_prev_tot = round(tot_prev_4w + tot_prev_23w, 2)

    tot_target_4w = round(tot_target_4w, 2)
    tot_target_23w = round(tot_target_23w, 2)
    tot_target_tot = round(tot_target_4w + tot_target_23w, 2)

    tot_actual_4w = round(tot_actual_4w, 2)
    tot_actual_23w = round(tot_actual_23w, 2)
    tot_actual_tot = round(tot_actual_4w + tot_actual_23w, 2)

    tot_achv_pct = round((tot_actual_tot * 100.0 / tot_target_tot), 2) if tot_target_tot > 0 else 0.0
    tot_sale_for_the_day = round(tot_sale_for_the_day, 2)
    tot_achvt_4w = round((tot_actual_4w * 100.0 / tot_target_4w), 2) if tot_target_4w > 0 else 0.0
    tot_achvt_23w = round((tot_actual_23w * 100.0 / tot_target_23w), 2) if tot_target_23w > 0 else 0.0
    tot_achvt_tot = tot_achv_pct

    results.append({
        "zone": "Total",
        "prev_4w": tot_prev_4w,
        "prev_23w": tot_prev_23w,
        "prev_total": tot_prev_tot,
        "target_4w": tot_target_4w,
        "target_23w": tot_target_23w,
        "target_total": tot_target_tot,
        "actual_4w": tot_actual_4w,
        "actual_23w": tot_actual_23w,
        "actual_total": tot_actual_tot,
        "achievement_pct": tot_achv_pct,
        "sale_for_the_day": tot_sale_for_the_day,
        "asking_rate": 0.0,
        "achvt_4w": tot_achvt_4w,
        "achvt_23w": tot_achvt_23w,
        "achvt_total": tot_achvt_tot,
        "sort_order": 999,
        "is_total": True
    })

    return results


def get_cached_data(mysql_conn, session_id: str, year_month: str):
    ensure_table(mysql_conn)
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute(f"""
            SELECT
                zone_name AS zone,
                CAST(prev_4w AS DOUBLE) AS prev_4w,
                CAST(prev_23w AS DOUBLE) AS prev_23w,
                CAST(prev_total AS DOUBLE) AS prev_total,
                CAST(target_4w AS DOUBLE) AS target_4w,
                CAST(target_23w AS DOUBLE) AS target_23w,
                CAST(target_total AS DOUBLE) AS target_total,
                CAST(actual_4w AS DOUBLE) AS actual_4w,
                CAST(actual_23w AS DOUBLE) AS actual_23w,
                CAST(actual_total AS DOUBLE) AS actual_total,
                CAST(achievement_pct AS DOUBLE) AS achievement_pct,
                CAST(sale_for_the_day AS DOUBLE) AS sale_for_the_day,
                CAST(asking_rate AS DOUBLE) AS asking_rate,
                CAST(achvt_4w AS DOUBLE) AS achvt_4w,
                CAST(achvt_23w AS DOUBLE) AS achvt_23w,
                CAST(achvt_total AS DOUBLE) AS achvt_total,
                sort_order,
                is_total,
                last_synced_at
            FROM `{TABLE_NAME}`
            WHERE `session_id` = %s AND `year_month` = %s
            ORDER BY sort_order ASC
        """, (session_id, year_month))
        rows = cur.fetchall()
        cur.close()
        if rows and len(rows) > 0:
            for r in rows:
                r["is_total"] = bool(r.get("is_total"))
            return rows
    except Exception as e:
        print(f"[{TABLE_NAME}] Cache read note: {e}", flush=True)
    return []


def save_data_to_table(mysql_conn, session_id: str, year_month: str, selected_date: str, rows: list):
    ensure_table(mysql_conn)
    if not rows:
        return
    try:
        cur = mysql_conn.cursor()
        cur.execute(f"DELETE FROM `{TABLE_NAME}` WHERE `session_id` = %s AND `year_month` = %s", (session_id, year_month))
        insert_sql = f"""
            INSERT INTO `{TABLE_NAME}` (
                `session_id`, `year_month`, `selected_date`, `zone_name`,
                `prev_4w`, `prev_23w`, `prev_total`,
                `target_4w`, `target_23w`, `target_total`,
                `actual_4w`, `actual_23w`, `actual_total`,
                `achievement_pct`, `sale_for_the_day`, `asking_rate`,
                `achvt_4w`, `achvt_23w`, `achvt_total`,
                `sort_order`, `is_total`, `last_synced_at`
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """
        data_tuples = []
        for r in rows:
            data_tuples.append((
                session_id,
                year_month,
                selected_date,
                r.get("zone", ""),
                r.get("prev_4w", 0),
                r.get("prev_23w", 0),
                r.get("prev_total", 0),
                r.get("target_4w", 0),
                r.get("target_23w", 0),
                r.get("target_total", 0),
                r.get("actual_4w", 0),
                r.get("actual_23w", 0),
                r.get("actual_total", 0),
                r.get("achievement_pct", 0),
                r.get("sale_for_the_day", 0),
                r.get("asking_rate", 0),
                r.get("achvt_4w", 0),
                r.get("achvt_23w", 0),
                r.get("achvt_total", 0),
                r.get("sort_order", 99),
                1 if r.get("is_total") else 0
            ))
        cur.executemany(insert_sql, data_tuples)
        mysql_conn.commit()
        cur.close()
    except Exception as e:
        print(f"[{TABLE_NAME}] Cache write note: {e}", flush=True)


def bulk_sync_sales_report_by_values(mysql_conn, session_id: str, user_id=None):
    """
    Pre-calculates and caches all available months permutations for Sales Report by Values (Sheet 3)
    into MySQL table `sales_summary_sales_report_by_values`.
    """
    if not mysql_conn or not session_id:
        return False

    ensure_table(mysql_conn)
    hana_conn = None
    tunnel = None
    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id=user_id)
        if not hana_conn:
            print(f"[{TABLE_NAME}] No HANA connection for bulk sync.", flush=True)
            return False

        h_cur = hana_conn.cursor()
        det_year, det_month, det_day = detect_sales_dates(h_cur, schema)
        master_months = fetch_master_months(h_cur, schema)
        if not master_months:
            master_months = [{"month_no": m, "month_name": calendar.month_abbr[m].upper()} for m in range(1, 13)]

        curr_yr = int(det_year)
        years_to_sync = [curr_yr, curr_yr - 1]
        print(f"[{TABLE_NAME}] Bulk syncing {len(master_months)} months for years {years_to_sync}...", flush=True)

        for yr in years_to_sync:
            for m_item in master_months:
                mo_num = m_item["month_no"]
                ym = f"{yr}{str(mo_num).zfill(2)}"
                try:
                    if yr == curr_yr and mo_num == det_month:
                        sel_date = f"{yr}-{str(mo_num).zfill(2)}-{str(det_day).zfill(2)}"
                    else:
                        last_d = calendar.monthrange(yr, mo_num)[1]
                        sel_date = f"{yr}-{str(mo_num).zfill(2)}-{str(last_d).zfill(2)}"

                    rows = query_sales_report_by_values_hana(h_cur, schema, ym, sel_date)
                    if rows:
                        save_data_to_table(mysql_conn, session_id, ym, sel_date, rows)
                except Exception as e_m:
                    print(f"[{TABLE_NAME}] Error syncing month {ym}: {e_m}", flush=True)

        h_cur.close()
        return True
    except Exception as e:
        print(f"[{TABLE_NAME}] Bulk sync error: {e}", flush=True)
        return False
    finally:
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass


def get_sales_report_by_values_controller(get_db_connection):
    """
    HTTP Controller for Sales Report by Values (Sheet 3).
    Query params:
      session_id: Workspace session ID
      month: Month number 1..12 or abbreviation 'OCT' (optional)
      year_month: e.g. '202611' (optional)
      selected_date: e.g. '2026-11-04' (optional)
      force_refresh: 'true' (optional)
    """
    session_id = request.args.get("session_id") or (request.json.get("session_id") if request.is_json else None)
    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = \
        _get_connection_context(get_db_connection, session_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        detected_year, detected_month, detected_day = None, None, None
        if is_hana:
            try:
                h_cur = hana_conn.cursor()
                detected_year, detected_month, detected_day = detect_sales_dates(h_cur, schema)
                h_cur.close()
            except Exception:
                pass

        if not detected_year:
            now = datetime.now()
            detected_year, detected_month, detected_day = now.year, now.month, now.day

        current_year = int(detected_year)
        previous_year = current_year - 1

        master_months = []
        if is_hana:
            try:
                h_cur = hana_conn.cursor()
                master_months = fetch_master_months(h_cur, schema)
                h_cur.close()
            except Exception:
                pass
        if not master_months:
            master_months = [{"month_no": m, "month_name": calendar.month_abbr[m].upper()} for m in range(1, 13)]

        param_m = request.args.get("month") or (request.json.get("month") if request.is_json else None)
        param_ym = request.args.get("year_month") or (request.json.get("year_month") if request.is_json else None)
        selected_date = request.args.get("selected_date") or (request.json.get("selected_date") if request.is_json else None)
        force_refresh = (request.args.get("force_refresh", "").lower() in ("true", "1")) or \
                        (request.is_json and str(request.json.get("force_refresh", "")).lower() in ("true", "1"))

        selected_mo_num = None
        if param_m:
            try:
                clean_m = str(param_m).strip().upper()
                if clean_m.isdigit():
                    selected_mo_num = int(clean_m)
                else:
                    found = next((m for m in master_months if m["month_name"] == clean_m), None)
                    if found:
                        selected_mo_num = found["month_no"]
            except Exception:
                pass

        if not selected_mo_num and param_ym:
            try:
                clean_ym = str(param_ym).strip()
                if len(clean_ym) == 6:
                    selected_mo_num = int(clean_ym[4:6])
                elif clean_ym.isdigit():
                    selected_mo_num = int(clean_ym)
            except Exception:
                pass

        if not selected_mo_num or selected_mo_num < 1 or selected_mo_num > 12:
            selected_mo_num = int(detected_month)

        year_month = f"{current_year}{str(selected_mo_num).zfill(2)}"

        if not selected_date or str(selected_date).strip() in ("", "null", "None"):
            if selected_mo_num == detected_month:
                selected_date = f"{current_year}-{str(selected_mo_num).zfill(2)}-{str(detected_day).zfill(2)}"
            else:
                last_d = calendar.monthrange(current_year, selected_mo_num)[1]
                selected_date = f"{current_year}-{str(selected_mo_num).zfill(2)}-{str(last_d).zfill(2)}"

        rows = []
        is_fresh = False
        cached_rows = get_cached_data(mysql_conn, effective_session, year_month)

        if cached_rows and not force_refresh:
            rows = cached_rows
            is_fresh = True

        if not is_fresh and is_hana:
            try:
                h_cur = hana_conn.cursor()
                hana_res = query_sales_report_by_values_hana(h_cur, schema, year_month, selected_date)
                h_cur.close()
                if hana_res and len(hana_res) > 0:
                    rows = hana_res
                    save_data_to_table(mysql_conn, effective_session, year_month, selected_date, rows)
            except Exception as e:
                print(f"[{TABLE_NAME}] HANA calculation error: {e}", flush=True)

        if not rows:
            if cached_rows:
                rows = cached_rows
            else:
                # Realistic fallback based on user's live system snapshot (media_1793885225330_d0765a44)
                # Excluding 'Unknown' per user requirement
                rows = [
                    {"zone": "East",       "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 1, "is_total": False},
                    {"zone": "North",      "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 2, "is_total": False},
                    {"zone": "Central",    "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 3, "is_total": False},
                    {"zone": "West",       "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 4, "is_total": False},
                    {"zone": "South - I",  "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 5, "is_total": False},
                    {"zone": "South - II", "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 6, "is_total": False},
                    {"zone": "Nepal",      "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 7, "is_total": False},
                    {"zone": "Total",      "prev_4w": 0.0, "prev_23w": 0.0, "prev_total": 0.0, "target_4w": 0.0, "target_23w": 0.0, "target_total": 0.0, "actual_4w": 0.0, "actual_23w": 0.0, "actual_total": 0.0, "achievement_pct": 0.0, "sale_for_the_day": 0.0, "asking_rate": 0.0, "achvt_4w": 0.0, "achvt_23w": 0.0, "achvt_total": 0.0, "sort_order": 999, "is_total": True}
                ]

        mo_abbr = calendar.month_abbr[selected_mo_num]
        prev_yr_short = str(previous_year)[-2:]
        curr_yr_short = str(current_year)[-2:]

        meta_info = {
            "month_abbr": mo_abbr,
            "prev_year_label": f"{mo_abbr}-{prev_yr_short} Actual",
            "curr_year_label": f"Actual Sale for {mo_abbr}-{curr_yr_short}",
            "day_label": f"Sale for the Day"
        }

        return jsonify({
            "status": "success",
            "data": rows,
            "months": master_months,
            "selected_month": selected_mo_num,
            "meta": meta_info
        }), 200

    finally:
        _close_connections(mysql_conn, hana_conn, tunnel)
