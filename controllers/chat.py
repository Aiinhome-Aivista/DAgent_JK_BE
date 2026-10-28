import re
import io
import uuid
import json
import pypdf
import logging
import chromadb
import itertools
import numpy as np
import pandas as pd
import mysql.connector
from rapidfuzz import fuzz
from flask import request, jsonify
from sqlalchemy import create_engine
from model.llm_client import call_llm
from database.config import ACTIVE_LLM, MYSQL_CONFIG
from sentence_transformers import SentenceTransformer
from sqlalchemy import Text
from sqlalchemy.dialects.mysql import LONGTEXT

# Logging
logging.basicConfig(level=logging.INFO)

# Build SQLAlchemy URL
MYSQL_URL = (
    f"mysql+pymysql://{MYSQL_CONFIG['user']}:{MYSQL_CONFIG['password'].replace('@', '%40')}"
    f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}"
)
engine = create_engine(MYSQL_URL, pool_pre_ping=True)

sessions = {}

# Load embedding model
import os
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
model_path = os.path.join(base_dir, 'local_models', 'all-MiniLM-L6-v2')
embedding_model = SentenceTransformer(model_path)

# ------------------------ ChromaDB Init ------------------------
chroma_client = chromadb.PersistentClient(path="./chroma_store")

# ------------------------ Helpers ------------------------
def detect_relationships(dfs):
    relationships = []
    for (t1, df1), (t2, df2) in itertools.combinations(dfs.items(), 2):
        for c1 in df1.columns:
            for c2 in df2.columns:
                sim = fuzz.ratio(c1.lower(), c2.lower())
                if sim > 75:
                    relationships.append({
                        "table1": t1, "column1": c1,
                        "table2": t2, "column2": c2,
                        "similarity": sim
                    })
    return relationships

def store_df_mysql(df, table_name):
    safe_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in table_name)

    dtype_map = {}
    if "document_text" in df.columns:
        dtype_map["document_text"] = LONGTEXT()

    df.to_sql(
        safe_name,
        con=engine,
        if_exists='replace',
        index=False,
        dtype=dtype_map
    )
    return safe_name

