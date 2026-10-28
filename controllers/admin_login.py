import jwt
import datetime
import pymysql
import logging
from flask import request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime as dt
from helper.captcha_helper import verify_captcha

# Import your database config
from database.config import MYSQL_CONFIG

# --- Configuration ---
# key should be secure and hidden in .env in production
SECRET_KEY = "soulbuddy_staff_secret_key_2025" 

# Setup Logging
logging.basicConfig(level=logging.INFO)

def get_connection():
    return pymysql.connect(**MYSQL_CONFIG)

# ================= STAFF LOGIN CONTROLLER =================
def staff_login_controller():
    """
    Endpoint: /api/staff/login
    Access: Public (for staff to authenticate)
    Input: { "email": "admin@example.com", "password": "..." }
    Output: { "token": "...", "role": "super_admin", ... }
    """
    try:
        data = request.get_json()
        email = data.get("email")
        password = data.get("password")
        # CAPTCHA
        captcha_id = data.get("captchaId")
        captcha_value = data.get("captchaValue")

        # 1. Validation
        if not email or not password:
            return jsonify({
                "status": "failed", 
                "message": "Email and password are required", 
                "statusCode": 400
            }), 400
        if not captcha_id or not captcha_value:
            return jsonify({
                "status": "failed",
                "message": "Captcha is required",
                "refreshCaptcha": True,
                "statusCode": 400
            }), 400

        # 2. Database Lookup
        conn = get_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        #CAPTCHA VALIDATION
        cursor.execute("""
            SELECT captcha_hash
            FROM captcha_store
            WHERE id = %s AND expires_at > UTC_TIMESTAMP()
        """, (captcha_id,))
        captcha_row = cursor.fetchone()

        if not captcha_row or not verify_captcha(captcha_value.strip().upper(), captcha_row["captcha_hash"]):
            cursor.close()
            conn.close()
            return jsonify({
                "status": "failed",
                "message": "Invalid or expired captcha",
                "refreshCaptcha": True,
                "statusCode": 401
            }), 401


        # One-time captcha delete
        cursor.execute("DELETE FROM captcha_store WHERE id = %s", (captcha_id,))
        conn.commit()

        
        # Strictly query the 'staff_users' table
        cursor.execute("SELECT * FROM staff_users WHERE email = %s", (email,))
        staff_user = cursor.fetchone()
        cursor.close()
        conn.close()

        # 3. Verify User Exists
        if not staff_user:
            return jsonify({
                "status": "failed", 
                "message": "Staff account not found",
                "refreshCaptcha": True, 
                "statusCode": 404
            }), 404

        # 4. Verify Password
        if check_password_hash(staff_user['password_hash'], password):
            # 5. Generate Token
            token_payload = {
                'id': staff_user['id'],
                'username': staff_user['username'],
                'role': staff_user['role'], # super_admin, admin, or expert
                'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24) # 1 day expiry
            }
            
            token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")

            # Log the successful login
            logging.info(f"Staff Login Success: {staff_user['username']} ({staff_user['role']})")

            return jsonify({
                "status": "success",
                "statusCode": 200,
                "message": "Login successful",
                "data": {
                    "token": token,
                    "role": staff_user['role'],
                    "username": staff_user['username'],
                    "full_name": staff_user.get('full_name')
                }
            })
        else:
            logging.warning(f"Staff Login Failed: Invalid password for {email}")
            return jsonify({
                "status": "failed", 
                "message": "Invalid credentials", 
                "statusCode": 401
            }), 401

    except Exception as e:
        logging.error(f"Staff Login Error: {str(e)}")
        return jsonify({"status": "failed", "message": "Internal Server Error", "statusCode": 500}), 500


# ================= STAFF REGISTRATION CONTROLLER =================
def create_staff_account_controller():
    """
    Endpoint: /api/staff/create
    Access: Should be restricted (e.g., only Super Admins can create others)
    """
    try:
        data = request.get_json()
        username = data.get("username")
        email = data.get("email")
        password = data.get("password")
        role = data.get("role", "expert") # Default role
        full_name = data.get("full_name", "")

        # 1. Validate Role (Security Check)
        allowed_roles = ['super_admin', 'admin', 'expert']
        if role not in allowed_roles:
            return jsonify({"status": "failed", "message": f"Invalid role. Choose from {allowed_roles}"}), 400

        if not all([username, email, password]):
            return jsonify({"status": "failed", "message": "Username, Email, and Password are required"}), 400

        # 2. Hash Password
        hashed_password = generate_password_hash(password)

        # 3. Insert into DB
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO staff_users (username, email, password_hash, role, full_name)
                VALUES (%s, %s, %s, %s, %s)
            """, (username, email, hashed_password, role, full_name))
            conn.commit()
            
            logging.info(f"New Staff Created: {username} as {role}")
            return jsonify({"status": "success", "message": f"New {role} account created successfully"}), 201
            
        except pymysql.IntegrityError as e:
            return jsonify({"status": "failed", "message": "Username or Email already exists"}), 409
        except Exception as e:
            return jsonify({"status": "failed", "message": str(e)}), 500
        finally:
            conn.close()

    except Exception as e:
        return jsonify({"status": "failed", "message": str(e)}), 500

# ================= STAFF LIST CONTROLLER =================
def get_all_staff_controller():
    """
    Endpoint: /api/staff/list
    Method: GET
    Access: Protected (Recommended for Admin/Super Admin use)
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # Query to fetch specific fields
        query = """
            SELECT id, full_name, username, email, role, created_at 
            FROM staff_users 
            ORDER BY created_at DESC
        """
        cursor.execute(query)
        staff_list = cursor.fetchall()
        
        conn.close()

        # Check if list is empty
        if not staff_list:
            return jsonify({
                "status": "success", 
                "message": "No staff members found", 
                "data": []
            }), 200

        return jsonify({
            "status": "success",
            "statusCode": 200,
            "count": len(staff_list),
            "data": staff_list
        })

    except Exception as e:
        print(f"Error fetching staff list: {e}")
        return jsonify({"status": "failed", "message": "Internal Server Error", "statusCode": 500}), 500
