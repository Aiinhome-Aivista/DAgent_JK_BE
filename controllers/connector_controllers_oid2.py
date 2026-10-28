# from urllib.parse import quote_plus
# from flask import request, jsonify
# from sqlalchemy import create_engine, text
# import pandas as pd
# import json
# import uuid

# # This stores active engines in memory for the agent to use
# active_connectors = {}


# def create_workspace_controller(get_db_connection):
#     """Creates a brand new workspace for a verified user."""
#     data = request.json
    
#     user_id = data.get('user_id')
#     workspace_name = data.get('workspace_name', 'Untitled Workspace')

#     if not user_id:
#         return jsonify({"status": "error", "message": "user_id is required"}), 400

#     try:
#         db_conn = get_db_connection()
#         if not db_conn:
#             return jsonify({"status": "error", "message": "Cannot connect to database"}), 500
            
#         cursor = db_conn.cursor(dictionary=True) 
        
#         # --- 1. VERIFY USER EXISTS IN USERS TABLE ---
#         cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
#         user_exists = cursor.fetchone()
        
#         if not user_exists:
#             cursor.close()
#             db_conn.close()
#             return jsonify({
#                 "status": "error", 
#                 "message": f"Invalid User: User ID {user_id} does not exist in the system."
#             }), 404

#         # --- 2. GENERATE AND SAVE NEW WORKSPACE ---
#         new_session_id = str(uuid.uuid4())
        
#         insert_query = """
#             INSERT INTO workspaces (session_id, user_id, workspace_name) 
#             VALUES (%s, %s, %s)
#         """
#         cursor.execute(insert_query, (new_session_id, user_id, workspace_name))
#         db_conn.commit()

#         cursor.close()
#         db_conn.close()

#         return jsonify({
#             "status": "success", 
#             "message": "Workspace created successfully",
#             "session_id": new_session_id,
#             "workspace_name": workspace_name
#         }), 201

#     except Exception as e:
#         return jsonify({"status": "error", "message": str(e)}), 500


# # =========================================================
# # Function 1: Handles the /create_connector route (POST)
# # =========================================================
# def create_connector_controllers(get_db_connection):
#     data = request.json
    
#     user_id = data.get('user_id')
#     user_session_id = data.get('session_id') # <--- MUST come from the frontend now
    
#     if not user_id:
#         return jsonify({"status": "error", "message": "user_id is required"}), 400
        
#     if not user_session_id:
#         return jsonify({"status": "error", "message": "session_id is required. Please open a workspace first."}), 400

#     # --- TABLE 1: VERIFY WORKSPACE EXISTS ---
#     try:
#         db_conn = get_db_connection()
#         if not db_conn:
#             return jsonify({"status": "error", "message": "Cannot connect to main database"}), 500
            
#         cursor = db_conn.cursor(dictionary=True)
        
#         # Check if the workspace (session_id) actually exists for this user
#         cursor.execute("SELECT session_id FROM workspaces WHERE session_id = %s AND user_id = %s", (user_session_id, user_id))
#         workspace_exists = cursor.fetchone()
        
#         if not workspace_exists:
#             cursor.close()
#             db_conn.close() 
#             return jsonify({
#                 "status": "error", 
#                 "message": "Invalid workspace. Please create or select a valid workspace first."
#             }), 404

#         cursor.close()
#         db_conn.close() 

#     except Exception as e:
#         return jsonify({"status": "error", "message": "Error verifying workspace", "details": str(e)}), 500
#     # ------------------------------------------

#     # --- GATHER DATA & SMART LOGIC ---
#     topic = data.get('topic') 
    
#     provided_type = data.get('type')
#     if not provided_type and topic:
#         db_type = 'web_search'
#     else:
#         db_type = provided_type.lower() if provided_type else 'mysql'
        
#     conn_name = data.get('name') 
    
#     if not conn_name and db_type == 'web_search':
#         conn_name = f"Search Agent: {topic}"
        
