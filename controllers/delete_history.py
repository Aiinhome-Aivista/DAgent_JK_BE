import os
import json
import traceback
from flask import request, jsonify
from flask_cors import CORS
from sqlalchemy import text 
from database.config import engine  
 

def delete_session_controller(session_name):
    """
    Delete all records from 'tracker' table where SESSION_NAME matches the given session_name.
    """
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("DELETE FROM tracker WHERE SESSION_NAME = :session_name"),
                {"session_name": session_name}
            )
            conn.commit()

            if result.rowcount == 0:
                return jsonify({"error": "Session not found"}), 404

        return jsonify({"message": f"Deleted {result.rowcount} record(s) for session '{session_name}'"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
		
		