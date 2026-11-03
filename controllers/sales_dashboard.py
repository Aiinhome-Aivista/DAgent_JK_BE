from flask import jsonify, request
from helper.sap_analytics_service import (
    execute_sales_revenue_by_zone,
    execute_sales_by_account_category,
    execute_non_billed_accounts_pct,
    execute_overdue_pct,
    execute_exposure_pct,
)
from helper.dashboard_cache_service import get_cached_component_data, set_cached_component_data, make_filter_context


SALES_REVENUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `sales_revenue_plan_vs_sale` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `city_or_zone_name` VARCHAR(100) NOT NULL,
    `plan_value_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `sale_value_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `achiev_value_pct` DECIMAL(8, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_session_zone_city` (`session_id`, `zone_code`, `city_or_zone_name`),
    INDEX `idx_session_zone` (`session_id`, `zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_sr_table_initialized = False

def ensure_sales_revenue_table(conn):
    global _sr_table_initialized
    if _sr_table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(SALES_REVENUE_TABLE_SQL)
        conn.commit()
        cur.close()
        _sr_table_initialized = True
    except Exception as e:
        print(f"[Sales Revenue Table] init note: {e}", flush=True)

def get_sales_revenue_table_data(conn, session_id, zone=None):
    if not conn or not session_id:
        return None
    try:
        ensure_sales_revenue_table(conn)
        zone_val = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("""
            SELECT city_or_zone_name, plan_value_cr, sale_value_cr, achiev_value_pct, last_synced_at
            FROM `sales_revenue_plan_vs_sale`
            WHERE session_id = %s AND zone_code = %s
            ORDER BY city_or_zone_name ASC
        """, (str(session_id).strip(), zone_val))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return None

        result = []
        last_sync = None
        for r in rows:
            c_name = r.get("city_or_zone_name") if isinstance(r, dict) else r[0]
            p_val = float(r.get("plan_value_cr") if isinstance(r, dict) else r[1])
            s_val = float(r.get("sale_value_cr") if isinstance(r, dict) else r[2])
            a_pct = float(r.get("achiev_value_pct") if isinstance(r, dict) else r[3])
            last_sync = r.get("last_synced_at") if isinstance(r, dict) else r[4]
            result.append({
                "zone": c_name,
                "city": c_name,
                "city_or_zone_name": c_name,
                "planValueCr": p_val,
                "saleValueCr": s_val,
                "achievValuePct": a_pct
            })

        if result:
            return {"data": result, "_last_synced_at": last_sync}
        return None
    except Exception as e:
        print(f"[Sales Revenue Table] Read error: {e}", flush=True)
        return None

def save_sales_revenue_table_data(conn, session_id, zone, chart_data):
    if not conn or not session_id or not chart_data:
        return False
    try:
        ensure_sales_revenue_table(conn)
        zone_val = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = conn.cursor()
        # Trigger / Refresh rule: Delete existing rows for this session and zone, then insert fresh
        cur.execute("DELETE FROM `sales_revenue_plan_vs_sale` WHERE session_id = %s AND zone_code = %s", (str(session_id).strip(), zone_val))

        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')

        ins_sql = """
            INSERT INTO `sales_revenue_plan_vs_sale`
                (`session_id`, `zone_code`, `city_or_zone_name`, `plan_value_cr`, `sale_value_cr`, `achiev_value_pct`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        batch = []
        for item in chart_data:
            c_name = str(item.get("city") or item.get("zone") or item.get("Region") or item.get("region") or "Unknown")[:100]
            p_val = float(item.get("planValueCr") or 0.0)
            s_val = float(item.get("saleValueCr") or item.get("saleV") or 0.0)
            a_val = float(item.get("achievValuePct") or 0.0)
            batch.append((str(session_id).strip(), zone_val, c_name, p_val, s_val, a_val, ist_now, ist_now))

        if batch:
            cur.executemany(ins_sql, batch)
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Sales Revenue Table] Save error: {e}", flush=True)
        return False


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

        # 1. Fast read from dedicated sales_revenue_plan_vs_sale table
        if not force_refresh:
            tbl_res = get_sales_revenue_table_data(conn, session_id, zone=zone)
            if tbl_res and tbl_res.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "source": "sales_revenue_plan_vs_sale",
                    "data": tbl_res["data"],
                    "_cached": True,
                    "_last_synced_at": tbl_res.get("_last_synced_at")
                }), 200

            cached = get_cached_component_data(conn, session_id, "sales_revenue", filter_context)
            if cached and cached.get("data") is not None:
                return jsonify({
                    "status": "success",
                    "data": cached["data"],
                    "_cached": True,
                    "_last_synced_at": cached.get("_last_synced_at")
                }), 200

        # 2. Live query execution
        chart_data = execute_sales_revenue_by_zone(conn, session_id, zone=zone)
        if chart_data is not None:
            save_sales_revenue_table_data(conn, session_id, zone, chart_data)
            set_cached_component_data(conn, session_id, "sales_revenue", chart_data, filter_context)

        return jsonify({
            "status": "success",
            "source": "live_query",
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

        from helper.dashboard_cache_service import get_account_category_sales_table_data, save_account_category_sales_table_data
        if not force_refresh:
            tbl_acc = get_account_category_sales_table_data(conn, session_id, zone=zone)
            if tbl_acc and tbl_acc.get("data"):
                return jsonify({
                    "status": "success",
                    "source": "dashboard_account_category_sales",
                    "data": tbl_acc["data"],
                    "_cached": True,
                    "_last_synced_at": tbl_acc.get("_last_synced_at")
                }), 200

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
            save_account_category_sales_table_data(conn, session_id, zone, chart_data)
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
