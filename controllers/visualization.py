import re
import uuid
import os
import io
import json
import requests
import traceback
import numpy as np
import pandas as pd
import mysql.connector
from flask_cors import CORS
from datetime import datetime
from arango import ArangoClient
from pyvis.network import Network
from typing import Any, Dict, List
from flask import Flask, request, jsonify, send_from_directory
from database.config import GRAPH_FOLDER
 

# --- NEW IMPORTS FOR UNSTRUCTURED DATA ---
from pypdf import PdfReader
from docx import Document

# --- CONTROLLERS ---
from controllers.chat import push_to_vector_db
from database.config import (
    ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB, 
    MISTRAL_API_KEY, MISTRAL_MODEL, BASE_URL, 
    UPLOAD_FOLDER, GRAPH_FOLDER, TEMP_UPLOAD_FOLDER,
    MYSQL_CONFIG 
)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GRAPH_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)

# ============================ DATABASE CONNECTION ============================
def get_db():
    try:
        client = ArangoClient(hosts=ARANGO_HOST)
        sysdb = client.db("_system", username=ARANGO_USER, password=ARANGO_PASS)
        if not sysdb.has_database(ARANGO_DB):
            sysdb.create_database(ARANGO_DB)
        db = client.db(ARANGO_DB, username=ARANGO_USER, password=ARANGO_PASS)
        return db
    except Exception as e:
        print(f" ArangoDB Connection Failed: {e}")
        return None

db = get_db()

# ============================ HELPER FUNCTIONS ============================

def sanitize_key(col: str) -> str:
    return re.sub(r'^\d+', '', re.sub(r'\W+', '_', str(col))).strip('_')

def clean_value(v: Any) -> Any:
    if pd.isna(v) or v is None: return None
    if isinstance(v, (np.generic, pd.Timestamp)): return v.item() if hasattr(v, 'item') else str(v)
    return v

def ensure_collection(name: str, edge: bool = False):
    if not db: raise Exception("Database not connected.")
    if not re.match(r'^[a-zA-Z0-9_-]+$', name): raise ValueError(f"Invalid collection name: {name}")
    if not db.has_collection(name): db.create_collection(name, edge=edge)

def insert_docs(name: str, df: pd.DataFrame):
    if df.empty: return
    col = db.collection(name)
    # col.truncate() # NOTE: Truncating might be undesirable in multi-session environment
    batch = []
    key_col_candidates = ["_key", "id", "key", f"{name.split('_')[-1].lower()}_id"]
    key_col = next((c for c in df.columns if c.lower() in key_col_candidates), None)
    
    for row in df.to_dict(orient="records"):
        doc = {}
        for k, v in row.items():
            sanitized_k = sanitize_key(k)
            cleaned_v = clean_value(v)
            if cleaned_v is not None: doc[sanitized_k] = cleaned_v
        if key_col and key_col in row: doc["_key"] = str(clean_value(row[key_col])) 
        batch.append(doc)

    if batch: col.import_bulk(batch, on_duplicate='update', overwrite=True)


