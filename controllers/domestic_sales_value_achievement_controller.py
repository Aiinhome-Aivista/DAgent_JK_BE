"""
Dedicated Controller for Domestic Sales Value Achievement (Sheet 1).
Manages dedicated cache table: `sales_summary_domestic_sales_value_achievement`
Self-contained HANA query execution and MySQL cache read/write.
"""

import calendar
from datetime import datetime, timezone, timedelta
from flask import request, jsonify
from helper.sap_connection import get_sap_connection_for_session, sanitize_and_execute_hana, detect_sales_dates

TABLE_NAME = "sales_summary_domestic_sales_value_achievement"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `year_month` VARCHAR(10) NOT NULL,
    `selected_date` DATE NOT NULL,
    `market_segment` VARCHAR(60) NOT NULL,
    `prev_year_actual_month` DECIMAL(18,2) DEFAULT 0,
    `prev_year_actual_mtd` DECIMAL(18,2) DEFAULT 0,
    `target` DECIMAL(18,2) DEFAULT 0,
    `actual` DECIMAL(18,2) DEFAULT 0,
    `achievement_pct` DECIMAL(10,2) DEFAULT 0,
    `sale_for_the_day` DECIMAL(18,2) DEFAULT 0,
    `growth_over_lysmt` DECIMAL(10,2) DEFAULT 0,
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
            cur.execute(f"ALTER TABLE `{TABLE_NAME}` MODIFY COLUMN `year_month` VARCHAR(60) NOT NULL")
            conn.commit()
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
    """
    Fetches the 12 calendar months dynamically from HANA day attributes view.
    Exact query from ZITR_DayAttribute_01_TR (MONTH_NO 1..12 and MONTH_NAME JAN..DEC).
    """
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


