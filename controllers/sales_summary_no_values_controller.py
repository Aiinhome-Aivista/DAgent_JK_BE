"""
Dedicated Controller for Sales Summary in No's & Values (Sheet 4).
Manages dedicated cache table: `sales_summary_no_values`
Calculates:
- For each Zone (East, North, Central, West, South - I, South - II, Nepal):
  3 rows: Actual, Target, and Achv %
- Values in Crores: 4 Whlrs., 2/3 Whlrs, Total
- Quantities in Tyre Units:
  TBB Total, TBR Total, LCV Bias, LCV Rdl, SCV Bias, SCV Rdl,
  Car Bias, Car Radial, Jeep Bias, Jeep Radial, Tr. Fro, Tr. Rear,
  Tr. Trail, ADV, 3 Whlrs, Scooter, Motor
- Grand Total summary row set (Actual, Target, Achv %)
- Strict channel 10 (Replacement), active sales filter, and official Datasphere joins.
"""

import calendar
from datetime import datetime
from flask import request, jsonify
from helper.sap_connection import get_sap_connection_for_session, sanitize_and_execute_hana, detect_sales_dates

TABLE_NAME = "sales_summary_no_values"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `year_month` VARCHAR(10) NOT NULL,
    `selected_date` DATE NOT NULL,
    `zone_name` VARCHAR(60) NOT NULL,
    `row_type` VARCHAR(20) NOT NULL,
    `val_4w` DECIMAL(18,2) DEFAULT 0,
    `val_23w` DECIMAL(18,2) DEFAULT 0,
    `val_total` DECIMAL(18,2) DEFAULT 0,
    `tbb` DECIMAL(18,2) DEFAULT 0,
    `tbr` DECIMAL(18,2) DEFAULT 0,
    `lcv_b` DECIMAL(18,2) DEFAULT 0,
    `lcv_r` DECIMAL(18,2) DEFAULT 0,
    `scv_b` DECIMAL(18,2) DEFAULT 0,
    `scv_r` DECIMAL(18,2) DEFAULT 0,
    `car_b` DECIMAL(18,2) DEFAULT 0,
    `car_r` DECIMAL(18,2) DEFAULT 0,
    `jeep_b` DECIMAL(18,2) DEFAULT 0,
    `jeep_r` DECIMAL(18,2) DEFAULT 0,
    `tr_fro` DECIMAL(18,2) DEFAULT 0,
    `tr_rear` DECIMAL(18,2) DEFAULT 0,
    `tr_trail` DECIMAL(18,2) DEFAULT 0,
    `adv` DECIMAL(18,2) DEFAULT 0,
    `three_whlrs` DECIMAL(18,2) DEFAULT 0,
    `scooter` DECIMAL(18,2) DEFAULT 0,
    `motor` DECIMAL(18,2) DEFAULT 0,
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


