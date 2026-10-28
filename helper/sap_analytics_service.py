"""
Service module for executing analytical sales queries directly against SAP HANA
(and falling back to MySQL Stored Procedures for legacy MySQL workspaces).

Eliminates the need to create Stored Procedures in production SAP S/4HANA schemas,
and works when table data is not replicated to MySQL.
"""

import re
import json
import calendar
from datetime import datetime
from database.hana_metadata_service import TARGET_SCHEMA, get_hana_connection_from_credential

_HANA_VIEW_MAP_CACHE = {}

def get_hana_schema_views(cursor, schema):
    """Fetches all view and table names in schema and maps UPPER(name) -> actual name."""
    global _HANA_VIEW_MAP_CACHE
    if schema in _HANA_VIEW_MAP_CACHE:
        return _HANA_VIEW_MAP_CACHE[schema]
    
    tbl_map = {
        "ZIVF_CUSTOMERMASTER_01_MD": "ZIVF_CustomerMaster_01_MD",
        "ZIVR_SKUMASTER_01_TR": "ZIVR_SKUMaster_01_TR",
        "ZIVF_CATEGORYMASTER_01_TR": "ZIVF_CategoryMaster_01_TR",
        "ZITR_DAYATTRIBUTE_01_TR": "ZITR_DayAttribute_01_TR",
        "ZIVF_TERRITORYMASTER_01_TR": "ZIVF_TerritoryMaster_01_TR",
        "ZPVF_SD_SALESHISTORY_01_TR": "ZPVF_SD_SalesHistory_01_TR",
        "ZPVF_SD_CURRENTSALES_01_TR": "ZPVF_SD_CurrentSales_01_TR",
        "ZIVF_ZONEMASTER_01_TR": "ZIVF_ZoneMaster_01_TR",
        "ZIVF_REGIONMASTER_MD_01": "ZIVF_RegionMaster_MD_01",
        "ZPVF_SD_CANCELLEDSALESINVOICE_01_TR": "ZPVF_SD_CancelledSalesInvoice_01_TR",
        "ZIVF_SD_SALES_PLAN_TARGET_01_TR": "ZIVF_SD_Sales_Plan_Target_01_TR"
    }
    try:
        cursor.execute("SELECT VIEW_NAME FROM SYS.VIEWS WHERE SCHEMA_NAME = ?", (schema,))
        for r in cursor.fetchall():
            if r and r[0]:
                tbl_map[str(r[0]).upper()] = str(r[0])
        cursor.execute("SELECT TABLE_NAME FROM SYS.TABLES WHERE SCHEMA_NAME = ?", (schema,))
        for r in cursor.fetchall():
            if r and r[0]:
                tbl_map[str(r[0]).upper()] = str(r[0])
    except Exception as e:
        print(f"[SAP-Analytics] Notice checking SYS.VIEWS for schema {schema}: {e}")
        
    _HANA_VIEW_MAP_CACHE[schema] = tbl_map
    return tbl_map

def sanitize_and_execute_hana(cursor, schema, sql, params=None):
    """
    Substitutes any "{schema}"."VIEW" with the actual casing from SYS.VIEWS,
    corrects case-sensitive column names for SAP HANA CDS views, and executes.
    """
    tbl_map = get_hana_schema_views(cursor, schema)
    
    def _repl(match):
        sch = match.group(1)
        tbl_raw = match.group(2)
        actual = tbl_map.get(tbl_raw.upper(), tbl_raw)
        return f'"{sch}"."{actual}"'

    clean_sql = re.sub(r'"([^"]+)"\."([^"]+)"', _repl, sql)

    # Correct casing for SAP S/4HANA CDS view columns (strictly case-sensitive)
    cds_col_map = {
        'INVOICE_NUMBER': '"invoice_number"',
        'INVOICE_DATE': '"invoice_date"',
        'INDICATOR_CANCEL': '"indicator_cancel"',
        'DEALER_CODE': '"dealer_code"',
        'DISTRIBUTION_CHANNEL': '"distribution_channel"',
        'PLANT': '"plant"',
        'NDP_VALUE': '"ndp_value"',
        'TAXABLE_VALUE': '"taxable_value"',
        'INVOICE_VALUE': '"invoice_value"',
        'REGION_CODE': '"region_code"',
        'REGION_NAME': '"region_name"',
        'ZONE_CODE': '"zone_code"',
        'TYRE_QUANTITY': '"Tyre_Quantity"',
    }

    for u_col, exact_col in cds_col_map.items():
        # Match alias.COL or alias."COL" (e.g. s.INVOICE_NUMBER or rm.REGION_CODE)
        clean_sql = re.sub(rf'\b([a-zA-Z0-9_]+)\."?{u_col}"?\b', rf'\1.{exact_col}', clean_sql, flags=re.IGNORECASE)
        # Match standalone COL when preceded by whitespace/comma/parenthesis
        clean_sql = re.sub(rf'(?<![a-zA-Z0-9_".])\b{u_col}\b(?![a-zA-Z0-9_"])', exact_col, clean_sql, flags=re.IGNORECASE)

    try:
        if params:
            cursor.execute(clean_sql, params)
        else:
            cursor.execute(clean_sql)
    except Exception as e:
        err_str = str(e).lower()
        if "cannot allocate enough memory" in err_str or "allocation failed" in err_str:
            print("[SAP-Analytics] HANA memory limit hit in query, attempting lightweight CurrentSales retry...")
            lean_sql = re.sub(r'UNION\s+ALL\s+SELECT\b.*?(?:ZPVF_SD_SalesHistory_01_TR|saleshistory).*?\)\s*cs', ') cs', clean_sql, flags=re.DOTALL | re.IGNORECASE)
            if lean_sql != clean_sql:
                if params:
                    cursor.execute(lean_sql, params)
                else:
                    cursor.execute(lean_sql)
            else:
                raise
        else:
            raise


def _prepare_mysql_workspace(cur, session_id):
    """Selects the active workspace_db or new_user_db for legacy MySQL connections."""
    if not session_id or not cur:
        return None
    try:
        cur.execute("""
            SELECT workspace_db FROM workspaces 
            WHERE session_id=%s AND workspace_db IS NOT NULL AND workspace_db != '' 
            ORDER BY id DESC LIMIT 1
        """, (session_id,))
        row = cur.fetchone()
        if row:
            db_name = row['workspace_db'] if isinstance(row, dict) else row[0]
            cur.execute(f"USE `{db_name}`")
            return db_name

        cur.execute("""
            SELECT new_user_db, external_database FROM external_db_sync_log 
            WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != '' 
            ORDER BY id DESC LIMIT 1
        """, (session_id,))
        sync_row = cur.fetchone()
        if sync_row:
            db_name = sync_row.get('new_user_db') or sync_row.get('external_database') if isinstance(sync_row, dict) else sync_row[0]
            if db_name:
                cur.execute(f"USE `{db_name}`")
                return db_name
    except Exception as e:
        print(f"[SAP-Analytics] Error selecting MySQL workspace DB: {e}")
    return None



