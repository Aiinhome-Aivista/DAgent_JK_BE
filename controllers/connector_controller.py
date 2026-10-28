from flask import request, jsonify
from sqlalchemy import create_engine, text
import pandas as pd

# This stores active engines in memory for the agent to use
# In production, you might fetch these from your MySQL metadata table
active_connectors = {}

def create_connector_controller():
    data = request.json
    conn_name = data.get('name')
    db_type = data.get('type', 'mysql') # Default to mysql
    
    # Construct URI based on your screenshot fields
    driver = "mysql+pymysql" if db_type == 'mysql' else "postgresql+psycopg2"
    uri = f"{driver}://{data['username']}:{data['password']}@{data['host']}:{data['port']}/{data['database']}"
    
    try:
        engine = create_engine(uri)
        # Test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        
        active_connectors[conn_name] = engine
        return jsonify({"status": "success", "message": f"Connected to {conn_name}"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

def agent_query_controller():
    data = request.json
    conn_name = data.get('name')
    query = data.get('query')
    
    if conn_name not in active_connectors:
        return jsonify({"error": "Connector not found"}), 404
        
    try:
        engine = active_connectors[conn_name]
        df = pd.read_sql(query, engine)
        return jsonify(df.to_dict(orient="records")), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400