from flask import request, jsonify
import traceback

def _list_backup_timestamps(db, nodes_col_name: str = "session_nodes", edges_col_name: str = "session_edges") -> list:
    """Returns a sorted list of timestamps (descending) of available backups."""
    known_prefixes = (nodes_col_name, edges_col_name)
    all_colls = db.collections()
    old_backups = [
        c["name"] for c in all_colls
        if "_backup_" in c["name"]
        and not c["name"].startswith("_")
        and any(c["name"].startswith(p) for p in known_prefixes)
    ]
    timestamps = set()
    for coll in old_backups:
        parts = coll.split("_backup_")
        if len(parts) > 1:
            ts_part = parts[1].split("_nodes_")[0]
            timestamps.add(ts_part)
    return sorted(list(timestamps), reverse=True)

def _get_backup_collection_names(db, target_timestamp: str, nodes_col_name: str = "session_nodes", edges_col_name: str = "session_edges"):
    """Returns the exact node and edge backup collection names for a given timestamp."""
    all_colls = db.collections()
    node_bkp = None
    edge_bkp = None
    for c in all_colls:
        name = c["name"]
        if "_backup_" in name and target_timestamp in name:
            if name.startswith(nodes_col_name):
                node_bkp = name
            elif name.startswith(edges_col_name):
                edge_bkp = name
    return node_bkp, edge_bkp

def rollback_kgraph_controller(get_db_connection):
    try:
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        target_timestamp = data.get("backup_timestamp")

        if not session_id:
            return jsonify({"status": "error", "message": "session_id is required"}), 400

        conn = get_db_connection()
        workspace_arango_db = None
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("SELECT workspace_arango_db FROM workspaces WHERE session_id = %s", (session_id,))
                row = cur.fetchone()
                if row:
                    workspace_arango_db = row["workspace_arango_db"]
        finally:
            if conn:
                conn.close()

        if not workspace_arango_db:
            return jsonify({"status": "error", "message": "ArangoDB not found for this session."}), 404

        from arango import ArangoClient
        from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB

        client = ArangoClient(hosts=ARANGO_HOST)
        sys_db = client.db('_system', username=ARANGO_USER, password=ARANGO_PASS)
        db_to_use = workspace_arango_db if workspace_arango_db else ARANGO_DB

        if not sys_db.has_database(db_to_use):
            return jsonify({"status": "error", "message": "ArangoDB database does not exist."}), 404

        db = client.db(db_to_use, username=ARANGO_USER, password=ARANGO_PASS)
        
        nodes_col_name = "session_nodes"
        edges_col_name = "session_edges"

        if not target_timestamp:
            timestamps = _list_backup_timestamps(db)
            if not timestamps:
                return jsonify({"status": "error", "message": "No backups found to roll back to."}), 404
            target_timestamp = timestamps[0]

        nodes_backup, edges_backup = _get_backup_collection_names(db, target_timestamp)

        if not nodes_backup or not edges_backup or not db.has_collection(nodes_backup) or not db.has_collection(edges_backup):
            return jsonify({"status": "error", "message": f"Backup collections for {target_timestamp} do not exist."}), 404

        if not db.has_collection(nodes_col_name):
            db.create_collection(nodes_col_name)
        if not db.has_collection(edges_col_name):
            db.create_collection(edges_col_name, edge=True)

        nodes_col = db.collection(nodes_col_name)
        edges_col = db.collection(edges_col_name)

        # Truncate active collections
        nodes_col.truncate()
        edges_col.truncate()

        # Perform atomic AQL insert from backup to active
        db.aql.execute(f"FOR doc IN {nodes_backup} INSERT UNSET(doc, '_id', '_rev') INTO {nodes_col_name}")
        db.aql.execute(f"FOR doc IN {edges_backup} INSERT UNSET(doc, '_id', '_rev') INTO {edges_col_name}")

        return jsonify({
            "status": "success", 
            "message": f"Successfully rolled back to K-Graph backup {target_timestamp}",
            "timestamp": target_timestamp
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

def get_all_kgraphs_controller(get_db_connection):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT id, session_name, graph_url, created_at FROM graph ORDER BY created_at DESC")
            graphs = cursor.fetchall()
            return jsonify({"status": "success", "graphs": graphs}), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


def get_kgraph_ontology_analysis_controller(get_db_connection):
    """
    Returns the Golden Sales Ontology comparison and AI missing node analysis
    for the given session/workspace.
    """
    try:
        body = request.get_json(silent=True) or {}
        session_id = request.args.get("session_id") or body.get("session_id")
        if not session_id:
            return jsonify({"status": "error", "message": "session_id is required"}), 400

        conn = get_db_connection()
        workspace_arango_db = None
        workspace_db = None
        try:
            with conn.cursor(dictionary=True) as cur:
                cur.execute("SELECT workspace_db, workspace_arango_db FROM workspaces WHERE session_id = %s", (session_id,))
                row = cur.fetchone()
                if row:
                    workspace_db = row["workspace_db"]
                    workspace_arango_db = row["workspace_arango_db"]
        finally:
            if conn:
                conn.close()

        # 1. Retrieve persisted analysis from ArangoDB session_metadata
        if workspace_arango_db:
            try:
                from arango import ArangoClient
                from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS
                client = ArangoClient(hosts=ARANGO_HOST)
                db = client.db(workspace_arango_db, username=ARANGO_USER, password=ARANGO_PASS)
                if db.has_collection("session_metadata"):
                    meta_col = db.collection("session_metadata")
                    if meta_col.has("ontology_analysis"):
                        doc = meta_col.get("ontology_analysis")
                        return jsonify({"status": "success", "data": doc.get("data")}), 200
            except Exception as arango_err:
                print(f"[KGRAPH] Could not fetch cached ontology analysis from Arango: {arango_err}")

        # 2. If not ready/cached yet, return a pending status instead of generating a false 0% report
        return jsonify({
            "status": "pending",
            "message": "Ontology analysis is pending or data has not been synced yet for this workspace."
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

