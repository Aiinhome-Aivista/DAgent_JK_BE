"""
csv_processor.py  —  hardened CSV → SQL ingester.

Why this rewrite exists
-----------------------
The previous version loaded every column with dtype=str, so all columns landed
in MySQL as VARCHAR. Monetary values such as "10,409.00" and "-1,518.78 INR"
were stored as text, and MySQL's SUM() on a text column coerces each string
left-to-right and STOPS at the first non-digit (the comma) — so
SUM(invoice_value) returned ~0.4% of the true total. Every ranking/aggregation
was therefore wrong and unstable.

This version:
  * detects each column's real type (numeric / integer / date / text),
  * cleans thousands separators, currency tokens (INR, RS, ₹, $, …) and
    parenthesis-negatives BEFORE casting,
  * creates the SQL table with explicit DECIMAL / BIGINT / DATE / TEXT types,
  * never silently drops rows (it reconciles row counts and warns loudly).

Drop-in replacement: same function name and signature, same return value.
"""

import re
import numpy as np
import pandas as pd
from urllib.parse import quote_plus
from sqlalchemy import create_engine
from sqlalchemy.types import DECIMAL, BigInteger, Date, Text
from database.schema_matcher import match_columns_to_existing

# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────
CHUNK_SIZE          = 300_000
SAMPLE_ROWS         = 50_000          # rows used to infer column types
NUMERIC_THRESHOLD   = 0.99            # ≥99% of non-blank values parse as number (strict to protect text data)
DATE_THRESHOLD      = 0.80            # ≥80% of non-blank values parse as a date
MONEY_PRECISION     = 30              # DECIMAL(precision, scale) for numeric cols
MONEY_SCALE         = 8

# Strip these currency / unit tokens before numeric parsing.
CURRENCY_RE = re.compile(r'(?i)\b(?:INR|RS|USD|EUR|GBP|AUD|CAD|JPY)\b|[₹$€£]')

# Date formats tried in order; the one parsing the most sample values wins.
DATE_FORMATS = [
    '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%Y/%m/%d',
    '%d-%b-%Y', '%d %b %Y', '%d-%m-%y', '%m-%d-%Y',
]

# Columns whose values are pure digits but are really identifiers (so we must
# NOT turn them into integers, e.g. to preserve leading zeros). Matches a
# sanitized column name ending in an id-ish suffix, or an exact known key.
ID_SUFFIX_RE = re.compile(r'(?:_id|_code|_no|_number|_num|_pin|_zip|_gstin|_pan)$', re.I)
ID_EXACT     = {'invoice_number'}     # add others here if needed


def _sanitize(name: str) -> str:
    """Mirror the original column/table sanitization: alnum/underscore, lower."""
    return "".join(c if c.isalnum() else "_" for c in str(name)).lower()


def _looks_like_id(col: str) -> bool:
    return col in ID_EXACT or bool(ID_SUFFIX_RE.search(col))


def _clean_numeric(series: pd.Series) -> pd.Series:
    """Return a float Series; values that cannot be parsed become NaN.

    Handles: thousands commas, currency tokens/symbols, surrounding spaces,
    and accounting negatives written as (123).
    """
    x = series.astype(str).str.strip()
    x = x.str.replace(CURRENCY_RE, '', regex=True)
    # (123)  ->  -123
    x = x.str.replace(r'^\((.*)\)$', r'-\1', regex=True)
    x = x.str.replace(',', '', regex=False)
    x = x.str.replace(r'\s+', '', regex=True)
    x = x.replace({'': np.nan, 'nan': np.nan, 'none': np.nan,
                   'None': np.nan, '-': np.nan, 'null': np.nan, 'NULL': np.nan})
    return pd.to_numeric(x, errors='coerce')

def _best_date_format(series: pd.Series):
    """Return (format, parse_ratio) for the best-matching date format, or (None, 0)."""
    nonblank = series.dropna().astype(str).str.strip()
    nonblank = nonblank[(nonblank != '') & (nonblank.str.lower() != 'nan')]
    if nonblank.empty:
        return None, 0.0
    # Sample from UNIQUE values, not raw row order — a chronologically
    # sorted, dense file (many rows per day) can otherwise never surface
    # day-of-month values >12 in the first N raw rows, hiding the exact
    # signal needed to tell day-first from month-first formats apart.
    unique_vals = pd.Series(nonblank.unique())
    sample = unique_vals.head(2000)
    best_fmt, best_ratio = None, 0.0
    for fmt in DATE_FORMATS:
        ratio = pd.to_datetime(sample, format=fmt, errors='coerce').notna().mean()
        if ratio > best_ratio:
            best_fmt, best_ratio = fmt, float(ratio)
    return best_fmt, best_ratio

