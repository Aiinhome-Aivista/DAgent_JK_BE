import re

def format_db_error(error_obj, source_name="the file"):
    """
    Parses a raw database or system exception and returns a human-readable smart error message.
    """
    error_str = str(error_obj).strip()
    error_lower = error_str.lower()
    
    # Common MySQL / MariaDB Errors
    if '1110' in error_str or 'specified twice' in error_lower:
        match = re.search(r"'([^']+)'", error_str)
        col_name = match.group(1) if match else "a column"
        return f"Import failed for '{source_name}'. We found a duplicate column name ({col_name}) inside the data. Databases require each column to have a unique name. Please rename the duplicate columns and upload again."
        
    if '1064' in error_str or 'syntax error' in error_lower:
        return f"Import failed for '{source_name}'. There seems to be a syntax issue or unsupported characters in the data. Please check the formatting."
        
    if '1406' in error_str or 'data too long' in error_lower:
        match = re.search(r"'([^']+)'", error_str)
        col_name = match.group(1) if match else "a column"
        return f"Import failed for '{source_name}'. The data in column '{col_name}' is too long to be stored in the database. Please check your data limits."
        
    if '1045' in error_str or 'access denied' in error_lower:
        return f"Database connection refused for '{source_name}'. Please check if your username and password are correct."
        
    if '2002' in error_str or 'connection refused' in error_lower:
        return f"Could not connect to the database host for '{source_name}'. Please ensure the database server is running and accessible."
        
    if '1049' in error_str or 'unknown database' in error_lower:
        return f"The specified database does not exist for '{source_name}'. Please check the database name."
        
    if '1146' in error_str or 'doesn\'t exist' in error_lower:
        match = re.search(r"'([^']+)'", error_str)
        table_name = match.group(1) if match else "a table"
        return f"The table '{table_name}' does not exist. Please check your schema."

    # Default to original error string
    return error_str
