
import json
import pymysql
from database.config import MYSQL_CONFIG
from helper.workspace_wise_store_procedures import run_stored_procedures
import math

# Whitelist of SAP Datasphere / HANA objects allowed to be accessed / synced
SAP_ALLOWED_DATASPEC_OBJECTS = [
    "ZIVF_CustomerMaster_01_MD",
    "ZIVR_SKUMaster_01_TR",
    "ZIVF_CategoryMaster_01_TR",
    "ZITR_DayAttribute_01_TR",
    "ZIVF_TerritoryMaster_01_TR",
    "ZPVF_SD_SalesHistory_01_TR",
    "ZPVF_SD_CurrentSales_01_TR",
    "ZIVF_ZoneMaster_01_TR",
    "ZIVF_RegionMaster_MD_01",
    "ZPVF_SD_CancelledSalesInvoice_01_TR",
    "ZIVF_SD_Sales_Plan_Target_01_TR"
]
SAP_ALLOWED_OBJECTS_SET = {name.upper() for name in SAP_ALLOWED_DATASPEC_OBJECTS}

def clean_row(row, col_types=None):
    cleaned = []
    for i, v in enumerate(row):
        if hasattr(v, 'read') and callable(v.read):
            try:
                v = v.read()
            except Exception:
                pass
        if isinstance(v, float) and math.isnan(v):
            cleaned.append(None)
        elif col_types and i < len(col_types) and col_types[i] in ['json', 'jsonb']:
            if v is None:
                cleaned.append(None)
            else:
                cleaned.append(json.dumps(v))
        elif isinstance(v, (dict, list)):
            cleaned.append(json.dumps(v))
        else:
            cleaned.append(v)
    return tuple(cleaned)

_HANA_COLS_CACHE = {}

