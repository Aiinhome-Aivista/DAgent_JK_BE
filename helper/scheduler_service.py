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
        try:
            record_scheduler_run(conn, "sync_dashboard_metrics", "running", 0.0)
        except Exception as r_err:
            print(f"[Scheduler] Could not set running status: {r_err}", flush=True)
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
        from controllers.domestic_sales_value_achievement_controller import ensure_table as ensure_domestic_sales_table
        ensure_domestic_sales_table(conn)
        from controllers.sales_report_by_values_controller import ensure_table as ensure_sales_report_by_values_table
        ensure_sales_report_by_values_table(conn)
        from controllers.sales_summary_no_values_controller import ensure_table as ensure_sales_summary_no_values_table
        ensure_sales_summary_no_values_table(conn)
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
        from utils.logger_service import log_system_error

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
            print(f"[Scheduler] ⏳ [1/13] Syncing Key Metrics (16 KPIs)...", flush=True)
            try:
                with _app_instance.test_request_context(json={"session_id": s_id, "user_id": u_id, "force_refresh": True}):
                    default_dashboard_metrics_controller(_get_db_conn_func)
                print(f"[Scheduler] ✅ [1/13] Key Metrics synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [1/13] Error syncing key_metrics: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:key_metrics", error_msg=str(e))
                except Exception: pass

            # 2. Executive Sales & Performance Analysis Summary (HANA Core KPIs + LLM Summary)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [2/13] Syncing Executive Sales & Performance Analysis Summary...", flush=True)
            try:
                from controllers.session_analysis_controller import run_daily_session_analysis_scheduler
                run_daily_session_analysis_scheduler(_get_db_conn_func, session_id=s_id)
                print(f"[Scheduler] ✅ [2/13] Executive Analysis Summary synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [2/13] Error syncing executive_analysis_summary: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:executive_analysis_summary", error_msg=str(e))
                except Exception: pass

            # 3. Domestic Sales Value Achievement (Sheet 1 - Dedicated table: sales_summary_domestic_sales_value_achievement - All Month Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [3/13] Syncing Domestic Sales Value Achievement (Sheet 1 - All Month Permutations)...", flush=True)
            try:
                from controllers.domestic_sales_value_achievement_controller import bulk_sync_domestic_sales_value_achievement
                bulk_sync_domestic_sales_value_achievement(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [3/13] Domestic Sales Value Achievement (Sheet 1) synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [3/13] Error syncing domestic_sales_value_achievement: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:domestic_sales_value_achievement", error_msg=str(e))
                except Exception: pass

            # 4. Sales Number's (Sheet 2 - Dedicated table: sales_summary_sales_numbers - All Month Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [4/13] Syncing Sales Number's (Sheet 2 - All Month Permutations)...", flush=True)
            try:
                from controllers.sales_numbers_report_controller import bulk_sync_sales_numbers_report
                bulk_sync_sales_numbers_report(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [4/13] Sales Number's (Sheet 2) synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [4/13] Error syncing sales_numbers_report: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:sales_numbers_report", error_msg=str(e))
                except Exception: pass

            # 5. Sales Report by Values (Sheet 3 - Dedicated table: sales_summary_sales_report_by_values - All Month Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [5/13] Syncing Sales Report by Values (Sheet 3 - All Month Permutations)...", flush=True)
            try:
                from controllers.sales_report_by_values_controller import bulk_sync_sales_report_by_values
                bulk_sync_sales_report_by_values(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [5/13] Sales Report by Values (Sheet 3) synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [5/13] Error syncing sales_report_by_values: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:sales_report_by_values", error_msg=str(e))
                except Exception: pass

            # 6. Sales Summary in No's & Values (Sheet 4 - Dedicated table: sales_summary_no_values - All Month Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [6/13] Syncing Sales Summary in No's & Values (Sheet 4 - All Month Permutations)...", flush=True)
            try:
                from controllers.sales_summary_no_values_controller import bulk_sync_sales_summary_no_values
                bulk_sync_sales_summary_no_values(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [6/13] Sales Summary in No's & Values (Sheet 4) synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [6/13] Error syncing sales_summary_no_values: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:sales_summary_no_values", error_msg=str(e))
                except Exception: pass

            # 7. Sales Revenue Plan vs Sale (Dedicated controller & table)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [7/13] Syncing Sales Revenue Plan vs Sale...", flush=True)
            try:
                from controllers.sales_revenue_dashboard_controller import get_sales_revenue_data_controller
                with _app_instance.test_request_context(query_string={"session_id": s_id, "user_id": u_id, "force_refresh": "true"}):
                    get_sales_revenue_data_controller(_get_db_conn_func)
                print(f"[Scheduler] ✅ [7/13] Sales Revenue Plan vs Sale synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [7/13] Error syncing sales_revenue: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:sales_revenue", error_msg=str(e))
                except Exception: pass

            # 8. Year-wise Sales Comparison (Dedicated table: year_wise_sales_comparison - Bulk Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [8/13] Syncing Year-wise Sales (All Bulk Permutations)...", flush=True)
            try:
                from controllers.year_wise_dashboard_controller import bulk_sync_year_wise_sales_comparison
                bulk_sync_year_wise_sales_comparison(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [8/13] Year-wise Sales permutations synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [8/13] Error syncing year_wise_sales: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:year_wise_sales", error_msg=str(e))
                except Exception: pass

            # 9. Sales By Zone (Pie Chart - Dedicated table: dashboard_sales_by_zone - Bulk Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [9/13] Syncing Sales by Zone (All Bulk Permutations)...", flush=True)
            try:
                from controllers.sales_by_zone_dashboard_controller import bulk_sync_sales_by_zone_permutations
                bulk_sync_sales_by_zone_permutations(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [9/13] Sales by Zone permutations synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [9/13] Error syncing sales_by_zone: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:sales_by_zone", error_msg=str(e))
                except Exception: pass

            # 10. Top 10 Tyre Types by Sales (Dedicated table: dashboard_tyre_sales_top_10 - Bulk Permutations)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [10/13] Syncing Top 10 Tyre Types by Sales (All Bulk Permutations)...", flush=True)
            try:
                from controllers.tyre_sales_dashboard_controller import bulk_sync_tyre_sales_permutations
                bulk_sync_tyre_sales_permutations(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [10/13] Tyre Sales permutations synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [10/13] Error syncing tyre_sales: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:tyre_sales", error_msg=str(e))
                except Exception: pass

            # 11. Top 10 & Bottom 10 Sales Performance (Dedicated table: dashboard_top_bottom_performance)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [11/13] Syncing Top 10 & Bottom 10 Sales Performance (All Days & Dimensions)...", flush=True)
            try:
                from controllers.top_bottom_performance_controller import bulk_sync_top_bottom_performance
                bulk_sync_top_bottom_performance(conn, s_id, u_id)
                print(f"[Scheduler] ✅ [11/13] Top 10 & Bottom 10 Performance synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [11/13] Error syncing top_bottom_performance: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:top_bottom_performance", error_msg=str(e))
                except Exception: pass

            # 12. Actual Sales by Account Category (Dedicated controller & table)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [12/13] Syncing Sales by Account Category...", flush=True)
            try:
                from controllers.account_category_sales_dashboard_controller import get_account_category_sales_controller
                with _app_instance.test_request_context(query_string={"session_id": s_id, "user_id": u_id, "force_refresh": "true"}):
                    get_account_category_sales_controller(_get_db_conn_func)
                print(f"[Scheduler] ✅ [12/13] Sales by Account Category synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [12/13] Error syncing sales_by_account: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:sales_by_account", error_msg=str(e))
                except Exception: pass

            # 13. Category Sales (Dedicated controller & table)
            t_step = time.time()
            print(f"[Scheduler] ⏳ [13/13] Syncing Category Sales...", flush=True)
            try:
                from controllers.category_sales_dashboard_controller import get_category_sales_controller
                with _app_instance.test_request_context(query_string={"session_id": s_id, "user_id": u_id, "force_refresh": "true"}):
                    get_category_sales_controller(_get_db_conn_func)
                print(f"[Scheduler] ✅ [13/13] Category Sales synced in {round(time.time() - t_step, 2)}s", flush=True)
            except Exception as e:
                print(f"[Scheduler] ❌ [13/13] Error syncing category_sales: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:category_sales", error_msg=str(e))
                except Exception: pass

            # Filter Metadata Cache
            try:
                with _app_instance.test_request_context(query_string={"session_id": s_id, "user_id": u_id}):
                    available_years_controller(_get_db_conn_func)
                with _app_instance.test_request_context(query_string={"session_id": s_id, "user_id": u_id}):
                    dashboard_filters_controller(_get_db_conn_func)
            except Exception as e:
                print(f"[Scheduler] Note on filters cache: {e}", flush=True)
                try: log_system_error(session_id=s_id, module_name="scheduler:filters_cache", error_msg=str(e))
                except Exception: pass

            print(f"[Scheduler] 🎉 Completed all 13 dashboard components (Key Metrics, 7 Charts, 4 Sales Summary Sheets & Executive Analysis) for '{ws_name}' in {round(time.time() - ws_start_t, 2)}s!", flush=True)
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
        try:
            from utils.logger_service import log_system_error
            log_system_error(session_id="scheduler", module_name="sync_dashboard_metrics", error_msg=err_msg)
        except Exception:
            pass
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
    elif freq in ("every_2_hours", "2_hours", "every_2_hour", "every_two_hours"):
        # Run every 2 hours starting from the given schedule_time hour & minute
        # e.g., if schedule_time is 11:45, runs at 11:45, 13:45 (1:45 PM), 15:45 (3:45 PM), 17:45 (5:45 PM), 19:45 (7:45 PM)...
        hours = sorted(list(set((hour + 2 * i) % 24 for i in range(12))))
        hour_str = ",".join(str(h) for h in hours)
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
        return None

    if not is_active:
        if _scheduler.get_job(JOB_ID):
            _scheduler.remove_job(JOB_ID)
            print(f"[Scheduler] Job '{JOB_ID}' paused by user from UI.", flush=True)
        return None

    trigger = parse_schedule_trigger(schedule_time, frequency)
    _scheduler.add_job(
        _execute_sync_task,
        trigger=trigger,
        id=JOB_ID,
        name="Sync Dashboard Key Metrics",
        replace_existing=True
    )
    job = _scheduler.get_job(JOB_ID)
    next_run = job.next_run_time if job else None
    next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
    print(f"[Scheduler] Rescheduled job '{JOB_ID}' to {schedule_time} ({frequency}). Next run: {next_run_str}", flush=True)
    return next_run_str


def get_next_run_time(schedule_time: str = None, frequency: str = "daily"):
    """Returns next run time string '%Y-%m-%d %H:%M:%S' from active APScheduler or computes it."""
    global _scheduler
    if _scheduler:
        job = _scheduler.get_job(JOB_ID)
        if job and job.next_run_time:
            return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
    if schedule_time:
        try:
            from datetime import datetime, timezone, timedelta
            ist = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(ist)
            parts = str(schedule_time).strip().split(":")
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

            freq = (frequency or "daily").lower().strip()
            if freq in ("every_2_hours", "2_hours", "every_2_hour", "every_two_hours"):
                hours = sorted(list(set((h + 2 * i) % 24 for i in range(12))))
                for day_offset in range(2):
                    for hr in hours:
                        cand = (now + timedelta(days=day_offset)).replace(hour=hr, minute=m, second=0, microsecond=0)
                        if cand > now:
                            return cand.strftime("%Y-%m-%d %H:%M:%S")
            elif freq == "hourly":
                cand = now.replace(minute=m, second=0, microsecond=0)
                if cand <= now:
                    cand += timedelta(hours=1)
                return cand.strftime("%Y-%m-%d %H:%M:%S")
            elif freq in ("twice_daily", "2_times_daily"):
                candidates = [
                    now.replace(hour=h, minute=m, second=0, microsecond=0),
                    now.replace(hour=(h + 12) % 24, minute=m, second=0, microsecond=0)
                ]
                for cand in sorted(candidates):
                    if cand > now:
                        return cand.strftime("%Y-%m-%d %H:%M:%S")
                return (min(candidates) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate <= now:
                    candidate += timedelta(days=1)
                return candidate.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return None


def trigger_now_in_background():
    """Triggers the sync task immediately in a separate background thread."""
    t = threading.Thread(target=_execute_sync_task, daemon=True)
    t.start()
    return True
