# controllers/sheet_scan_controller.py
#
# POST /sheet/scan
#
# User provides a Google Sheet public URL.
# This controller:
#   1. Reads the Google Sheet via CSV export
#   2. Detects all columns dynamically
#   3. Creates a NEW MySQL table named  sheet_<user_id>_<timestamp>
#   4. Inserts all rows into that table
#   5. Saves scan metadata (user_id, sheet_url, table_name) to `sheet_scans` table
#
# The returned `table_name` is used by /sheet/describe and /sheet/chat
#
# ─────────────────────────────────────────────────────────────────

import re
import hashlib
import requests
import pandas as pd
import mysql.connector
from io import StringIO
from datetime import datetime
from flask import request, jsonify
from database.config import MYSQL_CONFIG

# ── MySQL reserved words we must quote ──
MYSQL_RESERVED = {
    "select","from","where","table","index","key","group","order","by",
    "limit","offset","join","on","as","and","or","not","in","is","null",
    "create","drop","insert","update","delete","into","values","column",
    "database","schema","primary","foreign","unique","default","int",
    "varchar","text","datetime","timestamp","float","double","date","time"
}

def _safe_col(name: str) -> str:
    """Sanitize a column name for safe use in MySQL."""
    name = str(name).strip()
    name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    if not name:
        name = "col"
    if name[0].isdigit():
        name = "c_" + name
    if name.lower() in MYSQL_RESERVED:
        name = name + "_col"
    return name[:60]


def _sheet_url_to_csv(sheet_url: str) -> str:
    """
    Converts any Google Sheet URL to its CSV export URL.
    Supports:
      - https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0
      - https://docs.google.com/spreadsheets/d/<ID>/pub?...
    """
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', sheet_url)
    if not match:
        raise ValueError("Could not extract Sheet ID from URL. Make sure it's a valid Google Sheets URL.")

    sheet_id = match.group(1)

    # Extract gid if present
    gid_match = re.search(r'gid=(\d+)', sheet_url)
    gid = gid_match.group(1) if gid_match else "0"

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _fetch_sheet_as_dataframe(sheet_url: str) -> pd.DataFrame:
    """Downloads the Google Sheet as CSV and returns a DataFrame."""
    csv_url = _sheet_url_to_csv(sheet_url)
    resp = requests.get(csv_url, timeout=20)
    if resp.status_code != 200:
        raise ValueError(
            f"Failed to fetch sheet (HTTP {resp.status_code}). "
            "Make sure the sheet is shared as 'Anyone with the link can view'."
        )
    df = pd.read_csv(StringIO(resp.text))
    df = df.dropna(how='all')       # drop fully empty rows
    df = df.fillna("")              # replace NaN with empty string
    return df


def _create_and_insert(conn, table_name: str, df: pd.DataFrame):
    """Dynamically creates a MySQL table and inserts all DataFrame rows."""
    cursor = conn.cursor()

    # Build column definitions — everything stored as TEXT for flexibility
    col_defs = ["`_row_id` INT AUTO_INCREMENT PRIMARY KEY"]
    safe_cols = []
    for col in df.columns:
        sc = _safe_col(col)
        safe_cols.append(sc)
        col_defs.append(f"`{sc}` TEXT")

    create_sql = (
        f"CREATE TABLE IF NOT EXISTS `{table_name}` "
        f"({', '.join(col_defs)}) "
        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cursor.execute(create_sql)

    # Insert rows
    if not df.empty:
        placeholders = ", ".join(["%s"] * len(safe_cols))
        col_names    = ", ".join([f"`{c}`" for c in safe_cols])
        insert_sql   = f"INSERT INTO `{table_name}` ({col_names}) VALUES ({placeholders})"
        rows = [tuple(str(row[col]) for col in df.columns) for _, row in df.iterrows()]
        cursor.executemany(insert_sql, rows)

    conn.commit()
    cursor.close()
    return safe_cols


def _ensure_sheet_scans_table(conn):
    """Creates the sheet_scans metadata table if it doesn't exist."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sheet_scans (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            scan_id      VARCHAR(64)  NOT NULL UNIQUE,
            user_id      VARCHAR(255) NOT NULL,
            sheet_url    TEXT         NOT NULL,
            table_name   VARCHAR(100) NOT NULL,
            columns_json TEXT,
            row_count    INT          DEFAULT 0,
            scanned_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_user_scans (user_id)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """)
    conn.commit()
    cursor.close()