def query_sales_summary_no_values_hana(cursor, schema, year_month: str, selected_date: str):
    """
    Executes memory-optimized HANA query for Sales Summary in No's & Values (Sheet 4).
    Calculates Actuals and Targets grouped by Zone across:
    - Values (in Crores): 4 Whlrs., 2/3 Whlrs, Total
    - Quantities (in Units): TBB Total, TBR Total, LCV Bias, LCV Rdl, SCV Bias, SCV Rdl,
      Car Bias, Car Radial, Jeep Bias, Jeep Radial, Tr. Fro, Tr. Rear, Tr. Trail,
      ADV, 3 Whlrs, Scooter, Motor
    """
    curr_yr = int(year_month[:4])
    curr_mo = int(year_month[4:6])

    last_day_curr = calendar.monthrange(curr_yr, curr_mo)[1]
    try:
        sel_d = int(str(selected_date).split("-")[2])
    except Exception:
        sel_d = last_day_curr

    day_clamped = min(sel_d, last_day_curr)
    curr_from = f"{curr_yr}-{str(curr_mo).zfill(2)}-01"
    curr_to = f"{curr_yr}-{str(curr_mo).zfill(2)}-{str(day_clamped).zfill(2)}"

    hana_sql = f"""
    WITH
    SALES_BASE AS (
        SELECT
            "invoice_date",
            COALESCE(NULLIF("invoice_value", 0), NULLIF("taxable_value", 0), 0) AS "invoice_value",
            COALESCE(NULLIF("Tyre_Quantity", 0), NULLIF("volume", 0), 0) AS "qty",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "invoice_number",
            "plant",
            "FKART",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
        WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
          AND "invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD')
          AND "invoice_date" <= TO_DATE('{curr_to}', 'YYYY-MM-DD')
          AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
        UNION ALL
        SELECT
            "invoice_date",
            COALESCE(NULLIF("invoice_value", 0), NULLIF("taxable_value", 0), 0) AS "invoice_value",
            COALESCE(NULLIF("Tyre_Quantity", 0), NULLIF("volume", 0), 0) AS "qty",
            "distribution_channel",
            "dealer_code",
            "SKU",
            "invoice_number",
            "plant",
            "FKART",
            "indicator_cancel"
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
        WHERE ("distribution_channel" = '10' OR "distribution_channel" = 10)
          AND "invoice_date" >= TO_DATE('{curr_from}', 'YYYY-MM-DD')
          AND "invoice_date" <= TO_DATE('{curr_to}', 'YYYY-MM-DD')
          AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
    ),

    SALES_FILTERED AS (
        SELECT
            s."invoice_date",
            s."invoice_value",
            s."qty",
            s."dealer_code",
            s."SKU"
        FROM SALES_BASE s
        WHERE NOT EXISTS (
            SELECT 1
            FROM "{schema}"."ZPVF_SD_CancelledSalesInvoice_01_TR" x
            WHERE x."invoice_number" = s."invoice_number"
              AND x."plant" = s."plant"
              AND x."FKART" = s."FKART"
              AND x."indicator_cancel" = 'X'
        )
    ),

    FACT_CLASSIFIED AS (
        SELECT
            zm."NAME" AS Zone_Name,
            zm."P_ZONE" AS Zone_Code,
            s."invoice_value",
            s."qty",
            TRIM(COALESCE(cat."CAT_CODE", '')) AS cat_code,
            UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) AS cat_desc,
            UPPER(TRIM(COALESCE(sk."TYRE_TYPE", ''))) AS tyre_type,
            UPPER(TRIM(COALESCE(sk."CONSTUCTION", ''))) AS constr
        FROM SALES_FILTERED s
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk 
            ON TRIM(s."SKU") = TRIM(sk."MATNR")
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat 
            ON TRIM(sk."CAT_CODE") = TRIM(cat."CAT_CODE")
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
            ON (LTRIM(TRIM(s."dealer_code"), '0') = LTRIM(TRIM(cm."KUNNR"), '0') OR LPAD(TRIM(s."dealer_code"), 10, '0') = LPAD(TRIM(cm."KUNNR"), 10, '0'))
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm 
            ON TRIM(cm."BZIRK") = TRIM(tm."BZIRK")
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
            ON TRIM(tm."REGIO") = TRIM(rm."region_code")
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm 
            ON zm."P_ZONE" = COALESCE(NULLIF(TRIM(rm."zone_code"), ''), NULLIF(TRIM(tm."P_ZONE"), ''))
        WHERE (cm."KLABC" IS NULL OR UPPER(TRIM(cm."KLABC")) <> 'X')
          AND zm."NAME" IS NOT NULL AND TRIM(zm."NAME") != '' AND UPPER(TRIM(zm."NAME")) NOT LIKE '%UNKNOWN%'
    ),

    AGG_ACTUALS AS (
        SELECT
            Zone_Name,
            Zone_Code,
            SUM(CASE WHEN (cat_code IN ('B1', 'H1', 'S1') OR cat_desc IN ('MOTOR', 'SCOOTER', '3WH', '3W', 'MOTORCYCLE') OR tyre_type IN ('SCOOTER', 'MOTOR CYCLE', '3W')) THEN 0 ELSE "invoice_value" END) / 10000000.0 AS Act_Val_4W,
            SUM(CASE WHEN (cat_code IN ('B1', 'H1', 'S1') OR cat_desc IN ('MOTOR', 'SCOOTER', '3WH', '3W', 'MOTORCYCLE') OR tyre_type IN ('SCOOTER', 'MOTOR CYCLE', '3W')) THEN "invoice_value" ELSE 0 END) / 10000000.0 AS Act_Val_23W,
            -- Quantities
            SUM(CASE WHEN cat_code = '11' OR cat_desc = 'TBB' OR (tyre_type IN ('TRUCK', 'TRUCK/BUS', 'T&B') AND constr IN ('BIAS', '1', 1)) THEN "qty" ELSE 0 END) AS Act_TBB,
            SUM(CASE WHEN cat_code = '12' OR cat_desc = 'TBR' OR (tyre_type IN ('TRUCK', 'TRUCK/BUS', 'T&B') AND constr IN ('RADIAL', '2', '5', 2, 5)) THEN "qty" ELSE 0 END) AS Act_TBR,
            SUM(CASE WHEN cat_code = '21' OR cat_desc = 'LCB' OR (tyre_type = 'LCV' AND constr IN ('BIAS', '1', 1)) THEN "qty" ELSE 0 END) AS Act_LCV_B,
            SUM(CASE WHEN cat_code IN ('22', '25') OR cat_desc IN ('LCR(S)', 'LCR(N)') OR (tyre_type = 'LCV' AND constr IN ('RADIAL', '2', '5', 2, 5)) THEN "qty" ELSE 0 END) AS Act_LCV_R,
            SUM(CASE WHEN cat_code = 'D1' OR cat_desc = 'SCB' OR (tyre_type = 'SCV' AND constr IN ('BIAS', '1', 1)) THEN "qty" ELSE 0 END) AS Act_SCV_B,
            SUM(CASE WHEN cat_code = 'D2' OR cat_desc = 'SCR' OR (tyre_type = 'SCV' AND constr IN ('RADIAL', '2', '5', 2, 5)) THEN "qty" ELSE 0 END) AS Act_SCV_R,
            SUM(CASE WHEN cat_code = '31' OR cat_desc = 'CB' OR (tyre_type IN ('CAR', 'PASSENGER CAR', 'PCR') AND constr IN ('BIAS', '1', 1)) THEN "qty" ELSE 0 END) AS Act_Car_B,
            SUM(CASE WHEN cat_code = '32' OR cat_desc = 'CR' OR (tyre_type IN ('CAR', 'PASSENGER CAR', 'PCR') AND constr IN ('RADIAL', '2', '5', 2, 5)) THEN "qty" ELSE 0 END) AS Act_Car_R,
            SUM(CASE WHEN cat_code = '41' OR cat_desc = 'JB' OR (tyre_type IN ('JEEP', 'SUV', 'MUV') AND constr IN ('BIAS', '1', 1)) THEN "qty" ELSE 0 END) AS Act_Jeep_B,
            SUM(CASE WHEN cat_code = '42' OR cat_desc = 'JR' OR (tyre_type IN ('JEEP', 'SUV', 'MUV') AND constr IN ('RADIAL', '2', '5', 2, 5)) THEN "qty" ELSE 0 END) AS Act_Jeep_R,
            SUM(CASE WHEN cat_code = '61' OR cat_desc = 'TR.FRONT' OR tyre_type IN ('TRACTOR FRONT', 'TR. FRONT', 'TF') THEN "qty" ELSE 0 END) AS Act_TrF,
            SUM(CASE WHEN cat_code = '51' OR cat_desc = 'TR.REAR' OR tyre_type IN ('TRACTOR REAR', 'TR. REAR', 'TR') THEN "qty" ELSE 0 END) AS Act_TrR,
            SUM(CASE WHEN cat_code = '71' OR cat_desc = 'TR.TRAILER' OR tyre_type IN ('TRACTOR TRAILER', 'TR. TRAIL', 'TT') THEN "qty" ELSE 0 END) AS Act_TrT,
            SUM(CASE WHEN cat_code = '91' OR cat_desc = 'ADV' OR tyre_type = 'ADV' THEN "qty" ELSE 0 END) AS Act_ADV,
            SUM(CASE WHEN cat_code = 'H1' OR cat_desc = '3WH' OR tyre_type IN ('3W', '3 WHEELER', 'THREE WHEELER') THEN "qty" ELSE 0 END) AS Act_3W,
            SUM(CASE WHEN cat_code = 'S1' OR cat_desc = 'SCOOTER' OR tyre_type IN ('SCOOTER', 'SCOOTY') THEN "qty" ELSE 0 END) AS Act_Scoot,
            SUM(CASE WHEN cat_code = 'B1' OR cat_desc = 'MOTOR' OR tyre_type IN ('MOTOR CYCLE', 'MOTORCYCLE', 'MC') THEN "qty" ELSE 0 END) AS Act_Motor
        FROM FACT_CLASSIFIED
        GROUP BY Zone_Name, Zone_Code
    ),

    AGG_TARGETS AS (
        SELECT
            zm."NAME" AS Zone_Name,
            zm."P_ZONE" AS Zone_Code,
            -- Values
            SUM(CASE WHEN (TRIM(tgt."CAT_CODE") IN ('B1', 'H1', 'S1') 
                       OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('MOTOR', 'SCOOTER', '3WH', '3W', 'MOTORCYCLE') 
                       OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', '2/3 WHEELERS')) 
                     THEN 0 
                     ELSE COALESCE(NULLIF(tgt."REV_PLAN_VAL", 0), NULLIF(tgt."PLAN_VALUE", 0), 0) 
                END) / 10000000.0 AS Tgt_Val_4W,
            SUM(CASE WHEN (TRIM(tgt."CAT_CODE") IN ('B1', 'H1', 'S1') 
                       OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('MOTOR', 'SCOOTER', '3WH', '3W', 'MOTORCYCLE') 
                       OR UPPER(TRIM(COALESCE(tgt."T_TYPE", ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', '2/3 WHEELERS')) 
                     THEN COALESCE(NULLIF(tgt."REV_PLAN_VAL", 0), NULLIF(tgt."PLAN_VALUE", 0), 0) 
                     ELSE 0 
                END) / 10000000.0 AS Tgt_Val_23W,
            -- Quantities
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '11' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'TBB' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_TBB,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '12' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'TBR' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_TBR,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '21' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'LCB' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_LCV_B,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") IN ('22', '25') OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) IN ('LCR(S)', 'LCR(N)') THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_LCV_R,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = 'D1' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'SCB' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_SCV_B,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = 'D2' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'SCR' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_SCV_R,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '31' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'CB' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_Car_B,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '32' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'CR' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_Car_R,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '41' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'JB' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_Jeep_B,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '42' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'JR' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_Jeep_R,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '61' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'TR.FRONT' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_TrF,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '51' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'TR.REAR' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_TrR,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '71' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'TR.TRAILER' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_TrT,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = '91' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'ADV' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_ADV,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = 'H1' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = '3WH' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_3W,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = 'S1' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'SCOOTER' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_Scoot,
            SUM(CASE WHEN TRIM(tgt."CAT_CODE") = 'B1' OR UPPER(TRIM(COALESCE(cat."CAT_DESC", ''))) = 'MOTOR' THEN COALESCE(NULLIF(tgt."REV_PLAN", 0), tgt."ORG_PLAN", 0) ELSE 0 END) AS Tgt_Motor
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
        -- Actuals
        COALESCE(a.Act_Val_4W,  0) AS Act_Val_4W,
        COALESCE(a.Act_Val_23W, 0) AS Act_Val_23W,
        COALESCE(a.Act_TBB,     0) AS Act_TBB,
        COALESCE(a.Act_TBR,     0) AS Act_TBR,
        COALESCE(a.Act_LCV_B,   0) AS Act_LCV_B,
        COALESCE(a.Act_LCV_R,   0) AS Act_LCV_R,
        COALESCE(a.Act_SCV_B,   0) AS Act_SCV_B,
        COALESCE(a.Act_SCV_R,   0) AS Act_SCV_R,
        COALESCE(a.Act_Car_B,   0) AS Act_Car_B,
        COALESCE(a.Act_Car_R,   0) AS Act_Car_R,
        COALESCE(a.Act_Jeep_B,  0) AS Act_Jeep_B,
        COALESCE(a.Act_Jeep_R,  0) AS Act_Jeep_R,
        COALESCE(a.Act_TrF,     0) AS Act_TrF,
        COALESCE(a.Act_TrR,     0) AS Act_TrR,
        COALESCE(a.Act_TrT,     0) AS Act_TrT,
        COALESCE(a.Act_ADV,     0) AS Act_ADV,
        COALESCE(a.Act_3W,      0) AS Act_3W,
        COALESCE(a.Act_Scoot,   0) AS Act_Scoot,
        COALESCE(a.Act_Motor,   0) AS Act_Motor,
        -- Targets
        COALESCE(t.Tgt_Val_4W,  0) AS Tgt_Val_4W,
        COALESCE(t.Tgt_Val_23W, 0) AS Tgt_Val_23W,
        COALESCE(t.Tgt_TBB,     0) AS Tgt_TBB,
        COALESCE(t.Tgt_TBR,     0) AS Tgt_TBR,
        COALESCE(t.Tgt_LCV_B,   0) AS Tgt_LCV_B,
        COALESCE(t.Tgt_LCV_R,   0) AS Tgt_LCV_R,
        COALESCE(t.Tgt_SCV_B,   0) AS Tgt_SCV_B,
        COALESCE(t.Tgt_SCV_R,   0) AS Tgt_SCV_R,
        COALESCE(t.Tgt_Car_B,   0) AS Tgt_Car_B,
        COALESCE(t.Tgt_Car_R,   0) AS Tgt_Car_R,
        COALESCE(t.Tgt_Jeep_B,  0) AS Tgt_Jeep_B,
        COALESCE(t.Tgt_Jeep_R,  0) AS Tgt_Jeep_R,
        COALESCE(t.Tgt_TrF,     0) AS Tgt_TrF,
        COALESCE(t.Tgt_TrR,     0) AS Tgt_TrR,
        COALESCE(t.Tgt_TrT,     0) AS Tgt_TrT,
        COALESCE(t.Tgt_ADV,     0) AS Tgt_ADV,
        COALESCE(t.Tgt_3W,      0) AS Tgt_3W,
        COALESCE(t.Tgt_Scoot,   0) AS Tgt_Scoot,
        COALESCE(t.Tgt_Motor,   0) AS Tgt_Motor
    FROM ALL_ZONES z
    LEFT JOIN AGG_ACTUALS a ON (z.Zone_Code = a.Zone_Code OR z.Zone_Name = a.Zone_Name)
    LEFT JOIN AGG_TARGETS t ON (z.Zone_Code = t.Zone_Code OR z.Zone_Name = t.Zone_Name)
    ORDER BY z.Sort_Order ASC
    """

    sanitize_and_execute_hana(cursor, schema, hana_sql)
    raw_rows = cursor.fetchall()

    # Dictionary to collect aggregated zone data
    zone_dict = {}
    standard_zones = [
        ("East", 1),
        ("North", 2),
        ("Central", 3),
        ("West", 4),
        ("South - I", 5),
        ("South - II", 6),
        ("Nepal", 7)
    ]

    for sz_name, sz_order in standard_zones:
        zone_dict[sz_name] = {
            "name": sz_name,
            "order": sz_order,
            "act_v4w": 0.0, "act_v23w": 0.0,
            "act_tbb": 0.0, "act_tbr": 0.0, "act_lcv_b": 0.0, "act_lcv_r": 0.0,
            "act_scv_b": 0.0, "act_scv_r": 0.0, "act_car_b": 0.0, "act_car_r": 0.0,
            "act_jeep_b": 0.0, "act_jeep_r": 0.0, "act_tr_f": 0.0, "act_tr_r": 0.0,
            "act_tr_t": 0.0, "act_adv": 0.0, "act_3w": 0.0, "act_scoot": 0.0, "act_motor": 0.0,
            "tgt_v4w": 0.0, "tgt_v23w": 0.0,
            "tgt_tbb": 0.0, "tgt_tbr": 0.0, "tgt_lcv_b": 0.0, "tgt_lcv_r": 0.0,
            "tgt_scv_b": 0.0, "tgt_scv_r": 0.0, "tgt_car_b": 0.0, "tgt_car_r": 0.0,
            "tgt_jeep_b": 0.0, "tgt_jeep_r": 0.0, "tgt_tr_f": 0.0, "tgt_tr_r": 0.0,
            "tgt_tr_t": 0.0, "tgt_adv": 0.0, "tgt_3w": 0.0, "tgt_scoot": 0.0, "tgt_motor": 0.0,
        }

    for r in raw_rows:
        z_name, s_order = _clean_zone(r[0], r[1])
        if "UNKNOWN" in z_name.upper():
            continue
        if z_name not in zone_dict:
            zone_dict[z_name] = {
                "name": z_name,
                "order": s_order,
                "act_v4w": 0.0, "act_v23w": 0.0,
                "act_tbb": 0.0, "act_tbr": 0.0, "act_lcv_b": 0.0, "act_lcv_r": 0.0,
                "act_scv_b": 0.0, "act_scv_r": 0.0, "act_car_b": 0.0, "act_car_r": 0.0,
                "act_jeep_b": 0.0, "act_jeep_r": 0.0, "act_tr_f": 0.0, "act_tr_r": 0.0,
                "act_tr_t": 0.0, "act_adv": 0.0, "act_3w": 0.0, "act_scoot": 0.0, "act_motor": 0.0,
                "tgt_v4w": 0.0, "tgt_v23w": 0.0,
                "tgt_tbb": 0.0, "tgt_tbr": 0.0, "tgt_lcv_b": 0.0, "tgt_lcv_r": 0.0,
                "tgt_scv_b": 0.0, "tgt_scv_r": 0.0, "tgt_car_b": 0.0, "tgt_car_r": 0.0,
                "tgt_jeep_b": 0.0, "tgt_jeep_r": 0.0, "tgt_tr_f": 0.0, "tgt_tr_r": 0.0,
                "tgt_tr_t": 0.0, "tgt_adv": 0.0, "tgt_3w": 0.0, "tgt_scoot": 0.0, "tgt_motor": 0.0,
            }

        zd = zone_dict[z_name]
        zd["act_v4w"]   += float(r[3] or 0)
        zd["act_v23w"]  += float(r[4] or 0)
        zd["act_tbb"]   += float(r[5] or 0)
        zd["act_tbr"]   += float(r[6] or 0)
        zd["act_lcv_b"] += float(r[7] or 0)
        zd["act_lcv_r"] += float(r[8] or 0)
        zd["act_scv_b"] += float(r[9] or 0)
        zd["act_scv_r"] += float(r[10] or 0)
        zd["act_car_b"] += float(r[11] or 0)
        zd["act_car_r"] += float(r[12] or 0)
        zd["act_jeep_b"]+= float(r[13] or 0)
        zd["act_jeep_r"]+= float(r[14] or 0)
        zd["act_tr_f"]  += float(r[15] or 0)
        zd["act_tr_r"]  += float(r[16] or 0)
        zd["act_tr_t"]  += float(r[17] or 0)
        zd["act_adv"]   += float(r[18] or 0)
        zd["act_3w"]    += float(r[19] or 0)
        zd["act_scoot"] += float(r[20] or 0)
        zd["act_motor"] += float(r[21] or 0)

        zd["tgt_v4w"]   += float(r[22] or 0)
        zd["tgt_v23w"]  += float(r[23] or 0)
        zd["tgt_tbb"]   += float(r[24] or 0)
        zd["tgt_tbr"]   += float(r[25] or 0)
        zd["tgt_lcv_b"] += float(r[26] or 0)
        zd["tgt_lcv_r"] += float(r[27] or 0)
        zd["tgt_scv_b"] += float(r[28] or 0)
        zd["tgt_scv_r"] += float(r[29] or 0)
        zd["tgt_car_b"] += float(r[30] or 0)
        zd["tgt_car_r"] += float(r[31] or 0)
        zd["tgt_jeep_b"]+= float(r[32] or 0)
        zd["tgt_jeep_r"]+= float(r[33] or 0)
        zd["tgt_tr_f"]  += float(r[34] or 0)
        zd["tgt_tr_r"]  += float(r[35] or 0)
        zd["tgt_tr_t"]  += float(r[36] or 0)
        zd["tgt_adv"]   += float(r[37] or 0)
        zd["tgt_3w"]    += float(r[38] or 0)
        zd["tgt_scoot"] += float(r[39] or 0)
        zd["tgt_motor"] += float(r[40] or 0)

    # Build final rows (3 rows per zone, plus Grand Total)
    results = []

    # Grand total accumulator
    gt_act = {
        "v4w": 0.0, "v23w": 0.0,
        "tbb": 0.0, "tbr": 0.0, "lcv_b": 0.0, "lcv_r": 0.0, "scv_b": 0.0, "scv_r": 0.0,
        "car_b": 0.0, "car_r": 0.0, "jeep_b": 0.0, "jeep_r": 0.0, "tr_f": 0.0, "tr_r": 0.0,
        "tr_t": 0.0, "adv": 0.0, "3w": 0.0, "scoot": 0.0, "motor": 0.0
    }
    gt_tgt = {
        "v4w": 0.0, "v23w": 0.0,
        "tbb": 0.0, "tbr": 0.0, "lcv_b": 0.0, "lcv_r": 0.0, "scv_b": 0.0, "scv_r": 0.0,
        "car_b": 0.0, "car_r": 0.0, "jeep_b": 0.0, "jeep_r": 0.0, "tr_f": 0.0, "tr_r": 0.0,
        "tr_t": 0.0, "adv": 0.0, "3w": 0.0, "scoot": 0.0, "motor": 0.0
    }

    sorted_zones = sorted(zone_dict.values(), key=lambda x: x["order"])

    for zd in sorted_zones:
        z_name = zd["name"]
        s_order = zd["order"]

        # Actual Row
        a_v4w = round(zd["act_v4w"], 2)
        a_v23w = round(zd["act_v23w"], 2)
        a_vtot = round(a_v4w + a_v23w, 2)

        a_tbb   = int(round(zd["act_tbb"]))
        a_tbr   = int(round(zd["act_tbr"]))
        a_lcv_b = int(round(zd["act_lcv_b"]))
        a_lcv_r = int(round(zd["act_lcv_r"]))
        a_scv_b = int(round(zd["act_scv_b"]))
        a_scv_r = int(round(zd["act_scv_r"]))
        a_car_b = int(round(zd["act_car_b"]))
        a_car_r = int(round(zd["act_car_r"]))
        a_jeep_b= int(round(zd["act_jeep_b"]))
        a_jeep_r= int(round(zd["act_jeep_r"]))
        a_tr_f  = int(round(zd["act_tr_f"]))
        a_tr_r  = int(round(zd["act_tr_r"]))
        a_tr_t  = int(round(zd["act_tr_t"]))
        a_adv   = int(round(zd["act_adv"]))
        a_3w    = int(round(zd["act_3w"]))
        a_scoot = int(round(zd["act_scoot"]))
        a_motor = int(round(zd["act_motor"]))

        # Target Row
        t_v4w = round(zd["tgt_v4w"], 2)
        t_v23w = round(zd["tgt_v23w"], 2)
        t_vtot = round(t_v4w + t_v23w, 2)

        t_tbb   = int(round(zd["tgt_tbb"]))
        t_tbr   = int(round(zd["tgt_tbr"]))
        t_lcv_b = int(round(zd["tgt_lcv_b"]))
        t_lcv_r = int(round(zd["tgt_lcv_r"]))
        t_scv_b = int(round(zd["tgt_scv_b"]))
        t_scv_r = int(round(zd["tgt_scv_r"]))
        t_car_b = int(round(zd["tgt_car_b"]))
        t_car_r = int(round(zd["tgt_car_r"]))
        t_jeep_b= int(round(zd["tgt_jeep_b"]))
        t_jeep_r= int(round(zd["tgt_jeep_r"]))
        t_tr_f  = int(round(zd["tgt_tr_f"]))
        t_tr_r  = int(round(zd["tgt_tr_r"]))
        t_tr_t  = int(round(zd["tgt_tr_t"]))
        t_adv   = int(round(zd["tgt_adv"]))
        t_3w    = int(round(zd["tgt_3w"]))
        t_scoot = int(round(zd["tgt_scoot"]))
        t_motor = int(round(zd["tgt_motor"]))

        # Achv % calculation helper
        def calc_achv(act, tgt):
            return round((act * 100.0 / tgt), 2) if tgt > 0 else 0.0

        p_v4w = calc_achv(a_v4w, t_v4w)
        p_v23w = calc_achv(a_v23w, t_v23w)
        p_vtot = calc_achv(a_vtot, t_vtot)

        p_tbb   = calc_achv(a_tbb, t_tbb)
        p_tbr   = calc_achv(a_tbr, t_tbr)
        p_lcv_b = calc_achv(a_lcv_b, t_lcv_b)
        p_lcv_r = calc_achv(a_lcv_r, t_lcv_r)
        p_scv_b = calc_achv(a_scv_b, t_scv_b)
        p_scv_r = calc_achv(a_scv_r, t_scv_r)
        p_car_b = calc_achv(a_car_b, t_car_b)
        p_car_r = calc_achv(a_car_r, t_car_r)
        p_jeep_b= calc_achv(a_jeep_b, t_jeep_b)
        p_jeep_r= calc_achv(a_jeep_r, t_jeep_r)
        p_tr_f  = calc_achv(a_tr_f, t_tr_f)
        p_tr_r  = calc_achv(a_tr_r, t_tr_r)
        p_tr_t  = calc_achv(a_tr_t, t_tr_t)
        p_adv   = calc_achv(a_adv, t_adv)
        p_3w    = calc_achv(a_3w, t_3w)
        p_scoot = calc_achv(a_scoot, t_scoot)
        p_motor = calc_achv(a_motor, t_motor)

        # Accumulate Grand Total
        gt_act["v4w"] += a_v4w
        gt_act["v23w"] += a_v23w
        gt_act["tbb"] += a_tbb
        gt_act["tbr"] += a_tbr
        gt_act["lcv_b"] += a_lcv_b
        gt_act["lcv_r"] += a_lcv_r
        gt_act["scv_b"] += a_scv_b
        gt_act["scv_r"] += a_scv_r
        gt_act["car_b"] += a_car_b
        gt_act["car_r"] += a_car_r
        gt_act["jeep_b"] += a_jeep_b
        gt_act["jeep_r"] += a_jeep_r
        gt_act["tr_f"] += a_tr_f
        gt_act["tr_r"] += a_tr_r
        gt_act["tr_t"] += a_tr_t
        gt_act["adv"] += a_adv
        gt_act["3w"] += a_3w
        gt_act["scoot"] += a_scoot
        gt_act["motor"] += a_motor

        gt_tgt["v4w"] += t_v4w
        gt_tgt["v23w"] += t_v23w
        gt_tgt["tbb"] += t_tbb
        gt_tgt["tbr"] += t_tbr
        gt_tgt["lcv_b"] += t_lcv_b
        gt_tgt["lcv_r"] += t_lcv_r
        gt_tgt["scv_b"] += t_scv_b
        gt_tgt["scv_r"] += t_scv_r
        gt_tgt["car_b"] += t_car_b
        gt_tgt["car_r"] += t_car_r
        gt_tgt["jeep_b"] += t_jeep_b
        gt_tgt["jeep_r"] += t_jeep_r
        gt_tgt["tr_f"] += t_tr_f
        gt_tgt["tr_r"] += t_tr_r
        gt_tgt["tr_t"] += t_tr_t
        gt_tgt["adv"] += t_adv
        gt_tgt["3w"] += t_3w
        gt_tgt["scoot"] += t_scoot
        gt_tgt["motor"] += t_motor

        # Append Actual Row
        results.append({
            "zone": z_name, "row_type": "Actual",
            "val_4w": a_v4w, "val_23w": a_v23w, "val_total": a_vtot,
            "tbb": a_tbb, "tbr": a_tbr, "lcv_b": a_lcv_b, "lcv_r": a_lcv_r,
            "scv_b": a_scv_b, "scv_r": a_scv_r, "car_b": a_car_b, "car_r": a_car_r,
            "jeep_b": a_jeep_b, "jeep_r": a_jeep_r, "tr_fro": a_tr_f, "tr_rear": a_tr_r,
            "tr_trail": a_tr_t, "adv": a_adv, "three_whlrs": a_3w, "scooter": a_scoot, "motor": a_motor,
            "sort_order": s_order * 10 + 1, "is_total": False
        })
        # Append Target Row
        results.append({
            "zone": z_name, "row_type": "Target",
            "val_4w": t_v4w, "val_23w": t_v23w, "val_total": t_vtot,
            "tbb": t_tbb, "tbr": t_tbr, "lcv_b": t_lcv_b, "lcv_r": t_lcv_r,
            "scv_b": t_scv_b, "scv_r": t_scv_r, "car_b": t_car_b, "car_r": t_car_r,
            "jeep_b": t_jeep_b, "jeep_r": t_jeep_r, "tr_fro": t_tr_f, "tr_rear": t_tr_r,
            "tr_trail": t_tr_t, "adv": t_adv, "three_whlrs": t_3w, "scooter": t_scoot, "motor": t_motor,
            "sort_order": s_order * 10 + 2, "is_total": False
        })
        # Append Achv % Row
        results.append({
            "zone": z_name, "row_type": "Achv %",
            "val_4w": p_v4w, "val_23w": p_v23w, "val_total": p_vtot,
            "tbb": p_tbb, "tbr": p_tbr, "lcv_b": p_lcv_b, "lcv_r": p_lcv_r,
            "scv_b": p_scv_b, "scv_r": p_scv_r, "car_b": p_car_b, "car_r": p_car_r,
            "jeep_b": p_jeep_b, "jeep_r": p_jeep_r, "tr_fro": p_tr_f, "tr_rear": p_tr_r,
            "tr_trail": p_tr_t, "adv": p_adv, "three_whlrs": p_3w, "scooter": p_scoot, "motor": p_motor,
            "sort_order": s_order * 10 + 3, "is_total": False
        })

    # Summary Row: Grand Total
    gt_act_v4w = round(gt_act["v4w"], 2)
    gt_act_v23w = round(gt_act["v23w"], 2)
    gt_act_vtot = round(gt_act_v4w + gt_act_v23w, 2)
    gt_act_tbb   = int(round(gt_act["tbb"]))
    gt_act_tbr   = int(round(gt_act["tbr"]))
    gt_act_lcv_b = int(round(gt_act["lcv_b"]))
    gt_act_lcv_r = int(round(gt_act["lcv_r"]))
    gt_act_scv_b = int(round(gt_act["scv_b"]))
    gt_act_scv_r = int(round(gt_act["scv_r"]))
    gt_act_car_b = int(round(gt_act["car_b"]))
    gt_act_car_r = int(round(gt_act["car_r"]))
    gt_act_jeep_b= int(round(gt_act["jeep_b"]))
    gt_act_jeep_r= int(round(gt_act["jeep_r"]))
    gt_act_tr_f  = int(round(gt_act["tr_f"]))
    gt_act_tr_r  = int(round(gt_act["tr_r"]))
    gt_act_tr_t  = int(round(gt_act["tr_t"]))
    gt_act_adv   = int(round(gt_act["adv"]))
    gt_act_3w    = int(round(gt_act["3w"]))
    gt_act_scoot = int(round(gt_act["scoot"]))
    gt_act_motor = int(round(gt_act["motor"]))

    gt_tgt_v4w = round(gt_tgt["v4w"], 2)
    gt_tgt_v23w = round(gt_tgt["v23w"], 2)
    gt_tgt_vtot = round(gt_tgt_v4w + gt_tgt_v23w, 2)
    gt_tgt_tbb   = int(round(gt_tgt["tbb"]))
    gt_tgt_tbr   = int(round(gt_tgt["tbr"]))
    gt_tgt_lcv_b = int(round(gt_tgt["lcv_b"]))
    gt_tgt_lcv_r = int(round(gt_tgt["lcv_r"]))
    gt_tgt_scv_b = int(round(gt_tgt["scv_b"]))
    gt_tgt_scv_r = int(round(gt_tgt["scv_r"]))
    gt_tgt_car_b = int(round(gt_tgt["car_b"]))
    gt_tgt_car_r = int(round(gt_tgt["car_r"]))
    gt_tgt_jeep_b= int(round(gt_tgt["jeep_b"]))
    gt_tgt_jeep_r= int(round(gt_tgt["jeep_r"]))
    gt_tgt_tr_f  = int(round(gt_tgt["tr_f"]))
    gt_tgt_tr_r  = int(round(gt_tgt["tr_r"]))
    gt_tgt_tr_t  = int(round(gt_tgt["tr_t"]))
    gt_tgt_adv   = int(round(gt_tgt["adv"]))
    gt_tgt_3w    = int(round(gt_tgt["3w"]))
    gt_tgt_scoot = int(round(gt_tgt["scoot"]))
    gt_tgt_motor = int(round(gt_tgt["motor"]))

    def calc_achv(act, tgt):
        return round((act * 100.0 / tgt), 2) if tgt > 0 else 0.0

    # Grand Total Actual
    results.append({
        "zone": "Total", "row_type": "Actual",
        "val_4w": gt_act_v4w, "val_23w": gt_act_v23w, "val_total": gt_act_vtot,
        "tbb": gt_act_tbb, "tbr": gt_act_tbr, "lcv_b": gt_act_lcv_b, "lcv_r": gt_act_lcv_r,
        "scv_b": gt_act_scv_b, "scv_r": gt_act_scv_r, "car_b": gt_act_car_b, "car_r": gt_act_car_r,
        "jeep_b": gt_act_jeep_b, "jeep_r": gt_act_jeep_r, "tr_fro": gt_act_tr_f, "tr_rear": gt_act_tr_r,
        "tr_trail": gt_act_tr_t, "adv": gt_act_adv, "three_whlrs": gt_act_3w, "scooter": gt_act_scoot, "motor": gt_act_motor,
        "sort_order": 9991, "is_total": True
    })
    # Grand Total Target
    results.append({
        "zone": "Total", "row_type": "Target",
        "val_4w": gt_tgt_v4w, "val_23w": gt_tgt_v23w, "val_total": gt_tgt_vtot,
        "tbb": gt_tgt_tbb, "tbr": gt_tgt_tbr, "lcv_b": gt_tgt_lcv_b, "lcv_r": gt_tgt_lcv_r,
        "scv_b": gt_tgt_scv_b, "scv_r": gt_tgt_scv_r, "car_b": gt_tgt_car_b, "car_r": gt_tgt_car_r,
        "jeep_b": gt_tgt_jeep_b, "jeep_r": gt_tgt_jeep_r, "tr_fro": gt_tgt_tr_f, "tr_rear": gt_tgt_tr_r,
        "tr_trail": gt_tgt_tr_t, "adv": gt_tgt_adv, "three_whlrs": gt_tgt_3w, "scooter": gt_tgt_scoot, "motor": gt_tgt_motor,
        "sort_order": 9992, "is_total": True
    })
    # Grand Total Achv %
    results.append({
        "zone": "Total", "row_type": "Achv %",
        "val_4w": calc_achv(gt_act_v4w, gt_tgt_v4w),
        "val_23w": calc_achv(gt_act_v23w, gt_tgt_v23w),
        "val_total": calc_achv(gt_act_vtot, gt_tgt_vtot),
        "tbb": calc_achv(gt_act_tbb, gt_tgt_tbb),
        "tbr": calc_achv(gt_act_tbr, gt_tgt_tbr),
        "lcv_b": calc_achv(gt_act_lcv_b, gt_tgt_lcv_b),
        "lcv_r": calc_achv(gt_act_lcv_r, gt_tgt_lcv_r),
        "scv_b": calc_achv(gt_act_scv_b, gt_tgt_scv_b),
        "scv_r": calc_achv(gt_act_scv_r, gt_tgt_scv_r),
        "car_b": calc_achv(gt_act_car_b, gt_tgt_car_b),
        "car_r": calc_achv(gt_act_car_r, gt_tgt_car_r),
        "jeep_b": calc_achv(gt_act_jeep_b, gt_tgt_jeep_b),
        "jeep_r": calc_achv(gt_act_jeep_r, gt_tgt_jeep_r),
        "tr_fro": calc_achv(gt_act_tr_f, gt_tgt_tr_f),
        "tr_rear": calc_achv(gt_act_tr_r, gt_tgt_tr_r),
        "tr_trail": calc_achv(gt_act_tr_t, gt_tgt_tr_t),
        "adv": calc_achv(gt_act_adv, gt_tgt_adv),
        "three_whlrs": calc_achv(gt_act_3w, gt_tgt_3w),
        "scooter": calc_achv(gt_act_scoot, gt_tgt_scoot),
        "motor": calc_achv(gt_act_motor, gt_tgt_motor),
        "sort_order": 9993, "is_total": True
    })

    return results


