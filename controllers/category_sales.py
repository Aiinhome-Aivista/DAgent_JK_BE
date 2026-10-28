import sys
import os
sys.path.append(os.path.abspath('e:\\D-API-UI\\api'))
from flask import request, jsonify
from helper.sap_analytics_service import execute_category_sales

def get_category_sales_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = request.args.get("session_id")
        zone = request.args.get("zone")
        results = execute_category_sales(conn, session_id, zone=zone)

        return jsonify({
            "status": "success",
            "data": results
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

