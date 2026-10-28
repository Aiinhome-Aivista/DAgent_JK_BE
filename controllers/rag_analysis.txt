
# pyrefly: ignore [missing-import]
# controllers/session_rag_chat_controller.py
# ADVANCED HYBRID RAG
#
# Architecture:
#   1. Query Understanding  — extract intent, entities, table hints
#   2. Hybrid Retrieval     — BM25 keyword + semantic vector search + metadata filter
#   3. Re-ranking           — cross-encoder reranking (ms-marco-MiniLM-L-6-v2)
#   4. Answer Generation    — strict grounding, deep business analysis
#
# Optimizations applied:
#   - Global SentenceTransformer model (loaded once at startup)
#   - Global CrossEncoder model (loaded once at startup)
#   - Embedding cache for repeated queries
#   - Batch embedding for multiple queries
#   - Duplicate chunk deduplication before ranking
#   - Cross-encoder reranking top-40 → keep best 20
#   - MAX_CTX_CHARS reduced to 15000 for faster LLM
#   - Auto chat history save after every answer

# controllers/session_rag_chat_controller.py
# ADVANCED HYBRID RAG
#
# Architecture:
#   1. Query Understanding  — extract intent, entities, table hints
#   2. Hybrid Retrieval     — BM25 keyword + semantic vector search + metadata filter
#   3. Re-ranking           — cross-encoder reranking (ms-marco-MiniLM-L-6-v2)
#   4. Answer Generation    — strict grounding, deep business analysis
#
# Optimizations applied:
#   - Global SentenceTransformer model (loaded once at startup)
#   - Global CrossEncoder model (loaded once at startup)
#   - Embedding cache for repeated queries
#   - Batch embedding for multiple queries
#   - Duplicate chunk deduplication before ranking
#   - Cross-encoder reranking top-40 → keep best 20
#   - MAX_CTX_CHARS reduced to 15000 for faster LLM
#   - Auto chat history save after every answer

import re, json, time, hashlib, math, requests, mysql.connector, threading, os
from collections import defaultdict

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("[RAG] psycopg2 not installed - PostgreSQL support disabled")

# pyrefly: ignore [missing-import]
from flask import request, jsonify
from database.config import MISTRAL_API_KEY, MISTRAL_MODEL, MYSQL_CONFIG
from database.prompt_loader import get_prompt
from controllers.kgraph_service import (
    load_kgraph, build_sql_rules, resolve_grouping, detect_drilldown, validate_sql
)
from controllers.intent_router import classify_intent
from controllers.query_branches import execute_hybrid
from helper.email_action_handler import is_email_action_request, execute_email_action
try:
    from utils.logger_service import log_system_error
except Exception:
    def log_system_error(*args, **kwargs): pass
# ChromaDB persistent storage — vectors survive server restarts
CHROMA_PERSIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chroma_store"
)
os.makedirs(CHROMA_PERSIST_DIR, exist_ok=True)

MISTRAL_URL        = "https://api.mistral.ai/v1/chat/completions"
MAX_ROWS           = 300
TOP_K              = 400
CACHE_TTL          = 600
MAX_CTX_CHARS      = 50000

_CACHE   = {}
_CLIENTS = {}
_LOCKS   = {}

_FOLLOWUP_CYCLE = ["What", "Where", "Why"]
_TURN_COUNTER   = {}

def _next_followup_type(session_id):
    turn = _TURN_COUNTER.get(session_id, 0)
    return _FOLLOWUP_CYCLE[turn % 3]

def _advance_turn(session_id):
    _TURN_COUNTER[session_id] = _TURN_COUNTER.get(session_id, 0) + 1

def _followup_instruction(ftype):
    instructions = {
        "What": (
            "follow_up_questions: Generate exactly 5 questions, ALL starting with 'What '. "
            "Focus on WHAT specific data, values, counts, or details exist in the business data."
        ),
        "Where": (
            "follow_up_questions: Generate exactly 5 questions, ALL starting with 'Where '. "
            "Focus on WHERE data comes from, where patterns exist, where in the database."
        ),
        "Why": (
            "follow_up_questions: Generate exactly 5 questions, ALL starting with 'Why '. "
            "Focus on WHY patterns exist, why certain data looks the way it does, business reasoning."
        )
    }
    return instructions.get(ftype, instructions["What"])

GRAPH_KW  = {"graph","chart","plot","visualize","visualise","bar","pie","line","histogram","scatter"}
REPORT_KW = {"report","summary report","generate report","make a report","create a report","write a report"}
GREET_RE  = re.compile(r'^\s*(hi+|hello+|hey+|howdy|greetings|sup|yo+|hiya|good\s*(morning|afternoon|evening|night)|what\'?s\s*up)\s*[!?.]*\s*$', re.I)

def _is_graph(q):  return bool(set(q.lower().split()) & GRAPH_KW)
def _is_report(q): return any(k in q.lower() for k in REPORT_KW)
def _is_greet(q):  return bool(GREET_RE.match(q.strip()))


# ══════════════════════════════════════════════════════
# OPTIMIZATION 1: Global models loaded once at startup
# ══════════════════════════════════════════════════════

_EMBED_MODEL   = None
_CROSS_ENCODER = None
_MODEL_LOCK    = threading.Lock()

# OPTIMIZATION 2: Embedding cache
_EMBED_CACHE   = {}


def _get_embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        with _MODEL_LOCK:
            if _EMBED_MODEL is None:
                from sentence_transformers import SentenceTransformer
                print("[RAG] Loading SentenceTransformer globally...")
                _EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
                print("[RAG] SentenceTransformer ready")
    return _EMBED_MODEL


def _get_cross_encoder():
    global _CROSS_ENCODER
    if _CROSS_ENCODER is None:
        with _MODEL_LOCK:
            if _CROSS_ENCODER is None:
                from sentence_transformers.cross_encoder import CrossEncoder
                print("[RAG] Loading CrossEncoder globally...")
                _CROSS_ENCODER = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
                print("[RAG] CrossEncoder ready")
    return _CROSS_ENCODER


def _encode_texts(texts):
    """OPTIMIZATION 2+3: Cache-aware batch embedding."""
    model  = _get_embed_model()
    result = [None] * len(texts)
    uncached_idx   = []
    uncached_texts = []

    for i, t in enumerate(texts):
        if t in _EMBED_CACHE:
            result[i] = _EMBED_CACHE[t]
        else:
            uncached_idx.append(i)
            uncached_texts.append(t)

    if uncached_texts:
        new_embeds = model.encode(uncached_texts, batch_size=128, show_progress_bar=False).tolist()
        for idx, emb, txt in zip(uncached_idx, new_embeds, uncached_texts):
            _EMBED_CACHE[txt] = emb
            result[idx] = emb

    return result


# ══════════════════════════════════════════════════════
# BM25
# ══════════════════════════════════════════════════════

