from flask import request, jsonify

# ===============================
# DATA CATEGORIES CRUD
# ===============================
def get_data_categories(get_db_connection):
    conn = get_db_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, category_code, category_name FROM data_categories ORDER BY id ASC")
        categories = cursor.fetchall()
        return jsonify({"success": True, "categories": categories}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()

def create_data_category(get_db_connection):
    data = request.json
    code = data.get("category_code")
    name = data.get("category_name")
    if not code or not name:
        return jsonify({"success": False, "message": "Code and name are required"}), 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO data_categories (category_code, category_name) VALUES (%s, %s)", (code, name))
        conn.commit()
        return jsonify({"success": True, "message": "Category created successfully", "id": cursor.lastrowid}), 201
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()

def update_data_category(get_db_connection, id):
    data = request.json
    code = data.get("category_code")
    name = data.get("category_name")
    if not code or not name:
        return jsonify({"success": False, "message": "Code and name are required"}), 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE data_categories SET category_code=%s, category_name=%s WHERE id=%s", (code, name, id))
        conn.commit()
        return jsonify({"success": True, "message": "Category updated successfully"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()

def delete_data_category(get_db_connection, id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM data_categories WHERE id=%s", (id,))
        conn.commit()
        return jsonify({"success": True, "message": "Category deleted successfully"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()

# ===============================
# PROMPT TYPES CRUD
# ===============================
def get_prompt_types_crud(get_db_connection):
    conn = get_db_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, type_code, display_name, description FROM prompt_types_master ORDER BY id ASC")
        types = cursor.fetchall()
        return jsonify({"success": True, "prompt_types": types}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()

def create_prompt_type(get_db_connection):
    data = request.json
    code = data.get("type_code")
    name = data.get("display_name")
    desc = data.get("description", "")
    if not code or not name:
        return jsonify({"success": False, "message": "Code and name are required"}), 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO prompt_types_master (type_code, display_name, description) VALUES (%s, %s, %s)", (code, name, desc))
        conn.commit()
        return jsonify({"success": True, "message": "Prompt type created successfully", "id": cursor.lastrowid}), 201
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()

def update_prompt_type(get_db_connection, id):
    data = request.json
    code = data.get("type_code")
    name = data.get("display_name")
    desc = data.get("description", "")
    if not code or not name:
        return jsonify({"success": False, "message": "Code and name are required"}), 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE prompt_types_master SET type_code=%s, display_name=%s, description=%s WHERE id=%s", (code, name, desc, id))
        conn.commit()
        return jsonify({"success": True, "message": "Prompt type updated successfully"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()

def delete_prompt_type(get_db_connection, id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM prompt_types_master WHERE id=%s", (id,))
        conn.commit()
        return jsonify({"success": True, "message": "Prompt type deleted successfully"}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if conn: conn.close()
