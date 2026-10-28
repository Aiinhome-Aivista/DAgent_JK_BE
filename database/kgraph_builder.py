"""
kgraph_builder.py — builds the schema Knowledge Graph for a database.

Runs ONCE at data-sync time (right after csv_processor.process_csv_job /
sql_processor.process_sql_job finish). It:

  1. Extracts the real schema from the user's allocated MySQL DB
     (SHOW TABLES, DESCRIBE, row counts, per-column distinct counts, and sample
     distinct values for low-cardinality columns).
  2. Asks the LLM (kgraph_build_prompt.txt) to PROPOSE a knowledge graph:
     fact/dimension nodes, join edges, hierarchies, synonyms, metric formulas.
  3. VERIFIES every proposed edge and hierarchy with cheap SQL — this turns LLM
     guesses into facts and prevents fan-out joins and false hierarchies.
  4. Returns the verified graph as a dict (no MySQL persistence).
     Callers should pass the result to the ArangoDB backup/update pipeline.

Wire-in (database/external_sync_service.py, right after the sync job succeeds):

    from database.kgraph_builder import build_kgraph
    result = build_kgraph(allocated_db_name=new_user_db,
                          db_host=db_host, db_user=db_user,
                          db_pass=db_pass, db_port=db_port)
"""

import os
import re
import json
import time
import hashlib

import pymysql

try:
    # pyrefly: ignore [missing-import]
    from json_repair import repair_json
except Exception:                       # pragma: no cover
    repair_json = None

from model.llm_client import call_llm_chat

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────
LOW_CARD_MAX      = 60        # columns with <= this many distinct values are "dimensional"
SAMPLE_VALUES_MAX = 50        # distinct values stored per low-cardinality column
VERIFY_ROW_CAP    = 500_000   # skip COUNT(DISTINCT) verification above this many rows

from database.db_connection import get_db_connection


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _bt(name: str) -> str:
    """Backtick-quote an identifier safely."""
    return "`" + str(name).replace("`", "``") + "`"


def _connect(db, host, user, pwd, port):
    return pymysql.connect(host=host, port=int(port), user=user, password=pwd,
                           database=db, cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=15)





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
        if repair_json:
            try:
                return json.loads(repair_json(s))
            except Exception:
                return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. Schema extraction
# ─────────────────────────────────────────────────────────────────────────────
def _extract_schema(cur):
    """Return {table: {row_count, columns:[{name,type,cardinality,values?}]}}."""
    cur.execute("SHOW TABLES")
    rows = cur.fetchall()
    tables = [list(r.values())[0] for r in rows]
    # Never describe our own metadata tables.
    tables = [t for t in tables if not str(t).lower().startswith("kgraph_")]

    schema = {}
    for t in tables:
        if not re.match(r"^[\w]+$", t):
            continue
        try:
            cur.execute(f"DESCRIBE {_bt(t)}")
            desc = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) AS n FROM {_bt(t)}")
            row_count = int(cur.fetchone()["n"])

            cols = []
            for d in desc:
                col = d.get("Field")
                dtype = d.get("Type")
                card = None
                values = None
                if row_count <= VERIFY_ROW_CAP:
                    try:
                        cur.execute(
                            f"SELECT COUNT(DISTINCT {_bt(col)}) AS c FROM {_bt(t)}")
                        card = int(cur.fetchone()["c"])
                    except Exception:
                        card = None
                # Sample distinct values for low-cardinality non-numeric columns.
                is_text = bool(re.search(r"char|text|enum|set", str(dtype), re.I))
                if is_text and card is not None and 0 < card <= LOW_CARD_MAX:
                    try:
                        cur.execute(
                            f"SELECT DISTINCT {_bt(col)} AS v FROM {_bt(t)} "
                            f"WHERE {_bt(col)} IS NOT NULL LIMIT {SAMPLE_VALUES_MAX}")
                        values = [str(r["v"]) for r in cur.fetchall()
                                  if r["v"] is not None and str(r["v"]).strip()]
                    except Exception:
                        values = None
                cols.append({"name": col, "type": str(dtype),
                             "cardinality": card, "values": values})
            schema[t] = {"row_count": row_count, "columns": cols}
        except Exception as e:
            print(f"[KGRAPH] describe {t} failed: {e}")
    return schema


