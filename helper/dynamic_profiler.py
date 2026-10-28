def get_table_aggregates(cursor, table_name, max_categorical_values=5):
    """
    Dynamically generates statistical aggregates for all columns in a table
    without hardcoding column names or limits.
    """
    try:
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        columns_info = cursor.fetchall()
    except Exception as e:
        print(f"[DynamicProfiler] Error fetching columns for {table_name}: {e}")
        return []

    numeric_types = ['int', 'decimal', 'float', 'double', 'numeric', 'bigint', 'smallint', 'tinyint']
    date_types = ['date', 'datetime', 'timestamp']
    
    aggregates = []
    
    try:
        # Get total row count
        cursor.execute(f"SELECT COUNT(*) as total_rows FROM `{table_name}`")
        total_rows = cursor.fetchone().get('total_rows', 0)
        aggregates.append(f"Total rows in table: {total_rows}")
        
        if total_rows == 0:
            return aggregates
            
        for col_info in columns_info:
            col_name = col_info['Field']
            col_type = col_info['Type'].lower()
            
            is_numeric = any(nt in col_type for nt in numeric_types)
            is_date = any(dt in col_type for dt in date_types)
            
            if is_numeric:
                cursor.execute(f"SELECT SUM(`{col_name}`) as sum_val, MIN(`{col_name}`) as min_val, MAX(`{col_name}`) as max_val, AVG(`{col_name}`) as avg_val FROM `{table_name}`")
                res = cursor.fetchone()
                if res and res['sum_val'] is not None:
                    aggregates.append(f"Numeric Column '{col_name}' -> SUM: {res['sum_val']}, MIN: {res['min_val']}, MAX: {res['max_val']}, AVG: {round(res['avg_val'], 2) if res['avg_val'] else None}")
            elif is_date:
                cursor.execute(f"SELECT MIN(`{col_name}`) as min_date, MAX(`{col_name}`) as max_date FROM `{table_name}`")
                res = cursor.fetchone()
                if res and res['min_date'] is not None:
                    aggregates.append(f"Date Column '{col_name}' -> Date Range: {res['min_date']} to {res['max_date']}")
            else:
                # Categorical
                cursor.execute(f"SELECT COUNT(DISTINCT `{col_name}`) as unique_count FROM `{table_name}`")
                res = cursor.fetchone()
                unique_count = res['unique_count'] if res else 0
                
                # Get top N frequent values
                cursor.execute(f"SELECT `{col_name}`, COUNT(*) as freq FROM `{table_name}` GROUP BY `{col_name}` ORDER BY freq DESC LIMIT %s", (max_categorical_values,))
                top_vals = cursor.fetchall()
                top_vals_str = ", ".join([f"{r[col_name]} ({r['freq']})" for r in top_vals if r.get(col_name) is not None])
                
                aggregates.append(f"Categorical Column '{col_name}' -> Unique values: {unique_count}, Top Values by Frequency: [{top_vals_str}]")
    except Exception as e:
        print(f"[DynamicProfiler] Error profiling table {table_name}: {e}")
        
    return aggregates
