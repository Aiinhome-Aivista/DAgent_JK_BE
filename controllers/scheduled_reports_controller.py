from flask import request, jsonify
from database.db_connection import get_db_connection

def add_schedule_controller():
    data = request.json
    recipient_id = data.get('recipient_id')
    workspace_id = data.get('workspace_id')
    report_ids = data.get('report_ids', []) # List of selected report IDs, optional
    time = data.get('time')
    days = data.get('days', []) # List of strings like ['Monday', 'Tuesday']

    if not recipient_id or not workspace_id or not time or not days:
        return jsonify({"status": "error", "message": "recipient_id, workspace_id, time, and days are required"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        import json
        report_ids_str = json.dumps(report_ids)
        
        # Insert into scheduled_reports
        cursor.execute(
            "INSERT INTO scheduled_reports (recipient_id, workspace_id, report_ids, delivery_time) VALUES (%s, %s, %s, %s)",
            (recipient_id, workspace_id, report_ids_str, time)
        )
        schedule_id = cursor.lastrowid
        
        # Insert days into scheduled_report_days
        for day in days:
            cursor.execute(
                "INSERT INTO scheduled_report_days (schedule_id, day_of_week) VALUES (%s, %s)",
                (schedule_id, day)
            )
            
        conn.commit()
        return jsonify({"status": "success", "message": "Schedule created", "data": {"id": schedule_id}}), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()

def get_schedules_controller():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Join to get all necessary info
        query = """
            SELECT 
                sr.id, sr.recipient_id, sr.workspace_id, sr.report_ids, sr.delivery_time as time,
                u.name as recipient_name, u.email as recipient_email,
                w.workspace_name
            FROM scheduled_reports sr
            JOIN users u ON sr.recipient_id = u.id
            LEFT JOIN workspaces w ON sr.workspace_id = w.id
            ORDER BY sr.id DESC
        """
        cursor.execute(query)
        schedules = cursor.fetchall()
        
        # Fetch days for each schedule
        import json
        for schedule in schedules:
            # format time delta to string HH:MM:SS
            schedule['time'] = str(schedule['time']) 
            
            try:
                schedule['report_ids'] = json.loads(schedule['report_ids']) if schedule['report_ids'] else []
            except:
                schedule['report_ids'] = []
                
            cursor.execute("SELECT day_of_week FROM scheduled_report_days WHERE schedule_id = %s", (schedule['id'],))
            days = cursor.fetchall()
            schedule['days'] = [d['day_of_week'] for d in days]
            
        return jsonify({"status": "success", "data": schedules}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()

def update_schedule_controller(schedule_id):
    data = request.json
    recipient_id = data.get('recipient_id')
    workspace_id = data.get('workspace_id')
    report_ids = data.get('report_ids', [])
    time = data.get('time')
    days = data.get('days', [])
    
    if not recipient_id or not workspace_id or not time or not days:
        return jsonify({"status": "error", "message": "recipient_id, workspace_id, time, and days are required"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        import json
        report_ids_str = json.dumps(report_ids)
        
        # Update main table
        cursor.execute(
            "UPDATE scheduled_reports SET recipient_id = %s, workspace_id = %s, report_ids = %s, delivery_time = %s WHERE id = %s",
            (recipient_id, workspace_id, report_ids_str, time, schedule_id)
        )
        
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "Schedule not found"}), 404
            
        # Delete existing days
        cursor.execute("DELETE FROM scheduled_report_days WHERE schedule_id = %s", (schedule_id,))
        
        # Insert new days
        for day in days:
            cursor.execute(
                "INSERT INTO scheduled_report_days (schedule_id, day_of_week) VALUES (%s, %s)",
                (schedule_id, day)
            )
            
        conn.commit()
        return jsonify({"status": "success", "message": "Schedule updated"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()

def delete_schedule_controller(schedule_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Deleting from scheduled_reports will cascade to scheduled_report_days due to ON DELETE CASCADE
        cursor.execute("DELETE FROM scheduled_reports WHERE id = %s", (schedule_id,))
        conn.commit()
        
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "Schedule not found"}), 404
            
        return jsonify({"status": "success", "message": "Schedule deleted"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()
