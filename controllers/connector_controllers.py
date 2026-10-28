import json
import uuid
import smtplib
import pandas as pd
from utils.crypto_utils import encrypt_password, decrypt_password
from database import config
from flask import request, jsonify
from flask import request, jsonify
from urllib.parse import quote_plus
from email.mime.text import MIMEText
from sqlalchemy import create_engine, text
from email.mime.multipart import MIMEMultipart
# pyrefly: ignore [missing-import]
from snowflake.sqlalchemy import URL
from database.user_db_service import create_workspace_database, create_workspace_arango_database
from helper.workspace_wise_store_procedures import run_stored_procedures
# This stores active engines in memory for the agent to use
active_connectors = {}
active_ssh_tunnels = {}


# =========================================================
# NEW: Handles the /users route (GET)
# Returns all users with id, name, email
# =========================================================
def get_all_users_controller(get_db_connection):
    """Fetch all users with their id, name, and email."""
    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({
                "status": "error",
                "statuscode": 500,
                "message": "Cannot connect to database"
            }), 500

        cursor = db_conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT u.id, u.name, u.email, u.password, u.visibility, u.role_id, u.company_id, c.company_name, u.created_at, u.updated_at, GROUP_CONCAT(wu.workspace_name SEPARATOR ', ') as workspaces
            FROM users u
            LEFT JOIN workspace_users wu ON u.id = wu.user_id
            LEFT JOIN companies c ON u.company_id = c.id
            WHERE u.role_id IN (2, 4)
            GROUP BY u.id, c.company_name
            ORDER BY COALESCE(u.updated_at, u.created_at) DESC
        """)
        users = cursor.fetchall()
        for user in users:
            user['password'] = decrypt_password(user['password'])

        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "statuscode": 200,
            "total": len(users),
            "users": users
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "statuscode": 500,
            "message": str(e)
        }), 500


# =========================================================
# MODIFIED: Handles the /create_workspace route (POST)
# Only Admin (role_id = 1) can create a workspace
# Old payload is unchanged — only role check is added
# =========================================================
def create_workspace_controller(get_db_connection):
    """Creates a brand new workspace. Only Admin users can create workspaces."""
    data = request.json

    user_id = data.get('user_id')
    workspace_name = data.get('workspace_name', 'Untitled Workspace')
    workspace_type = data.get('workspace_type', 'Generic')

    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required"}), 400

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"status": "error", "message": "Cannot connect to database"}), 500

        cursor = db_conn.cursor(dictionary=True)

        # --- 1. VERIFY USER EXISTS AND CHECK ROLE ---
        cursor.execute("SELECT id, role_id FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

        if not user:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": f"Invalid User: User ID {user_id} does not exist in the system."
            }), 404

        # Only Admin (role_id = 2) can create workspaces, unless it's a default workspace
        is_default = workspace_name.startswith("default_")
        
        if user['role_id'] != 1 and not is_default:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": "Access denied. Only Admin users can create workspaces."
            }), 403

        cursor.execute(
            "SELECT id, session_id, workspace_name FROM workspaces WHERE user_id = %s AND workspace_name = %s",
            (user_id, workspace_name)
        )
        existing = cursor.fetchone()

        if existing:
            if is_default:
                cursor.close()
                db_conn.close()
                return jsonify({
                    "status": "success",
                    "message": "Workspace already exists, reusing it.",
                    "workspace_id": existing["id"],
                    "session_id": existing["session_id"],
                    "workspace_name": existing["workspace_name"]
                }), 200
            else:
                cursor.close()
                db_conn.close()
                return jsonify({
                    "status": "error",
                    "message": f"Workspace name '{workspace_name}' already exists. Please use a different name."
                }), 409

        # --- 3. GENERATE AND SAVE NEW WORKSPACE ---
        new_session_id = str(uuid.uuid4())
        insert_query = """
            INSERT INTO workspaces (session_id, user_id, workspace_name, workspace_type, is_active)
            VALUES (%s, %s, %s, %s, 1)
        """
        cursor.execute(insert_query, (new_session_id, user_id, workspace_name, workspace_type))
        db_conn.commit()

        # Fetch the auto-generated workspace ID
        new_workspace_id = cursor.lastrowid
        
        # --- 3.5. CREATE DEDICATED DATABASE FOR WORKSPACE ---
        db_result = create_workspace_database(new_workspace_id, workspace_name)
        workspace_db = db_result.get("db_name")

        if workspace_db:
            cursor.execute(
                "UPDATE workspaces SET workspace_db = %s WHERE id = %s",
                (workspace_db, new_workspace_id)
            )
            db_conn.commit()
            
            # --- RUN WORKSPACE WISE STORE PROCEDURES FOR TYRE SALES ---
            if workspace_type and workspace_type.lower() == 'tyre sales':
                try:
                    run_stored_procedures(workspace_db)
                except Exception as e:
                    print(f"Error running stored procedures for {workspace_db}: {e}")

        # --- 3.6. CREATE ARANGODB FOR WORKSPACE ---
        arango_result = create_workspace_arango_database(workspace_name)
        workspace_arango_db = arango_result.get("arango_db_name")
        if workspace_arango_db:
            cursor.execute(
                "UPDATE workspaces SET workspace_arango_db = %s WHERE id = %s",
                (workspace_arango_db, new_workspace_id)
            )
            db_conn.commit()

        # --- 3.7. CREATE CHROMA COLLECTION NAME FOR WORKSPACE ---
        import re
        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', workspace_name).lower()[:40]
        # ChromaDB collection names must be 3-63 characters, alphanumeric or underscores, no double dots.
        workspace_chroma_collection = f"ws_{new_workspace_id}_{safe_name}"
        
        cursor.execute(
            "UPDATE workspaces SET workspace_chroma_collection = %s WHERE id = %s",
            (workspace_chroma_collection, new_workspace_id)
        )
        db_conn.commit()

        # --- 4. AUTO-ASSIGN USER TO WORKSPACE ---
        assign_query = """
            INSERT INTO workspace_users (workspace_id, workspace_name, session_id, user_id) 
            VALUES (%s, %s, %s, %s)
        """
        cursor.execute(assign_query, (new_workspace_id, workspace_name, new_session_id, user_id))
        db_conn.commit()

        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "message": f"Workspace created successfully. {db_result.get('message', '')}",
            "workspace_id": new_workspace_id,
            "session_id": new_session_id,
            "workspace_name": workspace_name,
            "workspace_db": workspace_db
        }), 201

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =========================================================
# NEW: Handles the /assign_workspace_users route (POST)
# Admin assigns one or multiple users to a workspace
# Those users can then work inside that workspace
# =========================================================
def assign_workspace_users_controller(get_db_connection):
    """
    Admin assigns multiple users to a workspace.
    Payload:
        admin_id     - ID of the admin performing the action
        workspace_id - ID of the target workspace
        user_ids     - List of user IDs to assign [ 1, 2, 3 ]
    """
    data = request.json

    admin_id    = data.get('admin_id')
    workspace_id = data.get('workspace_id')
    user_ids    = data.get('user_ids', [])   # list of user IDs

    # --- BASIC VALIDATION ---
    if not admin_id:
        return jsonify({"status": "error", "message": "admin_id is required"}), 400

    if not workspace_id:
        return jsonify({"status": "error", "message": "workspace_id is required"}), 400

    if not isinstance(user_ids, list) or len(user_ids) == 0:
        return jsonify({"status": "error", "message": "user_ids must be a non-empty list"}), 400

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"status": "error", "message": "Cannot connect to database"}), 500

        cursor = db_conn.cursor(dictionary=True)

        # --- 1. VERIFY ADMIN EXISTS AND HAS ADMIN ROLE ---
        cursor.execute("SELECT id, role_id FROM users WHERE id = %s", (admin_id,))
        admin_user = cursor.fetchone()

        if not admin_user:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": f"Invalid admin: User ID {admin_id} does not exist."
            }), 404

        if admin_user['role_id'] != 1:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": "Access denied. Only Admin users can assign users to workspaces."
            }), 403

        # --- 2. VERIFY WORKSPACE EXISTS ---
        cursor.execute("SELECT id, session_id, workspace_name FROM workspaces WHERE id = %s", (workspace_id,))
        workspace = cursor.fetchone()

        if not workspace:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": f"Workspace ID {workspace_id} does not exist."
            }), 404

        # --- 3. VERIFY ALL PROVIDED USER IDs EXIST ---
        format_placeholders = ','.join(['%s'] * len(user_ids))
        cursor.execute(
            f"SELECT id FROM users WHERE id IN ({format_placeholders})",
            tuple(user_ids)
        )
        found_users = [row['id'] for row in cursor.fetchall()]
        missing_users = [uid for uid in user_ids if uid not in found_users]

        if missing_users:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": f"The following user IDs do not exist: {missing_users}"
            }), 404

        # --- 4. INSERT INTO workspace_users (skip duplicates with INSERT IGNORE) ---
        assigned = []
        already_exists = []

        for uid in user_ids:
            # Check if already assigned
            cursor.execute(
                "SELECT id FROM workspace_users WHERE workspace_id = %s AND user_id = %s",
                (workspace_id, uid)
            )
            existing = cursor.fetchone()

            if existing:
                already_exists.append(uid)
            else:
                cursor.execute(
                    "INSERT INTO workspace_users (workspace_id, workspace_name, session_id, user_id) VALUES (%s, %s, %s, %s)",
                    (workspace_id, workspace['workspace_name'], workspace['session_id'], uid)
                )
                assigned.append(uid)

        db_conn.commit()
        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "message": "User assignment completed.",
            "workspace_id": workspace_id,
            "session_id": workspace['session_id'],
            "workspace_name": workspace['workspace_name'],
            "newly_assigned": assigned,
            "already_assigned": already_exists
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =========================================================
# NEW: Handles the /workspace_users route (GET)
# Returns all users assigned to a specific workspace
# =========================================================
def get_workspace_users_controller(get_db_connection):
    """
    Fetch all users assigned to a specific workspace, or all if none specified.
    Query params:
        workspace_id (optional) - ID of the workspace
    """
    data = request.json or {}
    workspace_id = data.get('workspace_id')

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"status": "error", "message": "Cannot connect to database"}), 500

        cursor = db_conn.cursor(dictionary=True)

        if workspace_id:
            query = """
                SELECT u.id, u.name, u.email, wu.assigned_at, w.workspace_name
                FROM workspace_users wu
                JOIN users u ON wu.user_id = u.id
                LEFT JOIN workspaces w ON wu.workspace_id = w.id
                WHERE wu.workspace_id = %s AND u.role_id != 1
                ORDER BY COALESCE(wu.updated_at, wu.assigned_at) DESC
            """
            cursor.execute(query, (workspace_id,))
        else:
            query = """
                SELECT u.id, u.name, u.email, wu.assigned_at, w.workspace_name
                FROM workspace_users wu
                JOIN users u ON wu.user_id = u.id
                LEFT JOIN workspaces w ON wu.workspace_id = w.id
                WHERE u.role_id != 1
                ORDER BY COALESCE(wu.updated_at, wu.assigned_at) DESC
            """
            cursor.execute(query)

        assigned_users = cursor.fetchall()

        # Format datetime for JSON
        for row in assigned_users:
            if row.get('assigned_at'):
                row['assigned_at'] = row['assigned_at'].strftime("%Y-%m-%d %H:%M:%S")

        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "statuscode": 200,
            "workspace_id": int(workspace_id) if workspace_id else None,
            "assigned_users": assigned_users
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =========================================================
# NEW: Handles the /remove_workspace_user route (DELETE)
# Admin removes a user from a workspace
# =========================================================
def remove_workspace_user_controller(get_db_connection):
    """
    Admin removes a user from a workspace.
    Payload:
        admin_id     - ID of the admin
        workspace_id - ID of the workspace
        user_id      - ID of the user to remove
    """
    data = request.json

    admin_id     = data.get('admin_id')
    workspace_id = data.get('workspace_id')
    user_id      = data.get('user_id')

    if not admin_id or not workspace_id or not user_id:
        return jsonify({
            "status": "error",
            "message": "admin_id, workspace_id, and user_id are all required"
        }), 400

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"status": "error", "message": "Cannot connect to database"}), 500

        cursor = db_conn.cursor(dictionary=True)

        # --- 1. VERIFY ADMIN ROLE ---
        cursor.execute("SELECT id, role_id FROM users WHERE id = %s", (admin_id,))
        admin_user = cursor.fetchone()

        if not admin_user or admin_user['role_id'] != 1:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": "Access denied. Only Admin users can remove users from workspaces."
            }), 403

        # --- 2. DELETE THE ASSIGNMENT ---
        cursor.execute(
            "DELETE FROM workspace_users WHERE workspace_id = %s AND user_id = %s",
            (workspace_id, user_id)
        )
        rows_affected = cursor.rowcount
        db_conn.commit()

        cursor.close()
        db_conn.close()

        if rows_affected == 0:
            return jsonify({
                "status": "error",
                "message": "No assignment found for this user in the specified workspace."
            }), 404

        return jsonify({
            "status": "success",
            "message": f"User {user_id} removed from workspace {workspace_id} successfully."
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =========================================================
# Function 1: Handles the /create_connector route (POST)
# =========================================================
def create_connector_controllers(get_db_connection):
    data = request.json
    
    user_id = data.get('user_id')
    user_session_id = data.get('session_id') # <--- MUST come from the frontend now
    
    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required"}), 400
        
    if not user_session_id:
        return jsonify({"status": "error", "message": "session_id is required. Please open a workspace first."}), 400

    # --- TABLE 1: VERIFY WORKSPACE EXISTS ---
    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"status": "error", "message": "Cannot connect to main database"}), 500
            
        cursor = db_conn.cursor(dictionary=True)
        
        # Check if the workspace (session_id) actually exists for this user
        cursor.execute("""
        SELECT w.session_id
        FROM workspaces w
        JOIN workspace_users wu ON wu.workspace_id = w.id
        WHERE w.session_id = %s AND wu.user_id = %s
        """, (user_session_id, user_id))
        workspace_exists = cursor.fetchone()
        
        if not workspace_exists:
            cursor.close()
            db_conn.close() 
            return jsonify({
                "status": "error", 
                "message": "Invalid workspace. Please create or select a valid workspace first."
            }), 404

        cursor.close()
        db_conn.close() 

    except Exception as e:
        return jsonify({"status": "error", "message": "Error verifying workspace", "details": str(e)}), 500
    # ------------------------------------------

    # --- GATHER DATA & SMART LOGIC ---
    topic = data.get('topic') 
    
    provided_type = data.get('type')
    if not provided_type and topic:
        db_type = 'web_search'
    else:
        db_type = provided_type.lower() if provided_type else 'mysql'
        
    conn_name = data.get('name') 
    
    if not conn_name and db_type == 'web_search':
        conn_name = f"Search Agent: {topic}"
        
    username = data.get('username', '').strip() if data.get('username') else ''
    password = data.get('password', '').strip() if data.get('password') else ''
    database = data.get('database')
    target_host = data.get('host') or data.get('account') 
    
    uri = ""
    status = ""
    message = ""
    error_msg = ""

    # --- TEST CONNECTIONS ---
    try:
        if db_type == 'web_search':
            if not topic:
                return jsonify({"status": "error", "message": "A 'topic' is required"}), 400
            status = "success"
            message = f"Successfully configured Web Search for topic: '{topic}'"
            
        elif db_type == 'google_sheets':
            sheet_url = data.get('url') or data.get('database')
            if not sheet_url:
                return jsonify({"status": "error", "message": "A 'url' or 'database' field is required"}), 400
            status = "success"
            message = f"Successfully configured Google Sheets connection: {conn_name}"
        elif db_type == 'tally':
            target_host = data.get('host')
            port = data.get('port')
            
            try:
                from database.tally_connector import fetch_tally_data
                company_name = data.get("database")
                
                # Test connection by fetching a simple XML payload
                fetch_tally_data(target_host, port, company_name, "Company")
                
                status = "success"
                message = f"Successfully connected to Tally database: {conn_name}"
            except Exception as e:
                status = "failed"
                message = "Failed to connect to Tally (Ensure Tally is running and XML API is accessible)"
                error_msg = str(e)
                raise Exception(error_msg)
        
        elif db_type == 'snowflake':

            host = data.get('host')

            if not host:
                return jsonify({
                    "status": "error",
                    "message": "Snowflake host is required"
                }), 400

            # Extract account name from host
            # Example:
            # gvrkcso-nm14601.snowflakecomputing.com
            # -> gvrkcso-nm14601
            account = host.split(".")[0]

            warehouse = data.get('warehouse')
            schema = data.get('schema')
            role = data.get('role')

            if not warehouse or not schema or not role:
                return jsonify({
                    "status": "error",
                    "message": "Warehouse, Schema, and Role are required for Snowflake"
                }), 400

            uri = (f"snowflake://{username}:{password}@{account}/"f"{database}/{schema}"f"?warehouse={warehouse}&role={role}")
            print("SNOWFLAKE URI:", uri)
              # ADD THESE
            engine = create_engine(uri)

            with engine.connect() as conn:
                conn.execute(text("SELECT CURRENT_VERSION()"))

            session_key = f"user_{user_id}_{conn_name}"
            active_connectors[session_key] = engine

            status = "success"
            message = f"Successfully connected to Snowflake database: {conn_name}"

        else:
            if not target_host or str(target_host).strip().lower() == 'none':
                return jsonify({"status": "error", "message": "Database 'host' is required and cannot be empty or 'None'"}), 400

            # Handle cases where user types host,port or host:port in the host field
            target_host = str(target_host).strip()
            if ',' in target_host:
                parts = target_host.split(',')
                target_host = parts[0].strip()
                if len(parts) > 1 and parts[1].strip().isdigit():
                    data['port'] = int(parts[1].strip())
            elif ':' in target_host:
                parts = target_host.split(':')
                target_host = parts[0].strip()
                if len(parts) > 1 and parts[1].strip().isdigit():
                    data['port'] = int(parts[1].strip())
            
            # Ensure the cleaned host is saved into the database credential later
            data['host'] = target_host
            port_val = data.get('port')
            port_str = f":{port_val}" if port_val else ""
            
            safe_password = quote_plus(password)
            
            from sqlalchemy.engine.url import URL

            if db_type == 'mysql':
                connect_args = {'connect_timeout': 5}
                
                import pymysql
                uri = "mysql+pymysql://"
                engine = create_engine(
                    uri, 
                    creator=lambda: pymysql.connect(
                        host=target_host,
                        port=int(port_val) if port_val else 3306,
                        user=username,
                        password=password,
                        database=database,
                        connect_timeout=5
                    )
                )
                tunnel = None
                try:
                    with engine.connect() as conn:
                        conn.execute(text("SELECT 1"))
                except Exception as direct_e:
                    ssh_host = data.get('ssh_host')
                    if ssh_host:
                        ssh_port = int(data.get('ssh_port', 22))
                        ssh_username = data.get('ssh_username')
                        ssh_password = data.get('ssh_password')
                        ssh_key_file = data.get('ssh_key_file')
                        remote_mysql_host = data.get('remote_mysql_host', target_host)
                        remote_mysql_port = int(data.get('remote_mysql_port', port_val or 3306))

                        from sshtunnel import SSHTunnelForwarder
                        tunnel = SSHTunnelForwarder(
                            (ssh_host, ssh_port),
                            ssh_username=ssh_username,
                            ssh_password=ssh_password,
                            ssh_pkey=ssh_key_file,
                            remote_bind_address=(remote_mysql_host, remote_mysql_port)
                        )
                        tunnel.start()

                        import pymysql
                        import ssl
                        
                        def safe_mysql_connect(db_name=None):
                            # First try with SSL
                            ssl_ctx = ssl.create_default_context()
                            ssl_ctx.check_hostname = False
                            ssl_ctx.verify_mode = ssl.CERT_NONE
                            
                            conn_args = {
                                'host': "127.0.0.1",
                                'port': tunnel.local_bind_port,
                                'user': username,
                                'password': password,
                                'charset': 'utf8mb4',
                                'ssl': ssl_ctx
                            }
                            if db_name:
                                conn_args['database'] = db_name
                                
                            try:
                                return pymysql.connect(**conn_args)
                            except pymysql.err.OperationalError as e:
                                # If server doesn't support SSL, retry without SSL
                                if e.args[0] == 2026 or 'SSL' in str(e):
                                    conn_args.pop('ssl')
                                    return pymysql.connect(**conn_args)
                                safe_args = {k: v for k, v in conn_args.items() if k != 'password' and k != 'ssl'}
                                safe_args['password_length'] = len(conn_args.get('password', ''))
                                safe_args['has_ssl'] = 'ssl' in conn_args
                                raise Exception(f"{str(e)} | DEBUG ARGS: {safe_args}")
                        
                        def try_tunnel_connect():
                            try:
                                conn = safe_mysql_connect(db_name=database)
                                conn.close()
                                return None
                            except Exception as e:
                                try:
                                    conn2 = safe_mysql_connect()
                                    conn2.close()
                                    return f"Authentication SUCCEEDED, but you don't have access to the database '{database}'. Check MySQL privileges for user '{username}' on database '{database}'."
                                except Exception as e2:
                                    return str(e)
                        
                        err_msg = try_tunnel_connect()
                        if err_msg:
                            raise Exception(err_msg)
                            
                        # If successful, continue as normal
                        tunnel_uri = "mysql+pymysql://"
                        
                        engine = create_engine(
                            tunnel_uri,
                            creator=lambda: safe_mysql_connect(db_name=database)
                        )
                        with engine.connect() as conn:
                            conn.execute(text("SELECT 1"))
                    else:
                        raise direct_e
            elif db_type == 'mssql':
                import pyodbc
                import platform
                drivers = pyodbc.drivers()
                
                # Prioritize drivers based on OS/availability
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
                
                if username:
                    if selected_driver == "FreeTDS":
                        uri = f"mssql+pyodbc://{username}:{safe_password}@{target_host}{port_str}/{database}?driver={driver_escaped}&tds_version=7.4&Encrypt=no&TrustServerCertificate=yes"
                    else:
                        uri = f"mssql+pyodbc://{username}:{safe_password}@{target_host}{port_str}/{database}?driver={driver_escaped}&Encrypt=no&TrustServerCertificate=yes"
                else:
                    if selected_driver == "FreeTDS":
                        uri = f"mssql+pyodbc://@{target_host}{port_str}/{database}?driver={driver_escaped}&tds_version=7.4&Encrypt=no&TrustServerCertificate=yes&Trusted_Connection=yes"
                    else:
                        uri = f"mssql+pyodbc://@{target_host}{port_str}/{database}?driver={driver_escaped}&Encrypt=no&TrustServerCertificate=yes&Trusted_Connection=yes"
                
                engine = create_engine(uri)
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            elif db_type in ['postgresql', 'postgres']:
                schema = data.get('schema')
                if schema:
                    uri = f"postgresql+psycopg2://{username}:{safe_password}@{target_host}{port_str}/{database}?options=-csearch_path%3D{schema}"
                else:
                    uri = f"postgresql+psycopg2://{username}:{safe_password}@{target_host}{port_str}/{database}"
                engine = create_engine(uri)
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            elif db_type in ['hana', 'sap_hana', 'saphana', 'sap']:
                schema = data.get('schema')
                encrypt = data.get('encrypt', False)
                h_port = int(port_val) if port_val else 30015
                tunnel = None

                # Detect SAP HANA Cloud (*.hanacloud.ondemand.com or port 443)
                is_hana_cloud = ('hanacloud.ondemand.com' in str(target_host).lower()) or (h_port == 443)
                if is_hana_cloud:
                    encrypt = True
                    data['encrypt'] = True

                ssh_host = data.get('ssh_host')
                if ssh_host:
                    ssh_port = int(data.get('ssh_port', 22))
                    ssh_username = data.get('ssh_username')
                    ssh_password = data.get('ssh_password')
                    ssh_key_file = data.get('ssh_key_file')
                    remote_hana_host = data.get('remote_mysql_host', target_host)
                    remote_hana_port = int(data.get('remote_mysql_port', h_port))

                    from sshtunnel import SSHTunnelForwarder
                    tunnel = SSHTunnelForwarder(
                        (ssh_host, ssh_port),
                        ssh_username=ssh_username,
                        ssh_password=ssh_password,
                        ssh_pkey=ssh_key_file,
                        remote_bind_address=(remote_hana_host, remote_hana_port)
                    )
                    tunnel.start()
                    target_host = "127.0.0.1"
                    h_port = tunnel.local_bind_port

                # In SAP HANA Cloud, tenant database names do not exist.
                # If user entered schema in database field (e.g. SAP_S4#AI_BI_EXT), use it as schema.
                effective_schema = schema or (database if is_hana_cloud else None)

                def build_hana_engine(use_tenant_db=True):
                    q_dict = {}
                    curr_schema = effective_schema or schema
                    if curr_schema:
                        q_dict["currentSchema"] = curr_schema
                    if encrypt:
                        q_dict["encrypt"] = "true"
                        q_dict["sslValidateCertificate"] = "false"
                    
                    db_val = database if (database and use_tenant_db and not is_hana_cloud) else None
                    hana_url = URL.create(
                        drivername="hana+hdbcli",
                        username=username,
                        password=password,
                        host=target_host,
                        port=h_port,
                        database=db_val,
                        query=q_dict
                    )
                    return create_engine(hana_url)

                try:
                    engine = build_hana_engine(use_tenant_db=True)
                    with engine.connect() as conn:
                        conn.execute(text("SELECT 1 FROM DUMMY"))
                except Exception as first_e:
                    err_str = str(first_e).lower()
                    if "not connected" in err_str or "database" in err_str or is_hana_cloud:
                        # Fallback: connect directly without tenant database and treat database as currentSchema
                        effective_schema = effective_schema or database
                        data['schema'] = effective_schema
                        engine = build_hana_engine(use_tenant_db=False)
                        with engine.connect() as conn:
                            conn.execute(text("SELECT 1 FROM DUMMY"))
                    else:
                        raise first_e
            else:
                return jsonify({"status": "error", "message": f"Unsupported connection type: {db_type}"}), 400

            session_key = f"user_{user_id}_{conn_name}"
            active_connectors[session_key] = engine
            if (db_type == 'mysql' or db_type in ['hana', 'sap_hana', 'saphana', 'sap']) and 'tunnel' in locals() and tunnel is not None:
                active_ssh_tunnels[session_key] = tunnel

            status = "success"
            message = f"Successfully connected to {db_type} database: {conn_name}"

    except Exception as e:
        status = "failed"
        message = f"Failed to connect to {db_type}"
        error_msg = str(e)

    # --- ONLY SAVE TO DATABASE IF SUCCESSFUL ---
    if status == "success":
        try:
            db_conn = get_db_connection() 
            if db_conn:
                cursor = db_conn.cursor()
                
                # --- TABLE 2: Insert into connection_history ---
                history_query = """INSERT INTO connection_history 
                                   (user_id, session_id, connection_name, db_type, target_host, status, error_message) 
                                   VALUES (%s, %s, %s, %s, %s, %s, %s)"""
                cursor.execute(history_query, (user_id, user_session_id, conn_name, db_type, target_host, status, error_msg))
                new_history_id = cursor.lastrowid
                
                # --- TABLE 3: Insert into database_credential ---
                cred_data = {
                    "host": data.get('host'), "port": data.get('port'),
                    "username": username, "password": password, 
                    "database": database, "url": data.get('url'),   
                    "account": data.get('account'), "warehouse": data.get('warehouse'),
                    "schema": data.get('schema'), "topic": topic,
                    "encrypt": data.get('encrypt', False),
                    "ssh_host": data.get('ssh_host'), "ssh_port": data.get('ssh_port'),
                    "ssh_username": data.get('ssh_username'), "ssh_password": data.get('ssh_password'),
                    "ssh_key_file": data.get('ssh_key_file'), "remote_mysql_host": data.get('remote_mysql_host'),
                    "remote_mysql_port": data.get('remote_mysql_port')
                }
                # Clean out empty values
                clean_cred_data = {}
                for k, v in cred_data.items():
                    if v is not None:
                        clean_cred_data[k] = v
                
                cred_query = """INSERT INTO database_credential 
                                (user_id, session_id, db_type, credential, connection_id) 
                                VALUES (%s, %s, %s, %s, %s)"""
                cursor.execute(cred_query, (user_id, user_session_id, db_type, json.dumps(clean_cred_data), new_history_id))
                new_connection_id = cursor.lastrowid

                # Fetch user details for sync
                cursor.execute("SELECT email FROM users WHERE id=%s", (user_id,))
                user_res = cursor.fetchone()
                user_email = user_res[0] if user_res else "unknown@mail.com"
                username_for_sync = user_email.split("@")[0]
                
                cursor.execute("SELECT workspace_db FROM workspaces WHERE session_id=%s", (user_session_id,))
                ws_res = cursor.fetchone()
                user_db_name = ws_res[0] if ws_res else None

                db_conn.commit()
                cursor.close()
                db_conn.close()
                


        except Exception as log_e:
            print(f"Logging Error: {log_e}")

    # --- RETURN FINAL RESPONSE ---
    if status == "success":
        response_data = {
            "status": "success", 
            "message": message,
            "session_id": user_session_id
        }

            
        return jsonify(response_data), 200
    else:
        return jsonify({"status": "error", "message": message, "details": error_msg}), 400


# =========================================================
# Function 2: Handles the /connection_history route (GET)
# =========================================================
def get_connection_history_controller(get_db_connection):
    """Fetches a clean timeline of connection history and saved web results grouped by topic."""
    
    session_id = request.args.get('session_id')
    
    if not session_id:
        return jsonify({
            "status": "error", 
            "statuscode": 400, 
            "message": "session_id is required in the URL parameters"
        }), 400

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({
                "status": "error", 
                "statuscode": 500, 
                "message": "Cannot connect to main database"
            }), 500
            
        cursor = db_conn.cursor(dictionary=True) 
        
        # --- 1. FETCH DATABASE CONNECTIONS ---
        query_conn = """
            SELECT ch.*, dc.connection_id, dc.credential
            FROM connection_history ch
            LEFT JOIN database_credential dc 
              ON ch.id = dc.connection_id 
            WHERE ch.session_id = %s 
        """
        cursor.execute(query_conn, (session_id,))
        raw_history = cursor.fetchall() 

        # [AUTO-FIX STUCK RECORDS]
        # Automatically change any 'Processing'/'Pending' status to 'Success' for records older than 2 hours
        import datetime
        fixed_any = False
        now = datetime.datetime.now()
        for record in raw_history:
            if record.get("status") not in ("Success", "Failed", "Completed", "Error"):
                created_at = record.get("created_at")
                if created_at and isinstance(created_at, datetime.datetime) and (now - created_at).total_seconds() > 2 * 3600:
                    record["status"] = "Success"
                    cursor.execute(
                        "UPDATE connection_history SET status = 'Success' WHERE id = %s", 
                        (record["id"],)
                    )
                    fixed_any = True
        
        if fixed_any:
            db_conn.commit()


        # --- 2. FETCH SAVED WEB RESULTS (GROUPED BY UNIQUE TOPIC) ---
        query_saved = """
            SELECT 
                topic, 
                COUNT(saved_id) as total_saved, 
                MAX(saved_at) as latest_saved_at
            FROM saved_web_results
            WHERE session_id = %s
              AND topic IS NOT NULL 
              AND topic != ''
            GROUP BY topic
        """
        cursor.execute(query_saved, (session_id,))
        raw_saved = cursor.fetchall()


        cursor.close()
        db_conn.close()


        # --- COMBINE EVERYTHING INTO ONE ARRAY ---
        combined_history = []


        # Formatting 1: Add Connections
        for row in raw_history:
            if row['db_type'] not in ['mysql', 'mssql', 'web_search', 'google_sheets', 'csv_upload', 'csv_chunk_upload','snowflake', 'postgresql', 'postgres', 'sql_upload', 'sql_chunk_upload', 'doc_upload','doc_chunk_upload', 'ftp','tally', 'sap_hana', 'hana', 'saphana', 'sap']:
                continue

            date_str = row['created_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['created_at'] else ""
            exact_id = str(row['connection_id']) if row.get('connection_id') else f"{row['id']}"


            # --- 1. SET THE ACTION STRING & DEFAULT DISPLAY NAME ---
            if row['db_type'] in ['web_search', 'google_sheets']:
                action_str = f"Configured {row['connection_name']}" 
                display_name = row['connection_name']
                
            elif row['db_type'] in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload', 'doc_upload', 'doc_chunk_upload']:
                raw_name = row.get('connection_name', '')
                is_sql = row['db_type'] in ['sql_upload', 'sql_chunk_upload'] or '.sql' in raw_name.lower()
                is_doc = row['db_type'] in ['doc_upload', 'doc_chunk_upload'] or any(ext in raw_name.lower() for ext in ['.pdf', '.doc', '.docx'])
                if is_sql:
                    if "Uploaded Dump: " in raw_name and " to " in raw_name:
                        file_names = raw_name.split("Uploaded Dump: ")[1].split(" to ")[0]
                        action_str = f"Uploaded SQL Dump: {file_names}"
                    elif "Chunk Upload: " in raw_name and " to " in raw_name:
                        file_names = raw_name.split("Chunk Upload: ")[1].split(" to ")[0]
                        action_str = f"Uploaded SQL Dump: {file_names}"
                    else:
                        action_str = "Uploaded SQL Dump"
                    display_name = "SQL Data"
                elif is_doc:
                    if "Chunk Upload: " in raw_name and " to " in raw_name:
                        file_names = raw_name.split("Chunk Upload: ")[1].split(" to ")[0]
                        action_str = f"Uploaded Document: {file_names}"
                    else:
                        action_str = raw_name.split(" to allocated DB")[0] if " to allocated DB" in raw_name else raw_name
                    display_name = "Document Data"
                else:
                    if " to allocated DB" in raw_name:
                        action_str = raw_name.split(" to allocated DB")[0] # Leaves "Uploaded 2 CSV(s)"
                    else:
                        action_str = raw_name
                    display_name = "CSV Data" 
                
            elif row['db_type'] in ['sap_hana', 'hana', 'saphana', 'sap']:
                action_str = f"Connected to {row['connection_name'] or 'SAP HANA'}"
                display_name = row['connection_name'] or "SAP HANA"
            else:
                action_str = f"Connected to {row['connection_name']}"
                display_name = row['connection_name']


            # --- 2. EXTRACT TOPIC AND FILE NAMES FROM JSON CREDENTIAL ---
            extracted_topic = ""
            if row.get('credential'):
                try:
                    cred_dict = json.loads(row['credential'])
                    extracted_topic = cred_dict.get('topic', "")
                    
                    # IF it's a CSV or Doc upload, dig into the JSON to find the actual file names
                    if row['db_type'] in ['csv_upload', 'doc_upload', 'csv_chunk_upload', 'doc_chunk_upload']:
                        # Look for common keys your upload function might have saved them under
                        files = cred_dict.get('files', []) or cred_dict.get('file_names', []) or cred_dict.get('file', '')
                        
                        if isinstance(files, list) and len(files) > 0:
                            display_name = ", ".join(files) # Joins multiple files like: "data1.csv, data2.csv"
                        elif isinstance(files, str) and files.strip() != "":
                            display_name = files
                    elif row['db_type'] in ['sap_hana', 'hana', 'saphana', 'sap']:
                        schema_name = cred_dict.get('schema') or cred_dict.get('database')
                        if schema_name:
                            action_str = f"Connected to SAP HANA ({schema_name})"
                    else:
                        db_name = cred_dict.get('database') or cred_dict.get('database_name')
                        if db_name:
                            action_str = f"Connected to {db_name}"
                except:
                    pass

            # --- 3. APPEND TO ARRAY ---
            combined_history.append({
                "id": exact_id,  
                "sessionId": row.get('session_id'),
                "date": date_str,
                "action": action_str,               # Outputs: "Uploaded 2 CSV(s)"
                "connectionName": display_name,     # Outputs: "sales_data.csv, users.csv"
                "db_type": row['db_type'],
                "topic": extracted_topic,
                "status": row.get('status') or "completed"
            })


        # Formatting 2: Add Saved Web Results (Grouped)
        for row in raw_saved:
            date_str = row['latest_saved_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['latest_saved_at'] else ""
            
            if row['total_saved'] > 1:
                action_str = f"Saved {row['total_saved']} articles for: {row['topic']}"
            else:
                action_str = f"Saved 1 article for: {row['topic']}"


            combined_history.append({
                "id": f"topic_group_{row['topic']}",
                "sessionId": session_id,
                "date": date_str,
                "action": action_str,
                "connectionName": f"Web Research: {row['topic']}",  
                "db_type": "saved_web_result", 
                "topic": row['topic'],
                "status": "completed"
            })


        # --- SORT THE COMBINED ARRAY BY DATE ---
        combined_history.sort(key=lambda x: x['date'], reverse=True)


        # --- ASSEMBLE THE FINAL MINIMAL RESPONSE ---
        return jsonify({
            "status": "success", 
            "statuscode": 200,   
            "history": combined_history 
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error", 
            "statuscode": 500, 
            "message": str(e)
        }), 500


# =========================================================
# Function 3: Handles the /agent/query_db route (POST)
# =========================================================
def agent_query_controllers():
    data = request.json
    
    user_id = data.get('user_id')
    conn_name = data.get('name')
    query = data.get('query')
    
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
        
    session_key = f"user_{user_id}_{conn_name}"
    
    if session_key not in active_connectors:
        return jsonify({"error": f"Connector '{conn_name}' not found. You must create the connection first!"}), 404
        
    try:
        engine = active_connectors[session_key]
        df = pd.read_sql(query, engine)
        return jsonify(df.to_dict(orient="records")), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# =========================================================
# Function 4: Handles the /saved_credentials route (GET)
# =========================================================
def get_saved_credentials_controller(get_db_connection):
    from flask import request, jsonify
    import json
    
    user_id = request.args.get('user_id')
    
    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required in the URL parameters"}), 400

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"error": "Cannot connect to main database"}), 500
            
        cursor = db_conn.cursor(dictionary=True) 
        
        cursor.execute("SELECT * FROM database_credential WHERE user_id = %s ORDER BY connection_id DESC", (user_id,))
        saved_creds = cursor.fetchall() 
        
        cursor.close()
        db_conn.close()

        for row in saved_creds:
            if isinstance(row['credential'], str):
                row['credential'] = json.loads(row['credential'])
                
        return jsonify({
            "status": "success", 
            "saved_connections": saved_creds
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# =========================================================
# Function 5: Handles the /workspace_history route (GET)
# =========================================================
def get_workspace_history_controller(get_db_connection):
    """Fetches connection history for a specific workspace using ONLY session_id."""
    
    session_id = request.args.get('session_id')
    
    if not session_id:
        return jsonify({"status": "error", "message": "session_id is required in the URL parameters"}), 400

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"error": "Cannot connect to main database"}), 500
            
        cursor = db_conn.cursor(dictionary=True) 
        
        query_conn = """
            SELECT ch.*, dc.connection_id, dc.credential
            FROM connection_history ch
            LEFT JOIN database_credential dc 
              ON ch.id = dc.connection_id
            WHERE ch.session_id = %s
            ORDER BY ch.created_at DESC
        """
        cursor.execute(query_conn, (session_id,))
        raw_history = cursor.fetchall() 


        # [AUTO-FIX STUCK RECORDS]
        # Automatically change any 'Processing'/'Pending' status to 'Success' for records older than 2 hours
        import datetime
        fixed_any = False
        now = datetime.datetime.now()
        for record in raw_history:
            if record.get("status") not in ("Success", "Failed", "Completed", "Error"):
                created_at = record.get("created_at")
                # Only fix if older than 2 hours to avoid interrupting active new uploads
                if created_at and isinstance(created_at, datetime.datetime) and (now - created_at).total_seconds() > 2 * 3600:
                    # Force status to Success in memory
                    record["status"] = "Success"
                    # Update it in the database
                    cursor.execute(
                        "UPDATE connection_history SET status = 'Success' WHERE id = %s", 
                        (record["id"],)
                    )
                    fixed_any = True
        
        if fixed_any:
            db_conn.commit()

        query_saved = """
            SELECT saved_id, topic, title, url, brief, session_id, saved_at
            FROM saved_web_results
            WHERE session_id = %s
            ORDER BY saved_at DESC
        """
        cursor.execute(query_saved, (session_id,))
        raw_saved = cursor.fetchall()

        cursor.close()
        db_conn.close()

        if len(raw_history) == 0 and len(raw_saved) == 0:
            return jsonify({
                "status": "success", 
                "message": "No connection found for this workspace", 
                "agents": []
            }), 200

        connect_history = []
        for row in raw_history:
            if row['db_type'] not in ['mysql', 'mssql', 'web_search', 'google_sheets','snowflake', 'postgresql', 'postgres', 'csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload', 'doc_upload', 'doc_chunk_upload', 'ftp', 'tally', 'sap_hana', 'hana', 'saphana', 'sap']:
                continue

            db_names = {
                "mysql": "MySQL", "mssql": "SQL Server",
                "web_search": "Web Search API", "google_sheets": "Google Sheets", "snowflake": "Snowflake",
                "postgresql": "PostgreSQL", "postgres": "PostgreSQL",
                "csv_upload": "CSV Upload", "csv_chunk_upload": "CSV Upload", "sql_upload": "SQL Upload",
                "sql_chunk_upload": "SQL Upload", "doc_upload": "Document Upload", "doc_chunk_upload": "Document Upload",
                "ftp": "FTP Connector", "tally": "Tally ERP",
                "sap_hana": "SAP HANA", "hana": "SAP HANA", "saphana": "SAP HANA", "sap": "SAP HANA"
            }
            db_name_display = db_names.get(row['db_type'], row['db_type'].capitalize())
            
            details = f"{db_name_display} connection established successfully."
            date_str = row['created_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['created_at'] else ""
            exact_id = str(row['connection_id']) if row.get('connection_id') else f"h{row['id']}"

            display_name = row['connection_name'] or db_name_display
            
            if row['db_type'] in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'doc_upload', 'doc_chunk_upload']:
                if row.get('credential'):
                    import json
                    try:
                        cred_dict = json.loads(row['credential'])
                        files = cred_dict.get('files', []) or cred_dict.get('file_names', []) or cred_dict.get('file', '')
                        if isinstance(files, list) and len(files) > 0:
                            display_name = ", ".join(files)
                        elif isinstance(files, str) and files.strip() != "":
                            display_name = files
                    except:
                        pass

            if row['db_type'] in ['web_search', 'google_sheets']:
                action_str = f"Configured {display_name}" 
            elif row['db_type'] in ['csv_upload', 'csv_chunk_upload', 'sql_upload']:
                action_str = f"Uploaded {display_name}"
            elif row['db_type'] in ['doc_upload', 'doc_chunk_upload']:
                action_str = f"Uploaded Document: {display_name}"
            elif row['db_type'] in ['sap_hana', 'hana', 'saphana', 'sap']:
                action_str = f"Connected to {display_name}"
            else:
                action_str = f"Connected to {display_name}"

            history_item = {
                "id": exact_id,  
                "sessionId": row.get('session_id'),
                "date": date_str,
                "action": action_str,
                "details": details,
                "connectionName": display_name,
                "status": row.get('status') or "completed",
                "db_type": row['db_type']
            }
            
            if row['db_type'] == 'web_search':
                history_item["activities"] = ["Verifying API Key...", "Establishing secure link to search provider...", "Testing query endpoints...", "Web Search ready."]
            elif row['db_type'] == 'google_sheets':
                history_item["activities"] = ["Parsing Spreadsheet URL...", "Authenticating Google API access...", "Mapping sheet tabs and columns...", "Google Sheets ready."]
            elif row['db_type'] in ['csv_upload', 'csv_chunk_upload', 'sql_upload']:
                history_item["activities"] = ["Validating file format...", "Extracting schema and data...", "Loading into workspace database...", "Data ready for AI."]
            else:
                history_item["activities"] = ["Verifying credentials...", "Establishing SSL tunnel...", f"Handshaking with {db_name_display}...", "Mapping schema structures..."]
                
            connect_history.append(history_item)

        saved_history = []
        for row in raw_saved:
            date_str = row['saved_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['saved_at'] else ""
            
            history_item = {
                "id": row['saved_id'],  
                "sessionId": row.get('session_id'),
                "date": date_str,
                "action": f"Saved Article: {row['title']}",
                "details": row['brief'],
                "connectionName": row['url'],  
                "status": "completed",
                "activities": [
                    f"Topic: {row['topic']}",
                    f"Link: {row['url']}",
                    "Web result successfully saved to workspace memory."
                ]
            }
            saved_history.append(history_item)

        agents_data = []
        
        if len(connect_history) > 0:
            agents_data.append({
                "id": 'connect',
                "name": 'Data source',
                "historyName": 'Data source',
                "icon": 'database',
                "description": 'Establishing secure link to database',
                "history": connect_history 
            })
            
        if len(saved_history) > 0:
            agents_data.append({
                "id": 'saved_results',
                "name": 'Saved Research',
                "historyName": 'Saved Web Links',
                "icon": 'link', 
                "description": 'AI web search results saved to workspace',
                "history": saved_history 
            })
        
        return jsonify({"status": "success", "agents": agents_data}), 200
        
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# =========================================================
# Function: Handles the /workspaces route (GET)
# =========================================================
def get_user_workspaces_controller(get_db_connection):
    """Fetch all workspaces for a user and ensure one active workspace."""
    from flask import request, jsonify

    user_id = request.args.get('user_id')

    if not user_id:
        return jsonify({
            "status": "error",
            "statuscode": 400,
            "message": "user_id is required"
        }), 400

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        query = """
            SELECT 
                w.id,
                w.session_id,
                w.workspace_name,
                w.is_active, w.workspace_type
            FROM workspace_users wu
            JOIN workspaces w ON wu.workspace_id = w.id
            WHERE wu.user_id = %s
            ORDER BY COALESCE(w.updated_at, w.created_at) DESC
        """
        
        cursor.execute(query, (user_id,))
        workspaces = cursor.fetchall()

        if workspaces:
            active_exists = any(w["is_active"] == 1 for w in workspaces)

            if not active_exists:
                first_workspace_id = workspaces[0]["id"]

                update_query = """
                    UPDATE workspaces
                    SET is_active = 1
                    WHERE id = %s
                """
                cursor.execute(update_query, (first_workspace_id,))
                db_conn.commit()

                workspaces[0]["is_active"] = 1

        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "statuscode": 200,
            "workspaces": workspaces
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "statuscode": 500,
            "message": str(e)
        }), 500


def set_active_workspace(get_db_connection):
    from flask import request, jsonify

    data = request.get_json()

    user_id = data.get("user_id")
    workspace_id = data.get("workspace_id")

    if not user_id or not workspace_id:
        return jsonify({
            "status": "error",
            "message": "user_id and workspace_id required"
        }), 400

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor()

        cursor.execute("""
            UPDATE workspaces
            SET is_active = 0
            WHERE user_id = %s
        """, (user_id,))

        cursor.execute("""
            UPDATE workspaces
            SET is_active = 1
            WHERE id = %s AND user_id = %s
        """, (workspace_id, user_id))

        db_conn.commit()

        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "message": "Workspace switched successfully"
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# =========================================================
# Function: Handles the /user_workspaces route (GET)
# Fetch all workspaces for a specific user
# =========================================================
def get_user_workspaces_simple(get_db_connection):

    user_id = request.args.get('user_id')

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({
                "status": "error",
                "statuscode": 500,
                "message": "Cannot connect to database"
            }), 500

        cursor = db_conn.cursor(dictionary=True)

        if user_id:
            query = """
                SELECT id, workspace_name, workspace_name as name, session_id, workspace_type
                FROM workspaces
                WHERE user_id = %s
                ORDER BY COALESCE(updated_at, created_at) DESC
            """
            cursor.execute(query, (user_id,))
        else:
            query = """
                SELECT id, workspace_name, workspace_name as name, session_id, workspace_type
                FROM workspaces
                ORDER BY COALESCE(updated_at, created_at) DESC
            """
            cursor.execute(query)
        workspaces = cursor.fetchall()

        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "statuscode": 200,
            "total": len(workspaces),
            "workspaces": workspaces
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "statuscode": 500,
            "message": str(e)
        }), 500


# =========================================================
# NEW: Handles the /create_user route (POST)
# Admin creates a new user account
# =========================================================
def create_user_controller(get_db_connection):

    data = request.get_json()

    admin_id = data.get("admin_id")
    name = data.get("name")
    email = data.get("email")
    password = data.get("password")
    visibility = data.get('visibility', 1)
    role_id = data.get('role_id', 2)
    company_id = data.get('company_id')

    if not admin_id:
        return jsonify({"status": "error", "message": "admin_id is required"}), 400

    if not name or not email or not password:
        return jsonify({
            "status": "error",
            "message": "name, email and password are required"
        }), 400

    db_conn = None
    cursor = None

    try:

        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        # --- 1. VERIFY ADMIN ---
        cursor.execute(
            "SELECT id, role_id FROM users WHERE id = %s",
            (admin_id,)
        )
        admin = cursor.fetchone()

        if not admin or admin["role_id"] != 1:
            return jsonify({
                "status": "error",
                "message": "Access denied. Only admin can create users."
            }), 403

        # --- 1.5 CHECK COMPANY USER LIMIT ---
        if company_id:
            cursor.execute('''
                SELECT c.plan_type, p.number_of_users, 
                       (SELECT COUNT(*) FROM users WHERE company_id = %s) as current_users
                FROM companies c
                LEFT JOIN pricing_plans p ON c.plan_type = p.plan_name
                WHERE c.id = %s
            ''', (company_id, company_id))
            company_data = cursor.fetchone()
            
            if company_data and company_data['number_of_users'] is not None:
                limit = company_data['number_of_users']
                current_users = company_data['current_users']
                
                # -1 or 0 means unlimited
                if limit != -1 and limit != 0 and current_users >= limit:
                    return jsonify({
                        "status": "error",
                        "message": f"User limit reached for the active plan ({limit} users max). Please upgrade to add more users."
                    }), 403


        # --- 2. CHECK EMAIL DUPLICATE ---
        cursor.execute(
            "SELECT id FROM users WHERE email = %s",
            (email,)
        )
        existing = cursor.fetchone()

        if existing:
            return jsonify({
                "status": "error",
                "message": "Email already exists"
            }), 409

        # --- 3. INSERT USER ---
        hashed_password = encrypt_password(password)
        insert_query = """
            INSERT INTO users (name, email, password, role_id, visibility, company_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """

        cursor.execute(insert_query, (name, email, hashed_password, role_id, visibility, company_id))
        db_conn.commit()

        new_user_id = cursor.lastrowid

        # --- 4. SEND EMAIL ---
        try:

            sender_email = config.MAIL_USERNAME
            sender_password = config.MAIL_PASSWORD

            msg = MIMEMultipart()
            msg["From"] = sender_email
            msg["To"] = email
            msg["Subject"] = "Your Account Has Been Created"

            body = f"""
Hello {name},

Your account has been created successfully.

Login Details:
Email: {email}
Password: {password}

Please login and change your password.

Regards,
Admin
"""

            msg.attach(MIMEText(body, "plain"))

            server = smtplib.SMTP(config.MAIL_SERVER, config.MAIL_PORT)

            if config.MAIL_USE_TLS:
                server.starttls()

            server.login(sender_email, sender_password)
            server.sendmail(sender_email, email, msg.as_string())
            server.quit()

        except Exception as mail_error:
            print("Email sending failed:", mail_error)

        return jsonify({
            "status": "success",
            "message": "User created successfully",
            "user_id": new_user_id
        }), 201

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

    finally:
        if cursor:
            cursor.close()
        if db_conn:
            db_conn.close()

def delete_connection_history_controller(get_db_connection):
    item_id = request.args.get('id')
    session_id = request.args.get('session_id')
        
    if not item_id:
        return jsonify({"status": "error", "message": "id is required"}), 400
    if not session_id:
        return jsonify({"status": "error", "message": "session_id is required"}), 400

    db_conn = None
    cursor = None
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        if str(item_id).startswith("topic_group_"):
            topic = str(item_id).replace("topic_group_", "", 1)
            cursor.execute("DELETE FROM saved_web_results WHERE session_id = %s AND topic = %s", (session_id, topic))
            db_conn.commit()
            return jsonify({"status": "success", "message": "Web search history deleted successfully."}), 200

        # Numeric DB connection
        cursor.execute("SELECT * FROM connection_history WHERE id = %s AND session_id = %s", (item_id, session_id))
        conn_history = cursor.fetchone()
        
        if not conn_history:
            return jsonify({"status": "error", "message": "Connection not found."}), 404

        user_id = conn_history["user_id"]

        # Fetch the allocated DB for the workspace
        cursor.execute("SELECT workspace_db FROM workspaces WHERE session_id = %s", (session_id,))
        workspace_row = cursor.fetchone()
        if workspace_row and workspace_row.get("workspace_db"):
            new_user_db = workspace_row["workspace_db"]
        else:
            cursor.execute("SELECT new_user_db FROM users WHERE id = %s", (user_id,))
            user_row = cursor.fetchone()
            new_user_db = user_row["new_user_db"] if user_row else None

        # Fetch credential to find external database names
        cursor.execute("SELECT credential FROM database_credential WHERE connection_id = %s", (item_id,))
        cred_row = cursor.fetchone()
        
        external_databases = []
        if cred_row and cred_row["credential"]:
            import json
            try:
                cred_json = json.loads(cred_row["credential"])
                if conn_history["db_type"] in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload', 'doc_upload', 'doc_chunk_upload']:
                    files = cred_json.get("files", [])
                    external_databases.extend(files)
                elif conn_history["db_type"] == 'ftp':
                    external_databases.append(cred_json.get("name", "FTP Connector"))
                else:
                    db_name = cred_json.get("database")
                    if db_name:
                        external_databases.append(db_name)
            except:
                pass
                
        # Fallback if external_databases is empty
        if not external_databases:
             external_databases.append(conn_history["connection_name"])

        # Find tables from sync log
        tables_to_drop = []
        if external_databases:
            format_strings = ','.join(['%s'] * len(external_databases))
            query = f"SELECT table_name FROM external_db_sync_log WHERE session_id = %s AND external_database IN ({format_strings})"
            cursor.execute(query, [session_id] + external_databases)
            sync_logs = cursor.fetchall()
            for log in sync_logs:
                if log["table_name"]:
                    tables_to_drop.append(log["table_name"])
            
            # Explicitly add workspace_files for documents to ensure physical deletion
            if conn_history["db_type"] in ['doc_upload', 'doc_chunk_upload']:
                if 'workspace_files' not in tables_to_drop:
                    tables_to_drop.append('workspace_files')

            # Delete from external_db_sync_log
            delete_log_query = f"DELETE FROM external_db_sync_log WHERE session_id = %s AND external_database IN ({format_strings})"
            cursor.execute(delete_log_query, [session_id] + external_databases)

        # Connect to new_user_db and drop tables (or rows for workspace_files)
        if new_user_db and tables_to_drop:
            # Also add prefixed versions (e.g. Poc_Users for Users) since physical name may differ
            prefixed_tables = []
            for ext_db_name in external_databases:
                for t in tables_to_drop:
                    if t != 'workspace_files':
                        prefixed_tables.append(f"{ext_db_name}_{t}")
            all_tables_to_drop = set(tables_to_drop + prefixed_tables)

            try:
                cursor.execute(f"USE `{new_user_db}`")
                cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                for table in all_tables_to_drop:
                    if table == 'workspace_files' and external_databases:
                        # For docs, delete only the row corresponding to this file
                        del_format = ','.join(['%s'] * len(external_databases))
                        try:
                            cursor.execute(f"DELETE FROM workspace_files WHERE file_name IN ({del_format})", external_databases)
                        except Exception as e:
                            print(f"[DELETE] Could not delete rows from workspace_files: {e}")
                    else:
                        try:
                            cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
                        except Exception as drop_err:
                            print(f"[DELETE] Could not drop table `{table}`: {drop_err}")
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            except Exception as use_err:
                print(f"[DELETE] Could not switch to {new_user_db}: {use_err}")
            finally:
                try:
                    cursor.execute(f"USE `{config.MYSQL_CONFIG['database']}`")
                except:
                    pass

        # Delete credential and connection history
        cursor.execute("DELETE FROM database_credential WHERE connection_id = %s", (item_id,))
        cursor.execute("DELETE FROM connection_history WHERE id = %s", (item_id,))

        # Delete SAP HANA KG & Metadata
        try:
            cursor.execute("DELETE FROM sap_hana_kgraph WHERE connection_id = %s", (item_id,))
            cursor.execute("DELETE FROM sap_hana_view_metadata WHERE connection_id = %s", (item_id,))
        except Exception as hana_del_err:
            print(f"[DELETE] Error cleaning up SAP HANA tables for connection {item_id}: {hana_del_err}")

        # Delete nodes & edges from ArangoDB for this connection and its tables
        try:
            from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB
            from arango import ArangoClient
            target_arango_db = workspace_row.get("workspace_arango_db") if workspace_row else None
            arango_db_name = target_arango_db or ARANGO_DB
            arango_client = ArangoClient(hosts=ARANGO_HOST)
            sys_db = arango_client.db('_system', username=ARANGO_USER, password=ARANGO_PASS)
            if sys_db.has_database(arango_db_name):
                adb = arango_client.db(arango_db_name, username=ARANGO_USER, password=ARANGO_PASS)
                
                # 1. Remove by connection_id (HANA or tagged items)
                for col in ["session_edges", "session_nodes"]:
                    if adb.has_collection(col):
                        try:
                            adb.aql.execute(
                                f"FOR d IN {col} FILTER d.connection_id == @cid REMOVE d IN {col}",
                                bind_vars={"cid": int(item_id)}
                            )
                        except Exception as aql_e:
                            print(f"[DELETE] ArangoDB {col} cleanup warning for connection {item_id}: {aql_e}")

                # 2. If tables were dropped (CSV, SQL, etc.), remove their corresponding nodes and connected edges
                if tables_to_drop and adb.has_collection("session_nodes") and adb.has_collection("session_edges"):
                    table_keys = [str(t) for t in tables_to_drop]
                    try:
                        # Remove edges where _from or _to belongs to dropped tables
                        adb.aql.execute("""
                            FOR e IN session_edges
                            FILTER e.session_id == @sid AND (
                                PARSE_IDENTIFIER(e._from).key IN @t_keys OR
                                PARSE_IDENTIFIER(e._to).key IN @t_keys
                            )
                            REMOVE e IN session_edges
                        """, bind_vars={"sid": session_id, "t_keys": table_keys})

                        # Remove nodes corresponding to dropped tables
                        adb.aql.execute("""
                            FOR n IN session_nodes
                            FILTER n.session_id == @sid AND (
                                n._key IN @t_keys OR
                                n.original_id IN @t_keys OR
                                n.label IN @t_keys
                            )
                            REMOVE n IN session_nodes
                        """, bind_vars={"sid": session_id, "t_keys": table_keys})
                        print(f"[DELETE] ArangoDB removed nodes/edges for dropped tables: {table_keys}")
                    except Exception as drop_aql_err:
                        print(f"[DELETE] ArangoDB table node cleanup warning: {drop_aql_err}")

        except Exception as arango_conn_err:
            print(f"[DELETE] ArangoDB cleanup skipped for connection {item_id}: {arango_conn_err}")
        
        # We DO NOT delete from session_analysis_cache, session_chat_history, or unstructured_docs here,
        # because those apply to the ENTIRE session (which may contain other active connections).
        # We only delete the specific connector credential and history.
        
        # Cancel any running background jobs associated with this connection
        try:
            from controllers.uploads_controller import scheduler
            if scheduler.get_job(f"doc_job_{item_id}"):
                scheduler.remove_job(f"doc_job_{item_id}")
                print(f"Canceled background document job doc_job_{item_id}")
            if scheduler.get_job(f"csv_job_{item_id}"):
                scheduler.remove_job(f"csv_job_{item_id}")
                print(f"Canceled background CSV job csv_job_{item_id}")
        except Exception as e:
            print(f"Failed to cancel background jobs for {item_id}: {e}")
            
        db_conn.commit()

        return jsonify({"status": "success", "message": "Connection and associated tables deleted successfully."}), 200

    except Exception as e:
        if db_conn:
            db_conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            try:
                cursor.execute(f"USE `{config.MYSQL_CONFIG['database']}`")
            except:
                pass
            cursor.close()
        if db_conn:
            db_conn.close()




# delete_workspace_controller
def delete_workspace_controller(workspace_id, get_db_connection):

    if not workspace_id:
        return jsonify({
            "status": "error",
            "message": "workspace_id is required"
        }), 400

    db_conn = None
    cursor = None

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        # Get workspace
        cursor.execute("""
            SELECT id, session_id, user_id, workspace_db, workspace_arango_db, workspace_chroma_collection
            FROM workspaces
            WHERE id = %s
        """, (workspace_id,))

        workspace = cursor.fetchone()

        if not workspace:
            return jsonify({
                "status": "error",
                "message": "Workspace not found"
            }), 404

        session_id = workspace["session_id"]
        user_id = workspace.get("user_id")
        workspace_db = workspace.get("workspace_db")
        workspace_arango_db = workspace.get("workspace_arango_db")

        if not user_id:
            # Fallback to workspace_users to find user_id
            cursor.execute("SELECT user_id FROM workspace_users WHERE workspace_id = %s LIMIT 1", (workspace_id,))
            wu_row = cursor.fetchone()
            if wu_row:
                user_id = wu_row["user_id"]

        print(f"Deleting workspace_id={workspace_id}")
        print(f"session_id={session_id}")
        print(f"user_id={user_id}")

        # 0. Drop/truncate associated tables in user's custom database
        # Get new_user_db and table_name directly from external_db_sync_log for this session
        cursor.execute("""
            SELECT DISTINCT new_user_db, table_name 
            FROM external_db_sync_log 
            WHERE session_id = %s 
              AND new_user_db IS NOT NULL AND new_user_db != ''
              AND table_name IS NOT NULL AND table_name != ''
        """, (session_id,))
        sync_entries = cursor.fetchall()

        # Group tables by database
        db_tables_map = {}
        for entry in sync_entries:
            db_name = entry["new_user_db"]
            tbl_name = entry["table_name"]
            if db_name not in db_tables_map:
                db_tables_map[db_name] = []
            db_tables_map[db_name].append(tbl_name)

        print(f"[Workspace Delete] Databases and tables to clean: {db_tables_map}")

        # For each user database, drop/truncate tables
        for target_db, tables_to_drop in db_tables_map.items():
            try:
                cursor.execute(f"USE `{target_db}`")
                cursor.execute("SET FOREIGN_KEY_CHECKS=0")

                for table in set(tables_to_drop):
                    if table.lower() == 'd__project_backend_2025_traverseai_2026_d_agentv1_1_d_agent_':
                        cursor.execute(f"TRUNCATE TABLE `{table}`")
                        print(f"[Workspace Delete] Truncated table: {target_db}.{table}")
                    else:
                        cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
                        print(f"[Workspace Delete] Dropped table: {target_db}.{table}")

                # Also check if d__project... table exists in this database and truncate it
                # (it may not be tracked in external_db_sync_log)
                d_project_table = 'd__project_backend_2025_traverseai_2026_d_agentv1_1_d_agent_'
                if d_project_table not in [t.lower() for t in tables_to_drop]:
                    try:
                        cursor.execute(f"SHOW TABLES LIKE %s", (d_project_table,))
                        if cursor.fetchone():
                            cursor.execute(f"TRUNCATE TABLE `{d_project_table}`")
                            print(f"[Workspace Delete] Truncated untracked table: {target_db}.{d_project_table}")
                    except Exception as dp_err:
                        print(f"[Workspace Delete] d__project table check/truncate failed: {dp_err}")

                cursor.execute("SET FOREIGN_KEY_CHECKS=1")

            except Exception as drop_err:
                print(f"[Workspace Delete] Error cleaning tables in {target_db}: {drop_err}")
            finally:
                try:
                    cursor.execute(f"USE `{config.MYSQL_CONFIG['database']}`")
                except Exception as use_err:
                    print(f"[Workspace Delete] Error switching back to main database: {use_err}")

        # 0.5 Drop custom workspace database if it exists
        if workspace_db:
            try:
                cursor.execute(f"DROP DATABASE IF EXISTS `{workspace_db}`")
                print(f"[Workspace Delete] Dropped MySQL database: {workspace_db}")
            except Exception as e:
                print(f"[Workspace Delete] Error dropping MySQL database {workspace_db}: {e}")

        # 1. Delete ChromaDB Collection
        try:
            import hashlib
            # pyrefly: ignore [missing-import]
            import chromadb
            import os
            # Compute the collection name as done in session_rag_chat_controller.py
            workspace_chroma_collection = workspace.get("workspace_chroma_collection")
            col_name = workspace_chroma_collection if workspace_chroma_collection else "s_" + hashlib.md5(session_id.encode()).hexdigest()[:12]
            # Get persist directory
            CHROMA_PERSIST_DIR = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chroma_store"
            )
            chroma_client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
            
            # Delete if exists
            try:
                chroma_client.delete_collection(col_name)
                print(f"Successfully deleted ChromaDB collection: {col_name}")
            except Exception as chroma_del_err:
                print(f"ChromaDB collection {col_name} could not be deleted or does not exist: {chroma_del_err}")
                
        except Exception as e:
            print(f"Error initializing ChromaDB client for deletion: {e}")

        # 2. Delete ArangoDB Data
        try:
            from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB
            # pyrefly: ignore [missing-import]
            from arango import ArangoClient
            
            arango_client = ArangoClient(hosts=ARANGO_HOST)
            sys_db = arango_client.db('_system', username=ARANGO_USER, password=ARANGO_PASS)
            
            if sys_db.has_database(ARANGO_DB):
                arango_db = arango_client.db(ARANGO_DB, username=ARANGO_USER, password=ARANGO_PASS)
                
                # Delete from session_nodes and session_edges collections
                # These are the actual collections used by _sync_to_arango()
                for collection_name in ["session_nodes", "session_edges"]:
                    if arango_db.has_collection(collection_name):
                        try:
                            aql_query = f"""
                            FOR doc IN {collection_name}
                                FILTER doc.session_id == @session_id
                                REMOVE doc IN {collection_name}
                            """
                            arango_db.aql.execute(aql_query, bind_vars={"session_id": session_id})
                            print(f"[Workspace Delete] Deleted ArangoDB data from {collection_name} for session {session_id}")
                        except Exception as aql_err:
                            print(f"[Workspace Delete] ArangoDB {collection_name} delete warning: {aql_err}")
                
                print(f"[Workspace Delete] ArangoDB cleanup complete for session {session_id}")

            if workspace_arango_db and sys_db.has_database(workspace_arango_db):
                try:
                    sys_db.delete_database(workspace_arango_db)
                    print(f"[Workspace Delete] Deleted ArangoDB database: {workspace_arango_db}")
                except Exception as e:
                    print(f"[Workspace Delete] Error deleting ArangoDB database {workspace_arango_db}: {e}")
                    
        except Exception as e:
            print(f"[Workspace Delete] Error connecting to ArangoDB for deletion: {e}")

        # 2.5 Delete session files from chroma_store, chunk_uploads, and graphs folders
        import os
        import shutil
        PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # (a) chunk_uploads/<session_id> folder
        try:
            chunk_folder = os.path.join(PROJECT_ROOT, "chunk_uploads", session_id)
            if os.path.isdir(chunk_folder):
                shutil.rmtree(chunk_folder)
                print(f"[Workspace Delete] Deleted chunk_uploads folder: {chunk_folder}")
        except Exception as chunk_err:
            print(f"[Workspace Delete] Error deleting chunk_uploads: {chunk_err}")

        # (b) graphs/ - find graph HTML files from the graph table before they get deleted
        try:
            cursor.execute("SELECT graph_url FROM `graph` WHERE session_name = %s", (session_id,))
            graph_rows = cursor.fetchall()
            graphs_folder = os.path.join(PROJECT_ROOT, "graphs")
            for grow in graph_rows:
                graph_url = grow.get("graph_url") or ""
                # Extract filename from URL like "http://.../graphs/graph_abc123.html"
                if "graphs/" in graph_url:
                    graph_filename = graph_url.split("graphs/")[-1]
                    graph_file_path = os.path.join(graphs_folder, graph_filename)
                    if os.path.isfile(graph_file_path):
                        os.remove(graph_file_path)
                        print(f"[Workspace Delete] Deleted graph file: {graph_file_path}")
        except Exception as graph_err:
            print(f"[Workspace Delete] Error deleting graph files: {graph_err}")

        # (c) chroma_store - ChromaDB internal directories
        # The ChromaDB collection was already deleted via API above.
        # But also check if any session-specific subfolder exists
        try:
            chroma_dir = os.path.join(PROJECT_ROOT, "chroma_store")
            session_chroma = os.path.join(chroma_dir, session_id)
            if os.path.isdir(session_chroma):
                shutil.rmtree(session_chroma)
                print(f"[Workspace Delete] Deleted chroma_store session folder: {session_chroma}")
        except Exception as chroma_fs_err:
            print(f"[Workspace Delete] Error deleting chroma_store files: {chroma_fs_err}")

        # 3. Delete MySQL Records
        tables_to_clean = [
            "analyze", "app_config", "captcha_store", "categories", 
            "connection_history", "database_credential", "error_logs", 
            "external_db_sync_log", "ftp_fetch_log", "ftp_schedules", 
            "graph", "saved_web_results", "session_analysis_cache", 
            "session_chat_history", "session_log", "session_tracking", 
            "sheet_scans", "tracker", "tracker2", "unstructured_docs", 
            "vector_store", "web_searches", "workspace_tables",
            "sap_hana_kgraph", "sap_hana_view_metadata"
        ]

        for table in tables_to_clean:
            try:
                cursor.execute(f"DELETE FROM `{table}` WHERE session_id = %s", (session_id,))
            except Exception as e:
                try:
                    cursor.execute(f"DELETE FROM `{table}` WHERE session_name = %s", (session_id,))
                except Exception as e2:
                    print(f"[Workspace Delete] Could not delete from {table}: {e2}")

        delete_queries = [
            ("DELETE FROM `workspace_users` WHERE workspace_id = %s", (workspace_id,)),
            ("DELETE FROM `workspaces` WHERE id = %s", (workspace_id,)),
            ("DELETE FROM `workspace_prompts` WHERE workspace_id = %s", (workspace_id,))
        ]

        for query, params in delete_queries:
            try:
                cursor.execute(query, params)
            except Exception as e:
                print(f"FAILED: {query}")
                print(str(e))

        db_conn.commit()

        return jsonify({
            "status": "success",
            "message": "Workspace and all related data deleted successfully"
        }), 200

    except Exception as e:

        if db_conn:
            db_conn.rollback()

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

    finally:

        if cursor:
            cursor.close()

        if db_conn:
            db_conn.close()


# =========================================================
# NEW: Handles the /edit_user/<user_id> route (POST)
# =========================================================
def edit_user_controller(get_db_connection, user_id):
    data = request.get_json()
    admin_id = data.get("admin_id")
    name = data.get("name")
    email = data.get("email")
    password = data.get("password")
    visibility = data.get("visibility", 1)
    role_id = data.get("role_id", 2)
    company_id = data.get("company_id")

    if not admin_id:
        return jsonify({"status": "error", "message": "admin_id is required"}), 400

    db_conn = None
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        # Verify Admin
        cursor.execute("SELECT id, role_id FROM users WHERE id = %s", (admin_id,))
        admin = cursor.fetchone()
        if not admin or admin["role_id"] != 1:
            return jsonify({"status": "error", "message": "Access denied."}), 403

        # Update User
        if password:
            hashed_password = encrypt_password(password)
            cursor.execute(
                "UPDATE users SET name = %s, email = %s, password = %s, role_id = %s, visibility = %s, company_id = %s WHERE id = %s",
                (name, email, hashed_password, role_id, visibility, company_id, user_id)
            )
        else:
            cursor.execute(
                "UPDATE users SET name = %s, email = %s, role_id = %s, visibility = %s, company_id = %s WHERE id = %s",
                (name, email, role_id, visibility, company_id, user_id)
            )
        db_conn.commit()
        return jsonify({"status": "success", "message": "User updated successfully"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if db_conn:
            db_conn.close()

# =========================================================
# NEW: Handles the /delete_user/<user_id> route (DELETE)
# =========================================================
def delete_user_controller(get_db_connection, user_id):
    data = request.get_json() or {}
    admin_id = data.get("admin_id")

    if not admin_id:
        return jsonify({"status": "error", "message": "admin_id is required"}), 400

    db_conn = None
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        # Verify Admin
        cursor.execute("SELECT id, role_id FROM users WHERE id = %s", (admin_id,))
        admin = cursor.fetchone()
        if not admin or admin["role_id"] != 1:
            return jsonify({"status": "error", "message": "Access denied."}), 403

        # Delete from workspace_users
        cursor.execute("DELETE FROM workspace_users WHERE user_id = %s", (user_id,))
        # Delete user
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        
        db_conn.commit()
        return jsonify({"status": "success", "message": "User deleted successfully"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if db_conn:
            db_conn.close()
