"""
kgraph_service.py — query-time use of the persisted Knowledge Graph.

Loads the kgraph_* tables for a database and turns them into:
  * a short SUMMARY (for the intent classifier, PROMPT 1),
  * an AUTHORITATIVE RULES block (for the SQL generator, PROMPT 2): column
    ownership, the exact verified join keys, hierarchies, word->column synonyms,
    and metric formulas — plus an explicit fan-out guard,
  * a deterministic SQL VALIDATOR that rejects columns/joins not in the graph
    BEFORE execution (so a wrong-table or fan-out join never reaches MySQL),
  * synonym resolution and hierarchy drill-down detection.

This is the K-Graph replacement for the inline business-map heuristics in
session_rag_chat_controller.py. When a verified graph exists, prefer it; otherwise
the controller can fall back to its inline map.

Usage in the controller (AGGREGATION branch):

    from controllers.kgraph_service import load_kgraph, build_sql_rules, \
         resolve_grouping, detect_drilldown, validate_sql

    kg = load_kgraph(target_db)                 # None if not built yet -> fallback
    if kg:
        sql_user += "\n\n" + build_sql_rules(kg)
        drill = detect_drilldown(question, kg)
        if drill:  sql_user += drill["hint"]
        else:
            gh = resolve_grouping(question, kg)
            if gh: sql_user += gh
        # after generation, before execution:
        bad = validate_sql(sql_query, kg)
        if bad:  # regenerate with bad["correction"] (same loop you already have)
"""

import re
import pymysql

from database.config import MYSQL_CONFIG

_RANK_OR_BREAKDOWN_RE = re.compile(
    r'\b(top|bottom|best|worst|highest|lowest|leading|poor|performing|rank|'
    r'ranking|wise|breakdown|break\s*down|split|distribution|each|per|'
    r'types?|kinds?|categor(?:y|ies)|segments?|variants?|constructions?)\b', re.I)


def _norm(s):
    return re.sub(r'[\s_]+', '', str(s).lower())


