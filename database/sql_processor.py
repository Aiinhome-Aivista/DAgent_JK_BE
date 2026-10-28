import re
import pymysql

def detect_sql_dialect(sql_content):
    sample = sql_content[:50000].upper()
    scores = {"mysql": 0, "mssql": 0, "postgresql": 0}

    if "MYSQLDUMP" in sample or "ENGINE=INNODB" in sample or "`" in sample: scores["mysql"] += 3
    if "[DBO]." in sample or "SET ANSI_NULLS ON" in sample or "\nGO\n" in sample: scores["mssql"] += 3
    if "PG_DUMP" in sample or "PUBLIC." in sample: scores["postgresql"] += 3

    best_match = max(scores, key=scores.get)
    return best_match if scores[best_match] > 0 else "unknown"

def parse_mysql_or_pg(sql_content):
    commands = sql_content.split(';')
    return [cmd.strip() for cmd in commands if cmd.strip() and not cmd.strip().startswith('--')]

def parse_mssql(sql_content):
    commands = re.split(r'(?i)^\s*GO\s*$', sql_content, flags=re.MULTILINE)
    return [cmd.strip() for cmd in commands if cmd.strip() and not cmd.strip().startswith('--')]

def process_sql_job(file_paths, allocated_db_name, db_host, db_user, db_pass, db_port,
                     trigger_source="sql_upload"):
    ext_conn = pymysql.connect(
        host=db_host, port=int(db_port),
        user=db_user, password=db_pass, database=allocated_db_name
    )
    ext_cursor = ext_conn.cursor()
    total_executed = 0
    try:
        for path in file_paths:
            print(f"\n🚀 Starting SQL processing for file: {path}")
            with open(path, 'r', encoding='utf-8') as f:
                sql_content = f.read()
            
            file_dialect = detect_sql_dialect(sql_content)
            if file_dialect == 'mssql':
                commands = parse_mssql(sql_content)
            else:
                commands = parse_mysql_or_pg(sql_content)
                
            for cmd in commands:
                ext_cursor.execute(cmd)
            ext_conn.commit()
            total_executed += len(commands)
            print(f"✅ Executed {len(commands)} commands from {path}")
    except Exception as e:
        print(f"Error executing SQL: {e}")
    finally:
        ext_cursor.close()
        ext_conn.close()
        
    print(f"\n🎉 SQL processing complete. Total executed: {total_executed}")
    try:
        from database.kgraph_builder import build_kgraph
        build_kgraph(allocated_db_name, db_host, db_user, db_pass, db_port,
                     trigger_source=trigger_source)
    except Exception as e:
        print(f"[KGRAPH] post-SQL build skipped: {e}")

    return True
