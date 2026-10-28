import os
import uuid
import re
import json
import mysql.connector
from datetime import datetime
from flask import Blueprint, request, jsonify
from database.llm_service import LLMService
from database.database_service import DatabaseService
from database.config import MYSQL_CONFIG, GRAPH_FOLDER, UPLOAD_FOLDER,BASE_URL
from pyvis.network import Network
import pandas as pd
from sqlalchemy import create_engine
# Initialize services
llm_service = LLMService()
db_service = DatabaseService()

# Ensure folders exist (using config variables is safer)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GRAPH_FOLDER, exist_ok=True)

def safe_key(text):
    """Sanitize text for use as an ArangoDB document key."""
    if not text:
        return f"auto_{str(uuid.uuid4())[:8]}"
    sanitized = re.sub(r'[^a-zA-Z0-9\-_]', '_', text).strip('_')
    if not sanitized:
        return f"auto_{str(uuid.uuid4())[:8]}"
    return sanitized[:200]

def get_external_db_content(config, query):
    """Connects to the MySQL/Postgres DB and returns results as text."""
    try:
        driver = "mysql+pymysql" if config['type'] == 'mysql' else "postgresql+psycopg2"
        uri = f"{driver}://{config['username']}:{config['password']}@{config['host']}:{config['port']}/{config['database']}"
        engine = create_engine(uri)
        df = pd.read_sql(query, engine)
        # Convert the table rows to a text string for the LLM to 'read'
        return df.to_string(index=False)
    except Exception as e:
        return f"Error fetching DB data: {str(e)}"