class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.N = len(docs)
        self.tokenized = [self._tok(d) for d in docs]
        self.avgdl = sum(len(t) for t in self.tokenized) / max(self.N, 1)
        self.df = defaultdict(int)
        for td in self.tokenized:
            for w in set(td): self.df[w] += 1

    def _tok(self, text):
        return re.findall(r'\b\w+\b', text.lower())

    def score(self, query, top_k):
        q_terms = self._tok(query)
        scores  = []
        for i, td in enumerate(self.tokenized):
            tf_map = defaultdict(int)
            for w in td: tf_map[w] += 1
            s = 0.0
            for term in q_terms:
                if term not in tf_map: continue
                tf  = tf_map[term]
                idf = math.log((self.N - self.df[term] + 0.5) / (self.df[term] + 0.5) + 1)
                den = tf + self.k1 * (1 - self.b + self.b * len(td) / self.avgdl)
                s  += idf * (tf * (self.k1 + 1)) / den
            if s > 0: scores.append((i, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


# ══════════════════════════════════════════════════════
# DATA LOADERS
# ══════════════════════════════════════════════════════

def _local_conn(fn):
    c = fn()
    if not c: raise RuntimeError("Local DB failed")
    return c


def _load_all(session_id, get_fn):
    chunks = []
    chunks += _load_db(session_id, get_fn)
    chunks += _load_sheets(session_id, get_fn)
    chunks += _load_web(session_id, get_fn)
    chunks += _load_analysis_report(session_id, get_fn)
    return chunks


def _load_analysis_report(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = local.cursor(dictionary=True)
        cur.execute(
            """SELECT topic, title, url, brief FROM saved_web_results
               WHERE session_id=%s ORDER BY topic""",
            (session_id,)
        )
        web_rows = cur.fetchall()
        if web_rows:
            from collections import defaultdict as _dd
            by_topic = _dd(list)
            for r in web_rows:
                by_topic[r["topic"]].append(r)
            for topic, items in by_topic.items():
                lines = [f"[ANALYSIS_WEB] Web research topic: {topic} ({len(items)} results)"]
                for item in items:
                    lines.append(f"  Title: {item['title']}")
                    if item.get("brief"):
                        lines.append(f"  Brief: {str(item['brief'])[:400]}")
                chunks.append(_chunk("\n".join(lines), kind="analysis_web", table=topic))
            print(f"[RAG] analysis_web: {len(by_topic)} topics")
        cur.execute(
            """SELECT DISTINCT external_database, new_user_db, table_name
               FROM external_db_sync_log
               WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db!=''
               ORDER BY external_database, table_name""",
            (session_id,)
        )
        sync_rows = cur.fetchall()
        if sync_rows:
            from collections import defaultdict as _dd2
            by_db = _dd2(list)
            for r in sync_rows:
                by_db[r["external_database"]].append(r)
            for ext_db, rows in by_db.items():
                new_db = rows[0]["new_user_db"]
                tables = [r["table_name"] for r in rows if r["table_name"]]
                text = (
                    f"[ANALYSIS_DB_META] Database analyzed: {ext_db} "
                    f"(stored as: {new_db})\n"
                    f"Tables found: {', '.join(tables)}\n"
                    f"Total tables: {len(tables)}"
                )
                chunks.append(_chunk(text, kind="analysis_db_meta", db=new_db))
            print(f"[RAG] analysis_db_meta: {len(by_db)} databases")
    except Exception as e:
        print(f"[RAG] analysis_report load error: {e}")
    finally:
        if cur:   cur.close()
        if local: local.close()
    return chunks


def _load_db(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = local.cursor(dictionary=True)
        cur.execute("""SELECT DISTINCT new_user_db FROM external_db_sync_log
                       WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db!=''""",
                    (session_id,))
        dbs = [r["new_user_db"] for r in cur.fetchall()]
    finally:
        if cur:   cur.close()
        if local: local.close()

    for db in dbs:
        if not re.match(r'^\w+$', db): continue
        conn = c2 = None
        try:
            conn = mysql.connector.connect(
                host=MYSQL_CONFIG["host"], port=MYSQL_CONFIG["port"],
                user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"],
                database=db, connection_timeout=10)
            c2 = conn.cursor(dictionary=True)
            c2.execute("SHOW TABLES")
            tables   = [list(r.values())[0] for r in c2.fetchall()]
            all_rows = {}

            for t in tables:
                if not re.match(r'^\w+$', t): continue
                try:
                    c2.execute(f"SELECT * FROM `{t}` LIMIT %s", (MAX_ROWS,))
                    rows = c2.fetchall()
                    if not rows: continue
                    
                    if t == 'workspace_files':
                        for r in rows:
                            try:
                                import json
                                file_data = json.loads(r['file_data']) if isinstance(r.get('file_data'), str) else (r.get('file_data') or {})
                                structured = file_data.get('structured_content')
                                if structured:
                                    paragraphs = structured.get('paragraphs', [])
                                    if paragraphs:
                                        all_rows['Document_Paragraphs'] = paragraphs
                                    for idx, vtable in enumerate(structured.get('tables', [])):
                                        base_vname = vtable.get('table_name', f"PDF_Table_{idx+1}")
                                        vname = base_vname
                                        v_counter = 1
                                        while vname in all_rows:
                                            vname = f"{base_vname} ({v_counter})"
                                            v_counter += 1
                                        vcols = vtable.get('headers', [])
                                        
                                        # Deduplicate headers
                                        seen_cols = {}
                                        dedup_cols = []
                                        for c in vcols:
                                            base = str(c).strip() if c else "Column"
                                            if base in seen_cols:
                                                seen_cols[base] += 1
                                                dedup_cols.append(f"{base} ({seen_cols[base]})")
                                            else:
                                                seen_cols[base] = 1
                                                dedup_cols.append(base)

                                        vrows = []
                                        for row_arr in vtable.get('rows', []):
                                            row_dict = {}
                                            for i, col in enumerate(dedup_cols):
                                                row_dict[col] = row_arr[i] if i < len(row_arr) else ""
                                            vrows.append(row_dict)
                                        all_rows[vname] = vrows
                            except Exception as e:
                                print(f"Error parsing workspace_files json in RAG: {e}")
                    else:
                        all_rows[t] = rows
                except Exception as e:
                    print(f"[RAG] skip {t}: {e}")

            for t, rows in all_rows.items():
                if not rows: continue
                cols = list(rows[0].keys())
                chunks.append(_chunk(
                    f"[SCHEMA] db:{db} table:{t} columns:{','.join(cols)} total_rows:{len(rows)}",
                    db=db, table=t, kind="schema"))
                lines = [
                    f"[COUNT] db:{db} table:{t} has {len(rows)} rows total.",
                    f"Number of {t}: {len(rows)}",
                    f"Total {t} count: {len(rows)}"
                ]
                for col in cols[:10]:
                    vals = list(dict.fromkeys(
                        str(r[col]) for r in rows if r.get(col) is not None and str(r.get(col)).strip()))
                    if vals:
                        lines.append(f"All values of {col} in {t}: {', '.join(vals[:40])}")
                chunks.append(_chunk("\n".join(lines), db=db, table=t, kind="count"))
                for i, row in enumerate(rows, 1):
                    parts = " | ".join(f"{k}:{v}" for k,v in row.items()
                                       if v is not None and str(v).strip())
                    chunks.append(_chunk(f"[ROW] db:{db} table:{t} row{i}: {parts}",
                                         db=db, table=t, kind="row"))
                print(f"[RAG] {db}.{t}: {len(rows)} rows → {len(rows)+2} chunks")

            chunks += _build_joins(db, all_rows)
        except Exception as e:
            print(f"[RAG] db connect {db}: {e}")
        finally:
            if c2:   c2.close()
            if conn: conn.close()
    return chunks


def _build_joins(db, all_rows):
    chunks = []
    user_tables = [t for t in all_rows if re.search(r'\busers?\b', t, re.I)]
    for ut in user_tables:
        u_rows = all_rows[ut]
        if not u_rows: continue
        ucols  = list(u_rows[0].keys())
        id_col = next((c for c in ucols if c in ('id','user_id','uid')), ucols[0])
        nm_col = next((c for c in ucols if re.search(r'\b(name|username)\b', c, re.I)), None)
        for ur in u_rows:
            uid   = str(ur.get(id_col,"")).strip()
            uname = str(ur.get(nm_col, uid)).strip() if nm_col else uid
            if not uid: continue
            for at, a_rows in all_rows.items():
                if at == ut or not a_rows: continue
                acols   = list(a_rows[0].keys())
                ref_col = next((c for c in acols if re.search(r'\buser_id\b|\buid\b|\bauthor\b', c, re.I)), None)
                if not ref_col: continue
                acts = [r for r in a_rows if str(r.get(ref_col,"")).strip() == uid]
                if not acts: continue
                detail = " || ".join(
                    " | ".join(f"{k}:{v}" for k,v in r.items() if v is not None and str(v).strip())
                    for r in acts[:15])
                chunks.append(_chunk(
                    f"[JOIN] db:{db} user:'{uname}' (id:{uid}) from:{ut} "
                    f"has {len(acts)} record(s) in table:{at}. data: {detail}",
                    db=db, table=f"{ut}+{at}", kind="join"))
    return chunks


def _load_sheets(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = local.cursor(dictionary=True)
        cur.execute("SELECT table_name,sheet_url FROM sheet_scans WHERE session_id=%s", (session_id,))
        for sc in cur.fetchall():
            t = sc["table_name"]
            if not re.match(r'^sheet_\w+$', t): continue
            try:
                cur.execute(f"SELECT * FROM `{t}` LIMIT %s", (MAX_ROWS,))
                rows = cur.fetchall()
                if not rows: continue
                cols = [c for c in rows[0].keys() if c != "_row_id"]
                chunks.append(_chunk(f"[SCHEMA] sheet:{t} url:{sc.get('sheet_url','')} columns:{','.join(cols)} rows:{len(rows)}", table=t, kind="schema"))
                lines = [f"[COUNT] sheet:{t} has {len(rows)} rows total."]
                for col in cols[:6]:
                    vals = list(dict.fromkeys(str(r[col]) for r in rows if r.get(col) is not None))
                    lines.append(f"All values of {col}: {', '.join(vals[:20])}")
                chunks.append(_chunk("\n".join(lines), table=t, kind="count"))
                for i, row in enumerate(rows, 1):
                    parts = " | ".join(f"{k}:{v}" for k,v in row.items()
                                       if k!="_row_id" and v is not None and str(v).strip())
                    chunks.append(_chunk(f"[ROW] sheet:{t} row{i}: {parts}", table=t, kind="row"))
            except Exception as e:
                print(f"[RAG] sheet {t}: {e}")
    except Exception as e:
        print(f"[RAG] sheets: {e}")
    finally:
        if cur:   cur.close()
        if local: local.close()
    return chunks


def _load_web(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = local.cursor(dictionary=True)
        cur.execute("SELECT title,url,brief,topic FROM saved_web_results WHERE session_id=%s", (session_id,))
        for r in cur.fetchall():
            chunks.append(_chunk(
                f"[WEB] title:{r['title']} url:{r['url']} topic:{r.get('topic','')} content:{r.get('brief','')}",
                kind="web"))
    except Exception as e:
        print(f"[RAG] web: {e}")
    finally:
        if cur:   cur.close()
        if local: local.close()
    return chunks


def _chunk(text, db="", table="", kind="row"):
    return {"text": text, "db": db, "table": table, "kind": kind}


# ══════════════════════════════════════════════════════
# VECTOR STORE (ChromaDB)
# ══════════════════════════════════════════════════════

def _get_or_create_lock(session_id):
    if session_id not in _LOCKS:
        _LOCKS[session_id] = threading.Lock()
    return _LOCKS[session_id]


def _col_safe_count(col):
    try:    return col.count()
    except: return 0


def _build_store(session_id, get_fn):
    import chromadb
    lock = _get_or_create_lock(session_id)
    with lock:
        now = time.time()
        if session_id in _CACHE:
            chunks, bm25, col, ts = _CACHE[session_id]
            if now - ts < CACHE_TTL and _col_safe_count(col) > 0:
                print(f"[RAG] cache hit — {len(chunks)} chunks")
                return chunks, bm25, col
            else:
                _CACHE.pop(session_id, None)

        print(f"[RAG] building store for {session_id[:8]}...")
        all_chunks = _load_all(session_id, get_fn)
        if not all_chunks:
            return None, None, None

        # OPTIMIZATION 4: Deduplicate chunks before indexing
        seen_texts = set()
        deduped    = []
        for c in all_chunks:
            if c["text"] not in seen_texts:
                seen_texts.add(c["text"])
                deduped.append(c)
        removed = len(all_chunks) - len(deduped)
        if removed > 0:
            print(f"[RAG] deduped {removed} duplicates → {len(deduped)} unique chunks")
        all_chunks = deduped

        texts    = [c["text"] for c in all_chunks]
        bm25_idx = BM25(texts)

        # Batch encode using global model + cache
        embeds = _encode_texts(texts)

        col_name = "s_" + hashlib.md5(session_id.encode()).hexdigest()[:12]
        if session_id not in _CLIENTS:
            _CLIENTS[session_id] = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
        client = _CLIENTS[session_id]
        try: client.delete_collection(col_name)
        except: pass
        col = client.create_collection(col_name)

        for i in range(0, len(all_chunks), 500):
            b = all_chunks[i:i+500]
            col.add(
                documents  = [c["text"]  for c in b],
                embeddings = embeds[i:i+500],
                metadatas  = [{"db":c["db"],"table":c["table"],"kind":c["kind"]} for c in b],
                ids        = [f"c{i+j}" for j in range(len(b))]
            )

        _CLIENTS[session_id] = client
        _CACHE[session_id]   = (all_chunks, bm25_idx, col, now)
        print(f"[RAG] ✓ {len(all_chunks)} chunks indexed")
        return all_chunks, bm25_idx, col


# ══════════════════════════════════════════════════════
# QUERY UNDERSTANDING
# ══════════════════════════════════════════════════════

def _understand(question, all_chunks):
    q      = question.lower()
    tokens = set(re.findall(r'\b\w{3,}\b', q))
    known_tables = list(dict.fromkeys(c["table"] for c in all_chunks if c["table"]))

    table_hints = []
    for t in known_tables:
        t_parts = set(re.findall(r'\b\w{3,}\b', t.lower()))
        overlap = tokens & t_parts
        if overlap: table_hints.append((t, len(overlap)))
    table_hints.sort(key=lambda x: -x[1])
    matched_tables = [t for t,_ in table_hints[:5]]

    intent = "lookup"
    if re.search(r'\bhow\s+many\b|\bcount\b|\btotal\b|\bnumber\s+of\b|\bhow\s+much\b', q):
        intent = "count"
    elif re.search(r'\blist\b|\ball\b|\beveryone\b|\bnames?\b|\bshow\s+(me\s+)?all\b', q):
        intent = "list"
    elif re.search(r'\bwho\b|\bwhich\s+user\b|\bwhose\b', q):
        intent = "who"
    elif re.search(r'\bcreated\s+by\b|\bbelongs?\s+to\b|\bby\s+whom\b|\bowned\s+by\b', q):
        intent = "join"
    elif re.search(r'\bwhat\s+is\b|\bwhat\s+are\b|\btell\s+me\b|\bfind\b|\bget\b', q):
        intent = "lookup"

    entities = re.findall(r"'([^']+)'|\"([^\"]+)\"", question)
    entities = [e[0] or e[1] for e in entities]

    queries = [question]
    if intent == "count" and matched_tables:
        for t in matched_tables[:3]:
            queries += [f"COUNT {t} total rows", f"Number of {t}", f"how many {t}", f"[COUNT] {t}"]
    elif intent == "list" and matched_tables:
        for t in matched_tables[:2]:
            queries += [f"All values of", f"list all {t}", f"[COUNT] {t}"]
    elif intent == "join":
        queries += ["[JOIN]"] + [f"user '{e}'" for e in entities]
    elif entities:
        queries += [f"'{e}'" for e in entities] + [f"{e}" for e in entities]

    if not matched_tables:
        for t in known_tables:
            for tok in tokens:
                if tok in t.lower() and len(tok) > 3:
                    matched_tables.append(t); break
        matched_tables = list(dict.fromkeys(matched_tables))[:5]

    return {
        "intent":      intent,
        "table_hints": matched_tables,
        "entities":    entities,
        "queries":     list(dict.fromkeys(queries))
    }


# ══════════════════════════════════════════════════════
# HYBRID RETRIEVAL (BM25 + Vector + Cross-Encoder Rerank)
# ══════════════════════════════════════════════════════

def _retrieve(all_chunks, bm25_idx, col, question, understanding):
    intent   = understanding["intent"]
    hints    = understanding["table_hints"]
    queries  = understanding["queries"]
    entities = understanding["entities"]

    scores = defaultdict(float)

    # BM25
    for q in queries:
        for idx, s in bm25_idx.score(q, top_k=80):
            scores[idx] += s * 1.0

    # OPTIMIZATION 3: Batch encode all queries at once
    q_embeds = _encode_texts(queries)
    per_q    = max(8, TOP_K // len(queries))
    seen     = set()
    for q_emb in q_embeds:
        n = min(per_q, _col_safe_count(col))
        if n == 0: continue
        res = col.query(query_embeddings=[q_emb], n_results=n,
                        include=["documents","metadatas"])
        for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
            key = doc[:100]
            if key in seen: continue
            seen.add(key)
            for i, c in enumerate(all_chunks):
                if c["text"][:100] == key:
                    scores[i] += 2.0; break

    # Boosts
    for i, c in enumerate(all_chunks):
        if c["table"] in hints:                               scores[i] += 5.0
        if intent == "count" and c["kind"] == "count":        scores[i] += 8.0
        elif intent == "list" and c["kind"] == "count":       scores[i] += 6.0
        elif intent == "join" and c["kind"] == "join":        scores[i] += 8.0
        for ent in entities:
            if ent.lower() in c["text"].lower():              scores[i] += 4.0

    ranked = sorted(scores.items(), key=lambda x: -x[1])

    forced = {i for i, c in enumerate(all_chunks)
              if c["kind"] == "count" and c["table"] in hints}

    candidate_indices = list(forced)
    for i, _ in ranked:
        if i not in forced: candidate_indices.append(i)
        if len(candidate_indices) >= TOP_K: break

    # OPTIMIZATION 5: Cross-encoder reranking top-300 → keep best 250
    RERANK_TOP  = 300
    RERANK_KEEP = 250
    rerank_pool = candidate_indices[:RERANK_TOP]

    if len(rerank_pool) > RERANK_KEEP:
        try:
            ce_model  = _get_cross_encoder()
            pairs     = [(question, all_chunks[i]["text"][:512]) for i in rerank_pool]
            ce_scores = ce_model.predict(pairs)
            reranked  = sorted(zip(rerank_pool, ce_scores), key=lambda x: -x[1])
            forced_in = [i for i in rerank_pool if i in forced]
            reranked_nf = [i for i, _ in reranked if i not in forced]
            rerank_pool = forced_in + reranked_nf[:RERANK_KEEP]
            candidate_indices = rerank_pool + candidate_indices[RERANK_TOP:]
            print(f"[RAG] cross-encoder reranked {RERANK_TOP} → kept {len(rerank_pool)}")
        except Exception as e:
            print(f"[RAG] cross-encoder skipped: {e}")

    parts, total = [], 0
    for i in candidate_indices:
        txt   = all_chunks[i]["text"]
        if total + len(txt) > MAX_CTX_CHARS: break
        parts.append(txt)
        total += len(txt)

    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════
# AUTO CHAT HISTORY SAVE
# ══════════════════════════════════════════════════════

# One-time flag — server startup এ একবার UNIQUE drop করবে
_UNIQUE_FIXED = False

def _ensure_no_unique_on_chat_id(get_fn):
    """chat_id column এ UNIQUE constraint থাকলে drop করো (once per server start)."""
    global _UNIQUE_FIXED
    if _UNIQUE_FIXED:
        return
    conn = cur = None
    try:
        conn = get_fn()
        cur  = conn.cursor()
        # Check if UNIQUE index named 'chat_id' exists
        cur.execute("""
            SELECT INDEX_NAME FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'session_chat_history'
              AND INDEX_NAME   = 'chat_id'
              AND NON_UNIQUE   = 0
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            cur.execute("ALTER TABLE session_chat_history DROP INDEX chat_id")
            conn.commit()
            print("[History] ✓ UNIQUE index on chat_id dropped — multiple rows per chat_id now allowed")
        _UNIQUE_FIXED = True
    except Exception as e:
        print(f"[History] unique-fix skipped: {e}")
        _UNIQUE_FIXED = True  # Don't retry on error
    finally:
        if cur:  cur.close()
        if conn: conn.close()


_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS session_chat_history (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    session_id          VARCHAR(100) NOT NULL,
    user_id             INT          NOT NULL,
    turn_index          INT          NOT NULL DEFAULT 0,
    visit_number        INT          NOT NULL DEFAULT 1,
    local_turn_index    INT          NOT NULL DEFAULT 0,
    question            TEXT         NOT NULL,
    answer              LONGTEXT     NOT NULL,
    follow_up_questions JSON         DEFAULT NULL,
    intent              VARCHAR(50)  DEFAULT NULL,
    mode                VARCHAR(30)  DEFAULT 'answer',
    login_token         VARCHAR(255) DEFAULT NULL,
    chat_id             VARCHAR(64)  DEFAULT NULL,
    visualizations      JSON         DEFAULT NULL,
    created_at          DATETIME     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session      (session_id),
    INDEX idx_user         (user_id),
    INDEX idx_session_user (session_id, user_id),
    INDEX idx_chat_id      (chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# SQL to drop UNIQUE constraint on chat_id if it exists (run once)
_FIX_UNIQUE_SQL = """
ALTER TABLE session_chat_history
DROP INDEX chat_id
"""

# ══════════════════════════════════════════════════
# CHAT_ID MANAGER
# ══════════════════════════════════════════════════

def _resolve_chat_id(get_fn, session_id, user_id, source_chat_id, force_new=False, visit_number=None):
    """
    CASE A — source_chat_id আছে + same day → same chat_id continue
    CASE B — source_chat_id আছে + different day → new chat_id + copy source history
    CASE C — source_chat_id নেই + today's chat exists → continue today's chat (unless force_new=True)
    CASE C — source_chat_id নেই + no today's chat → brand new chat_id
    """
    import uuid
    from datetime import date

    conn = cur = None
    try:
        conn = get_fn()
        cur  = conn.cursor(dictionary=True)
        cur.execute(_HISTORY_TABLE_SQL)
        today = date.today()

        if force_new:
            new_id = uuid.uuid4().hex[:32]
            print(f"[ChatID] force_new=True → brand new {new_id[:8]}...")
            return new_id, True

        if visit_number:
            cur.execute("""
                SELECT chat_id FROM session_chat_history
                WHERE session_id = %s AND user_id = %s AND visit_number = %s
                ORDER BY id DESC LIMIT 1
            """, (session_id, int(user_id), int(visit_number)))
            v_row = cur.fetchone()
            if v_row and v_row["chat_id"]:
                print(f"[ChatID] Found by visit_number {visit_number} → {v_row['chat_id'][:8]}...")
                return v_row["chat_id"], False

        if source_chat_id:
            cur.execute("""
                SELECT chat_id, created_at FROM session_chat_history
                WHERE chat_id = %s AND user_id = %s
                ORDER BY id DESC LIMIT 1
            """, (source_chat_id, int(user_id)))
            src = cur.fetchone()

            if not src:
                new_id = uuid.uuid4().hex[:32]
                print(f"[ChatID] source not found → new {new_id[:8]}...")
                return new_id, True

            if src["created_at"].date() == today:
                # CASE A
                print(f"[ChatID] CASE A same day → {source_chat_id[:8]}...")
                return source_chat_id, False

            # CASE B — new day
            new_id = uuid.uuid4().hex[:32]
            print(f"[ChatID] CASE B new day → {new_id[:8]}... (from {source_chat_id[:8]}...)")
            cur.execute("""
                SELECT session_id, user_id, question, answer,
                       follow_up_questions, intent, mode, login_token, visualizations
                FROM session_chat_history
                WHERE chat_id = %s AND user_id = %s ORDER BY turn_index ASC
            """, (source_chat_id, int(user_id)))
            for i, r in enumerate(cur.fetchall()):
                cur.execute("""
                    INSERT INTO session_chat_history
                        (session_id,user_id,turn_index,visit_number,local_turn_index,
                         question,answer,follow_up_questions,intent,mode,
                         login_token,chat_id,visualizations,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """, (r["session_id"] or session_id, int(user_id), i, 1, i,
                      r["question"], r["answer"], r["follow_up_questions"],
                      r["intent"], r["mode"], r["login_token"],
                      new_id, r["visualizations"]))
            conn.commit()
            print(f"[ChatID] copied history → {new_id[:8]}...")
            return new_id, True

        # CASE C — no source_chat_id
        if not force_new:
            cur.execute("""
                SELECT chat_id FROM session_chat_history
                WHERE session_id = %s AND user_id = %s AND DATE(created_at) = %s
                ORDER BY id DESC LIMIT 1
            """, (session_id, int(user_id), today.isoformat()))
            today_row = cur.fetchone()

            if today_row and today_row.get("chat_id"):
                print(f"[ChatID] CASE C today exists → {today_row['chat_id'][:8]}...")
                return today_row["chat_id"], False

        new_id = uuid.uuid4().hex[:32]
        print(f"[ChatID] CASE C brand new → {new_id[:8]}...")
        return new_id, True

    except Exception as e:
        print(f"[ChatID] error: {e}")
        return uuid.uuid4().hex[:32], True
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _save_history(get_fn, session_id, user_id, question, answer,
                  follow_ups, intent, mode,
                  login_token=None, visualizations=None, chat_id=None):
    if not user_id: return
    from datetime import datetime
    _ensure_no_unique_on_chat_id(get_fn)  # drop UNIQUE once if needed
    conn = cur = None
    try:
        conn = get_fn()
        cur  = conn.cursor(dictionary=True)

        eid  = chat_id or session_id

        cur.execute("""
            SELECT turn_index, visit_number, local_turn_index, created_at, login_token
            FROM session_chat_history
            WHERE chat_id = %s AND user_id = %s
            ORDER BY id DESC LIMIT 1
        """, (eid, int(user_id)))
        last = cur.fetchone()

        if last:
            turn   = last["turn_index"] + 1
            diff   = datetime.now() - last["created_at"]
            new_v  = (login_token is not None and last["login_token"] != login_token) or diff.total_seconds() > 3600
            visit  = last["visit_number"] + (1 if new_v else 0)
            local  = 0 if new_v else last["local_turn_index"] + 1
        else:
            turn = local = 0
            cur.execute("""
                SELECT COALESCE(MAX(visit_number), 0) AS max_v
                FROM session_chat_history
                WHERE session_id = %s AND user_id = %s
            """, (session_id, int(user_id)))
            max_v_row = cur.fetchone()
            visit = (max_v_row["max_v"] + 1) if max_v_row else 1

        cur.execute("""
            INSERT INTO session_chat_history
                (session_id,user_id,turn_index,visit_number,local_turn_index,
                 question,answer,follow_up_questions,intent,mode,
                 login_token,chat_id,visualizations)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (session_id, int(user_id), turn, visit, local,
              question, answer,
              json.dumps(follow_ups)     if follow_ups     else None,
              intent or None, mode, login_token, eid,
              json.dumps(visualizations) if visualizations else None))
        conn.commit()
        print(f"✅ [History] turn={turn} visit={visit} local={local} chat={eid[:8]}...")
    except Exception as e:
        print(f"❌ [History] save error: {e}")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
    return visit

# ══════════════════════════════════════════════════
# SYSTEM PROMPT
# ══════════════════════════════════════════════════

SYS = """You are a senior data analyst and database expert with deep analytical reasoning capabilities.
You have access to the user's actual database records as retrieved chunks.

Chunk types:
  [SCHEMA]           — table structure, column names, total row count
  [COUNT]            — exact row counts AND all distinct values per column — PRIMARY source for counts/lists
  [ROW]              — individual database records with all field values
  [JOIN]             — pre-computed cross-table joins: user X has N records in table Y with details
  [WEB]              — saved web content (raw)
  [ANALYSIS_WEB]     — web research grouped by topic with titles and summaries
  [ANALYSIS_DB_META] — database metadata: which databases and tables were analyzed

DEEP ANALYSIS RULES:
1. Read EVERY chunk exhaustively before forming your answer. Pay STRICT ATTENTION to the exact year, date, or context associated with each value.
2. STRICT DATA INTEGRITY: DO NOT hallucinate or swap values between different rows, years, or entities. Ensure absolute alignment.
3. For COUNT questions: find [COUNT] chunk with "Number of X: N" — this is authoritative.
4. For LIST questions: find [COUNT] chunk "All values of column_name:" — gives complete list.
5. For JOIN/relationship questions: find [JOIN] chunks — they show cross-table activity per user.
6. For WHY questions: analyze patterns, dates, sequences, frequencies across chunks to infer reasons.
7. For TREND questions: compare timestamps, sequences, values across [ROW] chunks.
8. For COMPARISON questions: pull data from multiple tables and compare side by side.
9. For DEEP questions: combine ROW + JOIN + COUNT chunks to give comprehensive multi-part answers.
10. NEVER say "I could not find" if ANY relevant data exists — dig deeper into chunks.
11. Always answer in full sentences with specifics — no vague responses.
12. DO NOT include source citations in the answer text — keep answer clean.
13. follow_up_questions MUST follow the EXACT format specified in the user prompt.
14. Respond ONLY in valid JSON."""

from model.llm_client import call_llm_chat

def _mistral(system, user, retries=2, temperature=0.15):
    if len(user) > 60000:
        print("[LLM] prompt is very large, but proceeding without arbitrary truncation to preserve JSON instructions.")
        
    messages = [
        {"role":"system","content":system},
        {"role":"user","content":user}
    ]
    
    for attempt in range(retries + 1):
        try:
            response = call_llm_chat(messages, json_mode=True, temperature=temperature)
            if response:
                return json.loads(response)
            return None
        except Exception as e:
            print(f"[LLM] error attempt {attempt+1}: {e}")
            if attempt == retries: return None
            import time
            time.sleep(1)
    return None


def _history(raw):
    if not raw or not isinstance(raw, list): return ""
    lines = [f"{str(t.get('role','user')).capitalize()}: {str(t.get('content',''))}"
             for t in raw[-6:] if isinstance(t, dict)]
    return ("Chat history:\n" + "\n".join(lines) + "\n\n") if lines else ""


def _to_str(val):
    if isinstance(val, str): return val
    if isinstance(val, dict):
        lines = []
        for k, v in val.items():
            if isinstance(v, list):
                lines.append(f"{k}:")
                for item in v:
                    if isinstance(item, dict):
                        lines.append("  • " + " | ".join(f"{ik}: {iv}" for ik, iv in item.items()))
                    else:
                        lines.append(f"  • {item}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)
    if isinstance(val, list):
        lines = []
        for item in val:
            if isinstance(item, dict):
                lines.append("• " + " | ".join(f"{k}: {v}" for k, v in item.items()))
            else:
                lines.append(f"• {item}")
        return "\n".join(lines)
    return str(val) if val else ""


SYS = """ """
# """
# You are a senior data analyst and database expert with deep analytical reasoning capabilities.
# You have access to the user's actual database records as retrieved chunks.

# Chunk types:
#   [SCHEMA]           — table structure, column names, total row count
#   [COUNT]            — exact row counts AND all distinct values per column — PRIMARY source for counts/lists
#   [ROW]              — individual database records with all field values
#   [JOIN]             — pre-computed cross-table joins: user X has N records in table Y with details
#   [WEB]              — saved web content (raw)
#   [ANALYSIS_WEB]     — web research grouped by topic with titles and summaries
#   [ANALYSIS_DB_META] — database metadata: which databases and tables were analyzed

# DEEP ANALYSIS RULES:
# 1. Read EVERY chunk exhaustively before forming your answer.
# 2. For COUNT questions: find [COUNT] chunk with "Number of X: N" — this is authoritative.
# 3. For LIST questions: find [COUNT] chunk "All values of column_name:" — gives complete list.
# 4. For JOIN/relationship questions: find [JOIN] chunks — they show cross-table activity per user.
# 5. For WHY questions: analyze patterns, dates, sequences, frequencies across chunks to infer reasons.
# 6. For TREND questions: compare timestamps, sequences, values across [ROW] chunks.
# 7. For COMPARISON questions: pull data from multiple tables and compare side by side.
# 8. For DEEP questions: combine ROW + JOIN + COUNT chunks to give comprehensive multi-part answers.
# 9. CRITICAL: If the requested data (e.g. specific columns or metrics) does NOT exist in the context, clearly state that it is unavailable. NEVER hallucinate or invent fake names, metrics, or records.
# 10. Always answer in full sentences with specifics — no vague responses.
# 11. DO NOT include source citations in the answer text — keep answer clean.
# 12. follow_up_questions MUST follow the EXACT format specified in the user prompt.
# 13. Respond ONLY in valid JSON.
# """


# ══════════════════════════════════════════════════════
# SCHEMA GROUNDING — column ownership index + join keys
# ══════════════════════════════════════════════════════
# The SQL model used to *guess* which table a column lived on (e.g. it would
# write `invoice`.`construction` when `construction` actually only exists on a
# dimension table), causing MySQL error 1054. These helpers turn that guess
# into a lookup: we parse the real [SCHEMA] lines and tell the model exactly
# which table owns each column, plus the likely join keys to pull a column in
# from another table. Data-driven, so it works for any schema.

# Generic/structural names that are NOT meaningful join keys even if shared.
_GENERIC_JOIN_COLS = {
    "id", "sl", "sno", "srno", "sr", "no", "index", "idx", "row", "rownum",
    "date", "created_at", "updated_at", "createddate", "timestamp", "ts",
    "month", "year", "day", "status", "type", "name", "description", "value",
}


def _parse_schema_chunks(schema_chunks):
    """Return {table_name: [original_col, ...]} parsed from the [SCHEMA] lines.

    The [SCHEMA] line lists the FULL column set (it is built from row.keys()),
    so it is authoritative. As a safety net we also harvest column names from
    the '[COUNT] ... table:T' / 'All values of C in T:' lines, so a table is
    still indexed even if its [SCHEMA] line is somehow missing from context."""
    table_cols = {}

    def _add(table, cols):
        if not table:
            return
        existing = table_cols.setdefault(table, [])
        seen = {c.lower() for c in existing}
        for c in cols:
            if c and c.lower() not in seen:
                existing.append(c)
                seen.add(c.lower())

    for text in schema_chunks:
        # current table context for "All values of C in T" lines
        for line in str(text).splitlines():
            if "[SCHEMA]" in line:
                m_tbl = re.search(r'(?:table|sheet):(\S+)', line)
                m_cols = re.search(r'columns:(.*?)\s+(?:total_rows|rows):', line)
                if not m_cols:
                    m_cols = re.search(r'columns:(.+)$', line)
                if m_tbl and m_cols:
                    cols = [c.strip() for c in m_cols.group(1).split(',') if c.strip()]
                    _add(m_tbl.group(1), cols)
            else:
                # Fallback: "All values of <col> in <table>: ..."
                mv = re.search(r'All values of (.+?) in (\S+?):', line)
                if mv:
                    _add(mv.group(2), [mv.group(1).strip()])
    return table_cols


def _build_schema_grounding(schema_chunks):
    """Build an authoritative column-location index + join-key hints string
    that is injected into the SQL prompt so the model never mis-attributes a
    column to the wrong table."""
    table_cols = _parse_schema_chunks(schema_chunks)
    if not table_cols:
        return "", {}

    # column (lowercased) -> set of (table, original_col)
    col_to_tables = {}
    for table, cols in table_cols.items():
        for c in cols:
            col_to_tables.setdefault(c.lower(), set()).add((table, c))

    lines = [
        "COLUMN LOCATION INDEX (authoritative). A column may be referenced ONLY "
        "on a table whose list below includes it. If a column you need is not in "
        "the table you are selecting FROM, JOIN the table that owns it.",
    ]
    for table, cols in table_cols.items():
        lines.append(f"- `{table}` owns: " + ", ".join(f"`{c}`" for c in cols))

    # Columns unique to one table are always safe to reference there.
    # Columns shared across tables are the likely join keys.
    join_hints = set()
    for col, locs in col_to_tables.items():
        tabs = sorted({t for t, _ in locs})
        if col in _GENERIC_JOIN_COLS or len(tabs) < 2 or len(tabs) > 4:
            continue
        for i in range(len(tabs)):
            for j in range(i + 1, len(tabs)):
                a, b = tabs[i], tabs[j]
                ca = next(oc for (t, oc) in locs if t == a)
                cb = next(oc for (t, oc) in locs if t == b)
                join_hints.add(f"- `{a}`.`{ca}` = `{b}`.`{cb}`")

    out = "\n".join(lines)
    if join_hints:
        out += ("\n\nLIKELY JOIN KEYS (shared columns — use these to bring a "
                "column in from another table):\n" + "\n".join(sorted(join_hints)))
    return out, col_to_tables


def _unknown_column_hint(error_msg, col_to_tables):
    """If a MySQL 1054 'Unknown column X.Y' error occurred, return a targeted
    correction telling the model which table actually owns column Y."""
    if not col_to_tables:
        return ""
    m = re.search(r"Unknown column '([^']+)'", str(error_msg))
    if not m:
        return ""
    ref = m.group(1)
    col = ref.split(".")[-1].strip("`")            # T.C or just C
    owners = sorted({t for t, _ in col_to_tables.get(col.lower(), set())})
    if not owners:
        return (f"\n\nIMPORTANT: column `{col}` does not exist anywhere in the "
                f"schema. Do not reference it; use only columns from the COLUMN "
                f"LOCATION INDEX.")
    owner_list = ", ".join(f"`{t}`" for t in owners)
    return (f"\n\nIMPORTANT: column `{col}` does NOT exist on the table you "
            f"referenced it on. It exists ONLY on: {owner_list}. Reference "
            f"`{col}` on one of those tables, JOINing it in via a LIKELY JOIN "
            f"KEY if needed.")


def _validate_column_refs(sql, table_cols):
    """Statically check a generated SQL string for column references that point
    at the WRONG base table (the cause of MySQL 1054). Returns a de-duplicated
    list of (qualifier, column) violations.

    Only references whose qualifier is a KNOWN BASE TABLE are checked. CTE names
    and short aliases (wd, cs, i, d, ...) are NOT base tables, so references like
    `wd`.`dealer` are correctly ignored — we cannot and should not validate
    derived columns."""
    if not sql or not table_cols:
        return []
    lower_cols = {t: {c.lower() for c in cols} for t, cols in table_cols.items()}
    base_tables = set(table_cols.keys())
    violations = {}

    # Backticked  `table`.`column`
    for m in re.finditer(r"`([^`]+)`\s*\.\s*`([^`]+)`", sql):
        q, c = m.group(1), m.group(2)
        if q in base_tables and c.lower() not in lower_cols[q]:
            violations[(q, c)] = True

    # Unbackticked  table.column  (qualifier still must be a real base table)
    for m in re.finditer(r"\b(\w+)\s*\.\s*(\w+)\b", sql):
        q, c = m.group(1), m.group(2)
        if q in base_tables and c.lower() not in lower_cols[q]:
            violations[(q, c)] = True

    return list(violations.keys())
# ─────────────────────────────────────────────
# VISUALIZATION SUPPORT
# ─────────────────────────────────────────────

def _normalize_visualizations(viz_list):
    if not isinstance(viz_list, list):
        return []
    normalized = []
    for v in viz_list:
        if not isinstance(v, dict): continue
        vtype = str(v.get("type","")).lower()
        if vtype in ("bar","barchart","bar-chart"):   vtype = "bar_chart"
        elif vtype in ("line","linechart"):            vtype = "line_chart"
        elif vtype in ("pie","piechart"):              vtype = "pie_chart"
        elif vtype in ("table","grid"):                vtype = "table"
        item = {"type": vtype, "title": v.get("title","")}
        if vtype in ("bar_chart","line_chart"):
            item["xKey"] = v.get("xKey","")
            item["yKey"] = v.get("yKey","")
            item["data"] = v.get("data",[])
        elif vtype == "pie_chart":
            item["data"] = v.get("data",[])
        elif vtype == "table":
            item["columns"] = v.get("columns",[])
            item["data"]    = v.get("data",[])
        normalized.append(item)
    return normalized


def _column_ref_correction(violations, col_to_tables):
    """Build a forceful correction message naming each wrong reference and the
    table that actually owns the column."""
    lines = []
    for (q, c) in violations:
        owners = sorted({t for t, _ in col_to_tables.get(c.lower(), set())})
        if owners:
            owner_list = ", ".join(f"`{o}`" for o in owners)
            lines.append(
                f"- `{q}`.`{c}` is INVALID: `{c}` is NOT a column of `{q}`. "
                f"`{c}` exists ONLY on {owner_list}. JOIN that table using a "
                f"LIKELY JOIN KEY and reference `{c}` there."
            )
        else:
            lines.append(
                f"- `{q}`.`{c}` is INVALID: column `{c}` does not exist in any "
                f"table. Remove it / use only columns from the COLUMN LOCATION INDEX."
            )
    return ("Your previous SQL referenced columns on the WRONG table. This will "
            "fail with error 1054. Fix EVERY issue below and return the same JSON "
            "shape:\n" + "\n".join(lines))


# ══════════════════════════════════════════════════════
# BUSINESS SCHEMA MAP  (drill-down + correct dimension joins + word→column)
# ══════════════════════════════════════════════════════
# Fixes three failure modes that produced wrong numbers:
#   1. Product attributes (CATEGORY/CONSTRUCTION/vehicle type) were joined from
#      the WRONG table (a customer-keyed dealer table), causing fan-out and
#      inflated sums. We pin them to the real product dimension joined on
#      Material, and forbid any other source.
#   2. "vehicle" / "vehicle type" was mapped to CATEGORY. We map words to the
#      exact column.
#   3. Drill-down: "top 3 tyre categories" => WHERE CATEGORY='Tyre' GROUP BY the
#      next level (CONSTRUCTION).
#   4. [PATCH] region/zone were falsely claimed as customer_master columns and
#      were resolvable via an ungoverned fallback scan straight to
#      region_master with no join path — this produced `cm.zone` in generated
#      SQL (MySQL error 1054). Geography is now handled by its own dedicated
#      GEOGRAPHY_CHAIN + _build_geography_prompt(), never by DIMENSIONS or the
#      generic synonym fallback.
#
# IMPORTANT: edit the CONFIG below to match your real schema. Tables are matched
# by the columns they contain (robust to munged table names), so you usually
# only need to keep the column/measure names correct.

# ── DYNAMIC CONFIG (Fetched from DB via workspace_config) ───────────────────
# Defaults are used if the workspace_config prompt is not found or is invalid JSON.
# ── END DYNAMIC CONFIG ────────────────────────────────────────────────────────
# ── CONFIG ───────────────────────────────────────────────────────────────────
FACT_TABLE_HINTS = ["sales_data", "invoice"]          # name substring(s) of the fact table
MEASURE_COLUMN   = "Invoice_Value_INR"      # the Sales measure column on the fact table
DATE_COLUMN      = "billing__doc_date"      # the Date column on the fact table used for year/month filtering

# Dimensions: each is auto-located as the (non-fact) table that contains its
# `dim_key` AND the most of its `owns` columns. `fact_key` is the column on the
# fact table that joins to `dim_key` on the dimension.
DIMENSIONS = [
    {
        "label":    "product",
        "fact_key": "material",
        "dim_key":  "MATNR",
        "owns":     ["category", "tyre_type", "construction", "MAKTX", "PROD_TITLE"],
        # [PATCH] NOTE: sku_master.construction is typed BIGINT in the live
        # schema, so alphabetic construction_master codes
        # (A, B, D, E, K, L, M, N, O, P, R, T, Z) can never match. This is a
        # data-load defect upstream, not a mapping bug in this file.
    },
    {
        "label":    "customer",
        "fact_key": "customer",
        "dim_key":  "KUNNR",
        # [PATCH] "region", "zone" REMOVED — customer_master does NOT own
        # them. They live two joins away (territory_master -> region_master).
        # Claiming them here let the synonym resolver treat them as reachable
        # in a single hop, which produced the `cm.zone` bug. See
        # GEOGRAPHY_CHAIN below, which is the only place region/zone resolve.
        "owns":     ["Cname", "acc_grp", "class", "territory", "sales_office"],
    },
]

# [PATCH — NEW] Multi-hop geography chain. `_build_business_map`'s DIMENSIONS
# only support a single fact_key/dim_key hop, but region/zone require
# customer_master -> territory_master -> region_master (two hops). Resolved
# and injected separately by _build_geography_prompt(), never folded into
# DIMENSIONS or COLUMN_SYNONYMS.
GEOGRAPHY_CHAIN = {
    "customer_fk":     "territory",          # customer_master.territory (bigint)
    "territory_table": "territory_master",
    "territory_key":   "territory_code",     # text
    "territory_owns":  ["territory_name"],
    "region_fk":       "region_code",        # territory_master.region_code (text)
    "region_table":    "region_master",
    "region_key":       "region",            # bigint
    "region_owns":      ["region_name", "zone"],
}

# Product hierarchy ROOT → LEAF (column names; matched case/space/underscore-insensitively)
PRODUCT_HIERARCHY = ["category", "tyre_type", "construction", "MAKTX"]

# Natural-language phrase → exact column name. Longest phrase wins.
COLUMN_SYNONYMS = {
    "product category": "category", "category": "category", "categories": "category",
    "construction": "construction", "tyre type": "tyre_type", "tire type": "tyre_type",
    "vehicle": "category", "by vehicle": "category",
    "customer": "Cname",
    # [PATCH] "region": "region", "zone": "zone" REMOVED. Leaving them here let
    # the fallback scan resolve them to `region_master` directly, with no
    # join path — that is exactly what produced `cm.zone` in generated SQL.
    # Geography wording is now handled exclusively by GEOGRAPHY_CHAIN /
    # _build_geography_prompt(), which enforces the mandatory 2-hop join.
}

# Explicit definitions for complex business entities that require specific joins, filters, and grouping.
ENTITY_DEFINITIONS = {
    "dealer": "If the user asks for 'dealer(s)', you MUST JOIN `customer_master` and `account_group_master` (ON `customer_master`.`acc_grp` = `account_group_master`.`KTOKD`), FILTER BY `account_group_master`.`account_group_name` = 'Dealer', and GROUP BY `customer_master`.`KUNNR`, `customer_master`.`Cname`.",
    # [PATCH] class_master's real value for code DB is 'DISTRIBUTOR' (all
    # caps) — matched case-insensitively now so this never silently returns
    # zero rows depending on collation.
    "distributor": "If the user asks for 'distributor(s)', you MUST JOIN `customer_master` and `class_master` (ON `customer_master`.`class` = `class_master`.`class_code`), FILTER BY UPPER(`class_master`.`class_name`) = 'DISTRIBUTOR', and GROUP BY `customer_master`.`KUNNR`, `customer_master`.`Cname`. Do NOT use `distribution_mapping` for distributors.",
    "fleet": "If the user asks for 'fleet(s)', you MUST JOIN `customer_master` and FILTER BY `customer_master`.`acc_grp` = 'Z009', and GROUP BY `customer_master`.`KUNNR`, `customer_master`.`Cname`.",
}
# ── END CONFIG ───────────────────────────────────────────────────────────────

# Words that signal the user wants a ranked breakdown (so we apply the GROUP BY).
_RANK_OR_BREAKDOWN_RE = re.compile(
    r'\b(top|bottom|best|worst|highest|lowest|leading|poor|performing|rank|'
    r'ranking|wise|breakdown|break\s*down|split|distribution|each|per|'
    r'types?|kinds?|categor(?:y|ies)|segments?|variants?|constructions?)\b', re.I)


def _norm_ident(s):
    return re.sub(r'[\s_]+', '', str(s).lower())


def _parse_value_index(schema_chunks):
    """From '[COUNT] ...' lines 'All values of <col> in <table>: v1, v2, ...'
    return {(table, col): [distinct original values]}."""
    out = {}
    for text in schema_chunks:
        for line in str(text).splitlines():
            m = re.search(r'All values of (.+?) in (\S+?):\s*(.+)$', line)
            if not m:
                continue
            col, table, rest = m.group(1).strip(), m.group(2).strip(), m.group(3)
            vals = [v.strip() for v in rest.split(',') if v.strip()]
            if not vals:
                continue
            bucket = out.setdefault((table, col), [])
            seen = {v.lower() for v in bucket}
            for v in vals:
                if v.lower() not in seen:
                    bucket.append(v)
                    seen.add(v.lower())
    return out


def _orig_col(table_cols, table, col_name):
    """Return the original-cased column name on `table` matching col_name."""
    for c in table_cols.get(table, []):
        if _norm_ident(c) == _norm_ident(col_name):
            return c
    return None


def _build_business_map(table_cols, workspace_id):
    """Resolve the CONFIG against the actually-loaded schema. Returns a dict with
    the fact table, each dimension's real table + join keys, the column→source map
    (so product attributes are pinned to the product table), the resolved synonym
    map, and the ordered hierarchy levels as (table, col)."""
    
    import json
    from database.prompt_loader import get_prompt
    
    # Defaults (Fallback if no config found)
    FACT_TABLE_HINTS = ["invoice"]
    MEASURE_COLUMN   = "Invoice_Value"
    DIMENSIONS = [
        {
            "label":    "product",
            "fact_key": "Material",
            "dim_key":  "Material",
            "owns":     ["CATEGORY", "CONSTRUCTION", "vehicle type", "OLD CODE"],
        },
        {
            "label":    "customer",
            "fact_key": "Customer",
            "dim_key":  "Customer",
            "owns":     ["CUSTOMER_CATEGORY", "Region", "Zone", "Account group"],
        },
    ]
    PRODUCT_HIERARCHY = ["CATEGORY", "CONSTRUCTION", "vehicle type"]
    COLUMN_SYNONYMS = {
        "product category": "CATEGORY", "category": "CATEGORY", "categories": "CATEGORY",
        "construction": "CONSTRUCTION", "tyre type": "CONSTRUCTION", "tire type": "CONSTRUCTION",
        "vehicle category": "vehicle type", "vehicle type": "vehicle type",
        "vehicle": "vehicle type", "by vehicle": "vehicle type",
        "region": "Region", "zone": "Zone",
        "dealer": "Customer", "customer": "Customer",
    }
    
    # Fetch from database
    config_json = get_prompt(workspace_id, 'workspace_config')
    if config_json and config_json.strip():
        try:
            cfg = json.loads(config_json)
            FACT_TABLE_HINTS = cfg.get("fact_table_hints", FACT_TABLE_HINTS)
            MEASURE_COLUMN = cfg.get("measure_column", MEASURE_COLUMN)
            DIMENSIONS = cfg.get("dimensions", DIMENSIONS)
            PRODUCT_HIERARCHY = cfg.get("product_hierarchy", PRODUCT_HIERARCHY)
            COLUMN_SYNONYMS = cfg.get("column_synonyms", COLUMN_SYNONYMS)
        except Exception as e:
            print(f"[RAG] Error parsing workspace_config JSON: {e}")

    norm_tables = {t: {_norm_ident(c) for c in cols} for t, cols in table_cols.items()}

    # Fact table: name hint match, else the table that has the measure column.
    fact = None
    for t in table_cols:
        if any(h in t.lower() for h in FACT_TABLE_HINTS):
            fact = t
            break
    if not fact:
        for t, ncols in norm_tables.items():
            if _norm_ident(MEASURE_COLUMN) in ncols:
                fact = t
                break

    # Locate each dimension as the non-fact table containing dim_key + most owns.
    dim_resolved = []          # list of {table, fact_key, dim_key, owns:[orig...]}
    attr_source = {}           # norm(col) -> (table, orig_col, fact_key, dim_key)
    for d in DIMENSIONS:
        dk = _norm_ident(d["dim_key"])
        owns_norm = [_norm_ident(c) for c in d["owns"]]
        best, best_score = None, -1
        for t, ncols in norm_tables.items():
            if t == fact or dk not in ncols:
                continue
            score = sum(1 for c in owns_norm if c in ncols)
            if score <= 0:
                continue
            # Prefer a leaner table (true dimension) on ties.
            score = score * 100 - len(ncols)
            if any(h in t.lower() for h in [d["label"]]):
                score += 50
            if score > best_score:
                best, best_score = t, score
        if not best:
            continue
        owns_present = [_orig_col(table_cols, best, c) for c in d["owns"]
                        if _orig_col(table_cols, best, c)]
        fk = _orig_col(table_cols, fact, d["fact_key"]) or d["fact_key"]
        dkey = _orig_col(table_cols, best, d["dim_key"]) or d["dim_key"]
        dim_resolved.append({"table": best, "fact_key": fk, "dim_key": dkey,
                             "owns": owns_present, "label": d["label"]})
        for oc in owns_present:
            attr_source[_norm_ident(oc)] = (best, oc, fk, dkey)

    # Hierarchy levels resolved to (table, orig_col), pinned to their source table.
    levels = []
    for h in PRODUCT_HIERARCHY:
        nh = _norm_ident(h)
        if nh in attr_source:
            t, oc, _, _ = attr_source[nh]
            levels.append((t, oc))
        elif fact and nh in norm_tables.get(fact, set()):
            levels.append((fact, _orig_col(table_cols, fact, h)))

    # Resolve synonyms to real (table, col): prefer a pinned source, else fact.
    def _plurals(p):
        out = {p}
        out.add(p + "s")
        if p.endswith("y"):
            out.add(p[:-1] + "ies")
        return out
    syn_resolved = {}
    for phrase, colname in COLUMN_SYNONYMS.items():
        nc = _norm_ident(colname)
        loc = None
        if nc in attr_source:
            t, oc, _, _ = attr_source[nc]
            loc = (t, oc)
        elif fact and nc in norm_tables.get(fact, set()):
            loc = (fact, _orig_col(table_cols, fact, colname))

        if not loc:
            # [PATCH] Restricted fallback: only resolve to a table this map
            # already knows how to JOIN to (a dimension table already
            # resolved above, or the two geography-chain tables). Previously
            # this scanned EVERY table with no join-path check, which let
            # "region"/"zone" resolve straight to region_master with no way
            # to reach it — the root cause of the `cm.zone` bug.
            reachable = {d["table"] for d in dim_resolved}
            reachable.add(GEOGRAPHY_CHAIN["territory_table"])
            reachable.add(GEOGRAPHY_CHAIN["region_table"])
            for tbl, cols in norm_tables.items():
                if tbl in reachable and nc in cols:
                    loc = (tbl, _orig_col(table_cols, tbl, colname))
                    break

        if loc:
            for variant in _plurals(phrase.lower()):
                syn_resolved.setdefault(variant, loc)

    measure = _orig_col(table_cols, fact, MEASURE_COLUMN) if fact else None

    date_col = _orig_col(table_cols, fact, DATE_COLUMN) if fact else None

    return {
        "fact": fact, "measure": measure or MEASURE_COLUMN, "date_column": date_col or DATE_COLUMN,
        "dims": dim_resolved, "attr_source": attr_source,
        "synonyms": syn_resolved, "levels": levels,
        "entity_definitions": ENTITY_DEFINITIONS,
    }


def _business_prompt(biz):
    """Authoritative instruction block: exact dimension joins, the measure, the
    forbidden sources, and word→column mapping."""
    if not biz.get("fact"):
        return ""
    fact = biz["fact"]
    out = [f"AUTHORITATIVE SCHEMA MAP (follow EXACTLY — overrides any guess):",
           f"- Fact table: `{fact}`. Sales / performance / revenue = "
           f"SUM(`{fact}`.`{biz['measure']}`).",
           f"- Date filtering: If the user mentions a year (e.g., '2026') or date, you MUST apply a WHERE clause using `{fact}`.`{biz['date_column']}` (e.g. YEAR({fact}.{biz['date_column']}) = 2026). Do NOT use STR_TO_DATE; assume the column is already a proper DATE type."]
    forbid = []
    for d in biz["dims"]:
        if not d["owns"]:
            continue
        cols = ", ".join(f"`{c}`" for c in d["owns"])
        out.append(
            f"- {cols} live ONLY on `{d['table']}`. To use any of them you MUST "
            f"JOIN `{d['table']}` ON `{fact}`.`{d['fact_key']}` = "
            f"`{d['table']}`.`{d['dim_key']}`. NEVER read these columns from any "
            f"other table, and NEVER join them on a different key (doing so "
            f"multiplies rows and inflates the totals).")
        forbid.append(f"`{d['table']}` only via `{d['fact_key']}`")
    if biz["synonyms"]:
        # Compact, de-duplicated word→column list
        seen = {}
        for phrase, (t, c) in biz["synonyms"].items():
            seen.setdefault((t, c), []).append(phrase)
        word_lines = []
        for (t, c), phrases in seen.items():
            ph = ", ".join(f'"{p}"' for p in sorted(set(phrases), key=len))
            word_lines.append(f"  {ph} -> `{c}` (on `{t}`)")
        out.append("WORD -> COLUMN (map the user's wording to the EXACT column):\n"
                   + "\n".join(word_lines))
    out.append("FAN-OUT GUARD: every dimension JOIN must be on the key above so "
               "each fact row matches at most one dimension row. If a column name "
               "exists on more than one table, use the table named in this map.")
    
    if biz.get("entity_definitions"):
        out.append("\nBUSINESS ENTITY DEFINITIONS (Strictly follow these rules if the user mentions these entities):")
        for entity, rule in biz["entity_definitions"].items():
            out.append(f"- {rule}")
            
    return "\n".join(out)


# [PATCH — NEW] Emits the mandatory 2-hop geography join chain. This is what
# actually fixes the `cm.zone` / Unknown column bug — region/zone are simply
# never reachable in one join, and the model has no way to know that without
# being told explicitly, table by table, with the correct CAST direction.
def _build_geography_prompt(table_cols):
    """Emits a mandatory 2-hop join chain for region/zone. These are NEVER
    reachable in a single join from sales_data or customer_master — omitting
    this let the model guess an alias like `cm.zone` (MySQL error 1054)."""
    g = GEOGRAPHY_CHAIN
    norm_tables = {t: {_norm_ident(c) for c in cols} for t, cols in table_cols.items()}

    cust_tbl = None
    for t, ncols in norm_tables.items():
        if "customer" in t.lower() and _norm_ident(g["customer_fk"]) in ncols:
            cust_tbl = t
            break
    if not cust_tbl or g["territory_table"] not in table_cols or g["region_table"] not in table_cols:
        return ""

    return (
        f"\n\nGEOGRAPHY CHAIN (MANDATORY — region/zone are NEVER columns on "
        f"`{cust_tbl}` or the fact table; both joins below are required, in order):\n"
        f"1. JOIN `{g['territory_table']}` ON "
        f"CAST(`{cust_tbl}`.`{g['customer_fk']}` AS CHAR) = `{g['territory_table']}`.`{g['territory_key']}`\n"
        f"2. JOIN `{g['region_table']}` ON "
        f"`{g['territory_table']}`.`{g['region_fk']}` = CAST(`{g['region_table']}`.`{g['region_key']}` AS CHAR)\n"
        f"   -- always CAST the BIGINT side to CHAR; never CAST the text side to "
        f"UNSIGNED (silently coerces non-numeric values to 0 and breaks the join).\n"
        f"`region`, `zone`, `region_name` exist ONLY on `{g['region_table']}`, reachable "
        f"only after both joins above. `{cust_tbl}` and any alias of it (e.g. `cm`) "
        f"NEVER owns `zone` or `region` — referencing them there is INVALID."
    )


def _detect_group_columns(question, biz):
    """Map the user's wording to the column(s) they want grouped, via synonyms.
    Longest matching phrase wins on any overlapping span, so 'vehicle categories'
    resolves to `vehicle type` and suppresses the bare 'categories'→CATEGORY."""
    ql = question.lower()
    candidates = []  # (start, end, table, col, phrase)
    for phrase, (t, c) in biz.get("synonyms", {}).items():
        for m in re.finditer(r'\b' + re.escape(phrase) + r'\b', ql):
            candidates.append((m.start(), m.end(), t, c, phrase))
    # Longest phrase first; greedily accept non-overlapping spans.
    candidates.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    taken, out, seen = [], [], set()
    for s, e, t, c, p in candidates:
        if any(not (e <= ts or s >= te) for ts, te in taken):
            continue  # overlaps an already-accepted (longer) phrase
        taken.append((s, e))
        if (t, c) not in seen:
            seen.add((t, c))
            out.append((t, c, p))
    return out


def _detect_drilldown(question, values_by_col, biz):
    """If the question names a value at some hierarchy level (e.g. 'tyre'),
    return {'filter': (table,col,value), 'group': (table,col)|None}."""
    levels = biz.get("levels") or []
    if not levels:
        return None
    ql = question.lower()

    matched = None  # (level_idx, table, col, original_value)
    for idx, (t, c) in enumerate(levels):
        for v in values_by_col.get((t, c), []):
            if len(v) < 2:
                continue
            if re.search(r'\b' + re.escape(v.lower()) + r'\b', ql):
                if matched is None or len(v) > len(matched[3]):
                    matched = (idx, t, c, v)
    if not matched:
        return None
    f_idx, f_t, f_c, f_val = matched

    # Group level: explicit deeper level the user named (via synonyms or name),
    # else the next level down.
    group = None
    named = _detect_group_columns(question, biz)
    for (gt, gc, _ph) in named:
        for idx in range(f_idx + 1, len(levels)):
            if (gt, gc) == levels[idx]:
                group = (gt, gc)
                break
        if group:
            break
    if group is None and f_idx + 1 < len(levels):
        group = levels[f_idx + 1]

    return {"filter": (f_t, f_c, f_val), "group": group}


def _drilldown_hint(spec, want_breakdown, biz):
    f_t, f_c, f_val = spec["filter"]
    fact = biz.get("fact") or "invoice"
    measure = biz.get("measure") or "Invoice_Value"
    s = (f"\n\nHIERARCHY DRILL-DOWN (apply this):\n"
         f"- The question names '{f_val}', a value of `{f_c}` on `{f_t}`. Use it as "
         f"a FILTER: WHERE `{f_t}`.`{f_c}` = '{f_val}' (exact value, case included).")
    grp = spec.get("group")
    if grp and want_breakdown:
        g_t, g_c = grp
        s += (f"\n- Break it down WITHIN '{f_val}': GROUP BY `{g_t}`.`{g_c}` (the "
              f"next level down) and rank by SUM(`{fact}`.`{measure}`) DESC, applying "
              f"the requested Top/Bottom N. Do NOT group by `{f_c}` itself. JOIN the "
              f"dimension table(s) per the AUTHORITATIVE SCHEMA MAP.")
    else:
        s += "\n- Apply this filter; aggregate/rank as the question asks."
    return s


def _grouping_hint(question, biz):
    """For non-drill questions, if the wording names a dimension to break by,
    tell the model exactly which column/table to GROUP BY."""
    named = _detect_group_columns(question, biz)
    if not named or not _RANK_OR_BREAKDOWN_RE.search(question):
        return ""
    fact = biz.get("fact") or "invoice"
    lines = ["\n\nGROUP-BY MAPPING (use these EXACT columns for the breakdown):"]
    for (t, c, ph) in named:
        if t == fact:
            lines.append(f"- '{ph}' -> GROUP BY `{t}`.`{c}`.")
        else:
            lines.append(f"- '{ph}' -> GROUP BY `{t}`.`{c}` (JOIN `{t}` per the "
                         f"AUTHORITATIVE SCHEMA MAP).")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# MAIN CONTROLLER
# ══════════════════════════════════════════════════════
def _safe_visualizations(vizs):
    return [v for v in vizs if isinstance(v,dict) and v.get("type") and v.get("title")]

def session_rag_chat_controller(get_connection_func):
    data       = request.json or {}
    session_id     = (data.get("session_id")     or "").strip()
    question       = (data.get("question")       or "").strip()
    history        = data.get("chat_history", [])
    user_id        = data.get("user_id")
    visit_number   = data.get("visit_number")
    login_token    = data.get("login_token")
    source_chat_id = (data.get("source_chat_id") or "").strip() or None

    if not session_id:
        return jsonify({"status":"failed","statusCode":400,
                        "message":"session_id is required"}), 400

    if user_id:
        from controllers.pricing_controller import get_usage_and_limits
        usage_res = get_usage_and_limits(user_id)
        if usage_res.get('status') == 'success':
            queries_metric = usage_res['usage_stats']['metrics']['queries']
            used = queries_metric['used']
            total = queries_metric['total']
            if total != -1 and used >= total:
                return jsonify({"status": "error", "message": "Daily query limit exceeded based on your active plan."}), 403

    # --- FIX: Frontend might pass chat_id as session_id when navigating ---
    try:
        conn_fix = get_connection_func()
        cur_fix = conn_fix.cursor(dictionary=True)
        cur_fix.execute("SELECT id FROM workspaces WHERE session_id = %s", (session_id,))
        if not cur_fix.fetchone():
            cur_fix.execute("SELECT session_id FROM session_chat_history WHERE chat_id = %s LIMIT 1", (session_id,))
            hr = cur_fix.fetchone()
            if hr and hr.get("session_id"):
                if not source_chat_id:
                    source_chat_id = session_id
                session_id = hr["session_id"]
    except Exception:
        pass
    finally:
        if 'cur_fix' in locals() and cur_fix: cur_fix.close()
        if 'conn_fix' in locals() and conn_fix: conn_fix.close()
    # ----------------------------------------------------------------------

    # ── Resolve active chat_id ──────────────────────────────────
    active_chat_id = None
    # Support both bool and string representations
    force_new = str(data.get("is_new_query")).lower() == "true"
    if user_id:
        active_chat_id, _ = _resolve_chat_id(
            get_connection_func, session_id, user_id, source_chat_id, force_new, visit_number
        )

    workspace_id = None
    sql_results = []
    system_prompt = SYS
    try:
        conn = get_connection_func()
        cur = conn.cursor(dictionary=True)

        # 1. Get workspace_id for this session
        cur.execute("SELECT id AS workspace_id FROM workspaces WHERE session_id = %s", (session_id,))
        s_row = cur.fetchone()
        if s_row and s_row.get("workspace_id"):
            workspace_id = s_row["workspace_id"]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[RAG] Session fetch error: {e}")

    # Apply fetched prompts
    system_prompt = get_prompt(workspace_id, 'rag_chat')
    if not system_prompt or not system_prompt.strip():
        return jsonify({
            "status": "error", "statusCode": 500,
            "message": "RAG chat prompt not configured in database. Please configure it in the Admin Panel."
        }), 500

    # Greeting
    if question and _is_greet(question):
        suggested = []
        if session_id in _CACHE:
            chunks, _, _, _ = _CACHE[session_id]
            sample = "\n".join(c["text"] for c in chunks if c["kind"] == "count")[:10000]
            res = _mistral(
                "Respond ONLY in valid JSON.",
                f"Data summary:\n{sample}\n\n"
                "Generate exactly 3 questions. Q1 starts with 'What ', Q2 starts with 'Where ', Q3 starts with 'Why '. "
                "Use actual table names and values from the data. "
                'Return ONLY: {"suggested_questions":["What ...?","Where ...?","Why ...?"]}'
            )
            if res: suggested = res.get("suggested_questions", [])
        return jsonify({
            "status":"success","statusCode":200,
            "answer":"Hi! I'm your advanced business intelligence assistant. I have full access to your session databases. Ask me anything about your business data!",
            "follow_up_questions": suggested,
            "chat_id": active_chat_id
        }), 200

    # Email Action Handler
    if question and is_email_action_request(question):
        email_res = execute_email_action(
            question,
            session_id=session_id,
            user_id=int(user_id) if user_id else 1,
            get_connection_func=get_connection_func
        )
        email_res["visit_number"] = visit_number
        return jsonify(email_res), 200

    # Build/get store
    try:
        all_chunks, bm25_idx, col = _build_store(session_id, get_connection_func)
    except Exception as e:
        log_system_error(session_id, "rag_chat_store", f"Store build error: {e}", error_type="store_error")
        return jsonify({"status":"error","statusCode":500,
                        "message":f"Store error: {e}"}), 500

    if not all_chunks:
        return jsonify({"status":"no_data","statusCode":200,
                        "session_id":session_id,
                        "message":"No data found for this session."}), 200

    # Suggest mode
    if not question:
        count_chunks = [c["text"] for c in all_chunks if c["kind"]=="count"]
        sample = "\n".join(count_chunks)[:15000]
        _default_tpl = get_prompt(workspace_id, 'rag_chat_default') or ''
        _default_msg = (
            _default_tpl
            .replace('{sample}', sample)
            .replace('{len_chunks}', str(len(all_chunks)))
        )
        res = _mistral(system_prompt, _default_msg)
        res = _mistral(SYS, f"""
Business data summary ({len(all_chunks)} total chunks):
{sample}

This is a business intelligence assistant. Generate exactly 5 "What" questions about the actual business data above.
ALL 5 questions MUST start with "What ".
Focus on business-relevant insights: counts, values, names, metrics.
Reference actual table names, column names, and values from the data.

Return ONLY: {{"suggested_questions":["What ...?","What ...?","What ...?"]}}
""")
        if not res:
            return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
        return jsonify({
            "status":              "success",
            "statusCode":          200,
            "suggested_questions": res.get("suggested_questions",[]),
            "chat_id":             active_chat_id
        }), 200

    # Understand + Retrieve
    understanding = _understand(question, all_chunks)
    context       = _retrieve(all_chunks, bm25_idx, col, question, understanding)
    hist          = _history(history)
    print(f"[RAG] intent={understanding['intent']} tables={understanding['table_hints']} entities={understanding['entities']}")

    # NEW ARCHITECTURE: INTENT ROUTER 
    intent = classify_intent(question, workspace_id, get_connection_func())
    print(f"[RAG] Router classified intent: {intent}")
    context = ""

    if intent == "HYBRID":
        print("[RAG] HYBRID query detected! AQL/SQL First Then RAG...")
        schema_chunks = [c["text"] for c in all_chunks if c["kind"] in ("schema", "count")]
        schema_context = "\n".join(schema_chunks)
        # 1. AQL First: run execute_hybrid to get specific entity filter
        hybrid_entities = execute_hybrid(question, get_connection_func, schema_context)
        if hybrid_entities:
            print(f"[RAG] Hybrid found targeted entities: {hybrid_entities[:10]}")
            # Inject these entities so Vector Search focuses heavily on them
            understanding["entities"].extend(hybrid_entities)
            
        # 2. Then RAG
        context = _retrieve(all_chunks, bm25_idx, col, question, understanding)

    elif intent == "INSIGHT":
        print("[RAG] INSIGHT query detected! Standard RAG...")
        context = _retrieve(all_chunks, bm25_idx, col, question, understanding)

    # ─────────────────────────────────────────────
    # TEXT-TO-SQL (Bypass VectorDB for aggregations)
    # ─────────────────────────────────────────────
    if intent == "AGGREGATION":
        print(" Bypassing VectorDB. Routing to Structured Database (SQL/AQL)...")
        schema_chunks = [c["text"] for c in all_chunks if c["kind"] in ("schema", "count")]
        schema_context = "\n".join(schema_chunks)

        # ─────────────────────────────────────────────
        # 1. CANONICALIZATION STEP
        # ─────────────────────────────────────────────
        canon_sys = get_prompt(workspace_id, 'canonicalization')
        if not canon_sys or not canon_sys.strip():
            return jsonify({
                "status": "error", "statusCode": 500, 
                "message": "Canonicalization prompt not configured in database."
            }), 500
#         """You are a Query Canonicalizer for Business Intelligence.
# Convert the user's natural language question into a structured JSON representation (Canonical Query).
# Do not generate SQL yet. Extract the core analytical components.

# Return ONLY a JSON object in this format:
# {
#   "analytical_intent": "e.g., dealer_ranking, sales_trend, total_revenue",
#   "metric": "e.g., sales, volume, discount",
#   "aggregation": "e.g., sum, count, avg",
#   "sort": "e.g., desc, asc",
#   "limit": 5
# }
# If a component is missing from the user's question, set it to null.
# """
        canon_json = _mistral(canon_sys, f"Natural Language Question: {question}", temperature=0.0)
        canonical_query_str = json.dumps(canon_json, indent=2) if canon_json else f'{{"raw_question": "{question}"}}'
        print(f"[CANONICAL_QUERY] {canonical_query_str}")

        # ─────────────────────────────────────────────
        # 2. SQL GENERATION STEP
        # ─────────────────────────────────────────────
        sql_sys = get_prompt(workspace_id, 'sql_generation')
        if not sql_sys or not sql_sys.strip():
            return jsonify({
                "status": "error", "statusCode": 500, 
                "message": "SQL Generation prompt not configured in database."
            }), 500
#         """You are a Senior Data Analyst, SQL Expert, and Business Intelligence Assistant.

# PRIMARY OBJECTIVE

# Generate SQL that computes answers from the FULL DATASET.

# BUSINESS DEFINITIONS
# - Dealer = Customer
# - Sales = SUM(invoice_value)
# - Revenue = SUM(invoice_value)
# - Volume = SUM(qty)
# - Net Sales = SUM(invoice_value) - SUM(total_discount)
# - Invoice Count = COUNT(DISTINCT invoice_number)
# - Product = Material
# - Product Category = the `CATEGORY` column (Tyre, Tube, Flap, ...) — a PRODUCT attribute; join product ON invoice.Material = product.Material
# - Construction / "tyre type" / "tube type" = the `CONSTRUCTION` column (RADIAL, BIAS, ...) — a PRODUCT attribute on the product table (join on Material)
# - Vehicle / "vehicle type" / "vehicle category" = the `vehicle type` column (TRUCK, CAR, LCV, ...) — a PRODUCT attribute on the product table (join on Material). This is DIFFERENT from CATEGORY; never substitute one for the other.
# - These three (CATEGORY, CONSTRUCTION, `vehicle type`) are PRODUCT attributes keyed by Material. They are NEVER on the customer/dealer table; do not join them on Customer.
# - "category-wise" / "by category" / "product category wise" / "per category" => GROUP BY `CATEGORY`, NOT Material
# - "product-wise" / "by product" => GROUP BY Material
# - Top Dealer = Dealer ranked by Sales descending
# - Worst Dealer = Dealer ranked by Sales ascending
# - Best Performing Dealer = Dealer ranked by Sales descending
# - Lowest Performing Dealer = Dealer ranked by Sales ascending
# - Top Product = Product ranked by Sales descending
# - Worst Product = Product ranked by Sales ascending
# - Region Performance = SUM(invoice_value) grouped by region
# - Zone Performance = SUM(invoice_value) grouped by zone
# - Average Realization = SUM(invoice_value) / NULLIF(SUM(qty),0)

# AUTHORITATIVE SCHEMA MAP PRECEDENCE
# - If the user message contains an "AUTHORITATIVE SCHEMA MAP", a "GROUP-BY MAPPING",
#   or a "HIERARCHY DRILL-DOWN" block, those are RESOLVED FROM THE REAL SCHEMA and
#   OVERRIDE these generic definitions for table names, column ownership, joins,
#   filters and group-by. Follow them exactly.
# - FAN-OUT: a dimension table must be joined on its key so each fact row matches
#   at most one dimension row. Joining a product attribute on the wrong key (e.g.
#   Customer) multiplies rows and inflates SUM — never do it.
# BUSINESS DEFINITIONS
# - Dealer = Customer
# - Sales = SUM(invoice_value)
# - Revenue = SUM(invoice_value)
# - Volume = SUM(qty)
# - Net Sales = SUM(invoice_value) - SUM(total_discount)
# - Invoice Count = COUNT(DISTINCT invoice_number)
# - Product = Material
# - Product Category ("Tyre", "Tube", "Flap") = ALWAYS LEFT JOIN `category_master` on `sku_master.category = category_master.category_code` and select `category_name`. Do NOT just select the category code from `sku_master`.
# - Construction / "tyre type" / "tube type" ("RADIAL", "BIAS") = ALWAYS LEFT JOIN `construction_master` on `sku_master.construction = construction_master.construction_code` and select `construction_description`.
# - Vehicle / "vehicle type" / "vehicle category" ("TRUCK", "CAR") = ALWAYS LEFT JOIN `tyre_type_master` on `sku_master.tyre_type = tyre_type_master.tyre_type_code` and select `tyre_type_name`. This is DIFFERENT from CATEGORY.
# - These attributes are PRODUCT attributes keyed by Material (`sku_master`). First LEFT JOIN `sku_master` to fact, then LEFT JOIN these master tables to `sku_master`. Never join them directly on Customer.
# - "category-wise" / "by category" / "product category wise" / "per category" => GROUP BY `CATEGORY`, NOT Material
# - "product-wise" / "by product" => GROUP BY Material
# - Top Dealer = Dealer ranked by Sales descending
# - Worst Dealer = Dealer ranked by Sales ascending
# - Best Performing Dealer = Dealer ranked by Sales descending
# - Lowest Performing Dealer = Dealer ranked by Sales ascending
# - Top Product = Product ranked by Sales descending
# - Worst Product = Product ranked by Sales ascending
# - Region Performance = SUM(invoice_value) grouped by region
# - Zone Performance = SUM(invoice_value) grouped by zone
# - Average Realization = SUM(invoice_value) / NULLIF(SUM(qty),0)



# PERFORMER RESOLUTION

# - The word "performer" does NOT imply Dealer.
# - Determine the ranking entity ONLY from the user's wording.
# - If the user explicitly says "dealer", rank dealers.
# - If the user explicitly says "customer", rank customers.
# - If the user explicitly says "product", rank products.
# - If the user explicitly says "region", rank regions.
# - If the user explicitly says "zone", rank zones.
# - If the user only says "performer" without specifying an entity, default to Customer. Only use Dealer, Product, Region, Zone, etc. when the user explicitly mentions them.
# - NEVER rewrite "performer" as "dealer" unless the user explicitly uses the word "dealer".

# AUTHORITATIVE SCHEMA MAP PRECEDENCE
# - If the user message contains an "AUTHORITATIVE SCHEMA MAP", a "GEOGRAPHY CHAIN",
#   a "GROUP-BY MAPPING", or a "HIERARCHY DRILL-DOWN" block, those are RESOLVED
#   FROM THE REAL SCHEMA and OVERRIDE these generic definitions for table names,
#   column ownership, joins, filters and group-by. Follow them exactly.
# - REGION / ZONE ARE NEVER A SINGLE JOIN: if the user asks about region or zone,
#   you MUST follow the GEOGRAPHY CHAIN block exactly — it requires TWO joins
#   (customer -> territory_master -> region_master). Never reference `region` or
#   `zone` on `customer_master` or any alias of it (e.g. `cm.zone` is INVALID).
# - FAN-OUT: a dimension table must be joined on its key so each fact row matches
#   at most one dimension row. Joining a product attribute on the wrong key (e.g.
#   Customer) multiplies rows and inflates SUM — never do it.


# METRIC PRIORITY
# - Whenever user asks: "Top Dealer", "Best Dealer", "Leading Dealer" -> Use: SUM(invoice_value)
# - Whenever user asks: "Worst Dealer", "Lowest Dealer", "Poor Performing Dealer" -> Use: SUM(invoice_value)
# - Never use: qty, taxable_value, gst, discount unless explicitly requested.

# DATA RELIABILITY RULES

# 1. Use schema information only to identify:

#    * tables
#    * columns
#    * relationships

# 1b. COLUMN OWNERSHIP IS NON-NEGOTIABLE. A "COLUMN LOCATION INDEX" is provided
#    in the user message listing exactly which table owns each column. Before you
#    write any column reference (`table`.`column`), verify that column appears in
#    that table's list. NEVER reference a column on a table that does not own it
#    (this causes MySQL error 1054). If a column you need lives on a different
#    table, JOIN that table using one of the provided LIKELY JOIN KEYS. Do not
#    assume a "natural"-sounding column (e.g. a product/customer attribute) lives
#    on the fact/invoice table — check the index.

# 2. Never use example values, retrieved rows, vector chunks, sample records, or context snippets to calculate business results.

# 3. Every ranking, trend, comparison, aggregation, KPI, sales metric, customer metric, dealer metric, category metric, region metric, and performance metric MUST be computed using SQL.

# 4. For Top N or Bottom N questions:

# Return ONLY the ranking result unless the user explicitly asks for:
# - monthwise analysis
# - trend analysis
# - yearly analysis
# - time series analysis

# Do not add monthly, yearly, trend, or detailed breakdowns unless explicitly requested.

# 5. For monthwise analysis:
#    Use the actual date column and aggregate by month before ranking.

# 6. Never generate SQL that ranks monthly rows directly using:
#    LIMIT N after GROUP BY month.

# 7. If the question asks for Top N entities (e.g., dealers, customers) month-wise or trend:
#    NEVER use `IN (SELECT ... LIMIT N)` because MySQL does not support LIMIT inside IN subqueries.
#    Instead, you MUST use a JOIN with a derived table:
   
#    SELECT t.entity, DATE_FORMAT(STR_TO_DATE(t.date_col, '%Y-%m-%d'), '%Y-%m') as month, SUM(t.metric) as total_sales
#    FROM `table` t
#    JOIN (
#        SELECT entity FROM `table`
#        GROUP BY entity
#        ORDER BY SUM(metric) DESC
#        LIMIT 2
#    ) as top_entities ON t.entity = top_entities.entity
#    GROUP BY t.entity, month
#    ORDER BY top_entities.total_sales DESC, month;
# 7. If the question asks for Top N entities (e.g., dealers, customers) month-wise or trend:
#    NEVER use `IN (SELECT ... LIMIT N)` because MySQL does not support LIMIT inside IN subqueries.
#    Instead, you MUST use a LEFT JOIN with a derived table:
   
#    SELECT t.entity, DATE_FORMAT(STR_TO_DATE(t.date_col, '%Y-%m-%d'), '%Y-%m') as month, SUM(t.metric) as total_sales
#    FROM `table` t
#    LEFT JOIN (
#        SELECT entity FROM `table`
#        GROUP BY entity
#        ORDER BY SUM(metric) DESC
#        LIMIT N
#    ) as top_entities ON t.entity = top_entities.entity
#    GROUP BY t.entity, month
#    ORDER BY top_entities.total_sales DESC, month;
   
#    Adjust the DATE_FORMAT and STR_TO_DATE depending on the actual date format in the table.

# 8. Use:
#    SUM()
#    COUNT()
#    AVG()
#    MIN()
#    MAX()
#    GROUP BY
#    ORDER BY
#    HAVING
# 8. Use:
#    SUM()
#    COUNT()
#    AVG()
#    MIN()
#    MAX()
#    GROUP BY (CRITICAL: Every non-aggregated column in the SELECT clause MUST be present in the GROUP BY clause to prevent `only_full_group_by` errors.)
#    ORDER BY
#    HAVING

# 9. If SQL execution is possible:
#    SQL results are always more authoritative than retrieved context.

# 10. Never estimate.

# 11. Never infer missing values.

# 12. Never hallucinate business results.

# 13. PRESERVE EXACT DECIMALS: Never round monetary values in SQL unless explicitly asked. Return the exact sum with decimals intact.
# 13. PRESERVE EXACT DECIMALS: Never round monetary values in SQL unless explicitly asked. Return the exact sum with decimals intact.
# 14. NEGATIVE VALUES: NEVER add `> 0` or `>= 0` filters to sales or invoice columns unless the user explicitly asks to "exclude returns" or "only show positive sales". If a dealer's total sales are negative (e.g. -19022.00), that is a valid exact figure and must be included.

# COLUMN HYGIENE
# - All numeric columns (sales, invoice_value, quantity, discount, tax) are strictly typed as DECIMAL or BIGINT in the database.
# - DO NOT use CAST or REGEXP_REPLACE or REPLACE to clean numeric columns. Just use SUM(`col`).
# - ONLY format strings if the column is explicitly a string format, but numeric columns are already typed.

# PER-GROUP TOP-N — "CATEGORY-WISE", "PER", "EACH", "BY X", "X-WISE"

# - "Top N customers per category", "category wise top N", "best N per region",
#   "top N dealers for each zone" all mean: rank WITHIN each group and keep N rows
#   from EVERY group. NEVER answer these with a single global ORDER BY ... LIMIT N
#   (that returns only the N biggest pairs overall, not N per group).
# - Use a window function partitioned by the group:
#       WITH agg AS (
#         SELECT `<group_col>` AS grp, `<entity_col>` AS entity,
#                SUM(`<value_col>`) AS metric
#         FROM `<fact>` JOIN `<dim>` ON ...
#         GROUP BY `<group_col>`, `<entity_col>`
#       ),
#       ranked AS (
#         SELECT grp, entity, metric,
#                ROW_NUMBER() OVER (PARTITION BY grp ORDER BY metric DESC) AS rn
#         FROM agg
#       )
#       SELECT grp, entity, metric FROM ranked WHERE rn <= N
#       ORDER BY grp, metric DESC;
# - Use a single global ORDER BY ... LIMIT N ONLY when the question has NO
#   per-group qualifier (plain "top N customers").
# - "Top N customers per category", "category wise top N", "best N per region",
#   "top N dealers for each zone" all mean: rank WITHIN each group and keep N rows
#   from EVERY group. NEVER answer these with a single global ORDER BY ... LIMIT N
#   (that returns only the N biggest pairs overall, not N per group).
# - Use a window function partitioned by the group:
#       WITH agg AS (
#         SELECT `<group_col>` AS grp, `<entity_col>` AS entity,
#                SUM(`<value_col>`) AS metric
#         FROM `<fact>` LEFT JOIN `<dim>` ON ...
#         GROUP BY `<group_col>`, `<entity_col>`
#       ),
#       ranked AS (
#         SELECT grp, entity, metric,
#                ROW_NUMBER() OVER (PARTITION BY grp ORDER BY metric DESC) AS rn
#         FROM agg
#       )
#       SELECT grp, entity, metric FROM ranked WHERE rn <= N
#       ORDER BY grp, metric DESC;
# - Use a single global ORDER BY ... LIMIT N ONLY when the question has NO
#   per-group qualifier (plain "top N customers").

# PLAIN TOP-N vs WINDOWED TOP-N
# - A plain "top N" / "worst N" with NO per-group qualifier needs only
#   `... GROUP BY entity ORDER BY metric DESC LIMIT N`. Do NOT use a window
#   function or CTE for it — that adds a needless alias that often breaks.
# - Use the window-function pattern ONLY for per-group ("X-wise") questions.

# HIERARCHY DRILL-DOWN
# - The product data has a hierarchy (e.g. CATEGORY -> CONSTRUCTION -> VEHICLE_TYPE
#   -> ... -> MATERIAL), from broad to specific.
# - When the user NAMES A VALUE at one level (e.g. "tyre", "radial", "truck") and
#   asks for "top/worst N <something> of/within it" or any breakdown, treat the
#   named value as a FILTER (WHERE that_level = 'value') and GROUP BY the NEXT
#   level DOWN, ranking by the metric (default Sales = SUM(invoice_value)).
#   Example: "top 3 performing tyre categories" =>
#       WHERE `category` = 'Tyre'
#       GROUP BY `construction`            -- the next level below CATEGORY
#       ORDER BY SUM(`Invoice_Value`) DESC, `construction` ASC
#       LIMIT 3
#   Never GROUP BY the same level you filtered on (that returns just one row).
# - If the user explicitly names the child level ("...constructions",
#   "...vehicle types"), GROUP BY exactly that level.
# - If a HIERARCHY DRILL-DOWN block is provided in the user message, follow it
#   exactly (it tells you the filter column/value and the group-by level, both
#   resolved to real tables). JOIN across tables via the LIKELY JOIN KEYS when the
#   filter level and group level live on different tables.
# JOINS AND MISSING DIMENSIONS (CRITICAL)
# - ALWAYS use `LEFT JOIN` for ANY join to a dimension table (e.g., `customer_master`, `sku_master`, `category_master`, etc.). NEVER use an `INNER JOIN` or `JOIN` anywhere in the query when fetching dimension data, even when joining from a CTE!
# - NEVER use an `INNER JOIN` (or plain `JOIN`) that might drop valid records just because the dimension data is missing.
# - When selecting ANY name from a dimension table (whether inside a CTE or in the final MAIN query), you MUST wrap it in `COALESCE` to prevent nulls in the JSON output. 
#   Example: `SELECT COALESCE(cm.Cname, 'N/A') AS dealer_name`
# - SUPER CRITICAL BUG FIX: When grouping or selecting after a LEFT JOIN, ALWAYS use the foreign key from the FACT table (e.g., `sales_data.customer`), NEVER the primary key from the DIMENSION table (e.g., `customer_master.KUNNR`). Grouping by the dimension key will lump all unmatched records into a single NULL bucket! This applies to ALL dimension tables.

# MULTI-LEVEL BREAKDOWN ("Top/Worst N along with their X-wise breakup")
# ENTITY RESOLUTION FOR BREAKDOWN QUERIES

# - In queries of the form:
#   "Top/Bottom/Worst N performers along with <dimension>-wise sales breakup"

#   the "<dimension>-wise" phrase specifies ONLY the breakdown dimension.

# - NEVER infer the ranking entity from the breakdown dimension.

# - "product category-wise", "product construction-wise", "vehicle-wise", "region-wise", etc. describe ONLY how to split the selected entities after ranking.
# - EXCEPTION: If the user explicitly asks for "Top/Worst N <Entity> wise sales" WITHOUT another ranking entity (e.g., "worst 2 construction type wise sales"), it means you must rank the <Entity> itself. Just GROUP BY the <Entity>, ORDER BY sales, and LIMIT N. DO NOT use ROW_NUMBER() or PARTITION BY unless explicitly asked to find "per <Entity>".

# - The ranking entity must be resolved independently:
#     - dealer -> Dealer
#     - customer -> Customer
#     - product -> Product
#     - region -> Region
#     - zone -> Zone
#     - performer -> Customer (default)

# Example:
# "Worst 2 performers along with their product construction wise sales breakup"

# Correct interpretation:
# 1. Rank Customers by SUM(invoice_value) ASC.
# 2. Select the Bottom 2 Customers.
# 3. Break down each selected Customer by Product Construction.

# Incorrect interpretation:
# Rank Products because "product construction" appears in the question.

# - When asked to find the Top N or Worst N entities overall AND THEN show their breakdown (e.g., "worst 2 performers along with their product category wise sales breakup"):
#   1. FIRST, create a CTE to calculate the total aggregate (SUM) per entity and LIMIT to Top/Worst N.
#      Example: `WITH top_entities AS (SELECT entity, SUM(metric) as total FROM fact GROUP BY entity ORDER BY total DESC LIMIT N)`
#   2. THEN, create a breakdown CTE that joins the first CTE back to the fact/dimensions. YOU MUST include the `total` from the first CTE in this second CTE so it can be used for sorting later.
#      Example: `breakdown AS (SELECT wp.entity, wp.total, dim.category, SUM(fact.metric) as category_sales FROM top_entities wp LEFT JOIN fact ... GROUP BY wp.entity, wp.total, dim.category)`
#   3. FINALLY, in the main query, select the columns from the breakdown CTE.
#   4. CRITICAL: In the final main query, you MUST `ORDER BY` the `total` column (e.g. `ORDER BY breakdown.total DESC`) so that the overall Top N / Worst N sequence is preserved, followed by the category sales!
#   5. NEVER rank individual unaggregated rows using ROW_NUMBER() without summing first.
#   6. Prefer simple `ORDER BY ... LIMIT N` for direct Top/Worst queries. Avoid complex window functions like `ROW_NUMBER()` unless a nested breakdown is strictly required.
#   7. CRITICAL: MySQL 8 supports `LIMIT` inside `WITH` CTEs. DO NOT comment out the `LIMIT N` clause inside the CTE. Use `LIMIT N` directly (e.g. `LIMIT 2` and NOT `-- LIMIT 2`).

# HIERARCHY DRILL-DOWN
# - The product data has a hierarchy (e.g. CATEGORY -> CONSTRUCTION -> VEHICLE_TYPE
#   -> ... -> MATERIAL), from broad to specific.
# - When the user NAMES A VALUE at one level (e.g. "tyre", "radial", "truck") and
#   asks for "top/worst N <something> of/within it" or any breakdown, treat the
#   named value as a FILTER (WHERE that_level = 'value') and GROUP BY the NEXT
#   level DOWN, ranking by the metric (default Sales = SUM(invoice_value)).
#   Example: "top 3 performing tyre categories" =>
#       WHERE `category` = 'Tyre'
#       GROUP BY `construction`            -- the next level below CATEGORY
#       ORDER BY SUM(`Invoice_Value`) DESC, `construction` ASC
#       LIMIT 3
#   Never GROUP BY the same level you filtered on (that returns just one row).
# - If the user explicitly names the child level ("...constructions",
#   "...vehicle types"), GROUP BY exactly that level.
# - If a HIERARCHY DRILL-DOWN block is provided in the user message, follow it
#   exactly (it tells you the filter column/value and the group-by level, both
#   resolved to real tables). JOIN across tables via the LIKELY JOIN KEYS when the
#   filter level and group level live on different tables.

# RESERVED WORDS — NEVER USE AS ALIASES
# - `RANK`, `ROW_NUMBER`, `ORDER`, `GROUP`, `DESC`, `ASC`, `ROWS`, `RANGE`,
#   `COUNT`, `SUM`, `OVER`, `PARTITION`, `DENSE_RANK`, `LAG`, `LEAD` are reserved
#   in MySQL 8.0 and will cause error 1064 if used as a column alias.
# - Name the row-number column `rn` (never `rank`). Backtick EVERY alias and
#   identifier without exception.
# DETERMINISTIC ORDERING — MANDATORY TIE-BREAKER
# - Many entities can tie on the same total (e.g. several customers at 0 sales).
#   ORDER BY the metric alone returns boundary rows in arbitrary order.
# - EVERY ranking ORDER BY must append the entity key as a tie-breaker:
#       ORDER BY total_sales DESC, `customer` ASC   -- top N
#       ORDER BY total_sales ASC,  `customer` ASC   -- worst N
# - Same inside windows: ROW_NUMBER() OVER (PARTITION BY grp ORDER BY metric DESC, `entity` ASC)

# DIALECT RULES

# 1. You MUST use valid MySQL syntax.
# 2. Do NOT use PostgreSQL functions like DATE_TRUNC.
# 3. For monthly grouping in MySQL, if the date is a string (e.g. 'DD-MM-YYYY'), parse it using STR_TO_DATE(date_col, '%d-%m-%Y') before grouping with DATE_FORMAT(..., '%Y-%m').
# 4. ALWAYS use backticks ` for table and column names.

# OUTPUT RULES

# Return ONLY valid JSON:

# {
# "db": "",
# "sql": "",
# "reasoning": ""
# }
# """
        sql_user = f"Schemas available:\n{schema_context}\n\nOriginal Question: {question}\n\nCanonical Query (Structured Intent):\n{canonical_query_str}"
        sql_user += "\n\nCRITICAL FINAL RULE: NEVER SELECT or GROUP BY `customer_master.KUNNR` or any other dimension's Primary Key! You MUST SELECT and GROUP BY the Fact Table's Foreign Key (e.g. `sales_data.customer`) instead. Selecting dimension keys causes unmatched rows to lump together as NULLs."
        schema_grounding, col_to_tables = _build_schema_grounding(schema_chunks)
        table_cols_map = _parse_schema_chunks(schema_chunks)
        if schema_grounding:
            sql_user += f"\n\n{schema_grounding}"

        # Business schema map: pin product attributes to the correct dimension
        # table/join key, map words→columns, and drill down hierarchies. This is
        # what makes "tyre categories" (=> construction within Tyre) and "vehicle"
        # (=> `vehicle type`, not CATEGORY) resolve correctly and without fan-out.
        try:
            biz = _build_business_map(table_cols_map, workspace_id)
            biz_prompt = _business_prompt(biz)
            # [PATCH] Geography chain is resolved and injected separately —
            # region/zone are never part of DIMENSIONS or biz_prompt.
            geo_prompt = _build_geography_prompt(table_cols_map)
            if biz_prompt:
                sql_user += f"\n\n{biz_prompt}"
                print(f"[BIZMAP] fact={biz['fact']} measure={biz['measure']} "
                      f"dims={[(d['label'], d['table']) for d in biz['dims']]} "
                      f"levels={biz['levels']}")
            if geo_prompt:
                sql_user += geo_prompt
                print("[GEOMAP] geography chain injected")

            values_by_col = _parse_value_index(schema_chunks)
            drill = _detect_drilldown(question, values_by_col, biz)
            if drill:
                want_breakdown = bool(_RANK_OR_BREAKDOWN_RE.search(question))
                sql_user += _drilldown_hint(drill, want_breakdown, biz)
                _f = drill["filter"]; _g = drill.get("group")
                print(f"[DRILLDOWN] filter {_f[0]}.{_f[1]}='{_f[2]}'"
                      + (f" -> group by {_g[0]}.{_g[1]}" if (_g and want_breakdown) else " (filter only)"))
            else:
                gh = _grouping_hint(question, biz)
                if gh:
                    sql_user += gh
                    print(f"[GROUPBY] {[ (c,p) for (_t,c,p) in _detect_group_columns(question, biz)]}")
        except Exception as _e:
            print(f"[BIZMAP] skipped: {_e}")

        sql_json = _mistral(sql_sys, sql_user, temperature=0.0)
        print(f"[DEBUG SQL JSON] {sql_json}")

        # Local LLM fallbacks (Mistral sometimes nests inside 'query' or uses uppercase)
        if isinstance(sql_json, dict):
            if "query" in sql_json and isinstance(sql_json["query"], dict) and "sql" in sql_json["query"]:
                sql_json["sql"] = sql_json["query"]["sql"]
            if "SQL" in sql_json and "sql" not in sql_json:
                sql_json["sql"] = sql_json["SQL"]

        # ── Deterministic pre-execution guard ──────────────────────────────
        # The model sometimes attributes a column to the wrong base table
        # (e.g. `invoice`.`construction`), which fails at execution with 1054.
        # Catch it statically against the real schema and force a correction
        # BEFORE touching the database, so the first DB attempt is already right.
        for _v in range(2):
            cand_sql = (sql_json or {}).get("sql", "") if isinstance(sql_json, dict) else ""
            if not cand_sql:
                break
            violations = _validate_column_refs(cand_sql, table_cols_map)
            if not violations:
                break
            print(f"[SQL_VALIDATE] wrong-table column refs {violations} — regenerating before execution")
            fix_user = (
                sql_user
                + "\n\nPREVIOUS SQL:\n" + cand_sql
                + "\n\n" + _column_ref_correction(violations, col_to_tables)
            )
            sql_json = _mistral(sql_sys, fix_user, temperature=0.0)

        # Track failures so we never fall through to an empty-context LLM answer.
        agg_sql_error = None
        sql_results = []

        if sql_json and sql_json.get("sql"):
            target_db = sql_json.get("db", "").strip()
            
            # Fallback: Extract DB from schema_context if the LLM forgot to include it
            if not target_db:
                m_db = re.search(r'db:([a-zA-Z0-9_]+)', schema_context)
                if m_db:
                    target_db = m_db.group(1)

            sql_query = sql_json.get("sql", "").strip()
            print(f"[SQL_GEN] db: {target_db} | sql: {sql_query}")

            conn_sql = cur_sql = None
            sql_results = []
            try:
                # Determine connection
                if not target_db:
                    conn_sql = get_connection_func()
                    cur_sql = conn_sql.cursor(dictionary=True)
                else:
                    try:
                        conn_sql = mysql.connector.connect(
                            host=MYSQL_CONFIG["host"], port=MYSQL_CONFIG["port"],
                            user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"],
                            database=target_db, connection_timeout=10)
                        cur_sql = conn_sql.cursor(dictionary=True)
                    except Exception as e:
                        print(f"[SQL_GEN] MySQL failed, trying Postgres: {e}")
                        if PSYCOPG2_AVAILABLE:
                            temp_conn = get_connection_func()
                            temp_cur = temp_conn.cursor(dictionary=True)
                            temp_cur.execute("SELECT credential FROM database_credential WHERE session_id=%s AND db_type IN ('postgresql', 'postgres')", (session_id,))
                            pg_rows = temp_cur.fetchall()
                            temp_cur.close()
                            temp_conn.close()
                            
                            for r in pg_rows:
                                cred = json.loads(r["credential"]) if isinstance(r["credential"], str) else r["credential"]
                                if cred.get("database") == target_db:
                                    conn_sql = psycopg2.connect(
                                        host=cred.get("host"), port=cred.get("port", 5432),
                                        user=cred.get("username"), password=cred.get("password"),
                                        dbname=target_db, connect_timeout=10)
                                    cur_sql = conn_sql.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                                    break
                
                if cur_sql:
                    for _attempt in range(2):
                        try:
                            cur_sql.execute(sql_query)
                            rows = cur_sql.fetchall()
                            sql_results = [dict(r) for r in rows]

                            # Convert non-serializable objects to string & clean 0E-8 / null names
                            for row in sql_results:
                                for k, v in row.items():
                                    if type(v).__name__ == 'Decimal':
                                        f_val = float(v)
                                        row[k] = 0 if abs(f_val) < 1e-6 else (int(f_val) if f_val.is_integer() else round(f_val, 2))
                                    elif v is not None and not isinstance(v, (str, int, float, bool)):
                                        row[k] = str(v)

                                if "customer_name" in row and not row["customer_name"]:
                                    row["customer_name"] = "N/A"

                            agg_sql_error = None
                            print(f"[SQL_EXEC] Success! Returned {len(sql_results)} rows.")
                            break
                        except Exception as ex:
                            agg_sql_error = str(ex)
                            print(f"[SQL_EXEC] Attempt {_attempt + 1} failed: {ex}")
                            if _attempt == 1:
                                log_system_error(session_id, "rag_chat_sql", f"SQL execution failed after retry: {ex} | Query: {sql_query}", error_type="sql_error")
                                break
                            # Self-heal: send the error back to the model once.
                            repair_user = (
                                f"The following MySQL query FAILED. Fix it and return the same JSON shape.\n\n"
                                f"SQL:\n{sql_query}\n\nMySQL error:\n{ex}\n\n"
                                f"Common fixes: a reserved word is used as an alias (rank, order, group, "
                                f"desc, asc, count, sum, rows, range) — rename the row-number alias to `rn` "
                                f"and backtick every identifier; verify MySQL 8.0 window syntax; keep the "
                                f"follow the COLUMN HYGIENE rule in the system prompt for numeric columns.\n\n"
                                f"Schemas:\n{schema_context}"
                            )
                            if schema_grounding:
                                repair_user += f"\n\n{schema_grounding}"
                            # Targeted correction for 'Unknown column X.Y' (error 1054).
                            repair_user += _unknown_column_hint(ex, col_to_tables)
                            repair_user += f"\n\nOriginal Question: {question}"
                            fix_json = _mistral(sql_sys, repair_user, temperature=0.0)
                            
                            # Fallback for nested SQL in self-heal
                            if isinstance(fix_json, dict):
                                if "query" in fix_json and isinstance(fix_json["query"], dict) and "sql" in fix_json["query"]:
                                    fix_json["sql"] = fix_json["query"]["sql"]
                                if "SQL" in fix_json and "sql" not in fix_json:
                                    fix_json["sql"] = fix_json["SQL"]
                            
                            if fix_json and fix_json.get("sql"):
                                sql_query = fix_json.get("sql", "").strip()
                                print(f"[SQL_REPAIR] Retrying with corrected SQL:\n{sql_query}")
                            else:
                                break
            except Exception as e:
                agg_sql_error = str(e)
                print(f"[SQL_EXEC] Execution failed: {e}")
                log_system_error(session_id, "rag_chat_sql", f"SQL database connection/execution crashed: {e}", error_type="sql_error")
            finally:
                if cur_sql: 
                    try: cur_sql.close() 
                    except: pass
                if conn_sql: 
                    try: conn_sql.close() 
                    except: pass

            if sql_results:
                # Override context with SQL results for final LLM generation
                context = f"SQL Query executed: {sql_query}\n\nSQL Results:\n" + json.dumps(sql_results, indent=2, default=str)

        # AGGREGATION fail-safe: if no SQL rows were produced, do NOT let the
        # model answer from an empty/stale context (that caused fabricated,
        # inconsistent numbers). Constrain it to an honest "could not compute".
        if intent == "AGGREGATION" and not sql_results:
            _detail = f" (error: {agg_sql_error})" if agg_sql_error else ""
            print(f"[AGGREGATION] No SQL rows — refusing to fabricate{_detail}")
            context = (
                "SQL_COMPUTATION_FAILED. The structured query returned no rows"
                f"{_detail}. You MUST NOT fabricate, estimate, infer, or guess "
                "any numbers, names, totals, or rankings. Reply that the result "
                "could not be computed from the database for this question and "
                "suggest the user rephrase or retry."
            )

    # Graph
    if _is_graph(question):
        ftype        = _next_followup_type(session_id)
        followup_ins = _followup_instruction(ftype)
        _graph_tpl = get_prompt(workspace_id, 'rag_chat_graph') or ''
        _graph_msg = (
            _graph_tpl
            .replace('{context}', context)
            .replace('{question}', question)
            .replace('{hist}', hist)
            .replace('{followup_ins}', followup_ins)
        )
        res = _mistral(system_prompt, _graph_msg)
        res = _mistral(SYS, f"""
Retrieved business data:
{context}

{hist}Chart request: "{question}"

Extract actual numeric/categorical values ONLY from the chunks.
{followup_ins}
Return ONLY:
{{
  "chart_type":"bar"|"line"|"pie"|"scatter",
  "title":"...",
  "labels":[...],
  "datasets":[{{"label":"...","data":[...]}}],
  "source_note":"...",
  "follow_up_questions":["{ftype} ...?","{ftype} ...?","{ftype} ...?"]
}}
""")
        if not res:
            return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
        _advance_turn(session_id)
        fuq = res.get("follow_up_questions",[])
        actual_visit = _save_history(get_connection_func, session_id, user_id,
                      question, json.dumps(res.get("datasets",[])), fuq, understanding["intent"], "graph",
                      login_token=login_token, chat_id=active_chat_id)
        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "chart_data": {
                "chart_type":  res.get("chart_type"),
                "title":       res.get("title",""),
                "labels":      res.get("labels",[]),
                "datasets":    res.get("datasets",[]),
                "source_note": res.get("source_note","")
            },
            "follow_up_questions": fuq,
            "chat_id": active_chat_id,
            "visit_number": actual_visit
        }), 200

    # Report
    if _is_report(question):
        ftype        = _next_followup_type(session_id)
        followup_ins = _followup_instruction(ftype)
        _report_tpl = get_prompt(workspace_id, 'rag_chat_report') or ''
        _report_msg = (
            _report_tpl
            .replace('{context}', context)
            .replace('{question}', question)
            .replace('{hist}', hist)
            .replace('{followup_ins}', followup_ins)
        )
        res = _mistral(system_prompt, _report_msg)
        res = _mistral(SYS, f"""
You are a senior business analyst. Write a comprehensive report from the business data below.
Retrieved data:
{context}

{hist}Report request: "{question}"

Write an analytical business report using ONLY the chunks above.
Be specific — use actual numbers, names, values from the data.
{followup_ins}
Return ONLY:
{{
  "report_title":"...",
  "sections":[{{"heading":"...","content":"..."}}],
  "key_findings":["Finding 1","Finding 2","Finding 3"],
  "follow_up_questions":["{ftype} ...?","{ftype} ...?","{ftype} ...?"]
}}
""")
        if not res:
            return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
        _advance_turn(session_id)
        fuq = res.get("follow_up_questions",[])
        actual_visit = _save_history(get_connection_func, session_id, user_id,
                      question, res.get("report_title",""), fuq, understanding["intent"], "report",
                      login_token=login_token, chat_id=active_chat_id)
        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "report": {
                "title":        res.get("report_title",""),
                "sections":     res.get("sections",[]),
                "key_findings": res.get("key_findings",[])
            },
            "follow_up_questions": fuq,
            "chat_id": active_chat_id,
            "visit_number": actual_visit
        }), 200

    # Answer
    ftype        = _next_followup_type(session_id)
    followup_ins = _followup_instruction(ftype)

    # Detect multi-part questions and add explicit instruction
    q_parts = [p.strip() for p in re.split(r'[?]\s+(?=[A-WY-Z])', question) if len(p.strip()) > 8]
    multi_hint = (
        f"\nNOTE: This question has {len(q_parts)} parts. Address EACH part with a clear numbered heading."
        if len(q_parts) > 1 else ""
    )

    _answer_tpl = get_prompt(workspace_id, 'rag_chat_answer')
    if not _answer_tpl or not _answer_tpl.strip():
        return jsonify({
            "status": "error", "statusCode": 500,
            "message": "RAG chat answer prompt not configured in database. Please configure it in the Admin Panel."
        }), 500
    _answer_msg = (
        _answer_tpl
        .replace('{context}', context)
        .replace('{question}', question)
        .replace('{hist}', hist)
        .replace('{followup_ins}', followup_ins)
        .replace('{multi_hint}', multi_hint)
        .replace('{intent}', str(understanding.get('intent', '')))
        .replace('{table_hints}', str(understanding.get('table_hints', '')))
    )
    
    if understanding.get("intent") == "AGGREGATION":
        _answer_msg += "\n\nCRITICAL INSTRUCTION FOR VISUALIZATIONS: Because this is an aggregation query, your 'visualizations' array MUST ONLY contain a 'table' (type='table'). DO NOT generate 'line_chart', 'bar_chart', or any other charts. DO NOT hallucinate dates or months!"

    res = _mistral(system_prompt, _answer_msg)
    if not res:
        log_system_error(session_id, "rag_chat_answer", "LLM generation failed or returned empty response", error_type="llm_error")
        return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500

    clean_answer = _to_str(res.get("answer",""))
    clean_answer = re.sub(r'\s*\(source:[^)]*\)', '', clean_answer).strip()
    clean_answer = re.sub(r'\s*\[source:[^\]]*\]', '', clean_answer).strip()

    raw_fuq = res.get("follow_up_questions", [])
    fuq = []
    if isinstance(raw_fuq, list):
        for q in raw_fuq:
            if isinstance(q, dict):
                fuq.append(str(q.get("question", list(q.values())[0] if q else "")))
            else:
                fuq.append(str(q))

    visualizations = _safe_visualizations(
        _normalize_visualizations(res.get("visualizations", []))
    )

    if understanding.get("intent") == "AGGREGATION" and sql_results:
        keys_lower = [k.lower() for k in sql_results[0].keys()]
        has_time = any(t in k for k in keys_lower for t in ['month', 'date', 'year'])
        
        columns = [{"key": k, "label": str(k).replace("_", " ").title()} for k in sql_results[0].keys()]
        table_vis = {
            "type": "table",
            "title": "Data Table",
            "columns": columns,
            "data": sql_results
        }
        
        if not has_time:
            # Keep only bar_charts; drop hallucinated line/pie charts
            visualizations = [v for v in visualizations if v.get("type") == "bar_chart"]
        else:
            # Keep all non-table charts (like line_chart)
            visualizations = [v for v in visualizations if v.get("type") != "table"]
            
        # ALWAYS append the perfectly formatted Python table, replacing any broken LLM table
        visualizations.append(table_vis)

    # If user explicitly asked for table → keep only table
    if "table" in question.lower():
        visualizations = [v for v in visualizations if v.get("type") == "table"]



    # Universal safety net: If any table visualization has missing/empty columns, 
    # dynamically populate them from the data keys to prevent UI binding failures.
    for v in visualizations:
        if v.get("type") == "table" and not v.get("columns") and v.get("data") and isinstance(v["data"], list) and len(v["data"]) > 0:
            if isinstance(v["data"][0], dict):
                v["columns"] = [{"key": k, "label": str(k).replace("_", " ").title()} for k in v["data"][0].keys()]

    if "table" in question.lower():
        visualizations = [v for v in visualizations if v.get("type") == "table"]

    _advance_turn(session_id)
    actual_visit = _save_history(get_connection_func, session_id, user_id,
                  question, clean_answer, fuq, understanding["intent"], "answer",
                  login_token=login_token, visualizations=visualizations,
                  chat_id=active_chat_id)

    return jsonify({
        "status":              "success",
        "statusCode":          200,
        "answer":              clean_answer,
        "follow_up_questions": fuq,
        "visualizations": visualizations,
        "chat_id": active_chat_id,
        "visit_number": actual_visit
    }), 200










## --------------------------------------- BKP --------------------------------------


# import re, json, time, hashlib, math, requests, mysql.connector, threading, os
# try:
#     import psycopg2
#     import psycopg2.extras
#     PSYCOPG2_AVAILABLE = True
# except ImportError:
#     PSYCOPG2_AVAILABLE = False
#     print("[RAG] psycopg2 not installed — PostgreSQL support disabled")
# from collections import defaultdict
# from flask import request, jsonify
# from controllers.intent_router import classify_intent
# from controllers.query_branches import execute_hybrid
# from database.config import MISTRAL_API_KEY, MISTRAL_MODEL, MYSQL_CONFIG
# from controllers.kgraph_service import (
#     load_kgraph, build_sql_rules, resolve_grouping, detect_drilldown, validate_sql)
# # ChromaDB persistent storage — vectors survive server restarts
# CHROMA_PERSIST_DIR = os.path.join(
#     os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chroma_store"
# )
# os.makedirs(CHROMA_PERSIST_DIR, exist_ok=True)

# MISTRAL_URL        = "https://api.mistral.ai/v1/chat/completions"
# MAX_ROWS           = 200
# TOP_K              = 80
# CACHE_TTL          = 600
# MAX_CTX_CHARS      = 15000

# _CACHE   = {}
# _CLIENTS = {}
# _LOCKS   = {}

# _FOLLOWUP_CYCLE = ["What", "Where", "Why"]
# _TURN_COUNTER   = {}

# def _next_followup_type(session_id):
#     turn = _TURN_COUNTER.get(session_id, 0)
#     return _FOLLOWUP_CYCLE[turn % 3]

# def _advance_turn(session_id):
#     _TURN_COUNTER[session_id] = _TURN_COUNTER.get(session_id, 0) + 1

# def _followup_instruction(ftype):

#     if ftype.lower() == "what":
#         count = 5
#     elif ftype.lower() == "where":
#         count = 3
#     elif ftype.lower() == "why":
#         count = 3
#     else:
#         count = 3

#     questions = ",\n".join([f'"{ftype} ...?"' for _ in range(count)])

#     return f"""
# Generate exactly {count} intelligent follow-up questions based on the previous answer.

# STRICT RULES:
# 1. Questions must be high-level BUSINESS INSIGHT questions.
# 2. Questions must be directly related to the returned data.

# Do not assume:
# - causes
# - risks
# - opportunities
# - business strategy
# - customer behaviour
# - operational issues

# Only ask questions supported by the data.
# 3. Questions must encourage deeper analysis, strategic thinking, or early problem detection.
# 4. DO NOT mention table names, database names, column names, or technical terms.
# 5. Questions should sound like executive/business analyst questions.
# 6. Each question must start with "{ftype}".

# Return ONLY:

# "follow_up_questions":[
# {questions}
# ]
# """

# GRAPH_KW  = {"graph","chart","plot","visualize","visualise","bar","pie","line","histogram","scatter"}
# REPORT_KW = {"report","summary report","generate report","make a report","create a report","write a report"}
# GREET_RE  = re.compile(r'^\s*(hi+|hello+|hey+|howdy|greetings|sup|yo+|hiya|good\s*(morning|afternoon|evening|night)|what\'?s\s*up)\s*[!?.]*\s*$', re.I)


# # ══════════════════════════════════════════════════════
# # OPTIMIZATION 1: Global models loaded once at startup
# # ══════════════════════════════════════════════════════

# _EMBED_MODEL   = None
# _CROSS_ENCODER = None
# _MODEL_LOCK    = threading.Lock()

# # OPTIMIZATION 2: Embedding cache
# _EMBED_CACHE   = {}


# def _get_embed_model():
#     global _EMBED_MODEL
#     if _EMBED_MODEL is None:
#         with _MODEL_LOCK:
#             if _EMBED_MODEL is None:
#                 from sentence_transformers import SentenceTransformer
#                 print("[RAG] Loading SentenceTransformer globally...")
#                 _EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
#                 print("[RAG] SentenceTransformer ready")
#     return _EMBED_MODEL


# def _get_cross_encoder():
#     global _CROSS_ENCODER
#     if _CROSS_ENCODER is None:
#         with _MODEL_LOCK:
#             if _CROSS_ENCODER is None:
#                 from sentence_transformers.cross_encoder import CrossEncoder
#                 print("[RAG] Loading CrossEncoder globally...")
#                 _CROSS_ENCODER = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
#                 print("[RAG] CrossEncoder ready")
#     return _CROSS_ENCODER


# def _encode_texts(texts):
#     """OPTIMIZATION 2+3: Cache-aware batch embedding."""
#     model  = _get_embed_model()
#     result = [None] * len(texts)
#     uncached_idx   = []
#     uncached_texts = []

#     for i, t in enumerate(texts):
#         if t in _EMBED_CACHE:
#             result[i] = _EMBED_CACHE[t]
#         else:
#             uncached_idx.append(i)
#             uncached_texts.append(t)

#     if uncached_texts:
#         new_embeds = model.encode(uncached_texts, batch_size=128, show_progress_bar=False).tolist()
#         for idx, emb, txt in zip(uncached_idx, new_embeds, uncached_texts):
#             _EMBED_CACHE[txt] = emb
#             result[idx] = emb

#     return result


# # ══════════════════════════════════════════════════════
# # BM25
# # ══════════════════════════════════════════════════════

# class BM25:
#     def __init__(self, docs, k1=1.5, b=0.75):
#         self.k1, self.b = k1, b
#         self.docs = docs
#         self.N = len(docs)
#         self.tokenized = [self._tok(d) for d in docs]
#         self.avgdl = sum(len(t) for t in self.tokenized) / max(self.N, 1)
#         self.df = defaultdict(int)
#         for td in self.tokenized:
#             for w in set(td): self.df[w] += 1

#     def _tok(self, text):
#         return re.findall(r'\b\w+\b', text.lower())

#     def score(self, query, top_k):
#         q_terms = self._tok(query)
#         scores  = []
#         for i, td in enumerate(self.tokenized):
#             tf_map = defaultdict(int)
#             for w in td: tf_map[w] += 1
#             s = 0.0
#             for term in q_terms:
#                 if term not in tf_map: continue
#                 tf  = tf_map[term]
#                 idf = math.log((self.N - self.df[term] + 0.5) / (self.df[term] + 0.5) + 1)
#                 den = tf + self.k1 * (1 - self.b + self.b * len(td) / self.avgdl)
#                 s  += idf * (tf * (self.k1 + 1)) / den
#             if s > 0: scores.append((i, s))
#         scores.sort(key=lambda x: -x[1])
#         return scores[:top_k]


# # ══════════════════════════════════════════════════════
# # DATA LOADERS
# # ══════════════════════════════════════════════════════

# def _local_conn(fn):
#     c = fn()
#     if not c: raise RuntimeError("Local DB failed")
#     return c


# def _load_all(session_id, get_fn):
#     chunks = []
#     chunks += _load_db(session_id, get_fn)
#     chunks += _load_sheets(session_id, get_fn)
#     chunks += _load_web(session_id, get_fn)
#     chunks += _load_analysis_report(session_id, get_fn)
#     return chunks


# def _load_analysis_report(session_id, get_fn):

#     chunks = []
#     local = cur = None
#     try:
#         local = _local_conn(get_fn)
#         cur   = local.cursor(dictionary=True)

#         # ── 1. saved_web_results: grouped by topic (richer than _load_web) ──
#         cur.execute(
#             """SELECT topic, title, url, brief FROM saved_web_results
#                WHERE session_id=%s ORDER BY topic""",
#             (session_id,)
#         )
#         web_rows = cur.fetchall()
#         if web_rows:
#             # Group by topic
#             from collections import defaultdict as _dd
#             by_topic = _dd(list)
#             for r in web_rows:
#                 by_topic[r["topic"]].append(r)
#             for topic, items in by_topic.items():
#                 lines = [f"[ANALYSIS_WEB] Web research topic: {topic} ({len(items)} results)"]
#                 for item in items:
#                     lines.append(f"  Title: {item['title']}")
#                     if item.get("brief"):
#                         lines.append(f"  Brief: {str(item['brief'])[:400]}")
#                 chunks.append(_chunk("\n".join(lines), kind="analysis_web", table=topic))
#             print(f"[RAG] analysis_web: {len(by_topic)} topics from saved_web_results")

#         # ── 2. external_db_sync_log: DB + table metadata summary ──
#         cur.execute(
#             """SELECT DISTINCT external_database, new_user_db, table_name
#                FROM external_db_sync_log
#                WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db!=''
#                ORDER BY external_database, table_name""",
#             (session_id,)
#         )
#         sync_rows = cur.fetchall()
#         if sync_rows:
#             from collections import defaultdict as _dd2
#             by_db = _dd2(list)
#             for r in sync_rows:
#                 by_db[r["external_database"]].append(r)
#             for ext_db, rows in by_db.items():
#                 new_db  = rows[0]["new_user_db"]
#                 tables  = [r["table_name"] for r in rows if r["table_name"]]
#                 text = (
#                     f"[ANALYSIS_DB_META] Database analyzed: {ext_db} "
#                     f"(stored as: {new_db})\n"
#                     f"Tables found: {', '.join(tables)}\n"
#                     f"Total tables: {len(tables)}"
#                 )
#                 chunks.append(_chunk(text, kind="analysis_db_meta", db=new_db))
#             print(f"[RAG] analysis_db_meta: {len(by_db)} databases from sync_log")

#     except Exception as e:
#         print(f"[RAG] analysis_report load error: {e}")
#     finally:
#         if cur:   cur.close()
#         if local: local.close()

#     return chunks


# def _load_db(session_id, get_fn):
#     """
#     Loads DB chunks for RAG — supports both MySQL and PostgreSQL.
#     For PostgreSQL:
#       - If schema was specified during connection → only that schema's tables
#       - If no schema → all non-system schemas (public + any custom ones)
#     """
#     chunks = []
#     local = cur = None

#     # ── 1. Fetch all DB credentials for this session ──
#     try:
#         local = _local_conn(get_fn)
#         cur   = local.cursor(dictionary=True)

#         # MySQL/MSSQL: get allocated db name from sync log
#         cur.execute("""
#             SELECT DISTINCT new_user_db
#             FROM external_db_sync_log
#             WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db!=''
#         """, (session_id,))
#         sync_rows = cur.fetchall()

#         # PostgreSQL: credentials stored in database_credential
#         cur.execute("""
#             SELECT credential, db_type
#             FROM database_credential
#             WHERE session_id=%s AND db_type IN ('postgresql', 'postgres')
#             ORDER BY connection_id DESC
#         """, (session_id,))
#         pg_cred_rows = cur.fetchall()

#     finally:
#         if cur:   cur.close()
#         if local: local.close()

#     # ── 2. MySQL / MSSQL (sync log approach — existing logic) ──
#     mysql_dbs = [r["new_user_db"] for r in sync_rows
#                  if r.get("db_type", "mysql") not in ("postgresql", "postgres")]

#     for db in mysql_dbs:
#         if not re.match(r'^\w+$', db): continue
#         conn = c2 = None
#         try:
#             conn = mysql.connector.connect(
#                 host=MYSQL_CONFIG["host"], port=MYSQL_CONFIG["port"],
#                 user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"],
#                 database=db, connection_timeout=10)
#             c2 = conn.cursor(dictionary=True)
#             c2.execute("SHOW TABLES")
#             tables   = [list(r.values())[0] for r in c2.fetchall()]
#             all_rows = {}

#             for t in tables:
#                 if not re.match(r'^\w+$', t): continue
#                 try:
#                     c2.execute(f"SELECT * FROM `{t}` LIMIT %s", (MAX_ROWS,))
#                     rows = c2.fetchall()
#                     if not rows: continue
#                     all_rows[t] = rows
#                     cols = list(rows[0].keys())
#                     chunks.append(_chunk(
#                         f"[SCHEMA] db:{db} table:{t} columns:{','.join(cols)} total_rows:{len(rows)}",
#                         db=db, table=t, kind="schema"))
#                     lines = [
#                         f"[COUNT] db:{db} table:{t} has {len(rows)} rows total.",
#                         f"Number of {t}: {len(rows)}",
#                         f"Total {t} count: {len(rows)}"
#                     ]
#                     for col in cols[:10]:
#                         vals = list(dict.fromkeys(
#                             str(r[col]) for r in rows if r[col] is not None and str(r[col]).strip()))
#                         if vals:
#                             lines.append(f"All values of {col} in {t}: {', '.join(vals[:40])}")
#                     chunks.append(_chunk("\n".join(lines), db=db, table=t, kind="count"))
#                     for i, row in enumerate(rows, 1):
#                         parts = " | ".join(f"{k}:{v}" for k,v in row.items()
#                                            if v is not None and str(v).strip())
#                         chunks.append(_chunk(f"[ROW] db:{db} table:{t} row{i}: {parts}",
#                                              db=db, table=t, kind="row"))
#                     print(f"[RAG] {db}.{t}: {len(rows)} rows → {len(rows)+2} chunks")
#                 except Exception as e:
#                     print(f"[RAG] skip {t}: {e}")

#             chunks += _build_joins(db, all_rows)
#         except Exception as e:
#             print(f"[RAG] db connect {db}: {e}")
#         finally:
#             if c2:   c2.close()
#             if conn: conn.close()

#     # ── 3. PostgreSQL (direct connection using stored credentials) ──
#     if not PSYCOPG2_AVAILABLE:
#         if pg_cred_rows:
#             print("[RAG] PostgreSQL credentials found but psycopg2 not installed — skipping")
#         return chunks

#     seen_pg_dbs = set()
#     for cred_row in pg_cred_rows:
#         try:
#             cred = cred_row["credential"]
#             if isinstance(cred, str):
#                 cred = json.loads(cred)

#             pg_host     = cred.get("host", "localhost")
#             pg_port     = int(cred.get("port", 5432))
#             pg_user     = cred.get("username", "")
#             pg_password = cred.get("password", "")
#             pg_database = cred.get("database", "")
#             pg_schema   = cred.get("schema")  # may be None/empty

#             # Deduplicate same DB+schema combos
#             dedup_key = f"{pg_host}:{pg_port}/{pg_database}/{pg_schema or '__all__'}"
#             if dedup_key in seen_pg_dbs:
#                 continue
#             seen_pg_dbs.add(dedup_key)

#             print(f"[RAG] Connecting PostgreSQL: {pg_host}:{pg_port}/{pg_database} schema={pg_schema or 'ALL'}")

#             pg_conn = psycopg2.connect(
#                 host=pg_host, port=pg_port,
#                 user=pg_user, password=pg_password,
#                 dbname=pg_database,
#                 connect_timeout=10
#             )
#             pg_cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

#             # Determine which schemas to fetch
#             if pg_schema and pg_schema.strip():
#                 # User specified a schema → use only that
#                 schemas_to_fetch = [pg_schema.strip()]
#             else:
#                 # No schema specified → fetch ALL non-system schemas
#                 pg_cur.execute("""
#                     SELECT schema_name
#                     FROM information_schema.schemata
#                     WHERE schema_name NOT IN ('pg_catalog', 'information_schema',
#                                               'pg_toast', 'pg_temp_1', 'pg_toast_temp_1')
#                       AND schema_name NOT LIKE 'pg_temp_%'
#                       AND schema_name NOT LIKE 'pg_toast_temp_%'
#                     ORDER BY schema_name
#                 """)
#                 schemas_to_fetch = [r["schema_name"] for r in pg_cur.fetchall()]
#                 print(f"[RAG] PostgreSQL schemas found: {schemas_to_fetch}")

#             all_rows_pg = {}

#             for schema in schemas_to_fetch:
#                 # Get all tables in this schema
#                 pg_cur.execute("""
#                     SELECT table_name
#                     FROM information_schema.tables
#                     WHERE table_schema = %s
#                       AND table_type = 'BASE TABLE'
#                     ORDER BY table_name
#                 """, (schema,))
#                 tables = [r["table_name"] for r in pg_cur.fetchall()]
#                 print(f"[RAG] PG schema '{schema}' tables: {tables}")

#                 for t in tables:
#                     qualified = f"{schema}.{t}"
#                     label     = f"{pg_database}.{qualified}"
#                     try:
#                         pg_cur.execute(
#                             f'SELECT * FROM "{schema}"."{t}" LIMIT %s',
#                             (MAX_ROWS,)
#                         )
#                         rows = [dict(r) for r in pg_cur.fetchall()]
#                         if not rows:
#                             continue

#                         # Convert non-serialisable types (dates, Decimal, etc.)
#                         for row in rows:
#                             for k, v in row.items():
#                                 if v is not None and not isinstance(v, (str, int, float, bool)):
#                                     row[k] = str(v)

#                         all_rows_pg[qualified] = rows
#                         cols = list(rows[0].keys())

#                         chunks.append(_chunk(
#                             f"[SCHEMA] db:{label} table:{qualified} schema:{schema} "
#                             f"columns:{','.join(cols)} total_rows:{len(rows)}",
#                             db=pg_database, table=qualified, kind="schema"))

#                         lines = [
#                             f"[COUNT] db:{label} table:{qualified} has {len(rows)} rows total.",
#                             f"Number of {t}: {len(rows)}",
#                             f"Total {t} count: {len(rows)}"
#                         ]
#                         for col in cols[:10]:
#                             vals = list(dict.fromkeys(
#                                 str(r[col]) for r in rows
#                                 if r[col] is not None and str(r[col]).strip()))
#                             if vals:
#                                 lines.append(f"All values of {col} in {t}: {', '.join(vals[:40])}")
#                         chunks.append(_chunk("\n".join(lines), db=pg_database, table=qualified, kind="count"))

#                         for i, row in enumerate(rows, 1):
#                             parts = " | ".join(
#                                 f"{k}:{v}" for k, v in row.items()
#                                 if v is not None and str(v).strip()
#                             )
#                             chunks.append(_chunk(
#                                 f"[ROW] db:{label} schema:{schema} table:{t} row{i}: {parts}",
#                                 db=pg_database, table=qualified, kind="row"))

#                         print(f"[RAG] PG {label}: {len(rows)} rows → {len(rows)+2} chunks")

#                     except Exception as e:
#                         print(f"[RAG] PG skip {qualified}: {e}")
#                         pg_conn.rollback()

#             pg_cur.close()
#             pg_conn.close()

#         except Exception as e:
#             print(f"[RAG] PostgreSQL connect error: {e}")

#     return chunks


# def _build_joins(db, all_rows):
#     chunks = []
#     user_tables = [t for t in all_rows if re.search(r'\busers?\b', t, re.I)]
#     for ut in user_tables:
#         u_rows = all_rows[ut]
#         if not u_rows: continue
#         ucols  = list(u_rows[0].keys())
#         id_col = next((c for c in ucols if c in ('id','user_id','uid')), ucols[0])
#         nm_col = next((c for c in ucols if re.search(r'\b(name|username)\b', c, re.I)), None)
#         for ur in u_rows:
#             uid   = str(ur.get(id_col,"")).strip()
#             uname = str(ur.get(nm_col, uid)).strip() if nm_col else uid
#             if not uid: continue
#             for at, a_rows in all_rows.items():
#                 if at == ut or not a_rows: continue
#                 acols   = list(a_rows[0].keys())
#                 ref_col = next((c for c in acols if re.search(r'\buser_id\b|\buid\b|\bauthor\b', c, re.I)), None)
#                 if not ref_col: continue
#                 acts = [r for r in a_rows if str(r.get(ref_col,"")).strip() == uid]
#                 if not acts: continue
#                 detail = " || ".join(
#                     " | ".join(f"{k}:{v}" for k,v in r.items() if v is not None and str(v).strip())
#                     for r in acts[:15])
#                 chunks.append(_chunk(
#                     f"[JOIN] db:{db} user:'{uname}' (id:{uid}) from:{ut} "
#                     f"has {len(acts)} record(s) in table:{at}. data: {detail}",
#                     db=db, table=f"{ut}+{at}", kind="join"))
#     return chunks


# def _load_sheets(session_id, get_fn):
#     chunks = []
#     local = cur = None
#     try:
#         local = _local_conn(get_fn)
#         cur   = local.cursor(dictionary=True)
#         cur.execute("SELECT table_name,sheet_url FROM sheet_scans WHERE session_id=%s", (session_id,))
#         for sc in cur.fetchall():
#             t = sc["table_name"]
#             if not re.match(r'^sheet_\w+$', t): continue
#             try:
#                 cur.execute(f"SELECT * FROM `{t}` LIMIT %s", (MAX_ROWS,))
#                 rows = cur.fetchall()
#                 if not rows: continue
#                 cols = [c for c in rows[0].keys() if c != "_row_id"]
#                 chunks.append(_chunk(f"[SCHEMA] sheet:{t} url:{sc.get('sheet_url','')} columns:{','.join(cols)} rows:{len(rows)}", table=t, kind="schema"))
#                 lines = [f"[COUNT] sheet:{t} has {len(rows)} rows total."]
#                 for col in cols[:6]:
#                     vals = list(dict.fromkeys(str(r[col]) for r in rows if r.get(col) is not None))
#                     lines.append(f"All values of {col}: {', '.join(vals[:20])}")
#                 chunks.append(_chunk("\n".join(lines), table=t, kind="count"))
#                 for i, row in enumerate(rows, 1):
#                     parts = " | ".join(f"{k}:{v}" for k,v in row.items()
#                                        if k!="_row_id" and v is not None and str(v).strip())
#                     chunks.append(_chunk(f"[ROW] sheet:{t} row{i}: {parts}", table=t, kind="row"))
#             except Exception as e:
#                 print(f"[RAG] sheet {t}: {e}")
#     except Exception as e:
#         print(f"[RAG] sheets: {e}")
#     finally:
#         if cur:   cur.close()
#         if local: local.close()
#     return chunks


# def _load_web(session_id, get_fn):
#     chunks = []
#     local = cur = None
#     try:
#         local = _local_conn(get_fn)
#         cur   = local.cursor(dictionary=True)
#         cur.execute("SELECT title,url,brief,topic FROM saved_web_results WHERE session_id=%s", (session_id,))
#         for r in cur.fetchall():
#             chunks.append(_chunk(
#                 f"[WEB] title:{r['title']} url:{r['url']} topic:{r.get('topic','')} content:{r.get('brief','')}",
#                 kind="web"))
#     except Exception as e:
#         print(f"[RAG] web: {e}")
#     finally:
#         if cur:   cur.close()
#         if local: local.close()
#     return chunks


# def _chunk(text, db="", table="", kind="row"):
#     return {"text": text, "db": db, "table": table, "kind": kind}


# # ══════════════════════════════════════════════════════
# # VECTOR STORE (ChromaDB)
# # ══════════════════════════════════════════════════════

# def _get_or_create_lock(session_id):
#     if session_id not in _LOCKS:
#         _LOCKS[session_id] = threading.Lock()
#     return _LOCKS[session_id]


# def _col_safe_count(col):
#     try:    return col.count()
#     except: return 0


# def _build_store(session_id, get_fn):
#     import chromadb
#     lock = _get_or_create_lock(session_id)
#     with lock:
#         now = time.time()
#         if session_id in _CACHE:
#             chunks, bm25, col, ts = _CACHE[session_id]
#             if now - ts < CACHE_TTL and _col_safe_count(col) > 0:
#                 print(f"[RAG] cache hit — {len(chunks)} chunks")
#                 return chunks, bm25, col
#             else:
#                 _CACHE.pop(session_id, None)

#         print(f"[RAG] building store for {session_id[:8]}...")
#         all_chunks = _load_all(session_id, get_fn)
#         if not all_chunks:
#             return None, None, None

#         # OPTIMIZATION 4: Deduplicate chunks before indexing
#         seen_texts = set()
#         deduped    = []
#         for c in all_chunks:
#             if c["text"] not in seen_texts:
#                 seen_texts.add(c["text"])
#                 deduped.append(c)
#         removed = len(all_chunks) - len(deduped)
#         if removed > 0:
#             print(f"[RAG] deduped {removed} duplicates → {len(deduped)} unique chunks")
#         all_chunks = deduped

#         texts    = [c["text"] for c in all_chunks]
#         bm25_idx = BM25(texts)

#         # Batch encode using global model + cache
#         embeds = _encode_texts(texts)

#         # Fetch workspace_chroma_collection from DB
#         col_name = "s_" + hashlib.md5(session_id.encode()).hexdigest()[:12]
#         try:
#             conn = get_fn()
#             cur = conn.cursor(dictionary=True)
#             cur.execute("SELECT workspace_chroma_collection FROM workspaces WHERE session_id = %s", (session_id,))
#             row = cur.fetchone()
#             if row and row.get("workspace_chroma_collection"):
#                 col_name = row["workspace_chroma_collection"]
#         except Exception as e:
#             print(f"[RAG] Error fetching workspace_chroma_collection: {e}")
#         finally:
#             if 'cur' in locals() and cur: cur.close()
#             if 'conn' in locals() and conn: conn.close()

#         if session_id not in _CLIENTS:
#             _CLIENTS[session_id] = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
#         client = _CLIENTS[session_id]
#         try: client.delete_collection(col_name)
#         except: pass
#         col = client.create_collection(col_name)

#         for i in range(0, len(all_chunks), 500):
#             b = all_chunks[i:i+500]
#             col.add(
#                 documents  = [c["text"]  for c in b],
#                 embeddings = embeds[i:i+500],
#                 metadatas  = [{"db":c["db"],"table":c["table"],"kind":c["kind"]} for c in b],
#                 ids        = [f"c{i+j}" for j in range(len(b))]
#             )

#         _CLIENTS[session_id] = client
#         _CACHE[session_id]   = (all_chunks, bm25_idx, col, now)
#         print(f"[RAG] ✓ {len(all_chunks)} chunks indexed")
#         return all_chunks, bm25_idx, col


# # ══════════════════════════════════════════════════════
# # QUERY UNDERSTANDING
# # ══════════════════════════════════════════════════════

# def _understand(question, all_chunks):
#     q      = question.lower()
#     tokens = set(re.findall(r'\b\w{3,}\b', q))
#     known_tables = list(dict.fromkeys(c["table"] for c in all_chunks if c["table"]))

#     table_hints = []
#     for t in known_tables:
#         t_parts = set(re.findall(r'\b\w{3,}\b', t.lower()))
#         overlap = tokens & t_parts
#         if overlap: table_hints.append((t, len(overlap)))
#     table_hints.sort(key=lambda x: -x[1])
#     matched_tables = [t for t,_ in table_hints[:5]]

#     intent = "lookup"
#     if re.search(r'\bhow\s+many\b|\bcount\b|\btotal\b|\bnumber\s+of\b|\bhow\s+much\b', q):
#         intent = "count"
#     elif re.search(r'\blist\b|\ball\b|\beveryone\b|\bnames?\b|\bshow\s+(me\s+)?all\b', q):
#         intent = "list"
#     elif re.search(r'\bwho\b|\bwhich\s+user\b|\bwhose\b', q):
#         intent = "who"
#     elif re.search(r'\bcreated\s+by\b|\bbelongs?\s+to\b|\bby\s+whom\b|\bowned\s+by\b', q):
#         intent = "join"
#     elif re.search(r'\bwhat\s+is\b|\bwhat\s+are\b|\btell\s+me\b|\bfind\b|\bget\b', q):
#         intent = "lookup"

#     entities = re.findall(r"'([^']+)'|\"([^\"]+)\"", question)
#     entities = [e[0] or e[1] for e in entities]

#     queries = [question]
#     if intent == "count" and matched_tables:
#         for t in matched_tables[:3]:
#             queries += [f"COUNT {t} total rows", f"Number of {t}", f"how many {t}", f"[COUNT] {t}"]
#     elif intent == "list" and matched_tables:
#         for t in matched_tables[:2]:
#             queries += [f"All values of", f"list all {t}", f"[COUNT] {t}"]
#     elif intent == "join":
#         queries += ["[JOIN]"] + [f"user '{e}'" for e in entities]
#     elif entities:
#         queries += [f"'{e}'" for e in entities] + [f"{e}" for e in entities]

#     if not matched_tables:
#         for t in known_tables:
#             for tok in tokens:
#                 if tok in t.lower() and len(tok) > 3:
#                     matched_tables.append(t); break
#         matched_tables = list(dict.fromkeys(matched_tables))[:5]

#     return {
#         "intent":      intent,
#         "table_hints": matched_tables,
#         "entities":    entities,
#         "queries":     list(dict.fromkeys(queries))
#     }


# # ══════════════════════════════════════════════════════
# # HYBRID RETRIEVAL (BM25 + Vector + Cross-Encoder Rerank)
# # ══════════════════════════════════════════════════════

# def _retrieve(all_chunks, bm25_idx, col, question, understanding):
#     intent   = understanding["intent"]
#     hints    = understanding["table_hints"]
#     queries  = understanding["queries"]
#     entities = understanding["entities"]

#     scores = defaultdict(float)

#     # BM25
#     for q in queries:
#         for idx, s in bm25_idx.score(q, top_k=80):
#             scores[idx] += s * 1.0

#     # OPTIMIZATION 3: Batch encode all queries at once
#     q_embeds = _encode_texts(queries)
#     per_q    = max(8, TOP_K // len(queries))
#     seen     = set()
#     for q_emb in q_embeds:
#         n = min(per_q, _col_safe_count(col))
#         if n == 0: continue
#         res = col.query(query_embeddings=[q_emb], n_results=n,
#                         include=["documents","metadatas"])
#         for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
#             key = doc[:100]
#             if key in seen: continue
#             seen.add(key)
#             for i, c in enumerate(all_chunks):
#                 if c["text"][:100] == key:
#                     scores[i] += 2.0; break

#     # Boosts
#     for i, c in enumerate(all_chunks):
#         if c["table"] in hints:                               scores[i] += 5.0
#         if intent == "count" and c["kind"] == "count":        scores[i] += 8.0
#         elif intent == "list" and c["kind"] == "count":       scores[i] += 6.0
#         elif intent == "join" and c["kind"] == "join":        scores[i] += 8.0
#         for ent in entities:
#             if ent.lower() in c["text"].lower():              scores[i] += 4.0

#     ranked = sorted(scores.items(), key=lambda x: -x[1])

#     forced = {i for i, c in enumerate(all_chunks)
#               if c["kind"] == "count" and c["table"] in hints}

#     candidate_indices = list(forced)
#     for i, _ in ranked:
#         if i not in forced: candidate_indices.append(i)
#         if len(candidate_indices) >= TOP_K: break

#     # OPTIMIZATION 5: Cross-encoder reranking top-40 → keep best 20
#     RERANK_TOP  = 40
#     RERANK_KEEP = 20
#     rerank_pool = candidate_indices[:RERANK_TOP]

#     if len(rerank_pool) > RERANK_KEEP:
#         try:
#             ce_model  = _get_cross_encoder()
#             pairs     = [(question, all_chunks[i]["text"][:512]) for i in rerank_pool]
#             ce_scores = ce_model.predict(pairs)
#             reranked  = sorted(zip(rerank_pool, ce_scores), key=lambda x: -x[1])
#             forced_in = [i for i in rerank_pool if i in forced]
#             reranked_nf = [i for i, _ in reranked if i not in forced]
#             rerank_pool = forced_in + reranked_nf[:RERANK_KEEP]
#             candidate_indices = rerank_pool + candidate_indices[RERANK_TOP:]
#             print(f"[RAG] cross-encoder reranked {RERANK_TOP} → kept {len(rerank_pool)}")
#         except Exception as e:
#             print(f"[RAG] cross-encoder skipped: {e}")

#     parts, total = [], 0
#     for i in candidate_indices:
#         txt   = all_chunks[i]["text"]
#         if total + len(txt) > MAX_CTX_CHARS: break
#         parts.append(txt)
#         total += len(txt)

#     return "\n\n".join(parts)


# # ══════════════════════════════════════════════════════
# # AUTO CHAT HISTORY SAVE
# # ══════════════════════════════════════════════════════

# _HISTORY_TABLE_SQL = """
# CREATE TABLE IF NOT EXISTS session_chat_history (
#     id                  INT AUTO_INCREMENT PRIMARY KEY,
#     session_id          VARCHAR(100) NOT NULL,
#     user_id             INT          NOT NULL,
#     turn_index          INT          NOT NULL DEFAULT 0,
#     visit_number        INT          NOT NULL DEFAULT 1,
#     question            TEXT         NOT NULL,
#     answer              LONGTEXT     NOT NULL,
#     follow_up_questions JSON         DEFAULT NULL,
#     visualizations      JSON         DEFAULT NULL,
#     intent              VARCHAR(50)  DEFAULT NULL,
#     mode                VARCHAR(30)  DEFAULT 'answer',
#     created_at          DATETIME     DEFAULT CURRENT_TIMESTAMP,
#     INDEX idx_session      (session_id),
#     INDEX idx_user         (user_id),
#     INDEX idx_session_user (session_id, user_id)
# );
# """

# def _save_history(get_fn, session_id, user_id, question, answer, follow_ups, intent, mode, visualizations=None, visit_number=1):
#     if not user_id: return
#     conn = cur = None
#     try:
#         conn = get_fn()
#         cur  = conn.cursor(dictionary=True)
#         cur.execute(_HISTORY_TABLE_SQL)
#         cur.execute("""
#             SELECT COALESCE(MAX(turn_index), -1) AS last_turn
#             FROM session_chat_history
#             WHERE session_id = %s AND user_id = %s
#         """, (session_id, int(user_id)))
#         row        = cur.fetchone()
#         turn_index = (row["last_turn"] + 1) if row else 0
#         cur.execute("""
#             INSERT INTO session_chat_history
#                 (session_id, user_id, turn_index, visit_number, question, answer,
#                  follow_up_questions, visualizations, intent, mode)
#             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
#         """, (session_id, int(user_id), turn_index, visit_number, question, answer,
#               json.dumps(follow_ups) if follow_ups else None,
#               json.dumps(visualizations) if visualizations else None,
#               intent or None, mode))
#         conn.commit()
#     except Exception as e:
#         print(f"[History] save error: {e}")
#     finally:
#         if cur:  cur.close()
#         if conn: conn.close()


# # ══════════════════════════════════════════════════════
# # MISTRAL
# # ══════════════════════════════════════════════════════

# from model.llm_client import call_llm_chat

# def _mistral(system, user, retries=2, temperature=0.15):
#     # Trim from the middle if too long, preserving both context start and prompt instructions at the end
#     if len(user) > 28000:
#         half = 13500
#         user = user[:half] + "\n\n[...context trimmed for token limit...]\n\n" + user[-half:]
#         print(f"[LLM] prompt trimmed")
        
#     messages = [
#         {"role":"system","content":system},
#         {"role":"user","content":user}
#     ]
    
#     for attempt in range(retries + 1):
#         try:
#             response = call_llm_chat(messages, json_mode=True, temperature=temperature)
#             if response:
#                 if response.startswith("[LLM Error]"):
#                     raise Exception(response)
#                 cleaned = response.strip()
#                 if cleaned.startswith("```json"):
#                     cleaned = cleaned[7:]
#                 elif cleaned.startswith("```"):
#                     cleaned = cleaned[3:]
#                 if cleaned.endswith("```"):
#                     cleaned = cleaned[:-3]
#                 return json.loads(cleaned.strip())
#             return None
#         except Exception as e:
#             print(f"[LLM] error attempt {attempt+1}: {e}")
#             if attempt == retries: return None
#             import time
#             time.sleep(1)
#     return None


# def _history(raw):
#     if not raw or not isinstance(raw, list): return ""
#     lines = [f"{str(t.get('role','user')).capitalize()}: {str(t.get('content',''))}"
#              for t in raw[-6:] if isinstance(t, dict)]
#     return ("Chat history:\n" + "\n".join(lines) + "\n\n") if lines else ""


# def _is_graph(q):  return bool(set(q.lower().split()) & GRAPH_KW)
# def _is_report(q): return any(k in q.lower() for k in REPORT_KW)
# def _is_greet(q):  return bool(GREET_RE.match(q.strip()))

# ANALYTICAL_KW = {"top", "highest", "average", "total", "trend", "dashboard", "how many", "sum", "vs", "compare", "lowest", "distribution", "revenue", "sales", "discount", "count", "maximum", "minimum", "profit", "ratio", "fastest", "declining", "percentage"}
# def _is_analytical(q): return bool(set(q.lower().split()) & ANALYTICAL_KW) or "how many" in q.lower()

# # ─────────────────────────────────────────────
# # VISUALIZATION SUPPORT
# # ─────────────────────────────────────────────

# def _normalize_visualizations(viz_list):

#     if not isinstance(viz_list, list):
#         return []

#     normalized = []

#     for v in viz_list:

#         if not isinstance(v, dict):
#             continue

#         vtype = str(v.get("type","")).lower()

#         if vtype in ("bar","barchart","bar-chart"):
#             vtype = "bar_chart"

#         elif vtype in ("line","linechart"):
#             vtype = "line_chart"

#         elif vtype in ("pie","piechart"):
#             vtype = "pie_chart"

#         elif vtype in ("table","grid"):
#             vtype = "table"

#         item = {
#             "type": vtype,
#             "title": v.get("title","")
#         }

#         # if vtype in ("bar_chart","line_chart"):

#         #     item["xKey"] = v.get("xKey","")
#         #     item["yKey"] = v.get("yKey","")
#         #     item["data"] = v.get("data",[])

#         if vtype in ("bar_chart","line_chart"):

#             item["xKey"] = v.get("xKey","")
#             item["yKey"] = v.get("yKey","")
#             item["seriesKey"] = v.get("seriesKey","")
#             item["data"] = v.get("data",[])

#         elif vtype == "pie_chart":

#             item["data"] = v.get("data",[])

#         elif vtype == "table":

#             item["columns"] = v.get("columns",[])
#             item["data"] = v.get("data",[])

#         normalized.append(item)

#     return normalized

# def _safe_visualizations(vizs):

#     safe = []

#     for v in vizs:

#         if not isinstance(v, dict):
#             continue

#         if not v.get("type"):
#             continue

#         if not v.get("title"):
#             continue

#         safe.append(v)

#     return safe


# def _to_str(val):
#     if isinstance(val, str): return val
#     if isinstance(val, dict):
#         lines = []
#         for k, v in val.items():
#             if isinstance(v, list):
#                 lines.append(f"{k}:")
#                 for item in v:
#                     if isinstance(item, dict):
#                         lines.append("  • " + " | ".join(f"{ik}: {iv}" for ik, iv in item.items()))
#                     else:
#                         lines.append(f"  • {item}")
#             else:
#                 lines.append(f"{k}: {v}")
#         return "\n".join(lines)
#     if isinstance(val, list):
#         lines = []
#         for item in val:
#             if isinstance(item, dict):
#                 lines.append("• " + " | ".join(f"{k}: {v}" for k, v in item.items()))
#             else:
#                 lines.append(f"• {item}")
#         return "\n".join(lines)
#     return str(val) if val else ""


# SYS = """You are a senior data analyst and database expert with deep analytical reasoning capabilities.
# You have access to the user's actual database records as retrieved chunks.

# Chunk types:
#   [SCHEMA]           — table structure, column names, total row count
#   [COUNT]            — exact row counts AND all distinct values per column — PRIMARY source for counts/lists
#   [ROW]              — individual database records with all field values
#   [JOIN]             — pre-computed cross-table joins: user X has N records in table Y with details
#   [WEB]              — saved web content (raw)
#   [ANALYSIS_WEB]     — web research grouped by topic with titles and summaries
#   [ANALYSIS_DB_META] — database metadata: which databases and tables were analyzed

# DEEP ANALYSIS RULES:
# 1. Read EVERY chunk exhaustively before forming your answer.
# 2. For COUNT questions: find [COUNT] chunk with "Number of X: N" — this is authoritative.
# 3. For LIST questions: find [COUNT] chunk "All values of column_name:" — gives complete list.
# 4. For JOIN/relationship questions: find [JOIN] chunks — they show cross-table activity per user.
# 5. For WHY questions: analyze patterns, dates, sequences, frequencies across chunks to infer reasons.
# 6. For TREND questions: compare timestamps, sequences, values across [ROW] chunks.
# 7. For COMPARISON questions: pull data from multiple tables and compare side by side.
# 8. For DEEP questions: combine ROW + JOIN + COUNT chunks to give comprehensive multi-part answers.
# 9. CRITICAL: If the requested data (e.g. specific columns or metrics) does NOT exist in the context, clearly state that it is unavailable. NEVER hallucinate or invent fake names, metrics, or records.
# 10. Always answer in full sentences with specifics — no vague responses.
# 11. DO NOT include source citations in the answer text — keep answer clean.
# 12. follow_up_questions MUST follow the EXACT format specified in the user prompt.
# 13. Respond ONLY in valid JSON."""


# # ══════════════════════════════════════════════════════
# # SCHEMA GROUNDING — column ownership index + join keys
# # ══════════════════════════════════════════════════════
# # The SQL model used to *guess* which table a column lived on (e.g. it would
# # write `invoice`.`construction` when `construction` actually only exists on a
# # dimension table), causing MySQL error 1054. These helpers turn that guess
# # into a lookup: we parse the real [SCHEMA] lines and tell the model exactly
# # which table owns each column, plus the likely join keys to pull a column in
# # from another table. Data-driven, so it works for any schema.

# # Generic/structural names that are NOT meaningful join keys even if shared.
# _GENERIC_JOIN_COLS = {
#     "id", "sl", "sno", "srno", "sr", "no", "index", "idx", "row", "rownum",
#     "date", "created_at", "updated_at", "createddate", "timestamp", "ts",
#     "month", "year", "day", "status", "type", "name", "description", "value",
# }


# def _parse_schema_chunks(schema_chunks):
#     """Return {table_name: [original_col, ...]} parsed from the [SCHEMA] lines.

#     The [SCHEMA] line lists the FULL column set (it is built from row.keys()),
#     so it is authoritative. As a safety net we also harvest column names from
#     the '[COUNT] ... table:T' / 'All values of C in T:' lines, so a table is
#     still indexed even if its [SCHEMA] line is somehow missing from context."""
#     table_cols = {}

#     def _add(table, cols):
#         if not table:
#             return
#         existing = table_cols.setdefault(table, [])
#         seen = {c.lower() for c in existing}
#         for c in cols:
#             if c and c.lower() not in seen:
#                 existing.append(c)
#                 seen.add(c.lower())

#     for text in schema_chunks:
#         # current table context for "All values of C in T" lines
#         for line in str(text).splitlines():
#             if "[SCHEMA]" in line:
#                 m_tbl = re.search(r'(?:table|sheet):(\S+)', line)
#                 m_cols = re.search(r'columns:(.*?)\s+(?:total_rows|rows):', line)
#                 if not m_cols:
#                     m_cols = re.search(r'columns:(.+)$', line)
#                 if m_tbl and m_cols:
#                     cols = [c.strip() for c in m_cols.group(1).split(',') if c.strip()]
#                     _add(m_tbl.group(1), cols)
#             else:
#                 # Fallback: "All values of <col> in <table>: ..."
#                 mv = re.search(r'All values of (.+?) in (\S+?):', line)
#                 if mv:
#                     _add(mv.group(2), [mv.group(1).strip()])
#     return table_cols


# def _build_schema_grounding(schema_chunks):
#     """Build an authoritative column-location index + join-key hints string
#     that is injected into the SQL prompt so the model never mis-attributes a
#     column to the wrong table."""
#     table_cols = _parse_schema_chunks(schema_chunks)
#     if not table_cols:
#         return "", {}

#     # column (lowercased) -> set of (table, original_col)
#     col_to_tables = {}
#     for table, cols in table_cols.items():
#         for c in cols:
#             col_to_tables.setdefault(c.lower(), set()).add((table, c))

#     lines = [
#         "COLUMN LOCATION INDEX (authoritative). A column may be referenced ONLY "
#         "on a table whose list below includes it. If a column you need is not in "
#         "the table you are selecting FROM, JOIN the table that owns it.",
#     ]
#     for table, cols in table_cols.items():
#         lines.append(f"- `{table}` owns: " + ", ".join(f"`{c}`" for c in cols))

#     # Columns unique to one table are always safe to reference there.
#     # Columns shared across tables are the likely join keys.
#     join_hints = set()
#     for col, locs in col_to_tables.items():
#         tabs = sorted({t for t, _ in locs})
#         if col in _GENERIC_JOIN_COLS or len(tabs) < 2 or len(tabs) > 4:
#             continue
#         for i in range(len(tabs)):
#             for j in range(i + 1, len(tabs)):
#                 a, b = tabs[i], tabs[j]
#                 ca = next(oc for (t, oc) in locs if t == a)
#                 cb = next(oc for (t, oc) in locs if t == b)
#                 join_hints.add(f"- `{a}`.`{ca}` = `{b}`.`{cb}`")

#     out = "\n".join(lines)
#     if join_hints:
#         out += ("\n\nLIKELY JOIN KEYS (shared columns — use these to bring a "
#                 "column in from another table):\n" + "\n".join(sorted(join_hints)))
#     return out, col_to_tables


# def _unknown_column_hint(error_msg, col_to_tables):
#     """If a MySQL 1054 'Unknown column X.Y' error occurred, return a targeted
#     correction telling the model which table actually owns column Y."""
#     if not col_to_tables:
#         return ""
#     m = re.search(r"Unknown column '([^']+)'", str(error_msg))
#     if not m:
#         return ""
#     ref = m.group(1)
#     col = ref.split(".")[-1].strip("`")            # T.C or just C
#     owners = sorted({t for t, _ in col_to_tables.get(col.lower(), set())})
#     if not owners:
#         return (f"\n\nIMPORTANT: column `{col}` does not exist anywhere in the "
#                 f"schema. Do not reference it; use only columns from the COLUMN "
#                 f"LOCATION INDEX.")
#     owner_list = ", ".join(f"`{t}`" for t in owners)
#     return (f"\n\nIMPORTANT: column `{col}` does NOT exist on the table you "
#             f"referenced it on. It exists ONLY on: {owner_list}. Reference "
#             f"`{col}` on one of those tables, JOINing it in via a LIKELY JOIN "
#             f"KEY if needed.")


# def _validate_column_refs(sql, table_cols):
#     """Statically check a generated SQL string for column references that point
#     at the WRONG base table (the cause of MySQL 1054). Returns a de-duplicated
#     list of (qualifier, column) violations.

#     Only references whose qualifier is a KNOWN BASE TABLE are checked. CTE names
#     and short aliases (wd, cs, i, d, ...) are NOT base tables, so references like
#     `wd`.`dealer` are correctly ignored — we cannot and should not validate
#     derived columns."""
#     if not sql or not table_cols:
#         return []
#     lower_cols = {t: {c.lower() for c in cols} for t, cols in table_cols.items()}
#     base_tables = set(table_cols.keys())
#     violations = {}

#     # Backticked  `table`.`column`
#     for m in re.finditer(r"`([^`]+)`\s*\.\s*`([^`]+)`", sql):
#         q, c = m.group(1), m.group(2)
#         if q in base_tables and c.lower() not in lower_cols[q]:
#             violations[(q, c)] = True

#     # Unbackticked  table.column  (qualifier still must be a real base table)
#     for m in re.finditer(r"\b(\w+)\s*\.\s*(\w+)\b", sql):
#         q, c = m.group(1), m.group(2)
#         if q in base_tables and c.lower() not in lower_cols[q]:
#             violations[(q, c)] = True

#     return list(violations.keys())


# def _column_ref_correction(violations, col_to_tables):
#     """Build a forceful correction message naming each wrong reference and the
#     table that actually owns the column."""
#     lines = []
#     for (q, c) in violations:
#         owners = sorted({t for t, _ in col_to_tables.get(c.lower(), set())})
#         if owners:
#             owner_list = ", ".join(f"`{o}`" for o in owners)
#             lines.append(
#                 f"- `{q}`.`{c}` is INVALID: `{c}` is NOT a column of `{q}`. "
#                 f"`{c}` exists ONLY on {owner_list}. JOIN that table using a "
#                 f"LIKELY JOIN KEY and reference `{c}` there."
#             )
#         else:
#             lines.append(
#                 f"- `{q}`.`{c}` is INVALID: column `{c}` does not exist in any "
#                 f"table. Remove it / use only columns from the COLUMN LOCATION INDEX."
#             )
#     return ("Your previous SQL referenced columns on the WRONG table. This will "
#             "fail with error 1054. Fix EVERY issue below and return the same JSON "
#             "shape:\n" + "\n".join(lines))


# # ══════════════════════════════════════════════════════
# # BUSINESS SCHEMA MAP  (drill-down + correct dimension joins + word→column)
# # ══════════════════════════════════════════════════════
# # Fixes three failure modes that produced wrong numbers:
# #   1. Product attributes (CATEGORY/CONSTRUCTION/vehicle type) were joined from
# #      the WRONG table (a customer-keyed dealer table), causing fan-out and
# #      inflated sums. We pin them to the real product dimension joined on
# #      Material, and forbid any other source.
# #   2. "vehicle" / "vehicle type" was mapped to CATEGORY. We map words to the
# #      exact column.
# #   3. Drill-down: "top 3 tyre categories" => WHERE CATEGORY='Tyre' GROUP BY the
# #      next level (CONSTRUCTION).
# #
# # IMPORTANT: edit the CONFIG below to match your real schema. Tables are matched
# # by the columns they contain (robust to munged table names), so you usually
# # only need to keep the column/measure names correct.

# # ── CONFIG ───────────────────────────────────────────────────────────────────
# FACT_TABLE_HINTS = ["invoice"]          # name substring(s) of the fact table
# MEASURE_COLUMN   = "Invoice_Value"      # the Sales measure column on the fact table

# # Dimensions: each is auto-located as the (non-fact) table that contains its
# # `dim_key` AND the most of its `owns` columns. `fact_key` is the column on the
# # fact table that joins to `dim_key` on the dimension.
# DIMENSIONS = [
#     {
#         "label":    "product",
#         "fact_key": "Material",
#         "dim_key":  "Material",
#         "owns":     ["CATEGORY", "CONSTRUCTION", "vehicle type", "OLD CODE"],
#     },
#     {
#         "label":    "customer",
#         "fact_key": "Customer",
#         "dim_key":  "Customer",
#         "owns":     ["CUSTOMER_CATEGORY", "Region", "Zone", "Account group"],
#     },
# ]

# # Product hierarchy ROOT → LEAF (column names; matched case/space/underscore-insensitively)
# PRODUCT_HIERARCHY = ["CATEGORY", "CONSTRUCTION", "vehicle type"]

# # Natural-language phrase → exact column name. Longest phrase wins.
# COLUMN_SYNONYMS = {
#     "product category": "CATEGORY", "category": "CATEGORY", "categories": "CATEGORY",
#     "construction": "CONSTRUCTION", "tyre type": "CONSTRUCTION", "tire type": "CONSTRUCTION",
#     "vehicle category": "vehicle type", "vehicle type": "vehicle type",
#     "vehicle": "vehicle type", "by vehicle": "vehicle type",
#     "region": "Region", "zone": "Zone",
#     "dealer": "Customer", "customer": "Customer",
# }
# # ── END CONFIG ───────────────────────────────────────────────────────────────

# # Words that signal the user wants a ranked breakdown (so we apply the GROUP BY).
# _RANK_OR_BREAKDOWN_RE = re.compile(
#     r'\b(top|bottom|best|worst|highest|lowest|leading|poor|performing|rank|'
#     r'ranking|wise|breakdown|break\s*down|split|distribution|each|per|'
#     r'types?|kinds?|categor(?:y|ies)|segments?|variants?|constructions?)\b', re.I)


# def _norm_ident(s):
#     return re.sub(r'[\s_]+', '', str(s).lower())


# def _parse_value_index(schema_chunks):
#     """From '[COUNT] ...' lines 'All values of <col> in <table>: v1, v2, ...'
#     return {(table, col): [distinct original values]}."""
#     out = {}
#     for text in schema_chunks:
#         for line in str(text).splitlines():
#             m = re.search(r'All values of (.+?) in (\S+?):\s*(.+)$', line)
#             if not m:
#                 continue
#             col, table, rest = m.group(1).strip(), m.group(2).strip(), m.group(3)
#             vals = [v.strip() for v in rest.split(',') if v.strip()]
#             if not vals:
#                 continue
#             bucket = out.setdefault((table, col), [])
#             seen = {v.lower() for v in bucket}
#             for v in vals:
#                 if v.lower() not in seen:
#                     bucket.append(v)
#                     seen.add(v.lower())
#     return out


# def _orig_col(table_cols, table, col_name):
#     """Return the original-cased column name on `table` matching col_name."""
#     for c in table_cols.get(table, []):
#         if _norm_ident(c) == _norm_ident(col_name):
#             return c
#     return None


# def _build_business_map(table_cols):
#     """Resolve the CONFIG against the actually-loaded schema. Returns a dict with
#     the fact table, each dimension's real table + join keys, the column→source map
#     (so product attributes are pinned to the product table), the resolved synonym
#     map, and the ordered hierarchy levels as (table, col)."""
#     norm_tables = {t: {_norm_ident(c) for c in cols} for t, cols in table_cols.items()}

#     # Fact table: name hint match, else the table that has the measure column.
#     fact = None
#     for t in table_cols:
#         if any(h in t.lower() for h in FACT_TABLE_HINTS):
#             fact = t
#             break
#     if not fact:
#         for t, ncols in norm_tables.items():
#             if _norm_ident(MEASURE_COLUMN) in ncols:
#                 fact = t
#                 break

#     # Locate each dimension as the non-fact table containing dim_key + most owns.
#     dim_resolved = []          # list of {table, fact_key, dim_key, owns:[orig...]}
#     attr_source = {}           # norm(col) -> (table, orig_col, fact_key, dim_key)
#     for d in DIMENSIONS:
#         dk = _norm_ident(d["dim_key"])
#         owns_norm = [_norm_ident(c) for c in d["owns"]]
#         best, best_score = None, -1
#         for t, ncols in norm_tables.items():
#             if t == fact or dk not in ncols:
#                 continue
#             score = sum(1 for c in owns_norm if c in ncols)
#             if score <= 0:
#                 continue
#             # Prefer a leaner table (true dimension) on ties.
#             score = score * 100 - len(ncols)
#             if any(h in t.lower() for h in [d["label"]]):
#                 score += 50
#             if score > best_score:
#                 best, best_score = t, score
#         if not best:
#             continue
#         owns_present = [_orig_col(table_cols, best, c) for c in d["owns"]
#                         if _orig_col(table_cols, best, c)]
#         fk = _orig_col(table_cols, fact, d["fact_key"]) or d["fact_key"]
#         dkey = _orig_col(table_cols, best, d["dim_key"]) or d["dim_key"]
#         dim_resolved.append({"table": best, "fact_key": fk, "dim_key": dkey,
#                              "owns": owns_present, "label": d["label"]})
#         for oc in owns_present:
#             attr_source[_norm_ident(oc)] = (best, oc, fk, dkey)

#     # Hierarchy levels resolved to (table, orig_col), pinned to their source table.
#     levels = []
#     for h in PRODUCT_HIERARCHY:
#         nh = _norm_ident(h)
#         if nh in attr_source:
#             t, oc, _, _ = attr_source[nh]
#             levels.append((t, oc))
#         elif fact and nh in norm_tables.get(fact, set()):
#             levels.append((fact, _orig_col(table_cols, fact, h)))

#     # Resolve synonyms to real (table, col): prefer a pinned source, else fact.
#     def _plurals(p):
#         out = {p}
#         out.add(p + "s")
#         if p.endswith("y"):
#             out.add(p[:-1] + "ies")
#         return out
#     syn_resolved = {}
#     for phrase, colname in COLUMN_SYNONYMS.items():
#         nc = _norm_ident(colname)
#         loc = None
#         if nc in attr_source:
#             t, oc, _, _ = attr_source[nc]
#             loc = (t, oc)
#         elif fact and nc in norm_tables.get(fact, set()):
#             loc = (fact, _orig_col(table_cols, fact, colname))
#         if loc:
#             for variant in _plurals(phrase.lower()):
#                 syn_resolved.setdefault(variant, loc)

#     measure = _orig_col(table_cols, fact, MEASURE_COLUMN) if fact else None

#     return {
#         "fact": fact, "measure": measure or MEASURE_COLUMN,
#         "dims": dim_resolved, "attr_source": attr_source,
#         "synonyms": syn_resolved, "levels": levels,
#     }


# def _business_prompt(biz):
#     """Authoritative instruction block: exact dimension joins, the measure, the
#     forbidden sources, and word→column mapping."""
#     if not biz.get("fact"):
#         return ""
#     fact = biz["fact"]
#     out = [f"AUTHORITATIVE SCHEMA MAP (follow EXACTLY — overrides any guess):",
#            f"- Fact table: `{fact}`. Sales / performance / revenue = "
#            f"SUM(`{fact}`.`{biz['measure']}`)."]
#     forbid = []
#     for d in biz["dims"]:
#         if not d["owns"]:
#             continue
#         cols = ", ".join(f"`{c}`" for c in d["owns"])
#         out.append(
#             f"- {cols} live ONLY on `{d['table']}`. To use any of them you MUST "
#             f"JOIN `{d['table']}` ON `{fact}`.`{d['fact_key']}` = "
#             f"`{d['table']}`.`{d['dim_key']}`. NEVER read these columns from any "
#             f"other table, and NEVER join them on a different key (doing so "
#             f"multiplies rows and inflates the totals).")
#         forbid.append(f"`{d['table']}` only via `{d['fact_key']}`")
#     if biz["synonyms"]:
#         # Compact, de-duplicated word→column list
#         seen = {}
#         for phrase, (t, c) in biz["synonyms"].items():
#             seen.setdefault((t, c), []).append(phrase)
#         word_lines = []
#         for (t, c), phrases in seen.items():
#             ph = ", ".join(f'"{p}"' for p in sorted(set(phrases), key=len))
#             word_lines.append(f"  {ph} -> `{c}` (on `{t}`)")
#         out.append("WORD -> COLUMN (map the user's wording to the EXACT column):\n"
#                    + "\n".join(word_lines))
#     out.append("FAN-OUT GUARD: every dimension JOIN must be on the key above so "
#                "each fact row matches at most one dimension row. If a column name "
#                "exists on more than one table, use the table named in this map.")
#     return "\n".join(out)


# def _detect_group_columns(question, biz):
#     """Map the user's wording to the column(s) they want grouped, via synonyms.
#     Longest matching phrase wins on any overlapping span, so 'vehicle categories'
#     resolves to `vehicle type` and suppresses the bare 'categories'→CATEGORY."""
#     ql = question.lower()
#     candidates = []  # (start, end, table, col, phrase)
#     for phrase, (t, c) in biz.get("synonyms", {}).items():
#         for m in re.finditer(r'\b' + re.escape(phrase) + r'\b', ql):
#             candidates.append((m.start(), m.end(), t, c, phrase))
#     # Longest phrase first; greedily accept non-overlapping spans.
#     candidates.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
#     taken, out, seen = [], [], set()
#     for s, e, t, c, p in candidates:
#         if any(not (e <= ts or s >= te) for ts, te in taken):
#             continue  # overlaps an already-accepted (longer) phrase
#         taken.append((s, e))
#         if (t, c) not in seen:
#             seen.add((t, c))
#             out.append((t, c, p))
#     return out


# def _detect_drilldown(question, values_by_col, biz):
#     """If the question names a value at some hierarchy level (e.g. 'tyre'),
#     return {'filter': (table,col,value), 'group': (table,col)|None}."""
#     levels = biz.get("levels") or []
#     if not levels:
#         return None
#     ql = question.lower()

#     matched = None  # (level_idx, table, col, original_value)
#     for idx, (t, c) in enumerate(levels):
#         for v in values_by_col.get((t, c), []):
#             if len(v) < 2:
#                 continue
#             if re.search(r'\b' + re.escape(v.lower()) + r'\b', ql):
#                 if matched is None or len(v) > len(matched[3]):
#                     matched = (idx, t, c, v)
#     if not matched:
#         return None
#     f_idx, f_t, f_c, f_val = matched

#     # Group level: explicit deeper level the user named (via synonyms or name),
#     # else the next level down.
#     group = None
#     named = _detect_group_columns(question, biz)
#     for (gt, gc, _ph) in named:
#         for idx in range(f_idx + 1, len(levels)):
#             if (gt, gc) == levels[idx]:
#                 group = (gt, gc)
#                 break
#         if group:
#             break
#     if group is None and f_idx + 1 < len(levels):
#         group = levels[f_idx + 1]

#     return {"filter": (f_t, f_c, f_val), "group": group}


# def _drilldown_hint(spec, want_breakdown, biz):
#     f_t, f_c, f_val = spec["filter"]
#     fact = biz.get("fact") or "invoice"
#     measure = biz.get("measure") or "Invoice_Value"
#     s = (f"\n\nHIERARCHY DRILL-DOWN (apply this):\n"
#          f"- The question names '{f_val}', a value of `{f_c}` on `{f_t}`. Use it as "
#          f"a FILTER: WHERE `{f_t}`.`{f_c}` = '{f_val}' (exact value, case included).")
#     grp = spec.get("group")
#     if grp and want_breakdown:
#         g_t, g_c = grp
#         s += (f"\n- Break it down WITHIN '{f_val}': GROUP BY `{g_t}`.`{g_c}` (the "
#               f"next level down) and rank by SUM(`{fact}`.`{measure}`) DESC, applying "
#               f"the requested Top/Bottom N. Do NOT group by `{f_c}` itself. JOIN the "
#               f"dimension table(s) per the AUTHORITATIVE SCHEMA MAP.")
#     else:
#         s += "\n- Apply this filter; aggregate/rank as the question asks."
#     return s


# def _grouping_hint(question, biz):
#     """For non-drill questions, if the wording names a dimension to break by,
#     tell the model exactly which column/table to GROUP BY."""
#     named = _detect_group_columns(question, biz)
#     if not named or not _RANK_OR_BREAKDOWN_RE.search(question):
#         return ""
#     fact = biz.get("fact") or "invoice"
#     lines = ["\n\nGROUP-BY MAPPING (use these EXACT columns for the breakdown):"]
#     for (t, c, ph) in named:
#         if t == fact:
#             lines.append(f"- '{ph}' -> GROUP BY `{t}`.`{c}`.")
#         else:
#             lines.append(f"- '{ph}' -> GROUP BY `{t}`.`{c}` (JOIN `{t}` per the "
#                          f"AUTHORITATIVE SCHEMA MAP).")
#     return "\n".join(lines)


# # ══════════════════════════════════════════════════════
# # MAIN CONTROLLER
# # ══════════════════════════════════════════════════════

# def session_rag_chat_controller(get_connection_func):
#     data       = request.json or {}
#     session_id = (data.get("session_id") or "").strip()
#     question   = (data.get("question")   or "").strip()
#     history    = data.get("chat_history", [])
#     user_id    = data.get("user_id")
#     visit_number = data.get("visit_number")

#     if not session_id:
#         return jsonify({"status":"failed","statusCode":400,
#                         "message":"session_id is required"}), 400

#     v_raw = visit_number
#     calc_new_visit = False
#     if not v_raw or str(v_raw).lower() in ["new", "session_visit_new"]:
#         calc_new_visit = True
#         visit_number = 1
#     else:
#         try:
#             visit_number = int(str(v_raw).replace("session_visit_", ""))
#         except:
#             calc_new_visit = True
#             visit_number = 1

#     if calc_new_visit:
#         conn = cur = None
#         try:
#             conn = get_connection_func()
#             cur  = conn.cursor(dictionary=True)
#             cur.execute("""
#                 SELECT COALESCE(MAX(visit_number), 0) AS max_v
#                 FROM session_chat_history
#                 WHERE session_id = %s AND user_id = %s
#             """, (session_id, int(user_id) if user_id else 0))
#             row = cur.fetchone()
#             visit_number = (row["max_v"] + 1) if row else 1
#         except Exception as e:
#             visit_number = 1
#         finally:
#             if cur: cur.close()
#             if conn: conn.close()

#     # Greeting
#     if question and _is_greet(question):
#         suggested = []
#         if session_id in _CACHE:
#             chunks, _, _, _ = _CACHE[session_id]
#             sample = "\n".join(c["text"] for c in chunks if c["kind"] == "count")[:10000]
#             res = _mistral(
#                 "Respond ONLY in valid JSON.",
#                 f"Data summary:\n{sample}\n\n"
#                 "Generate exactly 5 questions. Q1 starts with 'What ', Q2 starts with 'Where ', Q3 starts with 'Why '. "
#                 "Use natural, human-readable language. DO NOT mention internal system names, folder names, or long raw database table names (like 'd__project_backend...'). Use terms like 'the data' or 'the records' instead. "
#                 'Return ONLY: {"suggested_questions":["What ...?","Where ...?","Why ...?"]}'
#             )
#             if res: suggested = res.get("suggested_questions", [])
#         return jsonify({
#             "status":"success","statusCode":200,
#             "answer":"Hi! I'm your advanced business intelligence assistant. I have full access to your session databases. Ask me anything about your business data!",
#             "follow_up_questions": suggested,
#             "visit_number": visit_number
#         }), 200

#     # Build/get store
#     try:
#         all_chunks, bm25_idx, col = _build_store(session_id, get_connection_func)
#     except Exception as e:
#         return jsonify({"status":"error","statusCode":500,
#                         "message":f"Store error: {e}"}), 500

#     if not all_chunks:
#         return jsonify({"status":"no_data","statusCode":200,
#                         "session_id":session_id,
#                         "message":"No data found for this session."}), 200

#     # Suggest / Default Report mode
#     if not question or question.startswith("default_"):
#         count_chunks = [c["text"] for c in all_chunks if c["kind"]=="count"]
#         sample = "\n".join(count_chunks)[:15000]
#         res = _mistral(SYS, f"""
# Business data summary ({len(all_chunks)} total chunks):
# {sample}

# This is a business intelligence assistant. 
# 1. Write a brief "Executive Summary" (3-4 sentences) of the overall data trends.
# 2. Generate exactly 5 "What" critical questions about the actual business data above.
#    ALL 5 questions MUST start with "What ". Focus on concerns, sudden changes, or trends.
#    Use natural language. DO NOT mention internal system names or raw tables.
# 3. Generate a category-wise trend report as a "line_chart" visualization. Extract numeric/categorical trend values from the chunks.

# Return ONLY valid JSON:
# {{
#   "answer": "Executive Summary: ...",
#   "suggested_questions": ["What ...?", "What ...?", "What ...?", "What ...?", "What ...?"],
#   "visualizations": [
#     {{
#       "type": "line_chart",
#       "title": "Category-wise Trend Report",
#       "xKey": "category",
#       "yKey": "value",
#       "data": [
#         {{"category": "A", "value": 100}},
#         {{"category": "B", "value": 200}}
#       ]
#     }}
#   ]
# }}

# CRITICAL INSTRUCTIONS FOR VISUALIZATIONS:
# 1. The object keys inside the "data" array MUST exactly match what you specify for "xKey" and "yKey".
# 2. Only include categories/points that ACTUALLY EXIST in the data. Do NOT invent missing categories with 0 values.
# """)
#         if not res:
#             return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
            
#         viz = res.get("visualizations", [])
#         visualizations = _safe_visualizations(_normalize_visualizations(viz))

#         return jsonify({
#             "status":              "success",
#             "statusCode":          200,
#             "answer":              res.get("answer", ""),
#             "suggested_questions": res.get("suggested_questions",[]),
#             "visualizations":      visualizations,
#             "visit_number":        visit_number
#         }), 200

#     # Understand + Retrieve
#     understanding = _understand(question, all_chunks)
#     hist          = _history(history)
#     print(f"[RAG] intent={understanding['intent']} tables={understanding['table_hints']} entities={understanding['entities']}")

#     # NEW ARCHITECTURE: INTENT ROUTER 
#     intent = classify_intent(question)
#     print(f"[RAG] Router classified intent: {intent}")
#     context = ""

#     if intent == "HYBRID":
#         print("[RAG] HYBRID query detected! AQL/SQL First Then RAG...")
#         schema_chunks = [c["text"] for c in all_chunks if c["kind"] in ("schema", "count")]
#         schema_context = "\n".join(schema_chunks)
#         # 1. AQL First: run execute_hybrid to get specific entity filter
#         hybrid_entities = execute_hybrid(question, get_connection_func, schema_context)
#         if hybrid_entities:
#             print(f"[RAG] Hybrid found targeted entities: {hybrid_entities[:10]}")
#             # Inject these entities so Vector Search focuses heavily on them
#             understanding["entities"].extend(hybrid_entities)
            
#         # 2. Then RAG
#         context = _retrieve(all_chunks, bm25_idx, col, question, understanding)

#     elif intent == "INSIGHT":
#         print("[RAG] INSIGHT query detected! Standard RAG...")
#         context = _retrieve(all_chunks, bm25_idx, col, question, understanding)

#     # ─────────────────────────────────────────────
#     # TEXT-TO-SQL (Bypass VectorDB for aggregations)
#     # ─────────────────────────────────────────────
#     if intent == "AGGREGATION":
#         print(" Bypassing VectorDB. Routing to Structured Database (SQL/AQL)...")
#         schema_chunks = [c["text"] for c in all_chunks if c["kind"] in ("schema", "count")]
#         schema_context = "\n".join(schema_chunks)

#         # ─────────────────────────────────────────────
#         # 1. CANONICALIZATION STEP
#         # ─────────────────────────────────────────────
#         canon_sys = """You are a Query Canonicalizer for Business Intelligence.
# Convert the user's natural language question into a structured JSON representation (Canonical Query).
# Do not generate SQL yet. Extract the core analytical components.

# Return ONLY a JSON object in this format:
# {
#   "analytical_intent": "e.g., dealer_ranking, sales_trend, total_revenue",
#   "metric": "e.g., sales, volume, discount",
#   "aggregation": "e.g., sum, count, avg",
#   "sort": "e.g., desc, asc",
#   "limit": 5
# }
# If a component is missing from the user's question, set it to null.
# """
#         canon_json = _mistral(canon_sys, f"Natural Language Question: {question}", temperature=0.0)
#         canonical_query_str = json.dumps(canon_json, indent=2) if canon_json else f'{{"raw_question": "{question}"}}'
#         print(f"[CANONICAL_QUERY] {canonical_query_str}")

#         # ─────────────────────────────────────────────
#         # 2. SQL GENERATION STEP
#         # ─────────────────────────────────────────────
#         sql_sys = """You are a Senior Data Analyst, SQL Expert, and Business Intelligence Assistant.

# PRIMARY OBJECTIVE

# Generate SQL that computes answers from the FULL DATASET.

# BUSINESS DEFINITIONS
# - Dealer = Customer
# - Sales = SUM(invoice_value)
# - Revenue = SUM(invoice_value)
# - Volume = SUM(qty)
# - Net Sales = SUM(invoice_value) - SUM(total_discount)
# - Invoice Count = COUNT(DISTINCT invoice_number)
# - Product = Material
# - Product Category = the `CATEGORY` column (Tyre, Tube, Flap, ...) — a PRODUCT attribute; join product ON invoice.Material = product.Material
# - Construction / "tyre type" / "tube type" = the `CONSTRUCTION` column (RADIAL, BIAS, ...) — a PRODUCT attribute on the product table (join on Material)
# - Vehicle / "vehicle type" / "vehicle category" = the `vehicle type` column (TRUCK, CAR, LCV, ...) — a PRODUCT attribute on the product table (join on Material). This is DIFFERENT from CATEGORY; never substitute one for the other.
# - These three (CATEGORY, CONSTRUCTION, `vehicle type`) are PRODUCT attributes keyed by Material. They are NEVER on the customer/dealer table; do not join them on Customer.
# - "category-wise" / "by category" / "product category wise" / "per category" => GROUP BY `CATEGORY`, NOT Material
# - "product-wise" / "by product" => GROUP BY Material
# - Top Dealer = Dealer ranked by Sales descending
# - Worst Dealer = Dealer ranked by Sales ascending
# - Best Performing Dealer = Dealer ranked by Sales descending
# - Lowest Performing Dealer = Dealer ranked by Sales ascending
# - Top Product = Product ranked by Sales descending
# - Worst Product = Product ranked by Sales ascending
# - Region Performance = SUM(invoice_value) grouped by region
# - Zone Performance = SUM(invoice_value) grouped by zone
# - Average Realization = SUM(invoice_value) / NULLIF(SUM(qty),0)

# AUTHORITATIVE SCHEMA MAP PRECEDENCE
# - If the user message contains an "AUTHORITATIVE SCHEMA MAP", a "GROUP-BY MAPPING",
#   or a "HIERARCHY DRILL-DOWN" block, those are RESOLVED FROM THE REAL SCHEMA and
#   OVERRIDE these generic definitions for table names, column ownership, joins,
#   filters and group-by. Follow them exactly.
# - FAN-OUT: a dimension table must be joined on its key so each fact row matches
#   at most one dimension row. Joining a product attribute on the wrong key (e.g.
#   Customer) multiplies rows and inflates SUM — never do it.


# METRIC PRIORITY
# - Whenever user asks: "Top Dealer", "Best Dealer", "Leading Dealer" -> Use: SUM(invoice_value)
# - Whenever user asks: "Worst Dealer", "Lowest Dealer", "Poor Performing Dealer" -> Use: SUM(invoice_value)
# - Never use: qty, taxable_value, gst, discount unless explicitly requested.

# DATA RELIABILITY RULES

# 1. Use schema information only to identify:

#    * tables
#    * columns
#    * relationships

# 1b. COLUMN OWNERSHIP IS NON-NEGOTIABLE. A "COLUMN LOCATION INDEX" is provided
#    in the user message listing exactly which table owns each column. Before you
#    write any column reference (`table`.`column`), verify that column appears in
#    that table's list. NEVER reference a column on a table that does not own it
#    (this causes MySQL error 1054). If a column you need lives on a different
#    table, JOIN that table using one of the provided LIKELY JOIN KEYS. Do not
#    assume a "natural"-sounding column (e.g. a product/customer attribute) lives
#    on the fact/invoice table — check the index.

# 2. Never use example values, retrieved rows, vector chunks, sample records, or context snippets to calculate business results.

# 3. Every ranking, trend, comparison, aggregation, KPI, sales metric, customer metric, dealer metric, category metric, region metric, and performance metric MUST be computed using SQL.

# 4. For Top N or Bottom N questions:

# Return ONLY the ranking result unless the user explicitly asks for:
# - monthwise analysis
# - trend analysis
# - yearly analysis
# - time series analysis

# Do not add monthly, yearly, trend, or detailed breakdowns unless explicitly requested.

# 5. For monthwise analysis:
#    Use the actual date column and aggregate by month before ranking.

# 6. Never generate SQL that ranks monthly rows directly using:
#    LIMIT N after GROUP BY month.

# 7. If the question asks for Top N entities (e.g., dealers, customers) month-wise or trend:
#    NEVER use `IN (SELECT ... LIMIT N)` because MySQL does not support LIMIT inside IN subqueries.
#    Instead, you MUST use a JOIN with a derived table:
   
#    SELECT t.entity, DATE_FORMAT(STR_TO_DATE(t.date_col, '%Y-%m-%d'), '%Y-%m') as month, SUM(t.metric) as total_sales
#    FROM `table` t
#    JOIN (
#        SELECT entity FROM `table`
#        GROUP BY entity
#        ORDER BY SUM(metric) DESC
#        LIMIT 2
#    ) as top_entities ON t.entity = top_entities.entity
#    GROUP BY t.entity, month
#    ORDER BY top_entities.total_sales DESC, month;
   
#    Adjust the DATE_FORMAT and STR_TO_DATE depending on the actual date format in the table.

# 8. Use:
#    SUM()
#    COUNT()
#    AVG()
#    MIN()
#    MAX()
#    GROUP BY
#    ORDER BY
#    HAVING

# 9. If SQL execution is possible:
#    SQL results are always more authoritative than retrieved context.

# 10. Never estimate.

# 11. Never infer missing values.

# 12. Never hallucinate business results.

# 13. PRESERVE EXACT DECIMALS: Never round monetary values in SQL unless explicitly asked. Return the exact sum with decimals intact.

# COLUMN HYGIENE
# - All numeric columns (sales, invoice_value, quantity, discount, tax) are strictly typed as DECIMAL or BIGINT in the database.
# - DO NOT use CAST or REGEXP_REPLACE or REPLACE to clean numeric columns. Just use SUM(`col`).
# - ONLY format strings if the column is explicitly a string format, but numeric columns are already typed.

# PER-GROUP TOP-N — "CATEGORY-WISE", "PER", "EACH", "BY X", "X-WISE"

# - "Top N customers per category", "category wise top N", "best N per region",
#   "top N dealers for each zone" all mean: rank WITHIN each group and keep N rows
#   from EVERY group. NEVER answer these with a single global ORDER BY ... LIMIT N
#   (that returns only the N biggest pairs overall, not N per group).
# - Use a window function partitioned by the group:
#       WITH agg AS (
#         SELECT `<group_col>` AS grp, `<entity_col>` AS entity,
#                SUM(`<value_col>`) AS metric
#         FROM `<fact>` JOIN `<dim>` ON ...
#         GROUP BY `<group_col>`, `<entity_col>`
#       ),
#       ranked AS (
#         SELECT grp, entity, metric,
#                ROW_NUMBER() OVER (PARTITION BY grp ORDER BY metric DESC) AS rn
#         FROM agg
#       )
#       SELECT grp, entity, metric FROM ranked WHERE rn <= N
#       ORDER BY grp, metric DESC;
# - Use a single global ORDER BY ... LIMIT N ONLY when the question has NO
#   per-group qualifier (plain "top N customers").

# PLAIN TOP-N vs WINDOWED TOP-N
# - A plain "top N" / "worst N" with NO per-group qualifier needs only
#   `... GROUP BY entity ORDER BY metric DESC LIMIT N`. Do NOT use a window
#   function or CTE for it — that adds a needless alias that often breaks.
# - Use the window-function pattern ONLY for per-group ("X-wise") questions.

# HIERARCHY DRILL-DOWN
# - The product data has a hierarchy (e.g. CATEGORY -> CONSTRUCTION -> VEHICLE_TYPE
#   -> ... -> MATERIAL), from broad to specific.
# - When the user NAMES A VALUE at one level (e.g. "tyre", "radial", "truck") and
#   asks for "top/worst N <something> of/within it" or any breakdown, treat the
#   named value as a FILTER (WHERE that_level = 'value') and GROUP BY the NEXT
#   level DOWN, ranking by the metric (default Sales = SUM(invoice_value)).
#   Example: "top 3 performing tyre categories" =>
#       WHERE `category` = 'Tyre'
#       GROUP BY `construction`            -- the next level below CATEGORY
#       ORDER BY SUM(`Invoice_Value`) DESC, `construction` ASC
#       LIMIT 3
#   Never GROUP BY the same level you filtered on (that returns just one row).
# - If the user explicitly names the child level ("...constructions",
#   "...vehicle types"), GROUP BY exactly that level.
# - If a HIERARCHY DRILL-DOWN block is provided in the user message, follow it
#   exactly (it tells you the filter column/value and the group-by level, both
#   resolved to real tables). JOIN across tables via the LIKELY JOIN KEYS when the
#   filter level and group level live on different tables.

# RESERVED WORDS — NEVER USE AS ALIASES
# - `RANK`, `ROW_NUMBER`, `ORDER`, `GROUP`, `DESC`, `ASC`, `ROWS`, `RANGE`,
#   `COUNT`, `SUM`, `OVER`, `PARTITION`, `DENSE_RANK`, `LAG`, `LEAD` are reserved
#   in MySQL 8.0 and will cause error 1064 if used as a column alias.
# - Name the row-number column `rn` (never `rank`). Backtick EVERY alias and
#   identifier without exception.
# DETERMINISTIC ORDERING — MANDATORY TIE-BREAKER
# - Many entities can tie on the same total (e.g. several customers at 0 sales).
#   ORDER BY the metric alone returns boundary rows in arbitrary order.
# - EVERY ranking ORDER BY must append the entity key as a tie-breaker:
#       ORDER BY total_sales DESC, `customer` ASC   -- top N
#       ORDER BY total_sales ASC,  `customer` ASC   -- worst N
# - Same inside windows: ROW_NUMBER() OVER (PARTITION BY grp ORDER BY metric DESC, `entity` ASC)

# DIALECT RULES

# 1. You MUST use valid MySQL syntax.
# 2. Do NOT use PostgreSQL functions like DATE_TRUNC.
# 3. For monthly grouping in MySQL, if the date is a string (e.g. 'DD-MM-YYYY'), parse it using STR_TO_DATE(date_col, '%d-%m-%Y') before grouping with DATE_FORMAT(..., '%Y-%m').
# 4. ALWAYS use backticks ` for table and column names.

# OUTPUT RULES

# Return ONLY valid JSON:

# {
# "db": "",
# "sql": "",
# "reasoning": ""
# }
# """
#         sql_user = f"Schemas available:\n{schema_context}\n\nOriginal Question: {question}\n\nCanonical Query (Structured Intent):\n{canonical_query_str}"
#         schema_grounding, col_to_tables = _build_schema_grounding(schema_chunks)
#         table_cols_map = _parse_schema_chunks(schema_chunks)
#         if schema_grounding:
#             sql_user += f"\n\n{schema_grounding}"

#         # Business schema map: pin product attributes to the correct dimension
#         # table/join key, map words→columns, and drill down hierarchies. This is
#         # what makes "tyre categories" (=> construction within Tyre) and "vehicle"
#         # (=> `vehicle type`, not CATEGORY) resolve correctly and without fan-out.
#         try:
#             biz = _build_business_map(table_cols_map)
#             biz_prompt = _business_prompt(biz)
#             if biz_prompt:
#                 sql_user += f"\n\n{biz_prompt}"
#                 print(f"[BIZMAP] fact={biz['fact']} measure={biz['measure']} "
#                       f"dims={[(d['label'], d['table']) for d in biz['dims']]} "
#                       f"levels={biz['levels']}")

#             values_by_col = _parse_value_index(schema_chunks)
#             drill = _detect_drilldown(question, values_by_col, biz)
#             if drill:
#                 want_breakdown = bool(_RANK_OR_BREAKDOWN_RE.search(question))
#                 sql_user += _drilldown_hint(drill, want_breakdown, biz)
#                 _f = drill["filter"]; _g = drill.get("group")
#                 print(f"[DRILLDOWN] filter {_f[0]}.{_f[1]}='{_f[2]}'"
#                       + (f" -> group by {_g[0]}.{_g[1]}" if (_g and want_breakdown) else " (filter only)"))
#             else:
#                 gh = _grouping_hint(question, biz)
#                 if gh:
#                     sql_user += gh
#                     print(f"[GROUPBY] {[ (c,p) for (_t,c,p) in _detect_group_columns(question, biz)]}")
#         except Exception as _e:
#             print(f"[BIZMAP] skipped: {_e}")

#         sql_json = _mistral(sql_sys, sql_user, temperature=0.0)

#         # ── Deterministic pre-execution guard ──────────────────────────────
#         # The model sometimes attributes a column to the wrong base table
#         # (e.g. `invoice`.`construction`), which fails at execution with 1054.
#         # Catch it statically against the real schema and force a correction
#         # BEFORE touching the database, so the first DB attempt is already right.
#         for _v in range(2):
#             cand_sql = (sql_json or {}).get("sql", "") if isinstance(sql_json, dict) else ""
#             if not cand_sql:
#                 break
#             violations = _validate_column_refs(cand_sql, table_cols_map)
#             if not violations:
#                 break
#             print(f"[SQL_VALIDATE] wrong-table column refs {violations} — regenerating before execution")
#             fix_user = (
#                 sql_user
#                 + "\n\nPREVIOUS SQL:\n" + cand_sql
#                 + "\n\n" + _column_ref_correction(violations, col_to_tables)
#             )
#             sql_json = _mistral(sql_sys, fix_user, temperature=0.0)

#         # Track failures so we never fall through to an empty-context LLM answer.
#         agg_sql_error = None
#         sql_results = []

#         if sql_json and sql_json.get("sql"):
#             target_db = sql_json.get("db", "").strip()
#             sql_query = sql_json.get("sql", "").strip()
#             print(f"[SQL_GEN] db: {target_db} | sql: {sql_query}")

#             conn_sql = cur_sql = None
#             sql_results = []
#             try:
#                 # Determine connection
#                 if not target_db:
#                     conn_sql = get_connection_func()
#                     cur_sql = conn_sql.cursor(dictionary=True)
#                 else:
#                     try:
#                         conn_sql = mysql.connector.connect(
#                             host=MYSQL_CONFIG["host"], port=MYSQL_CONFIG["port"],
#                             user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"],
#                             database=target_db, connection_timeout=10)
#                         cur_sql = conn_sql.cursor(dictionary=True)
#                     except Exception as e:
#                         print(f"[SQL_GEN] MySQL failed, trying Postgres: {e}")
#                         if PSYCOPG2_AVAILABLE:
#                             temp_conn = get_connection_func()
#                             temp_cur = temp_conn.cursor(dictionary=True)
#                             temp_cur.execute("SELECT credential FROM database_credential WHERE session_id=%s AND db_type IN ('postgresql', 'postgres')", (session_id,))
#                             pg_rows = temp_cur.fetchall()
#                             temp_cur.close()
#                             temp_conn.close()
                            
#                             for r in pg_rows:
#                                 cred = json.loads(r["credential"]) if isinstance(r["credential"], str) else r["credential"]
#                                 if cred.get("database") == target_db:
#                                     conn_sql = psycopg2.connect(
#                                         host=cred.get("host"), port=cred.get("port", 5432),
#                                         user=cred.get("username"), password=cred.get("password"),
#                                         dbname=target_db, connect_timeout=10)
#                                     cur_sql = conn_sql.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#                                     break
                
#                 if cur_sql:
#                     for _attempt in range(2):
#                         try:
#                             cur_sql.execute(sql_query)
#                             rows = cur_sql.fetchall()
#                             sql_results = [dict(r) for r in rows]

#                             # Convert non-serializable objects to string
#                             for row in sql_results:
#                                 for k, v in row.items():
#                                     if v is not None and not isinstance(v, (str, int, float, bool)):
#                                         row[k] = str(v)

#                             agg_sql_error = None
#                             print(f"[SQL_EXEC] Success! Returned {len(sql_results)} rows.")
#                             break
#                         except Exception as ex:
#                             agg_sql_error = str(ex)
#                             print(f"[SQL_EXEC] Attempt {_attempt + 1} failed: {ex}")
#                             if _attempt == 1:
#                                 break
#                             # Self-heal: send the error back to the model once.
#                             repair_user = (
#                                 f"The following MySQL query FAILED. Fix it and return the same JSON shape.\n\n"
#                                 f"SQL:\n{sql_query}\n\nMySQL error:\n{ex}\n\n"
#                                 f"Common fixes: a reserved word is used as an alias (rank, order, group, "
#                                 f"desc, asc, count, sum, rows, range) — rename the row-number alias to `rn` "
#                                 f"and backtick every identifier; verify MySQL 8.0 window syntax; keep the "
#                                 f"follow the COLUMN HYGIENE rule in the system prompt for numeric columns.\n\n"
#                                 f"Schemas:\n{schema_context}"
#                             )
#                             if schema_grounding:
#                                 repair_user += f"\n\n{schema_grounding}"
#                             # Targeted correction for 'Unknown column X.Y' (error 1054).
#                             repair_user += _unknown_column_hint(ex, col_to_tables)
#                             repair_user += f"\n\nOriginal Question: {question}"
#                             fix_json = _mistral(sql_sys, repair_user, temperature=0.0)
#                             if fix_json and fix_json.get("sql"):
#                                 sql_query = fix_json.get("sql", "").strip()
#                                 print(f"[SQL_REPAIR] Retrying with corrected SQL:\n{sql_query}")
#                             else:
#                                 break
#             except Exception as e:
#                 agg_sql_error = str(e)
#                 print(f"[SQL_EXEC] Execution failed: {e}")
#             finally:
#                 if cur_sql: 
#                     try: cur_sql.close() 
#                     except: pass
#                 if conn_sql: 
#                     try: conn_sql.close() 
#                     except: pass

#             if sql_results:
#                 # Override context with SQL results for final LLM generation
#                 context = f"SQL Query executed: {sql_query}\n\nSQL Results:\n" + json.dumps(sql_results, indent=2)

#         # AGGREGATION fail-safe: if no SQL rows were produced, do NOT let the
#         # model answer from an empty/stale context (that caused fabricated,
#         # inconsistent numbers). Constrain it to an honest "could not compute".
#         if intent == "AGGREGATION" and not sql_results:
#             _detail = f" (error: {agg_sql_error})" if agg_sql_error else ""
#             print(f"[AGGREGATION] No SQL rows — refusing to fabricate{_detail}")
#             context = (
#                 "SQL_COMPUTATION_FAILED. The structured query returned no rows"
#                 f"{_detail}. You MUST NOT fabricate, estimate, infer, or guess "
#                 "any numbers, names, totals, or rankings. Reply that the result "
#                 "could not be computed from the database for this question and "
#                 "suggest the user rephrase or retry."
#             )

#     # Graph
#     if _is_graph(question):
#         ftype        = _next_followup_type(session_id)
#         followup_ins = _followup_instruction(ftype)
#         res = _mistral(SYS, f"""
# Retrieved business data:
# {context}

# {hist}Chart request: "{question}"

# Extract actual numeric/categorical values ONLY from the data above.
# --- TREND DETECTION & LINE CHART RULES (MANDATORY CONTRACT) ---
# If the question is trend-related (contains: trend, growth, decline, increase, decrease, over time, monthly, quarterly, yearly, seasonality, pattern, historical analysis, performance over time, month-on-month, MoM, YoY):
# 1. Visualization Type MUST be Line Chart ("type": "line_chart").
# 2. X-Axis (xKey) MUST be a Date/Month/Year field. IMPORTANT: Date values MUST be aggregated and formatted by month (e.g., 'Jan 2024' or 'January') on the X-Axis.
# 3. Y-Axis (yKey) MUST be a Numeric Measure.
# 4. MUST include "seriesKey": "series" at the visualization root level.
# 5. NEVER return multiple category fields like "category": "Tube", "construction": "RADIAL" separately for a line chart. Instead, combine them into a single "series" key.
#    - Example (CATEGORY + CONSTRUCTION): "series": "Tube - RADIAL"
#    - Example (CATEGORY + CONSTRUCTION + VEHICLE TYPE): "series": "Tyre - RADIAL - Truck"
#    - Example (VEHICLE TYPE ONLY): "series": "Truck"
# 6. NEVER use Pie Chart or Table as primary visualization for trend queries.
# 7. NEVER auto-detect legend. Always use seriesKey.
# 8. Every row in "data" MUST contain the exact key "series" (matching seriesKey) and the xKey and yKey.
# 9. CRITICAL: For any time-series data or trend charts, the items inside the "data" array MUST be sorted strictly in chronological order (e.g., Jan, Feb, Mar or April, May, June) so the graph renders correctly from left to right.
# 9. For every line chart row:

# REQUIRED FORMAT:

# {{
#   "month": "...",
#   "invoice_value": 123,
#   "series": "..."
# }}

# 10. NEVER use keys like:

# category
# category_construction
# category_type
# group
# legend

# Use ONLY:

# series

# 11. If CATEGORY + CONSTRUCTION + VEHICLE TYPE exists:

# series =
# CATEGORY + " - " + CONSTRUCTION + " - " + VEHICLE_TYPE

# Example:

# "Tyre - RADIAL - Truck"

# 12. seriesKey MUST ALWAYS be:

# "series"
# ------------------------------------------
# {followup_ins}
# Return ONLY valid JSON in this format:
# {{
#   "answer": "A short analytical summary of the chart and trends shown.",
#   "follow_up_questions": ["Question 1", "Question 2"],
#   "visualizations": [
#     {{
#       "type": "line_chart",
#       "title": "...",
#       "xKey": "...",
#       "yKey": "...",
#       "seriesKey": "...",
#       "data": [
#         {{ "x_key_name": "...", "y_key_name": 123, "series": "..." }}
#       ]
#     }}
#   ]
# }}
# """)
#         if not res:
#             return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500

#         _advance_turn(session_id)

#         fuq_raw = res.get("follow_up_questions",[])
#         fuq = []
#         if isinstance(fuq_raw, list):
#             for q in fuq_raw:
#                 if isinstance(q, dict) and "question" in q:
#                     fuq.append(q["question"])
#                 elif isinstance(q, str):
#                     fuq.append(q)
                    
#         visualizations = _safe_visualizations(_normalize_visualizations(res.get("visualizations", [])))

#         _save_history(
#             get_connection_func,
#             session_id,
#             user_id,
#             question,
#             json.dumps(res.get("datasets",[])),
#             fuq,
#             understanding["intent"],
#             "graph",
#             visualizations=visualizations,
#             visit_number=visit_number
#         )

#         return jsonify({
#             "status": "success",
#             "statusCode": 200,
#             "answer": res.get("answer", "Here is the visualization for your request."),
#             "follow_up_questions": fuq,
#             "visualizations": visualizations,
#             "visit_number": visit_number
#         }), 200



#     # Report
#     if _is_report(question):
#         ftype        = _next_followup_type(session_id)
#         followup_ins = _followup_instruction(ftype)
#         res = _mistral(SYS, f"""
# You are a senior business analyst. Write a comprehensive report from the business data below.
# Retrieved data:
# {context}

# {hist}Report request: "{question}"

# Write an analytical business report using ONLY the data above.
# Be specific — use actual numbers, names, values from the data.
# {followup_ins}
# Return ONLY:
# {{
#   "report_title":"...",
#   "sections":[{{"heading":"...","content":"..."}}],
#   "key_findings":["Finding 1","Finding 2","Finding 3"],
#   "follow_up_questions":[]
# }}
# """)
#         if not res:
#             return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
#         _advance_turn(session_id)
#         fuq = res.get("follow_up_questions",[])
#         _save_history(get_connection_func, session_id, user_id,
#                       question, res.get("report_title",""), fuq, understanding["intent"], "report", visit_number=visit_number)
#         return jsonify({
#             "status":     "success",
#             "statusCode": 200,
#             "report": {
#                 "title":        res.get("report_title",""),
#                 "sections":     res.get("sections",[]),
#                 "key_findings": res.get("key_findings",[])
#             },
#             "follow_up_questions": fuq,
#             "visit_number": visit_number
#         }), 200

#     # Answer
#     ftype        = _next_followup_type(session_id)
#     followup_ins = _followup_instruction(ftype)

#     # Detect multi-part questions and add explicit instruction
#     q_parts = [p.strip() for p in re.split(r'[?]\s+(?=[A-WY-Z])', question) if len(p.strip()) > 8]
#     multi_hint = (
#         f"\nNOTE: This question has {len(q_parts)} parts. Address EACH part with a clear numbered heading."
#         if len(q_parts) > 1 else ""
#     )

#     res = _mistral(SYS, f"""
# You are an advanced business intelligence AI — like Claude or GPT — specialized in analyzing actual business database records.
# This is NOT a general chatbot. Every answer must be grounded in the business data provided below.

# Retrieved data (read ALL carefully):
# {context}

# {hist}Business Question: "{question}"

# Detected intent: {understanding['intent']}
# Relevant tables: {understanding['table_hints']}

# {multi_hint}
# DEEP ANALYSIS PROTOCOL:

# 1. Exhaustively scan every piece of data.

# 2. If SQL execution results are present, SQL Results are the ONLY source of truth.

# 3. If SQL Results are present:

#    * Use only the rows and columns returned by SQL.
#    * Do not invent additional fields.
#    * Do not invent customer names.
#    * Do not invent dealer names.
#    * Do not invent dates.
#    * Do not invent transaction counts.
#    * Do not invent averages.
#    * Do not invent percentages.
#    * Do not invent churn risk.
#    * Do not invent engagement metrics.
#    * Do not invent business explanations.

# 4. Never infer reasons, causes, operational issues, market conditions, pricing issues, supply chain issues, customer behavior, customer intent, customer satisfaction, loyalty, churn risk, promotional response, business strategy, or recommendations unless those values explicitly exist in the SQL result.

# 5. If SQL returns:

#    * customer
#    * invoice_value
#    * qty

# Then answer only from those fields.

# 6. If a field is not present in SQL Results, state that the information is not available.

# 7. Never convert customer IDs into names unless SQL explicitly returns a name column.

# 8. Never create fictional examples such as:

#    * John Smith
#    * Emily Davis
#    * Customer 1003
#    * Customer 1005
#      or any other names not present in SQL results.

# 9. Never create:

#    * last purchase date
#    * average transaction value
#    * engagement score
#    * churn probability
#    * campaign response
#    * inactivity period
#      unless explicitly returned by SQL.

# 10. Answer strictly from SQL Results and retrieved context.

# 11. Accuracy is more important than completeness.

# 12. If SQL Results exist, ignore any conflicting RAG content.

# SQL RESULT PRIORITY RULE

# When SQL Results are present:

# SQL Results > Retrieved Context > General Reasoning

# Always trust SQL Results.
# Never override SQL Results with assumptions.

# 8. Do NOT include "(source:...)" tags in the answer text.
# 9. {followup_ins}

# VISUALIZATION RULES:

# If the question involves comparison, distribution, ranking, trends, or category breakdown,
# generate up to 3 visualizations.

# --- AGGREGATION VISUALIZATION RULES (MANDATORY CONTRACT) ---
# If the question involves aggregate data:

# 1. Always include a Table visualization.

# 2. If the data contains a time dimension
#    (month, date, quarter, year),
#    also include a Line Chart.

# 3. For ranking or Top/Bottom questions without time,
#    return only a Table
#    (optional Bar Chart if useful).

# 4. Never generate a Line Chart when no time dimension exists.
# --- TREND DETECTION & LINE CHART RULES (MANDATORY CONTRACT) ---
# If the question is trend-related (contains: trend, growth, decline, increase, decrease, over time, monthly, quarterly, yearly, seasonality, pattern, historical analysis, performance over time, month-on-month, MoM, YoY):
# 1. Visualization Type MUST be Line Chart ("type": "line_chart").
# 2. X-Axis (xKey) MUST be a Date/Month/Year field. IMPORTANT: Date values MUST be aggregated and formatted by month (e.g., 'Jan 2024' or 'January') on the X-Axis.
# 3. Y-Axis (yKey) MUST be a Numeric Measure.
# 4. MUST include "seriesKey": "series" at the visualization root level.
# 5. NEVER return multiple category fields like "category": "Tube", "construction": "RADIAL" separately for a line chart. Instead, combine them into a single "series" key.
#    - Example (CATEGORY + CONSTRUCTION): "series": "Tube - RADIAL"
#    - Example (CATEGORY + CONSTRUCTION + VEHICLE TYPE): "series": "Tyre - RADIAL - Truck"
#    - Example (VEHICLE TYPE ONLY): "series": "Truck"
# 6. NEVER use Pie Chart or Table as primary visualization for trend queries.
# 7. NEVER auto-detect legend. Always use seriesKey.
# 8. Every row in "data" MUST contain the exact key "series" (matching seriesKey) and the xKey and yKey.
# 9. CRITICAL: For any time-series data or trend charts, the items inside the "data" array MUST be sorted strictly in chronological order (e.g., Jan, Feb, Mar or April, May, June) so the graph renders correctly from left to right.
# ------------------------------------------

# CRITICAL INSTRUCTIONS FOR ALL VISUALIZATIONS:
# 1. The object keys inside the "data" array MUST exactly match what you specify for "xKey" and "yKey".
# 2. Only include categories/points that ACTUALLY EXIST in the data. Do NOT invent missing categories with 0 values.

# Supported visualization types:

# 1️⃣ Bar Chart

# {{
# "type":"bar_chart",
# "title":"...",
# "xKey":"...",
# "yKey":"...",
# "data":[
#  {{"category":"A","value":100}},
#  {{"category":"B","value":200}}
# ]
# }}

# 2️⃣ Pie Chart

# {{
# "type":"pie_chart",
# "title":"...",
# "data":[
#  {{"name":"Category A","value":120}},
#  {{"name":"Category B","value":80}}
# ]
# }}

# 3️⃣ Table

# {{
# "type":"table",
# "title":"...",
# "columns":[
#  {{"key":"columnKey","label":"Column Label"}}
# ],
# "data":[
#  {{"columnKey":"value"}}
# ]
# }}

# Return ALL visualizations inside the "visualizations" array.
# You may return multiple charts or tables if useful.




# Return ONLY valid JSON (answer must be a plain text string):
# {{"answer":"...","follow_up_questions":[], "visualizations":[]}}
# """)

#     if not res:
#         return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500

#     clean_answer = _to_str(res.get("answer",""))
#     clean_answer = re.sub(r'\s*\(source:[^)]*\)', '', clean_answer).strip()
#     clean_answer = re.sub(r'\s*\[source:[^\]]*\]', '', clean_answer).strip()

#     fuq = res.get("follow_up_questions", [])

#     visualizations = _safe_visualizations(
#         _normalize_visualizations(res.get("visualizations", []))
#     )

#     # If user explicitly asked for table → keep only table
#     if "table" in question.lower():
#         visualizations = [v for v in visualizations if v.get("type") == "table"]



#     _advance_turn(session_id)
#     _save_history(get_connection_func, session_id, user_id,
#                   question, clean_answer, fuq, understanding["intent"], "answer", visualizations=visualizations, visit_number=visit_number)

#     return jsonify({
#         "status": "success",
#         "statusCode": 200,
#         "answer": clean_answer,
#         "follow_up_questions": fuq,
#         "visualizations": visualizations,
#         "visit_number": visit_number,
#         "sql_query": sql_query,
#         "chat_id": active_chat_id
#     }), 200

