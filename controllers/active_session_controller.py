import json
import mysql.connector
from flask import request, jsonify
from database.config import MYSQL_CONFIG

def update_active_sessions_controller():
    """
    Saves the list of active session names to the database (app_config table).
    This allows the Voice Assistant to know which sessions are currently active.
    """
    try:
        data = request.get_json(silent=True) or {}
        active_sessions = data.get('active_sessions', [])
        
        # Ensure it's a list
        if not isinstance(active_sessions, list):
            return jsonify({"status": "failed", "message": "Invalid format. 'active_sessions' must be a list."}), 400

        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor()
        
        # Convert list to JSON string for storage
        json_val = json.dumps(active_sessions)
        
        # Upsert: Insert new row or update if 'active_sessions' key exists
        sql = """
        INSERT INTO app_config (config_key, config_value) 
        VALUES ('active_sessions', %s) 
        ON DUPLICATE KEY UPDATE config_value = VALUES(config_value)
        """
        
        cursor.execute(sql, (json_val,))
        conn.commit()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            "status": "success", 
            "message": "Active sessions updated successfully.",
            "active_sessions": active_sessions
        }), 200

    except mysql.connector.Error as err:
        print(f"Database Error: {err}")
        return jsonify({"status": "failed", "message": str(err)}), 500
    except Exception as e:
        print(f"Server Error: {e}")
        return jsonify({"status": "failed", "message": "Internal Server Error"}), 500