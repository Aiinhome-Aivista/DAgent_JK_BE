from functools import wraps
from flask import request, jsonify
from database.config import MYSQL_CONFIG
# pyrefly: ignore [missing-import]
import mysql.connector

def get_db_connection():
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        return conn
    except mysql.connector.Error as err:
        print(f"Error connecting to MySQL DB: {err}")
        return None

def check_usage_limit(limit_type):
    """
    Decorator to check if a user has exceeded their plan limits.
    limit_type can be: 'uploads', 'insights_queries', 'data_storage'
    Requires the route to provide user_id in request JSON or args.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 1. Get user_id from the request
            user_id = request.json.get('user_id') if request.json else None
            if not user_id:
                user_id = request.args.get('user_id')
            
            if not user_id:
                # If we can't identify the user, we might reject or allow depending on your policy.
                return jsonify({"status": "error", "message": "User ID is required"}), 400

            conn = get_db_connection()
            if not conn:
                return jsonify({"status": "error", "message": "Database error"}), 500
            
            try:
                cursor = conn.cursor(dictionary=True)
                
                # 2. Get User's Company & Plan
                cursor.execute("""
                    SELECT c.plan_type 
                    FROM users u 
                    JOIN companies c ON u.company_id = c.id 
                    WHERE u.id = %s
                """, (user_id,))
                user_record = cursor.fetchone()
                
                if not user_record or not user_record.get('plan_type'):
                    return jsonify({"status": "error", "message": "No active plan found for this user/company"}), 403
                
                plan_name = user_record['plan_type']
                
                # 3. Get Plan Limits
                cursor.execute("SELECT * FROM pricing_plans WHERE plan_name = %s", (plan_name,))
                plan = cursor.fetchone()
                
                if not plan:
                    return jsonify({"status": "error", "message": "Plan details not found"}), 403

                # 4. Check specific limit based on limit_type
                limit_value = plan.get(limit_type, -1)
                
                # If limit is -1 or 0, it means unlimited
                if limit_value in (-1, 0):
                    return f(*args, **kwargs)
                
                # Example for uploads checking:
                if limit_type == 'uploads':
                    # Check today's uploads for this user
                    cursor.execute("""
                        SELECT COUNT(*) as current_usage 
                        FROM unstructured_docs 
                        WHERE DATE(upload_date) = CURDATE() 
                        AND session_name IN (SELECT session_id FROM workspace_users WHERE user_id = %s)
                    """, (user_id,))
                    usage_record = cursor.fetchone()
                    current_usage = usage_record['current_usage'] if usage_record else 0
                    
                    if current_usage >= limit_value:
                        return jsonify({"status": "error", "message": f"Daily upload limit ({limit_value}) reached for {plan_name} plan"}), 403

                elif limit_type == 'insights_queries':
                    # Check today's queries
                    cursor.execute("""
                        SELECT COUNT(*) as current_usage 
                        FROM session_chat_history 
                        WHERE DATE(created_at) = CURDATE() AND user_id = %s
                    """, (user_id,))
                    usage_record = cursor.fetchone()
                    current_usage = usage_record['current_usage'] if usage_record else 0
                    
                    if current_usage >= limit_value:
                        return jsonify({"status": "error", "message": f"Daily query limit ({limit_value}) reached for {plan_name} plan"}), 403

                # Pass to original function if limits are OK
                return f(*args, **kwargs)
                
            except Exception as e:
                print(f"Limit Check Error: {e}")
                return jsonify({"status": "error", "message": "Failed to verify usage limits"}), 500
            finally:
                cursor.close()
                conn.close()
                
        return decorated_function
    return decorator
