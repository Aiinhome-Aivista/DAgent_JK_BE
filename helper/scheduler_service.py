import time
import threading
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler = None
_app_instance = None
_get_db_conn_func = None
JOB_ID = "sync_dashboard_metrics_job"


def _execute_sync_task():
    """Background task that calculates and caches metrics for all active workspaces."""
    global _app_instance, _get_db_conn_func
    if not _app_instance or not _get_db_conn_func:
        print("[Scheduler] App or DB connection not ready, skipping sync.", flush=True)
        return

    from helper.dashboard_cache_service import record_scheduler_run
    conn = _get_db_conn_func()
    if not conn:
        print("[Scheduler] Failed to connect to DB for sync job.", flush=True)
        return

    start_t = time.time()
    try:
        from helper.dashboard_cache_service import (
            ensure_key_metrics_table,
            ensure_sales_by_zone_table,
            ensure_tyre_sales_table,
            ensure_category_sales_table,
            ensure_account_category_sales_table
        )
        from controllers.sales_dashboard import ensure_sales_revenue_table
        from controllers.year_wise_dashboard_controller import ensure_year_wise_sales_table
        ensure_key_metrics_table(conn)
        ensure_sales_revenue_table(conn)
        ensure_year_wise_sales_table(conn)
        ensure_sales_by_zone_table(conn)
        ensure_tyre_sales_table(conn)
        ensure_category_sales_table(conn)
        ensure_account_category_sales_table(conn)
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("""
            SELECT session_id, MAX(user_id) AS user_id, MAX(workspace_name) AS workspace_name 
            FROM workspaces 
            WHERE session_id IS NOT NULL AND session_id != ''
            GROUP BY session_id
        """)
        workspaces = cur.fetchall()
        cur.close()

        print(f"[Scheduler] Starting sync for {len(workspaces)} workspaces...", flush=True)

        from controllers.dashboard_visuals import (
            default_dashboard_metrics_controller,
            tyre_sales_data_controller,
            sales_by_zone_data_controller,
            year_wise_sales_comparison_controller,
            available_years_controller,
            dashboard_filters_controller,
        )
        from controllers.export_report_controller import export_domestic_sales_preview_controller
        from helper.sap_analytics_service import (
            execute_sales_revenue_by_zone,
            execute_sales_by_account_category,
            execute_category_sales,
            execute_non_billed_accounts_pct,
            execute_overdue_pct,
            execute_exposure_pct,
        )
        from helper.dashboard_cache_service import set_cached_component_data

        for ws in workspaces:
            s_id = ws.get("session_id")
            u_id = ws.get("user_id")
            ws_name = ws.get("workspace_name") or ws.get("session_id")
            if not s_id:
                continue

            print(f"\n[Scheduler] ====================================================================", flush=True)
            print(f"[Scheduler] 🚀 Starting complete metrics sync for workspace '{ws_name}' ({s_id[:8]}...)", flush=True)
            print(f"[Scheduler] ====================================================================", flush=True)
            ws_start_t = time.time()

            # 1. Key Metrics (16 KPIs)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [1/7] Syncing Key Metrics (16 KPIs)...", flush=True)
            try:
                with _app_instance.test_request_context(json={"session_id": s_id, "user_id": u_id, "force_refresh": True}):
                    default_dashboard_metrics_controller(_get_db_conn_func)
                print(f"[Scheduler] ✅ [1/7] Key Metrics synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [1/7] Error syncing key_metrics: {e}", flush=True)

            # 2. Tyre Sales Chart
            t_step = time.time()
            print(f"[Scheduler] ⏳ [2/7] Syncing Top 10 Tyre Types by Sales...", flush=True)
            try:
                with _app_instance.test_request_context(json={"session_id": s_id, "user_id": u_id, "force_refresh": True}):
                    tyre_sales_data_controller(_get_db_conn_func)
                print(f"[Scheduler] ✅ [2/7] Tyre Sales synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [2/7] Error syncing tyre_sales: {e}", flush=True)

            # 3. Sales By Zone (Pie Chart)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [3/7] Syncing Sales by Zone (Pie Chart)...", flush=True)
            try:
                with _app_instance.test_request_context(json={"session_id": s_id, "user_id": u_id, "force_refresh": True}):
                    sales_by_zone_data_controller(_get_db_conn_func)
                print(f"[Scheduler] ✅ [3/7] Sales by Zone synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [3/7] Error syncing sales_by_zone: {e}", flush=True)

            # 4. Year-wise Sales Comparison (Dedicated table: year_wise_sales_comparison - Bulk Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [4/7] Syncing Year-wise Sales (All Bulk Permutations)...", flush=True)
            try:
                from controllers.year_wise_dashboard_controller import bulk_sync_year_wise_sales_comparison
                bulk_sync_year_wise_sales_comparison(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [4/7] Year-wise Sales permutations synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [4/7] Error syncing year_wise_sales: {e}", flush=True)

            # 5. Sales Revenue Plan vs Sale (Dedicated table: sales_revenue_plan_vs_sale)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [5/7] Syncing Sales Revenue Plan vs Sale...", flush=True)
            try:
                from controllers.sales_dashboard import save_sales_revenue_table_data
                rev = execute_sales_revenue_by_zone(conn, s_id)
                if rev is not None:
                    save_sales_revenue_table_data(conn, s_id, "ALL", rev)
                    set_cached_component_data(conn, s_id, "sales_revenue", rev, "all")
                print(f"[Scheduler] ✅ [5/7] Sales Revenue Plan vs Sale synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [5/7] Error syncing sales_revenue: {e}", flush=True)

            # 6. Actual Sales by Account Category (Dedicated table: dashboard_account_category_sales)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [6/7] Syncing Sales by Account Category...", flush=True)
            try:
                from helper.dashboard_cache_service import save_account_category_sales_table_data
                acc = execute_sales_by_account_category(conn, s_id)
                if acc is not None:
                    save_account_category_sales_table_data(conn, s_id, "ALL", acc)
                    set_cached_component_data(conn, s_id, "sales_by_account", acc, "all")
                print(f"[Scheduler] ✅ [6/7] Sales by Account Category synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [6/7] Error syncing sales_by_account: {e}", flush=True)

            # 7. Category Sales (Dedicated table: dashboard_category_sales)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [7/7] Syncing Category Sales...", flush=True)
            try:
                from helper.dashboard_cache_service import save_category_sales_table_data
                cats = execute_category_sales(conn, s_id)
                if cats is not None:
                    save_category_sales_table_data(conn, s_id, "ALL", cats)
                    set_cached_component_data(conn, s_id, "category_sales", cats, "all")
                print(f"[Scheduler] ✅ [7/7] Category Sales synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [7/7] Error syncing category_sales: {e}", flush=True)

            # 8. Filter Metadata Cache
            try:
                with _app_instance.test_request_context(query_string={"session_id": s_id, "user_id": u_id}):
                    available_years_controller(_get_db_conn_func)
                with _app_instance.test_request_context(query_string={"session_id": s_id, "user_id": u_id}):
                    dashboard_filters_controller(_get_db_conn_func)
            except Exception as e:
                print(f"[Scheduler] Note on filters cache: {e}", flush=True)

            print(f"[Scheduler] 🎉 Completed all 7 dashboard components for '{ws_name}' in {round(time.time() - ws_start_t, 2)}s!", flush=True)
            print(f"[Scheduler] ====================================================================\n", flush=True)

        duration = round(time.time() - start_t, 2)
        next_run = None
        if _scheduler:
            job = _scheduler.get_job(JOB_ID)
            if job and job.next_run_time:
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")

        record_scheduler_run(conn, "sync_dashboard_metrics", "success", duration, next_run)
        print(f"[Scheduler] Sync completed successfully in {duration}s.", flush=True)

    except Exception as e:
        duration = round(time.time() - start_t, 2)
        import traceback
        err_msg = str(e)
        print(f"[Scheduler] Sync job failed: {err_msg}", flush=True)
        traceback.print_exc()
        next_run = None
        if _scheduler:
            job = _scheduler.get_job(JOB_ID)
            if job and job.next_run_time:
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        record_scheduler_run(conn, "sync_dashboard_metrics", "failed", duration, next_run, error_message=err_msg)
    finally:
        if conn and hasattr(conn, "is_connected") and conn.is_connected():
            try: conn.close()
            except Exception: pass


def parse_schedule_trigger(schedule_time: str, frequency: str = "daily"):
    """
    Parses schedule_time (e.g. '19:00') and frequency into an APScheduler CronTrigger.
    Default time is 19:00 (7:00 PM evening).
    """
    time_str = (schedule_time or "19:00").strip()
    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        hour, minute = 19, 0

    freq = (frequency or "daily").lower().strip()
    if freq in ("twice_daily", "2_times_daily"):
        hour_str = f"{hour},{(hour + 12) % 24}"
        return CronTrigger(hour=hour_str, minute=minute)
    elif freq in ("thrice_daily", "3_times_daily"):
        hour_str = f"{hour},{(hour + 8) % 24},{(hour + 16) % 24}"
        return CronTrigger(hour=hour_str, minute=minute)
    elif freq == "hourly":
        return CronTrigger(minute=minute)
    else:
        return CronTrigger(hour=hour, minute=minute)


def init_scheduler(app, get_db_connection):
    """Initializes and starts the in-app BackgroundScheduler."""
    global _scheduler, _app_instance, _get_db_conn_func
    _app_instance = app
    _get_db_conn_func = get_db_connection

    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata", daemon=True)

    conn = get_db_connection()
    if conn:
        try:
            from helper.dashboard_cache_service import get_scheduler_config
            cfg = get_scheduler_config(conn, "sync_dashboard_metrics")

            # If server was restarted while a previous run was in progress, reset to idle
            if cfg and cfg.get("last_run_status") in ("running", "in_progress"):
                try:
                    cur_rst = conn.cursor()
                    cur_rst.execute("UPDATE scheduler_configurations SET last_run_status = 'idle' WHERE job_name = 'sync_dashboard_metrics'")
                    conn.commit()
                    cur_rst.close()
                    print("[Scheduler] Reset interrupted running state to idle on startup.", flush=True)
                except Exception:
                    pass

            if cfg and cfg.get("is_active"):
                trigger = parse_schedule_trigger(cfg.get("schedule_time", "19:00"), cfg.get("frequency", "daily"))
                _scheduler.add_job(
                    _execute_sync_task,
                    trigger=trigger,
                    id=JOB_ID,
                    name="Sync Dashboard Key Metrics",
                    replace_existing=True
                )
                print(f"[Scheduler] Initialized job '{JOB_ID}' at {cfg.get('schedule_time')} ({cfg.get('frequency')})", flush=True)
            else:
                print("[Scheduler] Job is inactive or not yet configured from UI.", flush=True)
        except Exception as e:
            print(f"[Scheduler] Config load note: {e}", flush=True)
        finally:
            if conn and hasattr(conn, "is_connected") and conn.is_connected():
                try: conn.close()
                except Exception: pass

    _scheduler.start()
    return _scheduler


def reschedule_job(schedule_time: str, frequency: str = "daily", is_active: bool = True):
    """Dynamically reschedules the sync job when updated from UI without server restart."""
    global _scheduler
    if not _scheduler:
        return False

    if not is_active:
        if _scheduler.get_job(JOB_ID):
            _scheduler.remove_job(JOB_ID)
            print(f"[Scheduler] Job '{JOB_ID}' paused by user from UI.", flush=True)
        return True

    trigger = parse_schedule_trigger(schedule_time, frequency)
    _scheduler.add_job(
        _execute_sync_task,
        trigger=trigger,
        id=JOB_ID,
        name="Sync Dashboard Key Metrics",
        replace_existing=True
    )
    next_run = _scheduler.get_job(JOB_ID).next_run_time
    print(f"[Scheduler] Rescheduled job '{JOB_ID}' to {schedule_time} ({frequency}). Next run: {next_run}", flush=True)
    return str(next_run) if next_run else None


def trigger_now_in_background():
    """Triggers the sync task immediately in a separate background thread."""
    t = threading.Thread(target=_execute_sync_task, daemon=True)
    t.start()
    return True
