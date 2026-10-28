# controllers/save_result_controller.py

import uuid
import mysql.connector
from flask import request, jsonify
from database.config import MYSQL_CONFIG

def save_result_controller(get_connection_func):
    data = request.json or {}

    user_id   = (data.get("user_id")   or "").strip() or None
    search_id = (data.get("search_id") or "").strip() or None
    topic     = (data.get("topic")     or "").strip()
    title     = (data.get("title")     or "").strip()
    url       = (data.get("url")       or "").strip()
    brief     = (data.get("brief")     or "").strip()
    session_id = (data.get("session_id") or "").strip() or None  # directly from request body

    # ── Validation ──
    missing = [f for f, v in [("topic", topic), ("title", title), ("url", url), ("brief", brief)] if not v]
    if missing:
        return jsonify({
            "status":     "failed",
            "statusCode": 400,
            "message":    f"Missing required fields: {', '.join(missing)}"
        }), 400

    if not url.startswith(("http://", "https://")):
        return jsonify({
            "status":     "failed",
            "statusCode": 400,
            "message":    "Field 'url' must be a valid URL starting with http:// or https://"
        }), 400

    conn = cursor = None
    try:
        conn = get_connection_func()
        if not conn:
            return jsonify({
                "status":     "error",
                "statusCode": 500,
                "message":    "Database connection failed"
            }), 500

        cursor = conn.cursor(dictionary=True)

        # ── Duplicate check ──
        cursor.execute("""
            SELECT saved_id FROM saved_web_results
            WHERE user_id = %s AND url = %s
            LIMIT 1
        """, (user_id, url))
        existing = cursor.fetchone()

        if existing:
            return jsonify({
                "status":     "already_saved",
                "statusCode": 200,
                "message":    "This URL is already saved in your collection.",
                "saved_id":   existing["saved_id"]
            }), 200

        #  Use session_id from request body first, fallback to connection_history
        user_session_id = session_id
        if not user_session_id and user_id:
            cursor.execute("""
                SELECT session_id FROM connection_history
                WHERE user_id = %s AND session_id IS NOT NULL
                LIMIT 1
            """, (user_id,))
            session_row = cursor.fetchone()
            if session_row and session_row.get('session_id'):
                user_session_id = session_row['session_id']

        # ── Insert ──
        saved_id = str(uuid.uuid4())
        cursor.execute("""
            INSERT INTO saved_web_results
                (saved_id, user_id, search_id, topic, title, url, brief, session_id, saved_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (saved_id, user_id, search_id, topic, title, url, brief, user_session_id))
        conn.commit()

        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "message":    "Result saved successfully.",
            "saved_id":   saved_id,
            "data": {
                "user_id":    user_id,
                "search_id":  search_id,
                "topic":      topic,
                "title":      title,
                "url":        url,
                "brief":      brief,
                "session_id": user_session_id  #  returned in response too
            }
        }), 200

    except mysql.connector.Error as e:
        print(f"[DB] MySQL error in save_result_controller: {e}")
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    f"Database error: {str(e)}"
        }), 500
    except Exception as e:
        print(f"[DB] Unexpected error in save_result_controller: {e}")
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    f"An unexpected error occurred: {str(e)}"
        }), 500
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


def get_saved_results_controller(get_connection_func):
    user_id = request.args.get("user_id", "").strip() or None
    topic   = request.args.get("topic",   "").strip() or None

    if not user_id:
        return jsonify({
            "status":     "failed",
            "statusCode": 400,
            "message":    "Query param 'user_id' is required."
        }), 400

    conn = cursor = None
    try:
        conn = get_connection_func()
        if not conn:
            return jsonify({
                "status":     "error",
                "statusCode": 500,
                "message":    "Database connection failed"
            }), 500

        cursor = conn.cursor(dictionary=True)

        if topic:
            cursor.execute("""
                SELECT saved_id, search_id, topic, title, url, brief, session_id, saved_at
                FROM saved_web_results
                WHERE user_id = %s AND topic LIKE %s
                ORDER BY saved_at DESC
            """, (user_id, f"%{topic}%"))
        else:
            cursor.execute("""
                SELECT saved_id, search_id, topic, title, url, brief, session_id, saved_at
                FROM saved_web_results
                WHERE user_id = %s
                ORDER BY saved_at DESC
            """, (user_id,))

        rows = cursor.fetchall()

        for row in rows:
            if row.get("saved_at"):
                row["saved_at"] = row["saved_at"].strftime("%Y-%m-%d %H:%M:%S")

        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "user_id":    user_id,
            "topic":      topic,
            "count":      len(rows),
            "results":    rows
        }), 200

    except mysql.connector.Error as e:
        print(f"[DB] MySQL error in get_saved_results_controller: {e}")
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    f"Database error: {str(e)}"
        }), 500
    except Exception as e:
        print(f"[DB] Unexpected error: {e}")
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    f"An unexpected error occurred: {str(e)}"
        }), 500
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


def delete_saved_result_controller(get_connection_func, saved_id: str):
    user_id = request.args.get("user_id", "").strip() or None

    if not user_id:
        return jsonify({
            "status":     "failed",
            "statusCode": 400,
            "message":    "Query param 'user_id' is required."
        }), 400

    conn = cursor = None
    try:
        conn = get_connection_func()
        if not conn:
            return jsonify({
                "status":     "error",
                "statusCode": 500,
                "message":    "Database connection failed"
            }), 500

        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT saved_id FROM saved_web_results
            WHERE saved_id = %s AND user_id = %s
        """, (saved_id, user_id))
        row = cursor.fetchone()

        if not row:
            return jsonify({
                "status":     "not_found",
                "statusCode": 404,
                "message":    "Saved result not found or does not belong to this user."
            }), 404

        cursor.execute("DELETE FROM saved_web_results WHERE saved_id = %s", (saved_id,))
        conn.commit()

        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "message":    "Saved result deleted successfully.",
            "saved_id":   saved_id
        }), 200

    except mysql.connector.Error as e:
        print(f"[DB] MySQL error in delete_saved_result_controller: {e}")
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    f"Database error: {str(e)}"
        }), 500
    except Exception as e:
        print(f"[DB] Unexpected error: {e}")
        return jsonify({
            "status":     "error",
            "statusCode": 500,
            "message":    f"An unexpected error occurred: {str(e)}"
        }), 500
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()