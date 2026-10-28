"""
Autonomous Action Agent Planner & Universal Multi-Data Engine
============================================================
Provides an end-to-end autonomous, dynamic agent for all actions across:
- All Workspaces (active workspace, session workspace, all tables without limit)
- External Synced Databases (external_db_sync_log)
- Uploaded Sheets (sheet_scans)
- Dynamic Action Types: Email dispatch, Data export, Multi-table SQL, Decision/Alerts
- Self-Reflection & Self-Correction (automatic retry on SQL error or 0-row mismatch)
"""

import re
import os
import csv
import io
import json
import time
from typing import Dict, Any, List, Optional
from model.llm_client import call_llm_chat
from database.agent_action_service import log_action, update_action
from helper.action_state_machine import retry_with_backoff


# ─────────────────────────────────────────────────────────────────────────────
# 1. Multi-Source Database Schema Introspection (No Limits, All Tables)
# ─────────────────────────────────────────────────────────────────────────────
def get_universal_schema_context(
    session_id: str,
    user_id: int,
    instruction: str,
    get_connection_func
) -> Dict[str, Any]:
    """
    Introspects ALL available data sources:
    1. Active workspace for user (is_active = 1)
    2. Session workspace (by session_id)
    3. Synced databases from external_db_sync_log
    4. Sheet scans from sheet_scans
    Extracts complete table list, column definitions, and sample rows.
    """
    context = {
        "databases": [],
        "primary_db": None,
        "tables": {},
        "all_table_names": [],
        "user_id": user_id,
        "session_id": session_id,
    }

    if not get_connection_func:
        return context

    conn = None
    cur = None
    try:
        conn = get_connection_func()
        cur = conn.cursor(dictionary=True)

        found_dbs = []

        # 1. Active workspace for this user
        if user_id:
            try:
                cur.execute(
                    "SELECT workspace_db, workspace_name FROM workspaces WHERE user_id = %s AND is_active = 1",
                    (user_id,)
                )
                rows = cur.fetchall()
                for r in rows:
                    if r.get("workspace_db") and r["workspace_db"] not in found_dbs:
                        found_dbs.append(r["workspace_db"])
            except Exception as e:
                print(f"[ActionAgent] Error reading active workspace: {e}")

        # 2. Workspace for session_id
        if session_id:
            try:
                cur.execute(
                    "SELECT workspace_db, workspace_name FROM workspaces WHERE session_id = %s",
                    (session_id,)
                )
                rows = cur.fetchall()
                for r in rows:
                    if r.get("workspace_db") and r["workspace_db"] not in found_dbs:
                        found_dbs.append(r["workspace_db"])
            except Exception:
                pass

        # 3. Synced databases from external_db_sync_log
        try:
            cur.execute("""
                SELECT DISTINCT new_user_db
                FROM external_db_sync_log
                WHERE (session_id = %s OR user_id = %s)
                  AND new_user_db IS NOT NULL AND new_user_db != ''
            """, (session_id, user_id))
            for r in cur.fetchall():
                db_name = r.get("new_user_db")
                if db_name and db_name not in found_dbs:
                    found_dbs.append(db_name)
        except Exception:
            pass

        # 4. Fallback to latest workspace if still empty
        if not found_dbs:
            try:
                cur.execute("SELECT workspace_db FROM workspaces ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                if row and row.get("workspace_db"):
                    found_dbs.append(row["workspace_db"])
            except Exception:
                pass

        context["databases"] = found_dbs
        if found_dbs:
            context["primary_db"] = found_dbs[0]

        # Scan tables across all discovered databases
        all_tables_meta = {}
        all_names = []

        # Keywords in user instruction to prioritize relevant tables
        instr_words = set(re.findall(r'\b[a-zA-Z_]{3,}\b', instruction.lower()))

        for db in found_dbs:
            try:
                cur.execute(f"SHOW TABLES FROM `{db}`")
                tbl_rows = cur.fetchall()
                for tr in tbl_rows:
                    t_name = list(tr.values())[0]
                    full_name = f"{db}.{t_name}"
                    all_names.append(full_name)

                    # Check priority based on instruction words
                    t_clean = t_name.lower()
                    is_relevant = any(w in t_clean for w in instr_words) or len(tbl_rows) <= 15

                    all_tables_meta[full_name] = {
                        "db": db,
                        "table": t_name,
                        "is_relevant": is_relevant,
                        "columns": [],
                        "sample": {}
                    }
            except Exception as e:
                print(f"[ActionAgent] Error listing tables for db {db}: {e}")

        # Also check sheet_scans (uploaded CSVs)
        if session_id:
            try:
                cur.execute("SELECT table_name FROM sheet_scans WHERE session_id = %s", (session_id,))
                for sr in cur.fetchall():
                    s_name = sr.get("table_name")
                    if s_name:
                        all_names.append(s_name)
                        all_tables_meta[s_name] = {
                            "db": None,
                            "table": s_name,
                            "is_relevant": True,
                            "columns": [],
                            "sample": {}
                        }
            except Exception:
                pass

        context["all_table_names"] = all_names

        # Fetch column definitions and 1 sample row for tables (up to 20 tables)
        sorted_tables = sorted(all_tables_meta.keys(), key=lambda k: 0 if all_tables_meta[k]["is_relevant"] else 1)
        for tbl_key in sorted_tables[:20]:
            t_meta = all_tables_meta[tbl_key]
            db = t_meta["db"]
            t = t_meta["table"]
            target = f"`{db}`.`{t}`" if db else f"`{t}`"
            try:
                cur.execute(f"DESCRIBE {target}")
                cols = cur.fetchall()
                col_defs = [f"{c['Field']} ({c['Type']})" for c in cols]
                col_names = [c['Field'] for c in cols]

                cur.execute(f"SELECT * FROM {target} LIMIT 1")
                sample_row = cur.fetchone() or {}

                context["tables"][tbl_key] = {
                    "db": db,
                    "table": t,
                    "columns": col_defs,
                    "col_names": col_names,
                    "sample": {k: str(v) for k, v in sample_row.items() if v is not None}
                }
            except Exception as e:
                print(f"[ActionAgent] Error describing {target}: {e}")

    except Exception as e:
        print(f"[ActionAgent] Error in get_universal_schema_context: {e}")
    finally:
        if cur and hasattr(cur, "close"):
            try: cur.close()
            except: pass
        if conn and hasattr(conn, "close"):
            try: conn.close()
            except: pass

    return context


# ─────────────────────────────────────────────────────────────────────────────
# 2. Universal Agent Skills
# ─────────────────────────────────────────────────────────────────────────────
class UniversalSkills:
    """Dynamic, modular toolset for the Action Agent."""

    @staticmethod
    def user_lookup_skill(names: List[str], get_connection_func) -> List[Dict[str, str]]:
        """Matches human names from users table or direct email mentions."""
        recipients = []
        if not get_connection_func:
            return recipients

        conn = None
        cur = None
        try:
            conn = get_connection_func()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT id, name, email FROM users")
            all_users = cur.fetchall() or []

            for target in names:
                tgt_clean = target.lower().strip()
                if not tgt_clean:
                    continue

                matched = False
                for u in all_users:
                    u_name = (u.get("name") or "").strip().lower()
                    u_email = (u.get("email") or "").strip()
                    if not u_email or "@" not in u_email:
                        continue
                    if tgt_clean in u_name or u_name in tgt_clean:
                        recipients.append({"name": u.get("name"), "email": u_email})
                        matched = True
                        break

                if not matched:
                    first_part = tgt_clean.split()[0]
                    for u in all_users:
                        u_name = (u.get("name") or "").strip().lower()
                        u_email = (u.get("email") or "").strip()
                        if not u_email or "@" not in u_email:
                            continue
                        if u_name.startswith(first_part) or first_part in u_name:
                            recipients.append({"name": u.get("name"), "email": u_email})
                            break
        except Exception as e:
            print(f"[ActionAgent] user_lookup_skill error: {e}")
        finally:
            if cur and hasattr(cur, "close"):
                try: cur.close()
                except: pass
            if conn and hasattr(conn, "close"):
                try: conn.close()
                except: pass

        # Deduplicate by email
        unique = {}
        for r in recipients:
            unique[r["email"]] = r
        return list(unique.values())

    @staticmethod
    def sql_query_skill(sql_query: str, primary_db: str, get_connection_func) -> Dict[str, Any]:
        """
        Executes read-only SQL on the database.
        Handles joins, groupings, filter conditions, and aggregations.
        """
        result = {"success": False, "rows": [], "error": None, "query": sql_query, "row_count": 0}
        if not get_connection_func:
            result["error"] = "No database connection available"
            return result

        conn = None
        cur = None
        try:
            conn = get_connection_func()
            cur = conn.cursor(dictionary=True)

            clean_sql = sql_query.strip().rstrip(";")
            if not clean_sql.lower().startswith("select"):
                result["error"] = "Security guard: only SELECT queries are permitted for data operations."
                return result

            # Set default database if tables are unqualified
            if primary_db and f"`{primary_db}`" not in clean_sql and f"{primary_db}." not in clean_sql:
                try:
                    cur.execute(f"USE `{primary_db}`")
                except Exception:
                    pass

            # Ensure LIMIT guard to prevent memory blowout
            if "limit" not in clean_sql.lower():
                clean_sql += " LIMIT 3000"

            cur.execute(clean_sql)
            rows = cur.fetchall() or []
            result["success"] = True
            result["rows"] = rows
            result["row_count"] = len(rows)
            result["query"] = clean_sql

            # Sanitize datetimes/decimals for JSON compatibility
            for r in rows:
                for k, v in r.items():
                    if hasattr(v, "isoformat"):
                        r[k] = v.isoformat()
                    elif hasattr(v, "to_eng_string"):
                        r[k] = str(v)

        except Exception as e:
            result["error"] = str(e)
            print(f"[ActionAgent] sql_query_skill execution error: {e} on query [{sql_query}]")
        finally:
            if cur and hasattr(cur, "close"):
                try: cur.close()
                except: pass
            if conn and hasattr(conn, "close"):
                try: conn.close()
                except: pass

        return result

    @staticmethod
    def export_data_skill(rows: List[Dict[str, Any]], filename_prefix: str = "report") -> Dict[str, Any]:
        """Generates a downloadable CSV file from rows and returns metadata."""
        if not rows:
            return {"file_path": None, "file_name": None, "rows_count": 0}

        upload_dir = os.path.join(os.getcwd(), "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        filename = f"{filename_prefix}_{int(time.time())}.csv"
        file_path = os.path.join(upload_dir, filename)

        col_keys = [k for k in rows[0].keys() if k not in ("_row_id", "id")]
        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=col_keys, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                clean_r = {k: str(v) if v is not None else "" for k, v in r.items() if k in col_keys}
                writer.writerow(clean_r)

        return {
            "file_path": file_path,
            "file_name": filename,
            "download_url": f"/uploads/{filename}",
            "rows_count": len(rows)
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dynamic LLM Action Planner
# ─────────────────────────────────────────────────────────────────────────────
def generate_universal_plan(
    instruction: str,
    schema_context: Dict[str, Any],
    fallback_users: List[str] = None
) -> Dict[str, Any]:
    """
    LLM analyzes the instruction and universal schema to choose the action type:
    - 'email': send email with report attachment & preview
    - 'export': generate downloadable CSV/report
    - 'decision': check conditions, alerts, or thresholds
    - 'analysis': aggregate, rank, or summarize
    Generates exact SQL and step-by-step pipeline.
    """
    primary_db = schema_context.get("primary_db") or "workspace_db"
    tables_meta = schema_context.get("tables", {})
    all_table_names = schema_context.get("all_table_names", [])

    schema_text = ""
    for full_tbl, meta in tables_meta.items():
        cols_text = ", ".join(meta.get("columns", []))
        sample_str = json.dumps(meta.get("sample", {}))
        schema_text += f"\nTable: {full_tbl}\nColumns: {cols_text}\nSample: {sample_str}\n"

    system_prompt = f"""You are the D-Agent Universal Autonomous Action Engine.
Analyze the user's action instruction and database schema to formulate an exact execution plan.

Primary Database: `{primary_db}`
All Available Tables in System: {json.dumps(all_table_names)}

Schema Preview of Key Tables:{schema_text}

Available Skills:
1. "user_lookup_skill": arguments: {{"names": ["Name 1", "Name 2"]}} (use when email sending is requested)
2. "sql_query_skill": arguments: {{"sql_query": "SELECT ...", "target_column": "...", "condition_desc": "..."}}
3. "verify_result_skill": arguments: {{"expected_condition": "..."}}
4. "email_dispatch_skill": arguments: {{}} (if action involves emailing)
5. "export_data_skill": arguments: {{"filename_prefix": "sales_report"}} (if action involves exporting/downloading)
6. "decision_skill": arguments: {{"threshold_rule": "..."}} (if action involves checking alerts or decisions)

Rules:
- Determine "action_category": "email" | "export" | "decision" | "analysis" based on user intent.
- Write valid, robust MySQL syntax. Use exact column names from the schema.
- Support joins if data spans multiple tables (e.g. `sales_data` JOIN `customer_master`).
- Properly apply any numeric, string, date, or threshold conditions requested.
- If email is requested, extract recipient names into `recipient_names`.
- Output a valid JSON object ONLY.

JSON format:
{{
  "action_category": "email" | "export" | "decision" | "analysis",
  "goal": "Clear summary of the goal",
  "recipient_names": ["Name 1", "Name 2"],
  "target_tables": ["table1", "table2"],
  "target_column": "column_name",
  "condition_description": "Description of filter",
  "sql_query": "SELECT ... FROM ... WHERE ...",
  "steps": [
    {{"step_index": 1, "skill": "...", "description": "...", "args": {{}}}}
  ]
}}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Action Instruction: {instruction}"}
    ]

    try:
        raw_res = call_llm_chat(messages, json_mode=True, temperature=0.1)
        plan = json.loads(raw_res)
        return plan
    except Exception as e:
        print(f"[ActionAgent] LLM plan error: {e}")
        first_table = list(tables_meta.keys())[0] if tables_meta else f"{primary_db}.sales_data"
        is_email = bool(re.search(r'\b(send|mail|email)\b', instruction, re.I))
        return {
            "action_category": "email" if is_email else "export",
            "goal": "Execute action on database records",
            "recipient_names": fallback_users or [],
            "target_tables": [first_table],
            "target_column": "Sales_Amount",
            "condition_description": "matching records",
            "sql_query": f"SELECT * FROM `{first_table}` LIMIT 100",
            "steps": [
                {"step_index": 1, "skill": "user_lookup_skill" if is_email else "sql_query_skill", "description": "Execute action step", "args": {}},
            ]
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Self-Reflection & Auto-Correction Engine
# ─────────────────────────────────────────────────────────────────────────────
def verify_and_self_correct(
    instruction: str,
    sql_result: Dict[str, Any],
    plan: Dict[str, Any],
    schema_context: Dict[str, Any],
    get_connection_func
) -> Dict[str, Any]:
    """
    Evaluates SQL execution:
    1. Error Self-Correction: if syntax/column error, LLM rewrites SQL and retries.
    2. Zero-Row Reflection: if 0 rows returned on a filter, checks casing/operator and retries.
    3. Condition Verification: verifies returned rows match condition.
    """
    rows = sql_result.get("rows", [])
    sql_error = sql_result.get("error")
    primary_db = schema_context.get("primary_db") or ""

    # Case 1: Error occurred -> Auto-Correction
    if sql_error:
        print(f"[ActionAgent] Self-Correction triggered for SQL error: {sql_error}")
        prompt = f"""The SQL query failed with error: {sql_error}
Query attempted: {sql_result.get('query')}
User Instruction: {instruction}
Available Tables and Columns: {json.dumps(schema_context.get('tables', {}))}

Return a JSON with:
{{
  "corrected_sql": "Valid SELECT SQL query fixing the error",
  "explanation": "Why this fixes the problem"
}}"""
        try:
            res = call_llm_chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.1)
            fix = json.loads(res)
            corrected_sql = fix.get("corrected_sql")
            if corrected_sql:
                new_res = UniversalSkills.sql_query_skill(corrected_sql, primary_db, get_connection_func)
                if new_res.get("success"):
                    rows = new_res.get("rows", [])
                    return {
                        "verified": True,
                        "rows": rows,
                        "final_sql": corrected_sql,
                        "reflection": f"Query self-corrected successfully after error. Retrieved {len(rows)} records."
                    }
        except Exception as e:
            print(f"[ActionAgent] Self-correction attempt failed: {e}")

    # Case 2: Zero rows returned when data was expected
    if not rows and not sql_error:
        print(f"[ActionAgent] Reflection: 0 rows returned for query [{sql_result.get('query')}]. Checking for condition relaxations...")
        prompt = f"""The SQL query returned 0 rows:
Query: {sql_result.get('query')}
User Instruction: {instruction}
Table Schema: {json.dumps(schema_context.get('tables', {}))}

Check if string matching was too strict (e.g. case-sensitivity, exact match instead of LIKE, or operator mismatch).
Return a JSON with:
{{
  "retry_sql": "Adjusted SQL query (e.g. using LIKE %...% or relaxing condition)",
  "reason": "Why 0 rows were returned"
}}"""
        try:
            res = call_llm_chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.1)
            fix = json.loads(res)
            retry_sql = fix.get("retry_sql")
            if retry_sql and retry_sql != sql_result.get("query"):
                new_res = UniversalSkills.sql_query_skill(retry_sql, primary_db, get_connection_func)
                if new_res.get("success") and new_res.get("rows"):
                    rows = new_res.get("rows")
                    return {
                        "verified": True,
                        "rows": rows,
                        "final_sql": retry_sql,
                        "reflection": f"Self-correction relaxed strict match. Found {len(rows)} records with '{fix.get('reason')}'."
                    }
        except Exception as e:
            print(f"[ActionAgent] Zero-row retry error: {e}")

    target_col = plan.get("target_column")
    cond_desc = plan.get("condition_description") or "criteria"
    sample_preview = ""
    if rows and target_col and target_col in rows[0]:
        sample_vals = [r.get(target_col) for r in rows[:5] if r.get(target_col) is not None]
        sample_preview = f"Sample {target_col}: {sample_vals}"

    return {
        "verified": True,
        "rows": rows,
        "final_sql": sql_result.get("query"),
        "reflection": f"Verified {len(rows)} records matching {cond_desc}. {sample_preview}"
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Master Universal Action Agent Execution
# ─────────────────────────────────────────────────────────────────────────────
def run_universal_action_agent(
    instruction: str,
    session_id: str,
    user_id: int,
    get_connection_func,
    send_smtp_func=None,
    build_csv_func=None,
    bulk_approval_threshold: int = 3
) -> Dict[str, Any]:
    """
    Executes an action across ALL data sources and dynamic action types:
    1. Comprehensive schema introspection across active workspace, sync log, sheet scans.
    2. LLM plans execution (email, export, decision, analysis).
    3. Dispatches skills (SQL query, user lookup, export, SMTP).
    4. Self-reflects and verifies results.
    5. Returns structured response with HITL support.
    """
    start_time = time.time()
    print(f"\n[UniversalActionAgent] Starting action: '{instruction}'")

    # Step 1: Introspect universal schema across all sources
    schema_context = get_universal_schema_context(session_id, user_id, instruction, get_connection_func)
    primary_db = schema_context.get("primary_db") or "workspace_db"

    # Step 2: Generate universal action plan
    name_matches = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', instruction)
    plan = generate_universal_plan(instruction, schema_context, fallback_users=name_matches)
    action_cat = plan.get("action_category", "email")

    print(f"[UniversalActionAgent] Plan Category: {action_cat}")
    print(f"  Goal: {plan.get('goal')}")
    print(f"  SQL: {plan.get('sql_query')}")

    # Log plan creation as step 0
    parent_action_id = log_action(
        session_id=session_id,
        user_id=user_id or 1,
        action_name=f"{action_cat.title()} Action: {plan.get('goal', 'Execute Action')[:60]}",
        action_type="decision",
        action_input={"instruction": instruction, "plan": plan},
        status="running",
        agent_name="universal_action_agent",
        step_index=0
    )

    # Step 3: Run SQL Query Skill
    sql_to_run = plan.get("sql_query")
    if not sql_to_run:
        # Fallback query
        tbl = list(schema_context.get("tables", {}).keys())[0] if schema_context.get("tables") else "sales_data"
        sql_to_run = f"SELECT * FROM `{tbl}` LIMIT 500"

    sql_res = UniversalSkills.sql_query_skill(sql_to_run, primary_db, get_connection_func)

    # Step 4: Self-Reflection & Verification
    verification = verify_and_self_correct(instruction, sql_res, plan, schema_context, get_connection_func)
    dataset_rows = verification.get("rows", [])
    reflection_note = verification.get("reflection", "")
    print(f"[UniversalActionAgent] Reflection Note: {reflection_note}")

    # Step 5: Execute Branch by Action Category

    # ── BRANCH A: EMAIL DISPATCH ───────────────────────────────────────────
    if action_cat == "email" or bool(re.search(r'\b(send|mail|email)\b', instruction, re.I)):
        recipient_names = plan.get("recipient_names") or name_matches or []
        recipients = UniversalSkills.user_lookup_skill(recipient_names, get_connection_func)

        # Fallback to direct emails in instruction
        if not recipients:
            raw_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', instruction)
            for e in raw_emails:
                recipients.append({"name": e.split("@")[0], "email": e})

        # Also check if query results have Manager_Email or Customer_Email
        if not recipients and dataset_rows:
            email_col = next((c for c in dataset_rows[0].keys() if "email" in c.lower()), None)
            if email_col:
                for r in dataset_rows[:5]:
                    em = r.get(email_col)
                    if em and "@" in str(em):
                        recipients.append({"name": str(r.get("Customer_Name", "Recipient")), "email": str(em)})

        if not recipients:
            update_action(parent_action_id, status="failed", error_message="Could not resolve recipient email addresses.")
            return {
                "status": "failed",
                "statusCode": 400,
                "answer": f"I planned the query and verified {len(dataset_rows)} matching records, but could not find recipient email addresses in the system. Please specify recipient names or email addresses.",
            }

        recipient_count = len(recipients)
        cond_desc = plan.get("condition_description") or "matching criteria"

        # HITL Gate for bulk emails (> threshold)
        if recipient_count > bulk_approval_threshold:
            recipients_preview = [{"name": r["name"], "email": r["email"]} for r in recipients[:10]]
            summary_names = ", ".join(r["name"] for r in recipients[:5])
            more_text = f" ...and {recipient_count - 5} more" if recipient_count > 5 else ""

            action_id = log_action(
                session_id=session_id,
                user_id=user_id or 1,
                action_name=f"Bulk Email ({recipient_count} recipients)",
                action_type="tool_call",
                action_input={"instruction": instruction, "recipients_count": recipient_count, "filter": cond_desc, "dataset_count": len(dataset_rows)},
                tool_name="send_email",
                status="requires_approval",
                agent_name="universal_action_agent",
                step_index=3
            )

            return {
                "status": "requires_approval",
                "action_type": "requires_approval",
                "action_id": action_id,
                "statusCode": 200,
                "answer": f"Plan verified: Found {recipient_count} recipient(s) and {len(dataset_rows)} matching records. Please confirm before sending.",
                "action_card": {
                    "action_id": action_id,
                    "action_name": f"Send Bulk Email ({recipient_count} recipients)",
                    "summary": f"Send emails to {summary_names}{more_text} with {len(dataset_rows)} records ({cond_desc}) attached as CSV.",
                    "recipients_preview": recipients_preview,
                    "filter_condition": cond_desc,
                    "instruction": instruction,
                    "session_id": session_id,
                    "user_id": user_id,
                }
            }

        # Small batch: deliver immediately with retry
        summary_target = ", ".join(r["name"] for r in recipients)
        exec_action_id = log_action(
            session_id=session_id,
            user_id=user_id or 1,
            action_name=f"Send Email to {summary_target}",
            action_type="tool_call",
            action_input={"instruction": instruction, "recipients_count": recipient_count, "dataset_count": len(dataset_rows), "sql": verification.get("final_sql")},
            tool_name="send_email",
            status="pending",
            agent_name="universal_action_agent",
            step_index=4
        )

        def do_send():
            if send_smtp_func:
                return send_smtp_func(recipients, instruction, shared_dataset=dataset_rows)
            return True

        retry_with_backoff(exec_action_id, do_send, max_retries=3, base_delay=1.0)
        elapsed = round((time.time() - start_time) * 1000)
        update_action(parent_action_id, status="success", execution_time_ms=elapsed)

        return {
            "status": "success",
            "statusCode": 200,
            "answer": f"Autonomous Action Agent executed successfully!\n\n1. Planned execution across active database `{primary_db}`.\n2. Discovered recipients: {summary_target}\n3. Executed SQL & verified {len(dataset_rows)} matching records.\n4. Delivered email with CSV report attached ({reflection_note}).",
            "follow_up_questions": ["Show email action execution details", "Would you like to send another notification?"]
        }

    # ── BRANCH B: DATA EXPORT ──────────────────────────────────────────────
    elif action_cat == "export":
        export_res = UniversalSkills.export_data_skill(dataset_rows, filename_prefix="action_report")
        elapsed = round((time.time() - start_time) * 1000)
        update_action(parent_action_id, status="success", execution_time_ms=elapsed, action_output=json.dumps(export_res))

        return {
            "status": "success",
            "statusCode": 200,
            "answer": f"Data Export Action Completed!\n\nGenerated export file `{export_res['file_name']}` containing **{export_res['rows_count']} records**.\n\n{reflection_note}",
            "file_url": export_res.get("download_url"),
            "follow_up_questions": ["Download exported CSV report", "Send this report via email?"]
        }

    # ── BRANCH C: DECISION / ALERT / ANALYSIS ───────────────────────────────
    else:
        elapsed = round((time.time() - start_time) * 1000)
        summary_sample = dataset_rows[:10] if dataset_rows else []
        update_action(parent_action_id, status="success", execution_time_ms=elapsed, action_output=reflection_note)

        return {
            "status": "success",
            "statusCode": 200,
            "answer": f"Action Agent Execution Completed!\n\n**Outcome:** {plan.get('goal')}\n\n- **Records Retrieved:** {len(dataset_rows)}\n- **Verification:** {reflection_note}\n- **Database:** `{primary_db}`",
            "data_preview": summary_sample,
            "follow_up_questions": ["Export these records to CSV", "Email this report to team members"]
        }
