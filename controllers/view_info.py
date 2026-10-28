import json
import traceback
import json, traceback 
from sqlalchemy import text
from database.config import MYSQL_CONFIG,engine
from flask import request, jsonify


def view_analyze_controller():
    try:
        # Get session_name from query parameter
        session_name = request.args.get("session_name")
        if not session_name:
            return jsonify({"error": "Missing session_name parameter"}), 400

        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    SELECT id, session_name, response, status, error_message, processing_status
                    FROM `analyze`
                    WHERE session_name = :sn
                    ORDER BY id DESC
                """),
                {"sn": session_name}
            ).mappings().all()  

        # Convert results to proper JSON
        data = []
        for row in result:
            data.append({
                "id": row["id"],
                "session_name": row["session_name"],
                "response": json.loads(row["response"]) if row["response"] else None,
                "status": row["status"],
                "error_message": row["error_message"],
                "processing_status": row["processing_status"]
            })

        # Fetch graph_url and insights from `graph` table
        with engine.begin() as conn:
            graph_info = conn.execute(
                text("""
                    SELECT graph_url, insights
                    FROM graph
                    WHERE session_name = :sn
                """),
                {"sn": session_name}
            ).mappings().first()

        graph_url = graph_info["graph_url"] if graph_info else None
        insights = json.loads(graph_info["insights"]) if graph_info and graph_info["insights"] else None

        # Fetch session_id from `session_tracking` table
        with engine.begin() as conn:
            session_info = conn.execute(
                text("""
                    SELECT session_id
                    FROM session_tracking
                    WHERE session_name = :sn
                """),
                {"sn": session_name}
            ).mappings().first()

        session_id = session_info["session_id"] if session_info else None

        # Merge all data into one response
        return jsonify({
            "data": data,
            "graph_url": graph_url,
            "insights": insights,
            "session_id": session_id
        })

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
