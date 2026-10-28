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
from controllers.chat import push_to_vector_db
from database.config import (
    ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB, 
    MISTRAL_API_KEY, MISTRAL_MODEL, BASE_URL, 
    UPLOAD_FOLDER, GRAPH_FOLDER, TEMP_UPLOAD_FOLDER,
    MYSQL_CONFIG 
)

# ============================ CONFIGURATION ============================
app = Flask(__name__)
CORS(app)

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GRAPH_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)

# ============================ DATABASE CONNECTION ============================

def get_db():
    """Initializes and returns the ArangoDB connection."""
    try:
        client = ArangoClient(hosts=ARANGO_HOST)
        sysdb = client.db("_system", username=ARANGO_USER, password=ARANGO_PASS)
        
        if not sysdb.has_database(ARANGO_DB):
            sysdb.create_database(ARANGO_DB)
            print(f" ArangoDB Database '{ARANGO_DB}' created.")
        
        db = client.db(ARANGO_DB, username=ARANGO_USER, password=ARANGO_PASS)
        print(f" ArangoDB connected to '{ARANGO_DB}'.")
        return db
    except Exception as e:
        print(f" ArangoDB Connection Failed: {e}")
        return None

db = get_db()
if not db: 
    print("FATAL: Cannot start application without ArangoDB connection.")

# ============================ HELPER FUNCTIONS ============================

def sanitize_key(col: str) -> str:
    """Sanitizes a string for use as an ArangoDB document key or field name."""
    # ArangoDB keys can only contain letters, numbers, and '_', '-'
    # We use '_' for full sanitization simplicity.
    return re.sub(r'^\d+', '', re.sub(r'\W+', '_', str(col))).strip('_')

def clean_value(v: Any) -> Any:
    """Cleans and normalizes a value before insertion."""
    if pd.isna(v) or v is None:
        return None
    if isinstance(v, (np.generic, pd.Timestamp)):
        return v.item() if hasattr(v, 'item') else str(v)
    return v

def ensure_collection(name: str, edge: bool = False):
    """Ensures an ArangoDB collection exists, creating it if necessary."""
    if not db: raise Exception("Database not connected.")
    # Check if collection name is valid (ArangoDB limits length and characters)
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError(f"Invalid collection name generated: {name}")

    if not db.has_collection(name): 
        db.create_collection(name, edge=edge)
        print(f"Created collection: {name}")

def insert_docs(name: str, df: pd.DataFrame):
    """Truncates the collection and inserts documents from a DataFrame."""
    if df.empty: return
    col = db.collection(name)
    col.truncate()
    
    batch = []
    key_col_candidates = ["_key", "id", "key", f"{name.split('_')[-1].lower()}_id"]
    key_col = next((c for c in df.columns if c.lower() in key_col_candidates), None)
    
    for row in df.to_dict(orient="records"):
        doc = {}
        for k, v in row.items():
            sanitized_k = sanitize_key(k)
            cleaned_v = clean_value(v)
            if cleaned_v is not None:
                doc[sanitized_k] = cleaned_v
        
        if key_col and key_col in row and clean_value(row[key_col]) is not None:
             doc["_key"] = str(clean_value(row[key_col])) 
        
        batch.append(doc)

    if batch: 
        col.import_bulk(batch, on_duplicate='update', overwrite=True) 
        print(f"Inserted {len(batch)} documents into {name}.")


# ============================ AI METADATA ANALYSIS ============================
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
    """Extracts a valid JSON object from a potentially text-wrapped response."""
    match = re.search(r"\{[\s\S]*\}", text, re.DOTALL)
    if match:
        try: return json.loads(match.group(0))
        except: pass
    return {}

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

# ============================ FILE PROCESSING ============================

def process_file_to_db(f, session_name: str) -> tuple[str, Dict[str, Any], List[str]]:
    """
    Handles file reading, delimiter sniffing, DB insertion, and schema analysis.
    Generates collection name as: session_name_basefilename
    """
    original_base_name = os.path.splitext(f.filename)[0]
    sanitized_base_name = sanitize_key(original_base_name)
    sanitized_session_name = sanitize_key(session_name)
    
    # NEW COLLECTION NAME: session_name_basefilename
    t_name = f"{sanitized_session_name}_{sanitized_base_name}"
    
    if not t_name:
        raise ValueError("Could not generate valid collection name from file and session name.")

    DELIMITERS = [',', ';', '|', '\t']
    file_extension = f.filename.split('.')[-1].lower()
    
    # Read file content directly into memory
    file_bytes = f.read()
    df = None

    if file_extension in ["csv", "txt"]:
        for delim in DELIMITERS:
            try:
                df = pd.read_csv(io.BytesIO(file_bytes), sep=delim, dtype=str).replace({np.nan: None})
                if len(df.columns) > 1 or (len(df.columns) == 1 and len(df) > 0):
                    break
            except Exception:
                continue
        if df is None or df.empty:
            raise ValueError(f"Failed to load file. Could not auto-detect delimiter for {f.filename}.")
    
    elif file_extension in ["xls", "xlsx"]:
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), dtype=str).replace({np.nan: None})
            if df.empty:
                raise ValueError(f"Excel file {f.filename} loaded but is empty.")
        except Exception as e:
            raise ValueError(f"Failed to load Excel file {f.filename}: {e}")
    
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

