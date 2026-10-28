"""
hana_kgraph_service.py — Knowledge Graph for SAP HANA, built STRICTLY from the
metadata stored in MySQL `sap_hana_view_metadata` (Step 2 of the connector).

    discover_hana_metadata()  ->  MySQL sap_hana_view_metadata
                                         |
    build_hana_kgraph_from_metadata()  <-+   (no HANA connection is opened here)
        1. load stored metadata from MySQL
        2. build a metadata-only payload (allow-listed fields; no rows / samples)
        3. LLM proposes nodes, edges, hierarchies, synonyms, metrics
        4. validate every proposal against the metadata (drop invented names)
        5. enrich edges with declared FKs + metadata-inferred keys
        6. upsert into ArangoDB (workspace DB, session_nodes / session_edges)
        7. persist full graph + build status in MySQL `sap_hana_kgraph`

Raw data records of master or transaction views are never read, sent to the
LLM, or stored — only structure.
"""

import os
import re
import json
import time
import hashlib
import threading

import pymysql

from database.config import MYSQL_CONFIG

STATUS_TABLE = "sap_hana_kgraph"
NODE_SOURCE = "sap_hana_metadata"
_PROMPT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "prompts", "hana_kgraph_build_prompt.txt")

# Only these keys may ever reach the LLM payload (defence against data leakage).
_ALLOWED_VIEW_KEYS = {"schema", "object_type", "role_hint", "row_count", "comment",
                      "primary_keys", "foreign_keys", "relationships", "technical_summary", "columns"}
_ALLOWED_COLUMN_KEYS = {"name", "type", "length", "scale", "nullable", "comment", "is_primary_key"}

_build_locks = {}
_build_locks_guard = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# MySQL helpers / status tracking
# ─────────────────────────────────────────────────────────────────────────────
def _mysql_conn(dict_cursor=True):
    return pymysql.connect(
        host=MYSQL_CONFIG["host"],
        port=MYSQL_CONFIG.get("port", 3306),
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"],
        cursorclass=pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor,
        autocommit=True,
    )


