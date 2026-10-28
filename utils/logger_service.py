import mysql.connector
from database.config import MYSQL_CONFIG

def log_system_error(session_id=None, module_name="system", error_msg="", error_type="error"):
    """
    Safely logs any failure (timeouts, SQL execution errors, LLM crashes, analysis errors)
    into the `error_logs` table.
    Guaranteed fail-safe: will never raise an exception or crash the caller.
    """
    conn = None
    cursor = None
    try:
        formatted_error = f"[{str(error_type).upper()}] {str(error_msg)}"
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO error_logs (session_name, module_name, error)
            VALUES (%s, %s, %s)
        """, (str(session_id or 'system'), str(module_name), formatted_error))
        conn.commit()
    except Exception as ex:
        print(f"[LoggerService Error] Could not write to error_logs: {ex}")
    finally:
        if cursor:
            try: cursor.close()
            except: pass
        if conn:
            try: conn.close()
            except: pass