# ============================ GRAPH VISUALIZATION ============================

def generate_graph_html(raw_edges: List[Dict[str, Any]], metadata: Dict[str, Dict[str, Any]], filename: str) -> str:
    """Generates an interactive pyvis HTML file from the graph data."""
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

    for e in raw_edges:
        src_tbl = e['from_col']
        tgt_tbl = e['to_col']
        
        if src_tbl not in color_map: color_map[src_tbl] = colors[len(color_map)%len(colors)]
        if tgt_tbl not in color_map: color_map[tgt_tbl] = colors[len(color_map)%len(colors)]

        src_field = metadata.get(src_tbl, {}).get("label_field", "Unknown")
        tgt_field = metadata.get(tgt_tbl, {}).get("label_field", "Unknown")
        
        src_label = str((e['source_data'] or {}).get(src_field, e['source_data'].get("_key", "Unknown"))).strip()
        tgt_label = str((e['target_data'] or {}).get(tgt_field, e['target_data'].get("_key", "Unknown"))).strip()
        
        src_viz_id = e['source_data']['_id'] 
        tgt_viz_id = e['target_data']['_id']
        
        if src_viz_id not in viz_nodes:
            viz_nodes[src_viz_id] = {"label": src_label, "group": src_tbl, "count": 0, "title_info": e['source_data']}
        if tgt_viz_id not in viz_nodes:
            viz_nodes[tgt_viz_id] = {"label": tgt_label, "group": tgt_tbl, "count": 0, "title_info": e['target_data']}

        viz_nodes[src_viz_id]["count"] += 1
        viz_nodes[tgt_viz_id]["count"] += 1
        viz_edges.add(tuple(sorted((src_viz_id, tgt_viz_id))))

    for vid, data in viz_nodes.items():
        min_size, max_size = 15, 50
        size = max(min_size, min(max_size, data["count"] * 3)) 
        
        title_html = f"Category: {data['group']}<br>Name: {data['label']}<br>Connections: {data['count']}<br>_id: {vid}"
        
        net.add_node(vid, 
                      label=data["label"], 
                      color=color_map[data["group"]], 
                      size=size, 
                      title=title_html)

    for src, tgt in viz_edges:
        net.add_edge(src, tgt, color="#888888", width=0.8, dashes=True)

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

# ============================ MAIN GRAPH API ============================
 