def process_unstructured_to_arangodb_graph(extracted_data: Dict[str, Any], session_name: str) -> bool:
    """
    Converts AI-extracted JSON (entities/relationships) into ArangoDB nodes/edges.
    This function acts as the bridge, moving graph data from the JSON object 
    (which originated from MySQL) into ArangoDB's graph collections.
    """
    if not extracted_data:
        print("Error: Extracted data is empty.")
        return False
    
    # --- Data Extraction Logic: Recursively find all graph lists ---
    nodes_data = []
    relationships_data = []
    
    def find_data_lists(data):
        nonlocal nodes_data, relationships_data
        if isinstance(data, dict):
            # 1. Look for explicit graph lists (entities, dates, relationships)
            if 'dates' in data and isinstance(data['dates'], list):
                nodes_data.extend(data['dates'])
            if 'entities' in data and isinstance(data['entities'], list):
                nodes_data.extend(data['entities'])
            if 'relationships' in data and isinstance(data['relationships'], list):
                relationships_data.extend(data['relationships'])
            
            # 2. Handle nested entities (e.g., 'subsections', 'offices', 'publication_team')
            for key, value in data.items():
                if isinstance(value, list) and all(isinstance(i, dict) for i in value):
                    for item in value:
                        node = item.copy()
                        if 'type' not in node:
                            node['type'] = sanitize_key(key) # Use list name as entity type
                        nodes_data.append(node)
                elif isinstance(value, dict) or isinstance(value, list):
                    find_data_lists(value) # Recurse deeper

    find_data_lists(extracted_data)
    
    if not nodes_data:
        print("Warning: No graphable nodes (entities, dates, etc.) found in the AI output after parsing.")
        return False

    # Define collections that will hold the graph data in ArangoDB
    entity_col_name = f"{sanitize_key(session_name)}_entities"
    edge_col_name = "unstructured_relations"
    ensure_collection(entity_col_name)
    ensure_collection(edge_col_name, edge=True)

    # 1. Create Nodes (Vertices)
    nodes_to_insert = []
    
    for entity in nodes_data:
        # Determine the display name (used for the ArangoDB _key and the 'name' field)
        display_name = entity.get('name') or entity.get('event') or entity.get('date') or entity.get('title') or str(uuid.uuid4())
        entity_key = sanitize_key(str(display_name))
        
        doc = {k: clean_value(v) for k, v in entity.items()}
        
        # 💡 FIX: Guarantee a 'name' field exists for graph visualization lookup later.
        if 'name' not in doc:
            doc['name'] = str(display_name)
            
        doc["_key"] = entity_key
        
        nodes_to_insert.append(doc)

    if nodes_to_insert:
        # Insert nodes into ArangoDB
        db.collection(entity_col_name).import_bulk(nodes_to_insert, on_duplicate='update', overwrite=True)

    # 2. Create Edges (Relationships) 
    edges_to_insert = []
    if relationships_data:
        for rel in relationships_data:
            from_name = rel.get('source')
            to_name = rel.get('target')
            rel_type = rel.get('type', 'RELATED_TO')
            
            if from_name and to_name:
                # Use sanitized names to generate the _key reference for the edge documents
                from_key = sanitize_key(from_name)
                to_key = sanitize_key(to_name)
                
                # Format: collectionName/documentKey
                from_id = f"{entity_col_name}/{from_key}"
                to_id = f"{entity_col_name}/{to_key}"
                
                edge = {'_from': from_id, '_to': to_id, 'relation': rel_type}
                edges_to_insert.append(edge)

    if edges_to_insert:
        # Insert edges into ArangoDB
        db.collection(edge_col_name).import_bulk(edges_to_insert, on_duplicate='update', overwrite=True)
        
    # Return True ONLY if nodes were inserted. The graph visualization handles whether edges exist.
    return bool(nodes_to_insert) # Only return True if both nodes AND edges were inserted

def analyze_table(table_name: str, collection_columns: List[str]) -> Dict[str, Any]:
    """Uses Mistral AI to guess the primary key and human-readable label field."""
    if not db: return {}
    
    rows = list(db.aql.execute(f"FOR d IN {table_name} LIMIT 3 RETURN d"))
    if not rows: return {}
    
    cols = [k for k in collection_columns if not k.startswith("_")]
    
    prompt = f"""
    Analyze this table to find the best Human Readable Label and Primary Key.
    Table: {table_name}
    Columns: {cols}
    Sample Data (First Record): {json.dumps(rows[0], default=str)}
    
    INSTRUCTIONS:
    - "label_field": The column with the Name (e.g. "Product Name", "Customer Name", "Item"). 
      DO NOT use IDs or Dates as the label field. Must be one of the 'Columns'.
    - "primary_key": The unique ID column for linking. Must be one of the 'Columns'.
    
    Return ONLY JSON: {{"primary_key": "...", "label_field": "...", "description": "..."}}
    """
    
    info = extract_json(ask_mistral(prompt))
    
    pk_field = info.get("primary_key")
    if not pk_field or pk_field not in collection_columns:
        info["primary_key"] = cols[0] if cols else "_key" 

    label_field = info.get("label_field")
    if not label_field or label_field not in collection_columns: 
        info["label_field"] = cols[1] if len(cols) > 1 else cols[0]
        
    return info