def save_data_to_table(conn, session_id, year_month, selected_date, rows):
    if not conn or not rows:
        return
    ensure_table(conn)
    try:
        cur = conn.cursor()
        del_sql = f"DELETE FROM `{TABLE_NAME}` WHERE `session_id` = %s AND `year_month` = %s"
        cur.execute(del_sql, (session_id, year_month))

        ins_sql = f"""
        INSERT INTO `{TABLE_NAME}` (
            `session_id`, `year_month`, `selected_date`, `zone_name`, `row_type`,
            `val_4w`, `val_23w`, `val_total`,
            `tbb`, `tbr`, `lcv_b`, `lcv_r`, `scv_b`, `scv_r`,
            `car_b`, `car_r`, `jeep_b`, `jeep_r`, `tr_fro`, `tr_rear`,
            `tr_trail`, `adv`, `three_whlrs`, `scooter`, `motor`,
            `sort_order`, `is_total`, `last_synced_at`, `created_at`
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, NOW(), NOW()
        )
        """
        vals = []
        for r in rows:
            vals.append((
                session_id, year_month, selected_date,
                r.get("zone", ""), r.get("row_type", ""),
                r.get("val_4w", 0), r.get("val_23w", 0), r.get("val_total", 0),
                r.get("tbb", 0), r.get("tbr", 0), r.get("lcv_b", 0), r.get("lcv_r", 0),
                r.get("scv_b", 0), r.get("scv_r", 0), r.get("car_b", 0), r.get("car_r", 0),
                r.get("jeep_b", 0), r.get("jeep_r", 0), r.get("tr_fro", 0), r.get("tr_rear", 0),
                r.get("tr_trail", 0), r.get("adv", 0), r.get("three_whlrs", 0), r.get("scooter", 0),
                r.get("motor", 0),
                r.get("sort_order", 99),
                1 if r.get("is_total") else 0
            ))
        cur.executemany(ins_sql, vals)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[{TABLE_NAME}] Error saving rows: {e}", flush=True)


