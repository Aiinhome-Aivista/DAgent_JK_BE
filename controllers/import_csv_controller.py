# import os
# import json
# import pandas as pd
# import pymysql
# from flask import request, jsonify
# from database.config import MYSQL_CONFIG
# from database.csv_processor import _infer_schema, _apply_schema, MONEY_PRECISION, MONEY_SCALE

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# UPLOAD_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "uploads"))

# def import_csv_data(get_db_connection):
#     data = request.json
#     user_id = data.get("user_id")
#     connection_id = data.get("connection_id")
#     session_id = data.get("session_id")

#     if not user_id or not connection_id or not session_id:
#         return jsonify({"error": "Missing parameters"}), 400

#     try:
#         conn = get_db_connection()
#         cursor = conn.cursor(dictionary=True)

#         imported_files = []
#         affected_tables_info = []
#         total_rows = 0

#         # fetch credential
#         query = """
#         SELECT dc.credential
#         FROM database_credential dc
#         WHERE (dc.connection_id = %s OR dc.connection_id IS NULL)
#         AND dc.user_id = %s
#         AND dc.session_id = %s
#         ORDER BY dc.connection_id DESC
#         LIMIT 1
#         """
#         cursor.execute(query, (connection_id, user_id, session_id))
#         result = cursor.fetchone()

#         if not result:
#             return jsonify({"status": "error", "message": "Credential not found"}), 404

#         credential = json.loads(result["credential"])
#         files = credential.get("files", [])

#         # fetch user db
#         cursor.execute("SELECT name,new_user_db FROM users WHERE id=%s", (user_id,))
#         user_data = cursor.fetchone()

#         username = user_data["name"]
#         user_db = user_data["new_user_db"]

#         # connect user database
#         user_conn = pymysql.connect(
#             host=MYSQL_CONFIG["host"],
#             user=MYSQL_CONFIG["user"],
#             password=MYSQL_CONFIG["password"],
#             database=user_db,
#             cursorclass=pymysql.cursors.DictCursor,
#             autocommit=True
#         )
#         user_cursor = user_conn.cursor()

#         files_in_folder = os.listdir(UPLOAD_DIR)

#         # Pre-fetch existing tables and their schemas to enable smart merging
#         user_cursor.execute("SHOW TABLES")
#         existing_tables = [list(row.values())[0] for row in user_cursor.fetchall()]
        
#         schema_map = {}
#         for t in existing_tables:
#             user_cursor.execute(f"SHOW COLUMNS FROM `{t}`")
#             t_cols = [r['Field'] for r in user_cursor.fetchall()]
#             schema_map[t] = set(t_cols)

#         for file in files:
#             if not file.lower().endswith('.csv'):
#                 continue

#             matched_file = next((f for f in files_in_folder if f.lower() == file.lower()), None)
#             if not matched_file:
#                 continue

#             file_path = os.path.join(UPLOAD_DIR, matched_file)
#             df = pd.read_csv(file_path, encoding="utf-8-sig", dtype=str)

#             if df.empty:
#                 continue

#             # Standardize column names: strip whitespace, replace spaces and dots with underscores
#             df.columns = df.columns.str.strip().str.replace(" ", "_").str.replace(".", "_", regex=False)
            
#             # Infer schema and apply data types using csv_processor
#             schema = _infer_schema(df.head(50000))
#             df = _apply_schema(df, schema)
            
#             df = df.where(pd.notnull(df), None)
#             df_cols_set = set(df.columns)
#             num_columns = len(df.columns)

#             # Check if columns match any existing table (Smart Schema Detection)
#             matched_table = None
#             for t_name, t_cols_set in schema_map.items():
#                 if df_cols_set == t_cols_set:
#                     matched_table = t_name
#                     break

#             # If match found, use that table. Else, create new table named after file.
#             table_name = matched_table if matched_table else file.replace(".csv", "").lower()

#             if not matched_table:
#                 # Create the new table
#                 col_defs = []
#                 for c in df.columns:
#                     kind = schema.get(c, {}).get('kind', 'text')
#                     if kind == 'numeric':
#                         col_defs.append(f"`{c}` DECIMAL({MONEY_PRECISION}, {MONEY_SCALE})")
#                     elif kind == 'int':
#                         col_defs.append(f"`{c}` BIGINT")
#                     elif kind == 'date':
#                         col_defs.append(f"`{c}` DATE")
#                     else:
#                         col_defs.append(f"`{c}` TEXT")
                
