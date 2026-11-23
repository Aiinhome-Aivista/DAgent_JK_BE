"""
Dedicated Controller for Zone-Wise Sales Achievement.
Manages dedicated cache table: `sales_summary_zone_wise_sales_achievement`
Self-contained HANA query execution and MySQL cache read/write.
Filtered strictly for Replacement (distribution_channel = '10').
Metrics:
  - Zone (East, North, Central, West, South - I, South - II, Nepal, Total (Repl))
  - Previous Year Month Actual (₹ in Cr)
  - Previous Year MTD Actual (₹ in Cr)
  - Target (₹ in Cr)
  - Actual (₹ in Cr)
  - % Achvd
  - Sale for the Day (₹ in Cr)
  - Growth Over LYSMT (%)
"""

import calendar
from datetime import datetime
from flask import request, jsonify
from helper.sap_connection import get_sap_connection_for_session, sanitize_and_execute_hana, detect_sales_dates
from helper.period_filter_helper import parse_period_args

TABLE_NAME = "sales_summary_zone_wise_sales_achievement"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `year_month` VARCHAR(60) NOT NULL,
    `selected_date` DATE NOT NULL,
    `zone` VARCHAR(60) NOT NULL,
    `prev_year_actual_month` DECIMAL(18,2) DEFAULT 0,
    `prev_year_actual_mtd` DECIMAL(18,2) DEFAULT 0,
    `target` DECIMAL(18,2) DEFAULT 0,
    `actual` DECIMAL(18,2) DEFAULT 0,
    `achievement_pct` DECIMAL(10,2) DEFAULT 0,
    `sale_for_the_day` DECIMAL(18,2) DEFAULT 0,
    `growth_over_lysmt` DECIMAL(10,2) DEFAULT 0,
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
        try:
            cur.execute(f"ALTER TABLE `{TABLE_NAME}` MODIFY `year_month` VARCHAR(60) NOT NULL")
        except Exception:
            pass
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
        print(f"[{TABLE_NAME}] HANA connection error: {e}", flush=True)

    return mysql_conn, hana_conn, tunnel, is_hana, sap_schema, effective_session