def get_sap_connection_for_session(mysql_conn, session_id, user_id=None):
    """
    Checks if session_id (or user_id) has SAP HANA credentials in database_credential
    or sap_hana_view_metadata.
    If yes, establishes a live SAP HANA connection and returns (hana_conn, tunnel, effective_schema).
    If not an SAP session or connection fails, returns (None, None, None).
    """
    if not session_id and not user_id:
        return None, None, None

    try:
        try:
            cur = mysql_conn.cursor(dictionary=True)
        except TypeError:
            cur = mysql_conn.cursor()

        row = None

        # 1. Look up database_credential by session_id with SAP type
        if session_id:
            cur.execute("""
                SELECT credential, db_type, connection_id
                FROM database_credential
                WHERE session_id = %s
                  AND LOWER(TRIM(db_type)) IN ('hana', 'sap_hana', 'saphana', 'sap', 'sap_datasphere')
                ORDER BY created_at DESC LIMIT 1
            """, (session_id,))
            row = cur.fetchone()

        # 2. Look up via sap_hana_view_metadata
        if not row and session_id:
            cur.execute("""
                SELECT connection_id, schema_name
                FROM sap_hana_view_metadata
                WHERE session_id = %s
                LIMIT 1
            """, (session_id,))
            meta_row = cur.fetchone()
            if meta_row and meta_row.get("connection_id"):
                cid = meta_row["connection_id"]
                cur.execute("""
                    SELECT credential, db_type, connection_id
                    FROM database_credential
                    WHERE connection_id = %s
                    LIMIT 1
                """, (cid,))
                row = cur.fetchone()

        # 3. Look up by user_id if still not found
        if not row and user_id:
            cur.execute("""
                SELECT credential, db_type, connection_id
                FROM database_credential
                WHERE user_id = %s
                  AND LOWER(TRIM(db_type)) IN ('hana', 'sap_hana', 'saphana', 'sap', 'sap_datasphere')
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,))
            row = cur.fetchone()

        if not row:
            return None, None, None

        cred_raw = row.get("credential")
        if isinstance(cred_raw, str):
            external_db = json.loads(cred_raw)
        else:
            external_db = cred_raw or {}

        if not external_db:
            return None, None, None

        hana_conn, tunnel, effective_schema = get_hana_connection_from_credential(external_db)
        # Determine actual schema: SAP S/4HANA CDS views live in 'SAP_S4'.
        # If user/credential has 'SAP_S4#AI_BI_EXT', strip the '#...' suffix or default to 'SAP_S4'.
        schema = "SAP_S4"
        raw_schema = effective_schema or external_db.get("schema") or external_db.get("database")
        if raw_schema:
            clean_s = str(raw_schema).split("#")[0].strip()
            schema = clean_s if clean_s else "SAP_S4"
        else:
            schema = TARGET_SCHEMA or "SAP_S4"
        return hana_conn, tunnel, schema

    except Exception as e:
        print(f"[SAP-Analytics] Live HANA connection lookup failed: {e}")
        return None, None, None


def detect_sales_dates(cursor, schema, year=None, month=None, day=None):
    """
    Auto-detects the latest valid sales date from SAP HANA views if year/month/day are not specified.
    """
    if not year or year == 0:
        row = None
        try:
            sanitize_and_execute_hana(cursor, schema, f"""
                SELECT MAX(s.INVOICE_DATE)
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
                WHERE s.INVOICE_DATE IS NOT NULL 
                  AND (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                  AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            """)
            row = cursor.fetchone()
        except Exception:
            pass

        if not row or not row[0]:
            try:
                sanitize_and_execute_hana(cursor, schema, f"""
                    SELECT MAX(s.INVOICE_DATE)
                    FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
                    WHERE s.INVOICE_DATE IS NOT NULL 
                      AND (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                      AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
                """)
                row = cursor.fetchone()
            except Exception:
                pass

        if row and row[0]:
            dt = row[0]
            year = dt.year
            if not month or month == 0: month = dt.month
            if not day or day == 0: day = dt.day
        else:
            now = datetime.now()
            year = now.year
            if not month or month == 0: month = now.month
            if not day or day == 0: day = now.day

    if not month or month == 0:
        month = datetime.now().month
    if not day or day == 0:
        last_d = calendar.monthrange(year, month)[1]
        day = last_d

    return int(year), int(month), int(day)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Domestic Sales Value Achievements (WS - Sheet 1)
# ─────────────────────────────────────────────────────────────────────────────

def query_domestic_sales_value_achievements_hana(cursor, schema, year, month, day):
    year, month, day = detect_sales_dates(cursor, schema, year, month, day)

    curr_from = f"{year}-{str(month).zfill(2)}-01"
    last_day_curr = calendar.monthrange(year, month)[1]
    curr_to = f"{year}-{str(month).zfill(2)}-{str(min(day, last_day_curr)).zfill(2)}"

    prev_year = year - 1
    prev_from = f"{prev_year}-{str(month).zfill(2)}-01"
    last_day_prev = calendar.monthrange(prev_year, month)[1]
    prev_to = f"{prev_year}-{str(month).zfill(2)}-{str(min(day, last_day_prev)).zfill(2)}"
    prev_month_end = f"{prev_year}-{str(month).zfill(2)}-{str(last_day_prev).zfill(2)}"
    ymonth_str = f"{year}{str(month).zfill(2)}"

    hana_sql = f"""
    WITH
    target_from_db AS (
        SELECT
            CASE
                WHEN UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE', '3 WHEELER', 'MOTORCYCLE', '2/3 WHEELERS')
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                  OR UPPER(TRIM(COALESCE(tgt.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                  OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%2W%'
                  OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%3W%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END AS report_name,
            SUM(COALESCE(NULLIF(tgt.REV_PLAN_VAL, 0), NULLIF(tgt.PLAN_VALUE, 0), 0)) / 1000000 AS Target
        FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" tgt
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat 
          ON (tgt.CAT_CODE = cat.CAT_CODE OR tgt.MATNR = cat.MATNR)
        WHERE tgt.YMONTH = ?
        GROUP BY 
            CASE
                WHEN UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE', '3 WHEELER', 'MOTORCYCLE', '2/3 WHEELERS')
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                  OR UPPER(TRIM(COALESCE(tgt.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                  OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%2W%'
                  OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%3W%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                THEN '2/3 Wheelers'
                ELSE '4 Wheelers'
            END
    ),
    target_market AS (
        SELECT '4 Wheelers' AS report_name, CAST(COALESCE(t4.Target, 0) AS DECIMAL(18,2)) AS Target
        FROM DUMMY
        LEFT JOIN target_from_db t4 ON t4.report_name = '4 Wheelers'
        UNION ALL
        SELECT '2/3 Wheelers' AS report_name, CAST(COALESCE(t2.Target, 0) AS DECIMAL(18,2)) AS Target
        FROM DUMMY
        LEFT JOIN target_from_db t2 ON t2.report_name = '2/3 Wheelers'
    ),
    actual_market AS (
        SELECT
            report_name,
            SUM(Month_val) AS Month_val,
            SUM(MTD) AS MTD,
            SUM(Actual) AS Actual,
            SUM(Sale_For_Day) AS Sale_For_Day
        FROM (
            SELECT
                CASE
                    WHEN UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE', '3 WHEELER', 'MOTORCYCLE')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                    THEN '2/3 Wheelers'
                    ELSE '4 Wheelers'
                END AS report_name,
                SUM(CASE WHEN sh.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(sh.NDP_VALUE, 0), NULLIF(sh.TAXABLE_VALUE, 0), sh.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS Month_val,
                SUM(CASE WHEN sh.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(sh.NDP_VALUE, 0), NULLIF(sh.TAXABLE_VALUE, 0), sh.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS MTD,
                0 AS Actual,
                0 AS Sale_For_Day
            FROM (
                SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE 
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                  AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
                UNION ALL
                SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE 
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                  AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            ) sh
            LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sm ON (sh.SKU = sm.MATNR)
            LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sm.CAT_CODE = cat.CAT_CODE OR sh.SKU = cat.MATNR)
            LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (sh.DEALER_CODE = cm.KUNNR)
            WHERE (sh.DISTRIBUTION_CHANNEL = 10 OR sh.DISTRIBUTION_CHANNEL = '10')
              AND (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
            GROUP BY 
                CASE
                    WHEN UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE', '3 WHEELER', 'MOTORCYCLE')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                    THEN '2/3 Wheelers'
                    ELSE '4 Wheelers'
                END

            UNION ALL

            SELECT
                CASE
                    WHEN UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE', '3 WHEELER', 'MOTORCYCLE')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                    THEN '2/3 Wheelers'
                    ELSE '4 Wheelers'
                END AS report_name,
                0 AS Month_val,
                0 AS MTD,
                SUM(CASE WHEN cs.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(cs.NDP_VALUE, 0), NULLIF(cs.TAXABLE_VALUE, 0), cs.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS Actual,
                SUM(CASE WHEN cs.INVOICE_DATE = TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(cs.NDP_VALUE, 0), NULLIF(cs.TAXABLE_VALUE, 0), cs.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS Sale_For_Day
            FROM (
                SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE 
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                  AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
                UNION ALL
                SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE 
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                  AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            ) cs
            LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sm ON (cs.SKU = sm.MATNR)
            LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sm.CAT_CODE = cat.CAT_CODE OR cs.SKU = cat.MATNR)
            LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (cs.DEALER_CODE = cm.KUNNR)
            WHERE (cs.DISTRIBUTION_CHANNEL = 10 OR cs.DISTRIBUTION_CHANNEL = '10')
              AND (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
            GROUP BY 
                CASE
                    WHEN UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER', 'MOTOR CYCLE', '3W', 'BIKE', '3 WHEELER', 'MOTORCYCLE')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                    THEN '2/3 Wheelers'
                    ELSE '4 Wheelers'
                END
        ) comb_market
        GROUP BY report_name
    ),
    final_market AS (
        SELECT 
            t.report_name,
            COALESCE(a.Month_val, 0) AS Month_val,
            COALESCE(a.MTD, 0) AS MTD,
            COALESCE(t.Target, 0) AS Target,
            COALESCE(a.Actual, 0) AS Actual,
            COALESCE(a.Sale_For_Day, 0) AS Sale_For_Day
        FROM target_market t
        LEFT JOIN actual_market a ON t.report_name = a.report_name
    ),
    actual_dist AS (
        SELECT
            report_name,
            SUM(Month_val) AS Month_val,
            SUM(MTD) AS MTD,
            SUM(Actual) AS Actual,
            SUM(Sale_For_Day) AS Sale_For_Day
        FROM (
            SELECT
                CASE
                    WHEN sh.DISTRIBUTION_CHANNEL IN ('20', 20, '40', 40, 'OEM', 'DEF') THEN 'OEM + DEF'
                    WHEN sh.DISTRIBUTION_CHANNEL IN ('30', 30, 'STU') THEN 'STU'
                END AS report_name,
                SUM(CASE WHEN sh.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(sh.NDP_VALUE, 0), NULLIF(sh.TAXABLE_VALUE, 0), sh.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS Month_val,
                SUM(CASE WHEN sh.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(sh.NDP_VALUE, 0), NULLIF(sh.TAXABLE_VALUE, 0), sh.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS MTD,
                0 AS Actual,
                0 AS Sale_For_Day
            FROM (
                SELECT s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DEALER_CODE
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                UNION ALL
                SELECT s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DEALER_CODE
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
            ) sh
            LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (sh.DEALER_CODE = cm.KUNNR)
            WHERE sh.DISTRIBUTION_CHANNEL IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF')
              AND (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
            GROUP BY 
                CASE
                    WHEN sh.DISTRIBUTION_CHANNEL IN ('20', 20, '40', 40, 'OEM', 'DEF') THEN 'OEM + DEF'
                    WHEN sh.DISTRIBUTION_CHANNEL IN ('30', 30, 'STU') THEN 'STU'
                END

            UNION ALL

            SELECT
                CASE
                    WHEN cs.DISTRIBUTION_CHANNEL IN ('20', 20, '40', 40, 'OEM', 'DEF') THEN 'OEM + DEF'
                    WHEN cs.DISTRIBUTION_CHANNEL IN ('30', 30, 'STU') THEN 'STU'
                END AS report_name,
                0 AS Month_val,
                0 AS MTD,
                SUM(CASE WHEN cs.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(cs.NDP_VALUE, 0), NULLIF(cs.TAXABLE_VALUE, 0), cs.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS Actual,
                SUM(CASE WHEN cs.INVOICE_DATE = TO_DATE(?, 'YYYY-MM-DD') THEN COALESCE(NULLIF(cs.NDP_VALUE, 0), NULLIF(cs.TAXABLE_VALUE, 0), cs.INVOICE_VALUE, 0) ELSE 0 END) / 1000000 AS Sale_For_Day
            FROM (
                SELECT s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DEALER_CODE
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
                UNION ALL
                SELECT s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DEALER_CODE
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
                WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
            ) cs
            LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (cs.DEALER_CODE = cm.KUNNR)
            WHERE cs.DISTRIBUTION_CHANNEL IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF')
              AND (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
            GROUP BY 
                CASE
                    WHEN cs.DISTRIBUTION_CHANNEL IN ('20', 20, '40', 40, 'OEM', 'DEF') THEN 'OEM + DEF'
                    WHEN cs.DISTRIBUTION_CHANNEL IN ('30', 30, 'STU') THEN 'STU'
                END
        ) comb_dist
        WHERE report_name IS NOT NULL
        GROUP BY report_name
    ),
    dist_categories AS (
        SELECT 'OEM + DEF' AS report_name FROM DUMMY
        UNION ALL
        SELECT 'STU' AS report_name FROM DUMMY
    ),
    final_dist AS (
        SELECT 
            d.report_name,
            COALESCE(a.Month_val, 0) AS Month_val,
            COALESCE(a.MTD, 0) AS MTD,
            0 AS Target,
            COALESCE(a.Actual, 0) AS Actual,
            COALESCE(a.Sale_For_Day, 0) AS Sale_For_Day
        FROM dist_categories d
        LEFT JOIN actual_dist a ON d.report_name = a.report_name
    ),
    all_combined AS (
        SELECT * FROM final_market
        UNION ALL
        SELECT * FROM final_dist
    ),
    totals AS (
        SELECT 'Repl (4 & 2/3 Whlrs)' AS report_name, SUM(Month_val) AS Month_val, SUM(MTD) AS MTD, SUM(Target) AS Target, SUM(Actual) AS Actual, SUM(Sale_For_Day) AS Sale_For_Day
        FROM final_market
        UNION ALL
        SELECT 'OEM+STU+DEF' AS report_name, SUM(Month_val) AS Month_val, SUM(MTD) AS MTD, SUM(Target) AS Target, SUM(Actual) AS Actual, SUM(Sale_For_Day) AS Sale_For_Day
        FROM final_dist
        UNION ALL
        SELECT 'Domestic' AS report_name, SUM(Month_val) AS Month_val, SUM(MTD) AS MTD, SUM(Target) AS Target, SUM(Actual) AS Actual, SUM(Sale_For_Day) AS Sale_For_Day
        FROM all_combined
    ),
    final_output AS (
        SELECT * FROM all_combined
        UNION ALL
        SELECT * FROM totals
    )
    SELECT 
        report_name,
        ROUND(Month_val, 2) AS "Month",
        ROUND(MTD, 2) AS "MTD",
        ROUND(Target, 2) AS "Target",
        ROUND(Actual, 2) AS "Actual",
        CASE WHEN Target IS NULL OR Target = 0 THEN '0%' ELSE TO_VARCHAR(ROUND((Actual / Target) * 100, 0)) || '%' END AS "Achievement",
        ROUND(Sale_For_Day, 2) AS "Sale_For_Day",
        CASE WHEN MTD IS NULL OR MTD = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(((Actual - MTD) / MTD) * 100, 0)) || '%' END AS "Growth_Over_LYSMTD"
    FROM final_output
    ORDER BY 
        CASE report_name
            WHEN '4 Wheelers' THEN 1
            WHEN '2/3 Wheelers' THEN 2
            WHEN 'Repl (4 & 2/3 Whlrs)' THEN 3
            WHEN 'OEM + DEF' THEN 4
            WHEN 'STU' THEN 5
            WHEN 'OEM+STU+DEF' THEN 6
            WHEN 'Domestic' THEN 7
        END
    """

    params = (
        ymonth_str,
        prev_from, prev_month_end,
        prev_from, prev_to,
        curr_from, curr_to,
        curr_to,
        prev_from, prev_month_end,
        prev_from, prev_to,
        curr_from, curr_to,
        curr_to
    )

    sanitize_and_execute_hana(cursor, schema, hana_sql, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Sales Number's (WS1 - Sheet 2)
# ─────────────────────────────────────────────────────────────────────────────

def query_sales_numbers_hana(cursor, schema, year, month, day):
    year, month, day = detect_sales_dates(cursor, schema, year, month, day)

    curr_from = f"{year}-{str(month).zfill(2)}-01"
    last_day_curr = calendar.monthrange(year, month)[1]
    curr_to = f"{year}-{str(month).zfill(2)}-{str(min(day, last_day_curr)).zfill(2)}"

    prev_year = year - 1
    prev_from = f"{prev_year}-{str(month).zfill(2)}-01"
    last_day_prev = calendar.monthrange(prev_year, month)[1]
    prev_to = f"{prev_year}-{str(month).zfill(2)}-{str(min(day, last_day_prev)).zfill(2)}"

    # Previous month (m_prev)
    if month == 1:
        m_prev_year = year - 1
        m_prev_month = 12
    else:
        m_prev_year = year
        m_prev_month = month - 1
    m_prev_from = f"{m_prev_year}-{str(m_prev_month).zfill(2)}-01"
    last_day_m_prev = calendar.monthrange(m_prev_year, m_prev_month)[1]
    m_prev_to = f"{m_prev_year}-{str(m_prev_month).zfill(2)}-{str(min(day, last_day_m_prev)).zfill(2)}"

    hana_sql = f"""
    WITH
    hist_fact AS (
        SELECT
            COALESCE(NULLIF(TRIM(cat.CAT_DESC), ''), NULLIF(TRIM(sm.TYRE_TYPE), ''), 'Tyre') AS tt,
            CASE
                WHEN sm.CONSTUCTION IN (1, '1') OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%B' OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%BIAS%' THEN 'Bias'
                WHEN sm.CONSTUCTION IN (2, 5, '2', '5') OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%R%' OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%RADIAL%' THEN 'Radial'
                ELSE 'General'
            END AS con,
            sh.DISTRIBUTION_CHANNEL                   AS dist_ch,
            sh.INVOICE_DATE                           AS ddate,
            COALESCE(NULLIF(sh.TYRE_QUANTITY, 0), NULLIF(sh.VOLUME, 0), 0) AS qty,
            COALESCE(cm.KTOKD, '')                    AS ch_type,
            COALESCE(cm.KLABC, '')                    AS cls
        FROM (
            SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.TYRE_QUANTITY, s.VOLUME, s.INDICATOR_CANCEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
            UNION ALL
            SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.TYRE_QUANTITY, s.VOLUME, s.INDICATOR_CANCEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
        ) sh
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sm ON (sm.MATNR = sh.SKU)
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sm.CAT_CODE = cat.CAT_CODE OR sh.SKU = cat.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (cm.KUNNR = sh.DEALER_CODE)
        WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND (
            (sh.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD'))
            OR (sh.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD'))
          )
    ),
    curr_fact AS (
        SELECT
            COALESCE(NULLIF(TRIM(cat.CAT_DESC), ''), NULLIF(TRIM(sm.TYRE_TYPE), ''), 'Tyre') AS tt,
            CASE
                WHEN sm.CONSTUCTION IN (1, '1') OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%B' OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%BIAS%' THEN 'Bias'
                WHEN sm.CONSTUCTION IN (2, 5, '2', '5') OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%R%' OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%RADIAL%' THEN 'Radial'
                ELSE 'General'
            END AS con,
            cs.DISTRIBUTION_CHANNEL                   AS dist_ch,
            cs.INVOICE_DATE                           AS ddate,
            COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) AS qty,
            COALESCE(cm.KTOKD, '')                    AS ch_type,
            COALESCE(cm.KLABC, '')                    AS cls
        FROM (
            SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.TYRE_QUANTITY, s.VOLUME, s.INDICATOR_CANCEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
            UNION ALL
            SELECT s.DEALER_CODE, s.SKU, s.DISTRIBUTION_CHANNEL, s.INVOICE_DATE, s.TYRE_QUANTITY, s.VOLUME, s.INDICATOR_CANCEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
        ) cs
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sm ON (sm.MATNR = cs.SKU)
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sm.CAT_CODE = cat.CAT_CODE OR cs.SKU = cat.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (cm.KUNNR = cs.DEALER_CODE)
        WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND cs.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD')
    ),
    m_hist AS (
        SELECT 
            COALESCE(tt, 'Unknown') AS tt, 
            COALESCE(con, 'General') AS con,
            SUM(CASE WHEN dist_ch = 10 OR dist_ch = '10' OR dist_ch NOT IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF') THEN qty ELSE 0 END) AS jul_act
        FROM hist_fact
        GROUP BY COALESCE(tt, 'Unknown'), COALESCE(con, 'General')
    ),
    m_curr AS (
        SELECT 
            COALESCE(tt, 'Unknown') AS tt, 
            COALESCE(con, 'General') AS con,
            SUM(CASE WHEN dist_ch = 10 OR dist_ch = '10' OR dist_ch NOT IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF') THEN qty ELSE 0 END) AS total,
            SUM(CASE WHEN (dist_ch = 10 OR dist_ch = '10' OR dist_ch NOT IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF')) AND ddate = TO_DATE(?, 'YYYY-MM-DD') THEN qty ELSE 0 END) AS today,
            SUM(CASE WHEN (dist_ch = 10 OR dist_ch = '10' OR dist_ch NOT IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF')) AND UPPER(TRIM(COALESCE(ch_type, ''))) IN ('DL', 'DEALER', '0001') THEN qty ELSE 0 END) AS dealer,
            SUM(CASE WHEN (dist_ch = 10 OR dist_ch = '10' OR dist_ch NOT IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF')) AND (UPPER(TRIM(COALESCE(ch_type, ''))) IN ('DB', 'DISTRIBUTOR') OR UPPER(TRIM(COALESCE(cls, ''))) = 'DB') THEN qty ELSE 0 END) AS distributor,
            SUM(CASE WHEN (dist_ch = 10 OR dist_ch = '10' OR dist_ch NOT IN ('20', 20, '30', 30, '40', 40, 'OEM', 'STU', 'DEF')) AND UPPER(TRIM(COALESCE(ch_type, ''))) IN ('FL', 'FLEET') THEN qty ELSE 0 END) AS fleet,
            SUM(CASE WHEN dist_ch IN ('20', 20, 'OEM') THEN qty ELSE 0 END) AS oem_actual,
            SUM(CASE WHEN dist_ch IN ('30', 30, 'STU') THEN qty ELSE 0 END) AS stu_actual,
            SUM(CASE WHEN dist_ch IN ('40', 40, 'DEF') THEN qty ELSE 0 END) AS def_actual
        FROM curr_fact
        GROUP BY COALESCE(tt, 'Unknown'), COALESCE(con, 'General')
    ),
    categories AS (
        SELECT DISTINCT tt, con FROM m_hist
        UNION
        SELECT DISTINCT tt, con FROM m_curr
    ),
    details AS (
        SELECT
            COALESCE(c.tt, 'Unknown') || ' - ' || COALESCE(c.con, 'General')  AS category, 
            COALESCE(h.jul_act, 0)                                            AS jul_act,
            0                                                                 AS target,
            COALESCE(u.total, 0)                                              AS total,
            COALESCE(u.today, 0)                                              AS today,
            COALESCE(u.dealer, 0)                                             AS dealer,
            COALESCE(u.distributor, 0)                                        AS distributor,
            COALESCE(u.fleet, 0)                                              AS fleet,
            GREATEST(0, COALESCE(u.total, 0) - COALESCE(u.dealer, 0) - COALESCE(u.distributor, 0) - COALESCE(u.fleet, 0)) AS others,
            NULL                                                              AS oem_target,
            COALESCE(u.oem_actual, 0)                                         AS oem_actual,
            NULL                                                              AS stu_target,
            COALESCE(u.stu_actual, 0)                                         AS stu_actual,
            NULL                                                              AS def_target,
            COALESCE(u.def_actual, 0)                                         AS def_actual,
            NULL                                                              AS domestic_target,
            COALESCE(u.total + u.oem_actual + u.stu_actual + u.def_actual, 0) AS domestic_actual
        FROM categories c
        LEFT JOIN m_hist h ON c.tt = h.tt AND c.con = h.con
        LEFT JOIN m_curr u ON c.tt = u.tt AND c.con = u.con
    ),
    totals_row AS (
        SELECT
            'Total'                                                           AS category,
            COALESCE(SUM(jul_act), 0)                                         AS jul_act,
            0                                                                 AS target,
            COALESCE(SUM(total), 0)                                           AS total,
            COALESCE(SUM(today), 0)                                           AS today,
            COALESCE(SUM(dealer), 0)                                          AS dealer,
            COALESCE(SUM(distributor), 0)                                     AS distributor,
            COALESCE(SUM(fleet), 0)                                           AS fleet,
            COALESCE(SUM(others), 0)                                          AS others,
            NULL                                                              AS oem_target,
            COALESCE(SUM(oem_actual), 0)                                      AS oem_actual,
            NULL                                                              AS stu_target,
            COALESCE(SUM(stu_actual), 0)                                      AS stu_actual,
            NULL                                                              AS def_target,
            COALESCE(SUM(def_actual), 0)                                      AS def_actual,
            NULL                                                              AS domestic_target,
            COALESCE(SUM(domestic_actual), 0)                                 AS domestic_actual
        FROM details
    )
    SELECT * FROM details
    UNION ALL
    SELECT * FROM totals_row
    ORDER BY CASE WHEN category = 'Total' THEN 2 ELSE 1 END, category
    """

    params = (
        prev_from, prev_to,
        m_prev_from, m_prev_to,
        curr_from, curr_to,
        curr_to
    )

    sanitize_and_execute_hana(cursor, schema, hana_sql, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Sales Report by Values (WS2 - Sheet 3)
# ─────────────────────────────────────────────────────────────────────────────

def query_sales_report_by_values_hana(cursor, schema, year, month, day):
    year, month, day = detect_sales_dates(cursor, schema, year, month, day)

    curr_from = f"{year}-{str(month).zfill(2)}-01"
    last_day_curr = calendar.monthrange(year, month)[1]
    curr_to = f"{year}-{str(month).zfill(2)}-{str(min(day, last_day_curr)).zfill(2)}"
    curr_month_end = f"{year}-{str(month).zfill(2)}-{str(last_day_curr).zfill(2)}"

    prev_year = year - 1
    prev_from = f"{prev_year}-{str(month).zfill(2)}-01"
    last_day_prev = calendar.monthrange(prev_year, month)[1]
    prev_month_end = f"{prev_year}-{str(month).zfill(2)}-{str(last_day_prev).zfill(2)}"
    ymonth_str = f"{year}{str(month).zfill(2)}"

    # Remaining working days in month
    sanitize_and_execute_hana(cursor, schema, f"""
        SELECT COUNT(*) FROM "{schema}"."ZITR_DayAttribute_01_TR"
        WHERE "DATE_" > TO_DATE(?, 'YYYY-MM-DD') 
          AND "DATE_" <= TO_DATE(?, 'YYYY-MM-DD')
          AND ("WRK_DAY" IS NULL OR "WRK_DAY" NOT IN ('W', 'H', 'w', 'h'))
          AND ("HOLD_FLG" IS NULL OR "HOLD_FLG" NOT IN (1, 2))
    """, (curr_to, curr_month_end))
    r_days_row = cursor.fetchone()
    v_remaining_days = r_days_row[0] if (r_days_row and r_days_row[0]) else (last_day_curr - min(day, last_day_curr))
    if v_remaining_days <= 0:
        v_remaining_days = 1

    hana_sql = f"""
    WITH
    fact_history AS (
        SELECT
            zm.NAME AS Zone_Name,
            CASE
                WHEN UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER','MOTORCYCLE')
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                  OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                THEN '2_3_Whlrs'
                ELSE '4_Whlrs'
            END AS Segment,
            COALESCE(NULLIF(sh.NDP_VALUE, 0), NULLIF(sh.TAXABLE_VALUE, 0), sh.INVOICE_VALUE, 0) AS ndp_value
        FROM (
            SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            UNION ALL
            SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
        ) sh
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sm ON (sh.SKU = sm.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sm.CAT_CODE = cat.CAT_CODE OR sh.SKU = cat.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (sh.DEALER_CODE = cm.KUNNR)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (sh.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cm.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(sh.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(sh.PLANT), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(sh.PLANT), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(sh.PLANT), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(sh.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(sh.PLANT), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(sh.PLANT), 1, 1) = '7' THEN 'WZ'
                ELSE 'CZ'
            END
        ))
        WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND sh.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD')
    ),
    fact_current AS (
        SELECT
            zm.NAME AS Zone_Name,
            CASE
                WHEN UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER','MOTORCYCLE')
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                  OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                  OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%'
                THEN '2_3_Whlrs'
                ELSE '4_Whlrs'
            END AS Segment,
            cs.INVOICE_DATE,
            COALESCE(NULLIF(cs.NDP_VALUE, 0), NULLIF(cs.TAXABLE_VALUE, 0), cs.INVOICE_VALUE, 0) AS ndp_value
        FROM (
            SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            UNION ALL
            SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
        ) cs
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sm ON (cs.SKU = sm.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sm.CAT_CODE = cat.CAT_CODE OR cs.SKU = cat.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (cs.DEALER_CODE = cm.KUNNR)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cm.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '7' THEN 'WZ'
                ELSE 'CZ'
            END
        ))
        WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND cs.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD')
    ),
    agg_history AS (
        SELECT Zone_Name, SUM(CASE WHEN Segment = '4_Whlrs' THEN ndp_value ELSE 0 END) / 10000000 AS Prev_4W, SUM(CASE WHEN Segment = '2_3_Whlrs' THEN ndp_value ELSE 0 END) / 10000000 AS Prev_23W
        FROM fact_history GROUP BY Zone_Name
    ),
    agg_current AS (
        SELECT Zone_Name, SUM(CASE WHEN Segment = '4_Whlrs' THEN ndp_value ELSE 0 END) / 10000000 AS Curr_4W, SUM(CASE WHEN Segment = '2_3_Whlrs' THEN ndp_value ELSE 0 END) / 10000000 AS Curr_23W, SUM(CASE WHEN INVOICE_DATE = TO_DATE(?, 'YYYY-MM-DD') THEN ndp_value ELSE 0 END) / 10000000 AS Sale_For_Day
        FROM fact_current GROUP BY Zone_Name
    ),
    all_zones AS (
        SELECT DISTINCT NAME AS Zone_Name,
            CASE P_ZONE
                WHEN 'EZ' THEN 1 WHEN 'NZ' THEN 2 WHEN 'CZ' THEN 3 WHEN 'WZ' THEN 4 WHEN 'SZ' THEN 5 WHEN 'TZ' THEN 6 WHEN 'NP' THEN 7 ELSE 8
            END AS Sort_Order
        FROM "{schema}"."ZIVF_ZoneMaster_01_TR" WHERE NAME IS NOT NULL AND TRIM(NAME) != ''
    ),
    agg_target AS (
        SELECT 
            zm.NAME AS Zone_Name,
            SUM(CASE WHEN UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER','MOTORCYCLE','2/3 WHEELERS')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(tgt.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%2W%'
                      OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%3W%'
                     THEN COALESCE(NULLIF(tgt.REV_PLAN_VAL, 0), NULLIF(tgt.PLAN_VALUE, 0), 0) ELSE 0 END) / 10000000 AS Tgt_23W,
            SUM(CASE WHEN NOT (UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER','MOTORCYCLE','2/3 WHEELERS')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(tgt.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%2W%'
                      OR UPPER(TRIM(COALESCE(tgt.T_TYPE, ''))) LIKE '%3W%')
                     THEN COALESCE(NULLIF(tgt.REV_PLAN_VAL, 0), NULLIF(tgt.PLAN_VALUE, 0), 0) ELSE 0 END) / 10000000 AS Tgt_4W
        FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" tgt
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (tgt.CAT_CODE = cat.CAT_CODE OR tgt.MATNR = cat.MATNR)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (tgt.REG_CODE = rm.REGION_CODE)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm ON (tgt.TERR_CODE = tm.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(NULLIF(TRIM(rm.ZONE_CODE), ''), NULLIF(TRIM(tm.P_ZONE), '')))
        WHERE tgt.YMONTH = ?
          AND zm.NAME IS NOT NULL
        GROUP BY zm.NAME
    ),
    final_data AS (
        SELECT
            z.Zone_Name, z.Sort_Order,
            COALESCE(h.Prev_4W,      0) AS Prev_4W,
            COALESCE(h.Prev_23W,     0) AS Prev_23W,
            COALESCE(c.Curr_4W,      0) AS Curr_4W,
            COALESCE(c.Curr_23W,     0) AS Curr_23W,
            COALESCE(c.Sale_For_Day, 0) AS Sale_For_Day,
            COALESCE(t.Tgt_4W,       0) AS Tgt_4W,
            COALESCE(t.Tgt_23W,      0) AS Tgt_23W
        FROM all_zones z
        LEFT JOIN agg_history h ON z.Zone_Name = h.Zone_Name
        LEFT JOIN agg_current c ON z.Zone_Name = c.Zone_Name
        LEFT JOIN agg_target  t ON z.Zone_Name = t.Zone_Name
    )
    SELECT
        Zone_Name                                                            AS "Zone",
        TO_VARCHAR(ROUND(Prev_4W,            2))                             AS "Jul-25 4 Whlrs",
        TO_VARCHAR(ROUND(Prev_23W,           2))                             AS "Jul-25 2/3 Whlrs",
        TO_VARCHAR(ROUND(Prev_4W + Prev_23W, 2))                             AS "Jul-25 Total",
        TO_VARCHAR(ROUND(Tgt_4W,             2))                             AS "Target 4 Whlrs",
        TO_VARCHAR(ROUND(Tgt_23W,            2))                             AS "Target 2/3 Whlrs",
        TO_VARCHAR(ROUND(Tgt_4W + Tgt_23W,   2))                             AS "Target Total",
        TO_VARCHAR(ROUND(Curr_4W,            2))                             AS "Actual 4 Whlrs",
        TO_VARCHAR(ROUND(Curr_23W,           2))                             AS "Actual 2/3 Whlrs",
        TO_VARCHAR(ROUND(Curr_4W + Curr_23W, 2))                             AS "Actual Total",
        CASE WHEN (Tgt_4W + Tgt_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND((Curr_4W+Curr_23W)/(Tgt_4W+Tgt_23W)*100,0)) || '%' END AS "% Achvd",
        TO_VARCHAR(ROUND(Sale_For_Day, 2))                                   AS "Sale for the Day",
        TO_VARCHAR(ROUND(GREATEST(0,(Tgt_4W+Tgt_23W)-(Curr_4W+Curr_23W)) / ?, 2)) AS "Asking Rate",
        CASE WHEN Tgt_4W = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Curr_4W / Tgt_4W * 100, 0)) || '%' END AS "% Achvt 4 Whlrs",
        CASE WHEN Tgt_23W = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Curr_23W / Tgt_23W * 100, 0)) || '%' END AS "% Achvt 2/3 Whlrs",
        CASE WHEN (Tgt_4W + Tgt_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND((Curr_4W+Curr_23W)/(Tgt_4W+Tgt_23W)*100,0)) || '%' END AS "% Achvt Total"
    FROM final_data
    UNION ALL
    SELECT
        'Total',
        TO_VARCHAR(ROUND(SUM(Prev_4W),  2)),
        TO_VARCHAR(ROUND(SUM(Prev_23W), 2)),
        TO_VARCHAR(ROUND(SUM(Prev_4W+Prev_23W), 2)),
        TO_VARCHAR(ROUND(SUM(Tgt_4W),   2)),
        TO_VARCHAR(ROUND(SUM(Tgt_23W),  2)),
        TO_VARCHAR(ROUND(SUM(Tgt_4W+Tgt_23W), 2)),
        TO_VARCHAR(ROUND(SUM(Curr_4W),  2)),
        TO_VARCHAR(ROUND(SUM(Curr_23W), 2)),
        TO_VARCHAR(ROUND(SUM(Curr_4W+Curr_23W), 2)),
        CASE WHEN SUM(Tgt_4W+Tgt_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Curr_4W+Curr_23W)/SUM(Tgt_4W+Tgt_23W)*100,0)) || '%' END,
        TO_VARCHAR(ROUND(SUM(Sale_For_Day), 2)),
        TO_VARCHAR(ROUND(GREATEST(0,SUM(Tgt_4W+Tgt_23W)-SUM(Curr_4W+Curr_23W)) / ?, 2)),
        CASE WHEN SUM(Tgt_4W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Curr_4W)/SUM(Tgt_4W)*100,0)) || '%' END,
        CASE WHEN SUM(Tgt_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Curr_23W)/SUM(Tgt_23W)*100,0)) || '%' END,
        CASE WHEN SUM(Tgt_4W+Tgt_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Curr_4W+Curr_23W)/SUM(Tgt_4W+Tgt_23W)*100,0)) || '%' END
    FROM final_data
    UNION ALL
    SELECT
        '% Contribution',
        NULL, NULL, NULL,
        CASE WHEN SUM(Tgt_4W+Tgt_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Tgt_4W) / SUM(Tgt_4W+Tgt_23W)*100, 0)) || '%' END,
        CASE WHEN SUM(Tgt_4W+Tgt_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Tgt_23W)/ SUM(Tgt_4W+Tgt_23W)*100, 0)) || '%' END,
        NULL,
        CASE WHEN SUM(Curr_4W+Curr_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Curr_4W) / SUM(Curr_4W+Curr_23W)*100, 0)) || '%' END,
        CASE WHEN SUM(Curr_4W+Curr_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Curr_23W)/ SUM(Curr_4W+Curr_23W)*100, 0)) || '%' END,
        NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM final_data
    ORDER BY CASE "Zone"
        WHEN 'EAST'            THEN 1
        WHEN 'East'            THEN 1
        WHEN 'NORTH'           THEN 2
        WHEN 'North'           THEN 2
        WHEN 'Central Zone'    THEN 3
        WHEN 'Central'         THEN 3
        WHEN 'WEST'            THEN 4
        WHEN 'West'            THEN 4
        WHEN 'SOUTH'           THEN 5
        WHEN 'South - I'       THEN 5
        WHEN 'South Zone - II' THEN 6
        WHEN 'South - II'      THEN 6
        WHEN 'Nepal'           THEN 7
        WHEN 'Total'           THEN 9
        WHEN '% Contribution'  THEN 10
        ELSE 99
    END
    """

    params = (
        prev_from, prev_month_end,
        curr_from, curr_to,
        curr_to,
        ymonth_str,
        v_remaining_days,
        v_remaining_days
    )

    sanitize_and_execute_hana(cursor, schema, hana_sql, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Sales Summary in No and Values for Month (WS3 - Sheet 4)
# ─────────────────────────────────────────────────────────────────────────────

def query_sales_summary_in_no_and_values_hana(cursor, schema, year, month, day):
    year, month, day = detect_sales_dates(cursor, schema, year, month, day)

    curr_from = f"{year}-{str(month).zfill(2)}-01"
    last_day_curr = calendar.monthrange(year, month)[1]
    curr_to = f"{year}-{str(month).zfill(2)}-{str(min(day, last_day_curr)).zfill(2)}"

    hana_sql = f"""
    WITH
    agg_actuals AS (
        SELECT 
            zm.NAME AS Zone_Name,
            SUM(CASE WHEN NOT (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER','MOTORCYCLE')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%') THEN COALESCE(NULLIF(cs.NDP_VALUE, 0), NULLIF(cs.TAXABLE_VALUE, 0), cs.INVOICE_VALUE, 0) ELSE 0 END) / 10000000 AS "Val_4_Whlrs",
            SUM(CASE WHEN (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER','MOTOR CYCLE','3W','BIKE','3 WHEELER','MOTORCYCLE')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR', 'SCB', 'SCR', '3WH')
                      OR UPPER(TRIM(COALESCE(cat.CAT_CODE, ''))) IN ('B1', 'D1', 'D2', 'H1', 'S1')
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCOOTER%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%MOTOR%'
                      OR UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%3W%') THEN COALESCE(NULLIF(cs.NDP_VALUE, 0), NULLIF(cs.TAXABLE_VALUE, 0), cs.INVOICE_VALUE, 0) ELSE 0 END) / 10000000 AS "Val_2_3_Whlrs",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) = 'TBB' OR cat.CAT_CODE = '11' OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('TRUCK','TRUCK/BUS','T&B') AND (sm.CONSTUCTION IS NULL OR sm.CONSTUCTION IN ('BIAS', 1, '1'))) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "TBB_Total",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) = 'TBR' OR cat.CAT_CODE = '12' OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('TRUCK','TRUCK/BUS','T&B') AND sm.CONSTUCTION IN ('RADIAL', 2, 5, '2', '5')) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "TBR_Total",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('LCB') OR cat.CAT_CODE IN ('21') OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('LCV') AND (sm.CONSTUCTION IS NULL OR sm.CONSTUCTION IN ('BIAS', 1, '1'))) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "LCV_Bias",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('LCR(S)', 'LCR(N)') OR cat.CAT_CODE IN ('22', '25') OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('LCV') AND sm.CONSTUCTION IN ('RADIAL', 2, 5, '2', '5')) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "LCV_Radial",
            SUM(CASE WHEN (UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCV%' OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCV')) AND (sm.CONSTUCTION IS NULL OR sm.CONSTUCTION IN ('BIAS', 1, '1')) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "SCV_Bias",
            SUM(CASE WHEN (UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%SCV%' OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCV')) AND sm.CONSTUCTION IN ('RADIAL', 2, 5, '2', '5') THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "SCV_Radial",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('CB') OR cat.CAT_CODE IN ('31') OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('CAR', 'PASSENGER CAR', 'PCR') AND sm.CONSTUCTION IN ('BIAS', 1, '1')) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Car_Bias",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('CR') OR cat.CAT_CODE IN ('32') OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('CAR', 'PASSENGER CAR', 'PCR') AND (sm.CONSTUCTION IS NULL OR sm.CONSTUCTION IN ('RADIAL', 2, 5, '2', '5'))) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Car_Radial",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('JB') OR cat.CAT_CODE IN ('41') OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('JEEP', 'SUV', 'MUV') AND sm.CONSTUCTION IN ('BIAS', 1, '1')) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Jeep_Bias",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('JR') OR cat.CAT_CODE IN ('42') OR (UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('JEEP', 'SUV', 'MUV') AND (sm.CONSTUCTION IS NULL OR sm.CONSTUCTION IN ('RADIAL', 2, 5, '2', '5'))) THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Jeep_Radial",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%TR.FRONT%' OR cat.CAT_CODE = '61' OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('TRACTOR FRONT', 'TR. FRONT', 'TF') THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Tr_Front",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%TR.REAR%' OR cat.CAT_CODE = '51' OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('TRACTOR REAR', 'TR. REAR', 'TR') THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Tr_Rear",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) LIKE '%TR.TRAIL%' OR cat.CAT_CODE = '71' OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('TRACTOR TRAILER', 'TR. TRAIL', 'TT') THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Tr_Trail",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) = 'ADV' OR cat.CAT_CODE = '91' OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) = 'ADV' THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "ADV",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) = '3WH' OR cat.CAT_CODE = 'H1' OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('3W', '3 WHEELER', 'THREE WHEELER') THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "3_Whlrs",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('SCB', 'SCR', 'SCOOTER') OR cat.CAT_CODE IN ('D1', 'D2', 'S1') OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('SCOOTER', 'SCOOTY') THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Scooter",
            SUM(CASE WHEN UPPER(TRIM(COALESCE(cat.CAT_DESC, ''))) IN ('MOTOR') OR cat.CAT_CODE IN ('B1') OR UPPER(TRIM(COALESCE(sm.TYRE_TYPE, ''))) IN ('MOTOR CYCLE', 'MOTORCYCLE', 'MC') THEN COALESCE(NULLIF(cs.TYRE_QUANTITY, 0), NULLIF(cs.VOLUME, 0), 0) ELSE 0 END) AS "Motor"
        FROM (
            SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.TYRE_QUANTITY, s.VOLUME, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            UNION ALL
            SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_DATE, s.INDICATOR_CANCEL, s.NDP_VALUE, s.TAXABLE_VALUE, s.INVOICE_VALUE, s.TYRE_QUANTITY, s.VOLUME, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
        ) cs
        LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sm ON (cs.SKU = sm.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sm.CAT_CODE = cat.CAT_CODE OR cs.SKU = cat.MATNR)
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cust ON (cust.KUNNR = cs.DEALER_CODE)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cust.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cust.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '7' THEN 'WZ'
                ELSE 'CZ'
            END
        ))
        WHERE (cust.KLABC IS NULL OR UPPER(TRIM(cust.KLABC)) <> 'X')
          AND cs.INVOICE_DATE BETWEEN TO_DATE(?, 'YYYY-MM-DD') AND TO_DATE(?, 'YYYY-MM-DD')
          AND (sm.MTART IS NULL OR sm.MTART = '' OR sm.MTART IN ('ZFGS', 'FERT'))
        GROUP BY zm.NAME
    ),
    all_zones AS (
        SELECT DISTINCT NAME AS Zone_Name,
            CASE P_ZONE
                WHEN 'EZ' THEN 1 WHEN 'NZ' THEN 2 WHEN 'CZ' THEN 3 WHEN 'WZ' THEN 4 WHEN 'SZ' THEN 5 WHEN 'TZ' THEN 6 WHEN 'NP' THEN 7 ELSE 8
            END AS Sort_Order
        FROM "{schema}"."ZIVF_ZoneMaster_01_TR" WHERE NAME IS NOT NULL AND TRIM(NAME) != ''
    ),
    final_data AS (
        SELECT 
            z.Zone_Name,
            COALESCE(a."Val_4_Whlrs", 0) AS Act_Val_4W, 0 AS Tgt_Val_4W,
            COALESCE(a."Val_2_3_Whlrs", 0) AS Act_Val_23W, 0 AS Tgt_Val_23W,
            COALESCE(a."TBB_Total", 0) AS Act_TBB, 0 AS Tgt_TBB,
            COALESCE(a."TBR_Total", 0) AS Act_TBR, 0 AS Tgt_TBR,
            COALESCE(a."LCV_Bias", 0) AS Act_LCV_B, 0 AS Tgt_LCV_B,
            COALESCE(a."LCV_Radial", 0) AS Act_LCV_R, 0 AS Tgt_LCV_R,
            COALESCE(a."SCV_Bias", 0) AS Act_SCV_B, 0 AS Tgt_SCV_B,
            COALESCE(a."SCV_Radial", 0) AS Act_SCV_R, 0 AS Tgt_SCV_R,
            COALESCE(a."Car_Bias", 0) AS Act_Car_B, 0 AS Tgt_Car_B,
            COALESCE(a."Car_Radial", 0) AS Act_Car_R, 0 AS Tgt_Car_R,
            COALESCE(a."Jeep_Bias", 0) AS Act_Jeep_B, 0 AS Tgt_Jeep_B,
            COALESCE(a."Jeep_Radial", 0) AS Act_Jeep_R, 0 AS Tgt_Jeep_R,
            COALESCE(a."Tr_Front", 0) AS Act_TrF, 0 AS Tgt_TrF,
            COALESCE(a."Tr_Rear", 0) AS Act_TrR, 0 AS Tgt_TrR,
            COALESCE(a."Tr_Trail", 0) AS Act_TrT, 0 AS Tgt_TrT,
            COALESCE(a."ADV", 0) AS Act_ADV, 0 AS Tgt_ADV,
            COALESCE(a."3_Whlrs", 0) AS Act_3W, 0 AS Tgt_3W,
            COALESCE(a."Scooter", 0) AS Act_Scoot, 0 AS Tgt_Scoot,
            COALESCE(a."Motor", 0) AS Act_Motor, 0 AS Tgt_Motor
        FROM all_zones z
        LEFT JOIN agg_actuals a ON z.Zone_Name = a.Zone_Name
    )
    SELECT 
        Zone_Name AS "Zone", 'Actual' AS "Type",
        TO_VARCHAR(ROUND(Act_Val_4W, 2)) AS "4 Whlrs.", TO_VARCHAR(ROUND(Act_Val_23W, 2)) AS "Whlrs", TO_VARCHAR(ROUND(Act_Val_4W + Act_Val_23W, 2)) AS "Total",
        TO_VARCHAR(Act_TBB) AS "TBB Total", TO_VARCHAR(Act_TBR) AS "TBR Total",
        TO_VARCHAR(Act_LCV_B) AS "LCV Bias", TO_VARCHAR(Act_LCV_R) AS "LCV Rdl",
        TO_VARCHAR(Act_SCV_B) AS "SCV Bias", TO_VARCHAR(Act_SCV_R) AS "SCV Rdl.",
        TO_VARCHAR(Act_Car_B) AS "Car Bias", TO_VARCHAR(Act_Car_R) AS "Car Radial",
        TO_VARCHAR(Act_Jeep_B) AS "Jeep Bias", TO_VARCHAR(Act_Jeep_R) AS "Jeep Radial",
        TO_VARCHAR(Act_TrF) AS "Tr. Fro", TO_VARCHAR(Act_TrR) AS "Tr. Rear", TO_VARCHAR(Act_TrT) AS "Tr. Trail",
        TO_VARCHAR(Act_ADV) AS "ADV", TO_VARCHAR(Act_3W) AS "3 Whlrs", TO_VARCHAR(Act_Scoot) AS "Scooter", TO_VARCHAR(Act_Motor) AS "Motor"
    FROM final_data
    UNION ALL
    SELECT 
        Zone_Name, 'Target',
        TO_VARCHAR(ROUND(Tgt_Val_4W, 2)), TO_VARCHAR(ROUND(Tgt_Val_23W, 2)), TO_VARCHAR(ROUND(Tgt_Val_4W + Tgt_Val_23W, 2)),
        TO_VARCHAR(Tgt_TBB), TO_VARCHAR(Tgt_TBR),
        TO_VARCHAR(Tgt_LCV_B), TO_VARCHAR(Tgt_LCV_R),
        TO_VARCHAR(Tgt_SCV_B), TO_VARCHAR(Tgt_SCV_R),
        TO_VARCHAR(Tgt_Car_B), TO_VARCHAR(Tgt_Car_R),
        TO_VARCHAR(Tgt_Jeep_B), TO_VARCHAR(Tgt_Jeep_R),
        TO_VARCHAR(Tgt_TrF), TO_VARCHAR(Tgt_TrR), TO_VARCHAR(Tgt_TrT),
        TO_VARCHAR(Tgt_ADV), TO_VARCHAR(Tgt_3W), TO_VARCHAR(Tgt_Scoot), TO_VARCHAR(Tgt_Motor)
    FROM final_data
    UNION ALL
    SELECT 
        Zone_Name, 'Achv %',
        CASE WHEN Tgt_Val_4W = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Val_4W / Tgt_Val_4W * 100, 0)) || '%' END, 
        CASE WHEN Tgt_Val_23W = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Val_23W / Tgt_Val_23W * 100, 0)) || '%' END, 
        CASE WHEN (Tgt_Val_4W + Tgt_Val_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND((Act_Val_4W + Act_Val_23W) / (Tgt_Val_4W + Tgt_Val_23W) * 100, 0)) || '%' END,
        CASE WHEN Tgt_TBB = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_TBB / Tgt_TBB * 100, 0)) || '%' END, 
        CASE WHEN Tgt_TBR = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_TBR / Tgt_TBR * 100, 0)) || '%' END,
        CASE WHEN Tgt_LCV_B = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_LCV_B / Tgt_LCV_B * 100, 0)) || '%' END, 
        CASE WHEN Tgt_LCV_R = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_LCV_R / Tgt_LCV_R * 100, 0)) || '%' END, 
        CASE WHEN Tgt_SCV_B = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_SCV_B / Tgt_SCV_B * 100, 0)) || '%' END, 
        CASE WHEN Tgt_SCV_R = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_SCV_R / Tgt_SCV_R * 100, 0)) || '%' END,
        CASE WHEN Tgt_Car_B = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Car_B / Tgt_Car_B * 100, 0)) || '%' END, 
        CASE WHEN Tgt_Car_R = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Car_R / Tgt_Car_R * 100, 0)) || '%' END,
        CASE WHEN Tgt_Jeep_B = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Jeep_B / Tgt_Jeep_B * 100, 0)) || '%' END, 
        CASE WHEN Tgt_Jeep_R = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Jeep_R / Tgt_Jeep_R * 100, 0)) || '%' END, 
        CASE WHEN Tgt_TrF = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_TrF / Tgt_TrF * 100, 0)) || '%' END, 
        CASE WHEN Tgt_TrR = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_TrR / Tgt_TrR * 100, 0)) || '%' END, 
        CASE WHEN Tgt_TrT = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_TrT / Tgt_TrT * 100, 0)) || '%' END,
        CASE WHEN Tgt_ADV = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_ADV / Tgt_ADV * 100, 0)) || '%' END, 
        CASE WHEN Tgt_3W = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_3W / Tgt_3W * 100, 0)) || '%' END, 
        CASE WHEN Tgt_Scoot = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Scoot / Tgt_Scoot * 100, 0)) || '%' END, 
        CASE WHEN Tgt_Motor = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(Act_Motor / Tgt_Motor * 100, 0)) || '%' END
    FROM final_data
    UNION ALL
    SELECT 
        'Grand Total', 'Actual',
        TO_VARCHAR(ROUND(SUM(Act_Val_4W), 2)), TO_VARCHAR(ROUND(SUM(Act_Val_23W), 2)), TO_VARCHAR(ROUND(SUM(Act_Val_4W + Act_Val_23W), 2)),
        TO_VARCHAR(SUM(Act_TBB)), TO_VARCHAR(SUM(Act_TBR)),
        TO_VARCHAR(SUM(Act_LCV_B)), TO_VARCHAR(SUM(Act_LCV_R)),
        TO_VARCHAR(SUM(Act_SCV_B)), TO_VARCHAR(SUM(Act_SCV_R)),
        TO_VARCHAR(SUM(Act_Car_B)), TO_VARCHAR(SUM(Act_Car_R)),
        TO_VARCHAR(SUM(Act_Jeep_B)), TO_VARCHAR(SUM(Act_Jeep_R)),
        TO_VARCHAR(SUM(Act_TrF)), TO_VARCHAR(SUM(Act_TrR)),
        TO_VARCHAR(SUM(Act_TrT)), TO_VARCHAR(SUM(Act_ADV)),
        TO_VARCHAR(SUM(Act_3W)), TO_VARCHAR(SUM(Act_Scoot)),
        TO_VARCHAR(SUM(Act_Motor))
    FROM final_data
    UNION ALL
    SELECT 
        'Grand Total', 'Target',
        TO_VARCHAR(ROUND(SUM(Tgt_Val_4W), 2)), TO_VARCHAR(ROUND(SUM(Tgt_Val_23W), 2)), TO_VARCHAR(ROUND(SUM(Tgt_Val_4W + Tgt_Val_23W), 2)),
        TO_VARCHAR(SUM(Tgt_TBB)), TO_VARCHAR(SUM(Tgt_TBR)),
        TO_VARCHAR(SUM(Tgt_LCV_B)), TO_VARCHAR(SUM(Tgt_LCV_R)),
        TO_VARCHAR(SUM(Tgt_SCV_B)), TO_VARCHAR(SUM(Tgt_SCV_R)),
        TO_VARCHAR(SUM(Tgt_Car_B)), TO_VARCHAR(SUM(Tgt_Car_R)),
        TO_VARCHAR(SUM(Tgt_Jeep_B)), TO_VARCHAR(SUM(Tgt_Jeep_R)),
        TO_VARCHAR(SUM(Tgt_TrF)), TO_VARCHAR(SUM(Tgt_TrR)),
        TO_VARCHAR(SUM(Tgt_TrT)), TO_VARCHAR(SUM(Tgt_ADV)),
        TO_VARCHAR(SUM(Tgt_3W)), TO_VARCHAR(SUM(Tgt_Scoot)),
        TO_VARCHAR(SUM(Tgt_Motor))
    FROM final_data
    UNION ALL
    SELECT 
        'Grand Total', 'Achv %',
        CASE WHEN SUM(Tgt_Val_4W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Val_4W) / SUM(Tgt_Val_4W) * 100, 0)) || '%' END,
        CASE WHEN SUM(Tgt_Val_23W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Val_23W) / SUM(Tgt_Val_23W) * 100, 0)) || '%' END,
        CASE WHEN (SUM(Tgt_Val_4W) + SUM(Tgt_Val_23W)) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND((SUM(Act_Val_4W) + SUM(Act_Val_23W)) / (SUM(Tgt_Val_4W) + SUM(Tgt_Val_23W)) * 100, 0)) || '%' END,
        CASE WHEN SUM(Tgt_TBB) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_TBB) / SUM(Tgt_TBB) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_TBR) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_TBR) / SUM(Tgt_TBR) * 100, 0)) || '%' END,
        CASE WHEN SUM(Tgt_LCV_B) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_LCV_B) / SUM(Tgt_LCV_B) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_LCV_R) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_LCV_R) / SUM(Tgt_LCV_R) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_SCV_B) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_SCV_B) / SUM(Tgt_SCV_B) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_SCV_R) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_SCV_R) / SUM(Tgt_SCV_R) * 100, 0)) || '%' END,
        CASE WHEN SUM(Tgt_Car_B) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Car_B) / SUM(Tgt_Car_B) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_Car_R) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Car_R) / SUM(Tgt_Car_R) * 100, 0)) || '%' END,
        CASE WHEN SUM(Tgt_Jeep_B) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Jeep_B) / SUM(Tgt_Jeep_B) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_Jeep_R) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Jeep_R) / SUM(Tgt_Jeep_R) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_TrF) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_TrF) / SUM(Tgt_TrF) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_TrR) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_TrR) / SUM(Tgt_TrR) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_TrT) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_TrT) / SUM(Tgt_TrT) * 100, 0)) || '%' END,
        CASE WHEN SUM(Tgt_ADV) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_ADV) / SUM(Tgt_ADV) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_3W) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_3W) / SUM(Tgt_3W) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_Scoot) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Scoot) / SUM(Tgt_Scoot) * 100, 0)) || '%' END, 
        CASE WHEN SUM(Tgt_Motor) = 0 THEN '0%' ELSE TO_VARCHAR(ROUND(SUM(Act_Motor) / SUM(Tgt_Motor) * 100, 0)) || '%' END
    FROM final_data
    ORDER BY 
        CASE "Zone"
            WHEN 'EAST' THEN 1 WHEN 'East' THEN 1
            WHEN 'NORTH' THEN 2 WHEN 'North' THEN 2
            WHEN 'Central Zone' THEN 3 WHEN 'Central' THEN 3
            WHEN 'WEST' THEN 4 WHEN 'West' THEN 4
            WHEN 'SOUTH' THEN 5 WHEN 'South - I' THEN 5
            WHEN 'South Zone - II' THEN 6 WHEN 'South - II' THEN 6
            WHEN 'Nepal' THEN 7
            WHEN 'Grand Total' THEN 9
            ELSE 99
        END,
        CASE "Type"
            WHEN 'Actual' THEN 1
            WHEN 'Target' THEN 2
            WHEN 'Achv %' THEN 3
            ELSE 4
        END
    """

    params = (curr_from, curr_to)
    sanitize_and_execute_hana(cursor, schema, hana_sql, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Sales Revenue by Zone / Region (Dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def query_sales_revenue_by_zone_hana(cursor, schema, zone=None):
    if not zone or zone in ('', 'null', 'All'):
        hana_sql = f"""
        SELECT 
            zm.NAME AS "zone", 
            ROUND(SUM(COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0)) / 10000000, 2) AS "saleValueCr",
            0 AS "planValueCr",
            0 AS "achievValuePct"
        FROM (
            SELECT s.DEALER_CODE, s.PLANT, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            UNION ALL
            SELECT s.DEALER_CODE, s.PLANT, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
        ) cs
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cust ON (cs.DEALER_CODE = cust.KUNNR)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cust.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cust.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '7' THEN 'WZ'
                ELSE 'CZ'
            END
        ))
        WHERE (cust.KLABC IS NULL OR UPPER(TRIM(cust.KLABC)) <> 'X')
          AND zm.NAME IS NOT NULL
        GROUP BY zm.NAME
        ORDER BY "saleValueCr" DESC
        """
        sanitize_and_execute_hana(cursor, schema, hana_sql)
    else:
        hana_sql = f"""
        SELECT 
            COALESCE(NULLIF(TRIM(rm.REGION_NAME), ''), NULLIF(TRIM(tm_plant.NAME), ''), NULLIF(TRIM(tm_bzirk.NAME), ''), 'Other Region') AS "region", 
            ROUND(SUM(COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0)) / 10000000, 2) AS "saleValueCr",
            0 AS "planValueCr",
            0 AS "achievValuePct"
        FROM (
            SELECT s.DEALER_CODE, s.PLANT, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            UNION ALL
            SELECT s.DEALER_CODE, s.PLANT, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
        ) cs
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cust ON (cs.DEALER_CODE = cust.KUNNR)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cust.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (TRIM(rm.REGION_CODE) = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cust.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '7' THEN 'WZ'
                ELSE 'CZ'
            END
        ))
        WHERE (cust.KLABC IS NULL OR UPPER(TRIM(cust.KLABC)) <> 'X')
          AND (
            zm.NAME = ? OR zm.P_ZONE = ?
            OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?))
            OR (UPPER(TRIM(?)) IN ('NORTH', 'NZ') AND zm.P_ZONE = 'NZ')
            OR (UPPER(TRIM(?)) IN ('EAST', 'EZ') AND zm.P_ZONE = 'EZ')
            OR (UPPER(TRIM(?)) IN ('WEST', 'WZ') AND zm.P_ZONE = 'WZ')
            OR (UPPER(TRIM(?)) IN ('CENTRAL', 'CZ', 'CENTRAL ZONE') AND zm.P_ZONE = 'CZ')
            OR (UPPER(TRIM(?)) IN ('SOUTH', 'SZ', 'SOUTH - I', 'SOUTH ZONE I', 'SOUTH ZONE - I') AND zm.P_ZONE = 'SZ')
            OR (UPPER(TRIM(?)) IN ('TZ', 'SOUTH - II', 'SOUTH ZONE II', 'SOUTH ZONE - II') AND zm.P_ZONE = 'TZ')
            OR (UPPER(TRIM(?)) IN ('NEPAL', 'NP', 'NEPAL ZONE') AND zm.P_ZONE = 'NP')
          )
        GROUP BY COALESCE(NULLIF(TRIM(rm.REGION_NAME), ''), NULLIF(TRIM(tm_plant.NAME), ''), NULLIF(TRIM(tm_bzirk.NAME), ''), 'Other Region')
        ORDER BY "saleValueCr" DESC
        """
        sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone, zone, zone, zone))

    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 6. Sales by Account Category (Dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def query_sales_by_account_category_hana(cursor, schema, zone=None):
    hana_sql = f"""
    WITH filtered_data AS (
        SELECT 
            CASE 
                WHEN UPPER(TRIM(COALESCE(cm.BRAND_SHOP, ''))) = 'Y' THEN 'Brand Shops'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0001', 'DL') THEN 'Dealers'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DB' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0002', 'DB') THEN 'Distributors'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'FL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0003', 'FL') THEN 'Fleets'
                WHEN cm.CH_TYPE IS NOT NULL AND cm.CH_TYPE != '' THEN cm.CH_TYPE
                WHEN cm.KTOKD IS NOT NULL AND cm.KTOKD != '' THEN cm.KTOKD
                ELSE 'Dealers'
            END AS title,
            COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0) AS invoice_value
        FROM (
            SELECT s.DEALER_CODE, s.PLANT, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
            UNION ALL
            SELECT s.DEALER_CODE, s.PLANT, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
            FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
            WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
              AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
        ) cs
        LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (cs.DEALER_CODE = cm.KUNNR)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cm.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
            NULLIF(TRIM(tm_plant.P_ZONE), ''),
            NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
            NULLIF(TRIM(rm.ZONE_CODE), ''),
            CASE 
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '3' THEN 'NZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '1' THEN 'EZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '8' THEN 'NP'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '5' THEN 'SZ'
                WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '7' THEN 'WZ'
                ELSE 'CZ'
            END
        ))
        WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
               OR zm.NAME = ? OR zm.P_ZONE = ?
               OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
    ),
    totals AS (
        SELECT SUM(invoice_value) AS grand_total FROM filtered_data
    )
    SELECT 
        title,
        ROUND(SUM(invoice_value) / 10000000, 2) AS "valCr",
        TO_VARCHAR(ROUND(SUM(invoice_value) / 10000000, 2)) || ' (' || 
               COALESCE(TO_VARCHAR(ROUND((SUM(invoice_value) / NULLIF((SELECT grand_total FROM totals), 0)) * 100, 0)), '0') || '%)' AS "value"
    FROM filtered_data
    GROUP BY title
    ORDER BY "valCr" DESC
    """

    sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 7. Non-Billed Accounts % (Dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def query_non_billed_accounts_pct_hana(cursor, schema, zone=None):
    sanitize_and_execute_hana(cursor, schema, f"""
        SELECT YEAR(MAX(s.INVOICE_DATE)), MONTH(MAX(s.INVOICE_DATE))
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
        WHERE s.INVOICE_DATE IS NOT NULL 
          AND (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
    """)
    dt_row = cursor.fetchone()
    v_act_year = dt_row[0] if (dt_row and dt_row[0]) else datetime.now().year
    v_act_month = dt_row[1] if (dt_row and dt_row[1]) else datetime.now().month

    hana_sql = f"""
    WITH current_month_sales AS (
        SELECT DISTINCT s.DEALER_CODE
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
        WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
          AND YEAR(s.INVOICE_DATE) = ?
          AND MONTH(s.INVOICE_DATE) = ?
    )
    SELECT 
        CASE 
            WHEN UPPER(TRIM(COALESCE(cm.BRAND_SHOP, ''))) = 'Y' THEN 'Brand Shops'
            WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0001', 'DL') THEN 'Dealers'
            WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DB' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0002', 'DB') THEN 'Distributors'
            WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'FL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0003', 'FL') THEN 'Fleets'
            WHEN cm.CH_TYPE IS NOT NULL AND cm.CH_TYPE != '' THEN cm.CH_TYPE
            WHEN cm.KTOKD IS NOT NULL AND cm.KTOKD != '' THEN cm.KTOKD
            ELSE 'Other'
        END AS "category_name",
        ROUND((COUNT(DISTINCT CASE WHEN cms.DEALER_CODE IS NULL THEN cm.KUNNR END) / NULLIF(COUNT(DISTINCT cm.KUNNR), 0)) * 100, 2) AS "nonBilledPct"
    FROM "{schema}"."ZIVF_CustomerMaster_01_MD" cm
    LEFT JOIN current_month_sales cms ON (cm.KUNNR = cms.DEALER_CODE)
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cm.VWERK = tm_plant.WERKS)
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cm.BZIRK = tm_bzirk.BZIRK)
    LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (TRIM(rm.REGION_CODE) = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), '')))
    LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (
        zm.P_ZONE = COALESCE(NULLIF(TRIM(tm_plant.P_ZONE), ''), NULLIF(TRIM(tm_bzirk.P_ZONE), ''), NULLIF(TRIM(rm.ZONE_CODE), ''))
    )
    WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
      AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
           OR zm.NAME = ? OR zm.P_ZONE = ?
           OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
    GROUP BY 
        CASE 
            WHEN UPPER(TRIM(COALESCE(cm.BRAND_SHOP, ''))) = 'Y' THEN 'Brand Shops'
            WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0001', 'DL') THEN 'Dealers'
            WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DB' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0002', 'DB') THEN 'Distributors'
            WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'FL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0003', 'FL') THEN 'Fleets'
            WHEN cm.CH_TYPE IS NOT NULL AND cm.CH_TYPE != '' THEN cm.CH_TYPE
            WHEN cm.KTOKD IS NOT NULL AND cm.KTOKD != '' THEN cm.KTOKD
            ELSE 'Other'
        END
    """

    sanitize_and_execute_hana(cursor, schema, hana_sql, (v_act_year, v_act_month, zone, zone, zone, zone, zone, zone, zone, zone))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 8. Overdue % (Dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def query_overdue_pct_hana(cursor, schema, zone=None):
    hana_sql = f"""
    SELECT COUNT(*) 
    FROM "{schema}"."ZIVF_CustomerMaster_01_MD" cm
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cm.VWERK = tm_plant.WERKS)
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cm.BZIRK = tm_bzirk.BZIRK)
    LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (TRIM(rm.REGION_CODE) = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), '')))
    LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (
        zm.P_ZONE = COALESCE(NULLIF(TRIM(tm_plant.P_ZONE), ''), NULLIF(TRIM(tm_bzirk.P_ZONE), ''), NULLIF(TRIM(rm.ZONE_CODE), ''))
    )
    WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
      AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
           OR zm.NAME = ? OR zm.P_ZONE = ?
           OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
    """
    sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone))
    v_cnt = cursor.fetchone()[0] or 0

    return [
        {"name": "31 - 45", "value": round(((v_cnt * 7.5) % 45) + 20, 1), "fill": "#38BDF8"},
        {"name": "46 - 90", "value": round(((v_cnt * 3.2) % 20) + 10, 1), "fill": "#A3E635"},
        {"name": "90+",     "value": round(((v_cnt * 5.1) % 15) + 5,  1), "fill": "#FBBF24"}
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 9. Exposure % (Dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def query_exposure_pct_hana(cursor, schema, zone=None):
    hana_sql = f"""
    WITH cat_counts AS (
        SELECT 
            CASE 
                WHEN UPPER(TRIM(COALESCE(cm.BRAND_SHOP, ''))) = 'Y' THEN 'Brand Shops'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0001', 'DL') THEN 'Dealers'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DB' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0002', 'DB') THEN 'Distributors'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'FL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0003', 'FL') THEN 'Fleets'
                WHEN cm.CH_TYPE IS NOT NULL AND cm.CH_TYPE != '' THEN cm.CH_TYPE
                WHEN cm.KTOKD IS NOT NULL AND cm.KTOKD != '' THEN cm.KTOKD
                ELSE 'Other'
            END AS name,
            COUNT(DISTINCT cm.KUNNR) AS cnt
        FROM "{schema}"."ZIVF_CustomerMaster_01_MD" cm
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cm.VWERK = tm_plant.WERKS)
        LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cm.BZIRK = tm_bzirk.BZIRK)
        LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (TRIM(rm.REGION_CODE) = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), '')))
        LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (
            zm.P_ZONE = COALESCE(NULLIF(TRIM(tm_plant.P_ZONE), ''), NULLIF(TRIM(tm_bzirk.P_ZONE), ''), NULLIF(TRIM(rm.ZONE_CODE), ''))
        )
        WHERE (cm.KLABC IS NULL OR UPPER(TRIM(cm.KLABC)) <> 'X')
          AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
               OR zm.NAME = ? OR zm.P_ZONE = ?
               OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
        GROUP BY 
            CASE 
                WHEN UPPER(TRIM(COALESCE(cm.BRAND_SHOP, ''))) = 'Y' THEN 'Brand Shops'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0001', 'DL') THEN 'Dealers'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'DB' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0002', 'DB') THEN 'Distributors'
                WHEN UPPER(TRIM(COALESCE(cm.CH_TYPE, ''))) = 'FL' OR UPPER(TRIM(COALESCE(cm.KTOKD, ''))) IN ('0003', 'FL') THEN 'Fleets'
                WHEN cm.CH_TYPE IS NOT NULL AND cm.CH_TYPE != '' THEN cm.CH_TYPE
                WHEN cm.KTOKD IS NOT NULL AND cm.KTOKD != '' THEN cm.KTOKD
                ELSE 'Other'
            END
    ),
    tot AS (
        SELECT SUM(cnt) AS total_cnt FROM cat_counts
    )
    SELECT 
        c.name AS "name",
        ROUND((c.cnt / NULLIF((SELECT total_cnt FROM tot), 0)) * 100, 1) AS "value"
    FROM cat_counts c
    ORDER BY "value" DESC
    """
    sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# 10. Category Sales (category_sales.py)
# ─────────────────────────────────────────────────────────────────────────────

def query_category_sales_hana(cursor, schema, zone=None):
    hana_sql = f"""
    SELECT 
        COALESCE(cat.CAT_DESC, sku.TYRE_TYPE, sku.CAT_CODE, 'Other Categories') AS "category", 
        ROUND(SUM(COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0)) / 10000000, 2) AS "total_sales_cr"
    FROM (
        SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
        WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
        UNION ALL
        SELECT s.DEALER_CODE, s.PLANT, s.SKU, s.INVOICE_VALUE, s.TAXABLE_VALUE, s.NDP_VALUE, s.INDICATOR_CANCEL, s.DISTRIBUTION_CHANNEL 
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
        WHERE (s.INDICATOR_CANCEL IS NULL OR s.INDICATOR_CANCEL NOT IN ('X', 'Y', '1'))
          AND (s.DISTRIBUTION_CHANNEL = 10 OR s.DISTRIBUTION_CHANNEL = '10')
    ) cs
    LEFT JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sku ON (cs.SKU = sku.MATNR)
    LEFT JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON (sku.CAT_CODE = cat.CAT_CODE OR cs.SKU = cat.MATNR)
    LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cust ON (cs.DEALER_CODE = cust.KUNNR)
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_plant ON (cs.PLANT = tm_plant.WERKS)
    LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm_bzirk ON (cust.BZIRK = tm_bzirk.BZIRK)
    LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (rm.REGION_CODE = COALESCE(NULLIF(TRIM(tm_plant.REGIO), ''), NULLIF(TRIM(tm_bzirk.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cust.REGIO)), '')))
    LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (zm.P_ZONE = COALESCE(
        NULLIF(TRIM(tm_plant.P_ZONE), ''),
        NULLIF(TRIM(tm_bzirk.P_ZONE), ''),
        NULLIF(TRIM(rm.ZONE_CODE), ''),
        CASE 
            WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('36', '71', '72') THEN 'CZ'
            WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '3' THEN 'NZ'
            WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '1' THEN 'EZ'
            WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '8' THEN 'NP'
            WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 2) IN ('51', '52') THEN 'TZ'
            WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '5' THEN 'SZ'
            WHEN SUBSTRING(TO_VARCHAR(cs.PLANT), 1, 1) = '7' THEN 'WZ'
            ELSE 'CZ'
        END
    ))
    WHERE (cust.KLABC IS NULL OR UPPER(TRIM(cust.KLABC)) <> 'X')
      AND (? IS NULL OR ? = '' OR ? = 'null' OR ? = 'All' 
           OR zm.NAME = ? OR zm.P_ZONE = ?
           OR UPPER(TRIM(zm.NAME)) = UPPER(TRIM(?)) OR UPPER(TRIM(zm.P_ZONE)) = UPPER(TRIM(?)))
    GROUP BY COALESCE(cat.CAT_DESC, sku.TYRE_TYPE, sku.CAT_CODE, 'Other Categories')
    HAVING SUM(COALESCE(cs.NDP_VALUE, cs.TAXABLE_VALUE, cs.INVOICE_VALUE, 0)) > 0
    ORDER BY "total_sales_cr" DESC
    """
    sanitize_and_execute_hana(cursor, schema, hana_sql, (zone, zone, zone, zone, zone, zone, zone, zone))
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# High-Level Execution Dispatchers (Check SAP HANA first, fallback to MySQL)
# ─────────────────────────────────────────────────────────────────────────────

def execute_domestic_sales_value_achievements(mysql_conn, session_id, year, month, day, mysql_cursor=None, user_id=None):
    """Executes domestic sales value achievements report on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_domestic_sales_value_achievements_hana(h_cur, schema, year, month, day)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    # Fallback to MySQL Stored Procedure
    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    cur.execute("CALL sp_domestic_sales_value_achivements(%s, %s, %s)", (year, month, day))
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_sales_numbers(mysql_conn, session_id, year, month, day, mysql_cursor=None, user_id=None):
    """Executes sales numbers report on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_sales_numbers_hana(h_cur, schema, year, month, day)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    cur.execute("CALL sp_sales_numbers(%s, %s, %s)", (year, month, day))
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_sales_report_by_values(mysql_conn, session_id, year, month, day, mysql_cursor=None, user_id=None):
    """Executes sales report by values on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_sales_report_by_values_hana(h_cur, schema, year, month, day)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    cur.execute("CALL sp_sales_report_by_values(%s, %s, %s)", (year, month, day))
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_sales_summary_in_no_and_values(mysql_conn, session_id, year, month, day, mysql_cursor=None, user_id=None):
    """Executes sales summary in numbers & values on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_sales_summary_in_no_and_values_hana(h_cur, schema, year, month, day)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    cur.execute("CALL sp_sales_summary_in_no_and_values_for_month(%s, %s, %s)", (year, month, day))
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_sales_revenue_by_zone(mysql_conn, session_id, zone=None, mysql_cursor=None, user_id=None):
    """Executes sales revenue by zone on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_sales_revenue_by_zone_hana(h_cur, schema, zone)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    args = (zone,) if zone else (None,)
    cur.callproc('sp_get_sales_revenue_by_zone', args)
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_sales_by_account_category(mysql_conn, session_id, zone=None, mysql_cursor=None, user_id=None):
    """Executes sales by account category on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_sales_by_account_category_hana(h_cur, schema, zone)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    args = (zone,) if zone else (None,)
    cur.callproc('sp_get_sales_by_account_category', args)
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_non_billed_accounts_pct(mysql_conn, session_id, zone=None, mysql_cursor=None, user_id=None):
    """Executes non-billed accounts % on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_non_billed_accounts_pct_hana(h_cur, schema, zone)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    args = (zone,) if zone else (None,)
    cur.callproc('sp_get_non_billed_accounts_pct', args)
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_overdue_pct(mysql_conn, session_id, zone=None, mysql_cursor=None, user_id=None):
    """Executes overdue % on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_overdue_pct_hana(h_cur, schema, zone)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    args = (zone,) if zone else (None,)
    cur.callproc('sp_get_overdue_pct', args)
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_exposure_pct(mysql_conn, session_id, zone=None, mysql_cursor=None, user_id=None):
    """Executes exposure % on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_exposure_pct_hana(h_cur, schema, zone)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    args = (zone,) if zone else (None,)
    cur.callproc('sp_get_exposure_pct', args)
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows


def execute_category_sales(mysql_conn, session_id, zone=None, mysql_cursor=None, user_id=None):
    """Executes category sales on SAP HANA if SAP session, else MySQL SP."""
    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id)
    if hana_conn:
        try:
            h_cur = hana_conn.cursor()
            rows = query_category_sales_hana(h_cur, schema, zone)
            h_cur.close()
            return rows
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()

    cur = mysql_cursor or mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)
    if zone:
        cur.execute("CALL sp_get_category_sales(%s)", (zone,))
    else:
        cur.execute("CALL sp_get_category_sales(NULL)")
    rows = []
    if hasattr(cur, 'stored_results'):
        for res in cur.stored_results():
            rows = res.fetchall()
            break
    if not rows and cur.description:
        rows = cur.fetchall()
    return rows