def _infer_schema(sample: pd.DataFrame) -> dict:
    """Map each (already-sanitized) column name -> dict(kind, fmt)."""
    schema = {}
    
    # [NEW] Deduplicate column names. If a CSV has "id" and "ID", pandas usually loads them
    # as "id" and "id.1", but our _sanitize lowercases them both to "id". When two columns
    # have the exact same name, sample[col] returns a DataFrame, not a Series, causing
    # AttributeError: 'DataFrame' object has no attribute 'str' during .str.strip().
    dedup_cols = []
    seen = {}
    for c in sample.columns:
        base = _sanitize(c)
        if base in seen:
            seen[base] += 1
            dedup_cols.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 0
            dedup_cols.append(base)
    sample.columns = dedup_cols

    for col in sample.columns:
        col_lower = col.lower()
        nonblank = sample[col].dropna().astype(str).str.strip()
        nonblank = nonblank[(nonblank != '') & (nonblank.str.lower() != 'nan')]

        if nonblank.empty:
            schema[col] = {'kind': 'text', 'fmt': None}
            continue
                # --------------------------------------------------
        # BUSINESS COLUMN OVERRIDES (HIGHEST PRIORITY)
        # --------------------------------------------------
        if "date" in col_lower:
            fmt, date_ratio = _best_date_format(nonblank)
            if date_ratio < DATE_THRESHOLD:
                fmt = None   # low confidence — fall back to the lenient
                             # generic dayfirst parser in _apply_schema
                             # instead of forcing a barely-matching format
            schema[col] = {"kind": "date", "fmt": fmt}
            continue

        INT_COLUMNS = {
            "qty",
            "quantity"
        }

        DECIMAL_COLUMNS = {
            "ndp",
            "qtd",
            "invoice_value",
            "taxable_value",
            "discount",
            "total_discount",
            "claim_discount",
            "ppd",
            "acer",
            "add_on",
            "ladder_discount",
            "cgst",
            "sgst",
            "utgst",
            "igst",
            "total_gst",
            "tcs",
            "round_off",
            "fraight"
        }

        if col_lower in INT_COLUMNS:
            schema[col] = {"kind": "int", "fmt": None}
            continue

        if col_lower in DECIMAL_COLUMNS:
            schema[col] = {"kind": "numeric", "fmt": None}
            continue    
        # 1) date?
        fmt, date_ratio = _best_date_format(nonblank)

        # 2) numeric?
        num = _clean_numeric(nonblank)
        num_ratio = float(num.notna().mean())

        if date_ratio >= DATE_THRESHOLD and date_ratio >= num_ratio:
            schema[col] = {'kind': 'date', 'fmt': fmt}
        elif num_ratio >= NUMERIC_THRESHOLD and not _looks_like_id(col):
            vals = num.dropna()
            is_int = (not vals.empty
                      and np.all(vals == np.floor(vals))
                      and vals.abs().max() < 9.0e18)
            schema[col] = {'kind': 'int' if is_int else 'numeric', 'fmt': None}
        else:
            schema[col] = {'kind': 'text', 'fmt': None}
    return schema


def _sqlalchemy_dtype(schema: dict) -> dict:
    out = {}
    for col, spec in schema.items():
        k = spec['kind']
        if k == 'numeric':
            out[col] = DECIMAL(MONEY_PRECISION, MONEY_SCALE)
        elif k == 'int':
            out[col] = BigInteger()
        elif k == 'date':
            out[col] = Date()
        else:
            out[col] = Text()
    return out


def _apply_schema(chunk: pd.DataFrame, schema: dict) -> pd.DataFrame:
    """Transform a raw (string) chunk into typed values according to schema."""
    for col, spec in schema.items():
        if col not in chunk.columns:
            continue
        k = spec['kind']
        if k in ('numeric', 'int'):
            chunk[col] = _clean_numeric(chunk[col])
            if k == 'int':
                # nullable integer so NaN survives as NULL
                chunk[col] = chunk[col].round().astype('Int64')
        elif k == 'date':
            # chunk[col] = pd.to_datetime(
            #     chunk[col].astype(str).str.strip(),
            #     format=spec['fmt'], errors='coerce'
            # ).dt.date
            
            if spec.get("fmt"):
                chunk[col] = pd.to_datetime(
                    chunk[col].astype(str).str.strip(),
                    format=spec["fmt"],
                    errors="coerce"
                ).dt.strftime('%Y-%m-%d')
            else:
                chunk[col] = pd.to_datetime(
                    chunk[col].astype(str).str.strip(),
                    errors="coerce",
                    dayfirst=True
                ).dt.strftime('%Y-%m-%d')
    return chunk