# ═══════════════════════════════════════════════════════════════
# MAIN CONTROLLER
# ═══════════════════════════════════════════════════════════════

def sheet_scan_controller(get_connection_func):
    """
    POST /sheet/scan
    Body: {
      "user_id"   : "abc123",
      "sheet_url" : "https://docs.google.com/spreadsheets/d/XXXX/edit"
    }

    Response: {
      "status"     : "success",
      "scan_id"    : "...",
      "table_name" : "sheet_abc123_20260306143022",
      "columns"    : ["col1", "col2", ...],
      "row_count"  : 42
    }
    """
    data      = request.json or {}
    user_id   = (data.get("user_id")   or "").strip()
    sheet_url = (data.get("sheet_url") or "").strip()

    if not user_id:
        return jsonify({"status": "failed", "statusCode": 400,
                        "message": "Field 'user_id' is required."}), 400
    if not sheet_url:
        return jsonify({"status": "failed", "statusCode": 400,
                        "message": "Field 'sheet_url' is required."}), 400
    if "docs.google.com/spreadsheets" not in sheet_url:
        return jsonify({"status": "failed", "statusCode": 400,
                        "message": "Only Google Sheets URLs are supported."}), 400

    # 1. Fetch sheet as DataFrame
    try:
        df = _fetch_sheet_as_dataframe(sheet_url)
    except ValueError as e:
        return jsonify({"status": "failed", "statusCode": 400,
                        "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "statusCode": 500,
                        "message": f"Failed to read sheet: {str(e)}"}), 500

    if df.empty:
        return jsonify({"status": "failed", "statusCode": 400,
                        "message": "The sheet appears to be empty."}), 400

    # 2. Generate unique table name:  sheet_<safe_uid>_<timestamp>
    safe_uid   = re.sub(r'[^a-zA-Z0-9]', '', user_id)[:20]
    timestamp  = datetime.now().strftime("%Y%m%d%H%M%S")
    table_name = f"sheet_{safe_uid}_{timestamp}"

    # 3. Connect to MySQL and create table + insert data
    conn = get_connection_func()
    if not conn:
        return jsonify({"status": "error", "statusCode": 500,
                        "message": "Database connection failed."}), 500

    try:
        _ensure_sheet_scans_table(conn)
        safe_cols = _create_and_insert(conn, table_name, df)

        # 4. Save scan metadata
        import json
        scan_id = hashlib.md5(f"{user_id}{sheet_url}{timestamp}".encode()).hexdigest()
        cursor  = conn.cursor()
        cursor.execute("""
            INSERT INTO sheet_scans
                (scan_id, user_id, sheet_url, table_name, columns_json, row_count, scanned_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (scan_id, user_id, sheet_url, table_name,
              json.dumps(safe_cols), len(df)))
        conn.commit()
        cursor.close()

    except Exception as e:
        return jsonify({"status": "error", "statusCode": 500,
                        "message": f"Database error: {str(e)}"}), 500
    finally:
        conn.close()

    return jsonify({
        "status":     "success",
        "statusCode": 200,
        "message":    f"Sheet scanned successfully. {len(df)} rows imported.",
        "scan_id":    scan_id,
        "table_name": table_name,
        "columns":    safe_cols,
        "row_count":  len(df),
        "preview":    df.head(3).to_dict(orient="records")   # first 3 rows preview
    }), 200