from flask import request, jsonify

def get_workspace_types_controller(get_db_connection):
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM workspace_types ORDER BY type_name")
        types = cursor.fetchall()
        cursor.close()
        db_conn.close()
        return jsonify({"status": "success", "data": types}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def create_workspace_type_controller(get_db_connection):
    data = request.json
    type_name = data.get("type_name")
    if not type_name:
        return jsonify({"status": "error", "message": "type_name is required"}), 400
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)
        cursor.execute("INSERT INTO workspace_types (type_name) VALUES (%s)", (type_name,))
        db_conn.commit()
        new_id = cursor.lastrowid
        cursor.close()
        db_conn.close()
        return jsonify({"status": "success", "message": "Workspace type created", "id": new_id, "type_name": type_name}), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
