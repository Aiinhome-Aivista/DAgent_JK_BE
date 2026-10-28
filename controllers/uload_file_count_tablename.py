import os
import pandas as pd
from flask_cors import CORS
from flask import request, jsonify
import json
import traceback 
import mysql.connector
from mysql.connector import Error
from database.config import MYSQL_CONFIG

 

# ---------------- DATABASE UTILS ----------------
def get_connection():
    """Create and return a MySQL connection."""
    return mysql.connector.connect(**MYSQL_CONFIG)

def upsert_tracker(session_name, file_names):
    """Upsert session_name and tables_name into tracker table."""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Ensure tracker table exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracker (
                session_name VARCHAR(255) PRIMARY KEY,
                tables_name TEXT
            )
        """)

        # Upsert logic
        sql = """
        INSERT INTO tracker (session_name, tables_name)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE tables_name = VALUES(tables_name)
        """
        cursor.execute(sql, (session_name, file_names))
        conn.commit()
    except Error as e:
        print(f"MySQL Error: {e}")
        raise
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

# ---------------- MAIN API ----------------

def upload_files_count_controller():
    try:
        session_name = request.form.get("session_name")
        if not session_name:
            return jsonify({"error": "Missing session_name"}), 400

        if "files" not in request.files:
            return jsonify({"error": "No files uploaded"}), 400

        files = request.files.getlist("files")

        file_names = []
        columns_summary = {}

        for file in files:
            file_name = file.filename
            file_names.append(file_name)

            # Read file with pandas to get total columns
            if file_name.endswith((".xlsx", ".xls")):
                df = pd.read_excel(file)
            elif file_name.endswith(".csv"):
                df = pd.read_csv(file)
            else:
                columns_summary[file_name] = "Unsupported file type"
                continue

            columns_summary[file_name] = len(df.columns)

        # Join all file names with commas
        file_names_str = ", ".join(file_names)

        # Upsert into tracker table
        upsert_tracker(session_name, file_names_str)

        return jsonify({
            "session_name": session_name,
            "uploaded_files": file_names,
            "columns_summary": columns_summary
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500