from flask import request, jsonify

def get_session_analysis_history(get_connection_func):
    session_id = request.args.get('session_id')
    
    if not session_id:
        return jsonify({"status": "failed", "statusCode": 400, "message": "session_id is required"}), 400

    conn = cur = None
    try:
        conn = get_connection_func()
        cur = conn.cursor(dictionary=True)
        
        # Auto-migrate session_analysis_cache to include report_content
        try:
            cur.execute("ALTER TABLE session_analysis_cache ADD COLUMN report_content LONGTEXT")
            conn.commit()
        except Exception:
            pass # Column already exists or table doesn't exist

        # Auto-patch workspace_prompts to pass report_content
        try:
            cur.execute("""
                UPDATE workspace_prompts 
                SET default_prompt = REPLACE(
                    default_prompt, 
                    '_save_cache(session_id, data_hash, raw_report, graph_url, topics, databases, conn)',
                    '_save_cache(session_id, data_hash, raw_report, graph_url, topics, databases, conn, report_content=json.dumps(analysis))'
                )
                WHERE default_prompt LIKE '%_save_cache(session_id, data_hash, raw_report, graph_url, topics, databases, conn)%'
            """)
            conn.commit()
        except Exception as e:
            print(f"Failed to auto-patch workspace_prompts: {e}")

        # Check if table exists
        cur.execute("SHOW TABLES LIKE 'session_analysis_cache'")
        if not cur.fetchone():
            return jsonify({"status": "success", "statusCode": 200, "history": []}), 200

        cur.execute("""
            SELECT id, session_id, report, report_content, graph_url, topics, databases, created_at 
            FROM session_analysis_cache 
            WHERE session_id = %s
            ORDER BY created_at DESC
        """, (session_id,))
        
        rows = cur.fetchall()
        
        history = []
        for r in rows:
            history.append({
                "id": r["id"],
                "session_id": r["session_id"],
                "report": r["report"],
                "report_content": r.get("report_content"),
                "graph_url": r["graph_url"],
                "topics": r["topics"],
                "databases": r["databases"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None
            })
            
        return jsonify({
            "status": "success",
            "statusCode": 200,
            "history": history
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "statusCode": 500, "message": str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()