def build_structured_edges(processed_tables: List[str],
                           structured_metadata: Dict[str, Dict[str, Any]],
                           session_name: str):
    """
    Auto-generate edges between structured collections using metadata:

    Rule:
    For each pair (src_tbl, tgt_tbl):
      - If tgt's primary_key field exists as a column in src,
        create edges src -> tgt where values match.

    This uses ONLY structured data; unstructured remains unchanged.
    """
    if not db:
        return

    # Ensure main edge collection exists
    ensure_collection("edges", edge=True)

    # 1) Clean up any old edges for these collections (optional but safer)
    try:
        db.aql.execute(
            """
            FOR e IN edges
                LET fc = PARSE_IDENTIFIER(e._from).collection
                LET tc = PARSE_IDENTIFIER(e._to).collection
                FILTER fc IN @cols OR tc IN @cols
                REMOVE e IN edges
            """,
            bind_vars={"cols": processed_tables}
        )
    except Exception as e:
        print(f"[build_structured_edges] Edge cleanup error: {e}")

    # 2) Build edges based on primary_key metadata
    for src_tbl in processed_tables:
        try:
            sample_list = list(db.aql.execute(f"FOR d IN {src_tbl} LIMIT 1 RETURN d"))
        except Exception as e:
            print(f"[build_structured_edges] Sample fetch error for {src_tbl}: {e}")
            continue

        if not sample_list:
            continue

        sample_doc = sample_list[0]

        for tgt_tbl in processed_tables:
            if src_tbl == tgt_tbl:
                continue

            tgt_meta = structured_metadata.get(tgt_tbl, {}) or {}
            tgt_pk = tgt_meta.get("primary_key") or "_key"

            # If src doesn't even have this field, no join possible
            if tgt_pk not in sample_doc:
                continue

            relation_name = f"{src_tbl}_to_{tgt_tbl}"

            aql = """
            FOR s IN @@src
                FILTER HAS(s, @fk_field) && s[@fk_field] != null
                FOR t IN @@tgt
                    FILTER HAS(t, @pk_field) && t[@pk_field] == s[@fk_field]
                    INSERT {
                        _from: s._id,
                        _to: t._id,
                        relation: @relation,
                        session: @session,
                        fk_field: @fk_field
                    } INTO edges
                    OPTIONS { ignoreErrors: true }
            """

            try:
                db.aql.execute(
                    aql,
                    bind_vars={
                        "@src": src_tbl,
                        "@tgt": tgt_tbl,
                        "fk_field": tgt_pk,   # foreign key field in src
                        "pk_field": tgt_pk,   # primary key field in tgt
                        "relation": relation_name,
                        "session": session_name,
                    }
                )
                print(f"[build_structured_edges] Edges created: {src_tbl} -> {tgt_tbl} on {tgt_pk}")
            except Exception as e:
                print(f"[build_structured_edges] Edge build error {src_tbl} -> {tgt_tbl}: {e}")


def process_file_to_db(

        file_bytes: bytes, 
        filename: str, 
        session_name: str
     ) -> tuple[str, Dict[str, Any], List[str]]:
    """
    Handles file reading, delimiter sniffing, DB insertion, and schema analysis.
    Generates collection name as: session_name_basefilename
    """

    # ---- filename se base name nikaalo ----
    original_base_name = os.path.splitext(filename)[0]
    sanitized_base_name = sanitize_key(original_base_name)
    sanitized_session_name = sanitize_key(session_name)

    # NEW COLLECTION NAME: session_name_basefilename
    t_name = f"{sanitized_session_name}_{sanitized_base_name}"

    if not t_name:
        raise ValueError("Could not generate valid collection name from file and session name.")

    DELIMITERS = [',', ';', '|', '\t']
    file_extension = filename.split('.')[-1].lower()

    df = None

    # ---- CSV / TXT ----
    if file_extension in ["csv", "txt"]:
        for delim in DELIMITERS:
            try:
                df = (
                    pd.read_csv(io.BytesIO(file_bytes), sep=delim, dtype=str)
                    .replace({np.nan: None})
                )
                # simple sanity check
                if len(df.columns) > 1 or (len(df.columns) == 1 and len(df) > 0):
                    break
            except Exception:
                continue

        if df is None or df.empty:
            raise ValueError(f"Failed to load file. Could not auto-detect delimiter for {filename}.")

    # ---- EXCEL ----
    elif file_extension in ["xls", "xlsx"]:
        try:
            df = (
                pd.read_excel(io.BytesIO(file_bytes), dtype=str)
                .replace({np.nan: None})
            )
            if df.empty:
                raise ValueError(f"Excel file {filename} loaded but is empty.")
        except Exception as e:
            raise ValueError(f"Failed to load Excel file {filename}: {e}")

    else:
        raise ValueError(f"Unsupported file type: .{file_extension}. Only CSV/Excel are supported.")

    # Sanitized columns list for LLM analysis
    collection_columns = [sanitize_key(c) for c in df.columns]

    # Import documents to ArangoDB with the new session-prefixed name
    ensure_collection(t_name)
    insert_docs(t_name, df)

    # Analyze and get metadata
    metadata = analyze_table(t_name, collection_columns)

    return t_name, metadata, collection_columns



