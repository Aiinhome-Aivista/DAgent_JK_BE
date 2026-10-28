import re
import os
import uuid
import json
import pandas as pd
from tqdm import tqdm
from flask import request, jsonify
from urllib.parse import quote_plus
from sqlalchemy import create_engine , text
# pyrefly: ignore [missing-import]
from apscheduler.schedulers.background import BackgroundScheduler
from database.csv_processor import process_csv_job
from database.sql_processor import detect_sql_dialect, parse_mysql_or_pg, parse_mssql, process_sql_job
from database.doc_processor import process_doc_job
from database.config import MYSQL_CONFIG


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "..", "uploads")
UPLOAD_DIR = os.path.abspath(UPLOAD_DIR)

os.makedirs(UPLOAD_DIR, exist_ok=True)
scheduler = BackgroundScheduler()

if not scheduler.running:
    scheduler.start()

def is_workspace_processing(workspace_db_name):
    """
    Checks if there are any active document or CSV processing jobs 
    for the given workspace database in the APScheduler.
    """
    if not workspace_db_name:
        return False
        
    for job in scheduler.get_jobs():
        # Both process_doc_job and process_csv_job receive allocated_db_name as their second argument (args[1])
        if job.args and len(job.args) > 1 and job.args[1] == workspace_db_name:
            return True
    return False


# =========================================================
# 1. HELPER: DETECTOR & PARSERS (Moved to database/sql_processor.py)
# =========================================================

def merge_chunks(folder, filename):

    merged_path = os.path.join(UPLOAD_DIR, filename)

    parts = sorted(
        os.listdir(folder),
        key=lambda x: int(x.split(".")[0])
    )

    with open(merged_path, "wb") as outfile:
        for part in parts:
            part_path = os.path.join(folder, part)

            with open(part_path, "rb") as infile:
                outfile.write(infile.read())

    return merged_path