#     username = data.get('username')
#     raw_password = data.get('password', '')
#     password = quote_plus(raw_password) 
#     database = data.get('database')
#     target_host = data.get('host') or data.get('account') 
    
#     uri = ""
#     status = ""
#     message = ""
#     error_msg = ""

#     # --- TEST CONNECTIONS ---
#     try:
#         if db_type == 'web_search':
#             if not topic:
#                 return jsonify({"status": "error", "message": "A 'topic' is required"}), 400
#             status = "success"
#             message = f"Successfully configured Web Search for topic: '{topic}'"
            
#         elif db_type == 'google_sheets':
#             sheet_url = data.get('url') or data.get('database')
#             if not sheet_url:
#                 return jsonify({"status": "error", "message": "A 'url' or 'database' field is required"}), 400
#             status = "success"
#             message = f"Successfully configured Google Sheets connection: {conn_name}"

#         else:
#             if db_type == 'mysql':
#                 uri = f"mysql+pymysql://{username}:{password}@{target_host}:{data.get('port', 3306)}/{database}"
#             elif db_type == 'mssql':
#                 uri = f"mssql+pymssql://{username}:{password}@{target_host}:{data.get('port', 1433)}/{database}"
#             else:
#                 return jsonify({"status": "error", "message": f"Unsupported connection type: {db_type}"}), 400

#             engine = create_engine(uri)
#             with engine.connect() as conn:
#                 conn.execute(text("SELECT 1"))
            
#             status = "success"
#             message = f"Successfully connected to {db_type} database: {conn_name}"

#     except Exception as e:
#         status = "failed"
#         message = f"Failed to connect to {db_type}"
#         error_msg = str(e)

#     # --- ONLY SAVE TO DATABASE IF SUCCESSFUL ---
#     if status == "success":
#         try:
#             db_conn = get_db_connection() 
#             if db_conn:
#                 cursor = db_conn.cursor()
                
#                 # --- TABLE 2: Insert into connection_history ---
#                 history_query = """INSERT INTO connection_history 
#                                    (user_id, session_id, connection_name, db_type, target_host, status, error_message) 
#                                    VALUES (%s, %s, %s, %s, %s, %s, %s)"""
#                 cursor.execute(history_query, (user_id, user_session_id, conn_name, db_type, target_host, status, error_msg))
                
#                 # --- TABLE 3: Insert into database_credential ---
#                 cred_data = {
#                     "host": data.get('host'), "port": data.get('port'),
#                     "username": username, "password": raw_password, 
#                     "database": database, "url": data.get('url'),   
#                     "account": data.get('account'), "warehouse": data.get('warehouse'),
#                     "schema": data.get('schema'), "topic": topic            
#                 }
#                 # Clean out empty values
#                 clean_cred_data = {k: v for k, v in cred_data.items() if v is not None}
                
#                 cred_query = """INSERT INTO database_credential 
#                                 (user_id, session_id, db_type, credential) 
#                                 VALUES (%s, %s, %s, %s)"""
#                 cursor.execute(cred_query, (user_id, user_session_id, db_type, json.dumps(clean_cred_data)))

#                 db_conn.commit()
#                 cursor.close()
#                 db_conn.close()
#         except Exception as log_e:
#             print(f"Logging Error: {log_e}")

#     # --- RETURN FINAL RESPONSE ---
#     if status == "success":
#         return jsonify({
#             "status": "success", 
#             "message": message,
#             "session_id": user_session_id
#         }), 200
#     else:
#         return jsonify({"status": "error", "message": message, "details": error_msg}), 400


# # =========================================================
# # Function 2: Handles the /connection_history route (GET)
# # =========================================================
# def get_connection_history_controller(get_db_connection):
#     """Fetches a clean timeline of connection history and saved web results grouped by topic."""
#     from flask import request, jsonify 
#     import json
    
#     session_id = request.args.get('session_id')
    
