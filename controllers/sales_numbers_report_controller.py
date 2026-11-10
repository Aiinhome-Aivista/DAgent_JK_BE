"""
Dedicated Controller for Sales Number's Report (Sheet 2).
Manages dedicated cache table: `sales_summary_sales_numbers`
Calculates sales volume in Tyre Quantity (Numbers / Units) across:
- Replacement (Channel 10): Prev Year Month Actual, Target, MTD Total, % Achv, Today, Dealer, Dist., Fleet, Others
- OEM (Channel 20): Target, Actual
- STU (Channel 30): Target, Actual
- DEF (Channel 40): Target, Actual
- Domestic Total: Target, Actual, % Achv.
Grouped by Category: <TYRE_TYPE> - <CONSTRUCTION> (Radial / Bias)
"""

import calendar
from datetime import datetime
from flask import request, jsonify
from helper.sap_connection import get_sap_connection_for_session, sanitize_and_execute_hana, detect_sales_dates
from utils.logger_service import log_system_error

TABLE_NAME = "sales_summary_sales_numbers"

CATEGORY_CODE_MAP = {
    "0": "TRUCK",
    "1": "LCV",
    "2": "CAR",
    "3": "JEEP",
    "4": "TRACTOR",
    "5": "TRACTOR",
    "6": "3W",
    "7": "FARM",
    "A": "ADV",
    "B": "2W / SCOOTER",
    "C": "LCV",
    "D": "OTR / DUMPER",
    "E": "OTR / EARTHMOVER",
    "F": "FARM / TRACTOR",
    "H": "3W",
    "I": "INDUSTRIAL",
    "J": "JEEP / SUV",
    "K": "CRANE / FORKLIFT",
    "L": "LIGHT TRUCK",
    "M": "2W / MOTORCYCLE",
    "MOTOR": "2W / MOTORCYCLE",
    "N": "LCV / BUS",
    "O": "OTR",
    "P": "CAR",
    "Q": "3W / QUAD",
    "R": "SPECIAL / ROLLER",
    "S": "2W / SCOOTER",
    "T": "TRACTOR",
    "TBB": "TRUCK",
    "TBR": "TRUCK",
    "TRACTOR": "TRACTOR",
    "V": "COMMERCIAL / OTR",
    "Y": "INDUSTRIAL",
    "Z": "RETREAD",
}

def normalize_category_name(raw_name: str) -> str:
    if not raw_name:
        return "Unknown"
    trimmed = str(raw_name).strip()
    if trimmed.lower() == "total":
        return "Total"
    if " - " in trimmed:
        parts = trimmed.split(" - ", 1)
        code = parts[0].strip().upper()
        construction = parts[1].strip()
        mapped = CATEGORY_CODE_MAP.get(code, parts[0].strip())
        return f"{mapped} - {construction}"
    code = trimmed.upper()
    return CATEGORY_CODE_MAP.get(code, trimmed)

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `year_month` VARCHAR(10) NOT NULL,
    `selected_date` DATE NOT NULL,
    `category` VARCHAR(100) NOT NULL,
    `prev_year_act` DECIMAL(18,2) DEFAULT 0,
    `target` DECIMAL(18,2) DEFAULT 0,
    `total` DECIMAL(18,2) DEFAULT 0,
    `achievement_pct` DECIMAL(10,2) DEFAULT 0,
    `today` DECIMAL(18,2) DEFAULT 0,
    `dealer` DECIMAL(18,2) DEFAULT 0,
    `distributor` DECIMAL(18,2) DEFAULT 0,
    `fleet` DECIMAL(18,2) DEFAULT 0,
    `others` DECIMAL(18,2) DEFAULT 0,
    `oem_target` DECIMAL(18,2) DEFAULT 0,
    `oem_actual` DECIMAL(18,2) DEFAULT 0,
    `stu_target` DECIMAL(18,2) DEFAULT 0,
    `stu_actual` DECIMAL(18,2) DEFAULT 0,
    `def_target` DECIMAL(18,2) DEFAULT 0,
    `def_actual` DECIMAL(18,2) DEFAULT 0,
    `domestic_target` DECIMAL(18,2) DEFAULT 0,
    `domestic_actual` DECIMAL(18,2) DEFAULT 0,
    `domestic_achv_pct` DECIMAL(10,2) DEFAULT 0,
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