def _close_connections(mysql_conn, hana_conn, tunnel):
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
            WHERE "Date_" IS NOT NULL
            ORDER BY MONTH_NO ASC
        """
        cursor.execute(sql)
        for r in cursor.fetchall():
            if r and r[0]:
                months.append({
                    "month_no": int(r[0]),
                    "month_name": str(r[1]).strip() if len(r) > 1 and r[1] else calendar.month_abbr[int(r[0])].upper()
                })
    except Exception as e:
        print(f"[{TABLE_NAME}] Error fetching master months from HANA: {e}", flush=True)

    if not months:
        for m in range(1, 13):
            months.append({
                "month_no": m,
                "month_name": calendar.month_abbr[m].upper()
            })

    return months


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


def query_zone_wise_sales_achievement_hana(cursor, schema, year_month: str, selected_date: str, period_info: dict = None):
    """
    Queries HANA for zone-wise sales achievement metrics matching Sheet 1 columns.
    """
    if period_info:
        curr_from = period_info["curr_from"]
        curr_to = period_info["curr_to"]
        curr_month_end = period_info["curr_to"]
        prev_from = period_info["prev_from"]
        prev_to = period_info["prev_to"]
        prev_month_end = period_info["prev_to"]
        day_date = period_info["day_date"]
        tgt_ym_list = ", ".join(f"'{ym}'" for ym in period_info.get("target_ymonths", [year_month]))
        target_where = f'(CAST(tgt."YMONTH" AS VARCHAR) IN ({tgt_ym_list}) OR tgt."YMONTH" IN ({tgt_ym_list}))'
    else:
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
        target_where = f'(CAST(tgt."YMONTH" AS VARCHAR) = \'{year_month}\' OR tgt."YMONTH" = \'{year_month}\')'

    hana_sql = f"""
    WITH
    SALES_BASE AS (
        SELECT
            "invoice_date",
            COALESCE("ndp_value", 0) AS "invoice_value",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
        WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
          AND (
              ("invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD'))
              OR
              ("invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD'))
          )
          AND "invoice_date" < '2026-10-18'
          AND COALESCE("indicator_cancel", '') <> 'X'

        UNION ALL

        SELECT
            "invoice_date",
            COALESCE("ndp_value", 0) AS "invoice_value",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
        WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
          AND (
              ("invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD'))
              OR
              ("invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD'))
          )
          AND "invoice_date" >= '2026-10-18'
          AND COALESCE("indicator_cancel", '') <> 'X'
    ),

    FACT_CLASSIFIED AS (
        SELECT
            zm."NAME" AS Zone_Name,
            zm."P_ZONE" AS Zone_Code,
            s."invoice_date",
            s."invoice_value"
        FROM SALES_BASE s
        INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
            ON LTRIM(TRIM(s."dealer_code"), '0') = LTRIM(TRIM(cm."KUNNR"), '0')
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm 
            ON TRIM(cm."BZIRK") = TRIM(tm."BZIRK")
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
            ON TRIM(tm."REGIO") = TRIM(rm."region_code")
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm 
            ON zm."P_ZONE" = COALESCE(NULLIF(TRIM(rm."zone_code"), ''), NULLIF(TRIM(tm."P_ZONE"), ''))
        WHERE COALESCE(cm."KLABC", '') <> 'X'
          AND zm."NAME" IS NOT NULL AND TRIM(zm."NAME") != '' AND UPPER(TRIM(zm."NAME")) NOT LIKE '%UNKNOWN%'
    ),

    AGG_SALES AS (
        SELECT
            Zone_Name,
            Zone_Code,
            SUM(CASE WHEN "invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Prev_Month,
            SUM(CASE WHEN "invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_to}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Prev_MTD,
            SUM(CASE WHEN "invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_to}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Curr_Actual,
            SUM(CASE WHEN "invoice_date" = TO_DATE('{day_date}', 'YYYY-MM-DD') THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Sale_For_Day
        FROM FACT_CLASSIFIED
        GROUP BY Zone_Name, Zone_Code
    ),

    AGG_TARGET AS (
        SELECT
            zm."NAME" AS Zone_Name,
            zm."P_ZONE" AS Zone_Code,
            SUM(COALESCE(NULLIF(tgt."REV_PLAN_VAL", 0), tgt."PLAN_VALUE", 0)) / 10000000.0 AS Target_Val
        FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" tgt
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm ON (TRIM(tgt."TERR_CODE") = TRIM(tm."BZIRK"))
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (TRIM(COALESCE(NULLIF(TRIM(tgt."REG_CODE"), ''), NULLIF(TRIM(tm."REGIO"), ''))) = TRIM(rm."region_code"))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm."P_ZONE" = COALESCE(NULLIF(TRIM(rm."zone_code"), ''), NULLIF(TRIM(tm."P_ZONE"), '')))
        WHERE {target_where}
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
        COALESCE(s.Prev_Month,   0) AS Prev_Month,
        COALESCE(s.Prev_MTD,     0) AS Prev_MTD,
        COALESCE(t.Target_Val,   0) AS Target_Val,
        COALESCE(s.Curr_Actual,  0) AS Curr_Actual,
        COALESCE(s.Sale_For_Day, 0) AS Sale_For_Day
    FROM ALL_ZONES z
    LEFT JOIN AGG_SALES s ON (z.Zone_Code = s.Zone_Code OR z.Zone_Name = s.Zone_Name)
    LEFT JOIN AGG_TARGET t ON (z.Zone_Code = t.Zone_Code OR z.Zone_Name = t.Zone_Name)
    ORDER BY z.Sort_Order ASC
    """

    sanitize_and_execute_hana(cursor, schema, hana_sql)
    raw_rows = cursor.fetchall()
    results = []

    tot_prev_month = 0.0
    tot_prev_mtd = 0.0
    tot_target = 0.0
    tot_actual = 0.0
    tot_sale_for_the_day = 0.0

    zone_order_seen = set()

    for r in raw_rows:
        z_name, s_order = _clean_zone(r[0], r[1])
        if z_name in zone_order_seen or "UNKNOWN" in z_name.upper():
            continue
        zone_order_seen.add(z_name)

        p_month = round(float(r[3] or 0), 2)
        p_mtd = round(float(r[4] or 0), 2)
        tgt = round(float(r[5] or 0), 2)
        act = round(float(r[6] or 0), 2)
        s_day = round(float(r[7] or 0), 2)

        achv_pct = round((act * 100.0 / tgt), 2) if tgt > 0 else 0.0
        grth_pct = round(((act - p_mtd) * 100.0 / p_mtd), 2) if p_mtd > 0 else 0.0

        tot_prev_month += p_month
        tot_prev_mtd += p_mtd
        tot_target += tgt
        tot_actual += act
        tot_sale_for_the_day += s_day

        results.append({
            "zone": z_name,
            "prev_year_actual_month": p_month,
            "prev_year_actual_mtd": p_mtd,
            "target": tgt,
            "actual": act,
            "achievement_pct": achv_pct,
            "sale_for_the_day": s_day,
            "growth_over_lysmt": grth_pct,
            "sort_order": s_order,
            "is_total": False
        })

    # Ensure all 7 standard zones are represented
    standard_zones = [
        ("East", 1), ("North", 2), ("Central", 3),
        ("West", 4), ("South - I", 5), ("South - II", 6), ("Nepal", 7)
    ]
    for sz_name, sz_order in standard_zones:
        if sz_name not in zone_order_seen:
            results.append({
                "zone": sz_name,
                "prev_year_actual_month": 0.0,
                "prev_year_actual_mtd": 0.0,
                "target": 0.0,
                "actual": 0.0,
                "achievement_pct": 0.0,
                "sale_for_the_day": 0.0,
                "growth_over_lysmt": 0.0,
                "sort_order": sz_order,
                "is_total": False
            })

    results.sort(key=lambda x: x["sort_order"])

    # Total Row (Reconciliation with Repl Total)
    tot_prev_month = round(tot_prev_month, 2)
    tot_prev_mtd = round(tot_prev_mtd, 2)
    tot_target = round(tot_target, 2)
    tot_actual = round(tot_actual, 2)
    tot_sale_for_the_day = round(tot_sale_for_the_day, 2)

    tot_achv_pct = round((tot_actual * 100.0 / tot_target), 2) if tot_target > 0 else 0.0
    tot_grth_pct = round(((tot_actual - tot_prev_mtd) * 100.0 / tot_prev_mtd), 2) if tot_prev_mtd > 0 else 0.0

    results.append({
        "zone": "Total (Repl)",
        "prev_year_actual_month": tot_prev_month,
        "prev_year_actual_mtd": tot_prev_mtd,
        "target": tot_target,
        "actual": tot_actual,
        "achievement_pct": tot_achv_pct,
        "sale_for_the_day": tot_sale_for_the_day,
        "growth_over_lysmt": tot_grth_pct,
        "sort_order": 99,
        "is_total": True
    })

    return results


def get_cached_data(mysql_conn, session_id: str, year_month: str):
    ensure_table(mysql_conn)
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute(f"""
            SELECT `zone`, `prev_year_actual_month`, `prev_year_actual_mtd`,
                   `target`, `actual`, `achievement_pct`, `sale_for_the_day`,
                   `growth_over_lysmt`, `sort_order`, `is_total`,
                   `selected_date`, `last_synced_at`
            FROM `{TABLE_NAME}`
            WHERE `session_id` = %s AND `year_month` = %s
            ORDER BY `sort_order` ASC, `id` ASC
        """, (str(session_id).strip(), str(year_month).strip()))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None

        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        sel_date = str(rows[0].get("selected_date")) if isinstance(rows[0], dict) and rows[0].get("selected_date") else None
        result = []
        for r in rows:
            result.append({
                "zone": r.get("zone"),
                "prev_year_actual_month": float(r.get("prev_year_actual_month") or 0),
                "prev_year_actual_mtd": float(r.get("prev_year_actual_mtd") or 0),
                "target": float(r.get("target") or 0),
                "actual": float(r.get("actual") or 0),
                "achievement_pct": float(r.get("achievement_pct") or 0),
                "sale_for_the_day": float(r.get("sale_for_the_day") or 0),
                "growth_over_lysmt": float(r.get("growth_over_lysmt") or 0),
                "sort_order": int(r.get("sort_order") or 99),
                "is_total": bool(r.get("is_total")) or "Total" in str(r.get("zone"))
            })
        return {
            "data": result,
            "selected_date": sel_date,
            "_last_synced_at": str(last_sync) if last_sync else None
        }
    except Exception as e:
        print(f"[{TABLE_NAME}] Cache fetch error: {e}", flush=True)
        return None


def save_data_to_table(mysql_conn, session_id: str, year_month: str, selected_date: str, rows: list):
    ensure_table(mysql_conn)
    try:
        cur = mysql_conn.cursor()
        cur.execute(f"DELETE FROM `{TABLE_NAME}` WHERE `session_id` = %s AND `year_month` = %s",
                    (str(session_id).strip(), str(year_month).strip()))

        ist_now = (datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
        sql = f"""
            INSERT INTO `{TABLE_NAME}`
            (`session_id`, `year_month`, `selected_date`, `zone`,
             `prev_year_actual_month`, `prev_year_actual_mtd`, `target`, `actual`,
             `achievement_pct`, `sale_for_the_day`, `growth_over_lysmt`,
             `sort_order`, `is_total`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        batch = []
        for r in rows:
            batch.append((
                str(session_id).strip(),
                str(year_month).strip(),
                str(selected_date).strip(),
                str(r.get("zone")),
                float(r.get("prev_year_actual_month") or 0),
                float(r.get("prev_year_actual_mtd") or 0),
                float(r.get("target") or 0),
                float(r.get("actual") or 0),
                float(r.get("achievement_pct") or 0),
                float(r.get("sale_for_the_day") or 0),
                float(r.get("growth_over_lysmt") or 0),
                int(r.get("sort_order") or 99),
                1 if r.get("is_total") else 0,
                ist_now,
                ist_now
            ))
        if batch:
            cur.executemany(sql, batch)
            mysql_conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[{TABLE_NAME}] Save error: {e}", flush=True)
        return False


def get_zone_wise_sales_achievement_controller(get_db_connection):
    """
    HTTP Controller for Zone-Wise Sales Achievement.
    Query params:
      session_id: Workspace session ID
      month: 1-12 (optional)
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
        # 1. Dynamically detect latest sales dates from HANA
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

        # 2. Master months dynamically
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

        # 3. Determine selected month number
        param_m = request.args.get("month") or (request.json.get("month") if request.is_json else None)
        param_ym = request.args.get("year_month") or (request.json.get("year_month") if request.is_json else None)
        selected_date = request.args.get("selected_date") or (request.json.get("selected_date") if request.is_json else None)
        force_refresh = (request.args.get("force_refresh", "").lower() in ("true", "1")) or \
                        (request.is_json and str(request.json.get("force_refresh", "")).lower() in ("true", "1"))

        combined_params = dict(request.args)
        if request.is_json and isinstance(request.json, dict):
            combined_params.update(request.json)

        period_info = parse_period_args(
            combined_params,
            detected_year=current_year,
            detected_month=detected_month,
            detected_day=detected_day,
            master_months=master_months
        )
        year_month = period_info["cache_key"]
        selected_date = period_info["day_date"]
        selected_mo_num = period_info["selected_months"][0] if period_info["selected_months"] else detected_month

        meta_info = period_info["meta"]
        meta_info["unit"] = "₹ in Cr"

        # 4. Check cache table if not force_refresh
        if not force_refresh:
            cached = get_cached_data(mysql_conn, effective_session, year_month)
            if cached and cached.get("data"):
                return jsonify({
                    "status": "success",
                    "source": TABLE_NAME,
                    "data": cached["data"],
                    "months": master_months,
                    "selected_month": selected_mo_num,
                    "current_year": current_year,
                    "previous_year": previous_year,
                    "year_month": year_month,
                    "selected_date": cached.get("selected_date") or selected_date,
                    "meta": meta_info,
                    "period_info": {
                        "mode": period_info["mode"],
                        "months": period_info["selected_months"],
                        "year": period_info["year"],
                        "start_date": period_info["start_date"],
                        "end_date": period_info["end_date"],
                        "label": period_info["meta"]["curr_period_label"]
                    },
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        # 5. Query from SAP HANA
        if is_hana:
            h_cur = hana_conn.cursor()
            rows = query_zone_wise_sales_achievement_hana(h_cur, schema, year_month, selected_date, period_info=period_info)
            h_cur.close()
        else:
            rows = [
                {"zone": "East", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 1, "is_total": False},
                {"zone": "North", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 2, "is_total": False},
                {"zone": "Central", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 3, "is_total": False},
                {"zone": "West", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 4, "is_total": False},
                {"zone": "South - I", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 5, "is_total": False},
                {"zone": "South - II", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 6, "is_total": False},
                {"zone": "Nepal", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 7, "is_total": False},
                {"zone": "Total (Repl)", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "sort_order": 99, "is_total": True}
            ]

        # 6. Save into cache table
        if rows:
            save_data_to_table(mysql_conn, effective_session, year_month, selected_date, rows)

        return jsonify({
            "status": "success",
            "source": TABLE_NAME,
            "data": rows,
            "months": master_months,
            "selected_month": selected_mo_num,
            "current_year": current_year,
            "previous_year": previous_year,
            "year_month": year_month,
            "selected_date": selected_date,
            "meta": meta_info,
            "period_info": {
                "mode": period_info["mode"],
                "months": period_info["selected_months"],
                "year": period_info["year"],
                "start_date": period_info["start_date"],
                "end_date": period_info["end_date"],
                "label": period_info["meta"]["curr_period_label"]
            },
            "_cached": False,
            "_last_synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[{TABLE_NAME}] Controller error: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel)


def bulk_sync_zone_wise_sales_achievement(mysql_conn, session_id: str, user_id=None):
    """
    Bulk pre-calculates and caches all 12 months permutations for Zone-Wise Sales Achievement.
    """
    ensure_table(mysql_conn)
    hana_conn = None
    tunnel = None
    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id=user_id)
        if not hana_conn:
            print(f"[{TABLE_NAME}] No HANA connection available for bulk sync", flush=True)
            return False

        h_cur = hana_conn.cursor()
        det_yr, det_mo, det_day = detect_sales_dates(h_cur, schema)
        if not det_yr:
            now = datetime.now()
            det_yr, det_mo, det_day = now.year, now.month, now.day
        current_year = int(det_yr)

        master_months = fetch_master_months(h_cur, schema)
        print(f"[{TABLE_NAME}] Bulk syncing {len(master_months)} months for session {session_id}...", flush=True)

        for m_item in master_months:
            m_no = m_item["month_no"]
            ym = f"{current_year}{str(m_no).zfill(2)}"
            if m_no == det_mo:
                sel_date = f"{current_year}-{str(m_no).zfill(2)}-{str(det_day).zfill(2)}"
            else:
                last_d = calendar.monthrange(current_year, m_no)[1]
                sel_date = f"{current_year}-{str(m_no).zfill(2)}-{str(last_d).zfill(2)}"

            try:
                rows = query_zone_wise_sales_achievement_hana(h_cur, schema, ym, sel_date)
                if rows:
                    save_data_to_table(mysql_conn, session_id, ym, sel_date, rows)
            except Exception as ex:
                print(f"[{TABLE_NAME}] Sync error for month {ym}: {ex}", flush=True)

        h_cur.close()
        return True
    except Exception as e:
        print(f"[{TABLE_NAME}] Bulk sync exception: {e}", flush=True)
        return False
    finally:
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass
