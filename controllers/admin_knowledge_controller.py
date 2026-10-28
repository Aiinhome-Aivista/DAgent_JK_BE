import os
import uuid
import datetime
import json
import shutil
import threading
from flask import request, jsonify
from database.config import UPLOAD_FOLDER, MYSQL_CONFIG
import mysql.connector
from model.llm_client import call_llm_chat
import chromadb
from sentence_transformers import SentenceTransformer
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# For background connection
from controllers.external_db import connect_external_db

# Initialize Chroma and Embedding Model for Indexing
chroma_client = chromadb.PersistentClient(path="./chroma_store")
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
model_path = os.path.join(base_dir, 'local_models', 'all-MiniLM-L6-v2')
embedding_model = SentenceTransformer(model_path)

# Initialize Background Scheduler
kg_scheduler = BackgroundScheduler(daemon=True)
kg_scheduler.start()

# Keep track of current schedule settings
# (Uncomment the one you want to use as default)

# DEFAULT: Every 6 hours
_current_schedule = {"type": "interval", "hours": 6}

# DAY WISE (e.g. Every day at 2:00 AM):
# _current_schedule = {"type": "cron", "hour": "2", "minute": "0"}

# WEEK WISE (e.g. Every Sunday at 2:00 AM):
# _current_schedule = {"type": "cron", "day_of_week": "sun", "hour": "2", "minute": "0"}


def _validate_with_llm(question, answer):
    system_prompt = (
        "You are an expert Data Auditor.\n"
        "Review the following Question and Answer.\n"
        "Determine if the Answer accurately, logically, and sufficiently addresses the Question.\n"
        "Respond in strict JSON format: {\"is_valid\": true/false, \"reason\": \"why\"}"
    )
    user_prompt = f"Question:\n{question}\n\nAnswer:\n{answer}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    try:
        response = call_llm_chat(messages, json_mode=True, temperature=0.1)
        if response and not response.startswith("[LLM Error]"):
            import json
            data = json.loads(response.strip())
            return data.get("is_valid", False), data.get("reason", "No reason provided")
    except Exception as e:
        print(f"[KG Validator Error] {e}")
    return False, "Validation failed or timed out."

def _ensure_db_schema(conn):
    """Ensure session_chat_history has the required columns for knowledge tracking."""
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM session_chat_history LIKE 'kg_status'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE session_chat_history ADD COLUMN kg_status VARCHAR(50) DEFAULT 'none'")
            cursor.execute("ALTER TABLE session_chat_history ADD COLUMN kg_file_path VARCHAR(255) DEFAULT NULL")
            cursor.execute("ALTER TABLE session_chat_history ADD COLUMN kg_reason TEXT DEFAULT NULL")
            conn.commit()
    except Exception as e:
        print(f"Error checking/altering schema: {e}")
    finally:
        cursor.close()