#     if not session_id:
#         return jsonify({
#             "status": "error", 
#             "statuscode": 400, 
#             "message": "session_id is required in the URL parameters"
#         }), 400


#     try:
#         db_conn = get_db_connection()
#         if not db_conn:
#             return jsonify({
#                 "status": "error", 
#                 "statuscode": 500, 
#                 "message": "Cannot connect to main database"
#             }), 500
            
#         cursor = db_conn.cursor(dictionary=True) 
        
#         # --- 1. FETCH DATABASE CONNECTIONS ---
#         query_conn = """
#             SELECT ch.*, dc.connection_id, dc.credential
#             FROM connection_history ch
#             LEFT JOIN database_credential dc 
#               ON ch.user_id = dc.user_id 
#              AND ch.db_type = dc.db_type 
#              AND ch.session_id = dc.session_id  
#             WHERE ch.session_id = %s 
#         """
#         cursor.execute(query_conn, (session_id,))
#         raw_history = cursor.fetchall() 


#         # --- 2. FETCH SAVED WEB RESULTS (GROUPED BY UNIQUE TOPIC) ---
#         # NEW: We use GROUP BY topic so it only returns ONE row per topic
#         query_saved = """
#             SELECT 
#                 topic, 
#                 COUNT(saved_id) as total_saved, 
#                 MAX(saved_at) as latest_saved_at
#             FROM saved_web_results
#             WHERE session_id = %s
#               AND topic IS NOT NULL 
#               AND topic != ''
#             GROUP BY topic
#         """
#         cursor.execute(query_saved, (session_id,))
#         raw_saved = cursor.fetchall()


#         cursor.close()
#         db_conn.close()


#         # --- COMBINE EVERYTHING INTO ONE ARRAY ---
#         combined_history = []


#         # Formatting 1: Add Connections
#         for row in raw_history:
#             if row['db_type'] not in ['mysql', 'mssql', 'web_search', 'google_sheets']:
#                 continue


#             date_str = row['created_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['created_at'] else ""
#             exact_id = str(row['connection_id']) if row.get('connection_id') else f"h{row['id']}"


#             if row['db_type'] in ['web_search', 'google_sheets']:
#                 action_str = f"Configured {row['connection_name']}" 
#             else:
#                 action_str = f"Connected to {row['connection_name']}"


#             extracted_topic = ""
#             if row.get('credential'):
#                 try:
#                     cred_dict = json.loads(row['credential'])
#                     extracted_topic = cred_dict.get('topic', "")
#                 except:
#                     pass


#             combined_history.append({
#                 "id": exact_id,  
#                 "sessionId": row.get('session_id'),
#                 "date": date_str,
#                 "action": action_str,
#                 "connectionName": row['connection_name'],
#                 "db_type": row['db_type'],
#                 "topic": extracted_topic,
#                 "status": "completed"
#             })


#         # Formatting 2: Add Saved Web Results (Grouped)
#         for row in raw_saved:
#             date_str = row['latest_saved_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['latest_saved_at'] else ""
            
#             # Smart phrasing based on how many articles were saved for this topic
#             if row['total_saved'] > 1:
#                 action_str = f"Saved {row['total_saved']} articles for: {row['topic']}"
#             else:
#                 action_str = f"Saved 1 article for: {row['topic']}"


#             combined_history.append({
#                 "id": f"topic_group_{row['topic']}",  # Unique ID generated from the topic name
#                 "sessionId": session_id,
#                 "date": date_str,
#                 "action": action_str,
#                 "connectionName": f"Web Research: {row['topic']}",  
#                 "db_type": "saved_web_result", 
#                 "topic": row['topic'],
#                 "status": "completed"
#             })


#         # --- SORT THE COMBINED ARRAY BY DATE ---
#         combined_history.sort(key=lambda x: x['date'], reverse=True)


