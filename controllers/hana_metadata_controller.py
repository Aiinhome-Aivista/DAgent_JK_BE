from flask import request, jsonify
from database.hana_metadata_service import discover_hana_metadata, get_stored_hana_metadata
from database.hana_kgraph_service import start_hana_kgraph_build_async, get_hana_kgraph_status


def discover_hana_metadata_controller():
    """
    POST /hana/discover-metadata
    Body: { "user_id": 6, "connection_id": 123, "session_id": "xyz", "build_kgraph": true }
    Extracts metadata only (no data rows) and, by default, queues the KGraph build.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        connection_id = data.get("connection_id")
        if not connection_id:
            return jsonify({"status": "error", "message": "connection_id is required"}), 400

        user_id = int(data.get("user_id")) if data.get("user_id") else 0
        connection_id = int(connection_id)
        session_id = data.get("session_id")

        result = discover_hana_metadata(user_id=user_id, connection_id=connection_id, session_id=session_id)
        session_id = session_id or result.get("session_id")

        kgraph = {"status": "not_requested"}
        if data.get("build_kgraph", True) and session_id:
            start_hana_kgraph_build_async(connection_id, session_id, user_id or None)
            kgraph = {"status": "queued"}
        result["kgraph"] = kgraph

        return jsonify({
            "status": "success",
            "message": f"Discovered metadata for {result.get('total_views', 0)} SAP HANA views (no data copied)",
            "data": result,
        }), 200
    except Exception as e:
        print(f"[HANA-METADATA-ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def get_hana_metadata_controller():
    """GET /hana/metadata?connection_id=123&session_id=xyz&user_id=6"""
    try:
        connection_id = request.args.get("connection_id")
        session_id = request.args.get("session_id")
        user_id = request.args.get("user_id")
        if not connection_id and not session_id:
            return jsonify({"status": "error", "message": "Either connection_id or session_id is required"}), 400

        metadata = get_stored_hana_metadata(
            connection_id=int(connection_id) if connection_id else None,
            session_id=session_id,
            user_id=int(user_id) if user_id else None,
        )
        return jsonify({"status": "success", "count": len(metadata), "data": metadata}), 200
    except Exception as e:
        print(f"[HANA-METADATA-GET-ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def build_hana_kgraph_controller():
    """
    POST /hana/build-kgraph
    Body: { "connection_id": 123, "session_id": "xyz", "user_id": 6 }
    Rebuilds the KGraph from the metadata already stored in MySQL (HANA is not contacted).
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        connection_id = data.get("connection_id")
        session_id = data.get("session_id")
        if not connection_id or not session_id:
            return jsonify({"status": "error", "message": "connection_id and session_id are required"}), 400
        user_id = int(data.get("user_id")) if data.get("user_id") else None
        start_hana_kgraph_build_async(int(connection_id), session_id, user_id)
        return jsonify({"status": "success", "message": "KGraph build queued", "data": {"status": "queued"}}), 202
    except Exception as e:
        print(f"[HANA-KGRAPH-BUILD-ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def get_hana_kgraph_status_controller():
    """GET /hana/kgraph-status?session_id=xyz[&connection_id=123][&include_graph=1]"""
    try:
        session_id = request.args.get("session_id")
        connection_id = request.args.get("connection_id")
        include_graph = str(request.args.get("include_graph", "0")).lower() in ("1", "true", "yes")
        if not session_id and not connection_id:
            return jsonify({"status": "error", "message": "session_id or connection_id is required"}), 400

        rows = get_hana_kgraph_status(session_id=session_id, connection_id=connection_id,
                                      include_graph=include_graph)
        states = [r.get("status") for r in rows]
        if not rows:
            overall = "not_started"
        elif "error" in states:
            overall = "error"
        elif any(s in ("queued", "building") for s in states):
            overall = "building"
        else:
            overall = "ready"
        return jsonify({"status": "success", "kgraph_status": overall, "data": rows}), 200
    except Exception as e:
        print(f"[HANA-KGRAPH-STATUS-ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
