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


def fetch_available_months(cursor, schema, mysql_conn=None):
    """Returns available YMONTH options for dropdown (e.g. ['202611', '202610', ...])."""
    months = []
    try:
        sql = f"""
            SELECT DISTINCT "YMONTH"
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR"
            WHERE "YMONTH" IS NOT NULL AND LENGTH("YMONTH") = 6
            ORDER BY "YMONTH" DESC
        """
        cursor.execute(sql)
        for r in cursor.fetchall():
            if r and r[0]:
                months.append(str(r[0]).strip())
    except Exception as e:
        print(f"[{TABLE_NAME}] Error fetching months from HANA: {e}", flush=True)

    if not months:
        # Fallback default months
        now = datetime.now()
        for i in range(12):
            dt = now - timedelta(days=i * 30)
            months.append(dt.strftime("%Y%m"))
        months = sorted(list(set(months)), reverse=True)

    return months


def query_domestic_sales_value_achievement_hana(cursor, schema, year_month: str, selected_date: str):
    """
    Executes user's exact HANA SQL query for Domestic Sales Value Achievement.
    Parameters:
      year_month: e.g. '202611'
      selected_date: e.g. '2026-11-04'
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
    ),

    SALES_DATA AS
    (
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
    ),

    CUSTOMER_MAP AS
    (
        SELECT
            LTRIM(TRIM("KUNNR"), '0') AS CUSTOMER_CODE,

            MAX(
                CASE
                    WHEN "KLABC" = 'X' THEN 1
                    ELSE 0
                END
            ) AS EXCLUDED_FLAG

        FROM "{schema}"."ZIVF_CustomerMaster_01_MD"

        GROUP BY
            LTRIM(TRIM("KUNNR"), '0')
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

        LEFT JOIN CUSTOMER_MAP C
            ON LTRIM(TRIM(S."dealer_code"), '0') = C.CUSTOMER_CODE

        CROSS JOIN DATE_PARAMS D

        WHERE
            S."invoice_date" >= D.PREVIOUS_MONTH_START

            AND S."invoice_date" < D.CURRENT_MTD_END

            AND S."distribution_channel" IN ('10', '20', '30', '40')

            AND COALESCE(S."indicator_cancel", '') <> 'X'

            AND COALESCE(C.EXCLUDED_FLAG, 0) = 0

            AND NOT EXISTS
            (
                SELECT 1

                FROM "{schema}"."ZPVF_SD_CancelledSalesInvoice_01_TR" X

                WHERE
                    X."invoice_number" = S."invoice_number"

                    AND X."plant" = S."plant"

                    AND X."FKART" = S."FKART"

                    AND X."indicator_cancel" = 'X'
            )
    ),

    CLASSIFIED_SALES AS
    (
        SELECT
            S."invoice_date",
            S."invoice_value",

            CASE
                WHEN UPPER(TRIM(C."CAT_DESC")) IN
                     ('MOTOR', '3WH', 'SCOOTER')
                    THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END AS MARKET_SEGMENT

        FROM VALID_SALES S

        INNER JOIN "{schema}"."ZIVR_SKUMaster_01_TR" M
            ON TRIM(S."SKU") = TRIM(M."MATNR")

        INNER JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" C
            ON M."CAT_CODE" = C."CAT_CODE"

        WHERE
            C."CAT_CODE" NOT IN ('TR', 'NT', 'AL', 'IE')
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
                WHEN UPPER(TRIM(C."CAT_DESC")) IN
                     ('MOTOR', '3WH', 'SCOOTER')
                    THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END AS MARKET_SEGMENT,

            SUM(T."PLAN_VALUE") AS TARGET

        FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" T

        INNER JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" C
            ON TRIM(T."CAT_CODE") = TRIM(C."CAT_CODE")

        CROSS JOIN DATE_PARAMS D

        WHERE
            T."YMONTH" = D.P_YEAR_MONTH

            AND T."CAT_CODE" NOT IN ('TR', 'NT', 'AL', 'IE')

        GROUP BY
            CASE
                WHEN UPPER(TRIM(C."CAT_DESC")) IN
                     ('MOTOR', '3WH', 'SCOOTER')
                    THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END
    ),

    MARKET_SEGMENTS AS
    (
        SELECT '2/3 Wheelers' AS MARKET_SEGMENT
        FROM DUMMY

        UNION ALL

        SELECT '4 Wheelers' AS MARKET_SEGMENT
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
            ELSE 2
        END
    """
    sanitize_and_execute_hana(cursor, schema, hana_sql)
    cols = [d[0].upper() for d in cursor.description]
    raw_rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

    # Format rows and compute 'Repl (4 & 2/3 Whlrs)' total row
    # Invoice values in Lakhs (divide by 100,000 for display matching user's Excel/UI)
    # Check magnitude: if raw values are in Rupees (>1,000,000), divide by 100,000 to show in Lakhs
    scale = 100000.0
    four_w = next((r for r in raw_rows if r.get("MARKET_SEGMENT") == "4 Wheelers"), {})
    two_three_w = next((r for r in raw_rows if r.get("MARKET_SEGMENT") == "2/3 Wheelers"), {})

    def _val(row, k):
        v = float(row.get(k) or 0)
        return round(v / scale, 2) if v > 100000 else round(v, 2)

    f_month = _val(four_w, "PREVIOUS_YEAR_ACTUAL_MONTH")
    f_mtd = _val(four_w, "PREVIOUS_YEAR_ACTUAL_MTD")
    f_tgt = _val(four_w, "TARGET")
    f_act = _val(four_w, "ACTUAL")
    f_day = _val(four_w, "SALE_FOR_THE_DAY")
    f_achv = round((f_act * 100.0 / f_tgt), 2) if f_tgt > 0 else 0.0
    f_grth = round(((f_act - f_mtd) * 100.0 / f_mtd), 2) if f_mtd > 0 else 0.0

    t_month = _val(two_three_w, "PREVIOUS_YEAR_ACTUAL_MONTH")
    t_mtd = _val(two_three_w, "PREVIOUS_YEAR_ACTUAL_MTD")
    t_tgt = _val(two_three_w, "TARGET")
    t_act = _val(two_three_w, "ACTUAL")
    t_day = _val(two_three_w, "SALE_FOR_THE_DAY")
    t_achv = round((t_act * 100.0 / t_tgt), 2) if t_tgt > 0 else 0.0
    t_grth = round(((t_act - t_mtd) * 100.0 / t_mtd), 2) if t_mtd > 0 else 0.0

    tot_month = round(f_month + t_month, 2)
    tot_mtd = round(f_mtd + t_mtd, 2)
    tot_tgt = round(f_tgt + t_tgt, 2)
    tot_act = round(f_act + t_act, 2)
    tot_day = round(f_day + t_day, 2)
    tot_achv = round((tot_act * 100.0 / tot_tgt), 2) if tot_tgt > 0 else 0.0
    tot_grth = round(((tot_act - tot_mtd) * 100.0 / tot_mtd), 2) if tot_mtd > 0 else 0.0

    result_rows = [
        {
            "market_segment": "4 Wheelers",
            "prev_year_actual_month": f_month,
            "prev_year_actual_mtd": f_mtd,
            "target": f_tgt,
            "actual": f_act,
            "achievement_pct": f_achv,
            "sale_for_the_day": f_day,
            "growth_over_lysmt": f_grth,
            "is_total": False
        },
        {
            "market_segment": "2/3 Wheelers",
            "prev_year_actual_month": t_month,
            "prev_year_actual_mtd": t_mtd,
            "target": t_tgt,
            "actual": t_act,
            "achievement_pct": t_achv,
            "sale_for_the_day": t_day,
            "growth_over_lysmt": t_grth,
            "is_total": False
        },
        {
            "market_segment": "Repl (4 & 2/3 Whlrs)",
            "prev_year_actual_month": tot_month,
            "prev_year_actual_mtd": tot_mtd,
            "target": tot_tgt,
            "actual": tot_act,
            "achievement_pct": tot_achv,
            "sale_for_the_day": tot_day,
            "growth_over_lysmt": tot_grth,
            "is_total": True
        }
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
            ORDER BY 
                CASE 
                    WHEN `market_segment` = '4 Wheelers' THEN 1
                    WHEN `market_segment` = '2/3 Wheelers' THEN 2
                    ELSE 3
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
                "is_total": "Repl" in seg
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
    year_month = request.args.get("year_month") or (request.json.get("year_month") if request.is_json else None)
    selected_date = request.args.get("selected_date") or (request.json.get("selected_date") if request.is_json else None)
    force_refresh = (request.args.get("force_refresh", "").lower() in ("true", "1")) or \
                    (request.is_json and str(request.json.get("force_refresh", "")).lower() in ("true", "1"))

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = \
        _get_connection_context(get_db_connection, session_id)

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        # Determine available months and default active month
        available_months = []
        if is_hana:
            h_cur = hana_conn.cursor()
            available_months = fetch_available_months(h_cur, schema, mysql_conn)
            h_cur.close()

        if not available_months:
            available_months = ["202611", "202610", "202609", "202608", "202607", "202606", "202605", "202604", "202603", "202602", "202601"]

        if not year_month or str(year_month).strip() in ("", "null", "None"):
            year_month = available_months[0]
        else:
            year_month = str(year_month).strip()

        # Determine selected_date for that year_month
        if not selected_date or str(selected_date).strip() in ("", "null", "None"):
            # Default to latest sales date or end of month/today
            yr = int(year_month[:4])
            mo = int(year_month[4:6])
            now = datetime.now()
            if yr == now.year and mo == now.month:
                selected_date = now.strftime("%Y-%m-%d")
            else:
                last_d = calendar.monthrange(yr, mo)[1]
                selected_date = f"{yr}-{str(mo).zfill(2)}-{str(last_d).zfill(2)}"
        else:
            selected_date = str(selected_date).strip()

        # 1. Check cache table if not force_refresh
        if not force_refresh:
            cached = get_cached_data(mysql_conn, effective_session, year_month)
            if cached and cached.get("data"):
                # Compute header labels
                yr = int(year_month[:4])
                mo = int(year_month[4:6])
                month_abbr = calendar.month_abbr[mo]
                prev_yr_short = str(yr - 1)[-2:]
                curr_yr_short = str(yr)[-2:]
                try:
                    s_day = int(selected_date.split("-")[2])
                except Exception:
                    s_day = 1

                return jsonify({
                    "status": "success",
                    "source": TABLE_NAME,
                    "data": cached["data"],
                    "available_months": available_months,
                    "active_month": year_month,
                    "selected_date": cached.get("selected_date") or selected_date,
                    "meta": {
                        "month_abbr": month_abbr,
                        "prev_year_label": f"{month_abbr}-{prev_yr_short} Actual",
                        "curr_year_label": f"Actual Sale for {month_abbr}-{curr_yr_short}",
                        "day_label": f"Sale for day {s_day}"
                    },
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        # 2. Query from SAP HANA
        if is_hana:
            h_cur = hana_conn.cursor()
            rows = query_domestic_sales_value_achievement_hana(h_cur, schema, year_month, selected_date)
            h_cur.close()
        else:
            # Fallback mock/empty if HANA unavailable
            rows = [
                {
                    "market_segment": "4 Wheelers", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0,
                    "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": False
                },
                {
                    "market_segment": "2/3 Wheelers", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0,
                    "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": False
                },
                {
                    "market_segment": "Repl (4 & 2/3 Whlrs)", "prev_year_actual_month": 0, "prev_year_actual_mtd": 0,
                    "target": 0, "actual": 0, "achievement_pct": 0, "sale_for_the_day": 0, "growth_over_lysmt": 0, "is_total": True
                }
            ]

        # 3. Save into cache table
        if rows:
            save_data_to_table(mysql_conn, effective_session, year_month, selected_date, rows)

        yr = int(year_month[:4])
        mo = int(year_month[4:6])
        month_abbr = calendar.month_abbr[mo]
        prev_yr_short = str(yr - 1)[-2:]
        curr_yr_short = str(yr)[-2:]
        try:
            s_day = int(selected_date.split("-")[2])
        except Exception:
            s_day = 1

        return jsonify({
            "status": "success",
            "source": TABLE_NAME,
            "data": rows,
            "available_months": available_months,
            "active_month": year_month,
            "selected_date": selected_date,
            "meta": {
                "month_abbr": month_abbr,
                "prev_year_label": f"{month_abbr}-{prev_yr_short} Actual",
                "curr_year_label": f"Actual Sale for {month_abbr}-{curr_yr_short}",
                "day_label": f"Sale for day {s_day}"
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
        months = fetch_available_months(h_cur, schema, mysql_conn)
        print(f"[{TABLE_NAME}] Bulk syncing {len(months)} months for session {session_id[:8]}...", flush=True)

        now = datetime.now()
        for ym in months:
            try:
                yr = int(ym[:4])
                mo = int(ym[4:6])
                if yr == now.year and mo == now.month:
                    sel_date = now.strftime("%Y-%m-%d")
                else:
                    last_d = calendar.monthrange(yr, mo)[1]
                    sel_date = f"{yr}-{str(mo).zfill(2)}-{str(last_d).zfill(2)}"

                rows = query_domestic_sales_value_achievement_hana(h_cur, schema, ym, sel_date)
                if rows:
                    save_data_to_table(mysql_conn, session_id, ym, sel_date, rows)
            except Exception as e_ym:
                print(f"[{TABLE_NAME}] Error syncing month {ym}: {e_ym}", flush=True)

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