def _ensure_status_table(cur):
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {STATUS_TABLE} (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NULL,
        connection_id INT NOT NULL,
        session_id VARCHAR(100) NOT NULL DEFAULT '',
        status VARCHAR(20) NOT NULL DEFAULT 'queued',
        node_count INT DEFAULT 0,
        edge_count INT DEFAULT 0,
        hierarchy_count INT DEFAULT 0,
        synonym_count INT DEFAULT 0,
        metric_count INT DEFAULT 0,
        llm_status VARCHAR(20) NULL,
        validation_report JSON NULL,
        graph JSON NULL,
        error TEXT NULL,
        started_at TIMESTAMP NULL,
        finished_at TIMESTAMP NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_hana_kg_conn_session (connection_id, session_id),
        INDEX idx_hana_kg_session (session_id)
    );
    """)


def set_hana_kgraph_status(connection_id, session_id, status, user_id=None, counts=None,
                           graph=None, error=None, llm_status=None, report=None):
    """UPSERT the build status row for (connection_id, session_id)."""
    counts = counts or {}
    try:
        conn = _mysql_conn()
        try:
            with conn.cursor() as cur:
                _ensure_status_table(cur)
                started = "CURRENT_TIMESTAMP" if status in ("queued", "building") else "started_at"
                finished = "CURRENT_TIMESTAMP" if status in ("ready", "error") else "NULL"
                cur.execute(f"""
                    INSERT INTO {STATUS_TABLE}
                      (user_id, connection_id, session_id, status, node_count, edge_count,
                       hierarchy_count, synonym_count, metric_count, llm_status, validation_report,
                       graph, error, started_at, finished_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,{finished})
                    ON DUPLICATE KEY UPDATE
                      user_id=COALESCE(VALUES(user_id), user_id),
                      status=VALUES(status),
                      node_count=VALUES(node_count), edge_count=VALUES(edge_count),
                      hierarchy_count=VALUES(hierarchy_count), synonym_count=VALUES(synonym_count),
                      metric_count=VALUES(metric_count),
                      llm_status=VALUES(llm_status),
                      validation_report=COALESCE(VALUES(validation_report), validation_report),
                      graph=COALESCE(VALUES(graph), graph),
                      error=VALUES(error),
                      started_at={started},
                      finished_at={finished}
                """, (
                    user_id, connection_id, session_id or "", status,
                    counts.get("nodes", 0), counts.get("edges", 0), counts.get("hierarchies", 0),
                    counts.get("synonyms", 0), counts.get("metrics", 0), llm_status,
                    json.dumps(report) if report is not None else None,
                    json.dumps(graph, default=str) if graph is not None else None,
                    (str(error)[:4000] if error else None),
                ))
        finally:
            conn.close()
    except Exception as e:  # status tracking must never break the build
        print(f"[HANA-KGRAPH] status update failed ({status}): {e}")


def get_hana_kgraph_status(session_id=None, connection_id=None, include_graph=False):
    conn = _mysql_conn()
    try:
        with conn.cursor() as cur:
            _ensure_status_table(cur)
            cols = ("id, user_id, connection_id, session_id, status, node_count, edge_count, "
                    "hierarchy_count, synonym_count, metric_count, llm_status, validation_report, "
                    "error, started_at, finished_at, updated_at")
            if include_graph:
                cols += ", graph"
            q = f"SELECT {cols} FROM {STATUS_TABLE} WHERE 1=1"
            params = []
            if session_id:
                q += " AND session_id=%s"; params.append(session_id)
            if connection_id:
                q += " AND connection_id=%s"; params.append(int(connection_id))
            q += " ORDER BY updated_at DESC"
            cur.execute(q, tuple(params))
            rows = cur.fetchall()
        for r in rows:
            for k in ("validation_report", "graph"):
                if isinstance(r.get(k), (str, bytes)):
                    try:
                        r[k] = json.loads(r[k])
                    except Exception:
                        pass
            for k in ("started_at", "finished_at", "updated_at"):
                if r.get(k) and hasattr(r[k], "strftime"):
                    r[k] = r[k].strftime("%Y-%m-%d %H:%M:%S")
        return rows
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Load stored metadata
# ─────────────────────────────────────────────────────────────────────────────
def _load_hana_metadata(connection_id, session_id=None, user_id=None):
    from database.hana_metadata_service import get_stored_hana_metadata
    rows = get_stored_hana_metadata(connection_id=connection_id, session_id=session_id)
    if not rows and session_id:
        # Metadata is keyed by connection; tolerate a session_id mismatch.
        rows = get_stored_hana_metadata(connection_id=connection_id)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 2. Metadata -> schema dict -> LLM payload (allow-listed fields only)
# ─────────────────────────────────────────────────────────────────────────────
def _metadata_to_schema(metadata_rows):
    schema = {}
    for row in metadata_rows:
        view_name = row.get("view_name")
        if not view_name:
            continue
        pks = row.get("primary_keys") or []
        pk_upper = {str(p).upper() for p in pks}
        summary = row.get("technical_summary") or {}
        columns = []
        for c in row.get("columns_detail") or []:
            name = c.get("name") or c.get("COLUMN_NAME") or ""
            if not name:
                continue
            columns.append({
                "name": name,
                "type": c.get("data_type") or c.get("DATA_TYPE_NAME") or "NVARCHAR",
                "length": c.get("length"),
                "scale": c.get("scale"),
                "nullable": c.get("is_nullable"),
                "comment": c.get("comment") or "",
                "is_primary_key": name.upper() in pk_upper,
            })
        schema[view_name] = {
            "schema_name": row.get("schema_name", ""),
            "view_type": row.get("view_type", "VIEW"),
            "row_count": row.get("row_count"),
            "columns": columns,
            "primary_keys": pks,
            "foreign_keys": row.get("foreign_keys") or [],
            "relationships": row.get("relationships") or [],
            "technical_summary": summary,
            "role": summary.get("role") or "transaction",
        }
    return schema


def _schema_for_prompt(schema):
    out = {}
    for t, meta in schema.items():
        ts = meta.get("technical_summary") or {}
        view = {
            "schema": meta.get("schema_name", ""),
            "object_type": meta.get("view_type", "VIEW"),
            "role_hint": "dimension" if meta.get("role") == "master" else "fact",
            "row_count": meta.get("row_count"),
            "comment": ts.get("view_comment", ""),
            "primary_keys": meta.get("primary_keys", []),
            "foreign_keys": [{k: fk.get(k) for k in ("column_name", "referenced_table",
                                                      "referenced_column", "source")}
                             for fk in meta.get("foreign_keys", [])],
            "relationships": meta.get("relationships", []),
            "technical_summary": {k: ts.get(k) for k in ("key_like_columns", "measure_columns",
                                                         "date_columns", "attribute_columns",
                                                         "type_distribution")},
            "columns": [{k: c.get(k) for k in _ALLOWED_COLUMN_KEYS if c.get(k) not in (None, "")}
                        for c in meta["columns"]],
        }
        out[t] = view
    _assert_metadata_only(out)
    return out


def _assert_metadata_only(payload):
    """Hard guard: refuse to send anything that is not an allow-listed metadata field."""
    for view, meta in payload.items():
        extra = set(meta) - _ALLOWED_VIEW_KEYS
        if extra:
            raise ValueError(f"Non-metadata keys in LLM payload for {view}: {extra}")
        for c in meta.get("columns", []):
            extra_c = set(c) - _ALLOWED_COLUMN_KEYS
            if extra_c:
                raise ValueError(f"Non-metadata column keys in LLM payload for {view}: {extra_c}")


def _load_prompt():
    if os.path.exists(_PROMPT_FILE):
        try:
            with open(_PROMPT_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"[HANA-KGRAPH] Failed to read prompt file {_PROMPT_FILE}: {e}")
    # Embedded fallback prompt if prompt file is missing
    return """You are a Schema Intelligence engine for SAP S/4HANA. You are given ONLY the
