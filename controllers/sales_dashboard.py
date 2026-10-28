from flask import jsonify, request
from helper.sap_analytics_service import (
    execute_sales_revenue_by_zone,
    execute_sales_by_account_category,
    execute_non_billed_accounts_pct,
    execute_overdue_pct,
    execute_exposure_pct,
)

def get_sales_revenue_data_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = request.args.get("session_id")
        zone = request.args.get("zone")
        chart_data = execute_sales_revenue_by_zone(conn, session_id, zone=zone)
        return jsonify({
            "status": "success",
            "data": chart_data
        }), 200

    except Exception as e:
        print(f"Error fetching sales revenue data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

def get_sales_by_account_category_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = request.args.get("session_id")
        zone = request.args.get("zone")
        chart_data = execute_sales_by_account_category(conn, session_id, zone=zone)
        return jsonify({"status": "success", "data": chart_data}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

def get_non_billed_accounts_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = request.args.get("session_id")
        zone = request.args.get("zone")
        chart_data = execute_non_billed_accounts_pct(conn, session_id, zone=zone)
        return jsonify({"status": "success", "data": chart_data}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

def get_overdue_pct_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = request.args.get("session_id")
        zone = request.args.get("zone")
        chart_data = execute_overdue_pct(conn, session_id, zone=zone)
        return jsonify({"status": "success", "data": chart_data}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

def get_exposure_pct_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = request.args.get("session_id")
        zone = request.args.get("zone")
        chart_data = execute_exposure_pct(conn, session_id, zone=zone)
        return jsonify({"status": "success", "data": chart_data}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()

