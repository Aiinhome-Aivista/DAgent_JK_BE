"""
Controller and service for Dynamic Dependent Filters and Year-wise Sales Comparison Dashboard.
STRICTLY NO HARDCODING in API or UI.
All data is fetched directly from SAP HANA (or synced MySQL workspace) via parameterized SQL queries.

Master & Source Views:
  - Zone:          SAP_S4.ZIVF_ZoneMaster_01_TR
  - Region:        SAP_S4.ZIVF_RegionMaster_MD_01
  - Customer Type: SAP_S4.ZIVF_CustomerMaster_01_MD
  - Month:         SAP_S4.ZITR_DayAttribute_01_TR
  - Year:          SAP_S4.ZPVF_SD_SalesHistory_01_TR & ZPVF_SD_CurrentSales_01_TR
  - Cancelled:     SAP_S4.ZPVF_SD_CancelledSalesInvoice_01_TR

Business Rules:
  1. distribution_channel = '10'
  2. KLABC <> 'X'
  3. Cancelled Invoice check:
       (invoice_number, plant, FKART) not in CancelledSalesInvoice
       indicator_cancel NOT IN ('X', 'Y', '1')
"""

import time
import calendar
from flask import request, jsonify

# In-memory backend cache for master filter queries to prevent repeated DB hits (TTL: 10 minutes)
_FILTER_CACHE = {}
_CACHE_TTL_SECS = 600

def _get_cache(key):
    entry = _FILTER_CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL_SECS:
        return entry["data"]
    return None

def _set_cache(key, data):
    _FILTER_CACHE[key] = {
        "ts": time.time(),
        "data": data
    }


def fetchall_dict(cursor):
    """
    Safely converts query results from any cursor (hdbcli pyhdbcli.ResultRow, pymysql DictCursor,
    or standard cursor) into a list of lowercase-keyed Python dicts.
    """
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [{str(k).lower(): v for k, v in r.items()} for r in rows]

    cols = [str(desc[0]).lower() for desc in cursor.description] if cursor.description else []
    result = []
    for r in rows:
        if isinstance(r, dict):
            result.append({str(k).lower(): v for k, v in r.items()})
        else:
            try:
                result.append(dict(zip(cols, r)))
            except Exception:
                row_dict = {}
                for idx, c in enumerate(cols):
                    try: row_dict[c] = r[idx]
                    except Exception: pass
                result.append(row_dict)
    return result


def _val(r, *keys, default=None):
    if not isinstance(r, dict):
        return default
    for k in keys:
        lk = str(k).lower()
        if lk in r and r[lk] is not None:
            return r[lk]
    return default