#         # --- ASSEMBLE THE FINAL MINIMAL RESPONSE ---
#         return jsonify({
#             "status": "success", 
#             "statuscode": 200,   
#             "history": combined_history 
#         }), 200
        
#     except Exception as e:
#         return jsonify({
#             "status": "error", 
#             "statuscode": 500, 
#             "message": str(e)
#         }), 500


# # =========================================================
# # Function 3: Handles the /agent/query_db route (POST)
# # =========================================================
# def agent_query_controllers():
#     data = request.json
    
#     user_id = data.get('user_id')
#     conn_name = data.get('name')
#     query = data.get('query')
    
#     if not user_id:
#         return jsonify({"error": "user_id is required"}), 400
        
#     # Recreate the exact same key we used when creating the connection
#     session_key = f"user_{user_id}_{conn_name}"
    
#     if session_key not in active_connectors:
#         return jsonify({"error": f"Connector '{conn_name}' not found. You must create the connection first!"}), 404
        
#     try:
#         engine = active_connectors[session_key]
#         df = pd.read_sql(query, engine)
#         return jsonify(df.to_dict(orient="records")), 200
#     except Exception as e:
#         return jsonify({"error": str(e)}), 400



# # =========================================================
# # Function 4: Handles the /saved_credentials route (GET)
# # =========================================================
# def get_saved_credentials_controller(get_db_connection):
#     from flask import request, jsonify
#     import json
    
#     user_id = request.args.get('user_id')
    
#     if not user_id:
#         return jsonify({"status": "error", "message": "user_id is required in the URL parameters"}), 400

#     try:
#         db_conn = get_db_connection()
#         if not db_conn:
#             return jsonify({"error": "Cannot connect to main database"}), 500
            
#         cursor = db_conn.cursor(dictionary=True) 
        
#         # Fetch all saved credentials for this specific user
#         cursor.execute("SELECT * FROM database_credential WHERE user_id = %s ORDER BY connection_id DESC", (user_id,))
#         saved_creds = cursor.fetchall() 
        
#         cursor.close()
#         db_conn.close()

#         # Format the data for the frontend
#         for row in saved_creds:
#             # If the database returns the JSON column as a string, convert it back to an object
#             if isinstance(row['credential'], str):
#                 row['credential'] = json.loads(row['credential'])
                
#         return jsonify({
#             "status": "success", 
#             "saved_connections": saved_creds
#         }), 200
        
#     except Exception as e:
#         return jsonify({"status": "error", "error": str(e)}), 500

# # =========================================================
# # Function 5: Handles the /workspace_history route (GET)
# # =========================================================
# def get_workspace_history_controller(get_db_connection):
#     """Fetches connection history for a specific workspace using ONLY session_id."""
    
#     # Only expecting session_id now
#     session_id = request.args.get('session_id')
    
#     if not session_id:
#         return jsonify({"status": "error", "message": "session_id is required in the URL parameters"}), 400

#     try:
#         db_conn = get_db_connection()
#         if not db_conn:
#             return jsonify({"error": "Cannot connect to main database"}), 500
            
#         cursor = db_conn.cursor(dictionary=True) 
        
#         # --- 1. FETCH DATABASE CONNECTIONS BY SESSION_ID ONLY ---
#         query_conn = """
#             SELECT ch.*, dc.connection_id
#             FROM connection_history ch
#             LEFT JOIN database_credential dc 
#               ON ch.user_id = dc.user_id 
#              AND ch.db_type = dc.db_type 
#              AND ch.created_at = dc.created_at
#             WHERE ch.session_id = %s
#             ORDER BY ch.created_at DESC
#         """
#         cursor.execute(query_conn, (session_id,))
#         raw_history = cursor.fetchall() 

#         # --- 2. FETCH SAVED WEB RESULTS BY SESSION_ID ONLY ---
#         query_saved = """
#             SELECT saved_id, topic, title, url, brief, session_id, saved_at
#             FROM saved_web_results
#             WHERE session_id = %s
#             ORDER BY saved_at DESC
#         """
#         cursor.execute(query_saved, (session_id,))
#         raw_saved = cursor.fetchall()

