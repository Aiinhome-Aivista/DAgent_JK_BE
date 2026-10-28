"""
hana_metadata_service.py — SAP HANA metadata extraction (STRICTLY metadata, no data).

Step 1 of the SAP HANA connector pipeline:

    POST /connect-external-db  (db_type = hana / sap_hana)
        -> discover_hana_metadata()            [synchronous, this module]
        -> build_hana_kgraph_from_metadata()   [background, hana_kgraph_service]

What this module reads from SAP HANA (catalog / system views only):
    SYS.VIEWS / SYS.TABLES                 -> which whitelisted objects exist (+ comments)
    SYS.VIEW_COLUMNS / SYS.TABLE_COLUMNS   -> column names, data types, length, scale,
                                              nullability, column comments
    SYS.CONSTRAINTS                        -> primary keys
    SYS.REFERENTIAL_CONSTRAINTS            -> foreign keys
    SELECT COUNT(*)                        -> row count (a single number, not rows)

What it NEVER reads: business data rows of master or transaction views.
There is no SELECT * / SELECT TOP n / sampling anywhere in this module.

All catalog results are loaded into pandas DataFrames and the per-view
"technical summary" (tabular structure) is derived with pandas.

Where SAP HANA has no declared PK/FK (typical for CDS / calculation views),
keys are inferred deterministically from METADATA ONLY (column names, data
types, positions, master-vs-transaction naming) and tagged
source = "metadata_inferred".
"""

import os
import re
import json

import pymysql
import pandas as pd

from database.config import MYSQL_CONFIG

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
HANA_DB_TYPES = ("hana", "sap_hana", "saphana", "sap")

# Whitelisted SAP HANA views (SAP_S4 schema) — metadata extraction only.
SAP_ALLOWED_DATASPEC_OBJECTS = [
    "ZIVF_CustomerMaster_01_MD",
    "ZIVR_SKUMaster_01_TR",
    "ZIVF_CategoryMaster_01_TR",
    "ZITR_DayAttribute_01_TR",
    "ZIVF_TerritoryMaster_01_TR",
    "ZPVF_SD_SalesHistory_01_TR",
    "ZPVF_SD_CurrentSales_01_TR",
    "ZIVF_ZoneMaster_01_TR",
    "ZIVF_RegionMaster_MD_01",
    "ZPVF_SD_CancelledSalesInvoice_01_TR",
    "ZIVF_SD_Sales_Plan_Target_01_TR"
]
SAP_ALLOWED_OBJECTS_SET = {name.upper() for name in SAP_ALLOWED_DATASPEC_OBJECTS}

TARGET_SCHEMA = os.getenv("HANA_TARGET_SCHEMA", "SAP_S4")

# COUNT(*) returns one number per view (metadata). Can be disabled for very
# slow calculation views by setting HANA_METADATA_ROW_COUNT=false.
HANA_COUNT_ROWS = os.getenv("HANA_METADATA_ROW_COUNT", "true").strip().lower() in ("1", "true", "yes")

METADATA_TABLE = "sap_hana_view_metadata"

# Naming hints used to classify a view as master (dimension) vs transaction (fact)
_MASTER_NAME_HINTS = ("MASTER", "_MD", "ATTRIBUTE", "DIM")

# Columns present in practically every SAP object — never used as join keys.
_IGNORED_JOIN_COLUMNS = {"MANDT", "CLIENT", "SAPCLIENT", "LANGU", "SPRAS", "LANGUAGE"}

# Common SAP technical key fields (in addition to *_ID / *CODE / *KEY ... suffixes)
_SAP_KEY_FIELDS = {
    "KUNNR", "MATNR", "VBELN", "POSNR", "WERKS", "VKORG", "VTWEG", "SPART", "BUKRS",
    "LIFNR", "KOKRS", "PRCTR", "KOSTL", "BZIRK", "REGIO", "LAND1", "MATKL", "VKBUR",
    "VKGRP", "LGORT", "CHARG", "FKART", "AUART", "PERNR",
}
_KEY_SUFFIXES = ("ID", "CODE", "KEY", "NO", "NUMBER", "NUM", "NR")

