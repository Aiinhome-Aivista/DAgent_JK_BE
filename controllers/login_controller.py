# controllers/login_controller.py (FINAL FIX)

import uuid
# pyrefly: ignore [missing-import]
import mysql.connector 
from flask import request, jsonify

VALID_LOGIN_TYPES = ["google", "facebook"] 

def login_controller(get_connection_func):
    data = request.json
    email = data.get("email")
    input_user_id = data.get("user_id")
    input_full_name = data.get("full_name")
    login_type = data.get("login_type")
    
    # --- Input Validation ---
    if not email or not login_type:
        return jsonify({
            "status": "failed",
            "statusCode": 400,
            "message": "Email and login_type are required for social login"
        }), 400

    if login_type.lower() not in VALID_LOGIN_TYPES:
         return jsonify({
            "status": "failed",
            "statusCode": 400,
            "message": f"Invalid login_type. Must be one of {VALID_LOGIN_TYPES}"
        }), 400

    conn = None
    cursor = None
    try:
        conn = get_connection_func()
        if not conn:
            return jsonify({"status": "error", "statusCode": 500, "message": "Database connection failed"}), 500
        
        
        cursor = conn.cursor(dictionary=True)

        # 1. MATCH USER BY EMAIL IN THE 'users' TABLE
        query = "SELECT user_id, full_name FROM users WHERE email = %s"
        cursor.execute(query, (email,))
        user = cursor.fetchone()

        if not user:
            # 2. USER NOT FOUND: New User / Registration Required
            return jsonify({
                "status": "new_user",
                "statusCode": 200,
                "message": "User not found. Proceed to registration.",
                "email": email,
                "full_name": input_full_name,
                "login_type": login_type.lower(),
                "user_id": input_user_id,
                "session_id": None
            }), 200

        # --- User Found ---
        # These now work because cursor(dictionary=True) was used:
        db_user_id = user["user_id"]
        db_full_name = user["full_name"]
        
        # 3. USER FOUND: Fetch or Create Session (Assuming session_log is in the same DB)
        
        # Check for existing session in session_log
        cursor.execute("""
            SELECT session_id
            FROM session_log
            WHERE user_id = %s
            ORDER BY created_at DESC LIMIT 1
        """, (db_user_id,))
        session_row = cursor.fetchone()

        # ... (Rest of the session logic remains the same, using db_user_id and session_id) ...
        if session_row:
            session_id = session_row["session_id"]
        else:
            session_id = str(uuid.uuid4())
            cursor.execute("INSERT INTO session_log (user_id, session_id, created_at) VALUES (%s, %s, NOW())", (db_user_id, session_id))
        
        # Update last_login_at
        cursor.execute("UPDATE session_log SET last_login_at = NOW() WHERE user_id = %s", (db_user_id,))
        conn.commit()

        # 5. Success Response
        return jsonify({
            "status": "success",
            "statusCode": 200,
            "message": f"Welcome back, {db_full_name}!",
            "user_id": db_user_id,
            "full_name": db_full_name,
            "login_type": login_type.lower(),
            "session_id": session_id
        }), 200

    except mysql.connector.Error as e:
        print(f"Database error during social login: {e}")
        return jsonify({"status": "error", "statusCode": 500, "message": f"Database error: {str(e)}"}), 500
    except Exception as e:
        print(f"Unexpected error during social login: {e}")
        return jsonify({"status": "error", "statusCode": 500, "message": f"An unexpected error occurred: {str(e)}"}), 500
    finally:
        if cursor: cursor.close()
        if conn: conn.close()