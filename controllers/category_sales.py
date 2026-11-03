import sys
import os
from flask import request, jsonify
from helper.sap_analytics_service import execute_category_sales
from helper.dashboard_cache_service import get_cached_component_data, set_cached_component_data, make_filter_context


def get_category_sales_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = request.args.get("session_id")
        zone = request.args.get("zone")
        force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")
        filter_context = make_filter_context(request.args)

        from helper.dashboard_cache_service import get_category_sales_table_data, save_category_sales_table_data
        if not force_refresh:
            tbl_cat = get_category_sales_table_data(conn, session_id, zone=zone)
            if tbl_cat and tbl_cat.get("data"):
                return jsonify({
                    "status": "success",
                    "source": "dashboard_category_sales",
                    "data": tbl_cat["data"],
                    "_cached": True,
                    "_last_synced_at": tbl_cat.get("_last_synced_at")
                }), 200

            cached = get_cached_component_data(conn, session_id, "category_sales", filter_context)
            if cached and cached.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        results = execute_category_sales(conn, session_id, zone=zone)
        if results is not None:
            save_category_sales_table_data(conn, session_id, zone, results)
            set_cached_component_data(conn, session_id, "category_sales", results, filter_context)

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