#                 cols = ", ".join(col_defs)
#                 create_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` ({cols})"
#                 user_cursor.execute(create_query)
#                 # Register in schema_map so subsequent files in this batch can match it
#                 schema_map[table_name] = df_cols_set

#             # 1. Deduplicate within the dataframe itself
#             df = df.drop_duplicates()
            
#             # Prepare data values
#             data_values = [tuple(None if pd.isna(x) else x for x in row) for row in df.values]
            
#             # 2. Setup Temporary Table
#             temp_table_name = "temp_csv_import_table"
#             user_cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS `{temp_table_name}`")
#             user_cursor.execute(f"CREATE TEMPORARY TABLE `{temp_table_name}` LIKE `{table_name}`")

#             # 3. Bulk Insert into Temporary Table
#             columns = ", ".join([f"`{c}`" for c in df.columns])
#             placeholders = ", ".join(["%s"] * len(df.columns))
#             insert_temp_query = f"INSERT INTO `{temp_table_name}` ({columns}) VALUES ({placeholders})"
#             user_cursor.executemany(insert_temp_query, data_values)

#             # 4. Insert into Main Table avoiding duplicates
#             # Build the ON conditions for all columns using NULL-safe equal (<=>)
#             join_conditions = " AND ".join([f"`{table_name}`.`{c}` <=> `{temp_table_name}`.`{c}`" for c in df.columns])

#             insert_main_query = f"""
#             INSERT INTO `{table_name}` ({columns})
#             SELECT {columns} FROM `{temp_table_name}`
#             WHERE NOT EXISTS (
#                 SELECT 1 FROM `{table_name}`
#                 WHERE {join_conditions}
#             )
#             """
#             user_cursor.execute(insert_main_query)
#             rows_inserted = user_cursor.rowcount

#             # 5. Drop Temporary Table
#             user_cursor.execute(f"DROP TEMPORARY TABLE `{temp_table_name}`")

#             total_rows += rows_inserted
#             imported_files.append(file)
#             affected_tables_info.append({
#                 "table": table_name,
#                 "rows": rows_inserted,
#                 "columns": num_columns
#             })

#             log_query = """
#             INSERT INTO external_db_sync_log
#             (user_id,username,external_database,table_name,
#             action_type,rows_affected,session_id,new_user_db)
#             VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
#             """
#             cursor.execute(log_query, (
#                 user_id, username, file, table_name,
#                 "IMPORT", rows_inserted, session_id, user_db
#             ))
#             conn.commit()

#         # Calculate approximate data size
#         data_size_mb = round((total_rows * 200) / (1024 * 1024), 2)  # Rough estimate

#         return jsonify({
#             "message": "Data imported successfully",
#             "status": "success",
#             "imported_files": imported_files,
#             "data": {
#                 "summary": {
#                     "total_rows": total_rows,
#                     "total_columns": sum(t["columns"] for t in affected_tables_info),
#                     "data_size_mb": max(data_size_mb, 0.01),
#                     "last_sync": "Just now"
#                 },
#                 "tables": affected_tables_info
#             }
#         })

#     except Exception as e:
#         print("ERROR:", str(e))
#         return jsonify({
#             "status": "error",
#             "message": str(e)
#         }), 500



import os
import json
import pandas as pd
import pymysql
from flask import request, jsonify
from database.config import MYSQL_CONFIG
from database.csv_processor import _infer_schema, _apply_schema, MONEY_PRECISION, MONEY_SCALE
from database.schema_matcher import match_columns_to_existing

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "uploads"))