def _detect_encoding(path: str) -> str:
    """Cheap encoding sniff: prefer utf-8, fall back to cp1252/latin1."""
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin1'):
        try:
            with open(path, encoding=enc) as f:
                for _ in range(2000):
                    if f.readline() == '':
                        break
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return 'latin1'


def process_csv_job(file_paths, allocated_db_name, db_host, db_user, db_pass, db_port,
                     trigger_source="csv_upload", session_id=None, user_id=None, username=None, ch_id=None):
    safe_user = quote_plus(db_user)
    safe_pass = quote_plus(db_pass)
    base_url = f"mysql+pymysql://{safe_user}:{safe_pass}@{db_host}:{db_port}"

    target_engine = create_engine(
        f"{base_url}/{allocated_db_name}",
        pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=3600,
    )

    tables_created = []

    # [NEW] Track if any new data was inserted across all files
    any_incremental = False
    
    # [NEW] Inspector to check if tables exist and fetch their schemas for Header Matching
    from sqlalchemy import inspect, text
    inspector = inspect(target_engine)
    existing_tables = inspector.get_table_names()
    
    # Pre-fetch existing tables and their schemas to enable smart merging
    schema_map = {}
    with target_engine.connect() as conn:
        for t in existing_tables:
            if t.startswith("temp_"):
                continue  # Skip temp tables to avoid concurrency / lock issues
            cols_res = conn.execute(text(f"SHOW COLUMNS FROM `{t}`"))
            schema_map[t] = set([row[0] for row in cols_res])

    import os
    # Sort file_paths by original filename (ignoring UUID prefix) so base files come first
    file_paths.sort(key=lambda p: os.path.basename(p).split('_', 1)[-1] if '_' in os.path.basename(p) else os.path.basename(p))

    for path in file_paths:
        print(f"\n🚀 Starting processing for file: {path}")
        encoding = _detect_encoding(path)
        print(f"🔤 Encoding detected: {encoding}")

        # Total data rows (minus header) for progress + loss reconciliation.
        with open(path, encoding=encoding) as f:
            total_rows = max(sum(1 for _ in f) - 1, 0)
        print(f"📊 Total data rows detected: {total_rows}")

        # Extract clean filename without UUID prefix
        filename = os.path.basename(path)
        if '_' in filename:
            parts = filename.split('_', 1)
            # Check if first part looks like a UUID (length ~36)
            if len(parts[0]) >= 32 and '-' in parts[0]:
                raw_name = parts[1].rsplit('.', 1)[0]
            else:
                raw_name = filename.rsplit('.', 1)[0]
        else:
            raw_name = filename.rsplit('.', 1)[0]

        default_table_name = _sanitize(raw_name)[:60]

        # ── Pass 1: infer schema from a sample ────────────────────────────
        sample = pd.read_csv(
            path, dtype=str, encoding=encoding, engine="python",
            on_bad_lines="warn", nrows=SAMPLE_ROWS,
        )
        # Drop any empty/Unnamed columns created by trailing commas or blank headers
        sample = sample.loc[:, ~sample.columns.str.contains('^Unnamed', case=False, na=False)]
        
        # [NEW] Deduplicate columns exactly as in _infer_schema to avoid Series/DataFrame ambiguity
        dedup_cols = []
        seen = {}
        for c in sample.columns:
            base = _sanitize(c)
            if base in seen:
                seen[base] += 1
                dedup_cols.append(f"{base}_{seen[base]}")
            else:
                seen[base] = 0
                dedup_cols.append(base)
        sample.columns = dedup_cols
        
        # Dynamic Schema & Header Matching: Step 1 Priority check for exact table name match
        matched_table = None
        target_candidate = next((t for t in schema_map if t.lower() == default_table_name.lower()), None)
        
        # Step 2: Strict high-confidence schema fallback (only if exact table name doesn't exist)
        if not target_candidate and len(sample.columns) > 0:
            best_match = None
            best_ratio = 0.0
            for t_name, t_cols in schema_map.items():
                if t_cols:
                    intersection_count = len(set(sample.columns).intersection(t_cols))
                    overlap_ratio = intersection_count / max(len(sample.columns), len(t_cols))
                    if overlap_ratio >= 0.70 and overlap_ratio > best_ratio:
                        best_ratio = overlap_ratio
                        best_match = t_name
            if best_match:
                target_candidate = best_match

        column_rename_map = {}
        if target_candidate:
            matched_table = target_candidate
            existing_cols = list(schema_map[matched_table])

            # Pull a real sample of the EXISTING table's data so the matcher can
            # compare actual data type / value-pattern fingerprints, not just
            # column-name text similarity. Without this, match_columns_to_existing()
            # falls back to header-only matching for every existing column, which
            # defeats "check the data type and pattern it contains" entirely.
            existing_sample_df = None
            try:
                with target_engine.connect() as sample_conn:
                    sample_rows = sample_conn.execute(
                        text(f"SELECT * FROM `{matched_table}` LIMIT 200")
                    ).fetchall()
                    if sample_rows:
                        existing_sample_df = pd.DataFrame(
                            sample_rows, columns=list(schema_map[matched_table])
                        )
            except Exception as sample_err:
                print(f"[SCHEMA-MATCH] could not sample existing table "
                      f"{matched_table} for pattern matching: {sample_err}")
                existing_sample_df = None

            # Match columns dynamically — by data type/pattern fingerprint first,
            # header-name similarity second (cross-checked together).
            match_res = match_columns_to_existing(sample, existing_cols, existing_sample_df)
            mapping = match_res['column_mapping']
            unmapped_cols = match_res['unmapped_new_cols']
            
            # Alter table to add unmapped new columns
            if unmapped_cols:
                with target_engine.begin() as alter_conn:
                    for c in unmapped_cols:
                        kind = _infer_schema(sample[[c]]).get(c, {}).get('kind', 'text')
                        col_type = "TEXT"
                        if kind == 'numeric':
                            col_type = f"DECIMAL({MONEY_PRECISION}, {MONEY_SCALE})"
                        elif kind == 'int':
                            col_type = "BIGINT"
                        elif kind == 'date':
                            col_type = "DATE"
                        try:
                            alter_conn.execute(text(f"ALTER TABLE `{matched_table}` ADD COLUMN `{c}` {col_type}"))
                        except Exception:
                            pass
                        schema_map[matched_table].add(c)
                        
            column_rename_map = {in_c: target_c for in_c, target_c in mapping.items() if target_c is not None}
            sample = sample.rename(columns=column_rename_map)

        table_name = matched_table if matched_table else default_table_name
        table_exists = matched_table is not None
        target_table_name = f"temp_{table_name}" if table_exists else table_name
        
        schema = _infer_schema(sample)
        sa_dtype = _sqlalchemy_dtype(schema)
        print("🧬 Inferred column types:")
        for c, s in schema.items():
            extra = f" [{s['fmt']}]" if s['fmt'] else ""
            print(f"     {c:<24} -> {s['kind']}{extra}")

        # ── Pass 2: stream the full file, typed, into SQL ─────────────────
        first_chunk = True
        processed_rows = 0
        for chunk in pd.read_csv(
            path, chunksize=CHUNK_SIZE, dtype=str, encoding=encoding,
            engine="python", on_bad_lines="warn",
        ):
            # Drop any empty/Unnamed columns in the chunk
            chunk = chunk.loc[:, ~chunk.columns.str.contains('^Unnamed', case=False, na=False)]
            
            # [NEW] Deduplicate chunk columns exactly like sample
            dedup_cols_chunk = []
            seen_chunk = {}
            for c in chunk.columns:
                base = _sanitize(c)
                if base in seen_chunk:
                    seen_chunk[base] += 1
                    dedup_cols_chunk.append(f"{base}_{seen_chunk[base]}")
                else:
                    seen_chunk[base] = 0
                    dedup_cols_chunk.append(base)
            chunk.columns = dedup_cols_chunk
            if column_rename_map:
                chunk = chunk.rename(columns=column_rename_map)
            chunk = _apply_schema(chunk, schema)
            processed_rows += len(chunk)

            pct = (processed_rows / total_rows * 100) if total_rows else 100.0
            print(f"[PROGRESS] {table_name} → {pct:6.2f}% "
                  f"({processed_rows}/{total_rows} rows)")

            # [MODIFIED] Write to target_table_name (temp table if exists, else main table)
            chunk.to_sql(
                target_table_name, target_engine,
                if_exists='replace' if first_chunk else 'append',
                index=False, method='multi', chunksize=1000,
                dtype=sa_dtype,
            )
            first_chunk = False

        # Loud reconciliation — never lose rows silently.
        if processed_rows != total_rows:
            print(f"⚠️  ROW COUNT MISMATCH for {table_name}: "
                  f"loaded {processed_rows} of {total_rows} "
                  f"({total_rows - processed_rows} unparsed). "
                  f"Check the CSV for malformed lines.")

        # [NEW] If table existed, we wrote to a temp table. Now merge incrementally using SQL.
        if table_exists:
            with target_engine.begin() as conn:
                # Build join conditions for duplicate checking
                columns = [f"`{c}`" for c in sample.columns]
                join_conds = " AND ".join([f"`{table_name}`.{c} <=> `{target_table_name}`.{c}" for c in columns])
                
                cols_str = ", ".join(columns)
                merge_sql = f"""
                    INSERT INTO `{table_name}` ({cols_str})
                    SELECT {cols_str} FROM `{target_table_name}`
                    WHERE NOT EXISTS (
                        SELECT 1 FROM `{table_name}`
                        WHERE {join_conds}
                    )
                """
                result = conn.execute(text(merge_sql))
                inserted_rows = result.rowcount
                print(f"🔄 Incremental Merge: Inserted {inserted_rows} new rows into {table_name}")
                
                if inserted_rows > 0:
                    any_incremental = True
                
                # Note: pandas to_sql creates a regular table. Drop it after merge.
                conn.execute(text(f"DROP TABLE IF EXISTS `{target_table_name}`"))
        else:
            # New table created
            any_incremental = True

        tables_created.append(table_name)
        print(f"✅ Finished loading table: {table_name} ({processed_rows} rows)")

        if session_id and user_id:
            try:
                from database.config import MYSQL_CONFIG
                import pymysql
                import os
                conn = pymysql.connect(host=MYSQL_CONFIG["host"], user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"], database=MYSQL_CONFIG["database"])
                with conn.cursor() as cur:
                    # Get actual total rows from the table after merge
                    true_total_rows = total_rows
                    try:
                        with target_engine.connect() as count_conn:
                            true_total_rows = count_conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar() or total_rows
                    except Exception:
                        pass
                        
                    # check if already exists to avoid duplicates (unless we merged new rows, then update)
                    cur.execute("SELECT id FROM external_db_sync_log WHERE session_id=%s AND table_name=%s", (session_id, table_name))
                    existing_log = cur.fetchone()
                    
                    file_size_bytes = os.path.getsize(path) if os.path.exists(path) else 0
                    table_data_size_mb = round(file_size_bytes / (1024 * 1024), 2)
                    num_columns = len(sample.columns)
                    exact_size_mb = file_size_bytes / (1024 * 1024)
                    
                    if not existing_log:
                        log_query = """
                        INSERT INTO external_db_sync_log
                        (user_id,username,external_database,table_name,
                        action_type,rows_affected,session_id,new_user_db,
                        total_rows,total_columns,data_size_mb,exact_size_mb)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """
                        cur.execute(log_query, (
                            user_id, username or "unknown", os.path.basename(path), table_name,
                            "IMPORT", processed_rows, session_id, allocated_db_name,
                            true_total_rows, num_columns, table_data_size_mb, exact_size_mb
                        ))
                    else:
                        if processed_rows > 0:
                            update_query = """
                            UPDATE external_db_sync_log
                            SET rows_affected = rows_affected + %s,
                                total_rows = %s,
                                total_columns = %s,
                                sync_time = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """
                            cur.execute(update_query, (processed_rows, true_total_rows, num_columns, existing_log[0]))
                        else:
                            cur.execute("UPDATE external_db_sync_log SET sync_time = CURRENT_TIMESTAMP WHERE id = %s", (existing_log[0],))
                            
                    conn.commit()
                conn.close()
            except Exception as e:
                print(f"Failed to log CSV import to external_db_sync_log: {e}")

    if ch_id and session_id and user_id:
        try:
            from database.config import MYSQL_CONFIG
            import pymysql
            conn = pymysql.connect(host=MYSQL_CONFIG["host"], user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"], database=MYSQL_CONFIG["database"])
            with conn.cursor() as cur:
                cur.execute("UPDATE connection_history SET status='Success' WHERE id = %s", (ch_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Failed to update connection_history status: {e}")

    print("\n🎉 All files processed successfully!")
    # ──────────────────────────────────────────────────────────────
    # KNOWLEDGE GRAPH BUILDER TRIGGER
    # ──────────────────────────────────────────────────────────────
    try:
        from database.kgraph_builder import build_kgraph
        # [MODIFIED] Pass force=any_incremental to avoid useless rebuilds
        build_kgraph(allocated_db_name, db_host, db_user, db_pass, db_port,
                     force=any_incremental, trigger_source=trigger_source)
    except Exception as e:
        print(f"[KGRAPH] post-CSV build skipped: {e}")
    return tables_created
