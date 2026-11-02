from flask import request, jsonify
from helper.dashboard_cache_service import get_scheduler_config, save_or_update_scheduler_config
from helper.scheduler_service import reschedule_job, trigger_now_in_background


def get_scheduler_config_controller(get_db_connection):
    """GET /api/scheduler/config: Returns current scheduler configuration for UI."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        cfg = get_scheduler_config(conn, "sync_dashboard_metrics")
        if not cfg:
            # Default unconfigured view for UI
            return jsonify({
                "status": "success",
                "data": {
                    "job_name": "sync_dashboard_metrics",
                    "title": "Dashboard Key Metrics & Analytics Sync",
                    "schedule_time": "19:00",
                    "frequency": "daily",
                    "is_active": True,
                    "is_configured": False,
                    "last_run_at": None,
                    "last_run_status": "idle",
                    "next_run_at": None
                }
            }), 200

        return jsonify({
            "status": "success",
            "data": {
                "job_name": cfg.get("job_name"),
                "title": cfg.get("title"),
                "schedule_time": cfg.get("schedule_time") or "19:00",
                "frequency": cfg.get("frequency") or "daily",
                "is_active": bool(cfg.get("is_active", True)),
                "is_configured": True,
                "last_run_at": str(cfg["last_run_at"]) if cfg.get("last_run_at") else None,
                "last_run_status": cfg.get("last_run_status") or "idle",
                "last_run_duration_sec": float(cfg.get("last_run_duration_sec") or 0),
                "error_message": cfg.get("error_message"),
                "next_run_at": str(cfg["next_run_at"]) if cfg.get("next_run_at") else None
            }
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


def update_scheduler_config_controller(get_db_connection):
    """POST /api/scheduler/config: Updates schedule directly from Master/Settings UI."""
    data = request.json or {}
    schedule_time = data.get("schedule_time", "19:00").strip()
    frequency = data.get("frequency", "daily").strip()
    is_active = data.get("is_active", True)
    title = data.get("title", "Dashboard Key Metrics & Analytics Sync")

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        success = save_or_update_scheduler_config(
            conn,
            job_name="sync_dashboard_metrics",
            schedule_time=schedule_time,
            frequency=frequency,
            is_active=is_active,
            title=title
        )
        if not success:
            return jsonify({"status": "error", "message": "Failed to save configuration in database"}), 500

        # Reschedule in-memory APScheduler dynamically
        next_run = reschedule_job(schedule_time, frequency, is_active)

        return jsonify({
            "status": "success",
            "message": "Scheduler configuration updated successfully.",
            "data": {
                "schedule_time": schedule_time,
                "frequency": frequency,
                "is_active": is_active,
                "next_run_at": next_run
            }
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


def run_scheduler_now_controller(get_db_connection):
    """POST /api/scheduler/run-now: Triggers immediate background sync."""
    conn = get_db_connection()
    if conn:
        try:
            from helper.dashboard_cache_service import record_scheduler_run
            record_scheduler_run(conn, "sync_dashboard_metrics", "running", 0.0)
        except Exception as err:
            print(f"[Scheduler] Could not mark running: {err}", flush=True)
        finally:
            if hasattr(conn, "is_connected") and conn.is_connected():
                try: conn.close()
                except Exception: pass
    try:
        trigger_now_in_background()
        return jsonify({
            "status": "success",
            "message": "Dashboard sync triggered in background successfully."
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