def import_csv_data(get_db_connection):
    data = request.json
    user_id = data.get("user_id")
    session_id = data.get("session_id")
    
    # Handle both single connection_id and multiple connection_ids
    connection_id_data = data.get("connection_id")
    if isinstance(connection_id_data, list):
        connection_ids = connection_id_data
    elif connection_id_data:
        connection_ids = [connection_id_data]
    else:
        connection_ids = data.get("connection_ids", [])

    if not user_id or not connection_ids or not session_id:
        return jsonify({"error": "Missing parameters"}), 400

    # from controllers.pricing_controller import get_usage_and_limits
    # usage_res = get_usage_and_limits(user_id)
    # if usage_res.get('status') == 'success':
    #     uploads_metric = usage_res['usage_stats']['metrics']['uploads']
    #     used = uploads_metric['used']
    #     total = uploads_metric['total']
    #     if total not in (-1, 0) and used >= total:
    #         return jsonify({"status": "error", "message": "Daily upload limit exceeded based on your active plan."}), 403

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        imported_files = []
        unique_tables_info = {}
        total_data_size_bytes = 0  # To track exact file size in bytes
        force_import_tables = set()  # Track tables that are newly created or empty during this API call
        created_tables = set() # Track tables created during this session for rollback

        # fetch credential
        query = """
        SELECT dc.credential
        FROM database_credential dc
        WHERE (dc.connection_id = %s OR dc.connection_id IS NULL)
        AND dc.user_id = %s
        AND dc.session_id = %s
        ORDER BY dc.connection_id DESC
        LIMIT 1
        """

        all_files = []
        file_to_cid = {}
        for cid in connection_ids:
            cursor.execute(query, (cid, user_id, session_id))
            result = cursor.fetchone()
            if result:
                credential = json.loads(result["credential"])
                cid_files = credential.get("files", [])
                all_files.extend(cid_files)
                for f in cid_files:
                    file_to_cid[f] = cid

        # Remove duplicates while preserving order
        files = list(dict.fromkeys(all_files))
        
        # Sort files alphabetically so base files (e.g. sales_data.csv) are processed BEFORE variants (sales_data_2.csv)
        files.sort()

        if not files:
            return jsonify({"status": "error", "message": "Credentials not found or no files to import"}), 404


        # fetch workspace db
        cursor.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s", (session_id,))
        workspace_data = cursor.fetchone()
        
        if not workspace_data or not workspace_data.get("workspace_db"):
            return jsonify({"status": "error", "message": "Workspace database not found"}), 404
            
        user_db = workspace_data["workspace_db"]
        
        # also fetch username for logs
        cursor.execute("SELECT name FROM users WHERE id=%s", (user_id,))
        user_data = cursor.fetchone()
        username = user_data["name"] if user_data else "Unknown"

        # connect user database
        user_conn = pymysql.connect(
            host=MYSQL_CONFIG["host"],
            user=MYSQL_CONFIG["user"],
            password=MYSQL_CONFIG["password"],
            database=user_db,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )
        user_cursor = user_conn.cursor()

        files_in_folder = os.listdir(UPLOAD_DIR)

        # Pre-fetch existing tables and their schemas to enable smart merging
        user_cursor.execute("SHOW TABLES")
        existing_tables = [list(row.values())[0] for row in user_cursor.fetchall()]
        
        schema_map = {}
        for t in existing_tables:
            user_cursor.execute(f"SHOW COLUMNS FROM `{t}`")
            # Convert to lower to avoid case-mismatches during schema subset validation
            t_cols = [r['Field'].lower() for r in user_cursor.fetchall()]
            schema_map[t] = set(t_cols)

        for file in files:
            if not file.lower().endswith('.csv'):
                continue

            matched_file = next(
                (f for f in files_in_folder if f.lower() == file.lower() or f.lower().endswith(f"_{file.lower()}")), 
                None
            )
            if not matched_file:
                continue

            file_path = os.path.join(UPLOAD_DIR, matched_file)
            file_size_bytes = os.path.getsize(file_path)
            
            df = pd.read_csv(file_path, encoding="utf-8-sig", dtype=str)

            if df.empty:
                continue

            # 1. Drop completely empty rows
            df = df.dropna(how='all')

            # 2. Standardize column names: strip whitespace, replace spaces/dots/hyphens with underscores, and convert to lowercase
            df.columns = df.columns.str.strip().str.replace(" ", "_").str.replace(".", "_", regex=False).str.replace("-", "_", regex=False).str.lower()
            
            # 3. Drop Unnamed/empty columns
            df = df.loc[:, ~df.columns.str.contains('^unnamed', na=False)]
            
            # 4. Strip leading/trailing spaces from string data
            df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
            
            # Infer schema and apply data types using csv_processor
            schema = _infer_schema(df.head(50000))
            df = _apply_schema(df, schema)
            
            df = df.where(pd.notnull(df), None)
            df_cols_set = set(df.columns)
            num_columns = len(df.columns)

            matched_table = None
            expected_table_name = file.replace(".csv", "").strip().lower()
            import re
            # Remove trailing file duplicate suffixes like ' (1)' or ' (1) (1)'
            expected_table_name = re.sub(r'(\s*\(\d+\))+$', '', expected_table_name)
            # Standardize table name: replace spaces, dots, and hyphens with underscores
            expected_table_name = expected_table_name.replace(" ", "_").replace("-", "_").replace(".", "_")
            
            # Step 1: Priority check for exact table name match (case-insensitive)
            target_table_candidate = next((t for t in schema_map if t.lower() == expected_table_name), None)
            
            # Step 2: Strict high-confidence schema fallback (only if exact table name doesn't exist)
            if not target_table_candidate and len(df.columns) > 0:
                best_match = None
                best_ratio = 0.0
                for t_name, t_cols in schema_map.items():
                    if t_cols:
                        intersection_count = len(set(df.columns).intersection(t_cols))
                        overlap_ratio = intersection_count / max(len(df.columns), len(t_cols))
                        # Must have at least 70% schema overlap to be considered the same table
                        if overlap_ratio >= 0.70 and overlap_ratio > best_ratio:
                            best_ratio = overlap_ratio
                            best_match = t_name
                if best_match:
                    target_table_candidate = best_match
                        
            if target_table_candidate:
                matched_table = target_table_candidate
                existing_table_cols = list(schema_map[matched_table])
                
                # Fetch a sample from the existing database table to assist profile matching if needed
                sample_db_df = None
                try:
                    user_cursor.execute(f"SELECT * FROM `{matched_table}` LIMIT 100")
                    rows = user_cursor.fetchall()
                    if rows:
                        sample_db_df = pd.DataFrame(rows)
                except Exception:
                    sample_db_df = None
                    
                match_res = match_columns_to_existing(df, existing_table_cols, sample_db_df)
                mapping = match_res['column_mapping']
                unmapped_cols = match_res['unmapped_new_cols']
                
                # Add unmapped new columns to table via ALTER TABLE
                if unmapped_cols:
                    for c in unmapped_cols:
                        kind = schema.get(c, {}).get('kind', 'text')
                        col_type = "TEXT"
                        if kind == 'numeric':
                            col_type = f"DECIMAL({MONEY_PRECISION}, {MONEY_SCALE})"
                        elif kind == 'int':
                            col_type = "BIGINT"
                        elif kind == 'date':
                            col_type = "DATE"
                            
                        alter_query = f"ALTER TABLE `{matched_table}` ADD COLUMN `{c}` {col_type}"
                        try:
                            user_cursor.execute(alter_query)
                        except pymysql.err.OperationalError as e:
                            if e.args[0] == 1060:
                                pass
                            else:
                                raise
                        schema_map[matched_table].add(c)
                        
                # Rename DataFrame columns to match existing target columns for mapped headers
                rename_dict = {in_c: target_c for in_c, target_c in mapping.items() if target_c is not None}
                df = df.rename(columns=rename_dict)
                df_cols_set = set(df.columns)

            table_name = matched_table if matched_table else expected_table_name

            if not matched_table:
                # Create the new table
                col_defs = []
                for c in df.columns:
                    kind = schema.get(c, {}).get('kind', 'text')
                    if kind == 'numeric':
                        col_defs.append(f"`{c}` DECIMAL({MONEY_PRECISION}, {MONEY_SCALE})")
                    elif kind == 'int':
                        col_defs.append(f"`{c}` BIGINT")
                    elif kind == 'date':
                        col_defs.append(f"`{c}` DATE")
                    else:
                        col_defs.append(f"`{c}` TEXT")
                
                cols = ", ".join(col_defs)
                create_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` ({cols})"
                user_cursor.execute(create_query)
                created_tables.add(table_name)
                # Register in schema_map so subsequent files in this batch can match it
                schema_map[table_name] = set(df.columns)

            # By default, we always process the file to allow incremental data uploads.
            # Deduplication handles avoiding duplicate rows for master tables.
            already_imported = False
            
            rows_inserted = 0
            
            if not already_imported:
                # 1. Deduplicate within the dataframe itself to prevent duplicates
                df = df.drop_duplicates()
                data_values = [tuple(None if pd.isna(x) else x for x in row) for row in df.values]
                
                columns = ", ".join([f"`{c}`" for c in df.columns])
                placeholders = ", ".join(["%s"] * len(df.columns))
                
                # 2. Setup Temporary Table
                temp_table_name = "temp_csv_import_table"
                user_cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS `{temp_table_name}`")
                user_cursor.execute(f"CREATE TEMPORARY TABLE `{temp_table_name}` LIKE `{table_name}`")

                # 3. Bulk Insert into Temporary Table
                insert_temp_query = f"INSERT INTO `{temp_table_name}` ({columns}) VALUES ({placeholders})"
                user_cursor.executemany(insert_temp_query, data_values)

                # 4. Insert into Main Table avoiding duplicates
                join_conditions = " AND ".join([f"`{table_name}`.`{c}` <=> `{temp_table_name}`.`{c}`" for c in df.columns])

                insert_main_query = f"""
                INSERT INTO `{table_name}` ({columns})
                SELECT {columns} FROM `{temp_table_name}`
                WHERE NOT EXISTS (
                    SELECT 1 FROM `{table_name}`
                    WHERE {join_conditions}
                )
                """
                user_cursor.execute(insert_main_query)
                rows_inserted = user_cursor.rowcount

                # 5. Drop Temporary Table
                user_cursor.execute(f"DROP TEMPORARY TABLE `{temp_table_name}`")

            if not already_imported:
                total_data_size_bytes += file_size_bytes
                
            # 6. Get actual total rows in the table for reporting
            user_cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table_name}`")
            table_total_rows = user_cursor.fetchone()['cnt']

            imported_files.append(file)
            unique_tables_info[table_name] = {
                "table": table_name,
                "rows": table_total_rows,
                "columns": num_columns
            }

            if not already_imported:
                table_data_size_mb = round(file_size_bytes / (1024 * 1024), 2)
                exact_size_mb = file_size_bytes / (1024 * 1024)
                
                if rows_inserted > 0:
                    log_query = """
                    INSERT INTO external_db_sync_log
                    (user_id,username,external_database,table_name,
                    action_type,rows_affected,session_id,new_user_db,
                    total_rows,total_columns,data_size_mb,exact_size_mb, sync_time)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
                    """
                    cursor.execute(log_query, (
                        user_id, username, file, table_name,
                        "IMPORT", rows_inserted, session_id, user_db,
                        table_total_rows, num_columns, table_data_size_mb, exact_size_mb
                    ))
                else:
                    update_query = """
                    UPDATE external_db_sync_log
                    SET sync_time = CURRENT_TIMESTAMP,
                        total_rows = %s,
                        total_columns = %s,
                        data_size_mb = %s,
                        exact_size_mb = %s
                    WHERE session_id = %s AND external_database = %s AND table_name = %s
                    ORDER BY id DESC LIMIT 1
                    """
                    cursor.execute(update_query, (
                        table_total_rows, num_columns, table_data_size_mb, exact_size_mb,
                        session_id, file, table_name
                    ))
                    
                    if cursor.rowcount == 0:
                        log_query = """
                        INSERT INTO external_db_sync_log
                        (user_id,username,external_database,table_name,
                        action_type,rows_affected,session_id,new_user_db,
                        total_rows,total_columns,data_size_mb,exact_size_mb)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """
                        cursor.execute(log_query, (
                            user_id, username, file, table_name,
                            "IMPORT", rows_inserted, session_id, user_db,
                            table_total_rows, num_columns, table_data_size_mb, exact_size_mb
                        ))

                conn.commit()

        affected_tables_info = list(unique_tables_info.values())
        total_rows = sum(t["rows"] for t in affected_tables_info)
        total_columns = sum(t["columns"] for t in affected_tables_info)

        data_size_mb = round(total_data_size_bytes / (1024 * 1024), 2) if total_data_size_bytes else 0.0

        # ── Auto-sync any live DB connections in this session ─────────────────
        # If the session has MySQL / PostgreSQL / etc. connections alongside CSV
        # uploads, sync them now and merge their tables into the same response so
        # the frontend gets a single unified summary for ALL source types.
        try:
            DB_TYPES = {'mysql', 'postgres', 'postgresql', 'mssql', 'sqlite', 'tally'}
            cursor.execute("""
                SELECT ch.id, dc.db_type
                FROM connection_history ch
                JOIN database_credential dc ON dc.connection_id = ch.id
                WHERE ch.session_id = %s
                AND dc.user_id = %s
                AND LOWER(dc.db_type) IN ('mysql','postgres','postgresql','mssql','sqlite','tally')
                GROUP BY ch.id, dc.db_type
            """, (session_id, user_id))
            db_connections = cursor.fetchall()

            if db_connections:
                from database.external_sync_service import sync_external_database
                seen_tables = {t["table"] for t in affected_tables_info}
                for row in db_connections:
                    cid = row["id"]
                    try:
                        sync_res = sync_external_database(user_id, cid, session_id)
                        if isinstance(sync_res, dict):
                            for t in sync_res.get("tables", []):
                                tname = t.get("table", "")
                                if tname not in seen_tables:
                                    affected_tables_info.append(t)
                                    seen_tables.add(tname)
                                    total_rows += t.get("rows", 0)
                                    total_columns += t.get("columns", 0)
                    except Exception as db_sync_err:
                        print(f"[Import] DB sync skipped for connection {cid}: {db_sync_err}")
        except Exception as db_scan_err:
            print(f"[Import] DB connection scan skipped: {db_scan_err}")

        # Update (never recreate) the workspace's Knowledge Graph now that this
        # credential-based import has written into it. build_kgraph() itself
        # takes a kgraph_backups snapshot of the prior graph before touching it,
        # and only re-evaluates newly added tables when a verified graph already
        # exists (incremental mode) — see database/kgraph_builder.py.
        # NOTE: Run in a background thread so the HTTP response is NOT blocked
        # by the LLM call inside build_kgraph (which can take 30-60 s with a
        # local model), which was causing a 504 Gateway Timeout on the frontend.
        if imported_files:
            import threading
            def _bg_kgraph(db, host, user, pwd, port):
                try:
                    from database.kgraph_builder import build_kgraph
                    build_kgraph(db, host, user, pwd, port,
                                 trigger_source="import_csv_data")
                except Exception as kg_err:
                    print(f"[KGRAPH] post-import build skipped: {kg_err}")
            threading.Thread(
                target=_bg_kgraph,
                args=(user_db, MYSQL_CONFIG["host"], MYSQL_CONFIG["user"],
                      MYSQL_CONFIG["password"], MYSQL_CONFIG.get("port", 3306)),
                daemon=True
            ).start()

        return jsonify({
            "message": "Data imported successfully",
            "status": "success",
            "imported_files": imported_files,
            "data": {
                "summary": {
                    "total_rows": total_rows,
                    "total_columns": total_columns,
                    "data_size_bytes": total_data_size_bytes,
                    "data_size_mb": data_size_mb,
                    "last_sync": "Just now"
                },
                "tables": affected_tables_info
            }
        })


    except Exception as e:
        from utils.error_formatter import format_db_error
        failed_file = locals().get('file', 'CSV data')
        smart_message = format_db_error(e, failed_file)
        
        try:
            # Rollback any created tables in user_db
            user_cursor_local = locals().get('user_cursor')
            created_tables_local = locals().get('created_tables', set())
            if user_cursor_local and created_tables_local:
                for t in created_tables_local:
                    user_cursor_local.execute(f"DROP TABLE IF EXISTS `{t}`")
        except Exception as rollback_e:
            print("Failed to rollback tables:", rollback_e)
            
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 1. Update connection_history status to 'failed' ONLY for the specific file
            failed_cid = locals().get('file_to_cid', {}).get(failed_file)
            
            if failed_cid:
                cursor.execute("UPDATE connection_history SET status = 'failed' WHERE id = %s", (failed_cid,))
            elif connection_ids:
                format_strings = ','.join(['%s'] * len(connection_ids))
                cursor.execute(f"UPDATE connection_history SET status = 'failed' WHERE id IN ({format_strings})", tuple(connection_ids))
                
            # 2. Insert into error_logs
            session_id_local = locals().get('session_id')
            if session_id_local:
                cursor.execute("""
                    INSERT INTO error_logs (session_name, module_name, error)
                    VALUES (%s, %s, %s)
                """, (session_id_local, 'import_csv_data', smart_message))
            
            conn.commit()
        except Exception as log_e:
            print("Failed to log error:", log_e)

        print("ERROR:", str(e))
        return jsonify({
            "status": "error",
            "message": smart_message
        }), 500

