import os
import json
import pymysql
import re
from flask import request, jsonify
from database.config import MYSQL_CONFIG
from controllers.uploads_controller import detect_sql_dialect, parse_mysql_or_pg, parse_mssql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "uploads"))

def import_sql_data(get_db_connection):
    data = request.json
    user_id = data.get("user_id")
    connection_id = data.get("connection_id")
    session_id = data.get("session_id")

    if not user_id or not connection_id or not session_id:
        return jsonify({"error": "Missing parameters"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        imported_files = []
        affected_tables_info = []
        total_rows = 0

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
            return jsonify({"status": "error", "message": "Credential not found"}), 404

        credential = json.loads(result["credential"])
        files = credential.get("files", [])

        # fetch user db
        cursor.execute("SELECT name,new_user_db FROM users WHERE id=%s", (user_id,))
        user_data = cursor.fetchone()

        username = user_data["name"]
        user_db = user_data["new_user_db"]

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

        for file in files:
            if not file.lower().endswith('.sql'):
                continue

            matched_file = next((f for f in files_in_folder if f.lower() == file.lower()), None)
            if not matched_file:
                continue

            file_path = os.path.join(UPLOAD_DIR, matched_file)
            
            with open(file_path, 'r', encoding='utf-8') as f:
                sql_content = f.read()
                
            file_dialect = detect_sql_dialect(sql_content)
            if file_dialect == 'mssql':
                commands = parse_mssql(sql_content)
            else:
                commands = parse_mysql_or_pg(sql_content)
                
            affected_tables = set()
            for cmd in commands:
                if re.search(r'(?i)^\s*(CREATE\s+DATABASE|USE)\s+', cmd):
                    continue
                    
                try:
                    user_cursor.execute(cmd)
                except Exception as e:
                    print(f"Ignoring SQL execution error: {e}")
                    
                m = re.search(r'(?i)(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|INSERT\s+INTO(?:\s+IGNORE)?|UPDATE|TRUNCATE\s+TABLE)\s+`?([a-zA-Z0-9_]+)`?', cmd)
                if m:
                    affected_tables.add(m.group(1))
                    
            user_conn.commit()
            
            if not affected_tables:
                affected_tables.add(file.lower().replace('.sql', ''))
            
            # Fetch metadata for affected tables
            for t_name in affected_tables:
                try:
                    # Get column count
                    user_cursor.execute(f"""
                        SELECT COUNT(*) as col_count 
                        FROM information_schema.columns 
                        WHERE table_schema = '{user_db}' AND table_name = '{t_name}'
                    """)
                    col_count = user_cursor.fetchone()['col_count'] or 0

                    # Get row count
                    user_cursor.execute(f"SELECT COUNT(*) as row_count FROM `{t_name}`")
                    row_count = user_cursor.fetchone()['row_count'] or 0

                    affected_tables_info.append({
                        "table": t_name,
                        "rows": row_count,
                        "columns": col_count
                    })
                    total_rows += row_count
                except Exception as e:
                    print(f"Error fetching metadata for table {t_name}: {e}")
                    affected_tables_info.append({
                        "table": t_name,
                        "rows": 0,
                        "columns": 0
                    })

            # exact file size
            file_size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            exact_size_mb = file_size_bytes / (1024 * 1024)

            rows_affected = len(commands)
            for t_name in affected_tables:
                t_info = next((t for t in affected_tables_info if t["table"] == t_name), {"rows": 0, "columns": 0})
                t_rows = t_info["rows"]
                t_cols = t_info["columns"]
                t_size = round((t_rows * 200) / (1024 * 1024), 2)
                
                log_query = """
                INSERT INTO external_db_sync_log
                (user_id,username,external_database,table_name,
                action_type,rows_affected,session_id,new_user_db,
                total_rows,total_columns,data_size_mb,exact_size_mb)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """
                cursor.execute(log_query, (
                    user_id, username, file, t_name,
                    "IMPORT", rows_affected, session_id, user_db,
                    t_rows, t_cols, t_size, exact_size_mb
                ))
            conn.commit()

            imported_files.append(file)

        # Use the exact size for response summary if there's only one file, otherwise keep an aggregate or just use the last one
        data_size_mb = max(exact_size_mb, 0.000001) if 'exact_size_mb' in locals() else 0.0

        return jsonify({
            "message": "Data imported successfully",
            "status": "success",
            "imported_files": imported_files,
            "data": {
                "summary": {
                    "total_rows": total_rows,
                    "total_columns": sum(t["columns"] for t in affected_tables_info),
                    "data_size_mb": max(data_size_mb, 0.01),
                    "last_sync": "Just now"
                },
                "tables": affected_tables_info
            }
        })

    except Exception as e:
        from utils.error_formatter import format_db_error
        failed_file = locals().get('file', 'SQL data')
        smart_message = format_db_error(e, failed_file)
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # 1. Update connection_history status to 'failed'
            if connection_id:
                cursor.execute("UPDATE connection_history SET status = 'failed' WHERE id = %s", (connection_id,))
                
            # 2. Insert into error_logs
            cursor.execute("""
                INSERT INTO error_logs (session_name, module_name, error)
                VALUES (%s, %s, %s)
            """, (session_id, 'import_sql_data', smart_message))
            
            conn.commit()
        except Exception as log_e:
            print("Failed to log error:", log_e)

        print("ERROR:", str(e))
        return jsonify({
            "status": "error",
            "message": smart_message
        }), 500