#         cursor.close()
#         db_conn.close()

#         # --- CHECK IF EMPTY ---
#         if len(raw_history) == 0 and len(raw_saved) == 0:
#             return jsonify({
#                 "status": "success", 
#                 "message": "No connection found for this workspace", 
#                 "agents": []
#             }), 200

#         # --- FORMATTING 1: CONNECTIONS ---
#         connect_history = []
#         for row in raw_history:
#             if row['db_type'] not in ['mysql', 'mssql', 'web_search', 'google_sheets']:
#                 continue

#             db_names = {
#                 "mysql": "MySQL", "mssql": "SQL Server",
#                 "web_search": "Web Search API", "google_sheets": "Google Sheets"
#             }
#             db_name_display = db_names.get(row['db_type'], row['db_type'].capitalize())
            
#             details = f"{db_name_display} connection established successfully."
#             date_str = row['created_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['created_at'] else ""
#             exact_id = str(row['connection_id']) if row.get('connection_id') else f"h{row['id']}"

#             if row['db_type'] in ['web_search', 'google_sheets']:
#                 action_str = f"Configured {row['connection_name']}" 
#             else:
#                 action_str = f"Connected to {row['connection_name']}"

#             history_item = {
#                 "id": exact_id,  
#                 "sessionId": row.get('session_id'),
#                 "date": date_str,
#                 "action": action_str,
#                 "details": details,
#                 "connectionName": row['connection_name'],
#                 "status": "completed"
#             }
            
#             if row['db_type'] == 'web_search':
#                 history_item["activities"] = ["Verifying API Key...", "Establishing secure link to search provider...", "Testing query endpoints...", "Web Search ready."]
#             elif row['db_type'] == 'google_sheets':
#                 history_item["activities"] = ["Parsing Spreadsheet URL...", "Authenticating Google API access...", "Mapping sheet tabs and columns...", "Google Sheets ready."]
#             else:
#                 history_item["activities"] = ["Verifying credentials...", "Establishing SSL tunnel...", f"Handshaking with {db_name_display}...", "Mapping schema structures..."]
                
#             connect_history.append(history_item)

#         # --- FORMATTING 2: SAVED WEB RESULTS ---
#         saved_history = []
#         for row in raw_saved:
#             date_str = row['saved_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['saved_at'] else ""
            
#             history_item = {
#                 "id": row['saved_id'],  
#                 "sessionId": row.get('session_id'),
#                 "date": date_str,
#                 "action": f"Saved Article: {row['title']}",
#                 "details": row['brief'],
#                 "connectionName": row['url'],  
#                 "status": "completed",
#                 "activities": [
#                     f"Topic: {row['topic']}",
#                     f"Link: {row['url']}",
#                     "Web result successfully saved to workspace memory."
#                 ]
#             }
#             saved_history.append(history_item)

#         # --- ASSEMBLE THE FINAL RESPONSE ---
#         agents_data = []
        
#         if len(connect_history) > 0:
#             agents_data.append({
#                 "id": 'connect',
#                 "name": 'Data source',
#                 "historyName": 'Data source',
#                 "icon": 'database',
#                 "description": 'Establishing secure link to database',
#                 "history": connect_history 
#             })
            
#         if len(saved_history) > 0:
#             agents_data.append({
#                 "id": 'saved_results',
#                 "name": 'Saved Research',
#                 "historyName": 'Saved Web Links',
#                 "icon": 'link', 
#                 "description": 'AI web search results saved to workspace',
#                 "history": saved_history 
#             })
        
#         return jsonify({"status": "success", "agents": agents_data}), 200
        
#     except Exception as e:
#         return jsonify({"status": "error", "error": str(e)}), 500


