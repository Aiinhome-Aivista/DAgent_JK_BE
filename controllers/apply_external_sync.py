from flask import request, jsonify
from database.external_sync_service import apply_external_sync


def apply_sync():

    data = request.json

    username = data.get("user_id")
    table = data.get("table")
    external_db = data.get("external_db")

    if not username or not table or not external_db:
        return jsonify({
            "error": "Missing required fields"
        }), 400

    try:

        apply_external_sync(username, external_db, table)

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500

    return jsonify({
        "message": f"{table} synced successfully"
    })