# ============================ AI & TEXT EXTRACTION ============================

from model.llm_client import call_llm_chat

def ask_mistral(prompt: str) -> str:
    try:
        messages = [{"role": "user", "content": prompt}]
        response = call_llm_chat(messages, json_mode=True, temperature=0.15)
        return response if response else "{}"
    except Exception as e:
        print(f"[LLM] Request failed: {e}")
        return "{}"

def extract_json(text: str) -> Dict[str, Any]:
    match = re.search(r"\{[\s\S]*\}", text, re.DOTALL)
    if match:
        try: return json.loads(match.group(0))
        except: pass
    return {}

# --- NEW: UNSTRUCTURED TEXT EXTRACTOR ---
def extract_text_from_binary(file_bytes: bytes, ext: str) -> str:
    """Extracts raw text from PDF or Docx bytes."""
    text_content = ""
    try:
        file_stream = io.BytesIO(file_bytes)
        
        if ext == 'pdf':
            reader = PdfReader(file_stream)
            for page in reader.pages:
                text_content += page.extract_text() + "\n"
        
        elif ext == 'docx':
            doc = Document(file_stream)
            for para in doc.paragraphs:
                text_content += para.text + "\n"
                
        return text_content.strip()
    except Exception as e:
        print(f"Text extraction failed: {e}")
        return ""

# --- NEW: MYSQL JSON TABLE SETUP ---
def ensure_mysql_unstructured_table():
    """Creates the MySQL table for storing unstructured JSON data if not exists."""
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor()
        sql = """
        CREATE TABLE IF NOT EXISTS unstructured_docs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            filename VARCHAR(255),
            session_name VARCHAR(255),
            upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            extracted_json JSON
        );
        """
        cursor.execute(sql)
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"MySQL Table Init Error: {e}")

# ============================ FILE PROCESSORS ============================

def process_unstructured_to_mysql(file_bytes, filename, session_name) -> Dict[str, Any]:
    """Handles PDF/Docx -> AI -> normalized JSON -> MySQL JSON"""
    ext = filename.split('.')[-1].lower()
    raw_text = extract_text_from_binary(file_bytes, ext)
    
    if not raw_text:
        return {"error": "No text extracted"}

    # 1. AI Extraction to JSON (force a specific schema)
    prompt = f"""
You are an information extraction engine.

From the document text below, extract graph-friendly structured data and
return ONLY a valid JSON object with this exact top-level structure:

{{
  "entities": [
    {{ "name": "React", "type": "Technology" }},
    {{ "name": "Virtual DOM", "type": "Concept" }}
  ],
  "dates": [
    {{ "name": "React release", "date": "2013" }}
  ],
  "relationships": [
    {{ "source": "React", "target": "Virtual DOM", "type": "USES" }}
  ],
  "summary": "One or two sentence summary of the document."
}}

Rules:
- "entities" MUST be a list of objects with at least "name" and "type".
- "dates" MUST be a list of objects with at least "name" and "date".
- "relationships" MUST be a list of objects with "source", "target", and "type".
- If you don't find anything for a section, return an empty list for it.
- Always include all four keys: "entities", "dates", "relationships", "summary".
- Do NOT wrap the JSON in backticks or any extra text. Return ONLY the JSON.

Document text (truncated):
\"\"\"{raw_text[:8000]}\"\"\"
"""
    try:
        llm_output = ask_mistral(prompt)
        extracted_data = extract_json(llm_output)
    except Exception as e:
        print(f"AI extraction error: {e}")
        extracted_data = {}

    # 1b. Normalize JSON shape so the graph code can rely on it
    if not isinstance(extracted_data, dict):
        extracted_data = {}

    # Ensure keys exist with correct types
    entities = extracted_data.get("entities")
    if not isinstance(entities, list):
        entities = []
    dates = extracted_data.get("dates")
    if not isinstance(dates, list):
        dates = []
    relationships = extracted_data.get("relationships")
    if not isinstance(relationships, list):
        relationships = []

    summary = extracted_data.get("summary")
    if not isinstance(summary, str):
        summary = ""

    normalized_data = extracted_data.copy()
    normalized_data["entities"] = entities
    normalized_data["dates"] = dates
    normalized_data["relationships"] = relationships
    normalized_data["summary"] = summary

    # 2. Store in MySQL
    try:
        ensure_mysql_unstructured_table()
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor()
        
        sql = """
            INSERT INTO unstructured_docs (filename, session_name, extracted_json)
            VALUES (%s, %s, %s)
        """
        cursor.execute(sql, (filename, session_name, json.dumps(normalized_data)))
        conn.commit()
        doc_id = cursor.lastrowid
        cursor.close()
        conn.close()
        
        return {"id": doc_id, "filename": filename, "data": normalized_data}
    except Exception as e:
        print(f"MySQL Insert Error: {e}")
        return {"error": str(e)}


