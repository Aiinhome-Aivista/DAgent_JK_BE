import os
import json
import ftplib
import threading
import uuid
from datetime import datetime
from flask import request, jsonify, Response, stream_with_context
# pyrefly: ignore [missing-import]
from apscheduler.schedulers.background import BackgroundScheduler
# pyrefly: ignore [missing-import]
from apscheduler.triggers.cron import CronTrigger
# pyrefly: ignore [missing-import]
from apscheduler.triggers.interval import IntervalTrigger
from database.config import UPLOAD_FOLDER, MYSQL_CONFIG
from database.csv_processor import process_csv_job
from database.sql_processor import process_sql_job

# ─────────────────────────────────────────────────────────────────
# In-memory store for live progress events (per job_id)
# ─────────────────────────────────────────────────────────────────
_ftp_progress: dict[str, list[dict]] = {}
_ftp_progress_lock = threading.Lock()

# Global APScheduler instance (shared across all FTP jobs)
_scheduler = BackgroundScheduler(daemon=True)
_scheduler.start()


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _push_event(job_id: str, event: dict):
    """Append an SSE event dict to the job's progress queue."""
    with _ftp_progress_lock:
        if job_id not in _ftp_progress:
            _ftp_progress[job_id] = []
        _ftp_progress[job_id].append(event)