def get_cached_data(conn, session_id, year_month):
    if not conn:
        return []
    ensure_table(conn)
    rows = []
    try:
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        sql = f"""
            SELECT
                `zone_name` AS `zone`, `row_type`,
                `val_4w`, `val_23w`, `val_total`,
                `tbb`, `tbr`, `lcv_b`, `lcv_r`, `scv_b`, `scv_r`,
                `car_b`, `car_r`, `jeep_b`, `jeep_r`, `tr_fro`, `tr_rear`,
                `tr_trail`, `adv`, `three_whlrs`, `scooter`, `motor`,
                `sort_order`, `is_total`, `last_synced_at`
            FROM `{TABLE_NAME}`
            WHERE `session_id` = %s AND `year_month` = %s
            ORDER BY `sort_order` ASC
        """
        cur.execute(sql, (session_id, year_month))
        res = cur.fetchall()
        cur.close()

        if res:
            for r in res:
                rows.append({
                    "zone": r["zone"],
                    "row_type": r["row_type"],
                    "val_4w": float(r["val_4w"] or 0),
                    "val_23w": float(r["val_23w"] or 0),
                    "val_total": float(r["val_total"] or 0),
                    "tbb": float(r["tbb"] or 0),
                    "tbr": float(r["tbr"] or 0),
                    "lcv_b": float(r["lcv_b"] or 0),
                    "lcv_r": float(r["lcv_r"] or 0),
                    "scv_b": float(r["scv_b"] or 0),
                    "scv_r": float(r["scv_r"] or 0),
                    "car_b": float(r["car_b"] or 0),
                    "car_r": float(r["car_r"] or 0),
                    "jeep_b": float(r["jeep_b"] or 0),
                    "jeep_r": float(r["jeep_r"] or 0),
                    "tr_fro": float(r["tr_fro"] or 0),
                    "tr_rear": float(r["tr_rear"] or 0),
                    "tr_trail": float(r["tr_trail"] or 0),
                    "adv": float(r["adv"] or 0),
                    "three_whlrs": float(r["three_whlrs"] or 0),
                    "scooter": float(r["scooter"] or 0),
                    "motor": float(r["motor"] or 0),
                    "sort_order": int(r["sort_order"] or 99),
                    "is_total": bool(r["is_total"]),
                    "last_synced_at": r.get("last_synced_at")
                })
    except Exception as e:
        print(f"[{TABLE_NAME}] Error reading cached data: {e}", flush=True)
    return rows