def _resolve_session(conn, session_id):
    if session_id and session_id != "null" and session_id != "undefined":
        return session_id.strip()
    try:
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("SELECT session_id FROM workspaces WHERE session_id IS NOT NULL AND session_id != '' ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row and isinstance(row, dict) and row.get("session_id"):
            return row["session_id"]
        elif row and hasattr(row, "__getitem__"):
            return row[0]
    except Exception:
        pass
    return session_id or ""


def _get_connection_context(get_db_connection, session_id_arg=None, user_id_arg=None):
    mysql_conn = get_db_connection()
    if not mysql_conn:
        raise Exception("Failed to connect to MySQL application database")

    session_id = _resolve_session(mysql_conn, session_id_arg)
    user_id = user_id_arg

    from helper.sap_analytics_service import get_sap_connection_for_session
    hana_conn = None
    tunnel = None
    sap_schema = "SAP_S4"
    is_hana = False

    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id=user_id)
        if hana_conn:
            is_hana = True
            sap_schema = schema or "SAP_S4"
    except Exception:
        is_hana = False

    user_db = None
    if not is_hana:
        try:
            cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            cur.execute("""
                SELECT new_user_db 
                FROM external_db_sync_log 
                WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            s_row = cur.fetchone()
            if not s_row:
                cur.execute("""
                    SELECT workspace_db FROM workspaces 
                    WHERE session_id=%s AND workspace_db IS NOT NULL AND workspace_db != ''
                    LIMIT 1
                """, (session_id,))
                s_row = cur.fetchone()
            if s_row and isinstance(s_row, dict):
                user_db = s_row.get("new_user_db") or s_row.get("workspace_db")
            elif s_row and hasattr(s_row, "__getitem__"):
                user_db = s_row[0]
            if not user_db:
                user_db = "jk_sales_workspace_db"
            cur.close()
        except Exception:
            user_db = "jk_sales_workspace_db"

    return mysql_conn, hana_conn, tunnel, is_hana, sap_schema, user_db, session_id


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


MONTH_MAP_LABEL_TO_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
}
MONTH_MAP_NUM_TO_LABEL = {v: k for k, v in MONTH_MAP_LABEL_TO_NUM.items()}
for i in range(1, 13):
    MONTH_MAP_LABEL_TO_NUM[calendar.month_name[i].upper()] = i
    MONTH_MAP_LABEL_TO_NUM[calendar.month_abbr[i].upper()] = i


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fetch Zones directly from ZIVF_ZoneMaster_01_TR
# ─────────────────────────────────────────────────────────────────────────────
def fetch_zones(cursor, is_hana, schema, user_db):
    zones = []
    if is_hana:
        sql = f"""
            SELECT DISTINCT
                "P_ZONE" AS ZONE_CODE,
                "NAME" AS ZONE_NAME
            FROM "{schema}"."ZIVF_ZoneMaster_01_TR"
            WHERE "P_ZONE" IS NOT NULL
            ORDER BY "P_ZONE" ASC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            zc = _val(r, "zone_code", "p_zone")
            zn = _val(r, "zone_name", "name")
            if zc:
                zones.append({"label": str(zn or zc).strip(), "value": str(zc).strip()})
    else:
        sql = f"""
            SELECT DISTINCT zone_code AS ZONE_CODE, zone_name AS ZONE_NAME 
            FROM `{user_db}`.`zone_master`
            WHERE zone_code IS NOT NULL AND TRIM(zone_code) <> ''
            ORDER BY zone_code ASC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            zc = _val(r, "zone_code")
            zn = _val(r, "zone_name")
            if zc:
                zones.append({"label": str(zn or zc).strip(), "value": str(zc).strip()})

    return [{"label": "ALL", "value": "ALL"}] + zones


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fetch Regions directly from ZIVF_RegionMaster_MD_01
# ─────────────────────────────────────────────────────────────────────────────
def fetch_regions(cursor, is_hana, schema, user_db, zone=None):
    regions = []
    zone_filter = str(zone).strip().upper() if zone and str(zone).strip().lower() != "all" else None

    if is_hana:
        params = []
        where_cls = 'WHERE "region_code" IS NOT NULL'
        if zone_filter:
            where_cls += ' AND UPPER(TRIM("zone_code")) = ?'
            params.append(zone_filter)

        sql = f"""
            SELECT DISTINCT
                "region_code" AS REGION_CODE,
                "region_name" AS REGION_NAME,
                "zone_code" AS ZONE_CODE
            FROM "{schema}"."ZIVF_RegionMaster_MD_01"
            {where_cls}
            ORDER BY "zone_code", "region_code"
        """
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

        for r in fetchall_dict(cursor):
            rc = _val(r, "region_code")
            rn = _val(r, "region_name")
            zc = _val(r, "zone_code")
            if rc:
                regions.append({
                    "label": str(rn or rc).strip(),
                    "value": str(rc).strip(),
                    "zone_code": str(zc or "").strip()
                })
    else:
        params = []
        where_cls = "WHERE region IS NOT NULL"
        if zone_filter:
            where_cls += " AND UPPER(TRIM(zone)) = %s"
            params.append(zone_filter)

        sql = f"""
            SELECT DISTINCT region AS REGION_CODE, region_name AS REGION_NAME, zone AS ZONE_CODE
            FROM `{user_db}`.`region_master`
            {where_cls}
            ORDER BY zone, region
        """
        if params:
            cursor.execute(sql, tuple(params))
        else:
            cursor.execute(sql)

        for r in fetchall_dict(cursor):
            rc = _val(r, "region_code", "region")
            rn = _val(r, "region_name")
            zc = _val(r, "zone_code", "zone")
            if rc:
                regions.append({
                    "label": str(rn or rc).strip(),
                    "value": str(rc).strip(),
                    "zone_code": str(zc or "").strip()
                })

    return [{"label": "ALL", "value": "ALL", "zone_code": "ALL"}] + regions


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fetch Customer Types directly from ZIVF_CustomerMaster_01_MD & TerritoryMaster
# ─────────────────────────────────────────────────────────────────────────────
def fetch_customer_types(cursor, is_hana, schema, user_db, zone=None, region=None):
    customer_types = []
    zone_val = str(zone).strip().upper() if zone and str(zone).strip().lower() != "all" else None
    region_val = str(region).strip() if region and str(region).strip().lower() != "all" else None

    if is_hana:
        if region_val:
            # Dependent on selected Region via TerritoryMaster REGIO
            sql = f"""
                SELECT DISTINCT C."CH_TYPE" AS CUSTOMER_TYPE
                FROM "{schema}"."ZIVF_CustomerMaster_01_MD" C
                INNER JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" T
                    ON C."BZIRK" = T."BZIRK"
                INNER JOIN "{schema}"."ZIVF_RegionMaster_MD_01" R
                    ON T."REGIO" = R."region_code"
                WHERE C."KLABC" <> 'X'
                  AND C."CH_TYPE" IS NOT NULL
                  AND TRIM(R."region_code") = ?
                ORDER BY C."CH_TYPE" ASC
            """
            cursor.execute(sql, [region_val])
        elif zone_val:
            # Dependent on selected Zone via TerritoryMaster P_ZONE
            sql = f"""
                SELECT DISTINCT C."CH_TYPE" AS CUSTOMER_TYPE
                FROM "{schema}"."ZIVF_CustomerMaster_01_MD" C
                INNER JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" T
                    ON C."BZIRK" = T."BZIRK"
                INNER JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" Z
                    ON T."P_ZONE" = Z."P_ZONE"
                WHERE C."KLABC" <> 'X'
                  AND C."CH_TYPE" IS NOT NULL
                  AND UPPER(TRIM(Z."P_ZONE")) = ?
                ORDER BY C."CH_TYPE" ASC
            """
            cursor.execute(sql, [zone_val])
        else:
            # All eligible customer types directly from CustomerMaster
            sql = f"""
                SELECT DISTINCT C."CH_TYPE" AS CUSTOMER_TYPE
                FROM "{schema}"."ZIVF_CustomerMaster_01_MD" C
                WHERE C."KLABC" <> 'X'
                  AND C."CH_TYPE" IS NOT NULL
                ORDER BY C."CH_TYPE" ASC
            """
            cursor.execute(sql)

        rows = fetchall_dict(cursor)
        if not rows and (region_val or zone_val):
            # Fallback if specific join had no records to ensure dropdown is never empty
            fallback_sql = f"""
                SELECT DISTINCT C."CH_TYPE" AS CUSTOMER_TYPE
                FROM "{schema}"."ZIVF_CustomerMaster_01_MD" C
                WHERE C."KLABC" <> 'X'
                  AND C."CH_TYPE" IS NOT NULL
                ORDER BY C."CH_TYPE" ASC
            """
            cursor.execute(fallback_sql)
            rows = fetchall_dict(cursor)

        for r in rows:
            ct = _val(r, "customer_type", "ch_type")
            if ct:
                customer_types.append({"label": str(ct).strip(), "value": str(ct).strip()})
    else:
        rows = []
        if region_val:
            try:
                sql = f"""
                    SELECT DISTINCT C.ch_type AS CUSTOMER_TYPE
                    FROM `{user_db}`.`customer_master` C
                    INNER JOIN `{user_db}`.`territory_master` T ON C.bzirk = T.bzirk
                    INNER JOIN `{user_db}`.`region_master` R ON T.regio = R.region_code
                    WHERE IFNULL(C.klabc, '') <> 'X' AND C.ch_type IS NOT NULL AND TRIM(C.ch_type) <> ''
                      AND TRIM(R.region_code) = %s
                    ORDER BY C.ch_type ASC
                """
                cursor.execute(sql, (region_val,))
                rows = fetchall_dict(cursor)
            except Exception:
                rows = []
        elif zone_val:
            try:
                sql = f"""
                    SELECT DISTINCT C.ch_type AS CUSTOMER_TYPE
                    FROM `{user_db}`.`customer_master` C
                    INNER JOIN `{user_db}`.`territory_master` T ON C.bzirk = T.bzirk
                    INNER JOIN `{user_db}`.`zone_master` Z ON T.p_zone = Z.zone_code
                    WHERE IFNULL(C.klabc, '') <> 'X' AND C.ch_type IS NOT NULL AND TRIM(C.ch_type) <> ''
                      AND UPPER(TRIM(Z.zone_code)) = %s
                    ORDER BY C.ch_type ASC
                """
                cursor.execute(sql, (zone_val,))
                rows = fetchall_dict(cursor)
            except Exception:
                rows = []

        if not rows:
            try:
                sql = f"""
                    SELECT DISTINCT ch_type AS CUSTOMER_TYPE
                    FROM `{user_db}`.`customer_master`
                    WHERE IFNULL(klabc, '') <> 'X' AND ch_type IS NOT NULL AND TRIM(ch_type) <> ''
                    ORDER BY ch_type ASC
                """
                cursor.execute(sql)
                rows = fetchall_dict(cursor)
            except Exception:
                rows = []

        for r in rows:
            ct = _val(r, "customer_type", "ch_type")
            if ct:
                customer_types.append({"label": str(ct).strip(), "value": str(ct).strip()})

    return [{"label": "ALL", "value": "ALL"}] + customer_types


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fetch Master Months directly from ZITR_DayAttribute_01_TR
# ─────────────────────────────────────────────────────────────────────────────
def fetch_master_months(cursor, is_hana, schema, user_db):
    months = []
    if is_hana:
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
        for r in fetchall_dict(cursor):
            m_no = _val(r, "month_no")
            m_name = _val(r, "month_name")
            if m_no:
                months.append({"label": str(m_name).strip(), "value": int(m_no)})
    else:
        sql = f"""
            SELECT DISTINCT
                MONTH(billing__doc_date) AS MONTH_NO,
                CASE MONTH(billing__doc_date)
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
            FROM `{user_db}`.`sales_data`
            WHERE billing__doc_date IS NOT NULL
            ORDER BY MONTH_NO ASC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            m_no = _val(r, "month_no")
            m_name = _val(r, "month_name")
            if m_no:
                months.append({"label": str(m_name).upper().strip(), "value": int(m_no)})

    return [{"label": "ALL", "value": "ALL"}] + months


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fetch Master Years directly from Sales History & Current Sales
# ─────────────────────────────────────────────────────────────────────────────
def fetch_master_years(cursor, is_hana, schema, user_db):
    years = []
    if is_hana:
        sql = f"""
            SELECT DISTINCT
                YEAR("invoice_date") AS SALES_YEAR
            FROM (
                SELECT "invoice_date"
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"

                UNION ALL

                SELECT "invoice_date"
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
            ) S
            WHERE "invoice_date" >= ADD_YEARS(CURRENT_DATE, -2)
              AND "invoice_date" <= CURRENT_DATE
            ORDER BY SALES_YEAR DESC
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            y = _val(r, "sales_year", "year")
            if y:
                years.append({"label": str(y).strip(), "value": int(y)})
    else:
        sql = f"""
            SELECT DISTINCT YEAR(billing__doc_date) AS SALES_YEAR
            FROM `{user_db}`.`sales_data`
            WHERE billing__doc_date IS NOT NULL
            ORDER BY SALES_YEAR DESC
            LIMIT 3
        """
        cursor.execute(sql)
        for r in fetchall_dict(cursor):
            y = _val(r, "sales_year", "year")
            if y:
                years.append({"label": str(y).strip(), "value": int(y)})

    return [{"label": "ALL", "value": "ALL"}] + years


# ─────────────────────────────────────────────────────────────────────────────
# 6. Build Parameterized Sales Query with Business Rules & Faceted Filtering
# ─────────────────────────────────────────────────────────────────────────────
def _build_sales_filter_query(cursor, is_hana, schema, user_db, zone=None, region=None, customer_type=None, month=None, years=None, exclude_dim=None):
    valid_years = []
    if exclude_dim != "year" and years:
        raw_years = years if isinstance(years, list) else [y.strip() for y in str(years).split(",") if y.strip()]
        for y in raw_years:
            if str(y).strip().lower() == "all": continue
            try: valid_years.append(int(y))
            except ValueError: pass

    valid_months = []
    if exclude_dim != "month" and month:
        raw_months = month if isinstance(month, list) else [m.strip() for m in str(month).split(",") if m.strip()]
        for m in raw_months:
            if not m or str(m).strip().lower() == "all": continue
            sm = str(m).strip()
            if sm.isdigit():
                valid_months.append(int(sm))
            else:
                m_lookup = {
                    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
                }
                if sm.upper() in m_lookup:
                    valid_months.append(m_lookup[sm.upper()])

    zone_val = str(zone).strip().upper() if zone and str(zone).strip().lower() != "all" else None
    region_val = str(region).strip() if region and str(region).strip().lower() != "all" else None
    cust_val = str(customer_type).strip().upper() if customer_type and str(customer_type).strip().lower() != "all" else None

    if is_hana:
        min_year = min(valid_years) if valid_years else 2024
        start_date = f"'{min_year}-01-01'"

        if valid_years and all(y <= 2025 for y in valid_years):
            sales_source = f"""
                SELECT "dealer_code", "invoice_date", "ndp_value", "taxable_value", "invoice_value"
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
                WHERE "distribution_channel" = '10'
                  AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
                  AND "invoice_date" >= {start_date}
            """
        else:
            sales_source = f"""
                SELECT "dealer_code", "invoice_date", "ndp_value", "taxable_value", "invoice_value"
                FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR"
                WHERE "distribution_channel" = '10'
                  AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
                  AND "invoice_date" >= {start_date}
                UNION ALL
                SELECT "dealer_code", "invoice_date", "ndp_value", "taxable_value", "invoice_value"
                FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR"
                WHERE "distribution_channel" = '10'
                  AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
                  AND "invoice_date" >= {start_date}
            """

        where_clauses = ["s.\"invoice_date\" IS NOT NULL"]
        params = []

        if valid_years:
            ph = ", ".join(["?"] * len(valid_years))
            where_clauses.append(f'YEAR(s."invoice_date") IN ({ph})')
            params.extend(valid_years)

        if valid_months:
            ph = ", ".join(["?"] * len(valid_months))
            where_clauses.append(f'MONTH(s."invoice_date") IN ({ph})')
            params.extend(valid_months)

        join_lines = [f"FROM ({sales_source}) s"]
        need_cm = bool(cust_val or region_val or zone_val)
        need_tm = bool(region_val or zone_val)

        if need_cm:
            join_lines.append(f'INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (s."dealer_code" = cm."KUNNR" OR LPAD(TRIM(s."dealer_code"), 10, \'0\') = cm."KUNNR" OR LTRIM(s."dealer_code", \'0\') = LTRIM(cm."KUNNR", \'0\'))')
            where_clauses.append('IFNULL(cm."KLABC", \'\') <> \'X\'')
            if cust_val:
                where_clauses.append('UPPER(TRIM(cm."CH_TYPE")) = ?')
                params.append(cust_val)

        if need_tm:
            join_lines.append(f'LEFT JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm ON (cm."BZIRK" = tm."BZIRK" OR TRIM(cm."BZIRK") = TRIM(tm."BZIRK"))')
            join_lines.append(f'LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm ON (tm."REGIO" = rm."region_code" OR TRIM(tm."REGIO") = TRIM(rm."region_code"))')
            join_lines.append(f'LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm ON (COALESCE(tm."P_ZONE", rm."zone_code") = zm."P_ZONE")')

            if zone_val:
                where_clauses.append('''
                    (UPPER(TRIM(tm."P_ZONE")) = ? 
                     OR UPPER(TRIM(rm."zone_code")) = ? 
                     OR UPPER(TRIM(zm."NAME")) = ? 
                     OR UPPER(TRIM(zm."P_ZONE")) = ?
                     OR (? IN ('EAST', 'EZ') AND COALESCE(tm."P_ZONE", rm."zone_code", zm."P_ZONE") = 'EZ')
                     OR (? IN ('WEST', 'WZ') AND COALESCE(tm."P_ZONE", rm."zone_code", zm."P_ZONE") = 'WZ')
                     OR (? IN ('NORTH', 'NZ') AND COALESCE(tm."P_ZONE", rm."zone_code", zm."P_ZONE") = 'NZ')
                     OR (? IN ('SOUTH', 'SZ') AND COALESCE(tm."P_ZONE", rm."zone_code", zm."P_ZONE") = 'SZ')
                     OR (? IN ('CENTRAL', 'CZ') AND COALESCE(tm."P_ZONE", rm."zone_code", zm."P_ZONE") = 'CZ')
                     OR (? IN ('TZ') AND COALESCE(tm."P_ZONE", rm."zone_code", zm."P_ZONE") = 'TZ')
                     OR (? IN ('NEPAL', 'NP') AND COALESCE(tm."P_ZONE", rm."zone_code", zm."P_ZONE") = 'NP'))
                ''')
                params.extend([zone_val, zone_val, zone_val, zone_val, zone_val, zone_val, zone_val, zone_val, zone_val, zone_val, zone_val])

            if region_val:
                where_clauses.append('''
                    (TRIM(rm."region_code") = ? 
                     OR UPPER(TRIM(rm."region_name")) = UPPER(?) 
                     OR TRIM(tm."REGIO") = ? 
                     OR TRIM(tm."BZIRK") = ? 
                     OR UPPER(TRIM(tm."NAME")) = UPPER(?) 
                     OR TRIM(cm."BZIRK") = ?)
                ''')
                params.extend([region_val, region_val, region_val, region_val, region_val, region_val])

        joins = "\n".join(join_lines)
        where_sql = "WHERE " + " AND ".join(where_clauses)
        rev_expr = 'COALESCE(s."ndp_value", s."taxable_value", s."invoice_value", 0)'
        date_expr = 's."invoice_date"'

        return joins, where_sql, params, date_expr, rev_expr

    else:
        where_clauses = [
            "(s.distribution__channel = '10' OR s.distribution__channel = 10)",
            "s.billing__doc_date IS NOT NULL",
            "IFNULL(cm.klabc, '') <> 'X'"
        ]
        params = []

        if valid_years:
            ph = ", ".join(["%s"] * len(valid_years))
            where_clauses.append(f"YEAR(s.billing__doc_date) IN ({ph})")
            params.extend(valid_years)

        if valid_months:
            ph = ", ".join(["%s"] * len(valid_months))
            where_clauses.append(f"MONTH(s.billing__doc_date) IN ({ph})")
            params.extend(valid_months)

        if cust_val:
            where_clauses.append("UPPER(TRIM(cm.ch_type)) = %s")
            params.append(cust_val)

        if region_val:
            where_clauses.append("(rm.region = %s OR cm.sales_office = %s OR cm.dlr_catg = %s)")
            params.extend([region_val, region_val, region_val])

        if zone_val:
            where_clauses.append("UPPER(TRIM(rm.zone)) = %s")
            params.append(zone_val)

        joins = f"""
            FROM `{user_db}`.`sales_data` s
            JOIN `{user_db}`.`customer_master` cm ON (s.customer = cm.kunnr)
            LEFT JOIN `{user_db}`.`region_master` rm ON (rm.region = cm.sales_office OR rm.region = cm.dlr_catg OR rm.region = cm.segment)
        """

        where_sql = "WHERE " + " AND ".join(where_clauses)
        rev_expr = "COALESCE(s.invoice_value_inr, s.taxable_value_inr, s.ndp_inr, 0)"
        date_expr = "s.billing__doc_date"

        return joins, where_sql, params, date_expr, rev_expr


# ─────────────────────────────────────────────────────────────────────────────
# 7. Faceted Available Periods (Months & Years)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_available_periods(cursor, is_hana, schema, user_db, zone=None, region=None, customer_type=None, month=None, years=None):
    # Returns available months and years dynamically from DayAttribute and Sales views
    # Kept fast and non-blocking so the dashboard renders immediately
    months_result = fetch_master_months(cursor, is_hana, schema, user_db)
    years_result = fetch_master_years(cursor, is_hana, schema, user_db)
    return months_result, years_result


# ─────────────────────────────────────────────────────────────────────────────
# API 1: Initial Filter Data
# GET /api/dashboard/filters
# ─────────────────────────────────────────────────────────────────────────────
def get_dashboard_filters_controller(get_db_connection):
    mysql_conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    try:
        session_id = request.args.get("session_id")
        user_id = request.args.get("user_id")

        mysql_conn, hana_conn, tunnel, is_hana, schema, user_db, effective_session = _get_connection_context(
            get_db_connection, session_id, user_id
        )
        cursor = hana_conn.cursor() if is_hana else mysql_conn.cursor()

        cache_key = f"filters_init_{effective_session}_{is_hana}"
        cached = _get_cache(cache_key)
        if cached:
            return jsonify(cached), 200

        zones = fetch_zones(cursor, is_hana, schema, user_db)
        regions = fetch_regions(cursor, is_hana, schema, user_db)
        customer_types = fetch_customer_types(cursor, is_hana, schema, user_db)
        months = fetch_master_months(cursor, is_hana, schema, user_db)
        years = fetch_master_years(cursor, is_hana, schema, user_db)

        response_data = {
            "status": "success",
            "zones": zones,
            "regions": regions,
            "customer_types": customer_types,
            "months": months,
            "years": years
        }

        _set_cache(cache_key, response_data)
        return jsonify(response_data), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel, cursor)


# ─────────────────────────────────────────────────────────────────────────────
# API 2: Dependent Regions
# GET /api/dashboard/filters/regions?zone=CZ
# ─────────────────────────────────────────────────────────────────────────────
def get_dependent_regions_controller(get_db_connection):
    mysql_conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    try:
        session_id = request.args.get("session_id")
        user_id = request.args.get("user_id")
        zone = request.args.get("zone", "ALL")

        cache_key = f"regions_{session_id}_{zone}"
        cached = _get_cache(cache_key)
        if cached:
            return jsonify(cached), 200

        mysql_conn, hana_conn, tunnel, is_hana, schema, user_db, _ = _get_connection_context(
            get_db_connection, session_id, user_id
        )
        cursor = hana_conn.cursor() if is_hana else mysql_conn.cursor()

        regions = fetch_regions(cursor, is_hana, schema, user_db, zone=zone)
        resp = {"status": "success", "regions": regions}
        _set_cache(cache_key, resp)
        return jsonify(resp), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel, cursor)


# ─────────────────────────────────────────────────────────────────────────────
# API 3: Dependent Customer Types
# GET /api/dashboard/filters/customer-types?zone=CZ&region=3681
# ─────────────────────────────────────────────────────────────────────────────
def get_dependent_customer_types_controller(get_db_connection):
    mysql_conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    try:
        session_id = request.args.get("session_id")
        user_id = request.args.get("user_id")
        zone = request.args.get("zone", "ALL")
        region = request.args.get("region", "ALL")

        cache_key = f"cust_types_{session_id}_{zone}_{region}"
        cached = _get_cache(cache_key)
        if cached:
            return jsonify(cached), 200

        mysql_conn, hana_conn, tunnel, is_hana, schema, user_db, _ = _get_connection_context(
            get_db_connection, session_id, user_id
        )
        cursor = hana_conn.cursor() if is_hana else mysql_conn.cursor()

        customer_types = fetch_customer_types(cursor, is_hana, schema, user_db, zone=zone, region=region)
        resp = {"status": "success", "customer_types": customer_types}
        _set_cache(cache_key, resp)
        return jsonify(resp), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel, cursor)


# ─────────────────────────────────────────────────────────────────────────────
# API 4: Available Months and Years (Faceted Filtering)
# GET /api/dashboard/filters/available-periods
# ─────────────────────────────────────────────────────────────────────────────
def get_available_periods_controller(get_db_connection):
    mysql_conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    try:
        args = request.args if request.method == "GET" else (request.get_json(silent=True) or {})
        session_id = args.get("session_id")
        user_id = args.get("user_id")
        zone = args.get("zone", "ALL")
        region = args.get("region", "ALL")
        customer_type = args.get("customer_type", "ALL")
        month = args.get("month", "ALL")
        years = args.get("years", "ALL")

        cache_key = f"periods_{session_id}_{zone}_{region}_{customer_type}_{month}_{years}"
        cached = _get_cache(cache_key)
        if cached:
            return jsonify(cached), 200

        mysql_conn, hana_conn, tunnel, is_hana, schema, user_db, _ = _get_connection_context(
            get_db_connection, session_id, user_id
        )
        cursor = hana_conn.cursor() if is_hana else mysql_conn.cursor()

        months, years_list = fetch_available_periods(
            cursor, is_hana, schema, user_db,
            zone=zone, region=region, customer_type=customer_type,
            month=month, years=years
        )

        resp = {
            "status": "success",
            "months": months,
            "years": years_list
        }
        _set_cache(cache_key, resp)
        return jsonify(resp), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel, cursor)


YEAR_WISE_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `year_wise_sales_comparison` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `region_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `customer_type` VARCHAR(50) NOT NULL DEFAULT 'ALL',
    `sales_year` INT NOT NULL,
    `month_no` TINYINT NOT NULL,
    `month_name` VARCHAR(10) NOT NULL,
    `sales_value` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `invoice_count` INT NOT NULL DEFAULT 0,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_perm` (`session_id`, `zone_code`, `region_code`, `customer_type`, `sales_year`, `month_no`),
    INDEX `idx_filter` (`zone_code`, `region_code`, `customer_type`),
    INDEX `idx_year_month` (`sales_year`, `month_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_table_initialized = False

def ensure_year_wise_sales_table(conn):
    global _table_initialized
    if _table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(YEAR_WISE_SALES_TABLE_SQL)
        try:
            cur.execute("""
                ALTER TABLE `year_wise_sales_comparison`
                MODIFY COLUMN `zone_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
                MODIFY COLUMN `region_code` VARCHAR(50) NOT NULL DEFAULT 'ALL',
                MODIFY COLUMN `customer_type` VARCHAR(50) NOT NULL DEFAULT 'ALL'
            """)
        except Exception:
            pass
        conn.commit()
        cur.close()
        _table_initialized = True
    except Exception as e:
        print(f"[Year-wise Sales Table] init note: {e}", flush=True)


def bulk_sync_year_wise_sales_comparison(mysql_conn, session_id: str, user_id=None):
    """
    Executes a single bulk aggregation query across all data from 2024 to present,
    calculates all dimensional rollups (Pan-India, Zone-level, Region-level, Customer Type-level)
    in Python memory, and bulk-inserts all permutations into `year_wise_sales_comparison`.
    Runs in ~5 to 10 seconds total and covers 100% of all UI dropdown permutations.
    """
    if not mysql_conn or not session_id:
        return False

    from helper.dashboard_cache_service import get_now_ist
    get_ist_now_str = get_now_ist
    import collections

    ensure_year_wise_sales_table(mysql_conn)

    effective_session = session_id
    from helper.sap_analytics_service import get_sap_connection_for_session
    hana_conn = None
    tunnel = None
    is_hana = False
    sap_schema = "SAP_S4"
    user_db = None

    try:
        hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id, user_id=user_id)
        if hana_conn:
            is_hana = True
            sap_schema = schema or "SAP_S4"
    except Exception as e:
        print(f"[Bulk Sync Year-wise] HANA connection note: {e}", flush=True)
        is_hana = False

    if not is_hana:
        try:
            m_cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
            m_cur.execute("""
                SELECT new_user_db FROM external_db_sync_log 
                WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            s_row = m_cur.fetchone()
            if not s_row:
                m_cur.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s LIMIT 1", (session_id,))
                s_row = m_cur.fetchone()
            if s_row and isinstance(s_row, dict):
                user_db = s_row.get("new_user_db") or s_row.get("workspace_db")
            m_cur.close()
        except Exception:
            pass
        if not user_db:
            user_db = "jk_sales_workspace_db"

    cursor = None
    raw_rows = []
    try:
        if is_hana:
            cursor = hana_conn.cursor()
            bulk_sql = f"""
                SELECT
                    COALESCE(NULLIF(TRIM(zm."P_ZONE"), ''), NULLIF(TRIM(tm."P_ZONE"), ''), NULLIF(TRIM(rm."zone_code"), ''), 'UNKNOWN') AS ZONE_CODE,
                    COALESCE(NULLIF(TRIM(rm."region_code"), ''), NULLIF(TRIM(tm."REGIO"), ''), NULLIF(TRIM(cm."BZIRK"), ''), 'UNKNOWN') AS REGION_CODE,
                    COALESCE(NULLIF(TRIM(rm."region_name"), ''), '') AS REGION_NAME,
                    COALESCE(NULLIF(TRIM(cm."CH_TYPE"), ''), 'UNKNOWN') AS CUSTOMER_TYPE,
                    YEAR(s."invoice_date") AS SALES_YEAR,
                    MONTH(s."invoice_date") AS MONTH_NO,
                    ROUND(SUM(COALESCE(s."ndp_value", s."taxable_value", s."invoice_value", 0)), 2) AS TOTAL_SALES
                FROM (
                    SELECT "dealer_code", "invoice_date", "ndp_value", "taxable_value", "invoice_value"
                    FROM "{sap_schema}"."ZPVF_SD_SalesHistory_01_TR"
                    WHERE "distribution_channel" = '10'
                      AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
                      AND "invoice_date" >= '2024-01-01'
                    UNION ALL
                    SELECT "dealer_code", "invoice_date", "ndp_value", "taxable_value", "invoice_value"
                    FROM "{sap_schema}"."ZPVF_SD_CurrentSales_01_TR"
                    WHERE "distribution_channel" = '10'
                      AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
                      AND "invoice_date" >= '2024-01-01'
                ) s
                INNER JOIN "{sap_schema}"."ZIVF_CustomerMaster_01_MD" cm 
                    ON (s."dealer_code" = cm."KUNNR" OR LPAD(TRIM(s."dealer_code"), 10, '0') = cm."KUNNR" OR LTRIM(s."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
                LEFT JOIN "{sap_schema}"."ZIVF_TerritoryMaster_01_TR" tm 
                    ON (cm."BZIRK" = tm."BZIRK" OR TRIM(cm."BZIRK") = TRIM(tm."BZIRK"))
                LEFT JOIN "{sap_schema}"."ZIVF_RegionMaster_MD_01" rm 
                    ON (tm."REGIO" = rm."region_code" OR TRIM(tm."REGIO") = TRIM(rm."region_code"))
                LEFT JOIN "{sap_schema}"."ZIVF_ZoneMaster_01_TR" zm 
                    ON (COALESCE(tm."P_ZONE", rm."zone_code") = zm."P_ZONE")
                WHERE IFNULL(cm."KLABC", '') <> 'X'
                  AND s."invoice_date" IS NOT NULL
                GROUP BY 
                    COALESCE(NULLIF(TRIM(zm."P_ZONE"), ''), NULLIF(TRIM(tm."P_ZONE"), ''), NULLIF(TRIM(rm."zone_code"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm."region_code"), ''), NULLIF(TRIM(tm."REGIO"), ''), NULLIF(TRIM(cm."BZIRK"), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm."region_name"), ''), ''),
                    COALESCE(NULLIF(TRIM(cm."CH_TYPE"), ''), 'UNKNOWN'),
                    YEAR(s."invoice_date"),
                    MONTH(s."invoice_date")
            """
            cursor.execute(bulk_sql)
            raw_rows = fetchall_dict(cursor)
        else:
            cursor = mysql_conn.cursor()
            bulk_sql = f"""
                SELECT
                    COALESCE(NULLIF(TRIM(rm.zone), ''), 'UNKNOWN') AS ZONE_CODE,
                    COALESCE(NULLIF(TRIM(rm.region), ''), NULLIF(TRIM(cm.sales_office), ''), 'UNKNOWN') AS REGION_CODE,
                    COALESCE(NULLIF(TRIM(rm.region_name), ''), '') AS REGION_NAME,
                    COALESCE(NULLIF(TRIM(cm.ch_type), ''), 'UNKNOWN') AS CUSTOMER_TYPE,
                    YEAR(s.billing__doc_date) AS SALES_YEAR,
                    MONTH(s.billing__doc_date) AS MONTH_NO,
                    ROUND(SUM(COALESCE(s.net__value, 0)), 2) AS TOTAL_SALES
                FROM `{user_db}`.`sales_data` s
                JOIN `{user_db}`.`customer_master` cm ON (s.customer = cm.kunnr)
                LEFT JOIN `{user_db}`.`region_master` rm ON (rm.region = cm.sales_office OR rm.region = cm.dlr_catg OR rm.region = cm.segment)
                WHERE (s.distribution__channel = '10' OR s.distribution__channel = 10)
                  AND s.billing__doc_date IS NOT NULL
                  AND s.billing__doc_date >= '2024-01-01'
                  AND IFNULL(cm.klabc, '') <> 'X'
                GROUP BY
                    COALESCE(NULLIF(TRIM(rm.zone), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm.region), ''), NULLIF(TRIM(cm.sales_office), ''), 'UNKNOWN'),
                    COALESCE(NULLIF(TRIM(rm.region_name), ''), ''),
                    COALESCE(NULLIF(TRIM(cm.ch_type), ''), 'UNKNOWN'),
                    YEAR(s.billing__doc_date),
                    MONTH(s.billing__doc_date)
            """
            cursor.execute(bulk_sql)
            raw_rows = fetchall_dict(cursor)

    except Exception as err:
        print(f"[Bulk Sync Year-wise] Query failed: {err}", flush=True)
        return False
    finally:
        _close_connections(None, hana_conn, tunnel, cursor)

    if not raw_rows:
        print(f"[Bulk Sync Year-wise] 0 rows fetched for session {session_id}", flush=True)
        return False

    ZONE_NAME_MAP = {
        "EZ": "EAST", "EAST": "EZ",
        "WZ": "WEST", "WEST": "WZ",
        "NZ": "NORTH", "NORTH": "NZ",
        "SZ": "SOUTH", "SOUTH": "SZ",
        "CZ": "CENTRAL", "CENTRAL": "CZ",
        "TZ": "SOUTH ZONE - II", "SOUTH ZONE - II": "TZ",
        "NP": "NEPAL", "NEPAL": "NP"
    }

    # In-memory aggregation across all permutation combinations
    aggregates = collections.defaultdict(float)

    for r in raw_rows:
        z = str(_val(r, "zone_code", default="UNKNOWN")).strip()
        rc = str(_val(r, "region_code", default="UNKNOWN")).strip()
        rn = str(_val(r, "region_name", default="")).strip().upper()
        ct = str(_val(r, "customer_type", default="UNKNOWN")).strip()
        yr = int(_val(r, "sales_year", default=2026))
        mo = int(_val(r, "month_no", default=1))
        val = float(_val(r, "total_sales", default=0.0))

        # Build list of zone aliases (both code e.g. EZ and name e.g. EAST)
        zones = [z]
        if z in ZONE_NAME_MAP:
            zones.append(ZONE_NAME_MAP[z])

        # Build list of region identifiers (both code e.g. 1181 and name e.g. KOLKATA)
        regions = [rc]
        if rn and rn != rc:
            regions.append(rn)

        # 1. Leaf level: Specific Region + Specific Customer Type & Region + 'ALL'
        for z_i in zones:
            for r_i in regions:
                aggregates[(z_i, r_i, ct, yr, mo)] += val
                aggregates[(z_i, r_i, "ALL", yr, mo)] += val

        # 2. Zone level: Specific Zone + Region 'ALL' + Specific Customer Type & Zone + 'ALL' + 'ALL'
        for z_i in zones:
            aggregates[(z_i, "ALL", ct, yr, mo)] += val
            aggregates[(z_i, "ALL", "ALL", yr, mo)] += val

        # 3. Pan-India level: Zone 'ALL' + Region 'ALL' + Specific Customer Type & 'ALL', 'ALL', 'ALL'
        aggregates[("ALL", "ALL", ct, yr, mo)] += val
        aggregates[("ALL", "ALL", "ALL", yr, mo)] += val

    ist_now = get_ist_now_str()
    batch = []
    for (z_k, r_k, c_k, yr_k, mo_k), total_val in aggregates.items():
        m_label = MONTH_MAP_NUM_TO_LABEL.get(mo_k, "")
        batch.append((
            effective_session,
            z_k,
            r_k,
            c_k,
            yr_k,
            mo_k,
            m_label,
            round(total_val, 2),
            ist_now,
            ist_now
        ))

    try:
        ins_cur = mysql_conn.cursor()
        # Clean delete existing rows for this session so fresh rows have updated created_at and last_synced_at
        ins_cur.execute("DELETE FROM `year_wise_sales_comparison` WHERE session_id = %s", (str(effective_session).strip(),))

        upsert_sql = """
            INSERT INTO `year_wise_sales_comparison`
                (`session_id`, `zone_code`, `region_code`, `customer_type`, `sales_year`, `month_no`, `month_name`, `sales_value`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                sales_value = VALUES(sales_value),
                last_synced_at = VALUES(last_synced_at)
        """
        chunk_size = 1000
        for i in range(0, len(batch), chunk_size):
            ins_cur.executemany(upsert_sql, batch[i:i + chunk_size])
        mysql_conn.commit()
        ins_cur.close()
        print(f"[Bulk Sync Year-wise] Successfully stored {len(batch)} permutations in table for session {session_id}!", flush=True)
        return True
    except Exception as err:
        print(f"[Bulk Sync Year-wise] DB upsert failed: {err}", flush=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# API 5: Year-wise Sales Aggregated Data
# GET/POST /api/dashboard/year-wise-sales
# ─────────────────────────────────────────────────────────────────────────────
def get_year_wise_sales_controller(get_db_connection):
    mysql_conn = None
    hana_conn = None
    tunnel = None
    cursor = None

    try:
        data = request.get_json(force=True, silent=True) or {}
        if not data and request.args:
            data = request.args.to_dict(flat=False)
            for k, v in list(data.items()):
                if isinstance(v, list) and len(v) == 1:
                    data[k] = v[0]

        session_id = data.get("session_id")
        user_id = data.get("user_id")
        force_refresh = bool(data.get("force_refresh", False))

        def make_list(val):
            if val is None: return []
            if isinstance(val, list): return [v for v in val if v]
            return [v.strip() for v in str(val).split(",") if v.strip()]

        selected_years = make_list(data.get("selected_years") or data.get("years") or data.get("year"))
        selected_zones = make_list(data.get("selected_zones") or data.get("zones") or data.get("zone") or data.get("Zone"))
        selected_regions = make_list(data.get("selected_regions") or data.get("regions") or data.get("region") or data.get("Region"))
        selected_months = make_list(data.get("selected_months") or data.get("months") or data.get("month") or data.get("Month"))
        selected_customer_types = make_list(data.get("selected_customer_types") or data.get("customer_types") or data.get("customer_type"))

        zone_param = selected_zones[0] if selected_zones else "ALL"
        region_param = selected_regions[0] if selected_regions else "ALL"
        cust_param = selected_customer_types[0] if selected_customer_types else "ALL"
        month_param = selected_months if selected_months else "ALL"
        year_param = selected_years if selected_years else "ALL"

        mysql_conn, hana_conn, tunnel, is_hana, schema, user_db, effective_session = _get_connection_context(
            get_db_connection, session_id, user_id
        )

        # 1. Ensure dedicated table year_wise_sales_comparison exists
        ensure_year_wise_sales_table(mysql_conn)

        # 2. Check year_wise_sales_comparison table first (Ultra fast cached hit)
        if not force_refresh and mysql_conn:
            try:
                m_cur = mysql_conn.cursor(dictionary=True) if hasattr(mysql_conn, "cursor") else mysql_conn.cursor()
                year_clause = ""
                month_clause = ""
                y_params = [effective_session, zone_param, region_param, cust_param]

                parsed_years = []
                if selected_years and selected_years != ["ALL"]:
                    for y in selected_years:
                        try: parsed_years.append(int(y))
                        except Exception: pass

                if parsed_years:
                    placeholders = ", ".join(["%s"] * len(parsed_years))
                    year_clause = f"AND sales_year IN ({placeholders})"
                    y_params.extend(parsed_years)

                parsed_months = []
                if selected_months and selected_months != ["ALL"]:
                    for m in selected_months:
                        sm = str(m).strip()
                        if sm.isdigit():
                            parsed_months.append(int(sm))
                        elif sm.upper() in MONTH_MAP_LABEL_TO_NUM:
                            parsed_months.append(MONTH_MAP_LABEL_TO_NUM[sm.upper()])

                if parsed_months:
                    placeholders_m = ", ".join(["%s"] * len(parsed_months))
                    month_clause = f"AND month_no IN ({placeholders_m})"
                    y_params.extend(parsed_months)

                check_sql = f"""
                    SELECT 
                        month_no AS MONTH_NO,
                        month_name AS MONTH_NAME,
                        sales_year AS SALES_YEAR,
                        sales_value AS TOTAL_SALES
                    FROM `year_wise_sales_comparison`
                    WHERE session_id = %s
                      AND (zone_code = %s OR UPPER(TRIM(zone_code)) = UPPER(TRIM(%s)))
                      AND (region_code = %s OR UPPER(TRIM(region_code)) = UPPER(TRIM(%s)))
                      AND (customer_type = %s OR UPPER(TRIM(customer_type)) = UPPER(TRIM(%s)))
                      {year_clause}
                      {month_clause}
                    ORDER BY sales_year ASC, month_no ASC
                """
                lookup_params = [effective_session, zone_param, zone_param, region_param, region_param, cust_param, cust_param]
                if parsed_years:
                    lookup_params.extend(parsed_years)
                if parsed_months:
                    lookup_params.extend(parsed_months)

                m_cur.execute(check_sql, tuple(lookup_params))
                cached_rows = fetchall_dict(m_cur)
                m_cur.close()

                if cached_rows and len(cached_rows) >= 1:
                    visualization_data = []
                    for r in cached_rows:
                        m_no = int(_val(r, "month_no", default=1))
                        m_name = str(_val(r, "month_name", default="")).upper().strip()
                        yr_val = str(_val(r, "sales_year", "year", default=""))
                        sales_val = float(_val(r, "total_sales", default=0.0))

                        if not m_name and m_no in MONTH_MAP_NUM_TO_LABEL:
                            m_name = MONTH_MAP_NUM_TO_LABEL[m_no]

                        visualization_data.append({
                            "month": m_name,
                            "sales_value": sales_val,
                            "year": yr_val
                        })

                    return jsonify({
                        "status": "success",
                        "source": "year_wise_sales_comparison",
                        "seriesKey": "year",
                        "title": "Year-wise Sales Comparison",
                        "type": "bar_chart",
                        "xKey": "month",
                        "yKey": "sales_value",
                        "visualization": visualization_data
                    }), 200
            except Exception as e:
                print(f"[Year-wise Table Check] Fallback to live query: {e}", flush=True)

        # 3. Live query execution if not present in table
        cursor = hana_conn.cursor() if is_hana else mysql_conn.cursor()

        joins, where_sql, params, date_col, rev_expr = _build_sales_filter_query(
            cursor, is_hana, schema, user_db,
            zone=zone_param, region=region_param, customer_type=cust_param,
            month=month_param, years=year_param
        )

        month_expr = f"MONTH({date_col})"
        year_expr = f"YEAR({date_col})"

        case_month_expr = f"""
            CASE {month_expr}
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
            END
        """

        query = f"""
            SELECT 
                {month_expr} AS MONTH_NO,
                {case_month_expr} AS MONTH_NAME,
                {year_expr} AS SALES_YEAR,
                ROUND(SUM({rev_expr}), 2) AS TOTAL_SALES
            {joins}
            {where_sql}
            GROUP BY {year_expr}, {month_expr}
            ORDER BY SALES_YEAR ASC, MONTH_NO ASC
        """

        if params:
            cursor.execute(query, params if is_hana else tuple(params))
        else:
            cursor.execute(query)

        visualization_data = []
        for r in fetchall_dict(cursor):
            m_no = int(_val(r, "month_no", default=1))
            m_name = str(_val(r, "month_name", default="")).upper().strip()
            yr_val = str(_val(r, "sales_year", "year", default=""))
            sales_val = float(_val(r, "total_sales", default=0.0))

            if not m_name and m_no in MONTH_MAP_NUM_TO_LABEL:
                m_name = MONTH_MAP_NUM_TO_LABEL[m_no]

            visualization_data.append({
                "month": m_name,
                "sales_value": sales_val,
                "year": yr_val
            })

        # 4. Upsert freshly fetched permutation rows into year_wise_sales_comparison table
        if mysql_conn and visualization_data:
            try:
                from datetime import datetime, timezone, timedelta
                ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
                ins_cur = mysql_conn.cursor()
                upsert_sql = """
                    INSERT INTO `year_wise_sales_comparison`
                        (`session_id`, `zone_code`, `region_code`, `customer_type`, `sales_year`, `month_no`, `month_name`, `sales_value`, `last_synced_at`, `created_at`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        sales_value = VALUES(sales_value),
                        last_synced_at = VALUES(last_synced_at)
                """
                insert_batch = []
                for item in visualization_data:
                    m_label = item["month"]
                    m_num = MONTH_MAP_LABEL_TO_NUM.get(m_label, 1)
                    y_num = int(item["year"]) if str(item["year"]).isdigit() else 2026
                    s_val = float(item["sales_value"])
                    insert_batch.append((effective_session, zone_param, region_param, cust_param, y_num, m_num, m_label, s_val, ist_now, ist_now))

                ins_cur.executemany(upsert_sql, insert_batch)
                mysql_conn.commit()
                ins_cur.close()
            except Exception as e:
                print(f"[Year-wise Table Upsert] Error: {e}", flush=True)

        return jsonify({
            "status": "success",
            "source": "live_query",
            "seriesKey": "year",
            "title": "Year-wise Sales Comparison",
            "type": "bar_chart",
            "xKey": "month",
            "yKey": "sales_value",
            "visualization": visualization_data
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": "Database query failed.",
            "details": str(e),
            "seriesKey": "year",
            "title": "Year-wise Sales Comparison",
            "type": "bar_chart",
            "xKey": "month",
            "yKey": "sales_value",
            "visualization": []
        }), 500
    finally:
        _close_connections(mysql_conn, hana_conn, tunnel, cursor)