def query_domestic_sales_value_achievement_hana(cursor, schema, year_month: str, selected_date: str, period_info: dict = None):
    """
    Executes user's exact HANA SQL query for Domestic Sales Value Achievement.
    Parameters:
      year_month: e.g. '202611' or cache_key
      selected_date: e.g. '2026-11-04'
      period_info: Optional parsed period dict with curr_from, curr_to, prev_from, prev_to, day_date, target_ymonths
    """
    if period_info:
        tgt_ym_list = ", ".join(f"'{ym}'" for ym in period_info.get("target_ymonths", [year_month]))
        target_where = f'(CAST(T."YMONTH" AS VARCHAR) IN ({tgt_ym_list}) OR T."YMONTH" IN ({tgt_ym_list}))'
        date_params_cte = f"""
            SELECT
                '{period_info.get("cache_key", year_month)}' AS P_YEAR_MONTH,
                TO_DATE('{period_info.get("day_date", selected_date)}') AS P_SELECTED_DATE,
                TO_DATE('{period_info.get("curr_from")}') AS CURRENT_MONTH_START,
                ADD_DAYS(TO_DATE('{period_info.get("curr_to")}'), 1) AS CURRENT_MTD_END,
                TO_DATE('{period_info.get("prev_from")}') AS PREVIOUS_MONTH_START,
                ADD_DAYS(TO_DATE('{period_info.get("prev_to")}'), 1) AS CURRENT_MONTH_END,
                TO_DATE('{period_info.get("prev_to")}') AS PREVIOUS_SELECTED_DATE
            FROM DUMMY
        """
    else:
        target_where = '(CAST(T."YMONTH" AS VARCHAR) = D.P_YEAR_MONTH OR T."YMONTH" = D.P_YEAR_MONTH)'
        date_params_cte = """
            SELECT
                P_YEAR_MONTH,
                P_SELECTED_DATE,
                TO_DATE(P_YEAR_MONTH || '01', 'YYYYMMDD') AS CURRENT_MONTH_START,
                ADD_DAYS(P_SELECTED_DATE, 1) AS CURRENT_MTD_END,
                ADD_MONTHS(
                    TO_DATE(P_YEAR_MONTH || '01', 'YYYYMMDD'),
                    -12
                ) AS PREVIOUS_MONTH_START,
                ADD_MONTHS(
                    TO_DATE(P_YEAR_MONTH || '01', 'YYYYMMDD'),
                    1
                ) AS CURRENT_MONTH_END,
                ADD_MONTHS(
                    P_SELECTED_DATE,
                    -12
                ) AS PREVIOUS_SELECTED_DATE
            FROM INPUT_PARAMS
        """

    hana_sql = f"""
    WITH INPUT_PARAMS AS
    (
        SELECT
            '{year_month}' AS P_YEAR_MONTH,
            TO_DATE('{selected_date}') AS P_SELECTED_DATE
        FROM DUMMY
    ),

    DATE_PARAMS AS
    (
        {date_params_cte}
    ),

    SALES_DATA AS
    (
        SELECT
            "invoice_date",
            COALESCE("ndp_value", 0) AS "invoice_value",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "invoice_number",
            "plant",
            "FKART",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
        WHERE "distribution_channel" = '10'
          AND COALESCE("indicator_cancel", '') <> 'X'
          AND "invoice_date" < '2026-10-18'

        UNION ALL

        SELECT
            cs."invoice_date",
            COALESCE(cs."ndp_value", 0) AS "invoice_value",
            cs."distribution_channel",
            cs."dealer_code",
            cs."SKU",
            cs."invoice_number",
            cs."plant",
            cs."FKART",
            cs."indicator_cancel"
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" cs
        WHERE cs."distribution_channel" = '10'
          AND COALESCE(cs."indicator_cancel", '') <> 'X'
          AND cs."invoice_date" >= '2026-10-18'
    ),

    VALID_SALES AS
    (
        SELECT
            S."invoice_date",
            S."invoice_value",
            S."distribution_channel",
            S."dealer_code",
            S."SKU",
            S."invoice_number",
            S."plant",
            S."FKART"
        FROM SALES_DATA S
        CROSS JOIN DATE_PARAMS D
        INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm
            ON LTRIM(S."dealer_code", '0') = LTRIM(cm."KUNNR", '0')
        WHERE
            S."invoice_date" >= D.PREVIOUS_MONTH_START
            AND S."invoice_date" < D.CURRENT_MTD_END
            AND COALESCE(cm."KLABC", '') <> 'X'
    ),

    CLASSIFIED_SALES AS
    (
        SELECT
            S."invoice_date",
            S."invoice_value",

            CASE
                WHEN (
                    TRIM(COALESCE(M."CAT_CODE", C."CAT_CODE", '')) IN ('B1', 'H1', 'S1')
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) IN ('MOTOR', '3WH', 'SCOOTER', '3W', 'MOTORCYCLE')
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%SCOOTER%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%MOTOR%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%3%WH%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%3%WHEEL%'
                )
                THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END AS MARKET_SEGMENT

        FROM VALID_SALES S

        LEFT JOIN (
            SELECT "MATNR", MAX("CAT_CODE") AS "CAT_CODE"
            FROM "{schema}"."ZIVR_SKUMaster_01_TR"
            GROUP BY "MATNR"
        ) M ON S."SKU" = M."MATNR"

        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" C
            ON M."CAT_CODE" = C."CAT_CODE"
    ),

    SALES_SUMMARY AS
    (
        SELECT
            S.MARKET_SEGMENT,

            SUM(
                CASE
                    WHEN S."invoice_date" >= D.PREVIOUS_MONTH_START
                     AND S."invoice_date" < D.CURRENT_MONTH_START
                    THEN S."invoice_value"
                    ELSE 0
                END
            ) AS PREVIOUS_YEAR_ACTUAL_MONTH,

            SUM(
                CASE
                    WHEN S."invoice_date" >= D.PREVIOUS_MONTH_START
                     AND S."invoice_date" < ADD_DAYS(
                         D.PREVIOUS_SELECTED_DATE, 1
                     )
                    THEN S."invoice_value"
                    ELSE 0
                END
            ) AS PREVIOUS_YEAR_ACTUAL_MTD,

            SUM(
                CASE
                    WHEN S."invoice_date" >= D.CURRENT_MONTH_START
                     AND S."invoice_date" < D.CURRENT_MTD_END
                    THEN S."invoice_value"
                    ELSE 0
                END
            ) AS ACTUAL,

            SUM(
                CASE
                    WHEN S."invoice_date" >= D.P_SELECTED_DATE
                     AND S."invoice_date" < D.CURRENT_MTD_END
                    THEN S."invoice_value"
                    ELSE 0
                END
            ) AS SALE_FOR_THE_DAY

        FROM CLASSIFIED_SALES S

        CROSS JOIN DATE_PARAMS D

        GROUP BY
            S.MARKET_SEGMENT
    ),

    TARGET_SUMMARY AS
    (
        SELECT
            CASE
                WHEN (
                    TRIM(COALESCE(T."CAT_CODE", C."CAT_CODE", '')) IN ('B1', 'H1', 'S1')
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) IN ('MOTOR', '3WH', 'SCOOTER', '3W', 'MOTORCYCLE')
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%SCOOTER%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%MOTOR%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%3%WH%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%3%WHEEL%'
                )
                THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END AS MARKET_SEGMENT,

            SUM(COALESCE(NULLIF(T."REV_PLAN_VAL", 0), T."PLAN_VALUE", 0)) AS TARGET

        FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" T

        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" C
            ON TRIM(T."CAT_CODE") = TRIM(C."CAT_CODE")

        CROSS JOIN DATE_PARAMS D

        WHERE
            {target_where}

        GROUP BY
            CASE
                WHEN (
                    TRIM(COALESCE(T."CAT_CODE", C."CAT_CODE", '')) IN ('B1', 'H1', 'S1')
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) IN ('MOTOR', '3WH', 'SCOOTER', '3W', 'MOTORCYCLE')
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%SCOOTER%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%MOTOR%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%3%WH%'
                    OR UPPER(TRIM(COALESCE(C."CAT_DESC", ''))) LIKE '%3%WHEEL%'
                )
                THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END
    ),

    MARKET_SEGMENTS AS
    (
        SELECT '4 Wheelers' AS MARKET_SEGMENT
        FROM DUMMY

        UNION ALL

        SELECT '2/3 Wheelers' AS MARKET_SEGMENT
        FROM DUMMY
    )

    SELECT
        MS.MARKET_SEGMENT,

        COALESCE(
            SS.PREVIOUS_YEAR_ACTUAL_MONTH, 0
        ) AS PREVIOUS_YEAR_ACTUAL_MONTH,

        COALESCE(
            SS.PREVIOUS_YEAR_ACTUAL_MTD, 0
        ) AS PREVIOUS_YEAR_ACTUAL_MTD,

        COALESCE(
            TS.TARGET, 0
        ) AS TARGET,

        COALESCE(
            SS.ACTUAL, 0
        ) AS ACTUAL,

        ROUND(
            COALESCE(SS.ACTUAL, 0) * 100.0
            / NULLIF(TS.TARGET, 0),
            2
        ) AS ACHIEVEMENT_PCT,

        COALESCE(
            SS.SALE_FOR_THE_DAY, 0
        ) AS SALE_FOR_THE_DAY,

        ROUND(
            (
                COALESCE(SS.ACTUAL, 0)
                - COALESCE(SS.PREVIOUS_YEAR_ACTUAL_MTD, 0)
            ) * 100.0
            / NULLIF(SS.PREVIOUS_YEAR_ACTUAL_MTD, 0),
            2
        ) AS GROWTH_OVER_LYSMT

    FROM MARKET_SEGMENTS MS

    LEFT JOIN SALES_SUMMARY SS
        ON MS.MARKET_SEGMENT = SS.MARKET_SEGMENT

    LEFT JOIN TARGET_SUMMARY TS
        ON MS.MARKET_SEGMENT = TS.MARKET_SEGMENT

    ORDER BY
        CASE
            WHEN MS.MARKET_SEGMENT = '4 Wheelers' THEN 1
            WHEN MS.MARKET_SEGMENT = '2/3 Wheelers' THEN 2
            WHEN MS.MARKET_SEGMENT = 'OEM + DEF' THEN 3
            WHEN MS.MARKET_SEGMENT = 'STU' THEN 4
            ELSE 5
        END
    """
    sanitize_and_execute_hana(cursor, schema, hana_sql)
    cols = [d[0].upper() for d in cursor.description]
    raw_rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

    # Format rows and compute all 7 market segments matching user's Excel/UI:
    # 1. 4 Wheelers (Distribution Channel 10, non-2/3W)
    # 2. 2/3 Wheelers (Distribution Channel 10, Scooter / Motor Cycle / 3WH)
    # 3. Repl (4 & 2/3 Whlrs) - Subtotal (Row 1 + Row 2)
    # 4. OEM + DEF (Distribution Channels 20 & 40)
    # 5. STU (Distribution Channel 30)
    # 6. OEM + STU + DEF - Subtotal (Row 4 + Row 5)
    # 7. Domestic - Grand Total (Row 3 + Row 6)
    # Scale to ₹ in Crores (1 Cr = 10,000,000) matching executive / client management reports
    scale = 10000000.0
    four_w = next((r for r in raw_rows if r.get("MARKET_SEGMENT") == "4 Wheelers"), {})
    two_three_w = next((r for r in raw_rows if r.get("MARKET_SEGMENT") == "2/3 Wheelers"), {})
    oem_def = next((r for r in raw_rows if r.get("MARKET_SEGMENT") == "OEM + DEF"), {})
    stu = next((r for r in raw_rows if r.get("MARKET_SEGMENT") == "STU"), {})

    def _val(row, k):
        v = float(row.get(k) or 0)
        return round(v / scale, 2) if v > 10000 else round(v, 2)

    def _calc_row(name, row_dict, is_tot=False):
        m = _val(row_dict, "PREVIOUS_YEAR_ACTUAL_MONTH")
        mtd = _val(row_dict, "PREVIOUS_YEAR_ACTUAL_MTD")
        tgt = _val(row_dict, "TARGET")
        act = _val(row_dict, "ACTUAL")
        day = _val(row_dict, "SALE_FOR_THE_DAY")
        achv = round((act * 100.0 / tgt), 2) if tgt > 0 else 0.0
        grth = round(((act - mtd) * 100.0 / mtd), 2) if mtd > 0 else 0.0
        return {
            "market_segment": name,
            "prev_year_actual_month": m,
            "prev_year_actual_mtd": mtd,
            "target": tgt,
            "actual": act,
            "achievement_pct": achv,
            "sale_for_the_day": day,
            "growth_over_lysmt": grth,
            "is_total": is_tot
        }

    def _sum_rows(name, row_a, row_b, is_tot=True):
        m = round(row_a["prev_year_actual_month"] + row_b["prev_year_actual_month"], 2)
        mtd = round(row_a["prev_year_actual_mtd"] + row_b["prev_year_actual_mtd"], 2)
        tgt = round(row_a["target"] + row_b["target"], 2)
        act = round(row_a["actual"] + row_b["actual"], 2)
        day = round(row_a["sale_for_the_day"] + row_b["sale_for_the_day"], 2)
        achv = round((act * 100.0 / tgt), 2) if tgt > 0 else 0.0
        grth = round(((act - mtd) * 100.0 / mtd), 2) if mtd > 0 else 0.0
        return {
            "market_segment": name,
            "prev_year_actual_month": m,
            "prev_year_actual_mtd": mtd,
            "target": tgt,
            "actual": act,
            "achievement_pct": achv,
            "sale_for_the_day": day,
            "growth_over_lysmt": grth,
            "is_total": is_tot
        }

    r_4w = _calc_row("4 Wheelers", four_w, is_tot=False)
    r_23w = _calc_row("2/3 Wheelers", two_three_w, is_tot=False)
    r_repl = _sum_rows("Repl (4 & 2/3 Whlrs)", r_4w, r_23w, is_tot=True)

    result_rows = [
        r_4w,
        r_23w,
        r_repl
    ]
    return result_rows