def query_sales_numbers_hana(cursor, schema, year_month: str, selected_date: str):
    """
    Executes memory-optimized HANA query for Sales Number's (Sheet 2).
    Slices sales numbers by:
    - Replacement (Channel 10): Prev Year Month Actual, Target, MTD Total, % Achv, Today, Dealer, Dist., Fleet, Others
    - OEM (Channel 20): Target, Actual
    - STU (Channel 30): Target, Actual
    - DEF (Channel 40): Target, Actual
    - Domestic Total: Target, Actual, % Achv.
    Grouped by Category: <TYRE_TYPE> - <CONSTRUCTION>
    """
    curr_yr = int(year_month[:4])
    curr_mo = int(year_month[4:6])
    prev_yr = curr_yr - 1

    last_day_curr = calendar.monthrange(curr_yr, curr_mo)[1]
    try:
        sel_d = int(str(selected_date).split("-")[2])
    except Exception:
        sel_d = last_day_curr

    curr_from = f"{curr_yr}-{str(curr_mo).zfill(2)}-01"
    curr_month_end = f"{curr_yr}-{str(curr_mo).zfill(2)}-{str(min(sel_d, last_day_curr)).zfill(2)}"
    day_date = str(selected_date)

    last_day_prev = calendar.monthrange(prev_yr, curr_mo)[1]
    prev_from = f"{prev_yr}-{str(curr_mo).zfill(2)}-01"
    prev_month_end = f"{prev_yr}-{str(curr_mo).zfill(2)}-{str(last_day_prev).zfill(2)}"

    hana_sql = f"""
    WITH
    SALES_BASE AS (
        SELECT
            "invoice_date",
            COALESCE(NULLIF("Tyre_Quantity", 0), NULLIF("volume", 0), 0) AS "qty",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "invoice_number",
            "plant",
            "FKART",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
        WHERE ("distribution_channel" IN ('10', 10, '20', 20, '30', 30, '40', 40))
          AND (
              ("invoice_date" >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD'))
              OR
              ("invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND "invoice_date" <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD'))
          )
          AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
        UNION ALL
        SELECT
            "invoice_date",
            COALESCE(NULLIF("Tyre_Quantity", 0), NULLIF("volume", 0), 0) AS "qty",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "invoice_number",
            "plant",
            "FKART",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
        WHERE ("distribution_channel" IN ('10', 10, '20', 20, '30', 30, '40', 40))
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
            s."qty",
            s."distribution_channel",
            s."dealer_code",
            s."SKU"
        FROM SALES_BASE s
    ),

    FACT_CLASSIFIED AS (
        SELECT
            (
                CASE
                    -- 1. Check Category Description
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('TBB', 'TBR', 'TRUCK') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%TRUCK%' THEN 'TRUCK'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('LCB', 'LCR', 'LCV') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%LCV%' THEN 'LCV'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('CB', 'CR', 'CAR', 'PCR') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%CAR%' THEN 'CAR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('JB', 'JR', 'JEEP') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%JEEP%' THEN 'JEEP'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%TRACTOR%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%FARM%' THEN 'TRACTOR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3W%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3 WHEELER%' THEN '3W'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%2W%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTOR%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%SCOOTER%' THEN '2W / MOTOR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%IND%' THEN 'INDUSTRIAL'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%OTR%' THEN 'OTR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%ADV%' THEN 'ADV'
                    -- 2. Check SKU Master TYRE_TYPE / TYRE_TP codes
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('0', 'TRUCK') THEN 'TRUCK'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('1', 'LCV') THEN 'LCV'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('2', 'CAR') THEN 'CAR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('3', 'JEEP') THEN 'JEEP'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('4', '5', '7', 'TRACTOR') THEN 'TRACTOR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('6', 'H', '3W') THEN '3W'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('M', 'B', 'S', '2W') THEN '2W / MOTOR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('I', 'Y', 'IND') THEN 'INDUSTRIAL'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('E', 'OTR') THEN 'OTR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", '')) IN ('A', 'ADV') THEN 'ADV'
                    WHEN NULLIF(TRIM(cat."CAT_DESC"), '') IS NOT NULL THEN TRIM(cat."CAT_DESC")
                    WHEN NULLIF(TRIM(sk."TYRE_TYPE"), '') IS NOT NULL THEN TRIM(sk."TYRE_TYPE")
                    ELSE 'OTHERS'
                END
                || ' - ' ||
                CASE
                    WHEN CAST(sk."CONSTUCTION" AS VARCHAR) IN ('1', 'BIAS')
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%BIAS%'
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%B'
                         OR UPPER(TRIM(CAST(COALESCE(sk."CONSTUCTION", '') AS VARCHAR))) = 'BIAS' THEN 'BIAS'
                    WHEN CAST(sk."CONSTUCTION" AS VARCHAR) IN ('2', '5', 'RADIAL')
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%RADIAL%'
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%R%'
                         OR UPPER(TRIM(CAST(COALESCE(sk."CONSTUCTION", '') AS VARCHAR))) = 'RADIAL' THEN 'RADIAL'
                    ELSE 'BIAS'
                END
            ) AS Category,
            s."distribution_channel" AS dist_ch,
            s."invoice_date" AS ddate,
            s."qty" AS qty,
            COALESCE(cm."CH_TYPE", '') AS ch_type,
            COALESCE(cm."KTOKD", '') AS ktokd,
            COALESCE(cm."KLABC", '') AS klabc
        FROM SALES_FILTERED s
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk
            ON LTRIM(TRIM(s."SKU"), '0') = LTRIM(TRIM(sk."MATNR"), '0')
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat
            ON TRIM(sk."CAT_CODE") = TRIM(cat."CAT_CODE")
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm
            ON LTRIM(TRIM(s."dealer_code"), '0') = LTRIM(TRIM(cm."KUNNR"), '0')
        WHERE (cm."KLABC" IS NULL OR UPPER(TRIM(cm."KLABC")) <> 'X')
    ),

    AGG_SALES AS (
        SELECT
            Category,
            -- Replacement Prior Year Month Actual
            SUM(CASE WHEN (dist_ch = '10' OR dist_ch = 10) AND ddate >= TO_DATE('{prev_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{prev_month_end}', 'YYYY-MM-DD') THEN qty ELSE 0 END) AS Prev_Act,
            -- Replacement Current Month MTD Total
            SUM(CASE WHEN (dist_ch = '10' OR dist_ch = 10) AND ddate >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD') THEN qty ELSE 0 END) AS Repl_Total,
            -- Replacement Today
            SUM(CASE WHEN (dist_ch = '10' OR dist_ch = 10) AND ddate = TO_DATE('{day_date}', 'YYYY-MM-DD') THEN qty ELSE 0 END) AS Repl_Today,
            -- Replacement Dealer
            SUM(CASE WHEN (dist_ch = '10' OR dist_ch = 10) AND ddate >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD')
                     AND (UPPER(TRIM(ch_type)) IN ('DL', 'DEALER') OR UPPER(TRIM(ktokd)) IN ('0001', 'DEALER')) THEN qty ELSE 0 END) AS Repl_Dealer,
            -- Replacement Distributor
            SUM(CASE WHEN (dist_ch = '10' OR dist_ch = 10) AND ddate >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD')
                     AND (UPPER(TRIM(ch_type)) IN ('DB', 'DISTRIBUTOR') OR UPPER(TRIM(ktokd)) IN ('DISTRIBUTOR') OR UPPER(TRIM(klabc)) = 'DB') THEN qty ELSE 0 END) AS Repl_Dist,
            -- Replacement Fleet
            SUM(CASE WHEN (dist_ch = '10' OR dist_ch = 10) AND ddate >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD')
                     AND (UPPER(TRIM(ch_type)) IN ('FL', 'FLEET') OR UPPER(TRIM(ktokd)) IN ('FLEET')) THEN qty ELSE 0 END) AS Repl_Fleet,
            -- OEM Actual (Channel 20)
            SUM(CASE WHEN (dist_ch = '20' OR dist_ch = 20) AND ddate >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD') THEN qty ELSE 0 END) AS OEM_Actual,
            -- STU Actual (Channel 30)
            SUM(CASE WHEN (dist_ch = '30' OR dist_ch = 30) AND ddate >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD') THEN qty ELSE 0 END) AS STU_Actual,
            -- DEF Actual (Channel 40)
            SUM(CASE WHEN (dist_ch = '40' OR dist_ch = 40) AND ddate >= TO_DATE('{curr_from}', 'YYYY-MM-DD') AND ddate <= TO_DATE('{curr_month_end}', 'YYYY-MM-DD') THEN qty ELSE 0 END) AS DEF_Actual
        FROM FACT_CLASSIFIED
        GROUP BY Category
    ),

    TARGET_CLASSIFIED AS (
        SELECT
            (
                CASE
                    -- 1. Check Category Description
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('TBB', 'TBR', 'TRUCK') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%TRUCK%' THEN 'TRUCK'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('LCB', 'LCR', 'LCV') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%LCV%' THEN 'LCV'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('CB', 'CR', 'CAR', 'PCR') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%CAR%' THEN 'CAR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('JB', 'JR', 'JEEP') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%JEEP%' THEN 'JEEP'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%TRACTOR%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%FARM%' THEN 'TRACTOR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3W%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%3 WHEELER%' THEN '3W'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%2W%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%MOTOR%' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%SCOOTER%' THEN '2W / MOTOR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%IND%' THEN 'INDUSTRIAL'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%OTR%' THEN 'OTR'
                    WHEN UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%ADV%' THEN 'ADV'
                    -- 2. Check SKU / Target MAT_TYPE codes
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('0', 'TRUCK') THEN 'TRUCK'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('1', 'LCV') THEN 'LCV'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('2', 'CAR') THEN 'CAR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('3', 'JEEP') THEN 'JEEP'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('4', '5', '7', 'TRACTOR') THEN 'TRACTOR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('6', 'H', '3W') THEN '3W'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('M', 'B', 'S', '2W') THEN '2W / MOTOR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('I', 'Y', 'IND') THEN 'INDUSTRIAL'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('E', 'OTR') THEN 'OTR'
                    WHEN TRIM(COALESCE(sk."TYRE_TYPE", sk."TYRE_TP", tgt."MAT_TYPE", '')) IN ('A', 'ADV') THEN 'ADV'
                    WHEN NULLIF(TRIM(cat."CAT_DESC"), '') IS NOT NULL THEN TRIM(cat."CAT_DESC")
                    WHEN NULLIF(TRIM(sk."TYRE_TYPE"), '') IS NOT NULL THEN TRIM(sk."TYRE_TYPE")
                    WHEN NULLIF(TRIM(tgt."MAT_TYPE"), '') IS NOT NULL THEN TRIM(tgt."MAT_TYPE")
                    ELSE 'OTHERS'
                END
                || ' - ' ||
                CASE
                    WHEN CAST(sk."CONSTUCTION" AS VARCHAR) IN ('1', 'BIAS')
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%BIAS%'
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%B'
                         OR UPPER(TRIM(CAST(COALESCE(sk."CONSTUCTION", '') AS VARCHAR))) = 'BIAS' THEN 'BIAS'
                    WHEN CAST(sk."CONSTUCTION" AS VARCHAR) IN ('2', '5', 'RADIAL')
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%RADIAL%'
                         OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) LIKE '%R%'
                         OR UPPER(TRIM(CAST(COALESCE(sk."CONSTUCTION", '') AS VARCHAR))) = 'RADIAL' THEN 'RADIAL'
                    ELSE 'BIAS'
                END
            ) AS Category,
            COALESCE(NULLIF(tgt."ORG_PLAN", 0), NULLIF(tgt."REV_PLAN", 0), NULLIF(tgt."HO_PLAN", 0), NULLIF(tgt."REG_PLAN", 0), NULLIF(tgt."AREA_PLAN", 0), 0) AS Target_Plan
        FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" tgt
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk
            ON LTRIM(TRIM(tgt."MATNR"), '0') = LTRIM(TRIM(sk."MATNR"), '0')
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat
            ON (COALESCE(NULLIF(TRIM(tgt."CAT_CODE"), ''), NULLIF(TRIM(sk."CAT_CODE"), '')) = TRIM(cat."CAT_CODE"))
        WHERE (CAST(tgt."YMONTH" AS VARCHAR) = '{year_month}' OR tgt."YMONTH" = '{year_month}')
    ),

    AGG_TARGET AS (
        SELECT
            Category,
            SUM(Target_Plan) AS Target_Qty
        FROM TARGET_CLASSIFIED
        GROUP BY Category
    ),

    ALL_CATS AS (
        SELECT Category FROM AGG_SALES
        UNION
        SELECT Category FROM AGG_TARGET
    )

    SELECT
        c.Category,
        COALESCE(s.Prev_Act, 0) AS Prev_Act,
        COALESCE(t.Target_Qty, 0) AS Target_Qty,
        COALESCE(s.Repl_Total, 0) AS Repl_Total,
        COALESCE(s.Repl_Today, 0) AS Repl_Today,
        COALESCE(s.Repl_Dealer, 0) AS Repl_Dealer,
        COALESCE(s.Repl_Dist, 0) AS Repl_Dist,
        COALESCE(s.Repl_Fleet, 0) AS Repl_Fleet,
        COALESCE(s.OEM_Actual, 0) AS OEM_Actual,
        COALESCE(s.STU_Actual, 0) AS STU_Actual,
        COALESCE(s.DEF_Actual, 0) AS DEF_Actual
    FROM ALL_CATS c
    LEFT JOIN AGG_SALES s ON c.Category = s.Category
    LEFT JOIN AGG_TARGET t ON c.Category = t.Category
    ORDER BY c.Category ASC
    """

    sanitize_and_execute_hana(cursor, schema, hana_sql)
    raw_data = cursor.fetchall()

    results = []
    tot_prev_act = 0.0
    tot_target = 0.0
    tot_repl_total = 0.0
    tot_today = 0.0
    tot_dealer = 0.0
    tot_dist = 0.0
    tot_fleet = 0.0
    tot_others = 0.0
    tot_oem_actual = 0.0
    tot_stu_actual = 0.0
    tot_def_actual = 0.0
    tot_dom_target = 0.0
    tot_dom_actual = 0.0

    sort_idx = 1
    for r in raw_data:
        cat_name = str(r[0]).strip() if r[0] is not None else "Unknown"
        if not cat_name or cat_name.lower() == 'none':
            cat_name = "Unknown - Unknown"
        cat_name = normalize_category_name(cat_name)

        p_act = round(float(r[1] or 0), 2)
        tgt = round(float(r[2] or 0), 2)
        r_tot = round(float(r[3] or 0), 2)
        today = round(float(r[4] or 0), 2)
        dealer = round(float(r[5] or 0), 2)
        dist = round(float(r[6] or 0), 2)
        fleet = round(float(r[7] or 0), 2)
        others = round(max(0.0, r_tot - dealer - dist - fleet), 2)

        oem_tgt = 0.0
        oem_act = round(float(r[8] or 0), 2)

        stu_tgt = 0.0
        stu_act = round(float(r[9] or 0), 2)

        def_tgt = 0.0
        def_act = round(float(r[10] or 0), 2)

        dom_tgt = round(tgt + oem_tgt + stu_tgt + def_tgt, 2)
        dom_act = round(r_tot + oem_act + stu_act + def_act, 2)

        achv_pct = round((r_tot * 100.0 / tgt), 2) if tgt > 0 else 0.0
        dom_achv_pct = round((dom_act * 100.0 / dom_tgt), 2) if dom_tgt > 0 else 0.0

        tot_prev_act += p_act
        tot_target += tgt
        tot_repl_total += r_tot
        tot_today += today
        tot_dealer += dealer
        tot_dist += dist
        tot_fleet += fleet
        tot_others += others
        tot_oem_actual += oem_act
        tot_stu_actual += stu_act
        tot_def_actual += def_act
        tot_dom_target += dom_tgt
        tot_dom_actual += dom_act

        results.append({
            "category": cat_name,
            "prev_year_act": p_act,
            "target": tgt,
            "total": r_tot,
            "achievement_pct": achv_pct,
            "today": today,
            "dealer": dealer,
            "distributor": dist,
            "fleet": fleet,
            "others": others,
            "oem_target": oem_tgt,
            "oem_actual": oem_act,
            "stu_target": stu_tgt,
            "stu_actual": stu_act,
            "def_target": def_tgt,
            "def_actual": def_act,
            "domestic_target": dom_tgt,
            "domestic_actual": dom_act,
            "domestic_achv_pct": dom_achv_pct,
            "sort_order": sort_idx,
            "is_total": False
        })
        sort_idx += 1

    # Grand Total Row
    tot_prev_act = round(tot_prev_act, 2)
    tot_target = round(tot_target, 2)
    tot_repl_total = round(tot_repl_total, 2)
    tot_today = round(tot_today, 2)
    tot_dealer = round(tot_dealer, 2)
    tot_dist = round(tot_dist, 2)
    tot_fleet = round(tot_fleet, 2)
    tot_others = round(tot_others, 2)
    tot_oem_actual = round(tot_oem_actual, 2)
    tot_stu_actual = round(tot_stu_actual, 2)
    tot_def_actual = round(tot_def_actual, 2)
    tot_dom_target = round(tot_dom_target, 2)
    tot_dom_actual = round(tot_dom_actual, 2)

    tot_achv_pct = round((tot_repl_total * 100.0 / tot_target), 2) if tot_target > 0 else 0.0
    tot_dom_achv_pct = round((tot_dom_actual * 100.0 / tot_dom_target), 2) if tot_dom_target > 0 else 0.0

    results.append({
        "category": "Total",
        "prev_year_act": tot_prev_act,
        "target": tot_target,
        "total": tot_repl_total,
        "achievement_pct": tot_achv_pct,
        "today": tot_today,
        "dealer": tot_dealer,
        "distributor": tot_dist,
        "fleet": tot_fleet,
        "others": tot_others,
        "oem_target": 0.0,
        "oem_actual": tot_oem_actual,
        "stu_target": 0.0,
        "stu_actual": tot_stu_actual,
        "def_target": 0.0,
        "def_actual": tot_def_actual,
        "domestic_target": tot_dom_target,
        "domestic_actual": tot_dom_actual,
        "domestic_achv_pct": tot_dom_achv_pct,
        "sort_order": 9999,
        "is_total": True
    })

    return results


