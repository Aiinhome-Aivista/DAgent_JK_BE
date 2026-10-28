import json
import time
import mysql.connector
from database.config import MYSQL_CONFIG


# ─────────────────────────────────────────────────────────────
# LOW-LEVEL: raw connection helper
# ─────────────────────────────────────────────────────────────

def _get_conn():
    return mysql.connector.connect(**MYSQL_CONFIG)


# ─────────────────────────────────────────────────────────────
# LOG A NEW ACTION  (call this from inside your agent/tools)
# Returns the auto-generated `id` of the new row.
# ─────────────────────────────────────────────────────────────

def log_action(
    session_id,
    user_id,
    action_name,
    action_type,
    *,
    workspace_id    = None,
    action_input    = None,   # dict  → stored as JSON
    action_output   = None,   # str
    tool_name       = None,
    tool_args       = None,   # dict  → stored as JSON
    tool_result     = None,   # str
    status          = "pending",
    error_message   = None,
    execution_time_ms = None,
    step_index      = None,
    agent_name      = None,
    model_used      = None,
):
    """
    Insert a new row into `agent_actions`.
    Returns the inserted row id, or None on failure.
    """
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor()

        sql = """
            INSERT INTO agent_actions (
                session_id, user_id, workspace_id,
                action_name, action_type, action_input, action_output,
                tool_name, tool_args, tool_result,
                status, error_message, execution_time_ms,
                step_index, agent_name, model_used
            ) VALUES (
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s
            )
        """
        params = (
            session_id,
            user_id,
            workspace_id,
            action_name,
            action_type,
            json.dumps(action_input)  if action_input  is not None else None,
            action_output,
            tool_name,
            json.dumps(tool_args)     if tool_args     is not None else None,
            tool_result,
            status,
            error_message,
            execution_time_ms,
            step_index,
            agent_name,
            model_used,
        )
        cursor.execute(sql, params)
        conn.commit()
        return cursor.lastrowid

    except Exception as e:
        print(f"[AgentActionService] log_action error: {e}")
        return None

    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


# ─────────────────────────────────────────────────────────────
# UPDATE STATUS / OUTPUT  (after a tool finishes)
# ─────────────────────────────────────────────────────────────

def update_action(
    action_id,
    *,
    status            = None,
    action_output     = None,
    tool_result       = None,
    error_message     = None,
    execution_time_ms = None,
):
    """
    Patch an existing agent_actions row by id.
    Only non-None kwargs are applied.
    """
    conn = cursor = None
    try:
        fields, vals = [], []
        if status            is not None: fields.append("status = %s");            vals.append(status)
        if action_output     is not None: fields.append("action_output = %s");     vals.append(action_output)
        if tool_result       is not None: fields.append("tool_result = %s");       vals.append(tool_result)
        if error_message     is not None: fields.append("error_message = %s");     vals.append(error_message)
        if execution_time_ms is not None: fields.append("execution_time_ms = %s"); vals.append(execution_time_ms)

        if not fields:
            return  # nothing to update

        vals.append(action_id)
        sql = f"UPDATE agent_actions SET {', '.join(fields)} WHERE id = %s"

        conn   = _get_conn()
        cursor = conn.cursor()
        cursor.execute(sql, vals)
        conn.commit()

    except Exception as e:
        print(f"[AgentActionService] update_action error: {e}")

    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


# ─────────────────────────────────────────────────────────────
# CONTEXT MANAGER — wraps a single step automatically
# Usage:
#   with track_action(session_id, user_id, "query_db", "tool_call",
#                     tool_name="sql_executor") as ctx:
#       result = run_my_tool(...)
#       ctx["output"] = result           # optional
# ─────────────────────────────────────────────────────────────

class track_action:
    """
    Context manager that logs an action on entry (status=running)
    and updates it on exit (status=success or failed).

    Example
    -------
    with track_action(session_id, user_id, "sql_query", "tool_call",
                      tool_name="sql_executor",
                      tool_args={"sql": "SELECT ..."},
                      agent_name="analysis_agent") as ctx:
        rows = engine.execute(ctx["tool_args"]["sql"])
        ctx["output"] = str(rows)
    """

    def __init__(self, session_id, user_id, action_name, action_type, **kwargs):
        self.session_id   = session_id
        self.user_id      = user_id
        self.action_name  = action_name
        self.action_type  = action_type
        self.kwargs       = kwargs
        self.action_id    = None
        self._start       = None
        self.ctx          = {"output": None}   # caller can set ctx["output"]

    def __enter__(self):
        self._start = time.time()
        self.action_id = log_action(
            self.session_id,
            self.user_id,
            self.action_name,
            self.action_type,
            status="running",
            **self.kwargs,
        )
        self.ctx["action_id"] = self.action_id
        return self.ctx

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed_ms = int((time.time() - self._start) * 1000)

        if exc_type is None:
            update_action(
                self.action_id,
                status="success",
                action_output=self.ctx.get("output"),
                execution_time_ms=elapsed_ms,
            )
        else:
            update_action(
                self.action_id,
                status="failed",
                error_message=str(exc_val),
                execution_time_ms=elapsed_ms,
            )
        return False   # do not suppress exceptions


# ─────────────────────────────────────────────────────────────
# FETCH HELPERS  (used by the controller / API)
# ─────────────────────────────────────────────────────────────

def get_actions_by_session(session_id, limit=100, offset=0):
    """Return all actions for a session, newest first."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT * FROM agent_actions
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (session_id, limit, offset),
        )
        rows = cursor.fetchall()
        return _serialize_rows(rows)
    except Exception as e:
        print(f"[AgentActionService] get_actions_by_session error: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


def get_actions_by_user(user_id, limit=100, offset=0):
    """Return all actions for a user across all sessions, newest first."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT * FROM agent_actions
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (user_id, limit, offset),
        )
        rows = cursor.fetchall()
        return _serialize_rows(rows)
    except Exception as e:
        print(f"[AgentActionService] get_actions_by_user error: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


def get_action_by_id(action_id):
    """Return a single action row by primary key."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM agent_actions WHERE id = %s", (action_id,))
        row = cursor.fetchone()
        if row:
            return _serialize_rows([row])[0]
        return None
    except Exception as e:
        print(f"[AgentActionService] get_action_by_id error: {e}")
        return None
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


def delete_actions_by_session(session_id):
    """Hard-delete all action rows for a given session."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agent_actions WHERE session_id = %s", (session_id,))
        affected = cursor.rowcount
        conn.commit()
        return affected
    except Exception as e:
        print(f"[AgentActionService] delete_actions_by_session error: {e}")
        return 0
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


def get_pending_approvals(session_id):
    """Return all actions with status='requires_approval' for a session, newest first."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT * FROM agent_actions
            WHERE session_id = %s AND status = 'requires_approval'
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (session_id,),
        )
        rows = cursor.fetchall()
        return _serialize_rows(rows)
    except Exception as e:
        print(f"[AgentActionService] get_pending_approvals error: {e}")
        return []
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _serialize_rows(rows):
    """Convert datetime objects and parse JSON columns for safe JSON output."""
    out = []
    for row in rows:
        r = dict(row)
        # Parse JSON columns back to dicts
        for col in ("action_input", "tool_args"):
            if isinstance(r.get(col), str):
                try:
                    r[col] = json.loads(r[col])
                except Exception:
                    pass
        # Serialize datetime → ISO string
        for col in ("created_at", "updated_at"):
            if r.get(col) and hasattr(r[col], "strftime"):
                r[col] = r[col].strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(r)
    return out
