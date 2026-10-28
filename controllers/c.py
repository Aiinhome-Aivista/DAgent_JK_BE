import re
import io
import json
import uuid
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
embedding_model = SentenceTransformer("all-MiniLM-L6-v2")


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
    df.to_sql(safe_name, con=engine, if_exists='replace', index=False)
    return safe_name


def extract_sql_from_response(response_text: str):
    text = re.sub(r"```sql|```", "", response_text, flags=re.IGNORECASE)
    text = re.split(r"\*\*SQL Query:\*\*|\*\*Summary:\*\*", text, flags=re.IGNORECASE)[0]
    return text.strip()


def call_llm_unified(prompt: str) -> str:
    try:
        response = call_llm(prompt)
        return str(response).strip()
    except Exception as e:
        logging.error(f"LLM call failed: {e}")
        return f"[LLM API Error] {str(e)}"


# ------------------------ ChromaDB Vector Store ------------------------
def push_to_vector_db(uploaded_files, nodes, edges, session_id):
    """
    Store embeddings into ChromaDB.
    """
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
        if isinstance(e, dict):
            t1 = e.get('table1', e.get('source', ''))
            c1 = e.get('column1', '')
            t2 = e.get('table2', e.get('target', ''))
            c2 = e.get('column2', '')
            text = f"{t1}.{c1} ≈ {t2}.{c2} (Similarity: {e.get('similarity', 0)})"
        else:
            text = str(e)
        emb = embedding_model.encode(text).tolist()
        documents.append(text)
        embeddings.append(emb)
        ids.append(str(uuid.uuid4()))

    collection.add(
        documents=documents,
        embeddings=embeddings,
        ids=ids
    )

    try:
        chroma_client.persist()
    except AttributeError:
        pass

    print(f"✅ ChromaDB updated for session {session_id}")


# ------------------------ JSON Extraction ------------------------
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


# ------------------------ Chat History Helpers (MySQL) ------------------------  # NEW
def save_chat_message(session_id: str, role: str, message: str):
    """
    Save a single chat message (user or assistant) into MySQL.
    """
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO session_chat_history (session_id, role, message)
            VALUES (%s, %s, %s)
            """,
            (session_id, role, message)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to save chat message: {e}")


def get_chat_history(session_id: str, limit: int = 8):
    """
    Fetch last N messages for this session from MySQL, oldest → newest.
    """
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT role, message
            FROM session_chat_history
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (session_id, limit)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        # reverse to oldest → newest
        rows.reverse()
        return rows
    except Exception as e:
        logging.error(f"Failed to load chat history: {e}")
        return []


# ------------------------ Insights ------------------------
def generate_insights(session, more=False):
    schema_context = ""
    for t in session["tables"]:
        df = pd.read_sql(f"SELECT * FROM `{t}` LIMIT 5", engine)
        schema_context += f"\nTable {t} (sample 5 rows):\n{df.to_markdown(index=False)}\n"

    rel_context_lines = []
    for r in session["relationships"]:
        if isinstance(r, dict):
            t1 = r.get('table1', r.get('source', ''))
            c1 = r.get('column1', '')
            t2 = r.get('table2', r.get('target', ''))
            c2 = r.get('column2', '')
            rel_context_lines.append(f"{t1}.{c1} ≈ {t2}.{c2}")
        else:
            rel_context_lines.append(str(r))
    rel_context = "\n".join(rel_context_lines)

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


# ------------------------ File Upload Controller ------------------------
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
        df = None  # Initialize df

        file_extension = file.filename.split('.')[-1].lower()

        # --- Tabular Data Handling ---
        if file_extension == "csv":
            df = pd.read_csv(io.BytesIO(content))
        elif file_extension == "xlsx":
            df = pd.read_excel(io.BytesIO(content))
        elif file_extension == "json":
            df = pd.read_json(io.BytesIO(content))

        # --- START: PDF Data Handling ---
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
                    return jsonify({"error": f"PDF file {file.filename} contained no readable text."}), 400

            except Exception as e:
                return jsonify({"error": f"Failed to read PDF file {file.filename}: {str(e)}"}), 400
        # --- END: PDF Data Handling ---

        else:
            return jsonify({"error": f"Unsupported file: {file.filename}"}), 400

        table_name = f"{name}_{uuid.uuid4().hex[:8]}"
        safe_table_name = store_df_mysql(df, table_name)
        dfs[safe_table_name] = df

    relationships = detect_relationships(dfs)

    sessions[session_id] = {
        "tables": list(dfs.keys()),
        "relationships": relationships,
        "chat_history": [],     # not really used anymore but kept for backward compat
        "last_insights": None
    }

    # Save to MySQL session_tracking
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
    except Exception as e:
        logging.error(f"Failed to save session_tracking: {e}")

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
    """
    LLM-only safety classifier.
    The model determines whether the message is safe or unsafe.
    """

    prompt = f"""
