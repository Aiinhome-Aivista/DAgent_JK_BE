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


def sync_tally_database(user_id, connection_id, session_id, external_db, user_db_name, username):
    """
    This function will replace the pyodbc logic inside `sync_external_database` for Tally.
    """
    import pymysql
    from database.config import MYSQL_CONFIG

    host = external_db.get("host")
    port = external_db.get("port")
    company_name = external_db.get("database") # Usually passed in the database field for Tally
    
    print(f"[*] Starting Tally Sync for company: {company_name}")

    # 1. Fetch data from Tally via XML API
    try:
        ledger_data = fetch_tally_data(host, port, company_name, "Ledger")
        print("[*] Successfully fetched Ledger data from Tally API.")
    except Exception as e:
        print(f"[!] Error fetching from Tally API: {e}")
        return {
            "summary": {"total_rows": 0, "total_columns": 0, "data_size_mb": 0.0, "last_sync": "Failed"},
            "tables": [],
            "situations": [{"type": "FAILED_ATTEMPT", "message": f"Tally API Error: {str(e)}", "buttons": ["Retry"]}],
            "new_tables": []
        }

    # 2. Extract rows
    collection = ledger_data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    ledgers = collection.get("LEDGER", [])
    if isinstance(ledgers, dict):
        ledgers = [ledgers] # Ensure it's a list
        
    if not ledgers:
        print("[!] No Ledgers found in the Tally response.")
        return {
            "summary": {"total_rows": 0, "total_columns": 0, "data_size_mb": 0.0, "last_sync": "Just now"},
            "tables": [],
            "situations": [],
            "new_tables": []
        }

    # 3. Dynamic columns extraction
    all_columns = set()
    for row in ledgers:
        if isinstance(row, dict):
            all_columns.update(row.keys())
    
    if "@attributes" in all_columns:
        all_columns.remove("@attributes")
    all_columns = list(all_columns)

    table_name = f"{company_name}_Ledger".lower()
    row_count = len(ledgers)
    col_count = len(all_columns)
    
    print(f"[*] Preparing to insert {row_count} rows into MySQL table: {table_name}")

    target_conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=user_db_name,
        autocommit=True
    )

    try:
        with target_conn.cursor() as target_cursor:
            col_defs = [f"`{col}` LONGTEXT" for col in all_columns]
            create_query = f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n  " + ",\n  ".join(col_defs) + "\n)"
            target_cursor.execute(create_query)
            
            target_cursor.execute(f"TRUNCATE TABLE `{table_name}`")
            
            placeholders = ", ".join(["%s"] * len(all_columns))
            insert_query = f"INSERT INTO `{table_name}` (`{'`, `'.join(all_columns)}`) VALUES ({placeholders})"
            
            insert_data = []
            for row in ledgers:
                if not isinstance(row, dict):
                    continue
                row_values = []
                for col in all_columns:
                    val = row.get(col)
                    if isinstance(val, (dict, list)):
                        val = json.dumps(val)
                    elif val is None:
                        val = ""
                    else:
                        val = str(val)
                    row_values.append(val)
                insert_data.append(row_values)
            
            if insert_data:
                target_cursor.executemany(insert_query, insert_data)
                print(f"[*] Successfully inserted {len(insert_data)} rows into `{table_name}`")
                
                # Log insert to external_db_sync_log
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
                        (user_id, new_user_db, username, external_database, table_name, action_type, rows_affected, session_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        user_id, user_db_name, username, company_name, table_name, "NEW_TABLE", len(insert_data), session_id
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

    data_size_mb = round((row_count * col_count * 8) / (1024 * 1024), 2)
    
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