def get_cached_data(mysql_conn, session_id: str, year_month: str):
    ensure_table(mysql_conn)
    try:
        cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
        cur.execute(f"""
            SELECT
                category,
                CAST(prev_year_act AS DOUBLE) AS prev_year_act,
                CAST(target AS DOUBLE) AS target,
                CAST(total AS DOUBLE) AS total,
                CAST(achievement_pct AS DOUBLE) AS achievement_pct,
                CAST(today AS DOUBLE) AS today,
                CAST(dealer AS DOUBLE) AS dealer,
                CAST(distributor AS DOUBLE) AS distributor,
                CAST(fleet AS DOUBLE) AS fleet,
                CAST(others AS DOUBLE) AS others,
                CAST(oem_target AS DOUBLE) AS oem_target,
                CAST(oem_actual AS DOUBLE) AS oem_actual,
                CAST(stu_target AS DOUBLE) AS stu_target,
                CAST(stu_actual AS DOUBLE) AS stu_actual,
                CAST(def_target AS DOUBLE) AS def_target,
                CAST(def_actual AS DOUBLE) AS def_actual,
                CAST(domestic_target AS DOUBLE) AS domestic_target,
                CAST(domestic_actual AS DOUBLE) AS domestic_actual,
                CAST(domestic_achv_pct AS DOUBLE) AS domestic_achv_pct,
                sort_order,
                is_total,
                last_synced_at
            FROM `{TABLE_NAME}`
            WHERE `session_id` = %s AND `year_month` = %s
            ORDER BY is_total ASC, sort_order ASC, category ASC
        """, (session_id, year_month))
        rows = cur.fetchall()
        cur.close()
        if rows and len(rows) > 0:
            for r in rows:
                r["is_total"] = bool(r.get("is_total"))
                r["category"] = normalize_category_name(r.get("category", ""))
            return rows
    except Exception as e:
        print(f"[{TABLE_NAME}] Cache fetch error: {e}", flush=True)
    return None