def upload_and_process_arangodb():
    """
    Handles file upload, ArangoDB import, Mistral analysis, graph building,
    MySQL logging, and returns a JSON response with the graph data and URL.
    """
    if not db:
        return jsonify({"error": "ArangoDB connection failed."}), 500

    try:
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "No files uploaded"}), 400

        # Generate a unique session name
        session_name = request.form.get("session_name") or f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}" 
        
        metadata = {}
        processed_tables = []
        
        # --- File Processing & ArangoDB Ingestion ---
        for f in files:
            # Pass the file and the session_name to generate unique collection name
            try:
                t_name, meta, _ = process_file_to_db(f, session_name) 
                metadata[t_name] = meta
                processed_tables.append(t_name)
            except Exception as e:
                print(f"Error processing file {f.filename}: {e}")
                return jsonify({"error": f"Error processing file {f.filename}: {str(e)}"}), 400
        
        if not metadata:
            return jsonify({"error": "No data successfully loaded from files."}), 400
        
        # --- BUILD GRAPH (Rule-Based FK Detection) ---
        # The edges collection should also be session-specific for cleanup, 
        # or globally shared if connections across sessions are allowed. 
        # For simplicity and isolation, we'll keep the edges global but be mindful of linking.
        ensure_collection("edges", edge=True)
        db.collection("edges").truncate() # Clear previous session edges (if reusing name)
        
        tables = list(metadata.keys())
        total_edges = 0
        
        # Cache all documents in memory for efficient FK lookup
        db_cache = {t: list(db.aql.execute(f"FOR d IN {t} RETURN d")) for t in tables}

        for table_A in tables:
            rows_A = db_cache.get(table_A, [])
            if not rows_A: continue
            
            columns_A = [k for k in rows_A[0].keys() if not k.startswith("_")]

            for table_B in tables:
                if table_A == table_B: continue
                rows_B = db_cache.get(table_B, [])
                if not rows_B: continue
                
                meta_B = metadata.get(table_B, {})
                pk_B_field = meta_B.get("primary_key")
                
                if not pk_B_field: continue
                
                # Create a lookup map: {PK value: ArangoDB _id} for table B
                b_lookup = {str(r.get(pk_B_field)): r["_id"] for r in rows_B if pk_B_field in r and r.get(pk_B_field) is not None}
                if not b_lookup: continue

                potential_fk = []
                # Check for Foreign Key candidates in A pointing to PK in B
                for col in columns_A:
                    matches, checks = 0, 0
                    for r in rows_A[:20]:
                        val = str(r.get(col, ""))
                        if val and val in b_lookup: matches += 1
                        checks += 1
                    
                    if checks > 0 and (matches / checks) > 0.5:
                        potential_fk.append(col)

                if potential_fk:
                    edge_batch = []
                    for fk_col in potential_fk:
                        for r_a in rows_A:
                            val_a = str(r_a.get(fk_col, ""))
                            if val_a in b_lookup:
                                edge_batch.append({
                                    "_from": r_a["_id"], "_to": b_lookup[val_a],
                                    "type": "RELATED_TO", "label": fk_col
                                })
                    
                    if edge_batch:
                        db.collection("edges").import_bulk(edge_batch)
                        total_edges += len(edge_batch)
        
        # --- RETRIEVE NODES AND EDGES FOR API RESPONSE ---
        
        # 1. Retrieve Nodes
        nodes_out = []
        for t_name_full in processed_tables:
            # The label should be based on the original file name, not the session prefix
            label_base = t_name_full.split('_', 1)[-1].capitalize() 
            
            table_nodes = list(db.aql.execute(f"FOR d IN {t_name_full} RETURN d"))
            
            for doc in table_nodes:
                props = {k: v for k, v in doc.items() if not k.startswith("_")}
                node_id = doc.get("_key") or doc.get("_id")
                
                nodes_out.append({
                    "id": str(node_id),
                    "label": label_base, # Use only the base name as the label
                    "props": props
                })

        # 2. Retrieve Edges
        edges_out = []
        raw_edges_data = list(db.aql.execute(f"FOR e IN edges RETURN e"))
        
        for edge in raw_edges_data:
            source_key = edge["_from"].split('/')[-1]
            target_key = edge["_to"].split('/')[-1]
            
            rel_type = edge.get("label") or edge.get("type") or "RELATED_TO"
            props = {k: v for k, v in edge.items() if not k.startswith("_")}

            edges_out.append({
                "source": source_key,
                "target": target_key,
                "type": rel_type.upper(),
                "props": props
            })
            
        # --- VISUALIZATION ---
        html_filename = f"graph_{session_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.html"
        
        viz_data_query = """
            FOR e IN edges
                SORT RAND() LIMIT 1000
                LET source = DOCUMENT(e._from)
                LET target = DOCUMENT(e._to)
                RETURN { 
                    from_col: PARSE_IDENTIFIER(e._from).collection,
                    to_col: PARSE_IDENTIFIER(e._to).collection,
                    source_data: source, 
                    target_data: target
                }
        """
        raw_edges_data_for_viz = list(db.aql.execute(viz_data_query))
        
        html_path = generate_graph_html(raw_edges_data_for_viz, metadata, html_filename)
        graph_url = f"{BASE_URL}/{GRAPH_FOLDER}/{html_filename}"

        # === START: MYSQL GRAPH TABLE INSERT LOGIC ===
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
        # === END: MYSQL GRAPH TABLE INSERT LOGIC ===

        # ---- PUSH TO VECTOR DATABASE ----
        # push_to_vector_db(request.files.getlist("files"), nodes_out, edges_out, session_name)
        # Create a proper vector session ID
        vector_session_id = str(uuid.uuid4())

# Push to ChromaDB using the correct session ID
        push_to_vector_db(request.files.getlist("files"),nodes_out,edges_out,vector_session_id)


        return jsonify({
            "message": f"Successfully created {total_edges} edges.",
            "total_edges": total_edges,
            "metadata": metadata,
            "html_url": graph_url,
            "nodes": nodes_out,
            "edges": edges_out
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

 
def send_from_directory_controller(filename):
    """Serves the static HTML graph file."""
    return send_from_directory(GRAPH_FOLDER, filename)