# ─────────────────────────────────────────────────────────────────────────────
# Load
# ─────────────────────────────────────────────────────────────────────────────
def load_kgraph(db_name):
    """Return the graph as a dict, or None if no verified graph exists."""
    if not db_name or not re.match(r'^\w+$', db_name):
        return None
    conn = None
    try:
        conn = pymysql.connect(
            host=MYSQL_CONFIG["host"], port=int(MYSQL_CONFIG["port"]),
            user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"],
            database=db_name, cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT status FROM kgraph_meta ORDER BY id DESC LIMIT 1")
        meta = cur.fetchone()
        if not meta or meta.get("status") != "ok":
            return None

        cur.execute("SELECT * FROM kgraph_nodes")
        nodes = cur.fetchall()
        cur.execute("SELECT * FROM kgraph_edges")
        edges = cur.fetchall()
        cur.execute("SELECT * FROM kgraph_hierarchy ORDER BY hierarchy_name, level_index")
        hier = cur.fetchall()
        cur.execute("SELECT * FROM kgraph_synonyms")
        syn = cur.fetchall()
        cur.execute("SELECT * FROM kgraph_metrics")
        metrics = cur.fetchall()
        cur.execute("SELECT table_name, column_name, value FROM kgraph_dim_values")
        dimvals = cur.fetchall()

        # Full table columns (via DESCRIBE) for SQL validation + ownership.
        table_cols = _load_table_columns(cur, [n["table_name"] for n in nodes])

        # hierarchies grouped
        hmap = {}
        for r in hier:
            hmap.setdefault(r["hierarchy_name"], {"dimension_table": r["dimension_table"],
                                                  "levels": []})
            hmap[r["hierarchy_name"]]["levels"].append(
                (r["column_name"], r["cardinality"], r["verified"], r["dimension_table"]))

        # dim values: (table, col) -> [values]
        vmap = {}
        for r in dimvals:
            vmap.setdefault((r["table_name"], r["column_name"]), []).append(r["value"])

        return {
            "db": db_name,
            "nodes": nodes,
            "edges": edges,
            "hierarchies": hmap,
            "synonyms": syn,
            "metrics": metrics,
            "dim_values": vmap,
            "table_cols": table_cols,
        }
    except Exception as e:
        print(f"[KGRAPH] load failed for {db_name}: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _load_table_columns(cur, tables):
    """{table: [cols]} via DESCRIBE — used for SQL validation."""
    out = {}
    for t in tables:
        if not re.match(r'^\w+$', str(t)):
            continue
        try:
            cur.execute(f"DESCRIBE `{t}`")
            out[t] = [d["Field"] for d in cur.fetchall()]
        except Exception:
            pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Prompt assembly
# ─────────────────────────────────────────────────────────────────────────────
def format_summary(kg):
    """Short summary for the intent classifier (PROMPT 1)."""
    facts = [n["table_name"] for n in kg["nodes"] if n["node_type"] == "fact"]
    dims = [n["table_name"] for n in kg["nodes"] if n["node_type"] != "fact"]
    lines = [f"Fact tables: {', '.join(facts) or '(none)'}",
             f"Dimensions: {', '.join(dims) or '(none)'}"]
    for name, h in kg["hierarchies"].items():
        cols = " -> ".join(c[0] for c in h["levels"])
        lines.append(f"Hierarchy {name}: {cols}")
    if kg["metrics"]:
        lines.append("Metrics: " + ", ".join(m["term"] for m in kg["metrics"]))
    return "\n".join(lines)


def build_sql_rules(kg):
    """Authoritative rules block injected into the SQL generator (PROMPT 2)."""
    out = ["AUTHORITATIVE KNOWLEDGE GRAPH (follow EXACTLY — overrides any guess):"]

    # Column ownership
    out.append("COLUMN OWNERSHIP (a column may be referenced ONLY on a table listed for it):")
    for t, cols in kg["table_cols"].items():
        out.append(f"- `{t}`: " + ", ".join(f"`{c}`" for c in cols))

    # Join keys (verified edges only, with cardinality / fan-out note)
    if kg["edges"]:
        out.append("JOIN KEYS (use ONLY these joins; each is the safe key — never join on another column):")
        for e in kg["edges"]:
            flag = "" if e["verified"] else "  (UNVERIFIED — avoid)"
            fan = "  [SAFE N:1]" if e["relationship"] in ("N:1", "1:1") else \
                  f"  [FAN-OUT RISK {e['relationship']} — do NOT SUM across this]"
            out.append(f"- `{e['from_table']}`.`{e['from_column']}` = "
                       f"`{e['to_table']}`.`{e['to_column']}`{fan}{flag}")

    # Hierarchies
    for name, h in kg["hierarchies"].items():
        cols = " -> ".join(f"`{c[0]}`" for c in h["levels"])
        out.append(f"HIERARCHY {name} (broad->granular on `{h['levels'][0][3]}`): {cols}")

    # Synonyms (longest phrase wins)
    if kg["synonyms"]:
        out.append("WORD -> COLUMN (map user wording to the EXACT column; longer phrase wins):")
        for s in sorted(kg["synonyms"], key=lambda x: -(x.get("phrase_len") or 0)):
            t_tbl = s['target_table']
            t_col = s['target_column']
            # FORCE FACT TABLE FOREIGN KEYS to prevent NULL-lumping!
            if t_tbl == "customer_master" and t_col == "KUNNR":
                t_tbl, t_col = "sales_data", "customer"
            elif t_tbl == "sku_master" and t_col == "MATNR":
                t_tbl, t_col = "sales_data", "material"
            
            out.append(f"  \"{s['business_term']}\" -> `{t_col}` (on `{t_tbl}`)")

    # Metrics
    if kg["metrics"]:
        out.append("METRICS (use the expression verbatim):")
        for m in kg["metrics"]:
            out.append(f"- {m['term']} = {m['expression']}")

    out.append("FAN-OUT GUARD: join every dimension on the key above so each fact "
               "row matches at most one dimension row. If a column exists on more "
               "than one table, use the table named in COLUMN OWNERSHIP.")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Synonym resolution + drill-down
# ─────────────────────────────────────────────────────────────────────────────
def _syn_spans(question, kg):
    """All synonym phrase matches with spans, longest-first, non-overlapping."""
    ql = question.lower()
    cands = []
    for s in kg["synonyms"]:
        term = s["business_term"]
        for m in re.finditer(r'\b' + re.escape(term) + r'\b', ql):
            cands.append((m.start(), m.end(), s["target_table"], s["target_column"], term))
        # simple plural
        if not term.endswith("s"):
            for m in re.finditer(r'\b' + re.escape(term) + r's\b', ql):
                cands.append((m.start(), m.end(), s["target_table"], s["target_column"], term + "s"))
    cands.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    taken, out, seen = [], [], set()
    for st, en, t, c, p in cands:
        if any(not (en <= ts or st >= te) for ts, te in taken):
            continue
        taken.append((st, en))
        
        # FORCE FACT TABLE FOREIGN KEYS
        if t == "customer_master" and c == "KUNNR":
            t, c = "sales_data", "customer"
        elif t == "sku_master" and c == "MATNR":
            t, c = "sales_data", "material"
            
        if (t, c) not in seen:
            seen.add((t, c))
            out.append((t, c, p))
    return out


def resolve_grouping(question, kg):
    """For non-drill questions, map the user's wording to the GROUP BY column(s)."""
    named = _syn_spans(question, kg)
    if not named or not _RANK_OR_BREAKDOWN_RE.search(question):
        return ""
    lines = ["\n\nGROUP-BY MAPPING (use these EXACT columns for the breakdown):"]
    for (t, c, p) in named:
        lines.append(f"- '{p}' -> GROUP BY `{t}`.`{c}` (JOIN `{t}` per the JOIN KEYS).")
    lines.append("  CRITICAL: If the mapped column is a dimension table's primary key (like customer_master.KUNNR), you MUST GROUP BY the corresponding foreign key in the FACT table instead (like sales_data.customer) to prevent unmatched records from being lumped into a single NULL bucket!")
    return "\n".join(lines)


def detect_drilldown(question, kg):
    """If the question names a value at a hierarchy level (e.g. 'tyre'), build the
    filter + next-level group-by hint."""
    ql = question.lower()
    # flatten hierarchy levels in order, as (table, col)
    levels = []
    for name, h in kg["hierarchies"].items():
        for (col, card, ver, tbl) in h["levels"]:
            levels.append((tbl, col))
    if not levels:
        return None

    matched = None  # (idx, table, col, value)
    for idx, (t, c) in enumerate(levels):
        for v in kg["dim_values"].get((t, c), []):
            if len(v) < 2:
                continue
            if re.search(r'\b' + re.escape(v.lower()) + r'\b', ql):
                if matched is None or len(v) > len(matched[3]):
                    matched = (idx, t, c, v)
    if not matched:
        return None
    f_idx, f_t, f_c, f_val = matched

    # group level: explicit deeper level named via synonym, else next level
    group = None
    for (gt, gc, _p) in _syn_spans(question, kg):
        for j in range(f_idx + 1, len(levels)):
            if (gt, gc) == levels[j]:
                group = (gt, gc)
                break
        if group:
            break
    if group is None and f_idx + 1 < len(levels):
        group = levels[f_idx + 1]

    fact = next((n["table_name"] for n in kg["nodes"] if n["node_type"] == "fact"), None)
    measure = ""
    for m in kg["metrics"]:
        if m["term"] in ("sales", "revenue"):
            measure = m["expression"]
            break

    want = bool(_RANK_OR_BREAKDOWN_RE.search(question))
    hint = (f"\n\nHIERARCHY DRILL-DOWN (apply this):\n"
            f"- '{f_val}' is a value of `{f_c}` on `{f_t}`. FILTER: "
            f"WHERE `{f_t}`.`{f_c}` = '{f_val}' (exact value, case included).")
    if group and want:
        g_t, g_c = group
        rank_by = measure or (f"SUM(`{fact}`.`<measure>`)" if fact else "SUM(<measure>)")
        hint += (f"\n- Break it down WITHIN '{f_val}': GROUP BY `{g_t}`.`{g_c}` "
                 f"(the next level down), rank by {rank_by} DESC, apply the requested "
                 f"Top/Bottom N. Do NOT group by `{f_c}`. JOIN per the JOIN KEYS.")
    else:
        hint += "\n- Apply this filter; aggregate/rank as the question asks."
    return {"filter": (f_t, f_c, f_val), "group": group, "hint": hint}


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic SQL validation (the safety net)
# ─────────────────────────────────────────────────────────────────────────────
def validate_sql(sql, kg):
    """Return None if the SQL is consistent with the graph, else a dict with a
    'correction' message describing wrong-table columns and illegal joins."""
    if not sql or not kg.get("table_cols"):
        return None
    table_cols = kg["table_cols"]
    lower = {t: {c.lower() for c in cols} for t, cols in table_cols.items()}
    base_tables = set(table_cols)

    problems = []

    # 1. Wrong-table column references (cause of MySQL 1054)
    col_owner = {}
    for t, cols in table_cols.items():
        for c in cols:
            col_owner.setdefault(c.lower(), []).append(t)
    seen = set()
    for m in re.finditer(r'`([^`]+)`\s*\.\s*`([^`]+)`', sql):
        q, c = m.group(1), m.group(2)
        if q in base_tables and c.lower() not in lower[q] and (q, c) not in seen:
            seen.add((q, c))
            owners = col_owner.get(c.lower(), [])
            if owners:
                problems.append(f"`{q}`.`{c}` is INVALID — `{c}` lives on "
                                f"{', '.join('`'+o+'`' for o in owners)}. Use it there.")
            else:
                problems.append(f"`{q}`.`{c}` is INVALID — column `{c}` does not exist.")

    # 2. Illegal joins: any ON `a`.`x` = `b`.`y` between two base tables must be a graph edge
    edgeset = set()
    for e in kg["edges"]:
        edgeset.add(frozenset([(e["from_table"], e["from_column"]),
                               (e["to_table"], e["to_column"])]))
    for m in re.finditer(
            r'`([^`]+)`\s*\.\s*`([^`]+)`\s*=\s*`([^`]+)`\s*\.\s*`([^`]+)`', sql):
        a_t, a_c, b_t, b_c = m.groups()
        if a_t in base_tables and b_t in base_tables and a_t != b_t:
            key = frozenset([(a_t, a_c), (b_t, b_c)])
            if key not in edgeset:
                problems.append(
                    f"JOIN `{a_t}`.`{a_c}` = `{b_t}`.`{b_c}` is NOT an allowed key. "
                    f"Join only on the JOIN KEYS in the knowledge graph (joining on "
                    f"the wrong key multiplies rows and inflates totals).")

    if not problems:
        return None
    return {"problems": problems,
            "correction": ("Your SQL violates the knowledge graph and will be wrong. "
                           "Fix EVERY issue and return the same JSON shape:\n- "
                           + "\n- ".join(problems))}
