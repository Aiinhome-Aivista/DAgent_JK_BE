import requests
import logging
import re
import xml.etree.ElementTree as ET
import json
import pymysql

logger = logging.getLogger(__name__)

class TallyService:
    def __init__(self, host='43.kcloud.in', port=43087, protocol="http", timeout=60):
        self.host = host
        self.port = int(port) if port else 9000
        self.protocol = protocol
        self.timeout = timeout

    @property
    def url(self):
        return f"{self.protocol}://{self.host}:{self.port}"

    def send_xml(self, xml_data):
        try:
            logger.info(f"Connecting to Tally: {self.url}")
            response = requests.post(
                self.url,
                data=xml_data.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=self.timeout
            )
            return {
                "success": response.ok,
                "status_code": response.status_code,
                "response": response.text
            }
        except Exception as e:
            logger.exception("Unexpected error")
            return {"success": False, "error": str(e)}


def clean_tally_xml(xml_data):
    # Remove invalid numeric XML character references
    xml_data = re.sub(r'&#(?:[0-8]|1[0-9]|2[0-9]|3[01]);', '', xml_data)
    # Remove invalid XML control characters
    xml_data = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', xml_data)
    return xml_data


def xml_to_dict(element):
    children = list(element)
    if not children:
        text = element.text.strip() if element.text else ""
        if element.attrib:
            return {"@attributes": dict(element.attrib), "#text": text}
        return text

    result_dict = {}
    if element.attrib:
        result_dict["@attributes"] = dict(element.attrib)
        
    for child in children:
        tag = child.tag
        child_data = xml_to_dict(child)
        if tag not in result_dict:
            result_dict[tag] = child_data
        else:
            if not isinstance(result_dict[tag], list):
                result_dict[tag] = [result_dict[tag]]
            result_dict[tag].append(child_data)
            
    return result_dict


def fetch_tally_data(host, port, company_name, collection_name="Ledger"):

    tally = TallyService(host=host, port=port)
    
    xml_request = f"""
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>{collection_name}</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>XML</SVEXPORTFORMAT>
                    <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </DESC>
        </BODY>
    </ENVELOPE>
    """
    
    result = tally.send_xml(xml_request)
    
    if not result.get("success"):
        raise Exception(result.get("error", "Failed to connect to Tally XML API"))
        
    xml_response = result.get("response")
    cleaned_xml = clean_tally_xml(xml_response)
    
    try:
        root = ET.fromstring(cleaned_xml)
        data_dict = xml_to_dict(root)
        return {root.tag: data_dict}
    except Exception as e:
        raise Exception(f"Failed to parse Tally XML: {str(e)}")


def flatten_dict(d, parent_key='', sep='_'):
    items = []
    if isinstance(d, dict):
        for k, v in d.items():
            if k == "@attributes":
                # Dynamically extract all attributes as simple columns
                for attr_k, attr_v in v.items():
                    if attr_k.lower() == "type":
                        continue
                    items.append((attr_k, attr_v))
                continue
            
            # Completely skip any key named 'type'
            if k.lower() == "type":
                continue
            
            # Use parent name only if this is just the text value node
            if k == "_text" or k == "#text":
                new_key = parent_key if parent_key else "text"
            else:
                new_key = k
                
            if isinstance(v, dict):
                items.extend(flatten_dict(v, new_key, sep=sep).items())
            elif isinstance(v, list):
                items.append((new_key, json.dumps(v)))
            else:
                items.append((new_key, v))
    else:
        items.append((parent_key, d))
    return dict(items)

def sync_tally_database(user_id, connection_id, session_id, external_db, user_db_name, username):
    """
    This function will replace the pyodbc logic inside `sync_external_database` for Tally.
    """
    import pymysql
    from database.config import MYSQL_CONFIG
    import pandas as pd
    from helper.global_helper import map_dtype
    import re
    import json

    host = external_db.get("host")
    port = external_db.get("port")
    company_name = external_db.get("database") 
    
    print(f"[*] Starting Tally Sync for company: {company_name}")

    try:
        ledger_data = fetch_tally_data(host, port, company_name, "Ledger")
        print("[*] Successfully fetched Ledger data from Tally API.")
    except Exception as e:
        return {
            "summary": {"total_rows": 0, "total_columns": 0, "data_size_mb": 0.0, "last_sync": "Failed"},
            "tables": [],
            "situations": [{"type": "FAILED_ATTEMPT", "message": f"Tally API Error: {str(e)}", "buttons": ["Retry"]}],
            "new_tables": []
        }

    collection = ledger_data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    ledgers = collection.get("LEDGER", [])
    if isinstance(ledgers, dict):
        ledgers = [ledgers]
        
    if not ledgers:
        return {
            "summary": {"total_rows": 0, "total_columns": 0, "data_size_mb": 0.0, "last_sync": "Just now"},
            "tables": [],
            "situations": [],
            "new_tables": []
        }

    # STEP 1: Flatten Data
    flattened_ledgers = []
    for row in ledgers:
        if not isinstance(row, dict):
            continue
        flattened_row = flatten_dict(row)
        flattened_ledgers.append(flattened_row)

    # STEP 2: Collect Columns Dynamically
    original_columns = set()
    for row in flattened_ledgers:
        if isinstance(row, dict):
            original_columns.update(row.keys())

    original_columns = sorted(list(original_columns))
    
    col_mapping = {}
    for orig in original_columns:
        column_name = re.sub(r"[^a-zA-Z0-9_]", "_", orig.lower())
        if column_name and column_name[0].isdigit():
            column_name = f"col_{column_name}"
        column_name = column_name[:60]
        col_mapping[orig] = column_name

    all_columns_lower = [col_mapping[orig] for orig in original_columns]

    # STEP 3: Table Info
    table_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"{company_name}_ledger").lower()
    row_count = len(flattened_ledgers)
    col_count = len(all_columns_lower)

    print(f"[*] Preparing to insert {row_count} rows and {col_count} columns into MySQL table: {table_name}")

    # STEP 4: Connect MySQL
    target_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=user_db_name,
        autocommit=True
    )

    try:
        with target_conn.cursor() as target_cursor:
            
            # STEP 5: Data Types
            col_defs = []
            col_types = {}
            for orig_col in original_columns:
                mysql_col = col_mapping[orig_col]
                col_vals = []
                for row in flattened_ledgers:
                    val = row.get(orig_col)
                    if val is not None and val != "":
                        col_vals.append(val)
                
                if not col_vals:
                    sql_type = "TEXT"
                else:
                    series = pd.Series(col_vals).replace(r"^\s*$", None, regex=True).dropna()
                    if series.empty:
                        sql_type = "TEXT"
                    else:
                        sql_type = map_dtype(series)
                        if sql_type in ["VARCHAR", "NVARCHAR", "TEXT"]:
                            max_len = series.astype(str).str.len().max()
                            if max_len <= 255:
                                sql_type = "VARCHAR(255)"
                            elif max_len <= 65535:
                                sql_type = "TEXT"
                            else:
                                sql_type = "LONGTEXT"
                
                col_defs.append(f"`{mysql_col}` {sql_type}")
                col_types[orig_col] = sql_type
                
            # STEP 6: CREATE TABLE
            create_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n" + ",\n".join(col_defs) + "\n)"
            target_cursor.execute(create_query)
            
            # STEP 7: CLEAR OLD DATA
            target_cursor.execute(f"TRUNCATE TABLE `{table_name}`")
            
            # STEP 8: BUILD INSERT QUERY
            placeholders = ", ".join(["%s"] * len(all_columns_lower))
            columns_sql = ", ".join(f"`{col}`" for col in all_columns_lower)
            insert_query = f"INSERT INTO `{table_name}` ({columns_sql}) VALUES ({placeholders})"
            
            # STEP 9: PREPARE DATA
            insert_data = []
            for row in flattened_ledgers:
                row_values = []
                for orig_col in original_columns:
                    value = row.get(orig_col)
                    
                    if value is None or value == "":
                        value = None
                    elif isinstance(value, bool):
                        value = int(value)
                    elif isinstance(value, (int, float)):
                        value = value
                    else:
                        value = str(value).strip()
                        if col_types.get(orig_col) in ["DATE", "DATETIME"]:
                            try:
                                dt = pd.to_datetime(value)
                                if col_types[orig_col] == "DATETIME":
                                    value = dt.strftime("%Y-%m-%d %H:%M:%S")
                                else:
                                    value = dt.strftime("%Y-%m-%d")
                            except Exception:
                                pass
                    row_values.append(value)
                insert_data.append(row_values)
            
            # STEP 10: INSERT
            if insert_data:
                target_cursor.executemany(insert_query, insert_data)
                print(f"[*] Successfully inserted {len(insert_data)} rows into `{table_name}`")
                
                # LOG
                log_conn = pymysql.connect(
                    host=MYSQL_CONFIG["host"],
                    user=MYSQL_CONFIG["user"],
                    password=MYSQL_CONFIG["password"],
                    database=MYSQL_CONFIG["database"],
                    autocommit=True
                )
                with log_conn.cursor() as log_cursor:
                    log_cursor.execute(f"""
                        INSERT INTO `{MYSQL_CONFIG["database"]}`.external_db_sync_log
                        (user_id, new_user_db, username, external_database, table_name, action_type, rows_affected, session_id, data_size_mb, exact_size_mb)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        user_id, user_db_name, username, company_name, table_name, "NEW_TABLE", len(insert_data), session_id,
                        (len(json.dumps(flattened_ledgers).encode('utf-8')) / (1024 * 1024)),
                        (len(json.dumps(flattened_ledgers).encode('utf-8')) / (1024 * 1024))
                    ))
                log_conn.close()

    except Exception as e:
        print(f"[!] Error saving data to MySQL: {e}")
        return {
            "summary": {"total_rows": 0, "total_columns": 0, "data_size_mb": 0.0, "last_sync": "Failed"},
            "tables": [],
            "situations": [{"type": "FAILED_ATTEMPT", "message": f"MySQL Insertion Error: {str(e)}", "buttons": ["Retry"]}],
            "new_tables": []
        }
    finally:
        target_conn.close()

    exact_size_mb = len(json.dumps(flattened_ledgers).encode('utf-8')) / (1024 * 1024)
    data_size_mb = exact_size_mb
    return {
        "summary": {
            "total_rows": row_count,
            "total_columns": col_count,
            "data_size_mb": data_size_mb,
            "last_sync": "Just now"
        },
        "tables": [{"table": table_name, "rows": row_count, "columns": col_count}],
        "situations": [],
        "new_tables": [table_name]
    }