def process_books():
    # 1. Determine Source & Session Name
    source_type = request.form.get("source_type", "local") 
    session_name = request.form.get("session_name") or f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    session_id = request.form.get("session_id")
    
    # Fetch workspace_arango_db if session_id is provided
    target_arango_db = None
    if session_id:
        try:
            conn_db = mysql.connector.connect(**MYSQL_CONFIG)
            cursor = conn_db.cursor(dictionary=True)
            cursor.execute("SELECT workspace_arango_db FROM workspaces WHERE session_id = %s", (session_id,))
            row = cursor.fetchone()
            if row and row.get("workspace_arango_db"):
                target_arango_db = row["workspace_arango_db"]
            cursor.close()
            conn_db.close()
        except Exception as e:
            print(f"DEBUG: Failed to fetch workspace_arango_db: {e}")

    # Use specific db if found, else default
    local_db_service = DatabaseService(db_name=target_arango_db) if target_arango_db else db_service

    # 2. Clear Database & Prepare
    try:
        local_db_service.truncate_collections()
    except Exception as e:
        print(f"DEBUG: Error clearing database: {e}")
        
    unique_session_id = str(uuid.uuid4())[:8]
    books_content = {}

    # 3. BRANCHING LOGIC: Extract Data from Files OR Database
    if source_type == "local":
        # Check for files (Your original Step 1 logic)
        if 'files' not in request.files:
            return jsonify({"error": "No files uploaded under key 'files'"}), 400
        
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({"error": "No valid files provided"}), 400

        for file in files:
            if file.filename == '': continue
            file_path = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(file_path)
            content = llm_service.extract_text_from_pdf(file_path)
            books_content[file.filename] = content[:3000]

    elif source_type == "database":
        # New Logic: Get DB Details from the request
        db_config = {
            "type": request.form.get("db_type"),     # 'mysql' or 'postgres'
            "host": request.form.get("host"),
            "port": request.form.get("port"),
            "username": request.form.get("username"),
            "password": request.form.get("password"),
            "database": request.form.get("database")
        }
        query = request.form.get("query", "SELECT * LIMIT 20")
        
        # Fetch DB data and treat it like a "Book"
        db_text = get_external_db_content(db_config, query)
        books_content[f"DB_{db_config['database']}"] = db_text[:5000]

    # 5. Unified Analysis (LLM)
    analysis = llm_service.get_topics_and_connections(books_content)
    
    books_data_raw = analysis.get("books", [])
    common_topics = [t.strip() for t in analysis.get("common_topics", []) if isinstance(t, str)]
    
    # Fallback Logic for Book Names
    original_filenames = list(books_content.keys())
    books_data = []
    for entry in books_data_raw:
        llm_name = entry.get("name", "")
        if llm_name in original_filenames:
            books_data.append(entry)
            original_filenames.remove(llm_name)
        else:
            if original_filenames:
                entry["name"] = original_filenames.pop(0)
                books_data.append(entry)

    if not books_data:
        return jsonify({
            "error": "Failed to extract any topics or connections from the uploaded file(s). Please check the AI API logs or ensure the file contains readable text."
        }), 400

    # 6. Ingestion into ArangoDB
    try:
        local_db_service.insert_document('Metadata', {"_key": unique_session_id, "results": analysis})
    except Exception as e:
        print(f"DEBUG: Metadata insert failed: {e}")

    added_nodes = set() # Track nodes for Graph

    for book in books_data:
        name = book.get("name")
        topics = book.get("topics", [])
        book_key = safe_key(name)
        
        # Insert Book
        try:
            local_db_service.insert_document('Books', {"_key": book_key, "name": name})
        except Exception: pass
        
        # Insert Topics & Edges
        for topic in topics:
            if not isinstance(topic, str): continue
            topic_clean = topic.strip()
            topic_key = safe_key(topic_clean)
            try:
                local_db_service.insert_document('Topics', {"_key": topic_key, "name": topic_clean})
                local_db_service.insert_edge('has_topic', f"Books/{book_key}", f"Topics/{topic_key}")
            except Exception: pass

  # ==========================================
    # ==========================================
    # 7. Graph Generation (Dark Mode + HTML Overlay Index)
    # ==========================================
    # Initialize Network with a dark background
    net = Network(
        height='850px',
        width='100%',
        bgcolor='#222222',  # Dark background
        font_color='white', # White text
        notebook=False,
        directed=False
    )

    # Adjust physics
    net.barnes_hut(
        gravity=-8000,
        central_gravity=0.3,
        spring_length=150,
        spring_strength=0.05,
        damping=0.09,
        overlap=0
    )

    # --- Define Node Styles (Your Colors) ---
    # Books: Teal
    BOOK_STYLE = {
        'color': {'background': '#00bfa5', 'border': '#009688', 'highlight': '#64ffda'},
        'shape': 'dot',
        'size': 25, 
        'font': {'size': 16, 'color': 'white'}
    }
    
    # Topics: Pink
    TOPIC_STYLE = {
        'color': {'background': '#ff4081', 'border': '#c51162', 'highlight': '#ff80ab'},
        'shape': 'dot',
        'size': 12, 
        'font': {'size': 12, 'color': '#eeeeee'} 
    }

    # Common Topics: Blue
    COMMON_TOPIC_STYLE = {
        'color': {'background': '#2979ff', 'border': '#2962ff', 'highlight': '#82b1ff'},
        'shape': 'dot',
        'size': 35, 
        'font': {'size': 18, 'color': 'white', 'bold': True}
    }
    
    EDGE_COLOR = '#aaaaaa' 

    # --- Indexing Helper ---
    node_index_map = {}
    current_index = [1]

    def get_indexed_label(text):
        """Returns the text prefixed with [N]."""
        if text not in node_index_map:
            node_index_map[text] = current_index[0]
            current_index[0] += 1
        return text

    added_nodes = set()
    
    # (Removed the "net.add_node" legend section to avoid the "giant ball" issue)

    # --- Add Real Nodes and Edges ---
    for book in books_data:
        book_name = book.get("name")
        topics = book.get("topics", [])
        
        # Add Book Node
        if book_name not in added_nodes:
            display_label = get_indexed_label(book_name)
            net.add_node(book_name, label=display_label, **BOOK_STYLE)
            added_nodes.add(book_name)
        
        # Add Topic Nodes & Edges
        for topic in topics:
            if not isinstance(topic, str): continue
            t_clean = topic.strip()
            
            if t_clean not in added_nodes:
                display_label = get_indexed_label(t_clean)
                net.add_node(t_clean, label=display_label, **TOPIC_STYLE)
                added_nodes.add(t_clean)
            
            # Link Book -> Topic
            net.add_edge(book_name, t_clean, color=EDGE_COLOR, width=1)

    # --- Add Common Topic Bridges ---
    for ct in common_topics:
        ct_label = ct.strip()
        display_label = get_indexed_label(ct_label)
        
        # Re-add/Update the node with the prominent "Common Topic" style
        net.add_node(ct_label, label=display_label, **COMMON_TOPIC_STYLE)
        added_nodes.add(ct_label)

        # Connect Common Topic to ALL books
        for book in books_data:
            net.add_edge(book.get("name"), ct_label, color=EDGE_COLOR, width=2, dashed=True)
    # ==========================================
    # Build Index List HTML (Number → Name)
    # ==========================================

    sorted_index_items = sorted(
        node_index_map.items(),
        key=lambda x: x[1]
    )

    index_rows_html = ""
    for name, idx in sorted_index_items:
        safe_name = name.replace("<", "&lt;").replace(">", "&gt;")
        index_rows_html += f"""
            <div class="index-row">
                <span class="index-number">[{idx}]</span>
                <span class="index-text">{safe_name}</span>
            </div>
        """

    # 1. Save the basic graph first
    html_filename = f"graph_{unique_session_id}.html"
    html_path = os.path.join(GRAPH_FOLDER, html_filename)
    net.save_graph(html_path)

    # ==========================================
    # 8. Post-Process: Inject HTML Overlay Legend
    # ==========================================
    # This manually adds the white "Index" box to the generated HTML file
    
    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # Define the HTML/CSS for the legend using YOUR specific colors
    legend_html = f"""
    <style>
        .legend-container {{
            position: absolute;
            top: 20px;
            left: 20px;
            background-color: rgba(255, 255, 255, 0.95); /* White background */
            padding: 14px;
            border-radius: 8px;
            font-family: Arial, sans-serif;
            box-shadow: 0px 0px 15px rgba(0,0,0,0.4);
            z-index: 1000;
            min-width: 180px;
        }}
        .legend-header {{
            font-size: 16px;
            font-weight: bold;
            color: #333;
            margin-bottom: 8px;
            border-bottom: 1px solid #ccc;
            padding-bottom: 4px;
        }}
        .legend-row {{
            display: flex;
            align-items: center;
            margin-bottom: 6px;
            font-size: 14px;
            color: #333;
        }}
        .legend-dot {{
            height: 14px;
            width: 14px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 8px;
            border: 1px solid #888;
        }}
     /* ================= INDEX PANEL ================= */

        .index-container {{
            top: 20px;
            position: absolute;
            right: 20px;
            background-color: rgba(255,255,255,0.95);
            padding: 14px;
            border-radius: 8px;
            width: 260px;
            max-height: 70vh;
            overflow-y: auto;
            font-family: Arial, sans-serif;
            box-shadow: 0 0 15px rgba(0,0,0,0.4);
            z-index: 1000;
        }}

        .index-title {{
            font-size: 16px;
            font-weight: bold;
            margin-bottom: 8px;
            border-bottom: 1px solid #ccc;
            padding-bottom: 4px;
        }}

        .index-row {{
            display: flex;
            align-items: flex-start;
            margin-bottom: 6px;
            font-size: 13px;
            color: #222;
        }}

        .index-number {{
            font-weight: bold;
            width: 36px;
            flex-shrink: 0;
        }}

        .index-text {{
        word-break: break-word;
        }}    
    </style>
    <!-- Legend -->
    <div class="legend-container">
        <div class="legend-header">Index</div>
        <div class="legend-row">
            <span class="legend-dot" style="background-color: #00bfa5;"></span> 
            <b>Books</b>
        </div>
        <div class="legend-row">
            <span class="legend-dot" style="background-color: #ff4081;"></span> 
            Topics
        </div>
        <div class="legend-row">
            <span class="legend-dot" style="background-color: #2979ff;"></span> 
            Common Topics
        </div>
    </div>
    <!-- Index -->
    <div class="index-container">
        <div class="index-title">Index</div>
        {index_rows_html}
    </div>
    """

    # Inject the legend immediately after the <body> tag
    if "<body>" in html_content:
        final_html = html_content.replace("<body>", f"<body>{legend_html}")
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(final_html)

    # Save Graph File
    html_filename = f"graph_{unique_session_id}.html"
    html_path = os.path.join(GRAPH_FOLDER, html_filename)
    net.save_graph(html_path)

    # 8. Save Graph URL to MySQL
    graph_url = f"{BASE_URL}/graphs/{html_filename}"
    
    try:
        conn_db = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn_db.cursor()
        sql = """
            INSERT INTO graph (session_name, graph_url)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE graph_url = VALUES(graph_url)
        """
        # Using the session_name captured at step 2
        cursor.execute(sql, (session_name, graph_url))
        conn_db.commit()
        cursor.close()
        conn_db.close()
        print(f"DEBUG: MySQL graph metadata inserted for session: {session_name}")
    except Exception as e:
        print(f"DEBUG: MySQL graph update failed (ignoring): {e}")

    return jsonify({
        "status": "success",
        "ID": unique_session_id,
        "session_name": session_name,
        "graph_url": graph_url,
        "analysis_output": analysis
    })