def save_data_to_table(mysql_conn, session_id: str, year_month: str, selected_date: str, rows: list):
    ensure_table(mysql_conn)
    if not rows:
        return
    try:
        cur = mysql_conn.cursor()
        cur.execute(f"DELETE FROM `{TABLE_NAME}` WHERE `session_id` = %s AND `year_month` = %s", (session_id, year_month))

        insert_sql = f"""
            INSERT INTO `{TABLE_NAME}` (
                `session_id`, `year_month`, `selected_date`, `category`,
                `prev_year_act`, `target`, `total`, `achievement_pct`, `today`,
                `dealer`, `distributor`, `fleet`, `others`,
                `oem_target`, `oem_actual`, `stu_target`, `stu_actual`,
                `def_target`, `def_actual`, `domestic_target`, `domestic_actual`, `domestic_achv_pct`,
                `sort_order`, `is_total`, `last_synced_at`
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, NOW()
            )
        """
        records = []
        for r in rows:
            records.append((
                session_id,
                year_month,
                selected_date,
                r.get("category", ""),
                r.get("prev_year_act", 0.0),
                r.get("target", 0.0),
                r.get("total", 0.0),
                r.get("achievement_pct", 0.0),
                r.get("today", 0.0),
                r.get("dealer", 0.0),
                r.get("distributor", 0.0),
                r.get("fleet", 0.0),
                r.get("others", 0.0),
                r.get("oem_target", 0.0),
                r.get("oem_actual", 0.0),
                r.get("stu_target", 0.0),
                r.get("stu_actual", 0.0),
                r.get("def_target", 0.0),
                r.get("def_actual", 0.0),
                r.get("domestic_target", 0.0),
                r.get("domestic_actual", 0.0),
                r.get("domestic_achv_pct", 0.0),
                r.get("sort_order", 99),
                1 if r.get("is_total") else 0
            ))
        cur.executemany(insert_sql, records)
        mysql_conn.commit()
        cur.close()
        print(f"[{TABLE_NAME}] Successfully cached {len(records)} rows for session={session_id}, ym={year_month}", flush=True)
    except Exception as e:
        print(f"[{TABLE_NAME}] Save error: {e}", flush=True)