# ============================ GRAPH GENERATION (PyVis) ============================

def generate_graph_html(raw_edges: List[Dict[str, Any]], metadata: Dict[str, Dict[str, Any]], filename: str) -> str:
    """Generates an interactive pyvis HTML file with robust label fallbacks."""
    if not raw_edges: 
        return ""
        
    net = Network(height="750px", width="100%", bgcolor="#222222", font_color="white", cdn_resources='remote', notebook=True)
    
    options_dict = {
        "physics": {"forceAtlas2Based": {"gravitationalConstant": -150, "centralGravity": 0.005, "springLength": 120, "springConstant": 0.05, "avoidOverlap": 0.8}, "minVelocity": 0.75, "solver": "forceAtlas2Based", "timestep": 0.35, "adaptiveTimestep": True},
        "manipulation": {"enabled": True, "initiallyActive": False, "addNode": False, "addEdge": False, "editNode": False, "editEdge": False, "deleteNode": False, "deleteEdge": False, "controlNodeStyle": {}},
        "nodes": {"font": {"size": 16, "color": "white"}, "borderWidth": 1, "borderWidthSelected": 2},
        "edges": {"color": {"inherit": True}, "smooth": {"enabled": True, "type": "dynamic"}}
    }
    net.set_options(json.dumps(options_dict))

    viz_nodes = {} 
    viz_edges = set()
    
    colors = ["#EF476F", "#118AB2", "#06D6A0", "#FFD166", "#9D4EDD", "#A2D2FF"]
    color_map = {}
    
    # Universal Label Fallback Fields for Structured/Unstructured data
    FALLBACK_FIELDS = ["name", "label", "title", "event", "year", "_key"] 

    for e in raw_edges:
        src_tbl = e['from_col']
        tgt_tbl = e['to_col']
        src_data = e['source_data'] or {}
        tgt_data = e['target_data'] or {}
        
        if src_tbl not in color_map: color_map[src_tbl] = colors[len(color_map)%len(colors)]
        if tgt_tbl not in color_map: color_map[tgt_tbl] = colors[len(color_map)%len(colors)]

        # Get the primary label field specified in metadata (defaulting to '_key')
        src_primary_field = metadata.get(src_tbl, {}).get("label_field", "_key") 
        tgt_primary_field = metadata.get(tgt_tbl, {}).get("label_field", "_key")
        
        
        # --- Source Label Determination ---
        src_label = str(src_data.get(src_primary_field)).strip()
        if not src_label or src_label.lower() in ("none", "null", "unknown"):
            src_label = ""
            for field in FALLBACK_FIELDS:
                label_candidate = str(src_data.get(field)).strip()
                if label_candidate and label_candidate.lower() not in ("none", "null", "unknown"):
                    src_label = label_candidate
                    break
        if not src_label: src_label = "Unknown"
        
        
        # --- Target Label Determination ---
        tgt_label = str(tgt_data.get(tgt_primary_field)).strip()
        if not tgt_label or tgt_label.lower() in ("none", "null", "unknown"):
            tgt_label = ""
            for field in FALLBACK_FIELDS:
                label_candidate = str(tgt_data.get(field)).strip()
                if label_candidate and label_candidate.lower() not in ("none", "null", "unknown"):
                    tgt_label = label_candidate
                    break
        if not tgt_label: tgt_label = "Unknown"

        src_viz_id = src_data.get('_id') 
        tgt_viz_id = tgt_data.get('_id')
        
        if not src_viz_id or not tgt_viz_id:
            continue
        
        if src_viz_id not in viz_nodes:
            viz_nodes[src_viz_id] = {"label": src_label, "group": src_tbl, "count": 0, "title_info": src_data}
        if tgt_viz_id not in viz_nodes:
            viz_nodes[tgt_viz_id] = {"label": tgt_label, "group": tgt_tbl, "count": 0, "title_info": tgt_data}

        viz_nodes[src_viz_id]["count"] += 1
        viz_nodes[tgt_viz_id]["count"] += 1
        viz_edges.add(tuple(sorted((src_viz_id, tgt_viz_id))))

    for vid, data in viz_nodes.items():
        min_size, max_size = 15, 50
        size = max(min_size, min(max_size, data["count"] * 3)) 
        
        title_html = f"Category: {data['group']}<br>Name: {data['label']}<br>Key: {vid.split('/')[-1]}<br>Connections: {data['count']}"
        
        net.add_node(vid, 
                      label=data["label"], 
                      color=color_map[data["group"]], 
                      size=size, 
                      title=title_html)

    for src, tgt in viz_edges:
        net.add_edge(src, tgt, color="#888888") 

    legend_html = '<div style="position: absolute; top: 10px; left: 10px; z-index: 1000; background: rgba(255,255,255,0.9); padding: 10px; border-radius: 8px; border: 1px solid #ccc; width: 300px; font-family: sans-serif;">'
    legend_html += '<h4 style="margin:0 0 10px 0; color:#333; border-bottom:1px solid #ddd; padding-bottom:5px; font-size:14px;">Index</h4>'
    for cat, col in color_map.items():
        legend_html += f'<div style="display:flex;align-items:center;margin-bottom:5px;"><div style="width:15px;height:15px;background:{col};border-radius:50%;margin-right:8px;border:1px solid #aaa;"></div><span style="color:#333;font-size:12px;">{cat}</span></div>'
    legend_html += "</div>"
    
    filepath = os.path.join(GRAPH_FOLDER, filename)
    net.save_graph(filepath)
    
    with open(filepath, 'r', encoding='utf-8') as f: 
        html_content = f.read()
    
    final_html_content = html_content.replace('<body>', f'<body>{legend_html}')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(final_html_content)
        
    return filepath

