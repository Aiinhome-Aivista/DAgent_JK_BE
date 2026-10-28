import json
import requests
from flask import request, jsonify

def store_hub_data_controller(get_db_connection):
    data = request.json or {}
    
    session_id = data.get("session_id")
    name = data.get("name")
    email = data.get("email")
    age = data.get("age")
    gender = data.get("gender")
    query_text = data.get("question") or data.get("query") 

    if not session_id:
        return jsonify({"status": "error", "message": "session_id is required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    cursor = None
    try:
        cursor = conn.cursor()
        
        # 1. Save user lead data
        if name or email or age or gender or query_text:
            chat_data_array = []
            if query_text:
                new_chat_entry = json.dumps({"query": query_text})
                chat_data_array.append(new_chat_entry)
            
            lead_query = """
                INSERT INTO user_chat_leads (session_id, name, email, age, gender, chat_data)
                VALUES (%s, %s, %s, %s, %s, JSON_ARRAY())
                ON DUPLICATE KEY UPDATE 
                    name = COALESCE(VALUES(name), name), 
                    email = COALESCE(VALUES(email), email), 
                    age = COALESCE(VALUES(age), age), 
                    gender = COALESCE(VALUES(gender), gender);
            """
            cursor.execute(lead_query, (session_id, name, email, age, gender))
            
            if query_text:
                new_chat_entry = json.dumps({"query": query_text})
                chat_query = """
                    UPDATE user_chat_leads 
                    SET chat_data = JSON_ARRAY_APPEND(COALESCE(chat_data, JSON_ARRAY()), '$', CAST(%s AS JSON))
                    WHERE session_id = %s;
                """
                cursor.execute(chat_query, (new_chat_entry, session_id))
                
            conn.commit()
            
        return jsonify({"status": "success", "message": "Lead data stored successfully"}), 200
        
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_hub_chat_controller(get_db_connection):
    data = request.json or {}
    
    session_id = data.get("session_id")
    query_text = data.get("query")
    answer_text = data.get("answer")
    
    if not session_id:
        return jsonify({"status": "error", "message": "session_id is required"}), 400
        
    if not query_text or not answer_text:
        return jsonify({"status": "error", "message": "query and answer are required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    cursor = None
    try:
        cursor = conn.cursor()
        
        new_chat_entry = json.dumps({"query": query_text, "response": answer_text})
        
        chat_query = """
            UPDATE user_chat_leads 
            SET chat_data = JSON_ARRAY_APPEND(COALESCE(chat_data, JSON_ARRAY()), '$', CAST(%s AS JSON))
            WHERE session_id = %s;
        """
        cursor.execute(chat_query, (new_chat_entry, session_id))
        
        # If no row was updated, the session_id doesn't exist
        if cursor.rowcount == 0:
            # We can optionally insert it if it doesn't exist, but usually update is enough
            insert_query = """
                INSERT INTO user_chat_leads (session_id, chat_data) 
                VALUES (%s, JSON_ARRAY(CAST(%s AS JSON)))
            """
            cursor.execute(insert_query, (session_id, new_chat_entry))
            
        conn.commit()
        return jsonify({"status": "success", "message": "Chat data updated successfully"}), 200
        
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
