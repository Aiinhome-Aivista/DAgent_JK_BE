from flask import request, jsonify

def get_companies_controller(get_db_connection):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM companies ORDER BY updated_at DESC, created_at DESC")
        companies = cur.fetchall()
        return jsonify({"status": "success", "data": companies}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()

def create_company_controller(get_db_connection):
    data = request.json
    if not data or 'company_name' not in data:
        return jsonify({"status": "error", "message": "company_name is required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM companies")
        count = cur.fetchone()[0]
        
        if count >= 1:
            return jsonify({"status": "error", "message": "A company already exists. You can only update it."}), 400
            
        query = """
            INSERT INTO companies (company_name, company_type, address, phone_number, plan_type)
            VALUES (%s, %s, %s, %s, %s)
        """
        values = (
            data.get('company_name'),
            data.get('company_type', 'General'),
            data.get('address', 'N/A'),
            data.get('phone_number', 'N/A'),
            data.get('plan_type', 'Bronze')
        )
        cur.execute(query, values)
        conn.commit()
        company_id = cur.lastrowid
        return jsonify({"status": "success", "message": "Company created successfully", "id": company_id}), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()

def update_company_controller(get_db_connection, company_id):
    data = request.json
    if not data or 'company_name' not in data:
        return jsonify({"status": "error", "message": "company_name is required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        cur = conn.cursor()
        query = """
            UPDATE companies 
            SET company_name = %s, company_type = %s, address = %s, phone_number = %s, plan_type = %s
            WHERE id = %s
        """
        values = (
            data.get('company_name'),
            data.get('company_type', 'General'),
            data.get('address', 'N/A'),
            data.get('phone_number', 'N/A'),
            data.get('plan_type', 'Bronze'),
            company_id
        )
        cur.execute(query, values)
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"status": "error", "message": "Company not found"}), 404
        return jsonify({"status": "success", "message": "Company updated successfully"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()

def delete_company_controller(get_db_connection, company_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM companies WHERE id = %s", (company_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"status": "error", "message": "Company not found"}), 404
        return jsonify({"status": "success", "message": "Company deleted successfully"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()