# # =========================================================
# # Function: Handles the /workspaces route (GET)
# # =========================================================
# def get_user_workspaces_controller(get_db_connection):
#     """Fetch all workspaces for a user and ensure one active workspace."""
#     from flask import request, jsonify

#     user_id = request.args.get('user_id')

#     if not user_id:
#         return jsonify({
#             "status": "error",
#             "statuscode": 400,
#             "message": "user_id is required"
#         }), 400

#     try:
#         db_conn = get_db_connection()
#         cursor = db_conn.cursor(dictionary=True)

#         query = """
#             SELECT id, session_id, workspace_name, is_active
#             FROM workspaces
#             WHERE user_id = %s
#             ORDER BY created_at DESC
#         """
#         cursor.execute(query, (user_id,))
#         workspaces = cursor.fetchall()

#         # ---- ensure one active workspace ----
#         if workspaces:
#             active_exists = any(w["is_active"] == 1 for w in workspaces)

#             if not active_exists:
#                 first_workspace_id = workspaces[0]["id"]

#                 update_query = """
#                     UPDATE workspaces
#                     SET is_active = 1
#                     WHERE id = %s
#                 """
#                 cursor.execute(update_query, (first_workspace_id,))
#                 db_conn.commit()

#                 workspaces[0]["is_active"] = 1

#         cursor.close()
#         db_conn.close()

#         return jsonify({
#             "status": "success",
#             "statuscode": 200,
#             "workspaces": workspaces
#         }), 200

#     except Exception as e:
#         return jsonify({
#             "status": "error",
#             "statuscode": 500,
#             "message": str(e)
#         }), 500

# def set_active_workspace(get_db_connection):
#     from flask import request, jsonify

#     data = request.get_json()

#     user_id = data.get("user_id")
#     workspace_id = data.get("workspace_id")

#     if not user_id or not workspace_id:
#         return jsonify({
#             "status": "error",
#             "message": "user_id and workspace_id required"
#         }), 400

#     try:
#         db_conn = get_db_connection()
#         cursor = db_conn.cursor()

#         # Step 1: make all inactive
#         cursor.execute("""
#             UPDATE workspaces
#             SET is_active = 0
#             WHERE user_id = %s
#         """, (user_id,))

#         # Step 2: activate selected workspace
#         cursor.execute("""
#             UPDATE workspaces
#             SET is_active = 1
#             WHERE id = %s AND user_id = %s
#         """, (workspace_id, user_id))

#         db_conn.commit()

#         cursor.close()
#         db_conn.close()

#         return jsonify({
#             "status": "success",
#             "message": "Workspace switched successfully"
#         })

#     except Exception as e:
#         return jsonify({
#             "status": "error",
#             "message": str(e)
#         }), 500


from urllib.parse import quote_plus
from flask import request, jsonify
from sqlalchemy import create_engine, text
import pandas as pd
import json
import uuid