You are a safety classifier.

Evaluate the user's message and classify it as either:
- SAFE
- UNSAFE

A message is UNSAFE if it involves or asks about ANY of the following:

 Irrelevant / not allowed topics:
- weather, sports, movies, entertainment, cooking, travel, astrology, jokes
- general knowledge not related to uploaded data

 Harmful or dangerous:
- self-harm, suicide, harming others, violence, weapons
- drug usage or creation, bomb instructions, illegal activities
- hacking, cyber attacks, bypassing security, malware

 Sensitive or restricted:
- sexual content, adult content, dating, relationships
- extremist content, hate speech, racism, harassment
- political persuasion or influencing voters
- medical diagnosis or health advice
- collecting personal data (PII), identity tracking

 Ethical / safety risks:
- impersonation, faking documents, fraud
- plagiarism, writing exam answers

Return ONLY ONE WORD:
SAFE or UNSAFE.

User message: "{user_message}"
"""

    result = call_llm_unified(prompt).strip().upper()
    return result == "SAFE"


def rag_chat_controller():
    data = request.get_json()
    session_id = data.get("session_id")
    user_message = data.get("query", "").strip()

    # ---- LLM Safety Gate ----
    if not is_message_safe(user_message):
        return jsonify({
            "answer": "Sorry, I can't help with that topic.",
            "sql_executed": None,
            "context_used": []
        }), 200

    if not session_id:
        return jsonify({"error": "Session ID missing"}), 400

    # Load session
    if session_id not in sessions:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM session_tracking WHERE session_id=%s", (session_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return jsonify({"error": "Session not found"}), 404

        sessions[session_id] = {
            "tables": json.loads(row["tables"]),
            "relationships": json.loads(row["relationships"]),
            "chat_history": [],      # kept but not used
            "last_insights": None
        }

    session = sessions[session_id]

    # Save user message to DB (if not empty)                            # NEW
    if user_message:
        save_chat_message(session_id, "user", user_message)

    # CASE 1: No query → insights (no user text actually)
    if not user_message:
        insights = generate_insights(session)
        # Optionally store assistant message as well
        save_chat_message(session_id, "assistant", insights)            # NEW
        return jsonify({"answer": insights})

    # CASE 2: "more insights"
    if re.search(r"\bmore\s+insights\b", user_message, re.IGNORECASE):
        insights = generate_insights(session, more=True)
        save_chat_message(session_id, "assistant", insights)            # NEW
        return jsonify({"answer": insights})

    # Intent classification
    intent_prompt = f"""
Classify the user's intent strictly as one of:

- "data_query" → The user is asking about information that requires reading database tables, aggregating values, filtering, joining, or anything that depends on stored data.
- "general_knowledge" → The user is asking about market trends, concepts, definitions, opinions, comparisons, advice, or anything answerable without database access.

Important:
- If the question can be answered without SQL, classify it as "general_knowledge".
- Only classify as "data_query" when SQL is *necessary* to answer.