_NUMERIC_TYPES = {"DECIMAL", "SMALLDECIMAL", "DOUBLE", "REAL", "FLOAT", "INTEGER",
                  "BIGINT", "SMALLINT", "TINYINT"}
_DATE_TYPES = {"DATE", "TIMESTAMP", "SECONDDATE", "TIME", "LONGDATE"}
_DATE_NAME_HINTS = ("DATE", "CALDAY", "FKDAT", "ERDAT", "BUDAT", "AUDAT", "PERIOD")


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _mysql_conn(dict_cursor=False, autocommit=False):
    kwargs = dict(
        host=MYSQL_CONFIG["host"],
        port=MYSQL_CONFIG.get("port", 3306),
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        database=MYSQL_CONFIG["database"],
        autocommit=autocommit,
    )
    if dict_cursor:
        kwargs["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**kwargs)


def _native(obj):
    """Recursively convert numpy / pandas scalars into plain Python (JSON-safe)."""
    if isinstance(obj, dict):
        return {str(k): _native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_native(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            return obj.item()
        except Exception:
            pass
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def _query_df(cursor, sql, params=()):
    """Run a catalog query and return the result as a pandas DataFrame."""
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cols = [d[0] for d in (cursor.description or [])]
    return pd.DataFrame([tuple(r) for r in rows], columns=cols)


def _first_working_query(cursor, variants, params):
    """Try several SQL variants (older HANA revisions lack some catalog columns)."""
    last_err = None
    for sql in variants:
        try:
            return _query_df(cursor, sql, params)
        except Exception as e:  # noqa: BLE001
            last_err = e
    print(f"[HANA-METADATA] catalog query failed: {last_err}")
    return pd.DataFrame()


def _norm(name):
    return re.sub(r"[^A-Z0-9]", "", str(name or "").upper())


def _type_family(dtype):
    t = str(dtype or "").upper()
    if t in _NUMERIC_TYPES:
        return "numeric"
    if t in _DATE_TYPES:
        return "date"
    return "string"


def _is_key_like(col_name):
    n = _norm(col_name)
    if not n or n in _IGNORED_JOIN_COLUMNS:
        return False
    if n in _SAP_KEY_FIELDS:
        return True
    return any(n.endswith(s) for s in _KEY_SUFFIXES) and len(n) > 2


def _is_date_like(col_name, dtype):
    if str(dtype or "").upper() in _DATE_TYPES:
        return True
    n = _norm(col_name)
    return any(h in n for h in _DATE_NAME_HINTS)


def _classify_role(view_name):
    up = str(view_name).upper()
    return "master" if any(h in up for h in _MASTER_NAME_HINTS) else "transaction"


def _entity_token(view_name):
    """'ZIVF_CustomerMaster_01_MD' -> 'CUSTOMER' (used to pick the master's key)."""
    m = re.search(r"([A-Za-z]+?)(?:Master|Attribute)", str(view_name))
    return m.group(1).upper() if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Credentials
# ─────────────────────────────────────────────────────────────────────────────
def _load_credential(user_id, connection_id, session_id=None):
    conn = _mysql_conn()
    try:
        with conn.cursor() as cursor:
            row = None
            if session_id:
                cursor.execute("""
                    SELECT credential, db_type, session_id FROM database_credential
                    WHERE user_id=%s AND connection_id=%s AND session_id=%s
                    LIMIT 1
                """, (user_id, connection_id, session_id))
                row = cursor.fetchone()
            if not row:
                cursor.execute("""
                    SELECT credential, db_type, session_id FROM database_credential
                    WHERE user_id=%s AND connection_id=%s
                    LIMIT 1
                """, (user_id, connection_id))
                row = cursor.fetchone()
            if not row:
                cursor.execute("""
                    SELECT credential, db_type, session_id FROM database_credential
                    WHERE connection_id=%s LIMIT 1
                """, (connection_id,))
                row = cursor.fetchone()
            if not row:
                raise Exception(f"Connection credential not found for connection_id: {connection_id}")

            external_db = json.loads(row[0])
            db_type = (row[1] or "").strip().lower()
            if not session_id and row[2]:
                session_id = row[2]
            return external_db, db_type, session_id
    finally:
        conn.close()


def get_hana_connection_from_credential(external_db):
    """
    Creates an hdbcli dbapi connection (SSH tunnel, HANA Cloud / SSL, tenant DB).
    Returns (connection, tunnel_or_None, effective_schema).
    """
    from hdbcli import dbapi  # pyrefly: ignore[missing-import]

    tunnel = None
    db_host = external_db["host"]
    db_port = int(external_db.get("port") or 30015)

    if external_db.get("ssh_host"):
        from sshtunnel import SSHTunnelForwarder  # pyrefly: ignore[missing-import]
        tunnel = SSHTunnelForwarder(
            (external_db["ssh_host"], int(external_db.get("ssh_port") or 22)),
            ssh_username=external_db.get("ssh_username"),
            ssh_password=external_db.get("ssh_password"),
            ssh_pkey=external_db.get("ssh_key_file"),
            remote_bind_address=(external_db.get("remote_mysql_host") or external_db["host"],
                                 int(external_db.get("remote_mysql_port") or db_port)),
        )
        tunnel.start()
        db_host = "127.0.0.1"
        db_port = tunnel.local_bind_port

    is_hana_cloud = ("hanacloud.ondemand.com" in str(external_db["host"]).lower()) or (db_port == 443)
    connect_kwargs = {
        "address": db_host,
        "port": db_port,
        "user": external_db["username"],
        "password": external_db["password"],
        "autocommit": True,
    }
    if external_db.get("database") and not is_hana_cloud:
        connect_kwargs["databaseName"] = external_db["database"]

    effective_schema = external_db.get("schema") or (external_db.get("database") if is_hana_cloud else None)
    if effective_schema:
        connect_kwargs["currentSchema"] = effective_schema

    if external_db.get("encrypt") or is_hana_cloud:
        connect_kwargs["encrypt"] = True
        connect_kwargs["sslValidateCertificate"] = False

    try:
        source_conn = dbapi.connect(**connect_kwargs)
    except Exception as e:
        if "not connected" in str(e).lower() and "databaseName" in connect_kwargs:
            fallback_db = connect_kwargs.pop("databaseName", None)
            if not connect_kwargs.get("currentSchema") and fallback_db:
                connect_kwargs["currentSchema"] = fallback_db
            try:
                source_conn = dbapi.connect(**connect_kwargs)
            except Exception:
                if tunnel:
                    try: tunnel.stop()
                    except Exception: pass
                raise
        else:
            if tunnel:
                try: tunnel.stop()
                except Exception: pass
            raise

    return source_conn, tunnel, effective_schema


# ─────────────────────────────────────────────────────────────────────────────
# 2. Catalog extraction -> pandas DataFrames (one round-trip per catalog view)
# ─────────────────────────────────────────────────────────────────────────────
def _extract_catalog_frames(cursor, schema):
    names = list(SAP_ALLOWED_DATASPEC_OBJECTS)
    ph = ", ".join(["?"] * len(names))
    name_args = [n.upper() for n in names]

    # Objects: views first, then (non-temporary) tables with the same whitelist.
    views_df = _first_working_query(cursor, [
        f"""SELECT VIEW_NAME AS OBJ_NAME, SCHEMA_NAME, 'VIEW' AS OBJ_TYPE,
                   VIEW_TYPE AS OBJ_SUBTYPE, COMMENTS
            FROM SYS.VIEWS
            WHERE UPPER(SCHEMA_NAME) = UPPER(?) AND UPPER(VIEW_NAME) IN ({ph})""",
        f"""SELECT VIEW_NAME AS OBJ_NAME, SCHEMA_NAME, 'VIEW' AS OBJ_TYPE,
                   '' AS OBJ_SUBTYPE, '' AS COMMENTS
            FROM SYS.VIEWS
            WHERE UPPER(SCHEMA_NAME) = UPPER(?) AND UPPER(VIEW_NAME) IN ({ph})""",
    ], [schema] + name_args)

    tables_df = _first_working_query(cursor, [
        f"""SELECT TABLE_NAME AS OBJ_NAME, SCHEMA_NAME, 'TABLE' AS OBJ_TYPE,
                   TABLE_TYPE AS OBJ_SUBTYPE, COMMENTS
            FROM SYS.TABLES
            WHERE UPPER(SCHEMA_NAME) = UPPER(?) AND IS_TEMPORARY = 'FALSE'
              AND UPPER(TABLE_NAME) IN ({ph})""",
    ], [schema] + name_args)

    frames = [df for df in (views_df, tables_df) if not df.empty]
    objects_df = pd.concat(frames, ignore_index=True) if frames else \
        pd.DataFrame(columns=["OBJ_NAME", "SCHEMA_NAME", "OBJ_TYPE", "OBJ_SUBTYPE", "COMMENTS"])
    if not objects_df.empty:
        objects_df = objects_df.drop_duplicates(subset=["OBJ_NAME"], keep="first").reset_index(drop=True)
        # Whitelist is enforced again in pandas (defence in depth).
        objects_df = objects_df[objects_df["OBJ_NAME"].astype(str).str.upper().isin(SAP_ALLOWED_OBJECTS_SET)]

    if objects_df.empty:
        empty = pd.DataFrame()
        return objects_df, empty, empty, empty

    exact_schema = str(objects_df["SCHEMA_NAME"].iloc[0])
    view_names = objects_df.loc[objects_df["OBJ_TYPE"] == "VIEW", "OBJ_NAME"].astype(str).tolist()
    table_names = objects_df.loc[objects_df["OBJ_TYPE"] == "TABLE", "OBJ_NAME"].astype(str).tolist()

    col_frames = []
    if view_names:
        vph = ", ".join(["?"] * len(view_names))
        col_frames.append(_first_working_query(cursor, [
            f"""SELECT VIEW_NAME AS OBJ_NAME, COLUMN_NAME, POSITION, DATA_TYPE_NAME,
                       LENGTH, SCALE, IS_NULLABLE, COMMENTS
                FROM SYS.VIEW_COLUMNS
                WHERE SCHEMA_NAME = ? AND VIEW_NAME IN ({vph}) ORDER BY VIEW_NAME, POSITION""",
            f"""SELECT VIEW_NAME AS OBJ_NAME, COLUMN_NAME, POSITION, DATA_TYPE_NAME,
                       LENGTH, SCALE, 'TRUE' AS IS_NULLABLE, '' AS COMMENTS
                FROM SYS.VIEW_COLUMNS
                WHERE SCHEMA_NAME = ? AND VIEW_NAME IN ({vph}) ORDER BY VIEW_NAME, POSITION""",
        ], [exact_schema] + view_names))
    if table_names:
        tph = ", ".join(["?"] * len(table_names))
        col_frames.append(_first_working_query(cursor, [
            f"""SELECT TABLE_NAME AS OBJ_NAME, COLUMN_NAME, POSITION, DATA_TYPE_NAME,
                       LENGTH, SCALE, IS_NULLABLE, COMMENTS
                FROM SYS.TABLE_COLUMNS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME IN ({tph}) ORDER BY TABLE_NAME, POSITION""",
        ], [exact_schema] + table_names))
    col_frames = [f for f in col_frames if not f.empty]
    columns_df = pd.concat(col_frames, ignore_index=True) if col_frames else pd.DataFrame()

    all_names = objects_df["OBJ_NAME"].astype(str).tolist()
    aph = ", ".join(["?"] * len(all_names))
    pk_df = _first_working_query(cursor, [
        f"""SELECT TABLE_NAME AS OBJ_NAME, COLUMN_NAME, POSITION
            FROM SYS.CONSTRAINTS
            WHERE SCHEMA_NAME = ? AND TABLE_NAME IN ({aph}) AND IS_PRIMARY_KEY = 'TRUE'
            ORDER BY TABLE_NAME, POSITION""",
    ], [exact_schema] + all_names)

    fk_df = _first_working_query(cursor, [
        f"""SELECT TABLE_NAME AS OBJ_NAME, CONSTRAINT_NAME, COLUMN_NAME,
                   REFERENCED_SCHEMA_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM SYS.REFERENTIAL_CONSTRAINTS
            WHERE SCHEMA_NAME = ? AND TABLE_NAME IN ({aph})""",
    ], [exact_schema] + all_names)

    return objects_df, columns_df, pk_df, fk_df


def _row_count(cursor, schema, name):
    """COUNT(*) — returns a single integer. No row content is fetched."""
    if not HANA_COUNT_ROWS:
        return None
    try:
        safe_schema = str(schema).replace('"', '""')
        safe_name = str(name).replace('"', '""')
        cursor.execute(f'SELECT COUNT(*) FROM "{safe_schema}"."{safe_name}"')
        res = cursor.fetchone()
        return int(res[0]) if res else 0
    except Exception as e:  # noqa: BLE001
        print(f"[HANA-METADATA] COUNT(*) failed for {name}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. pandas technical summary + metadata-only key inference
# ─────────────────────────────────────────────────────────────────────────────
def _columns_for(columns_df, obj_name):
    if columns_df.empty:
        return []
    df = columns_df[columns_df["OBJ_NAME"] == obj_name].copy()
    if df.empty:
        return []
    df = df.sort_values("POSITION")
    df["LENGTH"] = df["LENGTH"].fillna(0)
    df["SCALE"] = df["SCALE"].fillna(0)
    if "COMMENTS" not in df.columns:
        df["COMMENTS"] = ""
    df["COMMENTS"] = df["COMMENTS"].fillna("")
    df["IS_NULLABLE"] = df["IS_NULLABLE"].astype(str).str.upper().eq("TRUE")
    df = df.rename(columns={
        "COLUMN_NAME": "name", "DATA_TYPE_NAME": "data_type", "LENGTH": "length",
        "SCALE": "scale", "POSITION": "position", "IS_NULLABLE": "is_nullable",
        "COMMENTS": "comment",
    })
    return _native(df[["name", "data_type", "length", "scale", "position",
                       "is_nullable", "comment"]].to_dict(orient="records"))


def _technical_summary(cols, role, view_comment, subtype):
    """Pandas-derived structural profile of one view (no data involved)."""
    base = {"role": role, "view_comment": view_comment or "", "object_subtype": subtype or ""}
    if not cols:
        base.update({"type_distribution": {}, "key_like_columns": [], "measure_columns": [],
                     "date_columns": [], "attribute_columns": []})
        return base
    df = pd.DataFrame(cols)
    df["family"] = df["data_type"].map(_type_family)
    df["key_like"] = df["name"].map(_is_key_like)
    df["date_like"] = [_is_date_like(n, t) for n, t in zip(df["name"], df["data_type"])]
    df["measure"] = (df["family"] == "numeric") & ~df["key_like"] & ~df["date_like"]
    df["attribute"] = ~df["key_like"] & ~df["date_like"] & ~df["measure"]
    base.update({
        "type_distribution": df["data_type"].astype(str).value_counts().to_dict(),
        "key_like_columns": df.loc[df["key_like"], "name"].tolist(),
        "measure_columns": df.loc[df["measure"], "name"].tolist(),
        "date_columns": df.loc[df["date_like"], "name"].tolist(),
        "attribute_columns": df.loc[df["attribute"], "name"].tolist(),
    })
    return _native(base)


def _infer_keys_from_metadata(views):
    """
    Fill PK/FK gaps using metadata only (column names / types / positions).
    Declared SYS constraints always win; inferred entries are tagged
    source='metadata_inferred'. Nothing here touches the data.
    """
    # Column index across views: normalised name -> [(view, exact col, type family)]
    col_index = {}
    for v in views:
        for c in v["columns_detail"]:
            col_index.setdefault(_norm(c["name"]), []).append(
                (v["view_name"], c["name"], _type_family(c.get("data_type"))))

    # 3a. Primary keys for master views
    for v in views:
        if v["primary_keys"] or v["technical_summary"]["role"] != "master":
            continue
        key_cols = [c for c in v["columns_detail"] if _is_key_like(c["name"])]
        if not key_cols:
            # Calendar / day-attribute dimensions are keyed by their date column.
            key_cols = [c for c in v["columns_detail"] if _is_date_like(c["name"], c.get("data_type"))]
        if not key_cols:
            continue
        shared = [c for c in key_cols
                  if any(other != v["view_name"] for other, _, _ in col_index.get(_norm(c["name"]), []))]
        token = _entity_token(v["view_name"])
        pool = shared or key_cols
        preferred = [c for c in pool if token and token in _norm(c["name"])]
        chosen = sorted(preferred or pool, key=lambda c: c.get("position") or 0)[0]
        v["primary_keys"] = [chosen["name"]]
        v["technical_summary"]["primary_key_source"] = "metadata_inferred"

    # 3b. Foreign keys: any view column matching a master's single-column PK
    masters = []
    for m in views:
        if m["technical_summary"]["role"] != "master" or len(m["primary_keys"]) != 1:
            continue
        pk = m["primary_keys"][0]
        pk_type = next((c.get("data_type") for c in m["columns_detail"] if c["name"] == pk), None)
        masters.append((m["view_name"], pk, _type_family(pk_type)))

    for v in views:
        if v["technical_summary"].get("foreign_key_source") == "sys_constraints":
            continue
        existing = {(fk.get("column_name"), fk.get("referenced_table")) for fk in v["foreign_keys"]}
        for m_name, m_pk, m_family in masters:
            if m_name == v["view_name"]:
                continue
            for c in v["columns_detail"]:
                if _norm(c["name"]) != _norm(m_pk) or _norm(c["name"]) in _IGNORED_JOIN_COLUMNS:
                    continue
                if _type_family(c.get("data_type")) != m_family:
                    continue
                if (c["name"], m_name) in existing:
                    continue
                v["foreign_keys"].append({
                    "constraint_name": None,
                    "column_name": c["name"],
                    "referenced_schema": v["schema_name"],
                    "referenced_table": m_name,
                    "referenced_column": m_pk,
                    "source": "metadata_inferred",
                })
                v["relationships"].append({
                    "from_table": v["view_name"], "from_column": c["name"],
                    "to_table": m_name, "to_column": m_pk,
                    "source": "metadata_inferred",
                })
                existing.add((c["name"], m_name))
        if v["foreign_keys"] and not v["technical_summary"].get("foreign_key_source"):
            v["technical_summary"]["foreign_key_source"] = "metadata_inferred"
    return views


# ─────────────────────────────────────────────────────────────────────────────
# 4. MySQL persistence
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_metadata_table(cur):
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {METADATA_TABLE} (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        connection_id INT NOT NULL,
        session_id VARCHAR(100) NULL,
        schema_name VARCHAR(100) NOT NULL,
        view_name VARCHAR(200) NOT NULL,
        view_type VARCHAR(50) DEFAULT 'VIEW',
        row_count BIGINT DEFAULT 0,
        column_count INT DEFAULT 0,
        columns_detail JSON NULL,
        primary_keys JSON NULL,
        foreign_keys JSON NULL,
        relationships JSON NULL,
        technical_summary JSON NULL,
        discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_conn_schema_view (connection_id, schema_name, view_name),
        INDEX idx_user_conn (user_id, connection_id),
        INDEX idx_session (session_id)
    );
    """)
    # Older deployments created the table without technical_summary.
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = %s AND column_name = 'technical_summary'
    """, (METADATA_TABLE,))
    row = cur.fetchone()
    count = list(row.values())[0] if isinstance(row, dict) else row[0]
    if int(count) == 0:
        cur.execute(f"ALTER TABLE {METADATA_TABLE} ADD COLUMN technical_summary JSON NULL")


def _persist_metadata(user_id, connection_id, session_id, views):
    conn = _mysql_conn(autocommit=True)
    try:
        with conn.cursor() as cur:
            _ensure_metadata_table(cur)
            upsert = f"""
            INSERT INTO {METADATA_TABLE}
              (user_id, connection_id, session_id, schema_name, view_name, view_type, row_count,
               column_count, columns_detail, primary_keys, foreign_keys, relationships,
               technical_summary, discovered_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
              user_id=VALUES(user_id), session_id=VALUES(session_id), view_type=VALUES(view_type),
              row_count=VALUES(row_count), column_count=VALUES(column_count),
              columns_detail=VALUES(columns_detail), primary_keys=VALUES(primary_keys),
              foreign_keys=VALUES(foreign_keys), relationships=VALUES(relationships),
              technical_summary=VALUES(technical_summary), discovered_at=CURRENT_TIMESTAMP
            """
            for v in views:
                cur.execute(upsert, (
                    user_id, connection_id, session_id, v["schema_name"], v["view_name"],
                    v["view_type"], v["row_count"] or 0, v["column_count"],
                    json.dumps(_native(v["columns_detail"])),
                    json.dumps(_native(v["primary_keys"])),
                    json.dumps(_native(v["foreign_keys"])),
                    json.dumps(_native(v["relationships"])),
                    json.dumps(_native(v["technical_summary"])),
                ))
            # Remove views that are no longer discovered for this connection.
            if views:
                ph = ", ".join(["%s"] * len(views))
                cur.execute(
                    f"DELETE FROM {METADATA_TABLE} WHERE connection_id=%s AND view_name NOT IN ({ph})",
                    [connection_id] + [v["view_name"] for v in views])
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Public entry points
# ─────────────────────────────────────────────────────────────────────────────
def build_views_from_frames(objects_df, columns_df, pk_df, fk_df, row_counts=None):
    """Turn catalog DataFrames into per-view metadata dicts (pure pandas, testable)."""
    row_counts = row_counts or {}
    views = []
    for _, obj in objects_df.iterrows():
        name = str(obj["OBJ_NAME"])
        schema = str(obj["SCHEMA_NAME"])
        role = _classify_role(name)
        cols = _columns_for(columns_df, name)

        pks = []
        if not pk_df.empty:
            pks = pk_df.loc[pk_df["OBJ_NAME"] == name].sort_values("POSITION")["COLUMN_NAME"].tolist()

        fks, rels = [], []
        if not fk_df.empty:
            for _, fk in fk_df[fk_df["OBJ_NAME"] == name].iterrows():
                fks.append({
                    "constraint_name": fk["CONSTRAINT_NAME"], "column_name": fk["COLUMN_NAME"],
                    "referenced_schema": fk["REFERENCED_SCHEMA_NAME"],
                    "referenced_table": fk["REFERENCED_TABLE_NAME"],
                    "referenced_column": fk["REFERENCED_COLUMN_NAME"],
                    "source": "sys_constraints",
                })
                rels.append({
                    "from_table": name, "from_column": fk["COLUMN_NAME"],
                    "to_table": fk["REFERENCED_TABLE_NAME"], "to_column": fk["REFERENCED_COLUMN_NAME"],
                    "source": "sys_constraints",
                })

        summary = _technical_summary(cols, role, obj.get("COMMENTS"), obj.get("OBJ_SUBTYPE"))
        if pks:
            summary["primary_key_source"] = "sys_constraints"
        if fks:
            summary["foreign_key_source"] = "sys_constraints"

        views.append({
            "schema_name": schema,
            "view_name": name,
            "view_type": str(obj["OBJ_TYPE"]),
            "row_count": row_counts.get(name),
            "column_count": len(cols),
            "columns_detail": cols,
            "primary_keys": _native(pks),
            "foreign_keys": _native(fks),
            "relationships": _native(rels),
            "technical_summary": summary,
        })
    return _infer_keys_from_metadata(views)


def discover_hana_metadata(user_id, connection_id, session_id=None):
    """
    Connect to SAP HANA, extract metadata of the whitelisted SAP_S4 views and
    UPSERT it into MySQL `sap_hana_view_metadata`. STRICTLY metadata only.
    """
    external_db, db_type, session_id = _load_credential(user_id, connection_id, session_id)
    if db_type not in HANA_DB_TYPES:
        raise Exception(f"Connection type '{db_type}' is not SAP HANA.")

    source_conn, tunnel, _effective_schema = get_hana_connection_from_credential(external_db)
    try:
        cursor = source_conn.cursor()
        objects_df, columns_df, pk_df, fk_df = _extract_catalog_frames(cursor, TARGET_SCHEMA)
        print(f"[HANA-METADATA] {len(objects_df)} whitelisted objects in {TARGET_SCHEMA}; "
              f"{len(columns_df)} columns, {len(pk_df)} PK cols, {len(fk_df)} FK cols (catalog only).")
        row_counts = {str(o["OBJ_NAME"]): _row_count(cursor, o["SCHEMA_NAME"], o["OBJ_NAME"])
                      for _, o in objects_df.iterrows()}
    finally:
        try: source_conn.close()
        except Exception: pass
        if tunnel:
            try: tunnel.stop()
            except Exception: pass

    views = build_views_from_frames(objects_df, columns_df, pk_df, fk_df, row_counts)
    if not views:
        raise Exception(
            f"No whitelisted views were found in schema '{TARGET_SCHEMA}'. Check that the HANA user "
            f"can read the catalog (SYS.VIEWS / SYS.VIEW_COLUMNS) for {TARGET_SCHEMA}.")

    _persist_metadata(user_id, connection_id, session_id, views)

    found = {v["view_name"].upper() for v in views}
    missing = [n for n in SAP_ALLOWED_DATASPEC_OBJECTS if n.upper() not in found]
    if missing:
        print(f"[HANA-METADATA] Whitelisted views not found: {missing}")

    return _native({
        "status": "success",
        "connection_id": connection_id,
        "session_id": session_id,
        "schema": TARGET_SCHEMA,
        "total_views": len(views),
        "missing_views": missing,
        "extraction": {"engine": "pandas", "source": "SAP HANA system catalog", "raw_rows_read": 0},
        "views": views,
    })


def get_stored_hana_metadata(connection_id=None, session_id=None, user_id=None):
    """Read the stored SAP HANA metadata from MySQL."""
    conn = _mysql_conn(dict_cursor=True)
    try:
        with conn.cursor() as cur:
            try:
                _ensure_metadata_table(cur)
            except Exception:
                pass
            query = f"SELECT * FROM {METADATA_TABLE} WHERE 1=1"
            params = []
            if connection_id:
                query += " AND connection_id = %s"; params.append(connection_id)
            if session_id:
                query += " AND session_id = %s"; params.append(session_id)
            if user_id:
                query += " AND user_id = %s"; params.append(user_id)
            query += " ORDER BY schema_name, view_name ASC"
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        for r in rows:
            for col in ("columns_detail", "primary_keys", "foreign_keys", "relationships", "technical_summary"):
                val = r.get(col)
                if isinstance(val, (str, bytes)):
                    try:
                        r[col] = json.loads(val)
                    except Exception:
                        r[col] = {} if col == "technical_summary" else []
                elif val is None:
                    r[col] = {} if col == "technical_summary" else []
            if r.get("discovered_at") and hasattr(r["discovered_at"], "strftime"):
                r["discovered_at"] = r["discovered_at"].strftime("%Y-%m-%d %H:%M:%S")
        return rows
    finally:
        conn.close()
