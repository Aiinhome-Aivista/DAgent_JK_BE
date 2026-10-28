# ========================================================
# PRICING CONTROLLERS AND FUNCTIONS (COMMENTED OUT)
# ========================================================

# from flask import jsonify, request
# pyrefly: ignore [missing-import]
# import mysql.connector
# from database.config import MYSQL_CONFIG
# 
# def get_db_connection():
#     try:
#         conn = mysql.connector.connect(**MYSQL_CONFIG)
#         return conn
#     except mysql.connector.Error as err:
#         print(f"Error connecting to MySQL DB: {err}")
#         return None
# 
# def get_all_pricing_plans():
#     conn = get_db_connection()
#     if not conn:
#         return []
#     cursor = conn.cursor(dictionary=True)
#     query = "SELECT * FROM pricing_plans ORDER BY id ASC"
#     try:
#         cursor.execute(query)
#         return cursor.fetchall()
#     except Exception as e:
#         print(f"Error fetching pricing plans: {e}")
#         return []
#     finally:
#         cursor.close()
#         conn.close()
# 
# def safe_int(val, default=0):
#     try:
#         return int(val)
#     except (ValueError, TypeError):
#         return default
# 
# def create_pricing_plan(data):
#     conn = get_db_connection()
#     if not conn:
#         return False
#     cursor = conn.cursor()
#     query = """
#     INSERT INTO pricing_plans 
#     (plan_name, data_storage, uploads, insights_queries, basic_features, download_allowed, number_of_users, custom_kpi, scheduled_email, audit_memory, connectors, is_popular, price_text)
#     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
#     """
#     values = (
#         data.get("plan_name", ""),
#         safe_int(data.get("data_storage", 0)),
#         safe_int(data.get("uploads", 0)),
#         safe_int(data.get("insights_queries", 0)),
#         data.get("basic_features", ""),
#         data.get("download_allowed", ""),
#         safe_int(data.get("number_of_users", 0)),
#         data.get("custom_kpi", ""),
#         data.get("scheduled_email", ""),
#         data.get("audit_memory", ""),
#         data.get("connectors", ""),
#         int(bool(data.get("is_popular", False))),
#         data.get("price_text", "")
#     )
#     try:
#         cursor.execute(query, values)
#         conn.commit()
#         return True
#     except Exception as e:
#         print(f"Error creating pricing plan: {e}")
#         return False
#     finally:
#         cursor.close()
#         conn.close()
# 
# def update_pricing_plan(plan_id, data):
#     conn = get_db_connection()
#     if not conn:
#         return False
#     cursor = conn.cursor()
#     query = """
#     UPDATE pricing_plans 
#     SET plan_name = %s, data_storage = %s, uploads = %s, insights_queries = %s, 
#         basic_features = %s, download_allowed = %s, number_of_users = %s, 
#         custom_kpi = %s, scheduled_email = %s, audit_memory = %s, connectors = %s,
#         is_popular = %s, price_text = %s
#     WHERE id = %s
#     """
#     values = (
#         data.get("plan_name", ""),
#         safe_int(data.get("data_storage", 0)),
#         safe_int(data.get("uploads", 0)),
#         safe_int(data.get("insights_queries", 0)),
#         data.get("basic_features", ""),
#         data.get("download_allowed", ""),
#         safe_int(data.get("number_of_users", 0)),
#         data.get("custom_kpi", ""),
#         data.get("scheduled_email", ""),
#         data.get("audit_memory", ""),
#         data.get("connectors", ""),
#         int(bool(data.get("is_popular", False))),
#         data.get("price_text", ""),
#         plan_id
#     )
#     try:
#         cursor.execute(query, values)
#         conn.commit()
#         return True
#     except Exception as e:
#         print(f"Error updating pricing plan: {e}")
#         return False
#     finally:
#         cursor.close()
#         conn.close()
# 
# def delete_pricing_plan(plan_id):
#     conn = get_db_connection()
#     if not conn:
#         return False
#     cursor = conn.cursor()
#     query = "DELETE FROM pricing_plans WHERE id = %s"
#     try:
#         cursor.execute(query, (plan_id,))
#         conn.commit()
#         return True
#     except Exception as e:
#         print(f"Error deleting pricing plan: {e}")
#         return False
#     finally:
#         cursor.close()
#         conn.close()
# 
# def get_pricing_controller():
#     try:
#         plans = get_all_pricing_plans()
#         return jsonify({"status": "success", "pricing": plans}), 200
#     except Exception as e:
#         return jsonify({"status": "error", "message": str(e)}), 500
# 
# def create_pricing_controller():
#     data = request.json
#     if not data:
#         return jsonify({"status": "error", "message": "No data provided"}), 400
#     
#     success = create_pricing_plan(data)
#     if success:
#         return jsonify({"status": "success", "message": "Pricing plan created successfully"}), 201
#     else:
#         return jsonify({"status": "error", "message": "Failed to create pricing plan"}), 500
# 
# def update_pricing_controller(plan_id):
#     data = request.json
#     if not data:
#         return jsonify({"status": "error", "message": "No data provided"}), 400
#         
#     success = update_pricing_plan(plan_id, data)
#     if success:
#         return jsonify({"status": "success", "message": "Pricing plan updated successfully"}), 200
#     else:
#         return jsonify({"status": "error", "message": "Failed to update pricing plan"}), 500
# 
# def delete_pricing_controller(plan_id):
#     success = delete_pricing_plan(plan_id)
#     if success:
#         return jsonify({"status": "success", "message": "Pricing plan deleted successfully"}), 200
#     else:
#         return jsonify({"status": "error", "message": "Failed to delete pricing plan"}), 500
# 
# def get_usage_and_limits(user_id):
#     conn = get_db_connection()
#     if not conn:
#         return {'status': 'error', 'message': 'Database connection error'}
#         
#     try:
#         cursor = conn.cursor(dictionary=True)
#         # 1. Get user and company plan
#         cursor.execute('''
#             SELECT u.id, u.name, c.company_name, c.plan_type 
#             FROM users u 
#             JOIN companies c ON u.company_id = c.id 
#             WHERE u.id = %s
#         ''', (user_id,))
#         user_record = cursor.fetchone()
#         
#         if not user_record or not user_record.get('plan_type'):
#             return {'status': 'error', 'message': 'No active plan found'}
#             
#         plan_name = user_record['plan_type']
#         
#         # 2. Get plan limits
#         cursor.execute('SELECT * FROM pricing_plans WHERE plan_name = %s', (plan_name,))
#         plan = cursor.fetchone()
#         
#         if not plan:
#             return {'status': 'error', 'message': 'Plan details not found'}
#             
#         # 3. Calculate usage
#         # 3.1 Uploads today (Count by Batch/Action based on upload minute)
#         cursor.execute('''
#             SELECT COUNT(DISTINCT DATE_FORMAT(upload_date, '%Y-%m-%d %H:%i')) as current_usage 
#             FROM unstructured_docs 
#             WHERE DATE(upload_date) = CURDATE() 
#             AND session_name IN (SELECT session_id FROM workspace_users WHERE user_id = %s)
#         ''', (user_id,))
#         upload_record = cursor.fetchone()
#         uploads_used = upload_record['current_usage'] if upload_record else 0
#         
#         # 3.2 Insights Queries today
#         cursor.execute('''
#             SELECT COUNT(*) as current_usage 
#             FROM session_chat_history 
#             WHERE DATE(created_at) = CURDATE() AND user_id = %s
#         ''', (user_id,))
#         query_record = cursor.fetchone()
#         queries_used = query_record['current_usage'] if query_record else 0
#         
#         # 3.3 Data Storage used
#         cursor.execute('''
#             SELECT SUM(data_size_mb) as total_db_size 
#             FROM external_db_sync_log 
#             WHERE user_id = %s
#         ''', (user_id,))
#         db_size_record = cursor.fetchone()
#         
#         # Convert MB to GB for the response
#         db_storage_mb = float(db_size_record['total_db_size']) if db_size_record and db_size_record['total_db_size'] else 0.0
#         db_storage_gb = round(db_storage_mb / 1024.0, 4)
#         
#         return {
#             'status': 'success',
#             'usage_stats': {
#                 'company_name': user_record['company_name'],
#                 'plan_name': plan_name,
#                 'allowed_connectors': plan.get('connectors', ''),
#                 'metrics': {
#                     'storage': {
#                         'used': db_storage_gb,
#                         'total': plan.get('data_storage', -1),
#                         'unit': 'GB'
#                     },
#                     'uploads': {
#                         'used': uploads_used,
#                         'total': plan.get('uploads', -1)
#                     },
#                     'queries': {
#                         'used': queries_used,
#                         'total': plan.get('insights_queries', -1)
#                     }
#                 }
#             }
#         }
#         
#     except Exception as e:
#         print(f'Error fetching usage stats: {e}')
#         return {'status': 'error', 'message': str(e)}
#     finally:
#         cursor.close()
#         conn.close()
# 
# def get_user_usage_stats_controller():
#     user_id = request.args.get('user_id')
#     if not user_id:
#         return jsonify({'status': 'error', 'message': 'user_id is required'}), 400
#     
#     result = get_usage_and_limits(user_id)
#     if result.get('status') == 'error':
#         status_code = 500 if 'Database' in result['message'] or 'Error' in result['message'] else 404
#         return jsonify(result), status_code
#         
#     return jsonify(result), 200
# 
# def cleanup_audit_memory_chats():
#     """
#     Cron job function to delete old chat histories based on the company's pricing plan.
#     It runs daily at midnight and removes chats older than the audit_memory limit.
#     """
#     conn = get_db_connection()
#     if not conn:
#         print("Failed to connect to DB for cleanup_audit_memory_chats")
#         return
#         
#     try:
#         cursor = conn.cursor(dictionary=True)
#         # Fetch all pricing plans with 'days' in audit_memory
#         cursor.execute("SELECT plan_name, audit_memory FROM pricing_plans WHERE audit_memory LIKE '%days%'")
#         plans = cursor.fetchall()
#         
#         for plan in plans:
#             try:
#                 # Extract the integer number of days (e.g. '3' from '3 days')
#                 days_str = plan['audit_memory'].lower().replace('days', '').replace('day', '').strip()
#                 days_limit = int(days_str)
#                 
#                 # Find users belonging to companies with this plan
#                 cursor.execute('''
#                     DELETE FROM session_chat_history 
#                     WHERE user_id IN (
#                         SELECT u.id FROM users u 
#                         JOIN companies c ON u.company_id = c.id 
#                         WHERE c.plan_type = %s
#                     ) AND created_at < NOW() - INTERVAL %s DAY
#                 ''', (plan['plan_name'], days_limit))
#                 
#                 print(f"Cleanup: Deleted old chats for plan '{plan['plan_name']}' older than {days_limit} days.")
#             except ValueError:
#                 # Skip if 'audit_memory' string is malformed or cannot be parsed as int
#                 print(f"Cleanup: Could not parse days from audit_memory '{plan['audit_memory']}' for plan '{plan['plan_name']}'.")
#                 
#         conn.commit()
#     except Exception as e:
#         print(f"Error in cleanup_audit_memory_chats: {e}")
#     finally:
#         cursor.close()
#         conn.close()
