from flask import jsonify, request
from helper.sap_analytics_service import (
    execute_sales_revenue_by_zone,
    execute_sales_by_account_category,
    execute_non_billed_accounts_pct,
    execute_overdue_pct,
    execute_exposure_pct,
)
from helper.dashboard_cache_service import get_cached_component_data, set_cached_component_data, make_filter_context


def _resolve_session_id(conn, session_id):
    if session_id and session_id != "null" and session_id != "undefined":
        return session_id.strip()
    try:
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("SELECT session_id FROM workspaces WHERE session_id IS NOT NULL AND session_id != '' ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        cur.close()
        if row and row.get("session_id"):
            return row["session_id"]
    except Exception:
        pass
    return session_id or ""


def get_sales_revenue_data_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        session_id = _resolve_session_id(conn, request.args.get("session_id"))
        zone = request.args.get("zone")
        force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")
        filter_context = make_filter_context(request.args)

        if not force_refresh:
            cached = get_cached_component_data(conn, session_id, "sales_revenue", filter_context)
            if cached and cached.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

            # Instant in-memory filter from pre-cached 'all' data
            if zone and str(zone).lower() != "all":
                cached_all = get_cached_component_data(conn, session_id, "sales_revenue", "all")
                if cached_all and isinstance(cached_all.get("data"), list):
                    z_target = str(zone).strip().lower()
                    filtered_rows = [
                        r for r in cached_all["data"]
                        if (str(r.get("zone", "")).strip().lower() == z_target or
                            str(r.get("zoneCode", "")).strip().lower() == z_target)
                    ]
                    if filtered_rows:
                        set_cached_component_data(conn, session_id, "sales_revenue", filtered_rows, filter_context)
                        return jsonify({
                            "status": "success",
                            "data": filtered_rows,
                            "_cached": True,
                            "_last_synced_at": cached_all.get("_last_synced_at")
                        }), 200

        chart_data = execute_sales_revenue_by_zone(conn, session_id, zone=zone)
        if chart_data is not None:
            set_cached_component_data(conn, session_id, "sales_revenue", chart_data, filter_context)

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
        session_id = _resolve_session_id(conn, request.args.get("session_id"))
        zone = request.args.get("zone")
        force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")
        filter_context = make_filter_context(request.args)

        if not force_refresh:
            cached = get_cached_component_data(conn, session_id, "sales_by_account", filter_context)
            if cached and cached.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        chart_data = execute_sales_by_account_category(conn, session_id, zone=zone)
        if chart_data is not None:
            set_cached_component_data(conn, session_id, "sales_by_account", chart_data, filter_context)

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
        session_id = _resolve_session_id(conn, request.args.get("session_id"))
        zone = request.args.get("zone")
        force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")
        is_default = not zone or zone.lower() == "all"

        if is_default and not force_refresh:
            cached = get_cached_component_data(conn, session_id, "non_billed_pct", "all")
            if cached and cached.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        chart_data = execute_non_billed_accounts_pct(conn, session_id, zone=zone)
        if is_default and chart_data is not None:
            set_cached_component_data(conn, session_id, "non_billed_pct", chart_data, "all")

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
        session_id = _resolve_session_id(conn, request.args.get("session_id"))
        zone = request.args.get("zone")
        force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")
        is_default = not zone or zone.lower() == "all"

        if is_default and not force_refresh:
            cached = get_cached_component_data(conn, session_id, "overdue_pct", "all")
            if cached and cached.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        chart_data = execute_overdue_pct(conn, session_id, zone=zone)
        if is_default and chart_data is not None:
            set_cached_component_data(conn, session_id, "overdue_pct", chart_data, "all")

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
        session_id = _resolve_session_id(conn, request.args.get("session_id"))
        zone = request.args.get("zone")
        force_refresh = request.args.get("force_refresh", "").lower() in ("true", "1")
        is_default = not zone or zone.lower() == "all"

        if is_default and not force_refresh:
            cached = get_cached_component_data(conn, session_id, "exposure_pct", "all")
            if cached and cached.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        chart_data = execute_exposure_pct(conn, session_id, zone=zone)
        if is_default and chart_data is not None:
            set_cached_component_data(conn, session_id, "exposure_pct", chart_data, "all")

        return jsonify({"status": "success", "data": chart_data}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            conn.close()
