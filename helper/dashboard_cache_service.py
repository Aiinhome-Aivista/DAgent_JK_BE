import json
import time
from datetime import datetime, timezone, timedelta

IST_TZ = timezone(timedelta(hours=5, minutes=30))

def get_now_ist():
    return datetime.now(IST_TZ).strftime("%Y-%m-%d %H:%M:%S")

get_ist_now_str = get_now_ist

CACHE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_cached_metrics` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL COMMENT 'Workspace or Session ID',
    `component_key` VARCHAR(100) NOT NULL COMMENT 'e.g. key_metrics, sales_revenue, sales_by_zone',
    `filter_context` VARCHAR(255) DEFAULT 'all' COMMENT 'Filter details e.g. all or zone=NZ',
    `payload` LONGTEXT NOT NULL COMMENT 'Complete JSON payload rendered directly by UI',
    `row_count` INT DEFAULT 0 COMMENT 'Number of records represented',
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `sync_status` ENUM('success', 'failed', 'running') DEFAULT 'success',
    `execution_duration_sec` DECIMAL(8, 2) DEFAULT 0.00,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_session_component_filter` (`session_id`, `component_key`, `filter_context`),
    INDEX `idx_session_component` (`session_id`, `component_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

SCHEDULER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `scheduler_configurations` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `job_name` VARCHAR(100) UNIQUE NOT NULL,
    `title` VARCHAR(255) DEFAULT 'Dashboard Key Metrics & Analytics Sync',
    `schedule_time` VARCHAR(10) DEFAULT '19:00' COMMENT 'Daily time in 24hr HH:MM format',
    `frequency` VARCHAR(50) DEFAULT 'daily' COMMENT 'daily, twice_daily, thrice_daily',
    `is_active` BOOLEAN DEFAULT TRUE,
    `last_run_at` DATETIME DEFAULT NULL,
    `last_run_status` VARCHAR(50) DEFAULT 'idle',
    `last_run_duration_sec` DECIMAL(8, 2) DEFAULT 0.00,
    `error_message` TEXT DEFAULT NULL,
    `next_run_at` DATETIME DEFAULT NULL,
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_tables_initialized = False

def ensure_cache_tables(conn):
    """Ensures caching and scheduler tables exist in MySQL database (no hardcoded rows inserted)."""
    global _tables_initialized
    if _tables_initialized or not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(CACHE_TABLE_SQL)
        cur.execute(SCHEDULER_TABLE_SQL)
        conn.commit()
        # Ensure error_message column exists if table was created previously
        try:
            cur.execute("ALTER TABLE `scheduler_configurations` ADD COLUMN `error_message` TEXT DEFAULT NULL AFTER `last_run_duration_sec`")
            conn.commit()
        except Exception:
            pass
        _tables_initialized = True
    except Exception as e:
        print(f"[Dashboard Cache] Table init note: {e}", flush=True)
    finally:
        if cur:
            try: cur.close()
            except Exception: pass


def make_filter_context(params) -> str:
    """
    Builds a consistent, normalized filter context string from incoming filter parameters.
    Handles single values, lists, and dicts. Returns 'all' if no filters are selected.
    """
    if not params or not isinstance(params, dict):
        return "all"
    parts = []

    def get_clean_items(val):
        if val is None:
            return []
        if isinstance(val, (list, tuple, set)):
            items = []
            for x in val:
                s = str(x).strip()
                if s and s.lower() != "all":
                    items.append(s)
            return sorted(items)
        s = str(val).strip()
        return [s] if (s and s.lower() != "all") else []

    year_items = get_clean_items(params.get("selected_years") or params.get("years") or params.get("year"))
    if year_items:
        parts.append(f"year:{','.join(year_items)}")

    zone_items = get_clean_items(params.get("selected_zones") or params.get("zones") or params.get("zone") or params.get("Zone"))
    if zone_items:
        parts.append(f"zone:{','.join(zone_items)}")

    region_items = get_clean_items(params.get("selected_regions") or params.get("regions") or params.get("region") or params.get("Region"))
    if region_items:
        parts.append(f"reg:{','.join(region_items)}")

    cat_items = get_clean_items(params.get("selected_customer_categories") or params.get("customer_categories") or params.get("customer_category") or params.get("customer_types") or params.get("customer_type"))
    if cat_items:
        parts.append(f"cat:{','.join(cat_items)}")

    con_items = get_clean_items(params.get("selected_constructions") or params.get("construction_types") or params.get("construction_type") or params.get("constructions") or params.get("construction"))
    if con_items:
        parts.append(f"con:{','.join(con_items)}")

    month_items = get_clean_items(params.get("selected_months") or params.get("months") or params.get("month"))
    if month_items:
        parts.append(f"m:{','.join(month_items)}")

    if not parts:
        return "all"
    return "|".join(parts)


def get_cached_component_data(conn, session_id: str, component_key: str, filter_context: str = "all"):
    """
    Retrieves pre-calculated component data from MySQL cache table.
    Returns python dict with data or None.
    """
    if not conn or not component_key:
        return None

    cur = None
    try:
        ensure_cache_tables(conn)
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        row = None
        if session_id:
            cur.execute("""
                SELECT payload, last_synced_at, sync_status
                FROM dashboard_cached_metrics
                WHERE session_id = %s AND component_key = %s AND filter_context = %s
                LIMIT 1
            """, (str(session_id), str(component_key), str(filter_context)))
            row = cur.fetchone()

        if not row:
            # Fallback: check most recent cached record for this component across sessions
            cur.execute("""
                SELECT payload, last_synced_at, sync_status
                FROM dashboard_cached_metrics
                WHERE component_key = %s AND filter_context = %s
                ORDER BY last_synced_at DESC
                LIMIT 1
            """, (str(component_key), str(filter_context)))
            row = cur.fetchone()

        if row and row.get("payload"):
            payload = json.loads(row["payload"])
            return {
                "data": payload,
                "_cached": True,
                "_last_synced_at": str(row.get("last_synced_at")),
                "_sync_status": row.get("sync_status")
            }
        return None
    except Exception as e:
        print(f"[Dashboard Cache] Error reading cache for {component_key}: {e}", flush=True)
        return None
    finally:
        if cur:
            try: cur.close()
            except Exception: pass


import decimal

def _json_serial_default(obj):
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def set_cached_component_data(conn, session_id: str, component_key: str, payload: dict, filter_context: str = "all", duration_sec: float = 0.0):
    """
    Upserts calculated component data into MySQL cache table.
    """
    if not conn or not session_id or not component_key or payload is None:
        return False

    cur = None
    try:
        ensure_cache_tables(conn)
        cur = conn.cursor()
        json_str = json.dumps(payload, ensure_ascii=False, default=_json_serial_default)
        now_ist = get_now_ist()
        cur.execute("""
            INSERT INTO dashboard_cached_metrics
                (session_id, component_key, filter_context, payload, sync_status, execution_duration_sec, last_synced_at)
            VALUES (%s, %s, %s, %s, 'success', %s, %s)
            ON DUPLICATE KEY UPDATE
                payload = VALUES(payload),
                sync_status = 'success',
                execution_duration_sec = VALUES(execution_duration_sec),
                last_synced_at = %s;
        """, (str(session_id), str(component_key), str(filter_context), json_str, duration_sec, now_ist, now_ist))
        conn.commit()
        return True
    except Exception as e:
        print(f"[Dashboard Cache] Error saving cache for {component_key}: {e}", flush=True)
        return False
    finally:
        if cur:
            try: cur.close()
            except Exception: pass


def get_scheduler_config(conn, job_name: str = "sync_dashboard_metrics"):
    """Fetches configuration for a scheduler job (from DB, configured via UI)."""
    if not conn:
        return None
    cur = None
    try:
        ensure_cache_tables(conn)
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("SELECT * FROM scheduler_configurations WHERE job_name = %s LIMIT 1", (job_name,))
        row = cur.fetchone()
        return row
    except Exception as e:
        print(f"[Scheduler Service] Error reading config: {e}", flush=True)
        return None
    finally:
        if cur:
            try: cur.close()
            except Exception: pass


def save_or_update_scheduler_config(conn, job_name: str, schedule_time: str, frequency: str = "daily", is_active: bool = True, title: str = None, next_run_at=None):
    """Saves or updates scheduler configuration directly from UI form."""
    if not conn or not job_name or not schedule_time:
        return False
    cur = None
    try:
        ensure_cache_tables(conn)
        cur = conn.cursor()
        title_val = title or "Dashboard Key Metrics & Analytics Sync"
        cur.execute("""
            INSERT INTO scheduler_configurations
                (job_name, title, schedule_time, frequency, is_active, next_run_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                title = VALUES(title),
                schedule_time = VALUES(schedule_time),
                frequency = VALUES(frequency),
                is_active = VALUES(is_active),
                next_run_at = VALUES(next_run_at),
                updated_at = NOW();
        """, (str(job_name).strip(), title_val, str(schedule_time).strip(), str(frequency).strip(), bool(is_active), next_run_at))
        conn.commit()
        return True
    except Exception as e:
        print(f"[Scheduler Service] Error saving config from UI: {e}", flush=True)
        return False
    finally:
        if cur:
            try: cur.close()
            except Exception: pass


def record_scheduler_run(conn, job_name: str, status: str, duration_sec: float = 0.0, next_run_at=None, error_message: str = None):
    """Records the execution status and duration of a scheduler job."""
    if not conn:
        return False
    cur = None
    try:
        ensure_cache_tables(conn)
        cur = conn.cursor()
        err_val = str(error_message)[:4000] if error_message else None
        now_ist = get_now_ist()
        cur.execute("""
            INSERT INTO scheduler_configurations
                (job_name, title, schedule_time, frequency, is_active, last_run_at, last_run_status, last_run_duration_sec, error_message, next_run_at, updated_at)
            VALUES (%s, 'Dashboard Key Metrics & Analytics Sync', '19:00', 'daily', TRUE, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_run_at = %s,
                last_run_status = VALUES(last_run_status),
                last_run_duration_sec = VALUES(last_run_duration_sec),
                error_message = VALUES(error_message),
                next_run_at = COALESCE(VALUES(next_run_at), next_run_at),
                updated_at = %s;
        """, (str(job_name).strip(), now_ist, str(status), float(duration_sec), err_val, next_run_at, now_ist, now_ist, now_ist))
        conn.commit()
        return True
    except Exception as e:
        print(f"[Scheduler Service] Error recording run: {e}", flush=True)
        return False
    finally:
        if cur:
            try: cur.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# Dedicated Table for Dashboard Key Metrics (Right-side Panel)
# ─────────────────────────────────────────────────────────────────────────────
KEY_METRICS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_key_metrics` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `metric_key` VARCHAR(50) NOT NULL,
    `metric_label` VARCHAR(100) NOT NULL,
    `metric_value` VARCHAR(100) NOT NULL,
    `metric_subtext` VARCHAR(255) DEFAULT '',
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_session_metric` (`session_id`, `metric_key`),
    INDEX `idx_session` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_key_metrics_table_initialized = False

def ensure_key_metrics_table(conn):
    global _key_metrics_table_initialized
    if _key_metrics_table_initialized or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(KEY_METRICS_TABLE_SQL)
        conn.commit()
        cur.close()
        _key_metrics_table_initialized = True
    except Exception as e:
        print(f"[Key Metrics Table] init note: {e}", flush=True)

def get_dashboard_key_metrics(conn, session_id: str):
    if not conn or not session_id:
        return None
    try:
        ensure_key_metrics_table(conn)
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("""
            SELECT metric_key, metric_label, metric_value, metric_subtext, last_synced_at
            FROM `dashboard_key_metrics`
            WHERE session_id = %s
        """, (str(session_id).strip(),))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return None

        result = {}
        last_sync = None
        for r in rows:
            if isinstance(r, dict):
                k = r.get("metric_key")
                lbl = r.get("metric_label")
                val = r.get("metric_value")
                sub = r.get("metric_subtext")
                sync = r.get("last_synced_at")
            else:
                k, lbl, val, sub, sync = r[0], r[1], r[2], r[3], r[4]
            if k:
                result[k] = {"label": lbl or "", "value": val or "N/A", "subtext": sub or ""}
                if sync: last_sync = sync

        if len(result) >= 5:
            return {"data": result, "_last_synced_at": str(last_sync) if last_sync else None}
        return None
    except Exception as e:
        print(f"[Key Metrics Table] Read error: {e}", flush=True)
        return None

def save_dashboard_key_metrics(conn, session_id: str, metrics_data: dict):
    if not conn or not session_id or not metrics_data:
        return False
    try:
        ensure_key_metrics_table(conn)
        cur = conn.cursor()
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')

        # Clean delete existing rows for this session so fresh rows have updated created_at and last_synced_at
        cur.execute("DELETE FROM `dashboard_key_metrics` WHERE session_id = %s", (str(session_id).strip(),))

        sql = """
            INSERT INTO `dashboard_key_metrics`
                (`session_id`, `metric_key`, `metric_label`, `metric_value`, `metric_subtext`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        batch = []
        for k, v in metrics_data.items():
            if isinstance(v, dict):
                lbl = str(v.get("label", ""))[:100]
                val = str(v.get("value", ""))[:100]
                sub = str(v.get("subtext", ""))[:255]
                batch.append((str(session_id).strip(), str(k)[:50], lbl, val, sub, ist_now, ist_now))

        if batch:
            cur.executemany(sql, batch)
            conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Key Metrics Table] Save error: {e}", flush=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 1. Dedicated Table for Sales by Zone (Pie Chart)
# ─────────────────────────────────────────────────────────────────────────────
SALES_BY_ZONE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_sales_by_zone` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `zone_name` VARCHAR(100) NOT NULL,
    `sales_value` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `percentage` DECIMAL(5, 2) NOT NULL DEFAULT 0.00,
    `quantity` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session_zone` (`session_id`, `zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_sales_by_zone_table_init = False

def ensure_sales_by_zone_table(conn):
    global _sales_by_zone_table_init
    if _sales_by_zone_table_init or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(SALES_BY_ZONE_TABLE_SQL)
        conn.commit()
        cur.close()
        _sales_by_zone_table_init = True
    except Exception as e:
        print(f"[Sales by Zone Table] init: {e}", flush=True)

def get_sales_by_zone_table_data(conn, session_id: str, zone_filter: str = "ALL"):
    if not conn or not session_id:
        return None
    try:
        ensure_sales_by_zone_table(conn)
        z_code = str(zone_filter).strip().upper() if zone_filter and str(zone_filter).lower() != "all" else "ALL"
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("""
            SELECT zone_code, zone_name, sales_value, percentage, quantity, last_synced_at
            FROM `dashboard_sales_by_zone`
            WHERE session_id = %s AND zone_code = %s
            ORDER BY sales_value DESC
        """, (str(session_id).strip(), z_code))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None
        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        res_list = []
        for r in rows:
            if isinstance(r, dict):
                zc, zn, sv, pct, qty = r.get("zone_code"), r.get("zone_name"), float(r.get("sales_value") or 0), float(r.get("percentage") or 0), float(r.get("quantity") or 0)
            else:
                zc, zn, sv, pct, qty = r[0], r[1], float(r[2] or 0), float(r[3] or 0), float(r[4] or 0)
            res_list.append({
                "zone": zc,
                "name": zn,
                "sales_value": sv,
                "value": sv,
                "percentage": pct,
                "quantity": qty
            })
        return {"data": res_list, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[Sales by Zone Table] Read error: {e}", flush=True)
        return None

def save_sales_by_zone_table_data(conn, session_id: str, zone_filter: str, data_list: list):
    if not conn or not session_id or not data_list:
        return False
    try:
        ensure_sales_by_zone_table(conn)
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        z_code = str(zone_filter).strip().upper() if zone_filter and str(zone_filter).lower() != "all" else "ALL"
        cur = conn.cursor()
        cur.execute("DELETE FROM `dashboard_sales_by_zone` WHERE session_id = %s AND zone_code = %s", (str(session_id).strip(), z_code))
        sql = """
            INSERT INTO `dashboard_sales_by_zone`
                (`session_id`, `zone_code`, `zone_name`, `sales_value`, `percentage`, `quantity`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        batch = []
        for item in data_list:
            if isinstance(item, dict):
                zc = z_code
                zn = str(item.get("name") or item.get("zone") or "")[:100]
                sv = float(item.get("sales_value") or item.get("value") or 0.0)
                pct = float(item.get("percentage") or 0.0)
                qty = float(item.get("quantity") or 0.0)
                batch.append((str(session_id).strip(), zc, zn, sv, pct, qty, ist_now, ist_now))
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Sales by Zone Table] Save error: {e}", flush=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 2. Dedicated Table for Top 10 Tyre Types by Sales
# ─────────────────────────────────────────────────────────────────────────────
TYRE_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_tyre_sales_top_10` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `tyre_type` VARCHAR(100) NOT NULL,
    `sales_value` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_tyre_sales_table_init = False

def ensure_tyre_sales_table(conn):
    global _tyre_sales_table_init
    if _tyre_sales_table_init or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(TYRE_SALES_TABLE_SQL)
        conn.commit()
        cur.close()
        _tyre_sales_table_init = True
    except Exception as e:
        print(f"[Tyre Sales Table] init: {e}", flush=True)

def get_tyre_sales_table_data(conn, session_id: str):
    if not conn or not session_id:
        return None
    try:
        ensure_tyre_sales_table(conn)
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("""
            SELECT tyre_type, sales_value, last_synced_at
            FROM `dashboard_tyre_sales_top_10`
            WHERE session_id = %s
            ORDER BY sales_value DESC
            LIMIT 10
        """, (str(session_id).strip(),))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None
        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        res_list = []
        for r in rows:
            if isinstance(r, dict):
                tt, sv = str(r.get("tyre_type") or ""), float(r.get("sales_value") or 0)
            else:
                tt, sv = str(r[0] or ""), float(r[1] or 0)
            res_list.append({
                "vehicle_type": tt,
                "category": tt,
                "tyre_type": tt,
                "name": tt,
                "sales_value": sv
            })
        return {"data": res_list, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[Tyre Sales Table] Read error: {e}", flush=True)
        return None

def save_tyre_sales_table_data(conn, session_id: str, data_list: list):
    if not conn or not session_id or not data_list:
        return False
    try:
        ensure_tyre_sales_table(conn)
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.cursor()
        cur.execute("DELETE FROM `dashboard_tyre_sales_top_10` WHERE session_id = %s", (str(session_id).strip(),))
        sql = """
            INSERT INTO `dashboard_tyre_sales_top_10`
                (`session_id`, `tyre_type`, `sales_value`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s)
        """
        batch = []
        for item in data_list:
            if isinstance(item, dict):
                tt = str(item.get("vehicle_type") or item.get("tyre_type") or item.get("name") or "")[:100]
                sv = float(item.get("sales_value") or item.get("value") or 0.0)
                batch.append((str(session_id).strip(), tt, sv, ist_now, ist_now))
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Tyre Sales Table] Save error: {e}", flush=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dedicated Table for Category Sales
# ─────────────────────────────────────────────────────────────────────────────
CATEGORY_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_category_sales` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `category_name` VARCHAR(100) NOT NULL,
    `total_sales_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session_zone` (`session_id`, `zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_category_sales_table_init = False

def ensure_category_sales_table(conn):
    global _category_sales_table_init
    if _category_sales_table_init or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(CATEGORY_SALES_TABLE_SQL)
        conn.commit()
        cur.close()
        _category_sales_table_init = True
    except Exception as e:
        print(f"[Category Sales Table] init: {e}", flush=True)

def get_category_sales_table_data(conn, session_id: str, zone: str = None):
    if not conn or not session_id:
        return None
    try:
        ensure_category_sales_table(conn)
        z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("""
            SELECT category_name, total_sales_cr, last_synced_at
            FROM `dashboard_category_sales`
            WHERE session_id = %s AND zone_code = %s
            ORDER BY total_sales_cr DESC
        """, (str(session_id).strip(), z_code))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None
        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        res_list = []
        for r in rows:
            if isinstance(r, dict):
                cn, cr = r.get("category_name"), float(r.get("total_sales_cr") or 0)
            else:
                cn, cr = r[0], float(r[1] or 0)
            res_list.append({
                "category": cn,
                "total_sales_cr": cr
            })
        return {"data": res_list, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[Category Sales Table] Read error: {e}", flush=True)
        return None

def save_category_sales_table_data(conn, session_id: str, zone: str, data_list: list):
    if not conn or not session_id or not data_list:
        return False
    try:
        ensure_category_sales_table(conn)
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = conn.cursor()
        cur.execute("DELETE FROM `dashboard_category_sales` WHERE session_id = %s AND zone_code = %s", (str(session_id).strip(), z_code))
        sql = """
            INSERT INTO `dashboard_category_sales`
                (`session_id`, `zone_code`, `category_name`, `total_sales_cr`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        batch = []
        for item in data_list:
            if isinstance(item, dict):
                cn = str(item.get("category") or item.get("CATEGORY") or item.get("title") or "")[:100]
                cr = float(item.get("total_sales_cr") or item.get("valCr") or 0.0)
                batch.append((str(session_id).strip(), z_code, cn, cr, ist_now, ist_now))
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Category Sales Table] Save error: {e}", flush=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 4. Dedicated Table for Actual Sales by Account Category
# ─────────────────────────────────────────────────────────────────────────────
ACCOUNT_CATEGORY_SALES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `dashboard_account_category_sales` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(100) NOT NULL,
    `zone_code` VARCHAR(20) NOT NULL DEFAULT 'ALL',
    `category_title` VARCHAR(100) NOT NULL,
    `sales_value_cr` DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    `last_synced_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_session_zone` (`session_id`, `zone_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_account_cat_sales_table_init = False

def ensure_account_category_sales_table(conn):
    global _account_cat_sales_table_init
    if _account_cat_sales_table_init or not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(ACCOUNT_CATEGORY_SALES_TABLE_SQL)
        conn.commit()
        cur.close()
        _account_cat_sales_table_init = True
    except Exception as e:
        print(f"[Account Category Sales Table] init: {e}", flush=True)

def get_account_category_sales_table_data(conn, session_id: str, zone: str = None):
    if not conn or not session_id:
        return None
    try:
        ensure_account_category_sales_table(conn)
        z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        cur.execute("""
            SELECT category_title, sales_value_cr, last_synced_at
            FROM `dashboard_account_category_sales`
            WHERE session_id = %s AND zone_code = %s
              AND category_title IS NOT NULL
              AND TRIM(category_title) <> ''
              AND LOWER(TRIM(category_title)) NOT IN ('account category', 'unknown', '0', 'null', 'none')
            ORDER BY sales_value_cr DESC
        """, (str(session_id).strip(), z_code))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None
        last_sync = rows[0].get("last_synced_at") if isinstance(rows[0], dict) else None
        res_list = []
        for r in rows:
            if isinstance(r, dict):
                ct, cr = r.get("category_title"), float(r.get("sales_value_cr") or 0)
            else:
                ct, cr = r[0], float(r[1] or 0)
            res_list.append({
                "title": ct,
                "valCr": cr,
                "value": f"₹{cr} Cr"
            })
        return {"data": res_list, "_last_synced_at": str(last_sync) if last_sync else None}
    except Exception as e:
        print(f"[Account Category Sales Table] Read error: {e}", flush=True)
        return None

def save_account_category_sales_table_data(conn, session_id: str, zone: str, data_list: list):
    if not conn or not session_id or not data_list:
        return False
    try:
        ensure_account_category_sales_table(conn)
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S')
        z_code = str(zone).strip() if zone and str(zone).strip().lower() != "all" else "ALL"
        cur = conn.cursor()
        cur.execute("DELETE FROM `dashboard_account_category_sales` WHERE session_id = %s AND zone_code = %s", (str(session_id).strip(), z_code))
        sql = """
            INSERT INTO `dashboard_account_category_sales`
                (`session_id`, `zone_code`, `category_title`, `sales_value_cr`, `last_synced_at`, `created_at`)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        batch = []
        for item in data_list:
            if isinstance(item, dict):
                ct = str(item.get("title") or item.get("TITLE") or item.get("label") or "").strip()[:100]
                if not ct or ct.lower() in ("account category", "unknown", "0", "null", "none"):
                    continue
                cr = float(item.get("valCr") or item.get("value") or 0.0)
                batch.append((str(session_id).strip(), z_code, ct, cr, ist_now, ist_now))
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[Account Category Sales Table] Save error: {e}", flush=True)
        return False
