from flask import request, jsonify
from database.external_cv_sync import sync_csv_to_user_db


def sync_external_csv():

    data = request.json

    user_id = data.get("user_id")
    connection_id = data.get("connection_id")
    session_id = data.get("session_id")

    if not user_id or not connection_id or not session_id:
        return jsonify({"error": "Missing parameters"}), 400

    result = sync_csv_to_user_db(user_id, connection_id, session_id)

    return jsonify(result)

