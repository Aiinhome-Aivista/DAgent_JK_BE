# controllers/session_sources_controller.py
#
# GET /session-sources?session_id=xxx
#
# Returns unique topics from saved_web_results
# and unique external_database names from external_db_sync_log
# for a given session_id — as structured JSON

import re
from flask import request, jsonify


def session_sources_controller(get_connection_func):
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({
            "status": "failed", "statusCode": 400,
            "message": "Query param 'session_id' is required."
        }), 400

    conn = cursor = None
    try:
        conn   = get_connection_func()
        cursor = conn.cursor(dictionary=True)

        # ── Web topics ──────────────────────────────────────────
        cursor.execute("""
            SELECT
                topic,
                COUNT(*)        AS result_count,
                MIN(saved_at)   AS first_saved,
                MAX(saved_at)   AS last_saved
            FROM saved_web_results
            WHERE session_id = %s
              AND topic IS NOT NULL
              AND topic != ''
            GROUP BY topic
            ORDER BY result_count DESC
        """, (session_id,))
        topic_rows = cursor.fetchall()

        web_topics = [
            {
                "topic":        r["topic"],
                "result_count": r["result_count"],
                "first_saved":  str(r["first_saved"]) if r["first_saved"] else None,
                "last_saved":   str(r["last_saved"])  if r["last_saved"]  else None,
            }
            for r in topic_rows
        ]

        # ── External databases ───────────────────────────────────
        cursor.execute("""
            SELECT
                external_database,
                new_user_db,
                table_name,
                rows_affected,
                total_rows,
                data_size_mb,
                sync_time,
                id
            FROM external_db_sync_log
            WHERE session_id = %s
              AND external_database IS NOT NULL
              AND external_database != ''
            ORDER BY id ASC
        """, (session_id,))
        db_rows = cursor.fetchall()

        # Group by external_database
        db_groups = {}
        for r in db_rows:
            ext_db = r["external_database"]
            if ext_db not in db_groups:
                db_groups[ext_db] = {
                    "external_database": ext_db,
                    "new_user_db": r["new_user_db"],
                    "tables_dict": {},
                    "last_sync": r["sync_time"],
                    "total_rows": 0,
                    "data_size_mb": 0.0
                }
            
            grp = db_groups[ext_db]
            t_name = r["table_name"]
            
            # Fetch exact column count from information_schema
            cols = 0
            if r.get('new_user_db') and t_name:
                try:
                    cursor.execute("SELECT COUNT(*) as col_count FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (r['new_user_db'], t_name))
                    col_row = cursor.fetchone()
                    if col_row:
                        cols = col_row['col_count']
                except Exception:
                    pass
            
            # Keep the latest row count and size for each table
            ext_db = r.get('external_database')
            if ext_db:
                if ext_db.lower().endswith('.csv'):
                    display_table = ext_db
                elif ext_db.lower().endswith(('.xlsx', '.xls')):
                    display_table = f"{ext_db} ({t_name})"
                else:
                    display_table = f"{ext_db}.{t_name}"
            else:
                display_table = t_name
                
            grp["tables_dict"][t_name] = {
                "table": display_table,
                "rows": int(r.get("total_rows") or r["rows_affected"] or 0),
                "columns": cols,
                "size": float(r["data_size_mb"] or 0.0)
            }
            if r["sync_time"] and (not grp["last_sync"] or r["sync_time"] > grp["last_sync"]):
                grp["last_sync"] = r["sync_time"]

        external_dbs = []
        for ext_db, grp in db_groups.items():
            tables_list = list(grp["tables_dict"].values())
            
            # Calculate summary from the latest deduplicated tables
            tot_rows = sum(t["rows"] for t in tables_list)
            tot_cols = sum(t["columns"] for t in tables_list)
            tot_size = sum(t["size"] for t in tables_list)
            
            last_sync_str = "Just now"
            if grp["last_sync"]:
                import datetime
                try:
                    now = datetime.datetime.now()
                    if isinstance(grp["last_sync"], datetime.datetime):
                        diff = now - grp["last_sync"]
                        if diff.total_seconds() < 60:
                            last_sync_str = "Just now"
                        else:
                            last_sync_str = grp["last_sync"].strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        last_sync_str = str(grp["last_sync"])
                except Exception:
                    last_sync_str = str(grp["last_sync"])
            
            external_dbs.append({
                "external_database": grp["external_database"],
                "table_count": len(tables_list),
                "tables": tables_list,
                "last_sync": last_sync_str,
                "summary": {
                    "total_rows": tot_rows,
                    "total_columns": tot_cols,
                    "data_size_mb": round(tot_size, 2)
                }
            })

        # ── SAP HANA Metadata Sources ────────────────────────────
        try:
            cursor.execute("""
                SELECT connection_id, schema_name, view_name, row_count, column_count, discovered_at
                FROM sap_hana_view_metadata
                WHERE session_id = %s
            """, (session_id,))
            hana_rows = cursor.fetchall()
            if hana_rows:
                hana_tables = [
                    {
                        "table": f"{r.get('schema_name', '')}.{r['view_name']}",
                        "rows": int(r.get("row_count") or 0),
                        "columns": int(r.get("column_count") or 0),
                        "size": 0.0
                    }
                    for r in hana_rows
                ]
                external_dbs.append({
                    "external_database": "SAP HANA",
                    "table_count": len(hana_tables),
                    "tables": hana_tables,
                    "last_sync": str(hana_rows[0].get("discovered_at") or "Just now"),
                    "summary": {
                        "total_rows": sum(t["rows"] for t in hana_tables),
                        "total_columns": sum(t["columns"] for t in hana_tables),
                        "data_size_mb": 0.0
                    }
                })
        except Exception as h_err:
            print(f"[session_sources] SAP HANA check: {h_err}")

        simple_topics = [r["topic"] for r in topic_rows]
        simple_databases = [d["external_database"] for d in external_dbs]

        return jsonify({
            "status":       "success",
            "statusCode":   200,
            "session_id":   session_id,
                # new simple format
            "topics": simple_topics,
            "databases": simple_databases,
            "web_topics": {
                "total_unique_topics": len(web_topics),
                "topics": web_topics
            },
            "external_databases": {
                "total_unique_databases": len(external_dbs),
                "databases": external_dbs
            }
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error", "statusCode": 500,
            "message": str(e)
        }), 500
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()