def _schema_hash(schema):
    sig = []
    for t in sorted(schema):
        cols = ",".join(f"{c['name']}:{c['type']}" for c in schema[t]["columns"])
        sig.append(f"{t}|{cols}")
    return hashlib.md5("\n".join(sig).encode()).hexdigest()


def _schema_for_prompt(schema):
    """Compact schema the LLM can reason over (drops big value lists)."""
    out = {}
    for t, meta in schema.items():
        out[t] = {
            "row_count": meta["row_count"],
            "columns": [
                {"name": c["name"], "type": c["type"], "distinct": c["cardinality"],
                 **({"sample_values": c["values"][:15]} if c["values"] else {})}
                for c in meta["columns"]
            ],
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 2. Verification (SQL-confirm the LLM's proposals)
# ─────────────────────────────────────────────────────────────────────────────
def _verify_edge(cur, schema, e):
    """Classify an edge as 1:1 / N:1 / 1:N / N:N and compute a referential match
    ratio. A safe dimension join is N:1 (dimension key unique). Returns a dict or
    None if the columns don't exist."""
    ft, fc = e.get("from_table"), e.get("from_column")
    tt, tc = e.get("to_table"), e.get("to_column")
    if not all([ft, fc, tt, tc]):
        return None
    if ft not in schema or tt not in schema:
        return None
    fcols = {c["name"] for c in schema[ft]["columns"]}
    tcols = {c["name"] for c in schema[tt]["columns"]}
    if fc not in fcols or tc not in tcols:
        return None
    rel, match, verified = "unknown", None, 0
    try:
        cur.execute(f"SELECT COUNT(*) total, COUNT(DISTINCT {_bt(tc)}) dis "
                    f"FROM {_bt(tt)}")
        r = cur.fetchone()
        to_unique = r["total"] == r["dis"] and r["total"] > 0

        cur.execute(f"SELECT COUNT(*) total, COUNT(DISTINCT {_bt(fc)}) dis "
                    f"FROM {_bt(ft)}")
        r2 = cur.fetchone()
        from_unique = r2["total"] == r2["dis"] and r2["total"] > 0

        if to_unique and from_unique:
            rel = "1:1"
        elif to_unique:
            rel = "N:1"          # many fact rows -> one dim row  (SAFE for SUM)
        elif from_unique:
            rel = "1:N"
        else:
            rel = "N:N"          # fan-out risk

        # Referential match: fraction of fact keys present in the dimension.
        cur.execute(
            f"SELECT COUNT(*) miss FROM "
            f"(SELECT DISTINCT {_bt(fc)} v FROM {_bt(ft)} "
            f" WHERE {_bt(fc)} IS NOT NULL) f "
            f"LEFT JOIN {_bt(tt)} d ON f.v = d.{_bt(tc)} "
            f"WHERE d.{_bt(tc)} IS NULL")
        miss = cur.fetchone()["miss"]
        cur.execute(f"SELECT COUNT(DISTINCT {_bt(fc)}) c FROM {_bt(ft)} "
                    f"WHERE {_bt(fc)} IS NOT NULL")
        distinct_keys = cur.fetchone()["c"] or 1
        match = round(1.0 - (miss / distinct_keys), 4)
        verified = 1 if rel in ("N:1", "1:1") and match >= 0.5 else 0
    except Exception as ex:
        print(f"[KGRAPH] edge verify failed {ft}.{fc}->{tt}.{tc}: {ex}")
    return {"from_table": ft, "from_column": fc, "to_table": tt, "to_column": tc,
            "relationship": rel, "match_ratio": match, "verified": verified,
            "note": e.get("note", "")}


def _verify_hierarchy_level(cur, table, parent, child):
    """True if each child value maps to exactly one parent value (real nesting)."""
    try:
        cur.execute(
            f"SELECT COUNT(*) bad FROM ("
            f"  SELECT {_bt(child)} c FROM {_bt(table)} "
            f"  WHERE {_bt(child)} IS NOT NULL AND {_bt(parent)} IS NOT NULL "
            f"  GROUP BY {_bt(child)} HAVING COUNT(DISTINCT {_bt(parent)}) > 1"
            f") x")
        return cur.fetchone()["bad"] == 0
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dimension value extraction (in-memory only, no MySQL persistence)
# ─────────────────────────────────────────────────────────────────────────────
def _extract_dim_values(schema):
    """
    Return authoritative dimension values from the already-extracted schema
    (using the sampled `values` field). No extra DB round-trip needed.

    Returns: {table_name: {column_name: [value, ...]}}
    """
    dim_values = {}
    for t, meta in schema.items():
        for c in meta["columns"]:
            if c["values"]:
                dim_values.setdefault(t, {})[c["name"]] = c["values"][:SAMPLE_VALUES_MAX]
    return dim_values




# ─────────────────────────────────────────────────────────────────────────────
# Per-database build lock — prevents concurrent kgraph builds for the same db
# (e.g. 4 CSV jobs + 1 MySQL sync all triggering build_kgraph at once).
# ─────────────────────────────────────────────────────────────────────────────
import threading as _threading
_kgraph_locks: dict = {}          # allocated_db_name -> threading.Lock()
_kgraph_locks_meta: dict = {}     # allocated_db_name -> dict
_kgraph_locks_guard = _threading.Lock()

def _get_db_lock(db_name: str) -> _threading.Lock:
    with _kgraph_locks_guard:
        if db_name not in _kgraph_locks:
            _kgraph_locks[db_name] = _threading.Lock()
            _kgraph_locks_meta[db_name] = {"building": False}
        return _kgraph_locks[db_name]

# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def build_kgraph(allocated_db_name, db_host, db_user, db_pass, db_port,
                 force=False, trigger_source="unknown"):
    """
    Build and verify the Knowledge Graph for `allocated_db_name`.

    Returns a dict:
        {
          "status":         "ok" | "empty" | "error",
          "tables":         int,           # schema tables found
          "schema_hash":    str,           # MD5 of current schema structure
          "edges":          int,           # total proposed edges
          "edges_verified": int,           # edges passing SQL verification
          "hierarchies":    int,
          "synonyms":       int,
          "metrics":        int,
          "graph":          dict,          # full verified graph payload
          "dim_values":     dict,          # {table: {col: [values]}}
          "trigger_source": str,
          "elapsed_s":      float,
          "error":          str,           # only present on "error" status
        }

    No MySQL kgraph_* tables are created or written.
    Callers should forward "graph" + "dim_values" to the ArangoDB
    backup/update pipeline as required.
    """
    # ── Concurrency guard: skip if a build is already running for this db ──
    db_lock = _get_db_lock(allocated_db_name)
    if not db_lock.acquire(blocking=False):
        print(f"[KGRAPH] build already in progress for {allocated_db_name}, "
              f"skipping duplicate trigger ({trigger_source})")
        return {"status": "skipped", "tables": 0}
    try:
        _kgraph_locks_meta[allocated_db_name]["building"] = True
        # ── main build (lock is held until finally block) ────────────────────
        return _build_kgraph_inner(allocated_db_name, db_host, db_user, db_pass,
                                   db_port, force=force, trigger_source=trigger_source)
    finally:
        _kgraph_locks_meta[allocated_db_name]["building"] = False
        db_lock.release()


def _build_kgraph_inner(allocated_db_name, db_host, db_user, db_pass, db_port,
                        force=False, trigger_source="unknown"):
    """Actual build logic — always called with the per-db lock already held."""
    t0 = time.time()
    conn = None
    try:
        conn = _connect(allocated_db_name, db_host, db_user, db_pass, db_port)
        cur = conn.cursor()

        schema = _extract_schema(cur)
        if not schema:
            print("[KGRAPH] no tables found — nothing to build")
            return {"status": "empty", "tables": 0}

        # Schema hash — for logging / change-detection by callers.
        shash = _schema_hash(schema)

        # ── LLM proposes the graph ──────────────────────────────────────────
        from database.prompt_loader import get_prompt

        prompt_template = get_prompt(0, 'knowledge_graph')
        if not prompt_template or not prompt_template.strip():
            prompt_template = ("Return a STRICT JSON knowledge graph with keys fact_tables, nodes, "
                               "edges, hierarchies, synonyms, metrics, using ONLY the columns in:\n"
                               "{SCHEMA_JSON}")
        prompt = prompt_template.replace(
            "{SCHEMA_JSON}", json.dumps(_schema_for_prompt(schema), indent=2, default=str))
        raw = call_llm_chat([{"role": "user", "content": prompt}],
                            json_mode=True, temperature=0.0)
        graph = _parse_json(raw) or {}
        print(f"[KGRAPH] LLM proposed: "
              f"{len(graph.get('nodes', []))} nodes, "
              f"{len(graph.get('edges', []))} edges, "
              f"{len(graph.get('hierarchies', []))} hierarchies, "
              f"{len(graph.get('synonyms', []))} synonyms, "
              f"{len(graph.get('metrics', []))} metrics")

        # ── Verify edges ────────────────────────────────────────────────────
        edges = []
        for e in graph.get("edges", []):
            v = _verify_edge(cur, schema, e)
            if v:
                edges.append(v)

        # ── Verify hierarchies (and order by cardinality) ───────────────────
        hierarchies = []
        for h in graph.get("hierarchies", []):
            tbl = h.get("dimension_table")
            levels = [c for c in (h.get("levels") or []) if tbl in schema and
                      c in {col["name"] for col in schema[tbl]["columns"]}]
            if tbl not in schema or len(levels) < 2:
                continue
            card = {col["name"]: (col["cardinality"] or 0)
                    for col in schema[tbl]["columns"]}
            levels = sorted(dict.fromkeys(levels), key=lambda c: card.get(c, 0))
            verified_flags = []
            for i in range(len(levels) - 1):
                verified_flags.append(
                    _verify_hierarchy_level(cur, tbl, levels[i], levels[i + 1]))
            hierarchies.append({"name": h.get("name") or f"{tbl}_hierarchy",
                                "dimension_table": tbl, "levels": levels,
                                "cardinality": card,
                                "verified": verified_flags})

        # ── Authoritative dimension values (in-memory, no MySQL writes) ─────
        dim_values = _extract_dim_values(schema)

        verified_graph = {
            "fact_tables": graph.get("fact_tables", []),
            "nodes":       graph.get("nodes", []),
            "edges":       edges,
            "hierarchies": hierarchies,
            "synonyms":    graph.get("synonyms", []),
            "metrics":     graph.get("metrics", []),
        }

        # ── Compare with Golden Sales Ontology Graph ────────
        ontology_analysis = None
        try:
            from database.kgraph_ontology_comparator import compare_kgraph_with_ontology
            ontology_analysis = compare_kgraph_with_ontology(verified_graph, schema)
            verified_graph["ontology_analysis"] = ontology_analysis
            print(f"[KGRAPH] Golden Ontology comparison complete: completeness={ontology_analysis.get('completeness_score')}%")
        except Exception as on_e:
            print(f"[KGRAPH] Ontology comparison failed: {on_e}")

        # ── Sync to ArangoDB (Auto-backup and upsert) ────────
        try:
            _sync_kgraph_to_arango(allocated_db_name, verified_graph)
        except Exception as sync_e:
            print(f"[KGRAPH] ArangoDB sync failed: {sync_e}")

        edges_verified = sum(e["verified"] for e in edges)
        elapsed = round(time.time() - t0, 1)

        print(f"[KGRAPH] built for {allocated_db_name}: "
              f"{len(schema)} tables, {len(edges)} edges "
              f"({edges_verified} verified), "
              f"{len(hierarchies)} hierarchies in {elapsed}s")

        return {
            "status":            "ok",
            "tables":            len(schema),
            "schema_hash":       shash,
            "edges":             len(edges),
            "edges_verified":    edges_verified,
            "hierarchies":       len(hierarchies),
            "synonyms":          len(graph.get("synonyms", [])),
            "metrics":           len(graph.get("metrics", [])),
            "graph":             verified_graph,
            "ontology_analysis": ontology_analysis,
            "dim_values":        dim_values,
            "trigger_source":    trigger_source,
            "elapsed_s":         elapsed,
        }

    except Exception as e:
        print(f"[KGRAPH] build failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _sync_kgraph_to_arango(allocated_db_name, verified_graph):
    from database.db_connection import get_db_connection
    conn = get_db_connection()
    session_id = None
    target_arango_db = None
    try:
        with conn.cursor(dictionary=True) as cur:
            cur.execute("SELECT session_id, workspace_arango_db FROM workspaces WHERE workspace_db = %s", (allocated_db_name,))
            row = cur.fetchone()
            if row:
                session_id = row["session_id"]
                target_arango_db = row["workspace_arango_db"]
    except Exception as e:
        print(f"[KGRAPH] failed to get session_id for Arango sync: {e}")
    finally:
        if conn: conn.close()
        
    if not session_id:
        print("[KGRAPH] No session_id found, skipping Arango sync.")
        return

    from arango import ArangoClient
    from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB

    client = ArangoClient(hosts=ARANGO_HOST)
    sys_db = client.db('_system', username=ARANGO_USER, password=ARANGO_PASS)

    db_to_use = target_arango_db if target_arango_db else ARANGO_DB
    if not sys_db.has_database(db_to_use):
        sys_db.create_database(db_to_use)
    db = client.db(db_to_use, username=ARANGO_USER, password=ARANGO_PASS)

    nodes_col_name = "session_nodes"
    edges_col_name = "session_edges"
    if not db.has_collection(nodes_col_name):
        db.create_collection(nodes_col_name)
    if not db.has_collection(edges_col_name):
        db.create_collection(edges_col_name, edge=True)

    # 1. Save ontology analysis metadata FIRST in an isolated try/except block
    # This guarantees that even if collection backup fails, the analysis result is still preserved.
    if "ontology_analysis" in verified_graph and verified_graph["ontology_analysis"]:
        try:
            meta_col_name = "session_metadata"
            if not db.has_collection(meta_col_name):
                db.create_collection(meta_col_name)
            meta_col = db.collection(meta_col_name)
            meta_doc = {
                "_key": "ontology_analysis",
                "session_id": session_id,
                "data": verified_graph["ontology_analysis"]
            }
            if meta_col.has("ontology_analysis"):
                meta_col.update(meta_doc)
            else:
                meta_col.insert(meta_doc)
        except Exception as e:
            print(f"[KGRAPH] Failed to persist ontology_analysis to Arango: {e}")

    # 2. Backup existing collections
    from database.kgraph_backup_service import backup_kgraph_collections
    try:
        backup_kgraph_collections(db, nodes_col_name, edges_col_name)
    except Exception as e:
        print(f"[KGRAPH] Backup failed: {e}")
        # Proceed with syncing nodes/edges instead of discarding everything


    nodes_col = db.collection(nodes_col_name)
    edges_col = db.collection(edges_col_name)
    
    import re
    def _safe_key(val: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_:.@()-]', '_', str(val))

    for n in verified_graph.get("nodes", []):
        node_id = str(n.get("id") or n.get("table_name") or n.get("name") or "unknown")
        if node_id == "unknown": continue
        key = _safe_key(node_id)
        doc = {
            "_key": key,
            "session_id": session_id,
            "label": n.get("label", node_id),
            "original_id": node_id,
            "type": n.get("type", "SchemaTable")
        }
        try:
            if nodes_col.has(key): nodes_col.update(doc)
            else: nodes_col.insert(doc)
        except Exception: pass

    for e in verified_graph.get("edges", []):
        from_node = e.get("from_table") or e.get("from")
        to_node = e.get("to_table") or e.get("to")
        if not from_node or not to_node: continue
        from_key = _safe_key(from_node)
        to_key = _safe_key(to_node)
        edge_key = _safe_key(f"{from_key}_to_{to_key}")
        doc = {
            "_key": edge_key,
            "_from": f"{nodes_col_name}/{from_key}",
            "_to": f"{nodes_col_name}/{to_key}",
            "session_id": session_id,
            "label": e.get("relationship", e.get("label", "")),
            "title": f"Join {e.get('from_column')} -> {e.get('to_column')}"
        }
        try:
            if edges_col.has(edge_key): edges_col.update(doc)
            else: edges_col.insert(doc)
        except Exception: pass

    print("[KGRAPH] ArangoDB sync completed.")
