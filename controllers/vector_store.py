# controllers/vector_store.py

import os
import re
import uuid
import hashlib
import threading
import chromadb
from chromadb.config import Settings
from database.config import CHROMA_PERSIST_DIR, CHROMA_COLLECTION_NAME

# ---------------------------
# Persistent Chroma Client
# ---------------------------
try:
    _chroma_client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
except Exception as _e:
    _chroma_client = chromadb.Client(
        Settings(
            persist_directory=CHROMA_PERSIST_DIR,
            anonymized_telemetry=False
        )
    )

_collection = None
_embedding_model = None
_model_lock = threading.Lock()


def _get_embedding_model():
    """
    Load SentenceTransformer embedding model from local directory if present,
    otherwise fallback to sentence-transformers model identifier.
    """
    global _embedding_model
    if _embedding_model is None:
        with _model_lock:
            if _embedding_model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    model_path = os.path.join(base_dir, 'local_models', 'all-MiniLM-L6-v2')
                    if os.path.exists(model_path):
                        _embedding_model = SentenceTransformer(model_path)
                    else:
                        _embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
                    print("[VectorStore] SentenceTransformer loaded successfully.")
                except Exception as e:
                    print(f"[VectorStore] Failed to load SentenceTransformer: {e}")
                    _embedding_model = None
    return _embedding_model


def _encode_texts(texts):
    """
    Encode list of texts using local SentenceTransformer model.
    """
    if not texts:
        return []
    model = _get_embedding_model()
    if model is not None:
        try:
            return model.encode(texts, batch_size=64, show_progress_bar=False).tolist()
        except Exception as e:
            print(f"[VectorStore] Embedding encoding error: {e}")
    return None


def resolve_chroma_collection_name(session_id: str, conn=None) -> str:
    """
    Resolves the ChromaDB collection name for a given session.
    Checks workspaces.workspace_chroma_collection in MySQL first;
    falls back to deterministic s_<md5(session_id)[:12]>.
    """
    col_name = None
    if conn and session_id:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT workspace_chroma_collection FROM workspaces WHERE session_id = %s", (session_id,))
            row = cursor.fetchone()
            if row and row.get("workspace_chroma_collection"):
                col_name = row["workspace_chroma_collection"]
            cursor.close()
        except Exception as e:
            print(f"[VectorStore] DB lookup error for collection name: {e}")

    if not col_name:
        sid_str = str(session_id or "default")
        col_name = "s_" + hashlib.md5(sid_str.encode()).hexdigest()[:12]

    # Sanitize according to Chroma collection name rules:
    # 3-63 characters, start and end with alnum, contains only alnum, '_', or '-'
    clean = re.sub(r'[^a-zA-Z0-9_-]', '_', str(col_name))
    if len(clean) < 3:
        clean = f"col_{clean}"
    clean = clean[:63]
    return clean


def get_global_collection(collection_name=None):
    """
    Returns a ChromaDB collection. If collection_name is provided, it returns that specific collection.
    Otherwise, it returns the singleton global ChromaDB collection.
    """
    if collection_name:
        return _chroma_client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )

    global _collection
    if _collection is None:
        _collection = _chroma_client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
    return _collection


def push_unstructured_to_vector_db(filename, text_content, metadata=None, collection_name=None):
    """
    Push unstructured document text into ChromaDB (chunked).
    """
    if not text_content or not text_content.strip():
        return

    collection = get_global_collection(collection_name)

    chunk_size = 800
    overlap = 100
    words = text_content.split()

    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)

    if not chunks:
        return

    ids = [str(uuid.uuid4()) for _ in chunks]
    metadatas = [
        {
            "source_file": filename,
            "type": "unstructured",
            **(metadata or {})
        }
        for _ in chunks
    ]

    embeds = _encode_texts(chunks)
    if embeds is not None:
        collection.upsert(
            documents=chunks,
            embeddings=embeds,
            metadatas=metadatas,
            ids=ids
        )
    else:
        collection.upsert(
            documents=chunks,
            metadatas=metadatas,
            ids=ids
        )