# =========================================================
# 2. MAIN CONTROLLER
# =========================================================
def upload_universal_dump_controller(get_db_connection):
    session_id = request.form.get('session_id')
    connection_id = request.form.get('connection_id')

    if not session_id or not connection_id:
        return jsonify({"status": "error", "statuscode": 400, "message": "session_id and connection_id are required"}), 400

    if 'file' not in request.files and 'files' not in request.files:
        return jsonify({"status": "error", "statuscode": 400, "message": "No file uploaded"}), 400

    uploaded_files = request.files.getlist('files')
    if not uploaded_files and 'file' in request.files:
        uploaded_files = request.files.getlist('file')

    main_conn = None
    main_cursor = None
    
    try:
        # --- STEP 1: Fetch Credentials & User ID ---
        main_conn = get_db_connection()
        main_cursor = main_conn.cursor(dictionary=True)
        
        # PERFECTLY MATCHED TO YOUR database_credential TABLE
        query = "SELECT `user_id`, `db_type`, `credential` FROM `database_credential` WHERE `connection_id` = %s AND `session_id` = %s"
        main_cursor.execute(query, (connection_id, session_id))
        
        target_db = main_cursor.fetchone()
        
        if not target_db:
            main_cursor.close()
            main_conn.close()
            return jsonify({"status": "error", "statuscode": 404, "message": "Database connection not found."}), 404

        target_db_type = target_db['db_type']
        user_id = target_db['user_id']
        creds = json.loads(target_db['credential'])
        
        db_user = creds.get('user') or creds.get('username') or ''
        db_pass = creds.get('password') or creds.get('pwd') or ''
        db_host = creds.get('host') or creds.get('server') or 'localhost'
        db_name = creds.get('database') or creds.get('dbname') or ''
        
        # Safely get connection name or generate a fallback
        conn_name = creds.get('connection_name') or f"External {target_db_type.upper()} DB"

        # --- STEP 2: Connect to External DB ---
        ext_conn = None
        ext_cursor = None
        if target_db_type == 'mysql':
            import pymysql
            ext_conn = pymysql.connect(
                host=db_host, port=int(creds.get('port') or 3306),
                user=db_user, password=db_pass, database=db_name
            )
        elif target_db_type == 'mssql':
            # pyrefly: ignore [missing-import]
            import pyodbc
            import platform
            
            drivers = pyodbc.drivers()
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
                
            conn_str = f"DRIVER={{{selected_driver}}};SERVER={db_host};PORT={creds.get('port') or 1433};DATABASE={db_name};UID={db_user};PWD={db_pass};Encrypt=no;TrustServerCertificate=yes"
            if selected_driver == "FreeTDS":
                conn_str += ";TDS_Version=7.4"
                
            ext_conn = pyodbc.connect(conn_str)
        elif target_db_type in ['postgresql', 'postgres']:
            import psycopg2
            schema = creds.get('schema')
            if schema:
                ext_conn = psycopg2.connect(
                    host=db_host, port=creds.get('port') or 5432,
                    user=db_user, password=db_pass, dbname=db_name,
                    options=f"-c search_path={schema}"
                )
            else:
                ext_conn = psycopg2.connect(
                    host=db_host, port=creds.get('port') or 5432,
                    user=db_user, password=db_pass, dbname=db_name
                )

        if ext_conn:
            ext_cursor = ext_conn.cursor()

        # --- STEP 3: Iterate & Execute Files ---
        total_executed = 0
        file_names = []

        try:
            for file in uploaded_files:
                sql_content = file.read().decode('utf-8')
                file_dialect = detect_sql_dialect(sql_content)

                # Gatekeeper check (optional skip if unknown or doesn't match roughly)
                if file_dialect != "unknown" and file_dialect not in target_db_type and target_db_type not in file_dialect:
                    pass # We will let it try anyway, as dialect detect is fuzzy

                if target_db_type == 'mysql' or target_db_type in ['postgresql', 'postgres']:
                    commands = parse_mysql_or_pg(sql_content)
                elif target_db_type == 'mssql':
                    commands = parse_mssql(sql_content)
                else:
                    commands = []

                if ext_cursor:
                    for cmd in commands: 
                        ext_cursor.execute(cmd)
                    ext_conn.commit()

                total_executed += len(commands)
                file_names.append(file.filename)
                
        finally:
            if ext_cursor: ext_cursor.close()
            if ext_conn: ext_conn.close()

        # --- STEP 4: Log Success to connection_history ---
        log_query = """
            INSERT INTO `connection_history` 
            (`user_id`, `connection_name`, `db_type`, `target_host`, `status`, `session_id`) 
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        files_str = ", ".join(file_names)
        history_name = f"Uploaded Dump: {files_str} to {conn_name}"
        
        main_cursor.execute(log_query, (
            user_id,             # from database_credential
            history_name,        # dynamically generated name
            'sql_upload',        # db_type indicator
            db_host,             # target_host
            'Success',           # status
            session_id           # session_id
        ))
        
        # --- Track Daily Upload Limit ---
        for fname in file_names:
            main_cursor.execute("""
                INSERT INTO unstructured_docs (filename, session_name, extracted_json) 
                VALUES (%s, %s, %s)
            """, (fname, session_id, '{}'))

        main_conn.commit()
        
        main_cursor.close()
        main_conn.close()

        return jsonify({
            "status": "success",
            "statuscode": 200,
            "message": f"Successfully executed {total_executed} commands from {len(uploaded_files)} files on {conn_name}."
        }), 200

    except Exception as e:
        if main_conn and main_cursor:
            main_cursor.close()
            main_conn.close()
            
        return jsonify({
            "status": "error", 
            "statuscode": 500, 
            "message": f"Execution failed: {str(e)}"
        }), 500


CHUNK_DIR = os.path.join(BASE_DIR, "..", "chunk_uploads")
CHUNK_DIR = os.path.abspath(CHUNK_DIR)
os.makedirs(CHUNK_DIR, exist_ok=True)


def upload_chunk_controller(get_db_connection):

    chunk = request.files.get("chunk")
    chunk_index = request.form.get("chunk_index")
    total_chunks = request.form.get("total_chunks")
    session_id = request.form.get("session_id")
    filename = request.form.get("filename")

    if not chunk:
        return jsonify({"status":"error","message":"chunk missing"}),400

    session_folder = os.path.join(CHUNK_DIR, session_id, filename)
    
    if chunk_index == "0" and os.path.exists(session_folder):
        import shutil
        shutil.rmtree(session_folder)
        
    os.makedirs(session_folder, exist_ok=True)

    chunk_path = os.path.join(session_folder, f"{chunk_index}.part")
    chunk.save(chunk_path)

    uploaded = len([f for f in os.listdir(session_folder) if f.endswith(".part")])

    if uploaded == int(total_chunks):

        merged_path = merge_chunks(session_folder, filename)

        print(f"Merged file ready: {merged_path}")

        user_id = request.form.get("user_id")
        session_id = request.form.get("session_id")

        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT workspace_db
            FROM workspaces
            WHERE session_id=%s
        """, (session_id,))

        user_data = cursor.fetchone()
        allocated_db_name = user_data["workspace_db"] if user_data else None

        # 🔹 REPLACEMENT LOGIC: Delete existing history for this exact file in this session
        history_name = f"Chunk Upload: {filename} to allocated DB ({allocated_db_name})"
        cursor.execute("""
            SELECT id FROM connection_history 
            WHERE session_id = %s AND connection_name = %s
        """, (session_id, history_name))
        existing_history = cursor.fetchall()
        for row in existing_history:
            cursor.execute("DELETE FROM database_credential WHERE connection_id = %s", (row['id'],))
            cursor.execute("DELETE FROM connection_history WHERE id = %s", (row['id'],))

        # 🔹 INSERT INTO connection_history
        db_type_hist = 'sql_chunk_upload' if filename.lower().endswith('.sql') else 'doc_chunk_upload' if filename.lower().endswith(('.pdf', '.doc', '.docx')) else 'csv_chunk_upload'
        db_type_cred = 'sql_upload' if filename.lower().endswith('.sql') else 'doc_upload' if filename.lower().endswith(('.pdf', '.doc', '.docx')) else 'csv_upload'

        initial_status = 'Success'

        cursor.execute("""
            INSERT INTO connection_history
            (user_id, session_id, connection_name, db_type, target_host, status)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, session_id, history_name, db_type_hist, MYSQL_CONFIG.get("host"), initial_status))

        ch_id = cursor.lastrowid

        cred_json = json.dumps({"files":[filename], "merged_path": merged_path})

        cursor.execute("""
            INSERT INTO database_credential
            (user_id, session_id, connection_id, db_type, credential)
            VALUES (%s,%s,%s,%s,%s)
        """,(user_id,session_id,ch_id,db_type_cred,cred_json))

        # --- Track Daily Upload Limit ---
        cursor.execute("""
            INSERT INTO unstructured_docs (filename, session_name, extracted_json) 
            VALUES (%s, %s, %s)
        """, (filename, session_id, '{}'))

        db_conn.commit()

        # Fetch username for logging before closing connection
        cursor.execute("SELECT name FROM users WHERE id = %s", (user_id,))
        user_row = cursor.fetchone()
        username = user_row["name"] if user_row else "unknown"

        cursor.close()
        db_conn.close()

        # REMOVED: Automatic triggering of background jobs. They will be triggered during "Import" via external_db.py.

        import shutil
        shutil.rmtree(session_folder)

    return jsonify({
        "status":"success",
        "chunk_index":chunk_index
    })

# =========================================================
# FINAL: Handles the /upload_csv route (POST)
# 1. Validates user against workspace_users table
# 2. Streams huge CSVs directly into User-Allocated Database
# =========================================================
def upload_csv_controller(get_db_connection):

    user_id = request.form.get('user_id')
    session_id = request.form.get('session_id')

    if not user_id or not session_id:
        return jsonify({"status": "error", "message": "user_id and session_id required"}), 400

    uploaded_files = request.files.getlist('files')

    try:

        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        # Validate workspace
        cursor.execute("""
            SELECT user_id
            FROM workspace_users
            WHERE session_id=%s AND user_id=%s
        """, (session_id, user_id))

        if not cursor.fetchone():
            return jsonify({
                "status": "error",
                "message": "Access denied for workspace"
            }), 403

        cursor.execute("""
            SELECT workspace_db
            FROM workspaces
            WHERE session_id=%s
        """, (session_id,))

        user_data = cursor.fetchone()

        if not user_data or not user_data["workspace_db"]:
            return jsonify({"status":"error","message":"Workspace DB missing"}),404

        allocated_db_name = user_data["workspace_db"]

        # DB server credentials
        db_user = MYSQL_CONFIG.get("user")
        db_pass = MYSQL_CONFIG.get("password")
        db_host = MYSQL_CONFIG.get("host")
        db_port = int(MYSQL_CONFIG.get("port", 3306))

        # Save uploaded files
        saved_paths = []
        original_filenames = []

        for file in uploaded_files:
            original_filenames.append(file.filename)
            
            filename = f"{uuid.uuid4()}_{file.filename}"
            path = os.path.join(UPLOAD_DIR, filename)

            file.save(path)
            saved_paths.append(path)

        cursor.execute("SELECT email FROM users WHERE id=%s", (user_id,))
        user_row = cursor.fetchone()
        username = user_row['email'].split("@")[0] if user_row else "unknown"

        # Schedule background processing
        job = scheduler.add_job(
            func=process_csv_job,
            args=[
                saved_paths, allocated_db_name, db_host, db_user, db_pass, db_port, 
                "csv_upload", session_id, user_id, username
            ],
            trigger='date',
            id=str(uuid.uuid4()),
            replace_existing=True
        )

        print(f"Scheduled CSV processing job: {job.id}")

        # =========================================================
        # 3. INSERT HISTORY INTO DATABASE RIGHT HERE
        # =========================================================
        try:
            import json
            history_name = f"Uploaded {len(uploaded_files)} CSV(s) to allocated DB ({allocated_db_name})"
            
            # Save the main history row
            cursor.execute("""
                INSERT INTO connection_history (user_id, session_id, connection_name, db_type, target_host, status)
                VALUES (%s, %s, %s, 'csv_upload', %s, 'Success')
            """, (user_id, session_id, history_name, db_host))
            
            ch_id = cursor.lastrowid
            
            # Save the ACTUAL FILE NAMES into the credential JSON column
            cred_json = json.dumps({"files": original_filenames})
            cursor.execute("""
                INSERT INTO database_credential
                (user_id, connection_id, session_id, db_type, credential)
                VALUES (%s,%s,%s,'csv_upload',%s)
            """, (user_id, ch_id, session_id, cred_json))
            
            # --- Track Daily Upload Limit ---
            for orig_name in original_filenames:
                cursor.execute("""
                    INSERT INTO unstructured_docs (filename, session_name, extracted_json) 
                    VALUES (%s, %s, %s)
                """, (orig_name, session_id, '{}'))
                
            db_conn.commit()
        except Exception as log_err:
            print(f"Error logging CSV history: {log_err}")
        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "accepted",
            "message": "CSV upload scheduled for processing",
            "files": len(saved_paths)
        }), 200

    except Exception as e:
        return jsonify({
            "status":"error",
            "message":str(e)
        }),500