User: "{user_message}"
Tables: {session['tables']}
Return only one label.
"""

    intent = call_llm_unified(intent_prompt).strip().lower()

    # -------- CASE: General Knowledge --------
    if "general_knowledge" in intent:
        # Load last N messages from DB instead of in-memory history       # CHANGED
        history_rows = get_chat_history(session_id, limit=8)
        history = ""
        for row in history_rows:
            prefix = "User" if row["role"] == "user" else "AI"
            history += f"{prefix}: {row['message']}\n"

        conversation_prompt = f"""
You are a helpful and knowledgeable assistant. You have determined that the following question is **general knowledge** and **does not require database access**.
Use your internal, general knowledge to provide a comprehensive answer, maintaining context from the history.

History:
{history}

User: {user_message}

Answer:
"""
        answer = call_llm_unified(conversation_prompt).strip()

        # Save assistant reply to DB                                       # NEW
        save_chat_message(session_id, "assistant", answer)

        return jsonify({"answer": answer, "sql_executed": None, "context_used": []})

    # -------- CASE: Data Query (RAG + SQL Agent) --------
    collection = chroma_client.get_or_create_collection(name=f"session_{session_id}")
    query_emb = embedding_model.encode(user_message).tolist()
    results = collection.query(query_embeddings=[query_emb], n_results=5)

    context_docs = results["documents"][0] if results["documents"] else []
    context_text = "\n".join(context_docs) if context_docs else "No relevant documents found."

    # Build schema + relationships context
    schema_context = ""
    for t in session["tables"]:
        try:
            df = pd.read_sql(f"SELECT * FROM `{t}` LIMIT 0", engine)
            schema_context += f"Table {t}: {', '.join(df.columns)}\n"
        except:
            pass

    rel_context = "\n".join([
        f"{r['table1']}.{r['column1']} ≈ {r['table2']}.{r['column2']}"
        for r in session["relationships"]
    ])

    llm_prompt = f"""
You are a data analysis assistant.

RULES (STRICT):
1. You MUST return SQL ONLY inside a JSON block: {{"tool":"run_sql_query","query":"..."}}
2. You MUST NOT include SQL anywhere else in your answer.
3. You MUST use SQL ONLY if the user's question cannot be answered without reading data from tables.
4. If the question can be answered without SQL, respond with a normal answer.
5. NEVER include SQL in normal text.
6. If you are unsure, DO NOT generate SQL.

Schema:
{schema_context}

Relationships:
{rel_context}

Retrieved context:
{context_text}

User question: {user_message}

Respond STRICTLY following the rules above.
"""

    llm_response = call_llm_unified(llm_prompt)
    tool_call = extract_json_block(llm_response)

    sql_query = None
    answer = ""

    if tool_call and tool_call.get("tool") == "run_sql_query":
        sql_query = tool_call.get("query")
        try:
            df = pd.read_sql(sql_query, engine)
            if df.empty:
                fallback_prompt = f"""
The SQL query returned no results for the user's question.

User question: {user_message}
Use the retrieved context and general knowledge to answer accurately.
Context: {context_text}
"""
                answer = call_llm_unified(fallback_prompt)
            else:
                records = df.head(5).to_dict(orient="records")
                summarize_prompt = f"""
Summarize the SQL query result in 2–4 sentences and answer the user query.

User question: {user_message}
SQL Result (top 5 rows): {records}
Context: {context_text}
"""
                answer = call_llm_unified(summarize_prompt)
        except Exception as e:
            fallback_prompt = f"""
SQL query failed with error: {str(e)}

User question: {user_message}
Use the retrieved context and general knowledge to answer accurately.
Context: {context_text}
"""
            answer = call_llm_unified(fallback_prompt)
    else:
        # No SQL needed, answer directly with context
        answer = llm_response.strip()

    # Save assistant answer to DB                                          # NEW
    save_chat_message(session_id, "assistant", answer)

    return jsonify({"answer": answer, "sql_executed": sql_query, "context_used": context_docs})
