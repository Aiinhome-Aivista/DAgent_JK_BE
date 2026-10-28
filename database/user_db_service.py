import pymysql
from database.config import MYSQL_CONFIG
from helper.workspace_wise_store_procedures import run_stored_procedures



def create_user_database(email):

    username = email.split("@")[0]   
    db_name = f"user_{username}_db"

    # Connect without selecting specific database
    connection = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )

    try:
        with connection.cursor() as cursor:

            # Check if database already exists
            cursor.execute("SHOW DATABASES LIKE %s", (db_name,))
            result = cursor.fetchone()

            if result:
                return {
                    "status": "exists",
                    "message": "Database already exists for this user",
                    "db_name": db_name
                }

            # Create database if not exists
            cursor.execute(f"CREATE DATABASE `{db_name}`")
            
            try:
                # run_stored_procedures(db_name)
                pass
            except Exception as e:
                print(f"Failed to create stored procedures for {db_name}: {e}")

            return {
                "status": "created",
                "message": "User database created successfully",
                "db_name": db_name
            }

    finally:
        connection.close()


def create_workspace_arango_database(workspace_name):
    import re
    from arango import ArangoClient
    from arango.exceptions import CollectionCreateError
    from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS
    
    # Create a safe, lower-case identifier for the database
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', workspace_name).lower()
    # Trim to avoid extremely long DB names
    safe_name = safe_name[:40]
    arango_db_name = f"{safe_name}_arango_db"
    
    client = ArangoClient(hosts=ARANGO_HOST)
    sys_db = client.db('_system', username=ARANGO_USER, password=ARANGO_PASS)
    
    status = "created"
    if not sys_db.has_database(arango_db_name):
        sys_db.create_database(arango_db_name)
    else:
        status = "exists"
        
    db = client.db(arango_db_name, username=ARANGO_USER, password=ARANGO_PASS)
    
    # Document collections
    for coll in ['Books', 'Topics', 'Metadata']:
        try:
            if not db.has_collection(coll):
                db.create_collection(coll)
        except CollectionCreateError:
            pass
            
    # Edge collection
    try:
        if not db.has_collection('has_topic'):
            db.create_collection('has_topic', edge=True)
    except CollectionCreateError:
        pass
        
    return {
        "status": status,
        "arango_db_name": arango_db_name
    }


def create_workspace_database(workspace_id, workspace_name):
    import re
    # Create a safe, lower-case identifier for the database
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', workspace_name).lower()
    # Trim to avoid extremely long DB names
    safe_name = safe_name[:40]
    db_name = f"{safe_name}_db"

    # Connect without selecting specific database
    connection = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )

    try:
        with connection.cursor() as cursor:
            # Check if database already exists
            cursor.execute("SHOW DATABASES LIKE %s", (db_name,))
            result = cursor.fetchone()

            if result:
                return {
                    "status": "exists",
                    "message": "Database already exists for this workspace",
                    "db_name": db_name
                }

            # Create database if not exists
            cursor.execute(f"CREATE DATABASE `{db_name}`")
            
            try:
                # run_stored_procedures(db_name)
                pass
            except Exception as e:
                print(f"Failed to create stored procedures for {db_name}: {e}")

            return {
                "status": "created",
                "message": "Workspace database created successfully",
                "db_name": db_name
            }

    finally:
        connection.close()