def push_session_analysis_and_kgraph_to_vector_db(
    session_id: str,
    report: str = None,
    kgraph: dict = None,
    db_data: list = None,
    web_data: list = None,
    ontology_analysis: dict = None,
    collection_name: str = None,
    conn = None
) -> dict:
    """
    Step 4: Push generated analysis summary report, RtM Golden Ontology Gap Analysis,
    K-Graph entities/nodes, schema definitions, core KPIs, business insights,
    web research data, and relationship edges into ChromaDB.
    Uses local SentenceTransformer embedding model and persists collection.
    """
    if not session_id:
        return {"status": "skipped", "message": "No session_id provided"}

    col_name = collection_name or resolve_chroma_collection_name(session_id, conn=conn)

    # If workspace_chroma_collection was null in DB, save resolved col_name
    if conn:
        try:
            up_cur = conn.cursor()
            up_cur.execute(
                "UPDATE workspaces SET workspace_chroma_collection = %s WHERE session_id = %s AND (workspace_chroma_collection IS NULL OR workspace_chroma_collection = '')",
                (col_name, session_id)
            )
            conn.commit()
            up_cur.close()
        except Exception:
            pass

    collection = _chroma_client.get_or_create_collection(
        name=col_name,
        metadata={"hnsw:space": "cosine"}
    )

    documents = []
    metadatas = []
    ids = []

    # 1. Chunk and Ingest Analysis Report
    if report and isinstance(report, str) and report.strip():
        paragraphs = [p.strip() for p in re.split(r'\n{2,}|\n(?=###?\s+)', report) if p.strip()]
        for idx, para in enumerate(paragraphs):
            doc_text = f"[Analysis Summary - Session {session_id}]\n{para}"
            doc_id = f"{session_id}_report_{idx}"
            documents.append(doc_text)
            metadatas.append({
                "session_id": str(session_id),
                "source": "session_analysis",
                "kind": "analysis_report",
                "chunk_index": idx
            })
            ids.append(doc_id)

    # 2. Ingest RtM Golden Ontology Benchmark & Gap Analysis
    if ontology_analysis and isinstance(ontology_analysis, dict):
        score = ontology_analysis.get("completeness_score") or ontology_analysis.get("completion_percentage") or 0
        mapped_raw = ontology_analysis.get("mapped_vertices", [])
        missing_raw = ontology_analysis.get("missing_vertices", [])

        mapped_names = [
            (m.get("name") if isinstance(m, dict) else str(m)) for m in mapped_raw
        ]
        missing_names = [
            (m.get("name") if isinstance(m, dict) else str(m)) for m in missing_raw
        ]

        ai_an = ontology_analysis.get("ai_analysis") or {}
        recs = ""
        if isinstance(ai_an, dict) and ai_an.get("actionable_recommendations"):
            rec_items = ai_an.get("actionable_recommendations", [])
            if isinstance(rec_items, list):
                recs = "; ".join([(r.get("recommendation", str(r)) if isinstance(r, dict) else str(r)) for r in rec_items])
            else:
                recs = str(rec_items)
        if not recs:
            recs = str(ontology_analysis.get("recommendations", "None"))

        onto_summary = (
            f"[Route-to-Market (RtM) Golden Ontology Benchmark - Session {session_id}]\n"
            f"Completion Score: {score}%\n"
            f"Mapped Entities ({len(mapped_names)}): {', '.join(mapped_names) if mapped_names else 'None'}\n"
            f"Missing Critical Vertices ({len(missing_names)}): {', '.join(missing_names) if missing_names else 'None'}\n"
            f"Benchmark Recommendations: {recs}"
        )
        documents.append(onto_summary)
        metadatas.append({
            "session_id": str(session_id),
            "source": "kgraph_ontology_comparator",
            "kind": "ontology_gap_analysis",
            "score": float(score) if isinstance(score, (int, float)) else 0.0
        })
        ids.append(f"{session_id}_ontology_summary")

        # Ingest vertex-level mappings
        for m in mapped_raw:
            if isinstance(m, dict):
                v_name = m.get("name") or m.get("ontology_id")
                matched_tbls = ", ".join(m.get("matched_imported_tables", []))
                cov = m.get("attribute_coverage_pct", 0)
                doc_text = (
                    f"[Ontology Vertex Mapping] Golden Node: {v_name} | "
                    f"Matched Views/Tables: {matched_tbls or 'N/A'} | "
                    f"Attribute Coverage: {cov}%"
                )
                documents.append(doc_text)
                metadatas.append({
                    "session_id": str(session_id),
                    "source": "kgraph_ontology_comparator",
                    "kind": "ontology_vertex_mapping",
                    "vertex": str(v_name),
                    "coverage": float(cov) if isinstance(cov, (int, float)) else 0.0
                })
                clean_v = re.sub(r'[^a-zA-Z0-9_]', '_', str(v_name))
                ids.append(f"{session_id}_onto_v_{clean_v}")

    # 3. Ingest K-Graph Entities / Nodes
    if kgraph and isinstance(kgraph, dict):
        nodes = kgraph.get("nodes", [])
        for idx, n in enumerate(nodes):
            if not isinstance(n, dict):
                continue
            n_name = n.get("name") or n.get("id") or n.get("label") or f"node_{idx}"
            n_role = n.get("role") or n.get("type", "entity")
            n_label = n.get("business_label") or n.get("label") or ""
            n_cols = n.get("columns") or (n.get("props", {}).get("columns", []))
            n_metrics = n.get("key_metrics") or []
            
            cols_str = ", ".join(n_cols[:30]) if isinstance(n_cols, list) else str(n_cols)
            metrics_str = ", ".join(n_metrics) if isinstance(n_metrics, list) else str(n_metrics)
            
            doc_text = (
                f"[K-Graph Node] Entity: {n_name} | Role: {n_role} | Business Label: {n_label} | "
                f"Columns: {cols_str or 'N/A'} | Key Metrics: {metrics_str or 'N/A'}"
            )
            documents.append(doc_text)
            metadatas.append({
                "session_id": str(session_id),
                "source": "kgraph_engine",
                "kind": "kgraph_node",
                "table": str(n_name),
                "role": str(n_role)
            })
            clean_node_id = re.sub(r'[^a-zA-Z0-9_]', '_', str(n_name))
            ids.append(f"{session_id}_kg_node_{clean_node_id}_{idx}")

        # Ingest K-Graph Edges / Relationships
        edges = kgraph.get("edges", [])
        for idx, e in enumerate(edges):
            if not isinstance(e, dict):
                continue
            src = e.get("from") or e.get("source") or e.get("table1") or ""
            dst = e.get("to") or e.get("target") or e.get("table2") or ""
            rel = e.get("relationship") or e.get("label") or "CONNECTS_TO"
            cond = e.get("join_condition") or f"{e.get('column1', '')} = {e.get('column2', '')}"
            card = e.get("cardinality") or ""
            
            doc_text = (
                f"[K-Graph Relationship] {src} -[{rel}]-> {dst} | "
                f"Join Condition: {cond} | Cardinality: {card or 'unspecified'}"
            )
            documents.append(doc_text)
            metadatas.append({
                "session_id": str(session_id),
                "source": "kgraph_engine",
                "kind": "kgraph_edge",
                "source_table": str(src),
                "target_table": str(dst),
                "relationship": str(rel)
            })
            clean_edge_id = f"{re.sub(r'[^a-zA-Z0-9_]', '_', str(src))}_{re.sub(r'[^a-zA-Z0-9_]', '_', str(dst))}"
            ids.append(f"{session_id}_kg_edge_{clean_edge_id}_{idx}")

    # 4. Ingest Schemas and Views from db_data
    if db_data and isinstance(db_data, list):
        for db in db_data:
            ext_db = db.get("external_database") or "Database"
            for t_idx, t in enumerate(db.get("tables", [])):
                t_name = t.get("table_name", f"table_{t_idx}")
                cols = t.get("columns", [])
                aggs = t.get("business_aggregates", [])
                cols_str = ", ".join(cols[:40]) if isinstance(cols, list) else str(cols)
                aggs_str = "; ".join(aggs[:10]) if isinstance(aggs, list) else str(aggs)

                doc_text = (
                    f"[Schema Definition] Object: {t_name} | Source: {ext_db} | "
                    f"Columns ({len(cols)}): {cols_str} | Profile/Stats: {aggs_str or 'None'}"
                )
                documents.append(doc_text)
                metadatas.append({
                    "session_id": str(session_id),
                    "source": "metadata_discovery",
                    "kind": "table_schema",
                    "database": str(ext_db),
                    "table": str(t_name)
                })
                clean_t = re.sub(r'[^a-zA-Z0-9_]', '_', str(t_name))
                ids.append(f"{session_id}_schema_{clean_t}_{t_idx}")

            # Ingest Core KPIs (if pre-computed)
            core_kpis = db.get("core_kpis") or {}
            if isinstance(core_kpis, dict) and core_kpis:
                for kpi_name, kpi_val in core_kpis.items():
                    if not kpi_val:
                        continue
                    kpi_text = f"[Core KPI Metric] Metric: {kpi_name} | Data: {kpi_val}"
                    documents.append(kpi_text)
                    metadatas.append({
                        "session_id": str(session_id),
                        "source": "deterministic_kpi_engine",
                        "kind": "core_kpi",
                        "metric": str(kpi_name)
                    })
                    clean_kpi = re.sub(r'[^a-zA-Z0-9_]', '_', str(kpi_name))
                    ids.append(f"{session_id}_kpi_{clean_kpi}")

            # Ingest Executed Insights (if available)
            insights = db.get("executed_insights") or []
            for ins_idx, ins in enumerate(insights):
                ins_text = f"[Business Insight] {ins}"
                documents.append(ins_text)
                metadatas.append({
                    "session_id": str(session_id),
                    "source": "executed_insights",
                    "kind": "business_insight",
                    "index": ins_idx
                })
                ids.append(f"{session_id}_ins_{ins_idx}")

    # 5. Ingest Web Research Data (if available)
    if web_data and isinstance(web_data, list):
        for w_idx, w in enumerate(web_data):
            topic = w.get("topic") or "General"
            for item_idx, item in enumerate(w.get("items", [])):
                title = item.get("title", "")
                url = item.get("url", "")
                brief = item.get("brief", "")
                doc_text = f"[Web Source] Topic: {topic} | Title: {title} | URL: {url}\nSummary: {brief}"
                documents.append(doc_text)
                metadatas.append({
                    "session_id": str(session_id),
                    "source": "web_search",
                    "kind": "web_data",
                    "topic": str(topic)
                })
                ids.append(f"{session_id}_web_{w_idx}_{item_idx}")

    if not documents:
        return {"status": "empty", "message": "No data found to push to vector DB", "collection": col_name}

    # Deduplicate IDs if any duplicates occurred
    unique_docs = []
    unique_metas = []
    unique_ids = []
    seen_ids = set()
    for d, m, i in zip(documents, metadatas, ids):
        final_id = i
        cnt = 1
        while final_id in seen_ids:
            final_id = f"{i}_{cnt}"
            cnt += 1
        seen_ids.add(final_id)
        unique_docs.append(d)
        unique_metas.append(m)
        unique_ids.append(final_id)

    # Encode with SentenceTransformer
    embeddings = _encode_texts(unique_docs)

    # Upsert in batches of 100 to avoid payload size limits
    batch_size = 100
    total_added = len(unique_docs)
    for b_start in range(0, total_added, batch_size):
        b_end = b_start + batch_size
        b_docs = unique_docs[b_start:b_end]
        b_metas = unique_metas[b_start:b_end]
        b_ids = unique_ids[b_start:b_end]
        b_embeds = embeddings[b_start:b_end] if embeddings is not None else None

        if b_embeds is not None:
            collection.upsert(
                documents=b_docs,
                embeddings=b_embeds,
                metadatas=b_metas,
                ids=b_ids
            )
        else:
            collection.upsert(
                documents=b_docs,
                metadatas=b_metas,
                ids=b_ids
            )

    print(f"[VectorStore] Successfully upserted {total_added} vectors into ChromaDB collection '{col_name}' for session '{session_id}'.")
    return {
        "status": "success",
        "collection": col_name,
        "count": total_added
    }


# Convenient alias
push_session_kgraph_to_vector_db = push_session_analysis_and_kgraph_to_vector_db