def get_hana_columns_info(source_cursor, table_name, schema=None):
    """
    Retrieves column metadata for SAP Datasphere / HANA views and tables.
    Returns list of tuples: (column_name, data_type_name, is_nullable, length, scale)
    Note: SYS.VIEW_COLUMNS does NOT have IS_NULLABLE (views are treated as nullable).
          SYS.TABLE_COLUMNS does have IS_NULLABLE.
    """
    cache_key = (table_name.upper(), (schema or "").upper())
    if cache_key in _HANA_COLS_CACHE:
        return _HANA_COLS_CACHE[cache_key]
    if (table_name.upper(), "") in _HANA_COLS_CACHE:
        return _HANA_COLS_CACHE[(table_name.upper(), "")]

    candidate_schemas = []
    if schema:
        candidate_schemas.append(schema)
        if "#" in schema:
            candidate_schemas.append(schema.split("#")[0])
    candidate_schemas.append("SAP_S4")

    seen_s = set()
    unique_schemas = []
    for s in candidate_schemas:
        if s and s.upper() not in seen_s:
            seen_s.add(s.upper())
            unique_schemas.append(s)

    # 1. Query SYS.VIEW_COLUMNS with candidate schemas and (LENGTH, SCALE)
    for s in unique_schemas:
        try:
            source_cursor.execute("""
                SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE
                FROM SYS.VIEW_COLUMNS
                WHERE (VIEW_NAME = ? OR UPPER(VIEW_NAME) = UPPER(?))
                  AND (SCHEMA_NAME = ? OR UPPER(SCHEMA_NAME) = UPPER(?))
                ORDER BY POSITION
            """, (table_name, table_name, s, s))
            rows = source_cursor.fetchall()
            if rows:
                res = [(r[0], r[1], "TRUE", r[2], r[3]) for r in rows]
                _HANA_COLS_CACHE[cache_key] = res
                _HANA_COLS_CACHE[(table_name.upper(), "")] = res
                return res
        except Exception:
            pass

    # 2. Query SYS.VIEW_COLUMNS with candidate schemas (AWS console verified query: COLUMN_NAME, DATA_TYPE_NAME)
    for s in unique_schemas:
        try:
            source_cursor.execute("""
                SELECT COLUMN_NAME, DATA_TYPE_NAME
                FROM SYS.VIEW_COLUMNS
                WHERE (VIEW_NAME = ? OR UPPER(VIEW_NAME) = UPPER(?))
                  AND (SCHEMA_NAME = ? OR UPPER(SCHEMA_NAME) = UPPER(?))
                ORDER BY POSITION
            """, (table_name, table_name, s, s))
            rows = source_cursor.fetchall()
            if rows:
                res = [(r[0], r[1], "TRUE", None, None) for r in rows]
                _HANA_COLS_CACHE[cache_key] = res
                _HANA_COLS_CACHE[(table_name.upper(), "")] = res
                return res
        except Exception:
            pass

    # 3. Query SYS.VIEW_COLUMNS without schema restriction
    try:
        source_cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE
            FROM SYS.VIEW_COLUMNS
            WHERE (VIEW_NAME = ? OR UPPER(VIEW_NAME) = UPPER(?))
              AND SCHEMA_NAME NOT LIKE '_SYS_%'
              AND SCHEMA_NAME NOT IN ('SYS', '_SYS_REPO', '_SYS_STATISTICS', 'PUBLIC')
            ORDER BY POSITION
        """, (table_name, table_name))
        rows = source_cursor.fetchall()
        if rows:
            res = [(r[0], r[1], "TRUE", r[2], r[3]) for r in rows]
            _HANA_COLS_CACHE[cache_key] = res
            _HANA_COLS_CACHE[(table_name.upper(), "")] = res
            return res
    except Exception:
        pass

    try:
        source_cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE_NAME
            FROM SYS.VIEW_COLUMNS
            WHERE (VIEW_NAME = ? OR UPPER(VIEW_NAME) = UPPER(?))
              AND SCHEMA_NAME NOT LIKE '_SYS_%'
              AND SCHEMA_NAME NOT IN ('SYS', '_SYS_REPO', '_SYS_STATISTICS', 'PUBLIC')
            ORDER BY POSITION
        """, (table_name, table_name))
        rows = source_cursor.fetchall()
        if rows:
            res = [(r[0], r[1], "TRUE", None, None) for r in rows]
            _HANA_COLS_CACHE[cache_key] = res
            _HANA_COLS_CACHE[(table_name.upper(), "")] = res
            return res
    except Exception:
        pass

    # 4. Query SYS.TABLE_COLUMNS with candidate schemas (for base tables)
    for s in unique_schemas:
        try:
            source_cursor.execute("""
                SELECT COLUMN_NAME, DATA_TYPE_NAME, IS_NULLABLE, LENGTH, SCALE
                FROM SYS.TABLE_COLUMNS
                WHERE (TABLE_NAME = ? OR UPPER(TABLE_NAME) = UPPER(?))
                  AND (SCHEMA_NAME = ? OR UPPER(SCHEMA_NAME) = UPPER(?))
                ORDER BY POSITION
            """, (table_name, table_name, s, s))
            rows = source_cursor.fetchall()
            if rows:
                res = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]
                _HANA_COLS_CACHE[cache_key] = res
                _HANA_COLS_CACHE[(table_name.upper(), "")] = res
                return res
        except Exception:
            pass

    # 5. Query SYS.TABLE_COLUMNS without schema
    try:
        source_cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE_NAME, IS_NULLABLE, LENGTH, SCALE
            FROM SYS.TABLE_COLUMNS
            WHERE (TABLE_NAME = ? OR UPPER(TABLE_NAME) = UPPER(?))
              AND SCHEMA_NAME NOT LIKE '_SYS_%'
            ORDER BY POSITION
        """, (table_name, table_name))
        rows = source_cursor.fetchall()
        if rows:
            res = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]
            _HANA_COLS_CACHE[cache_key] = res
            _HANA_COLS_CACHE[(table_name.upper(), "")] = res
            return res
    except Exception:
        pass

    # 6. Fallback: probe SELECT * FROM ... WHERE 1=0 to retrieve column names from cursor.description
    for s in unique_schemas + [None]:
        try:
            sel_target = f'"{s}"."{table_name}"' if s else f'"{table_name}"'
            source_cursor.execute(f"SELECT * FROM {sel_target} WHERE 1=0")
            if source_cursor.description:
                res = [(d[0], "VARCHAR", "TRUE", 255, None) for d in source_cursor.description]
                _HANA_COLS_CACHE[cache_key] = res
                _HANA_COLS_CACHE[(table_name.upper(), "")] = res
                return res
        except Exception:
            pass

    return []

def get_source_column_types(source_cursor, table_name, db_type, schema='public'):
    if db_type in ["postgresql", "postgres"]:
        source_cursor.execute("""
            SELECT data_type 
            FROM information_schema.columns 
            WHERE table_name = %s AND table_schema = %s
            ORDER BY ordinal_position
        """, (table_name, schema))
        return [r[0].lower() for r in source_cursor.fetchall()]
    elif db_type == "mssql":
        source_cursor.execute("""
            SELECT DATA_TYPE 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?
            ORDER BY ORDINAL_POSITION
        """, (table_name, schema if schema else 'dbo'))
        return [r[0].lower() for r in source_cursor.fetchall()]
    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        info = get_hana_columns_info(source_cursor, table_name, schema)
        return [r[1].lower() for r in info]
    return None

def resolve_source_table(source_cursor, target_table_name, db_name, db_type, connection_schema=None):
    if db_type in ["postgresql", "postgres"]:
        prefix = f"{db_name}_"
        if target_table_name.startswith(prefix):
            remainder = target_table_name[len(prefix):]
        else:
            remainder = target_table_name

        if connection_schema:
            return remainder, connection_schema

        source_cursor.execute("""
            SELECT table_name, table_schema
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND table_type = 'BASE TABLE'
        """)
        all_tables = source_cursor.fetchall()
        for t_name, t_schema in all_tables:
            if remainder == f"{t_schema}_{t_name}":
                return t_name, t_schema
            if remainder == t_name:
                return t_name, t_schema
        
        return remainder, "public"
    elif db_type == "mssql":
        prefix = f"{db_name}_"
        if target_table_name.startswith(prefix):
            remainder = target_table_name[len(prefix):]
        else:
            remainder = target_table_name

        if connection_schema:
            return remainder, connection_schema

        source_cursor.execute("""
            SELECT TABLE_NAME, TABLE_SCHEMA
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
        """)
        all_tables = source_cursor.fetchall()
        for t_name, t_schema in all_tables:
            if remainder == f"{t_schema}_{t_name}":
                return t_name, t_schema
            if remainder == t_name:
                return t_name, t_schema
        
        return remainder, "dbo"
    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        prefix = f"{db_name}_"
        if target_table_name.startswith(prefix):
            remainder = target_table_name[len(prefix):]
        else:
            remainder = target_table_name

        for allowed in SAP_ALLOWED_DATASPEC_OBJECTS:
            if remainder.upper() == allowed.upper():
                remainder = allowed
                break

        eff_schema = connection_schema
        if eff_schema and '#' in eff_schema:
            eff_schema = eff_schema.split('#')[0]
        if eff_schema and eff_schema.upper() not in ["PUBLIC", "SYS", ""]:
            return remainder, eff_schema

        source_cursor.execute("""
            SELECT TABLE_NAME, SCHEMA_NAME
            FROM (
                SELECT TABLE_NAME, SCHEMA_NAME, IS_TEMPORARY FROM SYS.TABLES
                UNION ALL
                SELECT VIEW_NAME AS TABLE_NAME, SCHEMA_NAME, 'FALSE' AS IS_TEMPORARY FROM SYS.VIEWS
            )
            WHERE IS_TEMPORARY = 'FALSE'
              AND SCHEMA_NAME NOT LIKE '_SYS_%'
              AND SCHEMA_NAME NOT IN ('SYS', '_SYS_REPO', '_SYS_STATISTICS', '_SYS_AFL', '_SYS_BIC', '_SYS_BI', '_SYS_RT', '_SYS_DI', '_SYS_SECURITY', 'SAP_PA_APL', 'PUBLIC')
        """)
        all_tables = source_cursor.fetchall()
        for t_name, t_schema in all_tables:
            if remainder.upper() == f"{t_schema}_{t_name}".upper():
                return t_name, t_schema
            if remainder.upper() == t_name.upper():
                if not connection_schema or t_schema.upper() == connection_schema.upper() or (connection_schema and '#' in connection_schema and t_schema.upper() == connection_schema.split('#')[0].upper()):
                    return t_name, t_schema
        
        for t_name, t_schema in all_tables:
            if remainder.upper() == t_name.upper():
                return t_name, t_schema

        return remainder, eff_schema or "SAP_S4"
    elif db_type in ["mysql", "mariadb", "mysql2"]:
        return target_table_name, None
    else:
        prefix = f"{db_name}_"
        if target_table_name.startswith(prefix):
            remainder = target_table_name[len(prefix):]
        else:
            remainder = target_table_name
        return remainder, None

def postgres_to_mysql_type(data_type, char_len):
    dt = data_type.lower()
    if dt in ['integer', 'int', 'serial']:
        return 'INT'
    elif dt in ['bigint', 'bigserial']:
        return 'BIGINT'
    elif dt in ['smallint', 'smallserial']:
        return 'SMALLINT'
    elif dt in ['boolean', 'bool']:
        return 'TINYINT(1)'
    elif 'character varying' in dt or 'varchar' in dt:
        length = char_len if char_len else 255
        return f'VARCHAR({length})'
    elif 'character' in dt or 'char' in dt:
        length = char_len if char_len else 1
        return f'CHAR({length})'
    elif dt == 'text':
        return 'TEXT'
    elif 'double precision' in dt or dt == 'float8':
        return 'DOUBLE'
    elif dt in ['real', 'float4', 'float']:
        return 'FLOAT'
    elif dt in ['numeric', 'decimal']:
        return 'DECIMAL(20, 6)'
    elif 'timestamp' in dt:
        return 'DATETIME'
    elif dt == 'date':
        return 'DATE'
    elif 'time' in dt:
        return 'TIME'
    elif dt in ['json', 'jsonb']:
        return 'JSON'
    elif dt == 'bytea':
        return 'LONGBLOB'
    else:
        return 'TEXT'

def mssql_to_mysql_type(data_type, char_len=None):
    dt = data_type.lower()
    if dt in ['int', 'tinyint', 'smallint']:
        return 'INT'
    elif dt in ['bigint']:
        return 'BIGINT'
    elif dt in ['bit']:
        return 'TINYINT(1)'
    elif dt in ['varchar', 'nvarchar', 'char', 'nchar']:
        if char_len and char_len != -1:
            return f'VARCHAR({char_len})'
        else:
            return 'TEXT'
    elif dt in ['text', 'ntext']:
        return 'TEXT'
    elif dt in ['float', 'real']:
        return 'FLOAT'
    elif dt in ['decimal', 'numeric', 'money', 'smallmoney']:
        return 'DECIMAL(20, 6)'
    elif dt in ['datetime', 'datetime2', 'smalldatetime']:
        return 'DATETIME'
    elif dt == 'date':
        return 'DATE'
    elif dt == 'time':
        return 'TIME'
    elif dt in ['uniqueidentifier']:
        return 'VARCHAR(36)'
    elif dt in ['image', 'binary', 'varbinary']:
        return 'LONGBLOB'
    else:
        return 'TEXT'

def hana_to_mysql_type(data_type, char_len=None, precision=None, scale=None):
    dt = str(data_type).strip().lower()
    if any(k in dt for k in ['tinyint', 'smallint', 'integer', 'int']) and 'bigint' not in dt:
        return 'INT'
    elif 'bigint' in dt:
        return 'BIGINT'
    elif 'bool' in dt:
        return 'TINYINT(1)'
    elif any(k in dt for k in ['varchar', 'nvarchar', 'alphanum', 'shorttext', 'string', 'char']):
        try:
            cl = int(char_len) if char_len else 255
            if cl <= 255:
                return f'VARCHAR({cl})'
            elif cl <= 65535:
                return 'TEXT'
            else:
                return 'LONGTEXT'
        except Exception:
            return 'TEXT'
    elif any(k in dt for k in ['clob', 'nclob']):
        return 'LONGTEXT'
    elif any(k in dt for k in ['real', 'float']):
        return 'FLOAT'
    elif 'double' in dt:
        return 'DOUBLE'
    elif any(k in dt for k in ['decimal', 'smalldecimal', 'numeric']):
        p = precision if precision else 20
        s = scale if scale is not None else 6
        return f'DECIMAL({p}, {s})'
    elif dt == 'date':
        return 'DATE'
    elif dt == 'time':
        return 'TIME'
    elif any(k in dt for k in ['timestamp', 'seconddate']):
        return 'DATETIME'
    elif 'blob' in dt:
        return 'LONGBLOB'
    else:
        return 'TEXT'


def get_source_columns_list(source_cursor, table_name, db_type, schema='public'):
    if db_type in ["postgresql", "postgres"]:
        source_cursor.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = %s AND table_schema = %s
            ORDER BY ordinal_position
        """, (table_name, schema))
        return [r[0] for r in source_cursor.fetchall()]
    elif db_type == "mssql":
        source_cursor.execute("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?
            ORDER BY ORDINAL_POSITION
        """, (table_name, schema if schema else 'dbo'))
        return [r[0] for r in source_cursor.fetchall()]
    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        info = get_hana_columns_info(source_cursor, table_name, schema)
        return [r[0] for r in info]
    else:
        source_cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        return [c[0] for c in source_cursor.fetchall() if 'GENERATED' not in str(c[5]).upper()]

def get_quoted_columns(columns, db_type):
    if db_type in ["postgresql", "postgres", "hana", "sap_hana", "saphana", "sap"]:
        return ", ".join([f'"{c}"' for c in columns])
    elif db_type == "mssql":
        return ", ".join([f'[{c}]' for c in columns])
    else:
        return ", ".join([f'`{c}`' for c in columns])

def get_target_quoted_columns(columns):
    return ", ".join([f'`{c}`' for c in columns])

def get_source_table_select_name(table_name, db_type, schema=None):
    if db_type in ["postgresql", "postgres"]:
        if schema:
            return f'"{schema}"."{table_name}"'
        return f'"{table_name}"'
    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        if schema:
            return f'"{schema}"."{table_name}"'
        return f'"{table_name}"'
    elif db_type == "mssql":
        if schema:
            return f'[{schema}].[{table_name}]'
        return f'[{table_name}]'
    return f"`{table_name}`"

def get_source_columns(source_cursor, table_name, db_type, schema='public'):
    if db_type in ["postgresql", "postgres"]:
        source_cursor.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = %s AND table_schema = %s
            ORDER BY ordinal_position
        """, (table_name, schema))
        return [r[0] for r in source_cursor.fetchall()]
    elif db_type == "mssql":
        source_cursor.execute("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?
            ORDER BY ORDINAL_POSITION
        """, (table_name, schema if schema else 'dbo'))
        return [r[0] for r in source_cursor.fetchall()]
    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        info = get_hana_columns_info(source_cursor, table_name, schema)
        return [r[0] for r in info]
    else:
        source_cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        return [c[0] for c in source_cursor.fetchall()]

def sync_external_database(user_id, connection_id, session_id):

    # fetch credential from database_credential table
    cred_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        port=MYSQL_CONFIG["port"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"]
    )

    with cred_conn.cursor() as cursor:

        print("INPUT VALUES ->", user_id, connection_id, session_id)

        cursor.execute("SELECT user_id, connection_id, session_id FROM database_credential")
        print("DB ROWS ->", cursor.fetchall())

        cursor.execute("""
        SELECT credential, db_type
        FROM database_credential
        WHERE user_id=%s AND connection_id=%s AND session_id=%s
        """, (user_id, connection_id, session_id))

        result = cursor.fetchone()

        print("QUERY RESULT ->", result)

        if not result:
            raise Exception("Database credential not found")

        external_db = json.loads(result[0])
        db_type = result[1]

    cred_conn.close()

    db_type = db_type.strip().lower() if db_type else "mysql"

    # get username from users table first
    user_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"]
    )

    with user_conn.cursor() as cursor:
        # Fetch username from users table
        cursor.execute("SELECT email FROM users WHERE id=%s", (user_id,))
        user_result = cursor.fetchone()
        if not user_result:
            raise Exception("User not found")
        
        email = user_result[0]
        username = email.split("@")[0]

        # Fetch workspace database from workspaces table
        cursor.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s", (session_id,))
        workspace_result = cursor.fetchone()
        
        if not workspace_result or not workspace_result[0]:
            raise Exception("Workspace database not found for this session")
            
        new_user_db = workspace_result[0]

    user_conn.close()
    user_db_name = new_user_db

    if db_type in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload', 'doc_upload', 'doc_chunk_upload']:
        total_rows = 0
        total_columns = 0
        table_summary = []
        
        # For CSV/doc uploads: only return tables logged for THIS connection,
        # not ALL workspace tables (which would bleed in other sources' tables).
        try:
            log_conn_csv = pymysql.connect(
                host=MYSQL_CONFIG["host"], user=MYSQL_CONFIG["user"],
                password=MYSQL_CONFIG["password"], database=MYSQL_CONFIG["database"]
            )
            with log_conn_csv.cursor() as lc:
                lc.execute("""
                    SELECT table_name, rows_affected, total_rows, data_size_mb
                    FROM external_db_sync_log
                    WHERE session_id=%s AND new_user_db=%s
                      AND (
                        external_database IN (
                            SELECT JSON_UNQUOTE(JSON_EXTRACT(credential, '$.files[0]'))
                            FROM database_credential
                            WHERE connection_id=%s AND user_id=%s
                        )
                        OR external_database IS NULL
                      )
                    ORDER BY id ASC
                """, (session_id, user_db_name, connection_id, user_id))
                log_rows = lc.fetchall()
            log_conn_csv.close()

            if log_rows:
                # Get actual column counts from workspace DB
                ws_conn = pymysql.connect(
                    host=MYSQL_CONFIG["host"], user=MYSQL_CONFIG["user"],
                    password=MYSQL_CONFIG["password"], database=user_db_name
                )
                with ws_conn.cursor() as ws_cur:
                    for t_name, rows_affected, total_rows_log, size_mb in log_rows:
                        rows = int(total_rows_log or rows_affected or 0)
                        total_rows += rows
                        cols = 0
                        try:
                            ws_cur.execute(f"SHOW COLUMNS FROM `{t_name}`")
                            cols = len(ws_cur.fetchall())
                        except Exception:
                            pass
                        total_columns += cols
                        table_summary.append({"table": t_name, "rows": rows, "columns": cols})
                ws_conn.close()
                data_size_mb = sum(float(r[3] or 0) for r in log_rows)
            else:
                # Fallback: only return tables created by this specific CSV file
                cred_conn = pymysql.connect(
                    host=MYSQL_CONFIG["host"], user=MYSQL_CONFIG["user"],
                    password=MYSQL_CONFIG["password"], database=MYSQL_CONFIG["database"]
                )
                with cred_conn.cursor() as cc:
                    cc.execute("SELECT credential FROM database_credential WHERE connection_id=%s AND user_id=%s",
                               (connection_id, user_id))
                    cred_row = cc.fetchone()
                cred_conn.close()
                csv_file = None
                if cred_row:
                    import json as _json
                    cred_data = _json.loads(cred_row[0])
                    files = cred_data.get("files", [])
                    if files:
                        csv_file = files[0]

                target_conn = pymysql.connect(
                    host=MYSQL_CONFIG["host"], user=MYSQL_CONFIG["user"],
                    password=MYSQL_CONFIG["password"], database=user_db_name, autocommit=True
                )
                if db_type in ['doc_upload', 'doc_chunk_upload']:
                    import time
                    for _ in range(150):
                        with target_conn.cursor() as cursor:
                            cursor.execute("SHOW TABLES")
                            current_tables = [t[0] for t in cursor.fetchall()]
                        if "workspace_files" in current_tables:
                            time.sleep(2)
                            break
                        time.sleep(2)

                with target_conn.cursor() as cursor:
                    cursor.execute("SHOW TABLES")
                    all_tables = [t[0] for t in cursor.fetchall()]
                    # Only include tables whose name matches this CSV file pattern
                    if csv_file:
                        import re as _re
                        csv_base = _re.sub(r'[^a-z0-9]', '_', csv_file.lower().rsplit('.', 1)[0])
                        tables = [t for t in all_tables if t.lower().startswith(csv_base[:20])]
                    else:
                        tables = []
                    for t in tables:
                        cursor.execute(f"SELECT COUNT(*) FROM `{t}`")
                        row_count = cursor.fetchone()[0]
                        cursor.execute(f"SHOW COLUMNS FROM `{t}`")
                        col_count = len(cursor.fetchall())
                        total_rows += row_count
                        total_columns += col_count
                        table_summary.append({"table": t, "rows": row_count, "columns": col_count})
                    cursor.execute("""
                        SELECT SUM(data_length + index_length)
                        FROM information_schema.tables WHERE table_schema = %s
                    """, (user_db_name,))
                    size_result = cursor.fetchone()[0]
                    data_size_mb = round((size_result or 0) / (1024 * 1024), 2)
                target_conn.close()
        except Exception as e:
            print("Error fetching tables for file upload:", e)
            data_size_mb = 0.0

        return {
            "summary": {
                "total_rows": total_rows,
                "total_columns": total_columns,
                "data_size_mb": data_size_mb,
                "last_sync": "Just now"
            },
            "tables": table_summary,
            "situations": [],
            "new_tables": [t["table"] for t in table_summary]
        }


    if db_type == 'ftp':
        import ftplib
        import os
        import uuid
        import pandas as pd
        from database.config import UPLOAD_FOLDER
        
        host       = external_db.get("host", "")
        port       = int(external_db.get("port", 21))
        username   = external_db.get("username", "")
        password   = external_db.get("password", "")
        remote_dir = external_db.get("remote_dir", "/")
        passive    = external_db.get("passive_mode", True)
        
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)
        if passive:
            ftp.set_pasv(True)
            
        ftp.cwd(remote_dir)
        file_list = []
        
        def _parse_list(line):
            parts = line.split()
            if len(parts) >= 9:
                name = " ".join(parts[8:])
                size = parts[4] if parts[4].isdigit() else "0"
                is_dir = parts[0].startswith("d")
                file_list.append({"name": name, "size": int(size), "is_dir": is_dir})
        
        ftp.retrlines("LIST", _parse_list)
        files = [f for f in file_list if f.get("name") and not f.get("is_dir")]
        
        csv_files = []
        sql_files = []
        table_summary = []
        total_rows = 0
        total_columns = 0
        total_size_bytes = 0
        
        for file_info in files:
            fname = file_info["name"]
            local_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_{fname}")
            
            with open(local_path, "wb") as f:
                ftp.retrbinary(f"RETR {fname}", f.write)
                
            size_bytes = os.path.getsize(local_path)
            total_size_bytes += size_bytes
            
            rows = 0
            cols = 0
            
            if fname.lower().endswith(".csv"):
                csv_files.append(local_path)
                try:
                    df_chunk = pd.read_csv(local_path, nrows=0, encoding="latin1", on_bad_lines="skip")
                    cols = len(df_chunk.columns)
                    with open(local_path, encoding="latin1") as f_csv:
                        rows = sum(1 for _ in f_csv) - 1
                        if rows < 0: rows = 0
                except:
                    pass
            elif fname.lower().endswith(".sql"):
                sql_files.append(local_path)
            
            total_rows += rows
            total_columns += cols
            
            table_summary.append({
                "table": fname,
                "rows": rows,
                "columns": cols
            })
            
        ftp.quit()

        # Log to external_db_sync_log
        log_conn = pymysql.connect(
            host=MYSQL_CONFIG["host"],
            user=MYSQL_CONFIG["user"],
            password=MYSQL_CONFIG["password"],
            database=MYSQL_CONFIG["database"]
        )
        with log_conn.cursor() as cursor:
            for summary in table_summary:
                cursor.execute(f"""
                    INSERT INTO `{MYSQL_CONFIG["database"]}`.external_db_sync_log
                    (user_id, new_user_db, username, external_database, table_name, action_type, rows_affected, session_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    user_id,
                    user_db_name,
                    username,
                    external_db.get("name", "FTP Connector"),
                    summary["table"],
                    "NEW_TABLE",
                    summary["rows"],
                    session_id
                ))
            log_conn.commit()
        log_conn.close()
        
        # Schedule the background processing
        if user_db_name:
            from controllers.uploads_controller import scheduler as up_scheduler
            from database.csv_processor import process_csv_job
            from database.sql_processor import process_sql_job
            
            db_user = MYSQL_CONFIG.get("user")
            db_pass = MYSQL_CONFIG.get("password")
            db_host = MYSQL_CONFIG.get("host")
            db_port = int(MYSQL_CONFIG.get("port", 3306))

            if csv_files:
                up_scheduler.add_job(
                    func=process_csv_job,
                    args=[csv_files, user_db_name, db_host, db_user, db_pass, db_port],
                    trigger='date',
                    id=str(uuid.uuid4()),
                    replace_existing=True
                )
                
            if sql_files:
                up_scheduler.add_job(
                    func=process_sql_job,
                    args=[sql_files, user_db_name, db_host, db_user, db_pass, db_port],
                    trigger='date',
                    id=str(uuid.uuid4()),
                    replace_existing=True
                )
        
        data_size_mb = round(total_size_bytes / (1024 * 1024), 2)
        
        return {
            "summary": {
                "total_rows": total_rows,
                "total_columns": total_columns,
                "data_size_mb": data_size_mb,
                "last_sync": "Just now"
            },
            "tables": table_summary,
            "situations": [],
            "new_tables": [t["table"] for t in table_summary]
        }

    if "port" not in external_db or not str(external_db.get("port", "")).strip():
        external_db["port"] = "5432" if "postgres" in db_type else "3306"

    if db_type == 'tally' or db_type == 'ftp':
        required_fields = ["host", "port", "database"] if db_type == 'tally' else ["host", "port", "username", "password"]
    elif db_type == 'mssql':
        required_fields = ["host", "port", "database"]
    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        required_fields = ["host", "port", "username", "password"]
    else:
        required_fields = ["host", "port", "username", "password", "database"]
    for field in required_fields:
        if field not in external_db or not external_db[field]:
            raise Exception(f"Missing external DB field: {field}")

    # check if first sync
    log_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"]
    )

    with log_conn.cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(*)
            FROM external_db_sync_log
            WHERE username=%s AND external_database=%s AND session_id=%s
        """, (username, external_db.get("database") or external_db.get("schema") or "hana", session_id))

        sync_count = cursor.fetchone()[0]

    log_conn.close()

    first_sync = sync_count == 0
    tunnel = None
    
    if db_type == 'tally':
        from database.tally_connector import sync_tally_database
        return sync_tally_database(user_id, connection_id, session_id, external_db, user_db_name, username)

    elif db_type in ["postgresql", "postgres"]:
        import psycopg2
        
        tunnel = None
        db_host = external_db["host"]
        db_port = external_db.get("port", 5432)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        source_conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            user=external_db["username"],
            password=external_db["password"],
            database=external_db["database"]
        )
        source_conn.autocommit = True
    elif db_type == 'mssql':
        import pyodbc
        tunnel = None
        db_host = external_db["host"]
        db_port = external_db.get("port", 1433)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        drivers = pyodbc.drivers()
        import platform
        if platform.system() == "Windows":
            preferred_drivers = ["ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server", "SQL Server"]
        else:
            preferred_drivers = ["FreeTDS", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server", "SQL Server"]
        selected_driver = None
        for p in preferred_drivers:
            if p in drivers:
                selected_driver = p
                break
        if not selected_driver:
            selected_driver = drivers[0] if drivers else "SQL Server"
            
        driver_escaped = selected_driver.replace(" ", "+")
        if external_db.get("username"):
            conn_str = f"DRIVER={{{selected_driver}}};SERVER={db_host},{db_port};DATABASE={external_db['database']};UID={external_db['username']};PWD={external_db.get('password', '')};Encrypt=no;TrustServerCertificate=yes"
        else:
            conn_str = f"DRIVER={{{selected_driver}}};SERVER={db_host},{db_port};DATABASE={external_db['database']};Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes"
        if selected_driver == "FreeTDS":
            conn_str += ";TDS_Version=7.4"
            
        source_conn = pyodbc.connect(conn_str, autocommit=True)
    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        from hdbcli import dbapi
        tunnel = None
        db_host = external_db["host"]
        db_port = int(external_db.get("port", 30015))
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        is_hana_cloud = ("hanacloud.ondemand.com" in str(db_host).lower()) or (db_port == 443)
        connect_kwargs = {
            "address": db_host,
            "port": db_port,
            "user": external_db["username"],
            "password": external_db["password"],
            "autocommit": True
        }
        if external_db.get("database") and not is_hana_cloud:
            connect_kwargs["databaseName"] = external_db["database"]
        
        effective_schema = external_db.get("schema") or (external_db.get("database") if is_hana_cloud else None)
        if effective_schema:
            connect_kwargs["currentSchema"] = effective_schema

        if external_db.get("encrypt") or is_hana_cloud:
            connect_kwargs["encrypt"] = True
            connect_kwargs["sslValidateCertificate"] = False

        try:
            source_conn = dbapi.connect(**connect_kwargs)
        except Exception as e:
            if "not connected" in str(e).lower() and "databaseName" in connect_kwargs:
                fallback_db = connect_kwargs.pop("databaseName", None)
                if not connect_kwargs.get("currentSchema") and fallback_db:
                    connect_kwargs["currentSchema"] = fallback_db
                source_conn = dbapi.connect(**connect_kwargs)
            else:
                raise e
    else:
        tunnel = None
        db_host = external_db["host"]
        db_port = external_db.get("port", 3306)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        source_conn = pymysql.connect(
            host=db_host,
            port=db_port,
            user=external_db["username"],
            password=external_db["password"],
            database=external_db["database"],
            autocommit=True
        )

    target_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=user_db_name,
        autocommit=True
    )

    new_tables = []
    updated_tables = []
    situations = []
    table_summary = []
    total_rows = 0
    total_columns = 0

    try:
        with source_conn.cursor() as source_cursor, target_conn.cursor() as target_cursor:

            log_query = f"""
            INSERT INTO `{MYSQL_CONFIG["database"]}`.external_db_sync_log
            (user_id, new_user_db ,username, external_database, table_name, action_type, rows_affected, session_id, data_size_mb, exact_size_mb, total_rows, total_columns, sync_time)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
            """

            schema_name = external_db.get("schema")
            if db_type in ["hana", "sap_hana", "saphana", "sap"] and not schema_name:
                schema_name = external_db.get("schema") or (external_db.get("database") if is_hana_cloud else None) or external_db.get("database") or external_db.get("username")

            if db_type == "tally":
                source_tables = [("Ledger", None), ("Voucher", None), ("Company", None)]
                   
            elif db_type in ["postgresql", "postgres"]:
                if schema_name:
                    source_cursor.execute(f"SET search_path TO {schema_name}")
                    source_cursor.execute("""
                        SELECT table_name, %s::varchar as table_schema
                        FROM information_schema.tables 
                        WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    """, (schema_name, schema_name))
                else:
                    source_cursor.execute("""
                        SELECT table_name, table_schema
                        FROM information_schema.tables 
                        WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast') 
                          AND table_type = 'BASE TABLE'
                    """)
                source_tables = [(r[0], r[1]) for r in source_cursor.fetchall()]
            elif db_type == 'mssql':
                source_cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
                source_tables = [(t[0], None) for t in source_cursor.fetchall()]
            elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                # Build prioritized candidate schemas with SAP_S4 prominently featured
                cand_schemas = []
                if schema_name:
                    cand_schemas.append(schema_name)
                    if "#" in schema_name:
                        cand_schemas.append(schema_name.split("#")[0])
                if external_db.get("username") and "#" in external_db.get("username"):
                    cand_schemas.append(external_db["username"].split("#")[0])
                cand_schemas.append("SAP_S4")

                unique_cand_schemas = []
                for cs in cand_schemas:
                    if cs and cs.upper() not in [u.upper() for u in unique_cand_schemas]:
                        unique_cand_schemas.append(cs)

                placeholders = ", ".join(["?"] * len(SAP_ALLOWED_DATASPEC_OBJECTS))
                upper_allowed = [name.upper() for name in SAP_ALLOWED_DATASPEC_OBJECTS]

                source_tables = []
                # 1. Query SYS.VIEWS with prioritized candidate schemas in a single fast query
                schema_placeholders = ", ".join(["?"] * len(unique_cand_schemas))
                schema_args = [s for s in unique_cand_schemas] + [s.upper() for s in unique_cand_schemas]
                try:
                    source_cursor.execute(f"""
                        SELECT VIEW_NAME AS TABLE_NAME, SCHEMA_NAME
                        FROM SYS.VIEWS
                        WHERE (SCHEMA_NAME IN ({schema_placeholders}) OR UPPER(SCHEMA_NAME) IN ({schema_placeholders}))
                          AND UPPER(VIEW_NAME) IN ({placeholders})
                        ORDER BY VIEW_NAME
                    """, schema_args + upper_allowed)
                    source_tables = [(r[0], r[1]) for r in source_cursor.fetchall()]
                except Exception as _hana_err:
                    print(f"[HANA-DISCOVERY] Fast view query error: {_hana_err}")

                # 2. If some allowed objects were not found in SYS.VIEWS, search without schema restriction
                found_uppers = {t[0].upper() for t in source_tables}
                if len(found_uppers) < len(SAP_ALLOWED_OBJECTS_SET):
                    missing_allowed = [n for n in upper_allowed if n not in found_uppers]
                    missing_placeholders = ", ".join(["?"] * len(missing_allowed))
                    try:
                        source_cursor.execute(f"""
                            SELECT VIEW_NAME AS TABLE_NAME, SCHEMA_NAME
                            FROM SYS.VIEWS
                            WHERE SCHEMA_NAME NOT LIKE '_SYS_%'
                              AND SCHEMA_NAME NOT IN ('SYS', '_SYS_REPO', '_SYS_STATISTICS', 'PUBLIC')
                              AND UPPER(VIEW_NAME) IN ({missing_placeholders})
                        """, missing_allowed)
                        for r in source_cursor.fetchall():
                            if r[0].upper() not in found_uppers:
                                source_tables.append((r[0], r[1]))
                                found_uppers.add(r[0].upper())
                    except Exception:
                        pass

                # Deduplicate and ensure ONLY allowed SAP objects are retained
                seen_objs = set()
                deduped = []
                for t_name, s_name in source_tables:
                    t_upper = t_name.upper()
                    if t_upper in SAP_ALLOWED_OBJECTS_SET and t_upper not in seen_objs:
                        seen_objs.add(t_upper)
                        deduped.append((t_name, s_name or "SAP_S4"))
                source_tables = deduped
            else:
                source_cursor.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
                source_tables = [(t[0], None) for t in source_cursor.fetchall()]

            target_cursor.execute("SHOW TABLES")
            target_tables = [t[0] for t in target_cursor.fetchall()]

            # Disable FK
            target_cursor.execute("SET FOREIGN_KEY_CHECKS=0")

            for table_name, schema_name_from_db in source_tables:
                if db_type in ["postgresql", "postgres"] and schema_name_from_db:
                    source_cursor.execute(f"SET search_path TO {schema_name_from_db}")
                elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                    active_target_schema = schema_name_from_db or schema_name or "SAP_S4"
                    if active_target_schema and "#" in active_target_schema:
                        active_target_schema = active_target_schema.split("#")[0]
                    if active_target_schema:
                        try:
                            source_cursor.execute(f'SET SCHEMA "{active_target_schema}"')
                        except Exception:
                            pass
                
                active_schema = schema_name_from_db if schema_name_from_db else schema_name
                if db_type in ["hana", "sap_hana", "saphana", "sap"]:
                    if not active_schema or active_schema.upper() in ["PUBLIC", "SYS", ""] or "#" in active_schema:
                        if active_schema and "#" in active_schema:
                            active_schema = active_schema.split("#")[0]
                        else:
                            active_schema = "SAP_S4"

                # Pre-fetch columns and types upfront once per table!
                if db_type in ["hana", "sap_hana", "saphana", "sap"]:
                    hana_cols_info = get_hana_columns_info(source_cursor, table_name, active_schema)
                    columns = [r[0] for r in hana_cols_info]
                    col_count = len(columns)
                    col_list = columns
                    col_types = [r[1].lower() if r[1] else "varchar" for r in hana_cols_info]
                else:
                    hana_cols_info = None
                    columns = get_source_columns(source_cursor, table_name, db_type, active_schema)
                    col_count = len(columns)
                    col_list = get_source_columns_list(source_cursor, table_name, db_type, active_schema)
                    col_types = get_source_column_types(source_cursor, table_name, db_type, active_schema)

                # count rows
                source_cursor.execute(f"SELECT COUNT(*) FROM {get_source_table_select_name(table_name, db_type, active_schema)}")
                row_count = source_cursor.fetchone()[0]

                total_rows += row_count
                total_columns += col_count

                db_label = external_db.get("database") or active_schema or "hana"
                if db_type in ["postgresql", "postgres"] and not schema_name:
                    table_display_name = f"{db_label}.{active_schema}.{table_name}"
                elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                    table_display_name = table_name
                else:
                    table_display_name = f"{db_label}.{table_name}"

                table_summary.append({
                    "table": table_display_name,
                    "rows": row_count,
                    "columns": col_count
                })

                if db_type in ["postgresql", "postgres"] and not schema_name:
                    new_table_name = f"{db_label}_{active_schema}_{table_name}"
                elif db_type in ["mysql", "mariadb", "mysql2", "mssql", "hana", "sap_hana", "saphana", "sap"]:
                    new_table_name = table_name
                else:
                    new_table_name = f"{db_label}_{table_name}"

                # Auto-prefix physical workspace name on conflict so two different
                # sources can both have a "users" table without overwriting each other.
                # The LOG always stores the original source table_name for clean display.
                target_tables_lower = [t.lower() for t in target_tables]
                if new_table_name.lower() in target_tables_lower and first_sync and db_type not in ["hana", "sap_hana", "saphana", "sap"]:
                    # Give this table a prefixed physical name in the workspace
                    new_table_name = f"{external_db.get('database') or 'hana'}_{table_name}"


                # ---------- NEW TABLE ----------
                target_tables_lower = [t.lower() for t in target_tables]
                if new_table_name.lower() not in target_tables_lower:

                    new_tables.append(new_table_name)

                    if db_type in ["postgresql", "postgres"]:
                        source_cursor.execute("""
                            SELECT column_name, data_type, is_nullable, character_maximum_length
                            FROM information_schema.columns
                            WHERE table_name = %s AND table_schema = %s
                            ORDER BY ordinal_position
                        """, (table_name, active_schema))
                        cols_info = source_cursor.fetchall()
                        col_defs = []
                        for col_name, data_type, is_nullable, char_len in cols_info:
                            mysql_type = postgres_to_mysql_type(data_type, char_len)
                            null_def = "NULL" if is_nullable == "YES" else "NOT NULL"
                            col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                        create_query = f"CREATE TABLE `{new_table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                    elif db_type == "mssql":
                        source_cursor.execute("""
                            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH
                            FROM INFORMATION_SCHEMA.COLUMNS
                            WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?
                            ORDER BY ORDINAL_POSITION
                        """, (table_name, active_schema if active_schema else 'dbo'))
                        cols_info = source_cursor.fetchall()
                        col_defs = []
                        for col_name, data_type, is_nullable, char_len in cols_info:
                            mysql_type = mssql_to_mysql_type(data_type, char_len)
                            null_def = "NULL" if is_nullable == "YES" else "NOT NULL"
                            col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                        create_query = f"CREATE TABLE `{new_table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                        col_defs = []
                        for col_name, data_type, is_nullable, char_len, scale in hana_cols_info:
                            mysql_type = hana_to_mysql_type(data_type, char_len, char_len, scale)
                            null_def = "NULL" if str(is_nullable).upper() in ["TRUE", "YES", "1"] else "NOT NULL"
                            col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                        create_query = f"CREATE TABLE `{new_table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                    else:
                        source_cursor.execute(f"SHOW CREATE TABLE `{table_name}`")
                        create_query = source_cursor.fetchone()[1]

                        create_query = create_query.replace(
                            f"CREATE TABLE `{table_name}`",
                            f"CREATE TABLE `{new_table_name}`"
                        )

                    target_cursor.execute(create_query)

                    source_col_str = get_quoted_columns(col_list, db_type)
                    target_col_str = get_target_quoted_columns(col_list)

                    source_cursor.execute(f"SELECT {source_col_str} FROM {get_source_table_select_name(table_name, db_type, active_schema)}")

                    BATCH_SIZE = 5000
                    inserted_count = 0
                    insert_query = None

                    while True:
                        b_rows = source_cursor.fetchmany(BATCH_SIZE)
                        if not b_rows:
                            break
                        if insert_query is None:
                            placeholders = ", ".join(["%s"] * len(b_rows[0]))
                            insert_query = f"""
                            INSERT INTO `{new_table_name}` ({target_col_str})
                            VALUES ({placeholders})
                            """
                        cleaned_b = [clean_row(row, col_types) for row in b_rows]
                        target_cursor.executemany(insert_query, cleaned_b)
                        inserted_count += len(b_rows)

                    target_cursor.execute(f"""
                        SELECT (data_length + index_length) 
                        FROM information_schema.tables 
                        WHERE table_schema = '{new_user_db}' AND table_name = '{new_table_name}'
                    """)
                    table_size_bytes = target_cursor.fetchone()
                    table_size_bytes = table_size_bytes[0] if table_size_bytes and table_size_bytes[0] else 0
                    data_size_mb = round(table_size_bytes / (1024 * 1024), 2)
                    exact_size_mb = round(table_size_bytes / (1024 * 1024), 6)

                    #  LOG INSERT — always use original source table_name for clean display
                    # Delete any stale entries from previous failed syncs for this table
                    try:
                        _del_conn = pymysql.connect(
                            host=MYSQL_CONFIG["host"], user=MYSQL_CONFIG["user"],
                            password=MYSQL_CONFIG["password"], database=MYSQL_CONFIG["database"]
                        )
                        with _del_conn.cursor() as _dc:
                            _dc.execute("""
                                DELETE FROM external_db_sync_log
                                WHERE session_id=%s AND external_database=%s AND table_name=%s
                            """, (session_id, external_db.get("database") or external_db.get("schema") or "hana", table_name))
                        _del_conn.commit()
                        _del_conn.close()
                    except Exception as _del_e:
                        print(f"[LOG-CLEANUP] Could not delete stale log for {table_name}: {_del_e}")

                    target_cursor.execute(
                        log_query,
                        (   user_id,
                            new_user_db,
                            username,
                            external_db.get("database") or external_db.get("schema") or "hana",
                            table_name,      # original source name (not physical new_table_name)
                            "NEW_TABLE",
                            inserted_count,
                            session_id,
                            data_size_mb,
                            exact_size_mb,
                            inserted_count,
                            col_count
                        )
                    )

                else:
                    # EXISTING TABLE branch
                    # On first sync: this same table name existed from another source.
                    # Drop and recreate so that MSSQL data wins cleanly.
                    if first_sync:
                        target_cursor.execute(f"DROP TABLE IF EXISTS `{new_table_name}`")

                        if db_type in ["postgresql", "postgres"]:
                            source_cursor.execute("""
                                SELECT column_name, data_type, is_nullable, character_maximum_length
                                FROM information_schema.columns
                                WHERE table_name = %s AND table_schema = %s
                                ORDER BY ordinal_position
                            """, (table_name, active_schema))
                            cols_info = source_cursor.fetchall()
                            col_defs = []
                            for col_name, data_type, is_nullable, char_len in cols_info:
                                mysql_type = postgres_to_mysql_type(data_type, char_len)
                                null_def = "NULL" if is_nullable == "YES" else "NOT NULL"
                                col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                            create_query = f"CREATE TABLE `{new_table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                        elif db_type == "mssql":
                            source_cursor.execute("""
                                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH
                                FROM INFORMATION_SCHEMA.COLUMNS
                                WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ?
                                ORDER BY ORDINAL_POSITION
                            """, (table_name, active_schema if active_schema else 'dbo'))
                            cols_info = source_cursor.fetchall()
                            col_defs = []
                            for col_name, data_type, is_nullable, char_len in cols_info:
                                mysql_type = mssql_to_mysql_type(data_type, char_len)
                                null_def = "NULL" if is_nullable == "YES" else "NOT NULL"
                                col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                            create_query = f"CREATE TABLE `{new_table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                        elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                            col_defs = []
                            for col_name, data_type, is_nullable, char_len, scale in hana_cols_info:
                                mysql_type = hana_to_mysql_type(data_type, char_len, char_len, scale)
                                null_def = "NULL" if str(is_nullable).upper() in ["TRUE", "YES", "1"] else "NOT NULL"
                                col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                            create_query = f"CREATE TABLE `{new_table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                        else:
                            source_cursor.execute(f"SHOW CREATE TABLE `{table_name}`")
                            create_query = source_cursor.fetchone()[1].replace(
                                f"CREATE TABLE `{table_name}`",
                                f"CREATE TABLE `{new_table_name}`"
                            )

                        target_cursor.execute(create_query)
                        new_tables.append(new_table_name)

                        source_col_str = get_quoted_columns(col_list, db_type)
                        target_col_str = get_target_quoted_columns(col_list)
                        source_cursor.execute(f"SELECT {source_col_str} FROM {get_source_table_select_name(table_name, db_type, active_schema)}")

                        BATCH_SIZE = 5000
                        inserted_count = 0
                        insert_query = None

                        while True:
                            b_rows = source_cursor.fetchmany(BATCH_SIZE)
                            if not b_rows:
                                break
                            if insert_query is None:
                                placeholders = ", ".join(["%s"] * len(b_rows[0]))
                                insert_query = f"INSERT INTO `{new_table_name}` ({target_col_str}) VALUES ({placeholders})"
                            cleaned_b = [clean_row(row, col_types) for row in b_rows]
                            target_cursor.executemany(insert_query, cleaned_b)
                            inserted_count += len(b_rows)

                        target_cursor.execute(f"""
                            SELECT (data_length + index_length)
                            FROM information_schema.tables
                            WHERE table_schema = '{new_user_db}' AND table_name = '{new_table_name}'
                        """)
                        table_size_bytes = target_cursor.fetchone()
                        table_size_bytes = table_size_bytes[0] if table_size_bytes and table_size_bytes[0] else 0
                        data_size_mb = round(table_size_bytes / (1024 * 1024), 2)
                        exact_size_mb = round(table_size_bytes / (1024 * 1024), 6)

                        target_cursor.execute(
                            log_query,
                            (user_id, new_user_db, username, external_db.get("database") or external_db.get("schema") or "hana",
                             new_table_name, "NEW_TABLE", inserted_count, session_id,
                             data_size_mb, exact_size_mb, inserted_count, col_count)
                        )
                    else:
                        # check new columns
                        source_columns = col_list
                        target_cursor.execute(f"SHOW COLUMNS FROM `{new_table_name}`")
                        target_columns = [c[0] for c in target_cursor.fetchall()]
                        new_columns = set(source_columns) - set(target_columns)

                        if new_columns:
                            if not any(s["table"] == new_table_name and s["type"]=="SCHEMA_CHANGE" for s in situations):
                                situations.append({
                                    "type": "SCHEMA_CHANGE",
                                    "table": new_table_name,
                                    "message": f"New columns detected in {new_table_name}: {', '.join(new_columns)}",
                                    "buttons": ["Yes", "No"]
                                })

                        source_cursor.execute(f"SELECT COUNT(*) FROM {get_source_table_select_name(table_name, db_type, active_schema)}")
                        source_count = source_cursor.fetchone()[0]
                        target_cursor.execute(f"SELECT COUNT(*) FROM `{new_table_name}`")
                        target_count = target_cursor.fetchone()[0]

                        if source_count > target_count:
                            if not any(s["table"] == new_table_name for s in situations):
                                situations.append({
                                    "type": "DATA_DISCREPANCY",
                                    "table": new_table_name,
                                    "message": f"I found some discrepancy in table {new_table_name}. I'm doing reconciliation.",
                                    "buttons": ["Yes", "No"]
                                })

            # Get actual data size in bytes for the imported tables from information_schema
            target_cursor.execute(f"""
                SELECT SUM(data_length + index_length) 
                FROM information_schema.tables 
                WHERE table_schema = '{new_user_db}'
            """)
            actual_size = target_cursor.fetchone()[0] or 0
            data_size_mb = round(actual_size / (1024 * 1024), 2)


            # Enable FK
            target_cursor.execute("SET FOREIGN_KEY_CHECKS=1")

            if not situations and not new_tables:
                pass

    except Exception as e:

        situations.append({
            "type": "FAILED_ATTEMPT",
            "message": "I see that my last attempt failed. I'm trying again.",
            "buttons": ["Retry", "No"]
        })

        raise e

    finally:
        if 'source_conn' in locals() and source_conn:
            try: source_conn.close()
            except: pass
        if 'target_conn' in locals() and target_conn:
            try: target_conn.close()
            except: pass
        if 'tunnel' in locals() and tunnel:
            try: tunnel.stop()
            except: pass

    return {
        "summary": {
            "total_rows": total_rows,
            "total_columns": total_columns,
            "data_size_mb": data_size_mb,
            "last_sync": "Just now"
        },
        "tables": table_summary,
        "situations": situations,
        "new_tables": new_tables
    }

 
def apply_external_sync(user_id, connection_id, session_id, table):
    tunnel = None
    # fetch credential from database_credential table
    cred_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"]
    )

    with cred_conn.cursor() as cursor:
        cursor.execute("""
            SELECT credential, db_type
            FROM database_credential
            WHERE user_id=%s AND connection_id=%s AND session_id=%s
        """, (user_id, connection_id, session_id))

        result = cursor.fetchone()

        if not result:
            raise Exception("Database credential not found")

        external_db = json.loads(result[0])
        db_type = result[1]

    cred_conn.close()

    db_type = db_type.lower() if db_type else "mysql"

    # get username from users table
    user_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"]
    )

    with user_conn.cursor() as cursor:
        # Fetch username from users table
        cursor.execute("SELECT email FROM users WHERE id=%s", (user_id,))
        user_result = cursor.fetchone()
        if not user_result:
            raise Exception("User not found")
        
        email = user_result[0]
        username = email.split("@")[0]

        # Fetch workspace database from workspaces table
        cursor.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s", (session_id,))
        workspace_result = cursor.fetchone()
        
        if not workspace_result or not workspace_result[0]:
            raise Exception("Workspace database not found for this session")
            
        new_user_db = workspace_result[0]

    user_conn.close()

    user_db_name = new_user_db

    # ensure user database exists
    db_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"]
    )

    with db_conn.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{user_db_name}`")

    # Create stored procedures for this database
    try:
        # run_stored_procedures(user_db_name)
        pass
    except Exception as e:
        print(f"Error creating stored procedures for {user_db_name}: {e}")

    db_conn.close()

    if db_type in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload', 'doc_upload', 'doc_chunk_upload']:
        return
    if db_type == 'tally':
        # Logic to insert Tally's XML data directly into MySQL
        from database.tally_connector import fetch_tally_data
        import json
        
        target_host = external_db.get("host")
        port = external_db.get("port")
        company_name = external_db.get("database")
        
        # Fetching XML from Tally 
        ledger_data = fetch_tally_data(target_host, port, company_name, "Ledger")
        print("Tally Data Fetched Successfully")
        
        # Creating MySQL connection (for the database where the user wants to save)
        target_conn = pymysql.connect(
            host=MYSQL_CONFIG["host"],
            user=MYSQL_CONFIG["user"],
            password=MYSQL_CONFIG["password"],
            database=user_db_name,
            autocommit=True
        )
        
        try:
            with target_conn.cursor() as target_cursor:
                # 1. Extracting LEDGER list from XML dictionary
                collection = ledger_data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
                ledgers = collection.get("LEDGER", [])
                
                if isinstance(ledgers, dict):
                    ledgers = [ledgers]
                    
                if not ledgers:
                    print("No ledgers found in Tally response.")
                    return
                
                # 2. Extracting dynamic column names
                all_columns = set()
                for row in ledgers:
                    if isinstance(row, dict):
                        all_columns.update(row.keys())
                
                if "@attributes" in all_columns:
                    all_columns.remove("@attributes")
                    
                all_columns = list(all_columns)
                
                # 3. Creating MySQL table
                table_name = f"{external_db['database']}_Ledger"
                
                col_defs = []
                for col in all_columns:
                    # Using LONGTEXT since XML data can be large
                    col_defs.append(f"`{col}` LONGTEXT")
                    
                create_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                target_cursor.execute(create_query)
                
                # Truncate the table if it already exists (since a full sync is being done)
                target_cursor.execute(f"TRUNCATE TABLE `{table_name}`")
                
                # 4. Inserting data
                placeholders = ", ".join(["%s"] * len(all_columns))
                insert_query = f"INSERT INTO `{table_name}` (`{'`, `'.join(all_columns)}`) VALUES ({placeholders})"
                
                insert_data = []
                for row in ledgers:
                    if not isinstance(row, dict):
                        continue
                        
                    row_values = []
                    for col in all_columns:
                        val = row.get(col)
                        # If there is nested XML, it will be converted to JSON and saved
                        if isinstance(val, (dict, list)):
                            val = json.dumps(val)
                        elif val is None:
                            val = ""
                        else:
                            val = str(val)
                        row_values.append(val)
                        
                    insert_data.append(row_values)
                
                if insert_data:
                    target_cursor.executemany(insert_query, insert_data)
                    print(f"Successfully inserted {len(insert_data)} rows into `{table_name}`")
                    
        except Exception as e:
            print(f"Error saving Tally data to MySQL: {str(e)}")
            raise e
        finally:
            target_conn.close()
            
        return
    

    elif db_type in ["postgresql", "postgres"]:
        import psycopg2
        db_host = external_db["host"]
        db_port = external_db.get("port", 5432)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        source_conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            user=external_db["username"],
            password=external_db["password"],
            database=external_db["database"]
        )
        source_conn.autocommit = True

    elif db_type == "mssql":
        # pyrefly: ignore [missing-import]
        import pymssql
        db_host = external_db["host"]
        db_port = external_db.get("port", 1433)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        conn_kwargs = {
            "server": db_host,
            "port": str(db_port),
            "database": external_db["database"]
        }
        if external_db.get("username"):
            conn_kwargs["user"] = external_db["username"]
            conn_kwargs["password"] = external_db.get("password", "")

        source_conn = pymssql.connect(**conn_kwargs)
        source_conn.autocommit(True)

    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        from hdbcli import dbapi
        tunnel = None
        db_host = external_db["host"]
        db_port = int(external_db.get("port", 30015))
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        is_hana_cloud = ("hanacloud.ondemand.com" in str(db_host).lower()) or (db_port == 443)
        connect_kwargs = {
            "address": db_host,
            "port": db_port,
            "user": external_db["username"],
            "password": external_db["password"],
            "autocommit": True
        }
        if external_db.get("database") and not is_hana_cloud:
            connect_kwargs["databaseName"] = external_db["database"]
        
        effective_schema = external_db.get("schema") or (external_db.get("database") if is_hana_cloud else None)
        if effective_schema:
            connect_kwargs["currentSchema"] = effective_schema

        if external_db.get("encrypt") or is_hana_cloud:
            connect_kwargs["encrypt"] = True
            connect_kwargs["sslValidateCertificate"] = False

        try:
            source_conn = dbapi.connect(**connect_kwargs)
        except Exception as e:
            if "not connected" in str(e).lower() and "databaseName" in connect_kwargs:
                fallback_db = connect_kwargs.pop("databaseName", None)
                if not connect_kwargs.get("currentSchema") and fallback_db:
                    connect_kwargs["currentSchema"] = fallback_db
                source_conn = dbapi.connect(**connect_kwargs)
            else:
                raise e

    else:
        db_host = external_db["host"]
        db_port = external_db.get("port", 3306)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        source_conn = pymysql.connect(
            host=db_host,
            port=db_port,
            user=external_db["username"],
            password=external_db["password"],
            database=external_db["database"],
            autocommit=True
        )

    target_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=user_db_name,
        autocommit=True
    )

    try:
        with source_conn.cursor() as source_cursor, target_conn.cursor() as target_cursor:

            connection_schema = external_db.get("schema")
            if db_type in ["hana", "sap_hana", "saphana", "sap"] and not connection_schema:
                connection_schema = external_db.get("schema") or (external_db.get("database") if is_hana_cloud else None) or external_db.get("database") or external_db.get("username")

            original_table, schema_name_from_db = resolve_source_table(
                source_cursor, table, external_db.get("database", "hana"), db_type, connection_schema
            )

            if db_type in ["hana", "sap_hana", "saphana", "sap"]:
                if original_table.upper() not in SAP_ALLOWED_OBJECTS_SET:
                    print(f"Skipping table {original_table} as it is not in SAP Datasphere allowed objects list.")
                    return
            
            if db_type in ["postgresql", "postgres"] and schema_name_from_db:
                source_cursor.execute(f"SET search_path TO {schema_name_from_db}")
            elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                active_target_schema = schema_name_from_db or connection_schema
                if active_target_schema:
                    try:
                        source_cursor.execute(f'SET SCHEMA "{active_target_schema}"')
                    except Exception:
                        pass
            
            active_schema = schema_name_from_db if schema_name_from_db else connection_schema

            col_types = get_source_column_types(source_cursor, original_table, db_type, active_schema)

            # check if table exists in target database
            target_cursor.execute("SHOW TABLES LIKE %s", (table,))
            exists = target_cursor.fetchone()

            # if table does not exist → create it
            if not exists:

                if db_type in ["postgresql", "postgres"]:
                    source_cursor.execute("""
                        SELECT column_name, data_type, is_nullable, character_maximum_length
                        FROM information_schema.columns
                        WHERE table_name = %s AND table_schema = %s
                        ORDER BY ordinal_position
                    """, (original_table, active_schema))
                    cols_info = source_cursor.fetchall()
                    col_defs = []
                    for col_name, data_type, is_nullable, char_len in cols_info:
                        mysql_type = postgres_to_mysql_type(data_type, char_len)
                        null_def = "NULL" if is_nullable == "YES" else "NOT NULL"
                        col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                    create_query = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                    cols_info = get_hana_columns_info(source_cursor, original_table, active_schema)
                    col_defs = []
                    for col_name, data_type, is_nullable, char_len, scale in cols_info:
                        mysql_type = hana_to_mysql_type(data_type, char_len, char_len, scale)
                        null_def = "NULL" if str(is_nullable).upper() in ["TRUE", "YES", "1"] else "NOT NULL"
                        col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                    create_query = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                else:
                    source_cursor.execute(f"SHOW CREATE TABLE `{original_table}`")
                    create_query = source_cursor.fetchone()[1]

                    create_query = create_query.replace(
                        f"CREATE TABLE `{original_table}`",
                        f"CREATE TABLE `{table}`"
                    )

                target_cursor.execute(create_query)

                # copy all rows
                source_cursor.execute(f"SELECT * FROM {get_source_table_select_name(original_table, db_type, active_schema)}")
                BATCH_SIZE = 5000
                insert_query = None

                while True:
                    b_rows = source_cursor.fetchmany(BATCH_SIZE)
                    if not b_rows:
                        break
                    if insert_query is None:
                        placeholders = ", ".join(["%s"] * len(b_rows[0]))
                        insert_query = f"INSERT INTO `{table}` VALUES ({placeholders})"
                    cleaned_rows = [clean_row(row, col_types) for row in b_rows]
                    target_cursor.executemany(insert_query, cleaned_rows)

                return

            source_cursor.execute(f"SELECT COUNT(*) FROM {get_source_table_select_name(original_table, db_type, active_schema)}")
            source_count = source_cursor.fetchone()[0]

            target_cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
            target_count = target_cursor.fetchone()[0]

            if source_count > target_count:

                offset = target_count

                limit_str = f"LIMIT {source_count-offset} OFFSET {offset}"
                source_cursor.execute(
                    f"SELECT * FROM {get_source_table_select_name(original_table, db_type, active_schema)} {limit_str}"
                )

                BATCH_SIZE = 5000
                insert_query = None

                while True:
                    b_rows = source_cursor.fetchmany(BATCH_SIZE)
                    if not b_rows:
                        break
                    if insert_query is None:
                        placeholders = ", ".join(["%s"] * len(b_rows[0]))
                        insert_query = f"INSERT INTO `{table}` VALUES ({placeholders})"
                    cleaned_rows = [clean_row(row, col_types) for row in b_rows]
                    target_cursor.executemany(insert_query, cleaned_rows)

    finally:
        if 'source_conn' in locals() and source_conn:
            try: source_conn.close()
            except: pass
        if 'target_conn' in locals() and target_conn:
            try: target_conn.close()
            except: pass

def apply_bulk_external_sync(user_id, connection_id, session_id, tables, action):
    """
    Service to handle multiple tables at once and support Replace All vs Update Existing.
    """
    tunnel = None
    # 1. Fetch credential from database_credential table
    cred_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"]
    )

    with cred_conn.cursor() as cursor:
        cursor.execute("""
            SELECT credential, db_type
            FROM database_credential
            WHERE user_id=%s AND connection_id=%s AND session_id=%s
        """, (user_id, connection_id, session_id))

        result = cursor.fetchone()
        if not result:
            raise Exception("Database credential not found")
        external_db = json.loads(result[0])
        db_type = result[1]
    cred_conn.close()

    db_type = db_type.lower() if db_type else "mysql"

    # 2. Get username from users table to find target database
    user_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"]
    )

    with user_conn.cursor() as cursor:
        # Fetch username from users table
        cursor.execute("SELECT email FROM users WHERE id=%s", (user_id,))
        user_result = cursor.fetchone()
        if not user_result:
            raise Exception("User not found")
        
        email = user_result[0]
        username = email.split("@")[0]

        # Fetch workspace database from workspaces table
        cursor.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s", (session_id,))
        workspace_result = cursor.fetchone()
        
        if not workspace_result or not workspace_result[0]:
            raise Exception("Workspace database not found for this session")
            
        new_user_db = workspace_result[0]
    user_conn.close()

    user_db_name = new_user_db

    if db_type in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload', 'doc_upload', 'doc_chunk_upload']:
        return
    if db_type == 'tally':
        # Logic to insert Tally's XML data directly into MySQL
        from database.tally_connector import fetch_tally_data
        import json
        
        target_host = external_db.get("host")
        port = external_db.get("port")
        company_name = external_db.get("database")
        
        # Fetching XML from Tally 
        ledger_data = fetch_tally_data(target_host, port, company_name, "Ledger")
        print("Tally Data Fetched Successfully")
        
        # Creating MySQL connection (for the database where the user wants to save)
        target_conn = pymysql.connect(
            host=MYSQL_CONFIG["host"],
            user=MYSQL_CONFIG["user"],
            password=MYSQL_CONFIG["password"],
            database=user_db_name,
            autocommit=True
        )
        
        try:
            with target_conn.cursor() as target_cursor:
                # 1. Extracting LEDGER list from XML dictionary
                collection = ledger_data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
                ledgers = collection.get("LEDGER", [])
                
                if isinstance(ledgers, dict):
                    ledgers = [ledgers]
                    
                if not ledgers:
                    print("No ledgers found in Tally response.")
                    return
                
                # 2. Extracting dynamic column names
                all_columns = set()
                for row in ledgers:
                    if isinstance(row, dict):
                        all_columns.update(row.keys())
                
                if "@attributes" in all_columns:
                    all_columns.remove("@attributes")
                    
                all_columns = list(all_columns)
                
                # 3. Creating MySQL table
                table_name = f"{external_db['database']}_Ledger"
                
                col_defs = []
                for col in all_columns:
                    # Using LONGTEXT since XML data can be large
                    col_defs.append(f"`{col}` LONGTEXT")
                    
                create_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                target_cursor.execute(create_query)
                
                # Truncate the table if it already exists (since a full sync is being done)
                target_cursor.execute(f"TRUNCATE TABLE `{table_name}`")
                
                # 4. Inserting data
                placeholders = ", ".join(["%s"] * len(all_columns))
                insert_query = f"INSERT INTO `{table_name}` (`{'`, `'.join(all_columns)}`) VALUES ({placeholders})"
                
                insert_data = []
                for row in ledgers:
                    if not isinstance(row, dict):
                        continue
                        
                    row_values = []
                    for col in all_columns:
                        val = row.get(col)
                        # If there is nested XML, it will be converted to JSON and saved
                        if isinstance(val, (dict, list)):
                            val = json.dumps(val)
                        elif val is None:
                            val = ""
                        else:
                            val = str(val)
                        row_values.append(val)
                        
                    insert_data.append(row_values)
                
                if insert_data:
                    target_cursor.executemany(insert_query, insert_data)
                    print(f"Successfully inserted {len(insert_data)} rows into `{table_name}`")
                    
        except Exception as e:
            print(f"Error saving Tally data to MySQL: {str(e)}")
            raise e
        finally:
            target_conn.close()
            
        return

    

    # 3. Connect to Source and Target Databases
    elif db_type in ["postgresql", "postgres"]:
        import psycopg2
        db_host = external_db["host"]
        db_port = external_db.get("port", 5432)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        source_conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            user=external_db["username"],
            password=external_db["password"],
            database=external_db["database"]
        )
        source_conn.autocommit = True

    elif db_type == "mssql":
        # pyrefly: ignore [missing-import]
        import pymssql
        db_host = external_db["host"]
        db_port = external_db.get("port", 1433)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        conn_kwargs = {
            "server": db_host,
            "port": str(db_port),
            "database": external_db["database"]
        }
        if external_db.get("username"):
            conn_kwargs["user"] = external_db["username"]
            conn_kwargs["password"] = external_db.get("password", "")

        source_conn = pymssql.connect(**conn_kwargs)
        source_conn.autocommit(True)

    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
        from hdbcli import dbapi
        tunnel = None
        db_host = external_db["host"]
        db_port = int(external_db.get("port", 30015))
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        is_hana_cloud = ("hanacloud.ondemand.com" in str(db_host).lower()) or (db_port == 443)
        connect_kwargs = {
            "address": db_host,
            "port": db_port,
            "user": external_db["username"],
            "password": external_db["password"],
            "autocommit": True
        }
        if external_db.get("database") and not is_hana_cloud:
            connect_kwargs["databaseName"] = external_db["database"]
        
        effective_schema = external_db.get("schema") or (external_db.get("database") if is_hana_cloud else None)
        if effective_schema:
            connect_kwargs["currentSchema"] = effective_schema

        if external_db.get("encrypt") or is_hana_cloud:
            connect_kwargs["encrypt"] = True
            connect_kwargs["sslValidateCertificate"] = False

        try:
            source_conn = dbapi.connect(**connect_kwargs)
        except Exception as e:
            if "not connected" in str(e).lower() and "databaseName" in connect_kwargs:
                fallback_db = connect_kwargs.pop("databaseName", None)
                if not connect_kwargs.get("currentSchema") and fallback_db:
                    connect_kwargs["currentSchema"] = fallback_db
                source_conn = dbapi.connect(**connect_kwargs)
            else:
                raise e

    else:
        db_host = external_db["host"]
        db_port = external_db.get("port", 3306)
        
        if external_db.get("ssh_host"):
            # pyrefly: ignore[missing-import]
            from sshtunnel import SSHTunnelForwarder
            tunnel = SSHTunnelForwarder(
                (external_db["ssh_host"], int(external_db.get("ssh_port", 22))),
                ssh_username=external_db.get("ssh_username"),
                ssh_password=external_db.get("ssh_password"),
                ssh_pkey=external_db.get("ssh_key_file"),
                remote_bind_address=(external_db.get("remote_mysql_host", external_db["host"]), int(external_db.get("remote_mysql_port", db_port)))
            )
            tunnel.start()
            db_host = "127.0.0.1"
            db_port = tunnel.local_bind_port

        source_conn = pymysql.connect(
            host=db_host,
            port=db_port,
            user=external_db["username"],
            password=external_db["password"],
            database=external_db["database"],
            autocommit=True
        )

    target_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=user_db_name,
        autocommit=True
    )

    try:
        with source_conn.cursor() as source_cursor, target_conn.cursor() as target_cursor:
            
            connection_schema = external_db.get("schema")
            if db_type in ["hana", "sap_hana", "saphana", "sap"] and not connection_schema:
                connection_schema = external_db.get("schema") or (external_db.get("database") if is_hana_cloud else None) or external_db.get("database") or external_db.get("username")

            # Loop through all tables sent from the UI
            for table in tables:
                original_table, schema_name_from_db = resolve_source_table(
                    source_cursor, table, external_db.get("database", "hana"), db_type, connection_schema
                )

                if db_type in ["hana", "sap_hana", "saphana", "sap"]:
                    if original_table.upper() not in SAP_ALLOWED_OBJECTS_SET:
                        print(f"Skipping table {original_table} as it is not in SAP Datasphere allowed objects list.")
                        continue
                
                if db_type in ["postgresql", "postgres"] and schema_name_from_db:
                    source_cursor.execute(f"SET search_path TO {schema_name_from_db}")
                elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                    active_target_schema = schema_name_from_db or connection_schema
                    if active_target_schema:
                        try:
                            source_cursor.execute(f'SET SCHEMA "{active_target_schema}"')
                        except Exception:
                            pass
                
                active_schema = schema_name_from_db if schema_name_from_db else connection_schema

                col_types = get_source_column_types(source_cursor, original_table, db_type, active_schema)

                # ---------------------------------------------------------
                # LOGIC FOR "Replace All"
                # ---------------------------------------------------------
                if action == "replace":
                    # Drop the table if it exists to start fresh
                    target_cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
                    
                    # Get create schema from source
                    if db_type in ["postgresql", "postgres"]:
                        source_cursor.execute("""
                            SELECT column_name, data_type, is_nullable, character_maximum_length
                            FROM information_schema.columns
                            WHERE table_name = %s AND table_schema = %s
                            ORDER BY ordinal_position
                        """, (original_table, active_schema))
                        cols_info = source_cursor.fetchall()
                        col_defs = []
                        for col_name, data_type, is_nullable, char_len in cols_info:
                            mysql_type = postgres_to_mysql_type(data_type, char_len)
                            null_def = "NULL" if is_nullable == "YES" else "NOT NULL"
                            col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                        create_query = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                    elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                        cols_info = get_hana_columns_info(source_cursor, original_table, active_schema)
                        col_defs = []
                        for col_name, data_type, is_nullable, char_len, scale in cols_info:
                            mysql_type = hana_to_mysql_type(data_type, char_len, char_len, scale)
                            null_def = "NULL" if str(is_nullable).upper() in ["TRUE", "YES", "1"] else "NOT NULL"
                            col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                        create_query = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                    else:
                        source_cursor.execute(f"SHOW CREATE TABLE `{original_table}`")
                        create_query = source_cursor.fetchone()[1]
                        create_query = create_query.replace(
                            f"CREATE TABLE `{original_table}`",
                            f"CREATE TABLE `{table}`"
                        )
                    target_cursor.execute(create_query)

                    # Copy all rows
                    col_list = get_source_columns_list(source_cursor, original_table, db_type, active_schema)
                    source_col_str = get_quoted_columns(col_list, db_type)
                    target_col_str = get_target_quoted_columns(col_list)
                    
                    source_cursor.execute(f"SELECT {source_col_str} FROM {get_source_table_select_name(original_table, db_type, active_schema)}")
                    BATCH_SIZE = 5000
                    insert_query = None

                    while True:
                        b_rows = source_cursor.fetchmany(BATCH_SIZE)
                        if not b_rows:
                            break
                        if insert_query is None:
                            placeholders = ", ".join(["%s"] * len(b_rows[0]))
                            insert_query = f"INSERT INTO `{table}` ({target_col_str}) VALUES ({placeholders})"
                        cleaned_rows = [clean_row(row, col_types) for row in b_rows]
                        target_cursor.executemany(insert_query, cleaned_rows)   

                # ---------------------------------------------------------
                # LOGIC FOR "Update Existing" (Append / Create New)
                # ---------------------------------------------------------
                elif action == "update":
                    # check if table exists in target database
                    target_cursor.execute("SHOW TABLES LIKE %s", (table,))
                    exists = target_cursor.fetchone()

                    if not exists:
                        # Table doesn't exist, create and copy all
                        if db_type in ["postgresql", "postgres"]:
                            source_cursor.execute("""
                                SELECT column_name, data_type, is_nullable, character_maximum_length
                                FROM information_schema.columns
                                WHERE table_name = %s AND table_schema = %s
                                ORDER BY ordinal_position
                            """, (original_table, active_schema))
                            cols_info = source_cursor.fetchall()
                            col_defs = []
                            for col_name, data_type, is_nullable, char_len in cols_info:
                                mysql_type = postgres_to_mysql_type(data_type, char_len)
                                null_def = "NULL" if is_nullable == "YES" else "NOT NULL"
                                col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                            create_query = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                        elif db_type in ["hana", "sap_hana", "saphana", "sap"]:
                            cols_info = get_hana_columns_info(source_cursor, original_table, active_schema)
                            col_defs = []
                            for col_name, data_type, is_nullable, char_len, scale in cols_info:
                                mysql_type = hana_to_mysql_type(data_type, char_len, char_len, scale)
                                null_def = "NULL" if str(is_nullable).upper() in ["TRUE", "YES", "1"] else "NOT NULL"
                                col_defs.append(f"`{col_name}` {mysql_type} {null_def}")
                            create_query = f"CREATE TABLE `{table}` (\n  " + ",\n  ".join(col_defs) + "\n)"
                        else:
                            source_cursor.execute(f"SHOW CREATE TABLE `{original_table}`")
                            create_query = source_cursor.fetchone()[1]
                            create_query = create_query.replace(
                                f"CREATE TABLE `{original_table}`",
                                f"CREATE TABLE `{table}`"
                            )
                        target_cursor.execute(create_query)

                        col_list = get_source_columns_list(source_cursor, original_table, db_type, active_schema)
                        source_col_str = get_quoted_columns(col_list, db_type)
                        target_col_str = get_target_quoted_columns(col_list)
                        
                        source_cursor.execute(f"SELECT {source_col_str} FROM {get_source_table_select_name(original_table, db_type, active_schema)}")
                        BATCH_SIZE = 5000
                        insert_query = None

                        while True:
                            b_rows = source_cursor.fetchmany(BATCH_SIZE)
                            if not b_rows:
                                break
                            if insert_query is None:
                                placeholders = ", ".join(["%s"] * len(b_rows[0]))
                                insert_query = f"INSERT INTO `{table}` ({target_col_str}) VALUES ({placeholders})"
                            cleaned_rows = [clean_row(row, col_types) for row in b_rows]
                            target_cursor.executemany(insert_query, cleaned_rows)
                    else:
                        # Table exists, append only new rows
                        source_cursor.execute(f"SELECT COUNT(*) FROM {get_source_table_select_name(original_table, db_type, active_schema)}")
                        source_count = source_cursor.fetchone()[0]

                        target_cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                        target_count = target_cursor.fetchone()[0]

                        if source_count > target_count:
                            offset = target_count
                            limit_str = f"LIMIT {source_count-offset} OFFSET {offset}"
                            col_list = get_source_columns_list(source_cursor, original_table, db_type, active_schema)
                            source_col_str = get_quoted_columns(col_list, db_type)
                            target_col_str = get_target_quoted_columns(col_list)
                            
                            source_cursor.execute(
                                f"SELECT {source_col_str} FROM {get_source_table_select_name(original_table, db_type, active_schema)} {limit_str}"
                            )
                            BATCH_SIZE = 5000
                            insert_query = None

                            while True:
                                b_rows = source_cursor.fetchmany(BATCH_SIZE)
                                if not b_rows:
                                    break
                                if insert_query is None:
                                    placeholders = ", ".join(["%s"] * len(b_rows[0]))
                                    insert_query = f"INSERT INTO `{table}` ({target_col_str}) VALUES ({placeholders})"
                                cleaned_rows = [clean_row(row, col_types) for row in b_rows]
                                target_cursor.executemany(insert_query, cleaned_rows)
                            

    finally:
        if 'source_conn' in locals() and source_conn:
            try: source_conn.close()
            except: pass
        if 'target_conn' in locals() and target_conn:
            try: target_conn.close()
            except: pass
        if 'tunnel' in locals() and tunnel:
            try: tunnel.stop()
            except: pass

        try: source_conn.close()
        except: pass
        try: target_conn.close()
        except: pass
