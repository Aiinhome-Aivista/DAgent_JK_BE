import mysql.connector
from flask import jsonify
from database.config import MYSQL_CONFIG  # Adjust import based on your folder structure

def fetch_successful_sessions():
    """
    Connects to DB, fetches successful sessions, and returns a Flask JSON response directly.
    """
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor()

        query = """
            SELECT SESSION_NAME 
            FROM tracker 
            WHERE SESSION_STATUS = 'Success'
            AND DATA_TYPE_ANALYZER = 'Done'
            AND VISUALIZATION = 'Done'
            AND INSIGHTS = 'Done';
        """

        cursor.execute(query)
        results = cursor.fetchall()
        
        # Flatten list
        session_names = [row[0] for row in results]
        
        #  Return the JSON response directly
        return jsonify({"success": True, "data": session_names})

    except mysql.connector.Error as err:
        print(f"Database Error: {err}")
        return jsonify({"success": False, "message": str(err)}), 500

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()