def _run_auto_indexing(chat_ids=None):
    """
    Core indexing engine. 
    If chat_ids is provided, indexes only those. 
    If None, indexes ALL files where kg_status='staged'.
    """
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        
        if chat_ids:
            format_strings = ','.join(['%s'] * len(chat_ids))
            cursor.execute(f"SELECT id, kg_file_path, question, answer FROM session_chat_history WHERE id IN ({format_strings}) AND kg_status = 'staged'", tuple(chat_ids))
        else:
            cursor.execute("SELECT id, kg_file_path, question, answer FROM session_chat_history WHERE kg_status = 'staged'")
            
        staged_records = cursor.fetchall()

        if not staged_records:
            print("[KG Indexer] No staged files to index.")
            return

        completed_dir = os.path.join(UPLOAD_FOLDER, "completed_knowledge")
        os.makedirs(completed_dir, exist_ok=True)

        collection = chroma_client.get_or_create_collection(name="admin_knowledge_graph", metadata={"hnsw:space": "cosine"})
        
        indexed_count = 0
        documents = []
        embeddings = []
        ids = []

        for record in staged_records:
            file_path = record["kg_file_path"]
            cid = record["id"]
            question = record.get("question", "")
            answer = record.get("answer", "")
            
            if file_path and os.path.exists(file_path):
                print(f"[KG Indexer] Validating record {cid} with LLM...")
                is_valid, reason = _validate_with_llm(question, answer)
                
                if not is_valid:
                    print(f"[KG Indexer] Record {cid} REJECTED: {reason}")
                    os.remove(file_path)
                    cursor.execute("UPDATE session_chat_history SET kg_status = 'rejected', kg_file_path = NULL, kg_reason = %s WHERE id = %s", (reason, cid))
                    continue
                    
                print(f"[KG Indexer] Record {cid} APPROVED by LLM.")
                with open(file_path, "r", encoding="utf-8") as f:
                    text_content = f.read()

                emb = embedding_model.encode(text_content).tolist()
                documents.append(text_content)
                embeddings.append(emb)
                ids.append(f"admin_kg_{cid}")

                filename = os.path.basename(file_path)
                new_path = os.path.join(completed_dir, filename)
                shutil.move(file_path, new_path)

                cursor.execute("UPDATE session_chat_history SET kg_status = 'indexed', kg_file_path = %s WHERE id = %s", (new_path, cid))
                indexed_count += 1
        
        if documents:
            collection.add(documents=documents, embeddings=embeddings, ids=ids)
            try: chroma_client.persist()
            except AttributeError: pass

        conn.commit()
        print(f"[KG Indexer] Successfully auto-indexed {indexed_count} chat(s).")

    except Exception as e:
        print(f"[KG Indexer] Error during indexing: {e}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# Start the default background job
# (Uncomment the one you want to use as default)

# DEFAULT: Every 6 hours
kg_scheduler.add_job(
    _run_auto_indexing,
    IntervalTrigger(hours=6),
    id='kg_auto_indexer',
    replace_existing=True
)

# DAY WISE (e.g. Every day at 2:00 AM):
# kg_scheduler.add_job(
#     _run_auto_indexing,
#     CronTrigger(hour="2", minute="0"),
#     id='kg_auto_indexer',
#     replace_existing=True
# )

# WEEK WISE (e.g. Every Sunday at 2:00 AM):
# kg_scheduler.add_job(
#     _run_auto_indexing,
#     CronTrigger(day_of_week="sun", hour="2", minute="0"),
#     id='kg_auto_indexer',
#     replace_existing=True
# )

def admin_get_chats_controller(get_db_connection):
    try:
        limit = int(request.args.get("limit", 200))
        conn = get_db_connection()
        _ensure_db_schema(conn)
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT id, session_id, question, answer, visualizations, created_at, kg_status, is_public
            FROM session_chat_history
            WHERE question IS NOT NULL AND (kg_status = 'none' OR kg_status IS NULL)
            ORDER BY created_at DESC
            LIMIT %s
        """
        cursor.execute(query, (limit,))
        rows = cursor.fetchall()
        return jsonify({"status": "success", "data": rows}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

def admin_toggle_public_controller(get_db_connection):
    try:
        data = request.json
        if not data or 'id' not in data or 'is_public' not in data:
            return jsonify({"status": "error", "message": "id and is_public are required"}), 400
        
        chat_id = data['id']
        is_public = bool(data['is_public'])
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE session_chat_history SET is_public = %s WHERE id = %s",
            (is_public, chat_id)
        )
        conn.commit()
        
        return jsonify({"status": "success", "message": f"Chat visibility updated to {'Public' if is_public else 'Private'}"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

def admin_share_chat_controller(get_db_connection):
    try:
        data = request.json
        if not data or 'chat_id' not in data or 'workspace_id' not in data or 'user_ids' not in data:
            return jsonify({"status": "error", "message": "chat_id, workspace_id, and user_ids are required"}), 400
        
        chat_id = data['chat_id']
        workspace_id = data['workspace_id']
        user_ids = data['user_ids']
        
        if not isinstance(user_ids, list):
            return jsonify({"status": "error", "message": "user_ids must be a list"}), 400
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Optionally clear previous sharing for this chat in this workspace
        # cursor.execute("DELETE FROM shared_chat_insights WHERE chat_id = %s AND target_workspace_id = %s", (chat_id, workspace_id))
        
        # Insert new sharing records
        if len(user_ids) > 0:
            for uid in user_ids:
                # Use INSERT IGNORE or handle duplicates if unique constraint exists
                cursor.execute("""
                    INSERT INTO shared_chat_insights (chat_id, target_workspace_id, target_user_id) 
                    SELECT %s, %s, %s 
                    WHERE NOT EXISTS (
                        SELECT 1 FROM shared_chat_insights 
                        WHERE chat_id = %s AND target_workspace_id = %s AND target_user_id = %s
                    )
                """, (chat_id, workspace_id, uid, chat_id, workspace_id, uid))
                
        conn.commit()
        return jsonify({"status": "success", "message": "Chat shared successfully"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

def admin_push_knowledge_controller(get_db_connection):
    try:
        data = request.json
        if not data: return jsonify({"status": "error", "message": "No JSON payload provided"}), 400

        chat_id = data.get("id") or data.get("chat_id")
        question = data.get("question", "").strip()
        answer = data.get("answer", "").strip()
        visualizations = data.get("visualizations", None)

        if not chat_id or not question or not answer:
            return jsonify({"status": "error", "message": "'id' or 'chat_id', 'question', and 'answer' are required."}), 400

        viz_text = ""
        if visualizations:
            try:
                if isinstance(visualizations, str): visualizations = json.loads(visualizations)
                viz_text = "\\nCharts & Visualizations:\\n" + json.dumps(visualizations, indent=2) + "\\n"
            except:
                viz_text = "\\nCharts & Visualizations:\\n" + str(visualizations) + "\\n"

        knowledge_content = f"""[Verified Admin Knowledge]
Source: Approved Workspace Chat
Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Question:
{question}

Accurate Answer:
{answer}
{viz_text}"""
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:6]
        filename = f"verified_knowledge_{timestamp}_{unique_id}.txt"
        
        pending_dir = os.path.join(UPLOAD_FOLDER, "pending_knowledge")
        os.makedirs(pending_dir, exist_ok=True)
        file_path = os.path.join(pending_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(knowledge_content)

        conn = get_db_connection()
        _ensure_db_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE session_chat_history SET kg_status = 'staged', kg_file_path = %s WHERE id = %s",
            (file_path, chat_id)
        )
        conn.commit()

        return jsonify({"status": "success", "file_name": filename}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

def admin_get_staged_knowledge_controller(get_db_connection):
    try:
        conn = get_db_connection()
        _ensure_db_schema(conn)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, session_id, question, answer, kg_file_path, created_at FROM session_chat_history WHERE kg_status = 'staged' ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return jsonify({"status": "success", "data": rows}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

def admin_trigger_indexing_controller(get_db_connection):
    """Manual trigger API. Runs synchronously."""
    data = request.json or {}
    chat_ids = data.get("chat_ids")
    
    # Run the indexer logic synchronously
    _run_auto_indexing(chat_ids=chat_ids)

    return jsonify({"status": "success", "message": "Manual indexing triggered successfully."}), 200

def admin_set_schedule_controller(get_db_connection):
    """Configures the auto-indexing schedule."""
    global _current_schedule
    data = request.json or {}
    schedule_type = data.get("type", "interval")
    
    try:
        if schedule_type == "interval":
            hours = int(data.get("hours", 6))
            kg_scheduler.add_job(_run_auto_indexing, IntervalTrigger(hours=hours), id='kg_auto_indexer', replace_existing=True)
            _current_schedule = {"type": "interval", "hours": hours}
        elif schedule_type == "cron":
            hour = data.get("hour", "*")
            minute = data.get("minute", "0")
            kg_scheduler.add_job(_run_auto_indexing, CronTrigger(hour=hour, minute=minute), id='kg_auto_indexer', replace_existing=True)
            _current_schedule = {"type": "cron", "hour": hour, "minute": minute}
        else:
            return jsonify({"status": "error", "message": "Invalid schedule type. Use 'interval' or 'cron'."}), 400

        return jsonify({"status": "success", "message": "Schedule updated.", "schedule": _current_schedule}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def admin_get_schedule_controller(get_db_connection):
    """Returns the current auto-indexing schedule."""
    global _current_schedule
    return jsonify({"status": "success", "schedule": _current_schedule}), 200
