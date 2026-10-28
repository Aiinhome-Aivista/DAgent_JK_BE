from flask import request, jsonify

def get_all_prompt_types(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "message": "Database connection failed"}), 500
    
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT type_code as value, display_name as label FROM prompt_types_master ORDER BY id ASC")
        types = cursor.fetchall()
        return jsonify({"success": True, "prompt_types": types}), 200
    except Exception as e:
        print(f"Error fetching prompt types: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        conn.close()

def get_workspace_prompts(get_db_connection, workspace_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "message": "Database connection failed"}), 500
    
    try:
        cursor = conn.cursor(dictionary=True)
        query = "SELECT prompt_type, custom_prompt, data_category FROM workspace_prompts WHERE workspace_id = %s"
        cursor.execute(query, (workspace_id,))
        prompts = cursor.fetchall()
        return jsonify({"success": True, "prompts": prompts}), 200
    except Exception as e:
        print(f"Error fetching workspace prompts: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        conn.close()

def get_workspace_prompt_by_type(get_db_connection, workspace_id, prompt_type, data_category='global'):
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "message": "Database connection failed"}), 500
    
    try:
        cursor = conn.cursor(dictionary=True)
        query = "SELECT custom_prompt FROM workspace_prompts WHERE workspace_id = %s AND prompt_type = %s AND data_category = %s"
        cursor.execute(query, (workspace_id, prompt_type, data_category))
        result = cursor.fetchone()
        
        # Fallback to global if not found
        if not result and data_category != 'global':
            cursor.execute(query, (workspace_id, prompt_type, 'global'))
            result = cursor.fetchone()
            
        if result:
            return jsonify({"success": True, "custom_prompt": result['custom_prompt']}), 200
        else:
            return jsonify({"success": True, "custom_prompt": None}), 200
    except Exception as e:
        print(f"Error fetching workspace prompt by type: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        conn.close()

def set_workspace_prompt(get_db_connection):
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No JSON data provided"}), 400
    
    workspace_id = data.get('workspace_id')
    prompt_type = data.get('prompt_type')
    custom_prompt = data.get('custom_prompt')
    data_category = data.get('data_category', 'global')
    
    if workspace_id is None or not prompt_type or not custom_prompt:
        return jsonify({"success": False, "message": "Missing required fields (workspace_id, prompt_type, custom_prompt)"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "message": "Database connection failed"}), 500
    
    try:
        cursor = conn.cursor()
        query = """
            INSERT INTO workspace_prompts (workspace_id, prompt_type, custom_prompt, data_category) 
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE custom_prompt = VALUES(custom_prompt), updated_at = CURRENT_TIMESTAMP
        """
        cursor.execute(query, (workspace_id, prompt_type, custom_prompt, data_category))
        conn.commit()
        return jsonify({"success": True, "message": "Custom prompt saved successfully"}), 200
    except Exception as e:
        print(f"Error saving workspace prompt: {e}")
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        conn.close()

def get_all_workspace_prompts(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "message": "Database connection failed"}), 500
    
    try:
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT wp.workspace_id, w.workspace_name, wp.prompt_type, wp.data_category, wp.custom_prompt, pt.display_name as prompt_type_label
            FROM workspace_prompts wp
            LEFT JOIN workspaces w ON wp.workspace_id = w.id
            LEFT JOIN prompt_types_master pt ON wp.prompt_type = pt.type_code
            ORDER BY COALESCE(wp.updated_at, wp.created_at) DESC
        """
        cursor.execute(query)
        prompts = cursor.fetchall()
        return jsonify({"success": True, "prompts": prompts}), 200
    except Exception as e:
        print(f"Error fetching all workspace prompts: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        conn.close()

def delete_workspace_prompt(get_db_connection):
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No JSON data provided"}), 400

    workspace_id = data.get('workspace_id')
    prompt_type = data.get('prompt_type')
    data_category = data.get('data_category', 'global')

    if workspace_id is None or not prompt_type:
        return jsonify({"success": False, "message": "Missing required fields (workspace_id, prompt_type)"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "message": "Database connection failed"}), 500

    try:
        cursor = conn.cursor()
        query = "DELETE FROM workspace_prompts WHERE workspace_id = %s AND prompt_type = %s AND data_category = %s"
        cursor.execute(query, (workspace_id, prompt_type, data_category))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"success": False, "message": "Prompt not found"}), 404
        return jsonify({"success": True, "message": "Custom prompt deleted successfully"}), 200
    except Exception as e:
        print(f"Error deleting workspace prompt: {e}")
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        conn.close()