METADATA of a set of SAP HANA views: view names, view comments, column names,
data types, lengths, column comments, primary keys, foreign keys / relationships,
row counts, and a technical summary per view.

HARD RULES:
- Use ONLY view names and column names that appear verbatim in the METADATA.
- Build the knowledge graph strictly from the metadata. No raw data rows are available.
- Return ONLY valid JSON with keys: fact_tables, nodes, edges, hierarchies, synonyms, metrics.

METADATA:
{SCHEMA_JSON}
"""


def _parse_json(text):
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*", "", s).strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        return json.loads(s)
    except Exception:
        try:
            from json_repair import repair_json  # type: ignore
            return json.loads(repair_json(s))
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Validation against metadata
# ─────────────────────────────────────────────────────────────────────────────
def _family(dtype):
    t = str(dtype or "").upper()
    if t in {"DECIMAL", "SMALLDECIMAL", "DOUBLE", "REAL", "FLOAT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"}:
        return "numeric"
    if t in {"DATE", "TIMESTAMP", "SECONDDATE", "TIME", "LONGDATE"}:
        return "date"
    return "string"


class _Resolver:
    """Case-insensitive name resolution back to the exact metadata names."""

    def __init__(self, schema):
        self.schema = schema
        self.tables = {t.upper(): t for t in schema}
        self.cols = {t: {c["name"].upper(): c for c in m["columns"]} for t, m in schema.items()}

    def table(self, name):
        return self.tables.get(str(name or "").strip().strip('"').upper())

    def column(self, table, name):
        if not table:
            return None
        c = self.cols.get(table, {}).get(str(name or "").strip().strip('"').upper())
        return c["name"] if c else None

    def col_type(self, table, col):
        c = self.cols.get(table, {}).get(str(col).upper())
        return c["type"] if c else None

    def single_pk(self, table):
        pks = self.schema[table].get("primary_keys") or []
        return pks[0] if len(pks) == 1 else None


def _edge(r, ft, fc, tt, tc, source, note=""):
    to_pk = r.single_pk(tt)
    types_match = _family(r.col_type(ft, fc)) == _family(r.col_type(tt, tc))
    is_pk = bool(to_pk and to_pk.upper() == tc.upper())
    return {
        "from_table": ft, "from_column": fc, "to_table": tt, "to_column": tc,
        "relationship": "N:1" if is_pk else "unknown",
        "source": source,
        "verified": 1 if source == "sys_constraints" or (is_pk and types_match) else 0,
        "verification": "metadata",  # never data-based for HANA
        "note": note,
    }, types_match


def _validate_graph(graph, schema):
    r = _Resolver(schema)
    report = {"dropped_nodes": 0, "dropped_edges": 0, "dropped_hierarchies": 0,
              "dropped_synonyms": 0, "dropped_metrics": 0}

    # Nodes ---------------------------------------------------------------
    llm_nodes = {}
    for n in graph.get("nodes", []) or []:
        t = r.table(n.get("table") or n.get("id") or n.get("table_name") or n.get("name"))
        if not t:
            report["dropped_nodes"] += 1
            continue
        pk = r.column(t, n.get("primary_key"))
        llm_nodes[t] = {"type": n.get("type"), "primary_key": pk,
                        "business_name": n.get("business_name") or "", "note": n.get("note") or ""}

    nodes = []
    for t, meta in schema.items():
        ln = llm_nodes.get(t, {})
        ntype = ln.get("type") if ln.get("type") in ("fact", "dimension") else \
            ("dimension" if meta["role"] == "master" else "fact")
        pks = meta.get("primary_keys") or ([ln["primary_key"]] if ln.get("primary_key") else [])
        nodes.append({"table": t, "type": ntype, "primary_key": pks[0] if len(pks) == 1 else (pks or None),
                      "business_name": ln.get("business_name", ""), "note": ln.get("note", "")})

    # Edges: declared/inferred FKs first, then LLM proposals ----------------
    edges, seen = [], set()

    def add(e):
        k = (e["from_table"].upper(), e["from_column"].upper(), e["to_table"].upper(), e["to_column"].upper())
        if k in seen or e["from_table"] == e["to_table"] and e["from_column"] == e["to_column"]:
            return False
        seen.add(k)
        edges.append(e)
        return True

    for t, meta in schema.items():
        for rel in meta.get("relationships", []):
            ft = r.table(rel.get("from_table") or t)
            tt = r.table(rel.get("to_table"))
            fc = r.column(ft, rel.get("from_column"))
            tc = r.column(tt, rel.get("to_column"))
            if all([ft, fc, tt, tc]):
                e, _ = _edge(r, ft, fc, tt, tc, rel.get("source") or "sys_constraints",
                             "derived from SAP HANA metadata")
                add(e)

    for raw in graph.get("edges", []) or []:
        ft, tt = r.table(raw.get("from_table")), r.table(raw.get("to_table"))
        fc, tc = r.column(ft, raw.get("from_column")), r.column(tt, raw.get("to_column"))
        if not all([ft, fc, tt, tc]):
            report["dropped_edges"] += 1
            continue
        e, types_match = _edge(r, ft, fc, tt, tc, "llm_proposed", raw.get("note") or "")
        if not types_match:
            report["dropped_edges"] += 1
            continue
        add(e)

    # Hierarchies -----------------------------------------------------------
    hierarchies = []
    for h in graph.get("hierarchies", []) or []:
        t = r.table(h.get("dimension_table"))
        levels = list(dict.fromkeys(c for c in (r.column(t, l) for l in (h.get("levels") or [])) if c))
        if not t or len(levels) < 2:
            report["dropped_hierarchies"] += 1
            continue
        hierarchies.append({"name": h.get("name") or f"{t}_hierarchy", "dimension_table": t,
                            "levels": levels, "verified": "metadata"})

    # Synonyms --------------------------------------------------------------
    synonyms, syn_seen = [], set()
    for s in graph.get("synonyms", []) or []:
        t = r.table(s.get("table"))
        c = r.column(t, s.get("column"))
        term = str(s.get("term") or "").strip()
        if not (t and c and term):
            report["dropped_synonyms"] += 1
            continue
        key = (term.lower(), t, c)
        if key not in syn_seen:
            syn_seen.add(key)
            synonyms.append({"term": term, "table": t, "column": c})

    # Metrics: every quoted identifier must be a real column of the fact view
    metrics = []
    for m in graph.get("metrics", []) or []:
        t = r.table(m.get("fact_table"))
        expr = str(m.get("expression") or "")
        idents = re.findall(r'"([^"]+)"|`([^`]+)`', expr)
        names = [a or b for a, b in idents]
        if not t or not expr or not names or any(r.column(t, n) is None for n in names):
            report["dropped_metrics"] += 1
            continue
        for n in names:  # normalise to exact case + HANA quoting
            exact = r.column(t, n)
            expr = expr.replace(f'"{n}"', f'"{exact}"').replace(f"`{n}`", f'"{exact}"')
        metrics.append({"term": m.get("term") or "metric", "expression": expr, "fact_table": t,
                        "dialect": "hana"})

    fact_tables = [ft for ft in (r.table(x) for x in (graph.get("fact_tables") or [])) if ft]
    if not fact_tables:
        fact_tables = [n["table"] for n in nodes if n["type"] == "fact"]

    return {
        "fact_tables": list(dict.fromkeys(fact_tables)),
        "nodes": nodes,
        "edges": edges,
        "hierarchies": hierarchies,
        "synonyms": synonyms,
        "metrics": metrics,
    }, report


# ─────────────────────────────────────────────────────────────────────────────
# 4. ArangoDB sync
# ─────────────────────────────────────────────────────────────────────────────
def _safe_key(val):
    key = re.sub(r"[^a-zA-Z0-9_:.@()-]", "_", str(val))
    if len(key) > 240:
        key = key[:200] + "_" + hashlib.md5(key.encode()).hexdigest()[:12]
    return key


def _resolve_arango_db(session_id):
    from database.config import ARANGO_DB
    conn = _mysql_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_arango_db FROM workspaces WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
            if row and row.get("workspace_arango_db"):
                return row["workspace_arango_db"]
    except Exception as e:
        print(f"[HANA-KGRAPH] could not resolve workspace arango db for {session_id}: {e}")
    finally:
        conn.close()
    return ARANGO_DB


def _sync_hana_kgraph_to_arango(session_id, connection_id, graph, schema):
    from arango import ArangoClient  # type: ignore
    from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS

    target_db = _resolve_arango_db(session_id)
    client = ArangoClient(hosts=ARANGO_HOST)
    sys_db = client.db("_system", username=ARANGO_USER, password=ARANGO_PASS)
    if not sys_db.has_database(target_db):
        sys_db.create_database(target_db)
    db = client.db(target_db, username=ARANGO_USER, password=ARANGO_PASS)

    nodes_col_name, edges_col_name = "session_nodes", "session_edges"
    if not db.has_collection(nodes_col_name):
        db.create_collection(nodes_col_name)
    if not db.has_collection(edges_col_name):
        db.create_collection(edges_col_name, edge=True)

    # Mandatory backup (same policy as kgraph_builder): abort on failure.
    from database.kgraph_backup_service import backup_kgraph_collections
    backup_kgraph_collections(db, nodes_col_name, edges_col_name)

    nodes_col = db.collection(nodes_col_name)
    edges_col = db.collection(edges_col_name)

    syn_by_t, hier_by_t, met_by_t = {}, {}, {}
    for s in graph["synonyms"]:
        syn_by_t.setdefault(s["table"], []).append({"term": s["term"], "column": s["column"]})
    for h in graph["hierarchies"]:
        hier_by_t.setdefault(h["dimension_table"], []).append({"name": h["name"], "levels": h["levels"]})
    for m in graph["metrics"]:
        met_by_t.setdefault(m["fact_table"], []).append({"term": m["term"], "expression": m["expression"]})

    node_keys, edge_keys = [], []
    for n in graph["nodes"]:
        t = n["table"]
        meta = schema.get(t, {})
        key = _safe_key(t)
        node_keys.append(key)
        doc = {
            "_key": key,
            "session_id": session_id,
            "connection_id": int(connection_id),
            "source": NODE_SOURCE,
            "label": n.get("business_name") or t,
            "original_id": t,
            "type": n["type"],
            "schema_name": meta.get("schema_name", ""),
            "object_type": meta.get("view_type", "VIEW"),
            "row_count": meta.get("row_count"),
            "primary_keys": meta.get("primary_keys", []),
            "columns": [{"name": c["name"], "type": c["type"], "pk": c["is_primary_key"],
                         "comment": c.get("comment", "")} for c in meta.get("columns", [])],
            "note": n.get("note", ""),
            "synonyms": syn_by_t.get(t, []),
            "hierarchies": hier_by_t.get(t, []),
            "metrics": met_by_t.get(t, []),
        }
        try:
            if nodes_col.has(key):
                nodes_col.replace(doc)
            else:
                nodes_col.insert(doc)
        except Exception as e:
            print(f"[HANA-KGRAPH] skip node {key}: {e}")

    for e in graph["edges"]:
        fk, tk = _safe_key(e["from_table"]), _safe_key(e["to_table"])
        key = _safe_key(f"{fk}__{e['from_column']}__to__{tk}__{e['to_column']}")
        edge_keys.append(key)
        doc = {
            "_key": key,
            "_from": f"{nodes_col_name}/{fk}",
            "_to": f"{nodes_col_name}/{tk}",
            "session_id": session_id,
            "connection_id": int(connection_id),
            "source": NODE_SOURCE,
            "label": e.get("relationship") or "RELATES_TO",
            "title": f"Join {e['from_column']} -> {e['to_column']}",
            "from_column": e["from_column"],
            "to_column": e["to_column"],
            "edge_source": e["source"],
            "verified": e["verified"],
        }
        try:
            if edges_col.has(key):
                edges_col.replace(doc)
            else:
                edges_col.insert(doc)
        except Exception as ex:
            print(f"[HANA-KGRAPH] skip edge {key}: {ex}")

    # Remove HANA nodes/edges of this connection that are no longer in the graph.
    bind = {"s": session_id, "c": int(connection_id), "src": NODE_SOURCE}
    try:
        db.aql.execute(f"""FOR d IN {edges_col_name}
            FILTER d.source == @src AND d.session_id == @s AND d.connection_id == @c
               AND d._key NOT IN @keys REMOVE d IN {edges_col_name}""", bind_vars={**bind, "keys": edge_keys})
        db.aql.execute(f"""FOR d IN {nodes_col_name}
            FILTER d.source == @src AND d.session_id == @s AND d.connection_id == @c
               AND d._key NOT IN @keys REMOVE d IN {nodes_col_name}""", bind_vars={**bind, "keys": node_keys})
    except Exception as e:
        print(f"[HANA-KGRAPH] stale cleanup skipped: {e}")

    print(f"[HANA-KGRAPH] ArangoDB '{target_db}': {len(node_keys)} nodes, {len(edge_keys)} edges upserted.")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Public entry points
# ─────────────────────────────────────────────────────────────────────────────
def _get_lock(connection_id, session_id):
    k = f"{connection_id}:{session_id}"
    with _build_locks_guard:
        return _build_locks.setdefault(k, threading.Lock())


def build_hana_kgraph_from_metadata(connection_id, session_id, user_id=None):
    """Build the KG for one SAP HANA connection from stored metadata only."""
    lock = _get_lock(connection_id, session_id)
    if not lock.acquire(blocking=False):
        print(f"[HANA-KGRAPH] build already running for {connection_id}/{session_id}; skipped.")
        return {"status": "skipped"}
    t0 = time.time()
    try:
        set_hana_kgraph_status(connection_id, session_id, "building", user_id=user_id)

        rows = _load_hana_metadata(connection_id, session_id, user_id)
        if not rows:
            msg = "No stored SAP HANA metadata found. Run metadata discovery first."
            set_hana_kgraph_status(connection_id, session_id, "error", user_id=user_id, error=msg)
            return {"status": "empty", "tables": 0, "error": msg}

        schema = _metadata_to_schema(rows)
        payload = _schema_for_prompt(schema)
        print(f"[HANA-KGRAPH] metadata loaded for {len(schema)} views (no raw data).")

        llm_status, llm_graph = "ok", {}
        try:
            from model.llm_client import call_llm_chat  # type: ignore
            prompt = _load_prompt().replace("{SCHEMA_JSON}", json.dumps(payload, indent=2, default=str))
            raw = call_llm_chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.0)
            llm_graph = _parse_json(raw) or {}
            if not llm_graph or llm_graph.get("error"):
                llm_status = "failed"
                print(f"[HANA-KGRAPH] LLM returned no usable graph: {str(raw)[:300]}")
        except Exception as e:
            llm_status = "failed"
            print(f"[HANA-KGRAPH] LLM call failed, using metadata-only graph: {e}")

        graph, report = _validate_graph(llm_graph if llm_status == "ok" else {}, schema)
        counts = {"nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
                  "hierarchies": len(graph["hierarchies"]), "synonyms": len(graph["synonyms"]),
                  "metrics": len(graph["metrics"])}
        print(f"[HANA-KGRAPH] validated graph {counts}; dropped {report}")

        # ── Compare with Golden Sales Ontology Graph ────────
        ontology_analysis = None
        try:
            from database.kgraph_ontology_comparator import compare_kgraph_with_ontology
            ontology_analysis = compare_kgraph_with_ontology(graph, schema)
            graph["ontology_analysis"] = ontology_analysis
            print(f"[HANA-KGRAPH] Golden Ontology comparison complete: completeness={ontology_analysis.get('completeness_score')}%")
        except Exception as on_e:
            print(f"[HANA-KGRAPH] Ontology comparison failed: {on_e}")

        _sync_hana_kgraph_to_arango(session_id, connection_id, graph, schema)

        elapsed = round(time.time() - t0, 1)
        set_hana_kgraph_status(connection_id, session_id, "ready", user_id=user_id, counts=counts,
                               graph=graph, llm_status=llm_status, report=report)
        print(f"[HANA-KGRAPH] ready in {elapsed}s for connection {connection_id}.")
        return {"status": "ok", "tables": len(schema), **counts, "llm_status": llm_status,
                "validation_report": report, "ontology_analysis": ontology_analysis,
                "elapsed_s": elapsed, "graph": graph}

    except Exception as e:
        print(f"[HANA-KGRAPH] build failed: {e}")
        set_hana_kgraph_status(connection_id, session_id, "error", user_id=user_id, error=str(e))
        return {"status": "error", "error": str(e)}
    finally:
        lock.release()


def start_hana_kgraph_build_async(connection_id, session_id, user_id=None):
    """Mark the build as queued (so the UI can poll immediately) and run it in a thread."""
    set_hana_kgraph_status(connection_id, session_id, "queued", user_id=user_id)
    t = threading.Thread(target=build_hana_kgraph_from_metadata,
                         args=(connection_id, session_id, user_id), daemon=True)
    t.start()
    return t