def get_sales_summary_no_values_controller(get_db_connection):
    """
    Main controller for Sheet 4.
    Supports query params:
      - session_id
      - month (1-12)
      - year
      - day
      - refresh / force_refresh
    """
    args = request.args if request.method == "GET" else (request.get_json(silent=True) or {})
    session_id_arg = args.get("session_id")
    user_id_arg = args.get("user_id")
    param_month = args.get("month")
    param_year = args.get("year")
    param_day = args.get("day")
    param_ym = args.get("year_month") or args.get("ymonth")
    force_refresh = str(args.get("refresh", "")).lower() in ("true", "1") or str(args.get("force_refresh", "")).lower() in ("true", "1")

    mysql_conn, hana_conn, tunnel, is_hana, schema, effective_session = _get_connection_context(
        get_db_connection, session_id_arg, user_id_arg
    )

    if not mysql_conn:
        return jsonify({"status": "error", "message": "Database connection unavailable."}), 500

    try:
        ensure_table(mysql_conn)

        master_months = []
        if is_hana:
            try:
                h_cur = hana_conn.cursor()
                master_months = fetch_master_months(h_cur, schema)
                h_cur.close()
            except Exception:
                pass

        if not master_months:
            master_months = [
                {"month_no": 1, "month_name": "JAN"},
                {"month_no": 2, "month_name": "FEB"},
                {"month_no": 3, "month_name": "MAR"},
                {"month_no": 4, "month_name": "APR"},
                {"month_no": 5, "month_name": "MAY"},
                {"month_no": 6, "month_name": "JUN"},
                {"month_no": 7, "month_name": "JUL"},
                {"month_no": 8, "month_name": "AUG"},
                {"month_no": 9, "month_name": "SEP"},
                {"month_no": 10, "month_name": "OCT"},
                {"month_no": 11, "month_name": "NOV"},
                {"month_no": 12, "month_name": "DEC"}
            ]

        current_year, detected_month, detected_day = None, None, None
        if is_hana:
            try:
                h_cur = hana_conn.cursor()
                current_year, detected_month, detected_day = detect_sales_dates(
                    h_cur, schema, param_year, param_month, param_day
                )
                h_cur.close()
            except Exception:
                pass

        if not current_year:
            now = datetime.now()
            current_year = int(param_year) if param_year else now.year
            detected_month = int(param_month) if param_month else now.month
            detected_day = int(param_day) if param_day else now.day

        selected_mo_num = None
        if param_month is not None:
            try:
                p_str = str(param_month).strip().upper()
                if p_str.isdigit():
                    selected_mo_num = int(p_str)
                else:
                    found = next((m for m in master_months if m["month_name"] == p_str), None)
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

        if selected_mo_num == detected_month:
            selected_date = f"{current_year}-{str(selected_mo_num).zfill(2)}-{str(detected_day).zfill(2)}"
        else:
            last_d = calendar.monthrange(current_year, selected_mo_num)[1]
            selected_date = f"{current_year}-{str(selected_mo_num).zfill(2)}-{str(last_d).zfill(2)}"

        rows = []
        is_fresh = False
        cached_rows = get_cached_data(mysql_conn, effective_session, year_month)

        if cached_rows and not force_refresh:
            last_sync = cached_rows[0].get("last_synced_at")
            if last_sync:
                diff_sec = (datetime.now() - last_sync).total_seconds()
                if diff_sec < 7200:  # Fresh for 2 hours
                    rows = cached_rows
                    is_fresh = True

        if not is_fresh and is_hana:
            try:
                h_cur = hana_conn.cursor()
                hana_res = query_sales_summary_no_values_hana(h_cur, schema, year_month, selected_date)
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
                # Standard zones fallback
                standard_zones = ["East", "North", "Central", "West", "South - I", "South - II", "Nepal", "Total"]
                for idx, z_name in enumerate(standard_zones, start=1):
                    is_tot = (z_name == "Total")
                    base_order = 999 if is_tot else idx
                    for r_type_idx, r_type in enumerate(["Actual", "Target", "Achv %"], start=1):
                        rows.append({
                            "zone": z_name, "row_type": r_type,
                            "val_4w": 0.0, "val_23w": 0.0, "val_total": 0.0,
                            "tbb": 0.0, "tbr": 0.0, "lcv_b": 0.0, "lcv_r": 0.0,
                            "scv_b": 0.0, "scv_r": 0.0, "car_b": 0.0, "car_r": 0.0,
                            "jeep_b": 0.0, "jeep_r": 0.0, "tr_fro": 0.0, "tr_rear": 0.0,
                            "tr_trail": 0.0, "adv": 0.0, "three_whlrs": 0.0, "scooter": 0.0, "motor": 0.0,
                            "sort_order": base_order * 10 + r_type_idx,
                            "is_total": is_tot
                        })

        mo_abbr = calendar.month_abbr[selected_mo_num]
        curr_yr_short = str(current_year)[-2:]

        meta_info = {
            "month_abbr": mo_abbr,
            "curr_year_short": curr_yr_short,
            "title_label": f"Sales Summary in No's & Values for {mo_abbr}-{curr_yr_short}"
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


def bulk_sync_sales_summary_no_values(mysql_conn, session_id, user_id=None):
    """
    Hook for background scheduler service.
    Precalculates and caches Sheet 4 for all active months.
    """
    if not mysql_conn:
        return
    ensure_table(mysql_conn)
    effective_session = _resolve_session(mysql_conn, session_id)

    hana_conn, tunnel, schema = None, None, "SAP_S4"
    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, effective_session, user_id=user_id)
        if not hana_conn:
            return

        h_cur = hana_conn.cursor()
        now = datetime.now()
        curr_year, det_month, det_day = detect_sales_dates(h_cur, schema, now.year, now.month, now.day)
        if not curr_year:
            curr_year, det_month, det_day = now.year, now.month, now.day

        months_to_sync = [det_month]
        if det_month > 1:
            months_to_sync.append(det_month - 1)

        for mo in months_to_sync:
            ym = f"{curr_year}{str(mo).zfill(2)}"
            if mo == det_month:
                sel_date = f"{curr_year}-{str(mo).zfill(2)}-{str(det_day).zfill(2)}"
            else:
                ld = calendar.monthrange(curr_year, mo)[1]
                sel_date = f"{curr_year}-{str(mo).zfill(2)}-{str(ld).zfill(2)}"

            try:
                res = query_sales_summary_no_values_hana(h_cur, schema, ym, sel_date)
                if res:
                    save_data_to_table(mysql_conn, effective_session, ym, sel_date, res)
            except Exception as e:
                print(f"[{TABLE_NAME}] Scheduler sync error for {ym}: {e}", flush=True)

        h_cur.close()
    except Exception as e:
        print(f"[{TABLE_NAME}] Scheduler bulk sync note: {e}", flush=True)
    finally:
        if hana_conn:
            try: hana_conn.close()
            except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass

