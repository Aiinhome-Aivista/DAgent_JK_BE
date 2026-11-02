"""
System Configuration Service
Manages system-level configuration parameters in MySQL (such as csv_export_threshold)
allowing real-time updates from Admin without server restarts.
"""
import re

def ensure_system_configurations_table(conn):
    """Ensures system_configurations table exists in MySQL metadata database."""
    try:
        cur = conn.cursor()
        create_sql = """
        CREATE TABLE IF NOT EXISTS `system_configurations` (
            `setting_key` VARCHAR(100) PRIMARY KEY,
            `setting_value` VARCHAR(255) NOT NULL,
            `description` VARCHAR(255),
            `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        );
        """
        cur.execute(create_sql)
        
        # Seed default threshold if not present
        seed_sql = """
        INSERT INTO `system_configurations` (`setting_key`, `setting_value`, `description`)
        VALUES ('csv_export_threshold', '30', 'RAG Chat CSV Export Row Threshold')
        ON DUPLICATE KEY UPDATE `description` = VALUES(`description`);
        """
        cur.execute(seed_sql)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[SystemConfig] Error ensuring system_configurations table: {e}", flush=True)

def get_system_config(conn, key, default=30):
    """Gets a configuration setting value. Returns int if convertible, else string."""
    try:
        ensure_system_configurations_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT `setting_value` FROM `system_configurations` WHERE `setting_key` = %s", (key,))
        row = cur.fetchone()
        cur.close()
        if row and row[0] is not None:
            val_str = str(row[0]).strip()
            if val_str.isdigit():
                return int(val_str)
            return val_str
    except Exception as e:
        print(f"[SystemConfig] Error reading key '{key}': {e}", flush=True)
    return default

def set_system_config(conn, key, value, description=""):
    """Sets or updates a configuration setting value."""
    try:
        ensure_system_configurations_table(conn)
        cur = conn.cursor()
        sql = """
        INSERT INTO `system_configurations` (`setting_key`, `setting_value`, `description`)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE `setting_value` = VALUES(`setting_value`), `updated_at` = CURRENT_TIMESTAMP;
        """
        cur.execute(sql, (key, str(value), description))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[SystemConfig] Error setting key '{key}': {e}", flush=True)
        return False

def get_all_system_configs(conn):
    """Returns a dictionary of all configuration settings."""
    configs = {}
    try:
        ensure_system_configurations_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT `setting_key`, `setting_value`, `description`, `updated_at` FROM `system_configurations`")
        for row in cur.fetchall():
            k = row[0]
            v = row[1]
            configs[k] = int(v) if str(v).isdigit() else v
        cur.close()
    except Exception as e:
        print(f"[SystemConfig] Error fetching all configs: {e}", flush=True)
    if "csv_export_threshold" not in configs:
        configs["csv_export_threshold"] = 30
    return configs

def generate_relevant_filename(question):
    """
    Generates a clean, slugified filename relevant to the user question.
    Strips out command phrases like 'write query', 'sql', 'list', etc.
    """
    from datetime import datetime
    stop_words = {
        'what', 'is', 'the', 'of', 'for', 'show', 'give', 'me', 'all', 'list',
        'in', 'and', 'to', 'a', 'an', 'please', 'can', 'you', 'how', 'many',
        'much', 'from', 'with', 'by', 'at', 'on', 'tell', 'get',
        'write', 'query', 'queries', 'sql', 'run', 'find', 'fetch', 'select',
        'generate', 'their', 'our', 'my', 'his', 'her', 'its', 'them',
        'this', 'that', 'these', 'those', 'data', 'details', 'report', 'as'
    }
    raw = re.sub(r'[^a-zA-Z0-9]+', ' ', str(question or '').strip().lower())
    words = [w for w in raw.split() if w and w not in stop_words and len(w) > 1]
    slug = "_".join(words[:5]) if words else "export_data"
    slug = re.sub(r'^(write_query_|query_|sql_)+', '', slug)
    if not slug:
        slug = "export_data"
    date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f"{slug}_{date_str}.csv"
