import os
import json
import pandas as pd
import pymysql
from database.db_connection import get_db_connection
from database.config import MYSQL_CONFIG


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "uploads"))


def sync_csv_to_user_db(user_id, connection_id, session_id):

    try:

        conn = get_db_connection()
        cursor = conn.cursor()

        imported_files = []   # ✅ imported file track 

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

        cursor.execute(query, (connection_id, user_id, session_id))
        result = cursor.fetchone()



        if not result:
            return {"status": "error", "message": "Credential not found"}

        credential = json.loads(result["credential"])
        files = credential.get("files", [])

        print("FILES FROM DB:", files)

        # fetch workspace db
        cursor.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s", (session_id,))
        workspace_data = cursor.fetchone()
        
        if not workspace_data or not workspace_data.get("workspace_db"):
            return {"status": "error", "message": "Workspace database not found"}
            
        user_db = workspace_data["workspace_db"]
        
        # also fetch username for logs
        cursor.execute("SELECT name FROM users WHERE id=%s", (user_id,))
        user_data = cursor.fetchone()
        username = user_data["name"] if user_data else "Unknown"

        print("User DB:", user_db)

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
        print("FILES IN UPLOAD FOLDER:", files_in_folder)

        for file in files:

            matched_file = None

            for f in files_in_folder:
                if f.lower() == file.lower():
                    matched_file = f
                    break

            if not matched_file:
                print("File not found:", file)
                continue

            file_path = os.path.join(UPLOAD_DIR, matched_file)

            print("Processing file:", file_path)

            if matched_file.lower().endswith('.sql'):
                from controllers.uploads_controller import detect_sql_dialect, parse_mysql_or_pg, parse_mssql
                import re
                with open(file_path, 'r', encoding='utf-8') as f:
                    sql_content = f.read()
                    
                file_dialect = detect_sql_dialect(sql_content)
                if file_dialect == 'mssql':
                    commands = parse_mssql(sql_content)
                else:
                    commands = parse_mysql_or_pg(sql_content)
                    
                affected_tables = set()
                for cmd in commands:
                    # Skip CREATE DATABASE and USE statements to force tables into user_db
                    if re.search(r'(?i)^\s*(CREATE\s+DATABASE|USE)\s+', cmd):
                        continue
                        
                    try:
                        user_cursor.execute(cmd)
                    except Exception as e:
                        print(f"Ignoring SQL execution error in sync_csv_to_user_db: {e}")
                        
                    # Extract table name to log it like MySQL/Postgres direct connections do
                    m = re.search(r'(?i)(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|INSERT\s+INTO(?:\s+IGNORE)?|UPDATE|TRUNCATE\s+TABLE)\s+`?([a-zA-Z0-9_]+)`?', cmd)
                    if m:
                        affected_tables.add(m.group(1))
                        
                user_conn.commit()
                
                db_type_log = 'sql_upload'
                rows = len(commands)
                
                if not affected_tables:
                    # fallback if regex found nothing
                    affected_tables.add(file.lower())
                    
                for t_name in affected_tables:
                    log_query = """
                    INSERT INTO external_db_sync_log
                    (user_id,username,external_database,table_name,
                    action_type,rows_affected,session_id,new_user_db)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """
                    cursor.execute(log_query, (
                        user_id,
                        username,
                        db_type_log,
                        t_name,
                        "IMPORT",
                        rows,
                        session_id,
                        user_db
                    ))

                print(f"Executed {rows} commands from SQL file {file}. Logged tables: {affected_tables}")
                imported_files.append(file)
                continue  # Skip the CSV logging logic below

            else:
                df = pd.read_csv(file_path, encoding="utf-8-sig")

                if df.empty:
                    print("CSV file empty:", file)
                    continue

                df.columns = df.columns.str.strip().str.replace(" ", "_")
                
                # Replace NaN with None so PyMySQL inserts as NULL instead of crashing
                df = df.where(pd.notnull(df), None)

                table_name = file.replace(".csv", "").lower()

                # create table
                cols = ", ".join([f"`{c}` TEXT" for c in df.columns])
                create_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` ({cols})"

                user_cursor.execute(create_query)

                # check if table is empty before inserting
                user_cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table_name}`")
                row_cnt = user_cursor.fetchone()['cnt']

                if row_cnt == 0:
                    # insert data
                    columns = ", ".join([f"`{c}`" for c in df.columns])
                    placeholders = ", ".join(["%s"] * len(df.columns))

                    insert_query = f"""
                    INSERT INTO `{table_name}` ({columns})
                    VALUES ({placeholders})
                    """

                    data = [tuple(None if pd.isna(x) else x for x in row) for row in df.values]

                    user_cursor.executemany(insert_query, data)
                    rows = len(df)
                    print(f"Inserted {rows} rows into {table_name}")
                else:
                    rows = row_cnt
                    print(f"Table {table_name} already has data, skipping insert.")

                db_type_log = 'csv_upload'

            # ✅ imported file track
            imported_files.append(file)

            # insert log
            log_query = """
            INSERT INTO external_db_sync_log
            (user_id,username,external_database,table_name,
            action_type,rows_affected,session_id,new_user_db)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """

            cursor.execute(log_query, (
                user_id,
                username,
                db_type_log,
                table_name,
                "IMPORT",
                rows,
                session_id,
                user_db
            ))

        return {
            "message": "Data imported successfully",
            "status": "success",
            "imported_files": imported_files
        }

    except Exception as e:

        print("ERROR:", str(e))

        return {
            "status": "error",
            "message": str(e)
        }