def extract_json_block(text: str) -> dict | None:
    match_fence = re.search(r"```json\s*(\{.*\})\s*```", text, re.DOTALL)
    if match_fence:
        try:
            return json.loads(match_fence.group(1))
        except:
            pass
    match = re.search(r"(\{.*\})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except:
            pass
    return None

def call_llm_unified(prompt: str) -> str:
    try:
        response = call_llm(prompt)
        return str(response).strip()
    except Exception as e:
        logging.error(f"LLM call failed: {e}")
        return f"[LLM API Error] {str(e)}"

# ------------------------ ChromaDB Vector Store ------------------------
def push_to_vector_db(uploaded_files, nodes, edges, session_id):
    collection = chroma_client.get_or_create_collection(
        name=f"session_{session_id}",
        metadata={"hnsw:space": "cosine"}
    )

    documents = []
    embeddings = []
    ids = []

    # 1. File names
    for f in uploaded_files:
        text = f.filename
        emb = embedding_model.encode(text).tolist()
        documents.append(text)
        embeddings.append(emb)
        ids.append(str(uuid.uuid4()))

    # 2. Node schemas
    for n in nodes:
        cols = n['props'].get('columns', [])
        text = f"Node {n['label']} — columns: {', '.join(cols)}"
        emb = embedding_model.encode(text).tolist()
        documents.append(text)
        embeddings.append(emb)
        ids.append(str(uuid.uuid4()))

    # 3. Edge relationships
    for e in edges:
        if not all(k in e for k in ("table1", "column1", "table2", "column2")):
            continue
        text = (
            f"Edge {e['table1']}.{e['column1']} ≈ "
            f"{e['table2']}.{e['column2']} "
            f"(similarity: {e.get('similarity', 0)})"
        )
        emb = embedding_model.encode(text).tolist()
        documents.append(text)
        embeddings.append(emb)
        ids.append(str(uuid.uuid4()))

    collection.add(
        documents=documents,
        embeddings=embeddings,
        ids=ids
    )

# ------------------------ Insights ------------------------
def generate_insights(session, more=False):
    schema_context = ""
    for t in session["tables"]:
        try:
            df = pd.read_sql(f"SELECT * FROM `{t}` LIMIT 5", engine)
            schema_context += f"\nTable {t} (sample 5 rows):\n{df.to_markdown(index=False)}\n"
        except:
            continue

    rel_context = "\n".join([
        f"{r['table1']}.{r['column1']} ≈ {r['table2']}.{r['column2']}"
        for r in session["relationships"]
    ])

    insight_type = "more diverse and deeper" if more else "basic and useful"

    prompt = f"""
You are a data analysis assistant.

Tables:
{schema_context}

Relationships:
{rel_context}

Generate {insight_type} insights. Bullet points only.
"""
    return call_llm_unified(prompt)

# ------------------------ Upload Controller (Admin Only Logic) ------------------------
def upload_files_controller():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    session_name = request.form.get("session_name") or request.json.get("session_name")
    if not session_name:
        return jsonify({"error": "Session name is required"}), 400

    session_id = str(uuid.uuid4())
    dfs = {}

    for file in files:
        name = file.filename.rsplit('.', 1)[0]
        content = file.read()
        df = None 

        file_extension = file.filename.split('.')[-1].lower()

        if file_extension == "csv":
            df = pd.read_csv(io.BytesIO(content))
        elif file_extension == "xlsx":
            df = pd.read_excel(io.BytesIO(content))
        elif file_extension == "json":
            df = pd.read_json(io.BytesIO(content))
        elif file_extension == "pdf":
            try:
                reader = pypdf.PdfReader(io.BytesIO(content))
                full_text = []
                for page in reader.pages:
                    full_text.append(page.extract_text() or "")
                document_text = "\n".join(full_text)
                if document_text.strip():
                    df = pd.DataFrame([{"document_text": document_text}])
                else:
                    return jsonify({"error": f"PDF file {file.filename} empty."}), 400
            except Exception as e:
                return jsonify({"error": f"Failed to read PDF: {str(e)}"}), 400
        else:
            return jsonify({"error": f"Unsupported file: {file.filename}"}), 400

        # Store to MySQL
        table_name = f"{name}_{uuid.uuid4().hex[:8]}"
        safe_table_name = store_df_mysql(df, table_name)
        dfs[safe_table_name] = df

    relationships = detect_relationships(dfs)

    sessions[session_id] = {
        "tables": list(dfs.keys()),
        "relationships": relationships,
        "chat_history": [],
        "last_insights": None
    }

    # Save session metadata
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor()
        sql = """
        INSERT INTO session_tracking (session_name, session_id, tables, relationships)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE tables=VALUES(tables), relationships=VALUES(relationships)
        """
        cursor.execute(sql, (session_name, session_id,
                             json.dumps(list(dfs.keys())),
                             json.dumps(relationships)))
        conn.commit()
        cursor.close()
        conn.close()
    except:
        pass

    # Push to ChromaDB
    push_to_vector_db(
        uploaded_files=files,
        nodes=[{"label": t, "props": {"columns": list(df.columns)}} for t, df in dfs.items()],
        edges=relationships,
        session_id=session_id
    )

    return jsonify({
        "session_id": session_id,
        "tables": list(dfs.keys()),
        "relationships": relationships
    })

def is_message_safe(user_message: str) -> bool:
    prompt = f"""
You are a safety classifier.
Evaluate the user's message and classify it as either: SAFE or UNSAFE.

Return ONLY ONE WORD: SAFE or UNSAFE.
User message: "{user_message}"
"""
    result = call_llm_unified(prompt).strip().upper()
    return result == "SAFE"

# ------------------------ USER CONTEXT HELPERS ------------------------
def get_user_profile(user_id):
    if not user_id: return "Unknown User Profile"
    profile_summary = "=== USER PERSONALIZATION DATA ===\n"
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        user_query = "SELECT full_name, age, gender, work, health, emotional_state, relationship FROM users WHERE user_id = %s"
        cursor.execute(user_query, (user_id,))
        user_row = cursor.fetchone()
        if user_row:
            for k, v in user_row.items():
                profile_summary += f"{k}: {v}\n"
        
        survey_query = "SELECT q.question_text, ur.answer_value FROM user_responses ur JOIN questions q ON ur.question_id = q.question_id WHERE ur.user_id = %s"
        cursor.execute(survey_query, (user_id,))
        survey_rows = cursor.fetchall()
        if survey_rows:
            profile_summary += "\n--- Detailed Survey Responses ---\n"
            for row in survey_rows:
                profile_summary += f"- {row['question_text']}: {row['answer_value']}\n"
        cursor.close()
        conn.close()
        return profile_summary
    except Exception as e:
        logging.error(f"Error fetching user profile: {e}")
        return "Error fetching profile."

def get_long_term_history(user_id, limit=5):
    if not user_id: return ""
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        query = "SELECT user_input, model_response FROM conversation_history WHERE user_id = %s ORDER BY created_at DESC LIMIT %s"
        cursor.execute(query, (user_id, limit))
        rows = cursor.fetchall()
        if not rows: return ""
        history_text = "Previous Conversation History (Context):\n"
        for row in reversed(rows):
            history_text += f"User: {row['user_input']}\nAI: {row['model_response']}\n---\n"
        cursor.close()
        conn.close()
        return history_text
    except Exception as e:
        logging.error(f"Error fetching history: {e}")
        return ""

# ================= HELPER: Clean Text for Voice =================
def clean_text_for_voice(text: str) -> str:
    """Strips Markdown and visual artifacts for natural speech."""
    if not text: return ""
    # Remove bold/italic markers (* or _)
    text = re.sub(r"[\*_]{1,2}", "", text)
    # Remove code blocks
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # Remove headers (###)
    text = re.sub(r"#+\s", "", text)
    # Remove links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Normalize whitespace
    return " ".join(text.split())

# ================= UPDATED CONTROLLER =================
def rag_chat_controller():
    data = request.get_json()
    
    # [NEW] Check for voice flag
    is_voice = data.get("is_voice", False)
    
    user_id = data.get("user_id")
    user_message = data.get("query", "").strip()
    
    # Support multiple active sessions OR single session_id
    active_session_names = data.get("active_sessions", []) 
    session_id_single = data.get("session_id") 

    # --- Step 1: Resolve Target Sessions from DB ---
    target_sessions = [] # List of dicts: {id, tables, relationships, name}
    
    conn = mysql.connector.connect(**MYSQL_CONFIG)
    cursor = conn.cursor(dictionary=True)

    if active_session_names and isinstance(active_session_names, list):
        # Fetch multiple sessions based on names
        format_strings = ','.join(['%s'] * len(active_session_names))
        cursor.execute(f"SELECT * FROM session_tracking WHERE session_name IN ({format_strings})", tuple(active_session_names))
        rows = cursor.fetchall()
        for row in rows:
            target_sessions.append({
                "id": row["session_id"],
                "tables": json.loads(row["tables"]),
                "relationships": json.loads(row["relationships"]),
                "name": row["session_name"]
            })
    elif session_id_single:
        # Fetch single session
        cursor.execute("SELECT * FROM session_tracking WHERE session_id=%s", (session_id_single,))
        row = cursor.fetchone()
        if row:
            target_sessions.append({
                "id": row["session_id"],
                "tables": json.loads(row["tables"]),
                "relationships": json.loads(row["relationships"]),
                "name": row["session_name"]
            })
    
    cursor.close()
    conn.close()

    if not target_sessions:
        return jsonify({"error": "No valid active sessions found. Please select a session."}), 404

    # --- Step 2: Handle Empty Query (Generate Insights) ---
    if not user_message:
        # Logic to generate insights from multiple sessions
        schema_context = ""
        rel_context = ""
        total_sample_tables = 0
        
        for session in target_sessions:
            schema_context += f"\n--- Session: {session['name']} ---\n"
            for t in session['tables']:
                if total_sample_tables < 6: # Limit samples to keep prompt light
                    try:
                        df = pd.read_sql(f"SELECT * FROM `{t}` LIMIT 3", engine)
                        schema_context += f"Table {t} (Sample Data):\n{df.to_markdown(index=False)}\n"
                        total_sample_tables += 1
                    except:
                        schema_context += f"Table {t} (Schema Only)\n"
                else:
                    schema_context += f"Table {t}\n"
            
            for r in session['relationships']:
                if isinstance(r, dict):
                    t1 = r.get('table1', r.get('source', ''))
                    c1 = r.get('column1', '')
                    t2 = r.get('table2', r.get('target', ''))
                    c2 = r.get('column2', '')
                    rel_context += f"{t1}.{c1} ≈ {t2}.{c2} (Similarity: {r.get('similarity',0)})\n"
                else:
                    rel_context += f"{r}\n"

        prompt = f"""
        You are 'SoulBuddy', a helpful data assistant.
        The user has selected the following Active Sessions: {', '.join([s['name'] for s in target_sessions])} but hasn't asked a specific question yet.

        DATA OVERVIEW:
        {schema_context}

        RELATIONSHIPS:
        {rel_context}

        Please generate 3 interesting insights or summary points based on this data to get the conversation started.
        Be concise and welcoming.
        """
        
        insights = call_llm_unified(prompt)
        
        # [NEW] Clean insights if voice mode is requested on empty query (rare but possible)
        if is_voice:
            insights = clean_text_for_voice(insights)

        return jsonify({
            "answer": insights,
            "source": "auto_generated_insights",
            "context_used": []
        })

    # --- Step 3: Handle Normal Chat (If Query Exists) ---
    
    # Safety Gate
    if not is_message_safe(user_message):
        return jsonify({"answer": "Sorry, I can't help with that topic."}), 200

    # Fetch User Context
    user_profile_context = get_user_profile(user_id)
    user_history_context = get_long_term_history(user_id)
    full_user_context = f"{user_profile_context}\n{user_history_context}"

    # Aggregate Metadata
    all_tables = []
    all_relationships = []
    for s in target_sessions:
        all_tables.extend(s["tables"])
        all_relationships.extend(s["relationships"])

    # Multi-Session Vector Search
    query_emb = embedding_model.encode(user_message).tolist()
    aggregated_docs = []
    
    for session in target_sessions:
        try:
            collection_name = f"session_{session['id']}"
            collection = chroma_client.get_collection(name=collection_name)
            results = collection.query(query_embeddings=[query_emb], n_results=3)
            
            if results.get("documents") and results.get("distances"):
                for i, doc in enumerate(results["documents"][0]):
                    dist = results["distances"][0][i]
                    aggregated_docs.append({
                        "text": doc,
                        "distance": dist,
                        "source_session": session["name"]
                    })
        except Exception:
            continue

    # Sort aggregated results
    aggregated_docs.sort(key=lambda x: x["distance"])
    top_docs = aggregated_docs[:5]
    
    RELEVANCE_THRESHOLD = 0.60
    is_relevant = False
    context_text = ""
    
    if top_docs and top_docs[0]["distance"] < RELEVANCE_THRESHOLD:
        is_relevant = True
        context_text = "\n".join([f"[Source: {d['source_session']}] {d['text']}" for d in top_docs])
    
    # LLM Execution
    final_answer = ""
    context_used = [d['text'] for d in top_docs]

    # [NEW] DEFINE VOICE VS TEXT PERSONAS
    if is_voice:
        persona_instructions = """
        You are "SoulBuddy", a warm, spoken voice assistant (like Alexa).
        1. Answer in 1-2 short, conversational sentences.
        2. DO NOT read lists, IDs, or table rows. Summarize them.
        3. No Markdown (no bold, no headers).
        4. Be supportive and brief.
        """
    else:
        persona_instructions = """
        You are "SoulBuddy", a helpful data assistant.
        1. Provide detailed, structured answers.
        2. Use Markdown lists or tables where helpful.
        """

    if is_relevant:
        # --- PATH A: Answer from Dataset ---
        schema_context = ""
        for t in all_tables[:10]: 
            try:
                df = pd.read_sql(f"SELECT * FROM `{t}` LIMIT 0", engine)
                schema_context += f"Table {t}: {', '.join(df.columns)}\n"
            except: pass

        rel_context_lines = []
        for r in all_relationships:
            if isinstance(r, dict):
                t1 = r.get('table1', r.get('source', ''))
                c1 = r.get('column1', '')
                t2 = r.get('table2', r.get('target', ''))
                c2 = r.get('column2', '')
                rel_context_lines.append(f"{t1}.{c1} ≈ {t2}.{c2}")
            else:
                rel_context_lines.append(str(r))
        rel_context = "\n".join(rel_context_lines)

        llm_prompt = f"""
{persona_instructions}

{full_user_context}

CONTEXT FROM SELECTED SESSIONS ({', '.join([s['name'] for s in target_sessions])}):
{context_text}

AVAILABLE TABLES:
{schema_context}

RELATIONSHIPS:
{rel_context}

USER QUESTION: {user_message}

INSTRUCTIONS:
1. Answer the question using the Dataset Context from the active sessions.
2. If the question compares two sessions, use the source tags.
3. If specific data rows are needed, generate a JSON block: {{"tool": "run_sql_query", "query": "SELECT ..."}}
4. Personalize the answer based on the User Profile.

Respond with the answer or the JSON block.
"""
        llm_response = call_llm_unified(llm_prompt)
        tool_call = extract_json_block(llm_response)

        if tool_call and tool_call.get("tool") == "run_sql_query":
            sql_query = tool_call.get("query")
            try:
                df = pd.read_sql(sql_query, engine)
                if df.empty:
                    final_answer = "I checked the data but found no matching records."
                else:
                    records = df.head(5).to_dict(orient="records")
                    
                    # [NEW] Voice-aware summarization of SQL results
                    if is_voice:
                        enhance_prompt = f"""
                        User asked: {user_message}
                        SQL Results: {records}
                        User Profile: {user_profile_context}
                        
                        Summarize these findings in ONE warm, spoken sentence.
                        """
                    else:
                        enhance_prompt = f"""
                        User Question: {user_message}
                        SQL Results: {records}
                        User Profile: {user_profile_context}
                        Summarize these results naturally.
                        """
                    
                    final_answer = call_llm_unified(enhance_prompt)
            except Exception as e:
                final_answer = f"Error querying data: {str(e)}"
        else:
            final_answer = llm_response.strip()

        source_tag = "dataset_enhanced_multi_session"

    else:
        # --- PATH B: General Knowledge ---
        mistral_prompt = f"""
{persona_instructions}
{full_user_context}
The user asked: "{user_message}"
We checked the active sessions ({', '.join([s['name'] for s in target_sessions])}) but found NO relevant info.
Answer using general knowledge. STRICTLY personalize based on the User Profile.
"""
        final_answer = call_llm_unified(mistral_prompt).strip()
        source_tag = "mistral_general"

    # [NEW] Final Polish for Voice
    if is_voice:
        final_answer = clean_text_for_voice(final_answer)

    # Log to History
    if user_id:
        try:
            conn = mysql.connector.connect(**MYSQL_CONFIG)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO conversation_history (user_id, user_input, model_response) VALUES (%s, %s, %s)", (user_id, user_message, final_answer))
            conn.commit()
            cursor.close()
            conn.close()
        except: pass

    return jsonify({
        "answer": final_answer,
        "source": source_tag,
        "context_used": context_used,
        "is_voice": is_voice
    })