def _run_ftp_fetch(job_id: str, credential: dict, user_db: str, get_db_connection_fn):
    """
    Core FTP fetch logic.  Runs in a background thread / scheduler job.
    Emits progress events that the SSE endpoint streams to the client.
    Saves fetched file metadata into the user's workspace DB.
    """
    host       = credential.get("host", "")
    port       = int(credential.get("port", 21))
    username   = credential.get("username", "")
    password   = credential.get("password", "")
    remote_dir = credential.get("remote_dir", "/")
    passive    = credential.get("passive_mode", True)

    _push_event(job_id, {"type": "start", "message": f"Connecting to FTP {host}:{port} …", "ts": _now()})

    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(username, password)
        if passive:
            ftp.set_pasv(True)
        _push_event(job_id, {"type": "connected", "message": "Connection established.", "ts": _now()})

        # List files in remote directory
        ftp.cwd(remote_dir)
        file_list: list[dict] = []
        ftp.retrlines("LIST", lambda line: file_list.append(_parse_ftp_list_line(line)))
        files = [f for f in file_list if f.get("name")]

        _push_event(job_id, {
            "type": "listing",
            "message": f"Found {len(files)} item(s) in {remote_dir}.",
            "count": len(files),
            "ts": _now()
        })

        fetched, failed = [], []
        csv_files = []
        sql_files = []
        
        for idx, file_info in enumerate(files, 1):
            fname = file_info["name"]
            progress_pct = int(idx / max(len(files), 1) * 100)

            _push_event(job_id, {
                "type": "progress",
                "message": f"Fetching {fname} …",
                "file": fname,
                "current": idx,
                "total": len(files),
                "percent": progress_pct,
                "ts": _now()
            })

            try:
                # Save downloaded file to the UPLOAD_FOLDER
                local_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_{fname}")
                
                with open(local_path, "wb") as f:
                    ftp.retrbinary(f"RETR {fname}", f.write)
                
                size_bytes = os.path.getsize(local_path)

                fetched.append({
                    "name": fname,
                    "size": size_bytes,
                    "remote_dir": remote_dir,
                    "fetched_at": _now()
                })
                
                if fname.lower().endswith(".csv"):
                    csv_files.append(local_path)
                elif fname.lower().endswith(".sql"):
                    sql_files.append(local_path)

                _push_event(job_id, {
                    "type": "file_done",
                    "file": fname,
                    "size": size_bytes,
                    "percent": progress_pct,
                    "ts": _now()
                })

            except Exception as file_err:
                failed.append({"name": fname, "error": str(file_err)})
                _push_event(job_id, {
                    "type": "file_error",
                    "file": fname,
                    "error": str(file_err),
                    "ts": _now()
                })

        ftp.quit()

        # Post-fetch processing: schedule background jobs for CSV and SQL
        if user_db:
            db_user = MYSQL_CONFIG.get("user")
            db_pass = MYSQL_CONFIG.get("password")
            db_host = MYSQL_CONFIG.get("host")
            db_port = int(MYSQL_CONFIG.get("port", 3306))

            if csv_files:
                _scheduler.add_job(
                    func=process_csv_job,
                    args=[csv_files, user_db, db_host, db_user, db_pass, db_port],
                    kwargs={"trigger_source": "ftp_connector"},
                    trigger='date',
                    id=str(uuid.uuid4()),
                    replace_existing=True
                )
                
            if sql_files:
                _scheduler.add_job(
                    func=process_sql_job,
                    args=[sql_files, user_db, db_host, db_user, db_pass, db_port],
                    kwargs={"trigger_source": "ftp_connector"},
                    trigger='date',
                    id=str(uuid.uuid4()),
                    replace_existing=True
                )

        # Persist metadata to DB
        if fetched:
            try:
                conn = get_db_connection_fn()
                if conn:
                    cursor = conn.cursor()
                    for f in fetched:
                        cursor.execute(
                            """INSERT INTO ftp_fetch_log
                               (job_id, file_name, remote_dir, file_size, fetched_at)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (job_id, f["name"], f["remote_dir"], f["size"], f["fetched_at"])
                        )
                    conn.commit()
                    cursor.close()
                    conn.close()
            except Exception as db_err:
                _push_event(job_id, {"type": "warning", "message": f"DB log error: {db_err}", "ts": _now()})

        _push_event(job_id, {
            "type": "done",
            "message": f"Fetch complete. {len(fetched)} file(s) downloaded, {len(failed)} failed.",
            "fetched": fetched,
            "failed": failed,
            "ts": _now()
        })

    except Exception as exc:
        _push_event(job_id, {"type": "error", "message": str(exc), "ts": _now()})


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _parse_ftp_list_line(line: str) -> dict:
    """Parse a single line from an FTP LIST response into a dict."""
    parts = line.split()
    if len(parts) >= 9:
        name = " ".join(parts[8:])
        size = parts[4] if parts[4].isdigit() else "0"
        is_dir = parts[0].startswith("d")
        return {"name": name, "size": int(size), "is_dir": is_dir, "raw": line}
    return {"name": "", "raw": line}


# ─────────────────────────────────────────────────────────────────
# 1.  POST /ftp/connect  — test connection & save credential
# ─────────────────────────────────────────────────────────────────

def ftp_connect_controller(get_db_connection):
    """
    Test FTP credentials and, on success, persist them.
    Body: { user_id, session_id, name, host, port, username, password,
            remote_dir, passive_mode }
    """
    data = request.json or {}

    user_id    = data.get("user_id")
    session_id = data.get("session_id")
    conn_name  = data.get("name", "FTP Connector")
    host       = data.get("host", "")
    port       = int(data.get("port", 21))
    username   = data.get("username", "")
    password   = data.get("password", "")
    remote_dir = data.get("remote_dir", "/")
    passive    = data.get("passive_mode", True)

    if not user_id or not session_id:
        return jsonify({"status": "error", "message": "user_id and session_id are required"}), 400
    if not host:
        return jsonify({"status": "error", "message": "FTP host is required"}), 400

    # --- Test connection ---
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(bool(passive))
        ftp.cwd(remote_dir)
        ftp.quit()
    except Exception as exc:
        return jsonify({"status": "error", "message": f"FTP connection failed: {exc}"}), 400

    # --- Persist credential ---
    try:
        db_conn = get_db_connection()
        if not db_conn:
            return jsonify({"status": "error", "message": "Cannot connect to database"}), 500

        cursor = db_conn.cursor()

        # connection_history row
        cursor.execute(
            """INSERT INTO connection_history
               (user_id, session_id, connection_name, db_type, target_host, status, error_message)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user_id, session_id, conn_name, "ftp", host, "success", "")
        )

        # database_credential row
        cred = {
            "host": host, "port": port,
            "username": username, "password": password,
            "remote_dir": remote_dir, "passive_mode": passive
        }
        cursor.execute(
            """INSERT INTO database_credential
               (user_id, session_id, db_type, credential)
               VALUES (%s, %s, %s, %s)""",
            (user_id, session_id, "ftp", json.dumps(cred))
        )

        # Ensure ftp_fetch_log table exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ftp_fetch_log (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                job_id        VARCHAR(64)  NOT NULL,
                file_name     VARCHAR(512) NOT NULL,
                remote_dir    VARCHAR(512),
                file_size     BIGINT DEFAULT 0,
                fetched_at    DATETIME,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_job (job_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        # Ensure ftp_schedules table exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ftp_schedules (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                user_id       INT          NOT NULL,
                session_id    VARCHAR(128) NOT NULL,
                connection_id INT,
                scheduler_id  VARCHAR(64)  NOT NULL,
                schedule_type VARCHAR(32)  NOT NULL,
                schedule_value VARCHAR(64) NOT NULL,
                is_active     TINYINT(1)   DEFAULT 1,
                created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user_session (user_id, session_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        db_conn.commit()
        cursor.close()
        db_conn.close()

    except Exception as db_err:
        return jsonify({"status": "error", "message": f"Database error: {db_err}"}), 500

    return jsonify({
        "status": "success",
        "message": f"FTP connector '{conn_name}' created successfully.",
        "session_id": session_id
    }), 200


# ─────────────────────────────────────────────────────────────────
# 2.  POST /ftp/fetch  — trigger an immediate fetch job
# ─────────────────────────────────────────────────────────────────

def ftp_fetch_controller(get_db_connection):
    """
    Start an async FTP fetch job.
    Body: { user_id, session_id, connection_id }
    Returns: { job_id } — poll /ftp/progress/<job_id> (SSE) for updates.
    """
    data = request.json or {}

    user_id       = data.get("user_id")
    session_id    = data.get("session_id")
    connection_id = data.get("connection_id")

    if not user_id or not session_id:
        return jsonify({"status": "error", "message": "user_id and session_id are required"}), 400

    # Load credential
    try:
        db_conn = get_db_connection()
        cursor  = db_conn.cursor(dictionary=True)
        cursor.execute(
            """SELECT credential FROM database_credential
               WHERE user_id=%s AND session_id=%s AND db_type='ftp'
               ORDER BY connection_id DESC LIMIT 1""",
            (user_id, session_id)
        )
        row = cursor.fetchone()
        cursor.close()
        db_conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": f"DB error: {e}"}), 500

    if not row:
        return jsonify({"status": "error", "message": "No FTP credential found for this session"}), 404

    credential = json.loads(row["credential"])
    job_id     = str(uuid.uuid4())

    # Fetch user_db (not strictly needed here, kept for future storage)
    user_db = None
    try:
        db_conn2 = get_db_connection()
        cursor2  = db_conn2.cursor(dictionary=True)
        cursor2.execute("SELECT new_user_db FROM users WHERE id=%s", (user_id,))
        urow = cursor2.fetchone()
        if urow:
            user_db = urow.get("new_user_db")
        cursor2.close()
        db_conn2.close()
    except Exception:
        pass

    # Run in background thread
    t = threading.Thread(
        target=_run_ftp_fetch,
        args=(job_id, credential, user_db, get_db_connection),
        daemon=True
    )
    t.start()

    return jsonify({"status": "success", "job_id": job_id, "message": "FTP fetch started"}), 202


# ─────────────────────────────────────────────────────────────────
# 3.  GET /ftp/progress/<job_id>  — SSE stream of progress events
# ─────────────────────────────────────────────────────────────────

def ftp_progress_controller(job_id: str):
    """
    Server-Sent Events endpoint.  Streams incremental progress events
    for the given job_id until a 'done' or 'error' event is emitted.
    """
    def event_stream():
        sent_index = 0
        import time
        for _ in range(600):          # max ~60 s (100 ms * 600)
            with _ftp_progress_lock:
                events = _ftp_progress.get(job_id, [])
                new_events = events[sent_index:]

            for ev in new_events:
                yield f"data: {json.dumps(ev)}\n\n"
                sent_index += 1
                if ev.get("type") in ("done", "error"):
                    # Clean up memory
                    with _ftp_progress_lock:
                        _ftp_progress.pop(job_id, None)
                    return

            time.sleep(0.1)

        yield f"data: {json.dumps({'type': 'timeout', 'message': 'Stream timeout'})}\n\n"

    return Response(
        stream_with_context(event_stream()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


# ─────────────────────────────────────────────────────────────────
# 4.  POST /ftp/schedule  — create / update a recurring schedule
# ─────────────────────────────────────────────────────────────────

def ftp_schedule_controller(get_db_connection):
    """
    Create or update a recurring FTP fetch schedule.
    Body: {
        user_id, session_id,
        schedule_type: 'interval' | 'cron',
        schedule_value: '30m' | '1h' | '6h' | '24h'  (interval)
                     or '0 */6 * * *'                  (cron)
    }
    """
    data          = request.json or {}
    user_id       = data.get("user_id")
    session_id    = data.get("session_id")
    sched_type    = data.get("schedule_type", "interval")   # 'interval' | 'cron'
    sched_value   = data.get("schedule_value", "1h")

    if not user_id or not session_id:
        return jsonify({"status": "error", "message": "user_id and session_id are required"}), 400

    # Load FTP credential
    try:
        db_conn = get_db_connection()
        cursor  = db_conn.cursor(dictionary=True)
        cursor.execute(
            """SELECT credential FROM database_credential
               WHERE user_id=%s AND session_id=%s AND db_type='ftp'
               ORDER BY connection_id DESC LIMIT 1""",
            (user_id, session_id)
        )
        row = cursor.fetchone()
        cursor.close()
        db_conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": f"DB error: {e}"}), 500

    if not row:
        return jsonify({"status": "error", "message": "No FTP credential found. Connect first."}), 404

    credential = json.loads(row["credential"])

    # Build APScheduler trigger
    try:
        if sched_type == "cron":
            trigger = CronTrigger.from_crontab(sched_value)
        else:
            # Parse human intervals: 30m, 1h, 6h, 24h
            minutes = _parse_interval_to_minutes(sched_value)
            trigger = IntervalTrigger(minutes=minutes)
    except Exception as te:
        return jsonify({"status": "error", "message": f"Invalid schedule: {te}"}), 400

    scheduler_id = f"ftp_{user_id}_{session_id}"

    # Remove existing job if present
    if _scheduler.get_job(scheduler_id):
        _scheduler.remove_job(scheduler_id)

    def scheduled_job():
        job_id = str(uuid.uuid4())
        _run_ftp_fetch(job_id, credential, None, get_db_connection)

    _scheduler.add_job(
        scheduled_job,
        trigger=trigger,
        id=scheduler_id,
        replace_existing=True,
        max_instances=1
    )

    # Persist schedule config
    try:
        db_conn = get_db_connection()
        cursor  = db_conn.cursor()

        # Ensure table exists (safe re-create)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ftp_schedules (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                user_id       INT          NOT NULL,
                session_id    VARCHAR(128) NOT NULL,
                connection_id INT,
                scheduler_id  VARCHAR(64)  NOT NULL,
                schedule_type VARCHAR(32)  NOT NULL,
                schedule_value VARCHAR(64) NOT NULL,
                is_active     TINYINT(1)   DEFAULT 1,
                created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user_session (user_id, session_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        cursor.execute(
            """INSERT INTO ftp_schedules
               (user_id, session_id, scheduler_id, schedule_type, schedule_value, is_active)
               VALUES (%s, %s, %s, %s, %s, 1)
               ON DUPLICATE KEY UPDATE
                 schedule_type=VALUES(schedule_type),
                 schedule_value=VALUES(schedule_value),
                 is_active=1""",
            (user_id, session_id, scheduler_id, sched_type, sched_value)
        )

        db_conn.commit()
        cursor.close()
        db_conn.close()
    except Exception as db_err:
        print(f"[FTP Schedule] DB persistence error: {db_err}")

    next_run = _scheduler.get_job(scheduler_id).next_run_time
    return jsonify({
        "status": "success",
        "message": f"FTP schedule set ({sched_type}: {sched_value})",
        "scheduler_id": scheduler_id,
        "next_run": str(next_run) if next_run else None
    }), 200


# ─────────────────────────────────────────────────────────────────
# 5.  DELETE /ftp/schedule  — remove a running schedule
# ─────────────────────────────────────────────────────────────────

def ftp_delete_schedule_controller(get_db_connection):
    data       = request.json or {}
    user_id    = data.get("user_id")
    session_id = data.get("session_id")

    if not user_id or not session_id:
        return jsonify({"status": "error", "message": "user_id and session_id are required"}), 400

    scheduler_id = f"ftp_{user_id}_{session_id}"
    if _scheduler.get_job(scheduler_id):
        _scheduler.remove_job(scheduler_id)

    try:
        db_conn = get_db_connection()
        cursor  = db_conn.cursor()
        cursor.execute(
            "UPDATE ftp_schedules SET is_active=0 WHERE user_id=%s AND session_id=%s",
            (user_id, session_id)
        )
        db_conn.commit()
        cursor.close()
        db_conn.close()
    except Exception:
        pass

    return jsonify({"status": "success", "message": "FTP schedule removed"}), 200


# ─────────────────────────────────────────────────────────────────
# 6.  GET /ftp/schedule  — get current schedule info
# ─────────────────────────────────────────────────────────────────

def ftp_get_schedule_controller(get_db_connection):
    user_id    = request.args.get("user_id")
    session_id = request.args.get("session_id")

    if not user_id or not session_id:
        return jsonify({"status": "error", "message": "user_id and session_id are required"}), 400

    scheduler_id = f"ftp_{user_id}_{session_id}"
    job = _scheduler.get_job(scheduler_id)

    result = {"has_schedule": False, "is_active": False, "next_run": None}

    if job:
        result["has_schedule"]    = True
        result["is_active"]       = True
        result["scheduler_id"]    = scheduler_id
        result["next_run"]        = str(job.next_run_time) if job.next_run_time else None

    # Also load persisted config
    try:
        db_conn = get_db_connection()
        cursor  = db_conn.cursor(dictionary=True)
        cursor.execute(
            """SELECT schedule_type, schedule_value, is_active, created_at
               FROM ftp_schedules
               WHERE user_id=%s AND session_id=%s
               ORDER BY id DESC LIMIT 1""",
            (user_id, session_id)
        )
        row = cursor.fetchone()
        cursor.close()
        db_conn.close()
        if row:
            result["schedule_type"]  = row["schedule_type"]
            result["schedule_value"] = row["schedule_value"]
            result["db_is_active"]   = bool(row["is_active"])
    except Exception:
        pass

    return jsonify({"status": "success", "schedule": result}), 200


# ─────────────────────────────────────────────────────────────────
# 7.  GET /ftp/fetch_log  — fetch history
# ─────────────────────────────────────────────────────────────────

def ftp_fetch_log_controller(get_db_connection):
    user_id    = request.args.get("user_id")
    session_id = request.args.get("session_id")
    limit      = int(request.args.get("limit", 50))

    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required"}), 400

    try:
        db_conn = get_db_connection()
        cursor  = db_conn.cursor(dictionary=True)
        cursor.execute(
            """SELECT * FROM ftp_fetch_log
               WHERE job_id IN (
                 SELECT DISTINCT job_id FROM ftp_fetch_log
               )
               ORDER BY id DESC LIMIT %s""",
            (limit,)
        )
        rows = cursor.fetchall()
        cursor.close()
        db_conn.close()

        for r in rows:
            if hasattr(r.get("fetched_at"), "isoformat"):
                r["fetched_at"] = r["fetched_at"].isoformat()
            if hasattr(r.get("created_at"), "isoformat"):
                r["created_at"] = r["created_at"].isoformat()

        return jsonify({"status": "success", "logs": rows, "total": len(rows)}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────

def _parse_interval_to_minutes(value: str) -> int:
    """Convert strings like '30m', '1h', '6h', '24h' to integer minutes."""
    value = value.strip().lower()
    if value.endswith("m"):
        return int(value[:-1])
    if value.endswith("h"):
        return int(value[:-1]) * 60
    if value.endswith("d"):
        return int(value[:-1]) * 1440
    return int(value)   # assume already minutes
