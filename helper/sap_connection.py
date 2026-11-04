import calendar
from datetime import datetime
"""
SAP HANA Connection and SQL Sanitization Helper.
Provides live SAP HANA connection management and schema view mapping
without depending on analytics business logic.
"""

import re
import json
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
        print(f"[SAP-Connection] Notice checking SYS.VIEWS for schema {schema}: {e}")
        
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
        clean_sql = re.sub(rf'\b([a-zA-Z0-9_]+)\.(?:"{u_col}"|{u_col}\b)', rf'\1.{exact_col}', clean_sql, flags=re.IGNORECASE)
        clean_sql = re.sub(rf'(?<![a-zA-Z0-9_".])(?:"{u_col}"|{u_col}\b)', exact_col, clean_sql, flags=re.IGNORECASE)

    try:
        if params:
            cursor.execute(clean_sql, params)
        else:
            cursor.execute(clean_sql)
    except Exception as e:
        err_str = str(e).lower()
        if "cannot allocate enough memory" in err_str or "allocation failed" in err_str:
            print("[SAP-Connection] HANA memory limit hit in query, attempting lightweight CurrentSales retry...")
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
        print(f"[SAP-Connection] Error selecting MySQL workspace DB: {e}")
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
        schema = "SAP_S4"
        raw_schema = effective_schema or external_db.get("schema") or external_db.get("database")
        if raw_schema:
            clean_s = str(raw_schema).split("#")[0].strip()
            schema = clean_s if clean_s else "SAP_S4"
        else:
            schema = TARGET_SCHEMA or "SAP_S4"
        return hana_conn, tunnel, schema

    except Exception as e:
        print(f"[SAP-Connection] Live HANA connection lookup failed: {e}")
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