def upload_and_process_arangodb():
    if not db:
        return jsonify({"error": "ArangoDB connection failed."}), 500

    try:
        files = request.files.getlist("files")
        session_name = request.form.get("session_name") or f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        structured_metadata: Dict[str, Dict[str, Any]] = {}
        processed_tables: List[str] = []
        unstructured_results: List[Dict[str, Any]] = []

        # start with default edge collection
        all_edge_collections: List[str] = ["edges"]
        all_node_collections: List[str] = []

        structured_errors: List[str] = []
        unstructured_errors: List[str] = []

        # --- FILE PROCESSING LOOP ---
        for f in files:
            file_bytes = f.read()
            ext = f.filename.split('.')[-1].lower()

            # ---------- STRUCTURED ----------
            if ext in ['csv', 'txt', 'xls', 'xlsx']:
                try:
                    t_name, meta, _ = process_file_to_db(file_bytes, f.filename, session_name)
                    structured_metadata[t_name] = meta
                    processed_tables.append(t_name)
                    all_node_collections.append(t_name)
                except Exception as e:
                    msg = f"Structured Error ({f.filename}): {e}"
                    print(msg)
                    traceback.print_exc()
                    structured_errors.append(msg)

            # ---------- UNSTRUCTURED ----------
            elif ext in ['pdf', 'docx']:
                try:
                    res = process_unstructured_to_mysql(file_bytes, f.filename, session_name)
                    unstructured_results.append(res)

                    ai_graph_data = res.get("data")

                    # backward-compat guard if something wraps it again
                    if isinstance(ai_graph_data, dict) and 'extracted_json' in ai_graph_data:
                        json_string_candidate = ai_graph_data.get("extracted_json")
                        if isinstance(json_string_candidate, str):
                            try:
                                ai_graph_data = json.loads(json_string_candidate)
                            except (json.JSONDecodeError, TypeError):
                                print("Warning: Failed to deserialize nested JSON string for graph data.")
                                continue
                        else:
                            ai_graph_data = json_string_candidate

                    if isinstance(ai_graph_data, dict):
                        # create graph collections for unstructured
                        nodes_created = process_unstructured_to_arangodb_graph(ai_graph_data, session_name)

                        UNSTRUCTURED_NODE_COL = f"{sanitize_key(session_name)}_entities"
                        UNSTRUCTURED_EDGE_COL = "unstructured_relations"

                        if nodes_created:
                            all_node_collections.append(UNSTRUCTURED_NODE_COL)
                            all_edge_collections.append(UNSTRUCTURED_EDGE_COL)

                            structured_metadata[UNSTRUCTURED_NODE_COL] = {
                                "label_field": "name",
                                "primary_key": "_key"
                            }

                except Exception as e:
                    msg = f"Unstructured Error ({f.filename}): {e}"
                    print(msg)
                    traceback.print_exc()
                    unstructured_errors.append(msg)

            else:
                print(f"Skipping unsupported file: {f.filename}")

            if processed_tables:
                build_structured_edges(processed_tables, structured_metadata, session_name)
        # ================== GRAPH BUILDING (COMBINED DATA) ==================
        graph_url = None
        raw_viz: List[Dict[str, Any]] = []

        # Make sure main edge collection exists
        ensure_collection("edges", edge=True)

        # 1) Pull ALL edges from each edge collection (no FILTER here)
        for col_name in sorted(set(all_edge_collections)):
            if not db.has_collection(col_name):
                continue

            viz_query = f"""
                FOR e IN {col_name}
                    LIMIT 500
                    RETURN {{
                        from_col: PARSE_IDENTIFIER(e._from).collection,
                        to_col: PARSE_IDENTIFIER(e._to).collection,
                        source_data: DOCUMENT(e._from),
                        target_data: DOCUMENT(e._to)
                    }}
            """

            try:
                results = list(db.aql.execute(viz_query))
                raw_viz.extend(results)
            except Exception as q_err:
                print(f"AQL error for collection {col_name}: {q_err}")
                traceback.print_exc()

        # 2) Now filter in Python so we keep only THIS session's edges

        # structured tables for this session are named: "<sanitize(session_name)>_<something>"
        session_prefix = sanitize_key(session_name) + "_"
        # unstructured node collection for this session
        session_entities_col = f"{sanitize_key(session_name)}_entities"

        filtered_viz: List[Dict[str, Any]] = []
        for e in raw_viz:
            from_col = (e.get("from_col") or "")
            to_col = (e.get("to_col") or "")

            if (
                from_col.startswith(session_prefix)
                or to_col.startswith(session_prefix)
                or from_col == session_entities_col
                or to_col == session_entities_col
            ):
                filtered_viz.append(e)

        raw_viz = filtered_viz

        if raw_viz:
            html_filename = f"graph_{session_name}.html"
            generate_graph_html(raw_viz, structured_metadata, html_filename)
            graph_url = f"{BASE_URL}/{GRAPH_FOLDER}/{html_filename}"

            # Save graph URL in MySQL (same as before)
            try:
                conn_db = mysql.connector.connect(**MYSQL_CONFIG)
                cursor = conn_db.cursor()
                sql = """
                    INSERT INTO graph (session_name, graph_url)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE graph_url = VALUES(graph_url)
                """
                cursor.execute(sql, (session_name, graph_url))
                conn_db.commit()
                cursor.close()
                conn_db.close()
                print(" MySQL graph metadata inserted.")
            except Exception as e:
                print(f" MySQL graph update failed (ignoring): {e}")
        else:
            print("No edges for this session after filtering. Skipping graph visualization.")

        # ================== RESPONSE ==================
        return jsonify({
            "message": "Processing complete",
            "session_name": session_name,
            "structured_data": {
                "tables_created": processed_tables,
                "graph_url": graph_url,
                "errors": structured_errors
            },
            "unstructured_data": {
                "files_processed": len(unstructured_results),
                "unstructured_collections": list(set(all_node_collections) - set(processed_tables)),
                "results": unstructured_results,
                "errors": unstructured_errors
            }
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500