# This stores active engines in memory for the agent to use
active_connectors = {}


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
            SELECT u.id, u.name, u.email, u.created_at, u.company_id, c.company_name
            FROM users u
            LEFT JOIN companies c ON u.company_id = c.id
            WHERE u.role_id = 1
            ORDER BY COALESCE(u.updated_at, u.created_at) DESC
        """)
        users = cursor.fetchall()

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

        # Only Admin (role_id = 2) can create workspaces
        if user['role_id'] != 2:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": "Access denied. Only Admin users can create workspaces."
            }), 403

        # --- 2. CHECK DUPLICATE WORKSPACE NAME ---
        cursor.execute(
            "SELECT id FROM workspaces WHERE user_id = %s AND workspace_name = %s",
            (user_id, workspace_name)
        )
        existing = cursor.fetchone()

        if existing:
            cursor.close()
            db_conn.close()
            return jsonify({
                "status": "error",
                "message": f"Workspace name '{workspace_name}' already exists. Please use a different name."
            }), 409

        # --- 3. GENERATE AND SAVE NEW WORKSPACE ---
        new_session_id = str(uuid.uuid4())

        insert_query = """
            INSERT INTO workspaces (session_id, user_id, workspace_name)
            VALUES (%s, %s, %s)
        """
        cursor.execute(insert_query, (new_session_id, user_id, workspace_name))
        db_conn.commit()

        # Fetch the auto-generated workspace ID
        new_workspace_id = cursor.lastrowid

        cursor.close()
        db_conn.close()

        return jsonify({
            "status": "success",
            "message": "Workspace created successfully",
            "workspace_id": new_workspace_id,
            "session_id": new_session_id,
            "workspace_name": workspace_name
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

        if admin_user['role_id'] != 2:
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
                    "INSERT INTO workspace_users (workspace_id, workspace_name, session_id, user_id) VALUES (%s, %s, %s,%s)",
                    (workspace_id, workspace['session_id'], uid, workspace['workspace_name'])
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
    Fetch all users assigned to a specific workspace.
    Query params:
        workspace_id - ID of the workspace
    """
    workspace_id = request.args.get('workspace_id')

    if not workspace_id:
        return jsonify({"status": "error", "message": "workspace_id is required"}), 400

    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"status": "error", "message": "Cannot connect to database"}), 500

        cursor = db_conn.cursor(dictionary=True)

        query = """
            SELECT u.id, u.name, u.email, wu.assigned_at
            FROM workspace_users wu
            JOIN users u ON wu.user_id = u.id
            WHERE wu.workspace_id = %s
            ORDER BY COALESCE(wu.updated_at, wu.assigned_at) DESC
        """
        cursor.execute(query, (workspace_id,))
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
            "workspace_id": int(workspace_id),
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

        if not admin_user or admin_user['role_id'] != 2:
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
        
    username = data.get('username')
    raw_password = data.get('password', '')
    password = quote_plus(raw_password) 
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

        else:
            if db_type == 'mysql':
                uri = f"mysql+pymysql://{username}:{password}@{target_host}:{data.get('port', 3306)}/{database}"
            elif db_type == 'mssql':
                uri = f"mssql+pymssql://{username}:{password}@{target_host}:{data.get('port', 1433)}/{database}"
            else:
                return jsonify({"status": "error", "message": f"Unsupported connection type: {db_type}"}), 400

            engine = create_engine(uri)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            
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
                
                # --- TABLE 3: Insert into database_credential ---
                cred_data = {
                    "host": data.get('host'), "port": data.get('port'),
                    "username": username, "password": raw_password, 
                    "database": database, "url": data.get('url'),   
                    "account": data.get('account'), "warehouse": data.get('warehouse'),
                    "schema": data.get('schema'), "topic": topic            
                }
                # Clean out empty values
                clean_cred_data = {k: v for k, v in cred_data.items() if v is not None}
                
                cred_query = """INSERT INTO database_credential 
                                (user_id, session_id, db_type, credential) 
                                VALUES (%s, %s, %s, %s)"""
                cursor.execute(cred_query, (user_id, user_session_id, db_type, json.dumps(clean_cred_data)))

                db_conn.commit()
                cursor.close()
                db_conn.close()
        except Exception as log_e:
            print(f"Logging Error: {log_e}")

    # --- RETURN FINAL RESPONSE ---
    if status == "success":
        return jsonify({
            "status": "success", 
            "message": message,
            "session_id": user_session_id
        }), 200
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
              ON ch.user_id = dc.user_id 
             AND ch.db_type = dc.db_type 
             AND ch.session_id = dc.session_id
             AND ch.created_at = dc.created_at  
            WHERE ch.session_id = %s 
        """
        cursor.execute(query_conn, (session_id,))
        raw_history = cursor.fetchall() 


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
            if row['db_type'] not in ['mysql', 'mssql', 'web_search', 'google_sheets', 'csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload']:
                continue

            date_str = row['created_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['created_at'] else ""
            exact_id = str(row['connection_id']) if row.get('connection_id') else f"{row['id']}"


            # --- 1. SET THE ACTION STRING & DEFAULT DISPLAY NAME ---
            if row['db_type'] in ['web_search', 'google_sheets']:
                action_str = f"Configured {row['connection_name']}" 
                display_name = row['connection_name']
                
            elif row['db_type'] in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload']:
                raw_name = row.get('connection_name', '')
                is_sql = row['db_type'] in ['sql_upload', 'sql_chunk_upload'] or '.sql' in raw_name.lower()
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
                else:
                    if " to allocated DB" in raw_name:
                        action_str = raw_name.split(" to allocated DB")[0] # Leaves "Uploaded 2 CSV(s)"
                    else:
                        action_str = raw_name
                    display_name = "CSV Data" 
                
            else:
                action_str = f"Connected to {row['connection_name']}"
                display_name = row['connection_name']


            # --- 2. EXTRACT TOPIC AND FILE NAMES FROM JSON CREDENTIAL ---
            extracted_topic = ""
            if row.get('credential'):
                try:
                    cred_dict = json.loads(row['credential'])
                    extracted_topic = cred_dict.get('topic', "")
                    
                    # IF it's a CSV upload, dig into the JSON to find the actual file names
                    if row['db_type'] == 'csv_upload':
                        # Look for common keys your upload function might have saved them under
                        files = cred_dict.get('files', []) or cred_dict.get('file_names', []) or cred_dict.get('file', '')
                        
                        if isinstance(files, list) and len(files) > 0:
                            display_name = ", ".join(files) # Joins multiple files like: "data1.csv, data2.csv"
                        elif isinstance(files, str) and files.strip() != "":
                            display_name = files
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
                "status": "completed"
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
            SELECT ch.*, dc.connection_id
            FROM connection_history ch
            LEFT JOIN database_credential dc 
              ON ch.user_id = dc.user_id 
             AND ch.db_type = dc.db_type 
             AND ch.created_at = dc.created_at
            WHERE ch.session_id = %s
            ORDER BY ch.created_at DESC
        """
        cursor.execute(query_conn, (session_id,))
        raw_history = cursor.fetchall() 

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
            if row['db_type'] not in ['mysql', 'mssql', 'web_search', 'google_sheets', 'snowflake', 'postgresql', 'postgres', 'csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload']:
                continue

            db_names = {
                "mysql": "MySQL", "mssql": "SQL Server",
                "web_search": "Web Search API", "google_sheets": "Google Sheets"
            }
            db_name_display = db_names.get(row['db_type'], row['db_type'].capitalize())
            
            details = f"{db_name_display} connection established successfully."
            date_str = row['created_at'].strftime("%Y-%m-%dT%H:%M:%SZ") if row['created_at'] else ""
            exact_id = str(row['connection_id']) if row.get('connection_id') else f"h{row['id']}"

            if row['db_type'] in ['web_search', 'google_sheets']:
                action_str = f"Configured {row['connection_name']}" 
            elif row['db_type'] in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload']:
                action_str = f"Uploaded {row['connection_name']}"
            else:
                action_str = f"Connected to {row['connection_name']}"

            history_item = {
                "id": exact_id,  
                "sessionId": row.get('session_id'),
                "date": date_str,
                "action": action_str,
                "details": details,
                "connectionName": row['connection_name'],
                "status": "completed"
            }
            
            if row['db_type'] == 'web_search':
                history_item["activities"] = ["Verifying API Key...", "Establishing secure link to search provider...", "Testing query endpoints...", "Web Search ready."]
            elif row['db_type'] == 'google_sheets':
                history_item["activities"] = ["Parsing Spreadsheet URL...", "Authenticating Google API access...", "Mapping sheet tabs and columns...", "Google Sheets ready."]
            elif row['db_type'] in ['csv_upload', 'csv_chunk_upload', 'sql_upload', 'sql_chunk_upload']:
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