def bulk_sync_sales_numbers_report(mysql_conn, session_id: str, user_id=None):
    """
    Pre-calculates and caches all available months permutations for Sales Number's (Sheet 2)
    into MySQL table `sales_summary_sales_numbers`.
    """
    if not mysql_conn or not session_id:
        return False

    ensure_table(mysql_conn)
    hana_conn = None
    tunnel = None
    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id=user_id)
        if not hana_conn:
            return False

        h_cur = hana_conn.cursor()
        detected_year, detected_month, detected_day = detect_sales_dates(h_cur, schema)
        master_months = fetch_master_months(h_cur, schema)

        sync_months = master_months if master_months else [{"month_no": detected_month}]
        print(f"[{TABLE_NAME}] Bulk syncing {len(sync_months)} months for session {session_id}...", flush=True)

        for m_item in sync_months:
            m_no = m_item["month_no"]
            ym = f"{detected_year}{str(m_no).zfill(2)}"
            if m_no == detected_month:
                sel_date = f"{detected_year}-{str(m_no).zfill(2)}-{str(detected_day).zfill(2)}"
            else:
                last_d = calendar.monthrange(detected_year, m_no)[1]
                sel_date = f"{detected_year}-{str(m_no).zfill(2)}-{str(last_d).zfill(2)}"

            try:
                rows = query_sales_numbers_hana(h_cur, schema, ym, sel_date)
                if rows:
                    save_data_to_table(mysql_conn, session_id, ym, sel_date, rows)
            except Exception as e:
                print(f"[{TABLE_NAME}] Sync error for month {ym}: {e}", flush=True)
                try:
                    log_system_error(session_id=session_id, module_name=TABLE_NAME, error_msg=f"Sync error for month {ym}: {e}")
                except Exception:
                    pass

        h_cur.close()
        return True
    except Exception as e:
        print(f"[{TABLE_NAME}] Bulk sync failed: {e}", flush=True)
        try:
            log_system_error(session_id=session_id, module_name=TABLE_NAME, error_msg=f"Bulk sync failed: {e}")
        except Exception:
            pass
        return False
    finally:
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass


def get_sales_numbers_report_controller(get_db_connection):
    """
    Main controller for /sales-numbers-report API endpoint (Sheet 2).
    """
    session_id_arg = request.args.get("session_id")
    user_id_arg = request.args.get("user_id")
    param_month = request.args.get("month")
    param_ym = request.args.get("year_month") or request.args.get("ym")
    force_refresh = request.args.get("force_refresh", "false").lower() in ("true", "1")

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = _get_connection_context(
        get_db_connection, session_id_arg, user_id_arg
    )

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Failed to connect to database"}), 500

    try:
        ensure_table(mysql_conn)

        current_year = datetime.now().year
        detected_month = datetime.now().month
        detected_day = datetime.now().day
        selected_date = None
        master_months = []

        if is_hana:
            try:
                h_cur = hana_conn.cursor()
                detected_year, detected_month, detected_day = detect_sales_dates(h_cur, schema)
                current_year = detected_year
                master_months = fetch_master_months(h_cur, schema)
                h_cur.close()
            except Exception as e:
                print(f"[{TABLE_NAME}] HANA date detection error: {e}", flush=True)

        previous_year = current_year - 1

        selected_mo_num = None
        if param_month:
            try:
                clean_m = str(param_month).strip().upper()
                if clean_m.isdigit():
                    selected_mo_num = int(clean_m)
                else:
                    found = next((m for m in master_months if m["month_name"].upper() == clean_m or m["month_name"].upper().startswith(clean_m[:3])), None)
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
                hana_res = query_sales_numbers_hana(h_cur, schema, year_month, selected_date)
                h_cur.close()
                if hana_res and len(hana_res) > 0:
                    rows = hana_res
                    save_data_to_table(mysql_conn, effective_session, year_month, selected_date, rows)
            except Exception as e:
                print(f"[{TABLE_NAME}] HANA calculation error: {e}", flush=True)

        if not rows and cached_rows:
            rows = cached_rows

        mo_abbr = calendar.month_abbr[selected_mo_num]
        prev_yr_short = str(previous_year)[-2:]
        curr_yr_short = str(current_year)[-2:]

        meta_info = {
            "month_abbr": mo_abbr,
            "prev_year_label": f"{mo_abbr}-{prev_yr_short} Act",
            "curr_year_label": f"Actual Sale for {mo_abbr}-{curr_yr_short}",
            "day_label": f"Sale for day"
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