def get_cached_data(mysql_conn, session_id: str, year_month: str):
    ensure_table(mysql_conn)
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute(f"""
            SELECT `market_segment`, `prev_year_actual_month`, `prev_year_actual_mtd`,
                   `target`, `actual`, `achievement_pct`, `sale_for_the_day`, `growth_over_lysmt`,
                   `selected_date`, `last_synced_at`
            FROM `{TABLE_NAME}`
            WHERE `session_id` = %s AND `year_month` = %s
              AND `market_segment` IN ('4 Wheelers', '2/3 Wheelers', 'Repl (4 & 2/3 Whlrs)')
            ORDER BY 
                CASE 
                    WHEN `market_segment` = '4 Wheelers' THEN 1
                    WHEN `market_segment` = '2/3 Wheelers' THEN 2
                    WHEN `market_segment` = 'Repl (4 & 2/3 Whlrs)' THEN 3
                    ELSE 4
                END
        """, (str(session_id).strip(), str(year_month).strip()))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None

        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        sel_date = str(rows[0].get("selected_date")) if isinstance(rows[0], dict) and rows[0].get("selected_date") else None
        result = []
        for r in rows:
            seg = r.get("market_segment")
            result.append({
                "market_segment": seg,
                "prev_year_actual_month": float(r.get("prev_year_actual_month") or 0),
                "prev_year_actual_mtd": float(r.get("prev_year_actual_mtd") or 0),
                "target": float(r.get("target") or 0),
                "actual": float(r.get("actual") or 0),
                "achievement_pct": float(r.get("achievement_pct") or 0),
                "sale_for_the_day": float(r.get("sale_for_the_day") or 0),
                "growth_over_lysmt": float(r.get("growth_over_lysmt") or 0),
                "is_total": "Repl" in seg or "OEM + STU + DEF" in seg or seg == "Domestic"
            })
        return {"data": result, "selected_date": sel_date, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[{TABLE_NAME}] Cache read error: {e}", flush=True)
        return None


def save_data_to_table(mysql_conn, session_id: str, year_month: str, selected_date: str, rows: list):
    if not mysql_conn or not session_id or not rows:
        return False
    ensure_table(mysql_conn)
    try:
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        cur = mysql_conn.cursor()
        cur.execute(f"DELETE FROM `{TABLE_NAME}` WHERE `session_id` = %s AND `year_month` = %s", (str(session_id).strip(), str(year_month).strip()))
        
        sql = f"""
            INSERT INTO `{TABLE_NAME}`
                (`session_id`, `year_month`, `selected_date`, `market_segment`,
                 `prev_year_actual_month`, `prev_year_actual_mtd`, `target`, `actual`,
                 `achievement_pct`, `sale_for_the_day`, `growth_over_lysmt`,
                 `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        batch = []
        for r in rows:
            batch.append((
                str(session_id).strip(),
                str(year_month).strip(),
                str(selected_date).strip(),
                str(r.get("market_segment")),
                float(r.get("prev_year_actual_month") or 0),
                float(r.get("prev_year_actual_mtd") or 0),
                float(r.get("target") or 0),
                float(r.get("actual") or 0),
                float(r.get("achievement_pct") or 0),
                float(r.get("sale_for_the_day") or 0),
                float(r.get("growth_over_lysmt") or 0),
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


def fetch_zone_wise_domestic_achievement(mysql_conn, session_id: str, year_month: str, hana_conn=None, schema="SAP_S4", selected_date=None):
    """
    Fetches dynamic zone-wise sales achievement metrics from `sales_summary_sales_report_by_values` cache table
    or computes via HANA if cache not present.
    Format returned matches Sheet 1 column structure:
    [
        {
            "zone": "East",
            "prev_year_actual_month": 115.99,
            "prev_year_actual_mtd": 115.99,
            "target": 140.91,
            "actual": 66.13,
            "achievement_pct": 47.0,
            "sale_for_the_day": 2.41,
            "growth_over_lysmt": -43.0,
            "is_total": False
        },
        ...
        {
            "zone": "Total (Repl)",
            "prev_year_actual_month": 827.26,
            "prev_year_actual_mtd": 827.26,
            "target": 1029.23,
            "actual": 971.32,
            "achievement_pct": 94.0,
            "sale_for_the_day": 19.98,
            "growth_over_lysmt": 17.0,
            "is_total": True
        }
    ]
    """
    zone_rows = []
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute("""
            SELECT `zone_name`, `prev_4w`, `prev_23w`, `prev_total`,
                   `target_4w`, `target_23w`, `target_total`,
                   `actual_4w`, `actual_23w`, `actual_total`,
                   `achievement_pct`, `sale_for_the_day`, `sort_order`, `is_total`
            FROM `sales_summary_sales_report_by_values`
            WHERE `session_id` = %s AND `year_month` = %s
            ORDER BY `sort_order` ASC, `id` ASC
        """, (str(session_id).strip(), str(year_month).strip()))
        cached_zones = cur.fetchall()
        cur.close()

        if cached_zones:
            for r in cached_zones:
                z_name = str(r.get("zone_name") or "").strip()
                if not z_name or "UNKNOWN" in z_name.upper():
                    continue
                is_tot = bool(r.get("is_total")) or z_name.lower() == "total"
                display_name = "Total (Repl)" if is_tot else z_name

                p_mtd = float(r.get("prev_total") or 0.0)
                p_month = float(r.get("prev_total") or 0.0)
                tgt = float(r.get("target_total") or 0.0)
                act = float(r.get("actual_total") or 0.0)
                achv = float(r.get("achievement_pct") or 0.0)
                if achv == 0.0 and tgt > 0:
                    achv = round((act * 100.0 / tgt), 2)
                day_sale = float(r.get("sale_for_the_day") or 0.0)
                grth = round(((act - p_mtd) * 100.0 / p_mtd), 2) if p_mtd > 0 else 0.0

                zone_rows.append({
                    "zone": display_name,
                    "prev_year_actual_month": p_month,
                    "prev_year_actual_mtd": p_mtd,
                    "target": tgt,
                    "actual": act,
                    "achievement_pct": achv,
                    "sale_for_the_day": day_sale,
                    "growth_over_lysmt": grth,
                    "is_total": is_tot
                })
    except Exception as e:
        print(f"[{TABLE_NAME}] Error fetching cached zone achievement: {e}", flush=True)

    # If not in cache and HANA available, compute via HANA query
    if not zone_rows and hana_conn:
        try:
            from controllers.sales_report_by_values_controller import query_sales_report_by_values_hana
            h_cur = hana_conn.cursor()
            hana_res = query_sales_report_by_values_hana(h_cur, schema, year_month, selected_date or f"{year_month[:4]}-{year_month[4:6]}-01")
            h_cur.close()
            if hana_res:
                for r in hana_res:
                    z_name = str(r.get("zone") or "").strip()
                    if not z_name or "UNKNOWN" in z_name.upper():
                        continue
                    is_tot = bool(r.get("is_total")) or z_name.lower() == "total"
                    display_name = "Total (Repl)" if is_tot else z_name

                    p_mtd = float(r.get("prev_total") or 0.0)
                    p_month = float(r.get("prev_total") or 0.0)
                    tgt = float(r.get("target_total") or 0.0)
                    act = float(r.get("actual_total") or 0.0)
                    achv = float(r.get("achievement_pct") or 0.0)
                    if achv == 0.0 and tgt > 0:
                        achv = round((act * 100.0 / tgt), 2)
                    day_sale = float(r.get("sale_for_the_day") or 0.0)
                    grth = round(((act - p_mtd) * 100.0 / p_mtd), 2) if p_mtd > 0 else 0.0

                    zone_rows.append({
                        "zone": display_name,
                        "prev_year_actual_month": p_month,
                        "prev_year_actual_mtd": p_mtd,
                        "target": tgt,
                        "actual": act,
                        "achievement_pct": achv,
                        "sale_for_the_day": day_sale,
                        "growth_over_lysmt": grth,
                        "is_total": is_tot
                    })
        except Exception as e:
            print(f"[{TABLE_NAME}] Error calculating zone achievement from HANA: {e}", flush=True)

    return zone_rows


def get_domestic_sales_value_achievement_controller(get_db_connection):
    """
    HTTP Controller for Domestic Sales Value Achievement.
    Query params:
      session_id: Workspace session ID
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
        # 1. Dynamically detect latest sales dates from HANA (NO hardcoding!)
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

        # 2. Fetch the 12 master months dynamically from ZITR_DayAttribute_01_TR
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

        # 3. Determine dynamic period parameters (Single Month, Multi-Month, Custom Date Range)
        req_params = request.get_json(silent=True) if request.is_json else request.args
        from helper.period_filter_helper import parse_period_args
        period_info = parse_period_args(req_params, default_year=current_year, default_month=detected_month, default_day=detected_day)

        year_month = period_info["cache_key"]
        selected_date = period_info["day_date"]
        selected_mo_num = period_info["selected_month"]

        meta_info = {
            "month_abbr": period_info["meta"]["month_abbr"],
            "unit": "₹ in Cr",
            "prev_year_label": period_info["meta"]["prev_year_label"],
            "curr_year_label": period_info["meta"]["curr_year_label"],
            "day_label": period_info["meta"]["day_label"],
            "filter_mode": period_info["meta"]["filter_mode"]
        }
        if "months" in period_info["meta"]:
            meta_info["months"] = period_info["meta"]["months"]
        if "start_date" in period_info["meta"]:
            meta_info["start_date"] = period_info["meta"]["start_date"]
            meta_info["end_date"] = period_info["meta"]["end_date"]

        force_refresh = (request.args.get("force_refresh", "").lower() in ("true", "1")) or \
                        (request.is_json and str(request.json.get("force_refresh", "")).lower() in ("true", "1"))

        # 4. Check cache table if not force_refresh
        if not force_refresh:
            cached = get_cached_data(mysql_conn, effective_session, year_month)
            if cached and cached.get("data"):
                zone_data = fetch_zone_wise_domestic_achievement(
                    mysql_conn, effective_session, year_month, hana_conn, schema, selected_date
                )
                return jsonify({
                    "status": "success",
                    "source": TABLE_NAME,
                    "data": cached["data"],
                    "zone_data": zone_data,
                    "months": master_months,
                    "selected_month": selected_mo_num,
                    "current_year": current_year,
                    "previous_year": previous_year,
                    "year_month": year_month,
                    "selected_date": cached.get("selected_date") or selected_date,
                    "meta": meta_info,
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        # 5. Query from SAP HANA
        if is_hana:
            h_cur = hana_conn.cursor()
            rows = query_domestic_sales_value_achievement_hana(h_cur, schema, year_month, selected_date, period_info=period_info)
            h_cur.close()
        else:
            rows = [
                {"market_segment": "4 Wheelers", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": False},
                {"market_segment": "2/3 Wheelers", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": False},
                {"market_segment": "Repl (4 & 2/3 Whlrs)", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": True},
                {"market_segment": "OEM + DEF", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": False},
                {"market_segment": "STU", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": False},
                {"market_segment": "OEM + STU + DEF", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": True},
                {"market_segment": "Domestic", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0, "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": True}
            ]

        # 6. Save into cache table
        if rows:
            save_data_to_table(mysql_conn, effective_session, year_month, selected_date, rows)

        zone_data = fetch_zone_wise_domestic_achievement(
            mysql_conn, effective_session, year_month, hana_conn, schema, selected_date
        )

        return jsonify({
            "status": "success",
            "source": TABLE_NAME,
            "data": rows,
            "zone_data": zone_data,
            "months": master_months,
            "selected_month": selected_mo_num,
            "current_year": current_year,
            "previous_year": previous_year,
            "year_month": year_month,
            "selected_date": selected_date,
            "meta": meta_info,
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



def bulk_sync_domestic_sales_value_achievement(mysql_conn, session_id: str, user_id=None):
    """
    Pre-calculates and caches all available months permutations for Domestic Sales Value Achievement
    into MySQL table `sales_summary_domestic_sales_value_achievement`.
    """
    if not mysql_conn or not session_id:
        return False

    ensure_table(mysql_conn)
    from helper.sap_connection import get_sap_connection_for_session
    hana_conn = None
    tunnel = None
    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id=user_id)
        if not hana_conn:
            print(f"[{TABLE_NAME}] No HANA connection for bulk sync.", flush=True)
            return False

        h_cur = hana_conn.cursor()
        from helper.sap_connection import detect_sales_dates
        det_year, det_month, det_day = detect_sales_dates(h_cur, schema)
        master_months = fetch_master_months(h_cur, schema)
        curr_yr = int(det_year)
        years_to_sync = [curr_yr, curr_yr - 1]
        print(f"[{TABLE_NAME}] Bulk syncing {len(master_months)} months for years {years_to_sync} (current and previous)...", flush=True)

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

                    rows = query_domestic_sales_value_achievement_hana(h_cur, schema, ym, sel_date)
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
