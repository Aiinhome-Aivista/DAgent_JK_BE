"""
Email Action Handler (v3 - Multi-Recipient, Robust Filter, CSV Attachment & HTML Preview)
========================================================================================
Handles on-demand email sending triggered by chat instructions or agent actions.
Supports:
1. Dynamic DB User Lookup: finds all mentioned users (e.g. Subhajit Maity AND Anwesha Das).
2. Uploaded CSV / Session Dataset Lookup & Filtered Bulk Mailing.
3. Raw email addresses (e.g., user@domain.com).
4. Robust numeric filter matching (supports '>', '>than', '>=', '<=', '<', '<than', 'more than', etc.).
5. Full dataset CSV attachment (sales_data_report.csv) with Excel UTF-8-BOM support.
6. Clean HTML email summary card + 5-row preview table.
7. HITL Approval Gate for bulk emails (>3 recipients).
8. Exponential Backoff Retry via action_state_machine.
"""

import re
import csv
import html
import io
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from database.config import MAIL_SERVER, MAIL_PORT, MAIL_USE_TLS, MAIL_USERNAME, MAIL_PASSWORD
from database.agent_action_service import log_action, update_action
from helper.action_tool_registry import register_action_tool
from helper.action_state_machine import retry_with_backoff, transition, ActionState
from helper.action_agent_planner import run_universal_action_agent

# ── Approval threshold: bulk email triggers HITL if > this many recipients ──
BULK_APPROVAL_THRESHOLD = 3


def is_email_action_request(question: str) -> bool:
    """Returns True if the question is an action request (email, export, alert, dispatch)."""
    if not question:
        return False
    pattern = (
        r'\b(send|mail|email)\b.*\b(to|mail|email)\b|'
        r'\b(send\s+email|send\s+mail|email\s+to|mail\s+to)\b|'
        r'\b(export|download|generate\s+report)\b.*\b(sales|data|customer|records|orders)\b|'
        r'\b(alert|notify)\b.*\b(manager|admin|user|team)\b'
    )
    return bool(re.search(pattern, question, re.I))


def _is_numeric_val(val):
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return True
    try:
        clean = re.sub(r'[^\d.]', '', str(val))
        return len(clean) > 0 and float(clean) is not None
    except (ValueError, TypeError):
        return False


def parse_numeric_filter(question: str):
    """
    Parses condition from instruction like:
    'sales below 50000', 'sales amount is >than 50000', 'sales amount is > than 50000', 'sales < 50000', 'amount > 10000', 'profit >= 20000'
    Returns (operator, target_value) or None
    """
    if not question:
        return None
    op_pattern = r'(>=|<=|>\s*than|<\s*than|>|<|=|\bgreater\s+than\b|\bless\s+than\b|\bmore\s+than\b|\bhigher\s+than\b|\babove\b|\bbelow\b|\bunder\b|\bexceeding\b)'
    match = re.search(op_pattern + r'\s*(\d+(?:\.\d+)?)', question, re.I)
    if not match:
        return None

    raw_op = re.sub(r'\s+', ' ', match.group(1).lower().strip())
    val = float(match.group(2))

    if raw_op in ['<', '<than', '< than', 'less than', 'below', 'under']:
        op = '<'
    elif raw_op in ['>', '>than', '> than', 'greater than', 'more than', 'above', 'higher than', 'exceeding']:
        op = '>'
    elif raw_op in ['>=']:
        op = '>='
    elif raw_op in ['<=']:
        op = '<='
    else:
        op = '='

    return op, val


def _find_matching_numeric_col(cols, question, sample_row):
    """
    Identifies which numeric column in cols the user is filtering on.
    Only returns a column if it actually contains numeric data.
    """
    q_lower = question.lower()
    # Filter cols to those that actually hold numeric data in sample_row
    numeric_cols = [c for c in cols if _is_numeric_val(sample_row.get(c))]
    if not numeric_cols:
        numeric_cols = cols

    # 1. Exact phrase match: e.g. "sales amount"
    for c in numeric_cols:
        clean_c = c.lower().replace('_', ' ')
        if clean_c in q_lower:
            return c

    # 2. Significant word match: e.g. "sales", "amount", "profit", "price", "quantity"
    for c in numeric_cols:
        words = [w for w in re.split(r'[_ ]+', c.lower()) if len(w) > 3]
        for w in words:
            if re.search(r'\b' + re.escape(w) + r'\b', q_lower):
                return c

    # 3. Fallback: first numeric column
    for c in numeric_cols:
        val = sample_row.get(c)
        if isinstance(val, (int, float)):
            return c

    return numeric_cols[0] if numeric_cols else (cols[0] if cols else None)


def fetch_filtered_dataset_rows(question: str, session_id: str, get_connection_func) -> list:
    """
    Scans session CSV/sheet tables and returns ALL rows that match the numeric filter
    in the instruction (or all rows if no numeric filter is specified).
    Returns list of properly populated row dicts.
    """
    dataset_rows = []
    if not get_connection_func:
        return dataset_rows

    conn = None
    cur = None
    try:
        conn = get_connection_func()
        cur = conn.cursor()
        tables_to_check = []

        # Check sheet_scans
        if session_id:
            try:
                cur.execute("SELECT table_name FROM sheet_scans WHERE session_id = %s", (session_id,))
                for r in cur.fetchall():
                    t = r["table_name"] if isinstance(r, dict) else r[0]
                    if t:
                        tables_to_check.append(t)
            except Exception:
                pass

        # Check workspace_db
        if session_id:
            try:
                cur.execute("SELECT workspace_db FROM workspaces WHERE session_id = %s", (session_id,))
                ws_row = cur.fetchone()
                if ws_row:
                    ws_db = ws_row["workspace_db"] if isinstance(ws_row, dict) else ws_row[0]
                    if ws_db:
                        cur.execute(f"SHOW TABLES FROM `{ws_db}`")
                        for r in cur.fetchall():
                            t_val = list(r.values())[0] if isinstance(r, dict) else r[0]
                            tables_to_check.append(f"`{ws_db}`.`{t_val}`")
            except Exception:
                pass

        # Fallback to latest workspace if tables_to_check is empty
        if not tables_to_check:
            try:
                cur.execute("SELECT workspace_db FROM workspaces ORDER BY id DESC LIMIT 1")
                ws_row = cur.fetchone()
                if ws_row:
                    ws_db = ws_row["workspace_db"] if isinstance(ws_row, dict) else ws_row[0]
                    if ws_db:
                        cur.execute(f"SHOW TABLES FROM `{ws_db}`")
                        for r in cur.fetchall():
                            t_val = list(r.values())[0] if isinstance(r, dict) else r[0]
                            tables_to_check.append(f"`{ws_db}`.`{t_val}`")
            except Exception:
                pass

        filter_tuple = parse_numeric_filter(question)

        for table in tables_to_check:
            try:
                cur.execute(f"SELECT * FROM {table} LIMIT 2000")
                raw_rows = cur.fetchall()
                if not raw_rows:
                    continue

                col_names = [d[0] for d in cur.description] if cur.description else []
                if not col_names:
                    continue

                rows = [dict(zip(col_names, r)) if isinstance(r, (tuple, list)) else dict(r) for r in raw_rows]
                sample_row = rows[0]

                target_col = None
                if filter_tuple:
                    target_col = _find_matching_numeric_col(col_names, question, sample_row)

                for row_dict in rows:
                    matches_filter = True
                    if filter_tuple and target_col:
                        op, target_val = filter_tuple
                        raw_val = row_dict.get(target_col, 0)
                        try:
                            clean_str = re.sub(r'[^\d.]', '', str(raw_val or ''))
                            if not clean_str:
                                matches_filter = False
                            else:
                                cell_val = float(clean_str)
                                if op == '<' and not (cell_val < target_val):
                                    matches_filter = False
                                elif op == '<=' and not (cell_val <= target_val):
                                    matches_filter = False
                                elif op == '>' and not (cell_val > target_val):
                                    matches_filter = False
                                elif op == '>=' and not (cell_val >= target_val):
                                    matches_filter = False
                                elif op == '=' and not (cell_val == target_val):
                                    matches_filter = False
                        except (ValueError, TypeError):
                            matches_filter = False

                    if matches_filter:
                        dataset_rows.append(row_dict)

                if dataset_rows:
                    break  # Found data in this table
            except Exception as e:
                print(f"[EmailActionHandler] Error scanning table {table} for dataset: {e}")

    except Exception as e:
        print(f"[EmailActionHandler] fetch_filtered_dataset_rows error: {e}")
    finally:
        if cur and hasattr(cur, "close"):
            try: cur.close()
            except: pass
        if conn and hasattr(conn, "close"):
            try: conn.close()
            except: pass

    return dataset_rows


def find_csv_recipients_and_filter(question: str, session_id: str, get_connection_func):
    """
    Scans session tables (uploaded CSVs) for email columns and filters by instruction.
    Returns list of {'name', 'email', 'row_data'} dicts.
    """
    recipients = []
    if not get_connection_func:
        return recipients

    conn = None
    cur = None
    try:
        conn = get_connection_func()
        cur = conn.cursor()
        tables_to_check = []

        if session_id:
            try:
                cur.execute("SELECT table_name FROM sheet_scans WHERE session_id = %s", (session_id,))
                for r in cur.fetchall():
                    t = r["table_name"] if isinstance(r, dict) else r[0]
                    if t:
                        tables_to_check.append(t)
            except Exception:
                pass

        if session_id:
            try:
                cur.execute("SELECT workspace_db FROM workspaces WHERE session_id = %s", (session_id,))
                ws_row = cur.fetchone()
                if ws_row:
                    ws_db = ws_row["workspace_db"] if isinstance(ws_row, dict) else ws_row[0]
                    if ws_db:
                        cur.execute(f"SHOW TABLES FROM `{ws_db}`")
                        for r in cur.fetchall():
                            t_val = list(r.values())[0] if isinstance(r, dict) else r[0]
                            tables_to_check.append(f"`{ws_db}`.`{t_val}`")
            except Exception:
                pass

        filter_tuple = parse_numeric_filter(question)

        for table in tables_to_check:
            try:
                cur.execute(f"SELECT * FROM {table} LIMIT 1000")
                raw_rows = cur.fetchall()
                if not raw_rows:
                    continue

                col_names = [d[0] for d in cur.description] if cur.description else []
                if not col_names:
                    continue

                rows = [dict(zip(col_names, r)) if isinstance(r, (tuple, list)) else dict(r) for r in raw_rows]
                sample_row = rows[0]

                email_col = next((c for c in col_names if re.search(r'\b(email|mail|customer_email|dealer_email)\b', c, re.I)), None)
                name_col = next((c for c in col_names if re.search(r'\b(name|customer_name|dealer_name|client)\b', c, re.I)), None)
                if not email_col:
                    continue

                target_col = None
                if filter_tuple:
                    target_col = _find_matching_numeric_col(col_names, question, sample_row)

                for row_dict in rows:
                    raw_email = str(row_dict.get(email_col, "") or "").strip()
                    if not raw_email or "@" not in raw_email:
                        continue
                    matches_filter = True
                    if filter_tuple and target_col:
                        op, target_val = filter_tuple
                        try:
                            clean_str = re.sub(r'[^\d.]', '', str(row_dict.get(target_col, '') or ''))
                            if not clean_str:
                                matches_filter = False
                            else:
                                cell_val = float(clean_str)
                                if op == '<' and not (cell_val < target_val):
                                    matches_filter = False
                                elif op == '<=' and not (cell_val <= target_val):
                                    matches_filter = False
                                elif op == '>' and not (cell_val > target_val):
                                    matches_filter = False
                                elif op == '>=' and not (cell_val >= target_val):
                                    matches_filter = False
                                elif op == '=' and not (cell_val == target_val):
                                    matches_filter = False
                        except (ValueError, TypeError):
                            matches_filter = False
                    if matches_filter:
                        r_name = str(row_dict.get(name_col, raw_email.split("@")[0])) if name_col else raw_email.split("@")[0]
                        recipients.append({"name": r_name, "email": raw_email, "row_data": row_dict})
            except Exception as e:
                print(f"[EmailActionHandler] Error scanning table {table}: {e}")

    except Exception as e:
        print(f"[EmailActionHandler] find_csv_recipients error: {e}")
    finally:
        if cur and hasattr(cur, "close"):
            try: cur.close()
            except: pass
        if conn and hasattr(conn, "close"):
            try: conn.close()
            except: pass

    return recipients


def _build_csv_bytes(rows: list) -> bytes:
    """
    Converts a list of row dicts to UTF-8-BOM encoded CSV bytes for email attachment.
    Compatible with Microsoft Excel, Google Sheets, LibreOffice.
    """
    if not rows:
        return b""
    output = io.StringIO()
    col_keys = [k for k in rows[0].keys() if k not in ("_row_id", "id")]
    writer = csv.DictWriter(output, fieldnames=col_keys, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for r in rows:
        clean_row = {k: str(v) if v is not None else "" for k, v in r.items() if k in col_keys}
        writer.writerow(clean_row)
    return output.getvalue().encode("utf-8-sig")


def _send_smtp(recipients_list: list, question: str, shared_dataset: list = None) -> tuple:
    """
    Core SMTP sender. Returns (sent_list, failed_list).
    Attaches shared_dataset as sales_data_report.csv and renders a clean summary + preview in HTML body.
    Raises Exception on total SMTP failure so retry_with_backoff can catch it.
    """
    sent = []
    failed = []

    # Build CSV attachment bytes once
    dataset_to_use = shared_dataset or []
    csv_bytes = b""
    csv_row_count = len(dataset_to_use)
    if dataset_to_use:
        csv_bytes = _build_csv_bytes(dataset_to_use)

    for r in recipients_list:
        r_name = r["name"]
        r_email = r["email"]
        r_data = r.get("row_data") or {}

        # If recipient has individual row_data and no shared dataset
        current_csv_bytes = csv_bytes
        current_row_count = csv_row_count
        if r_data and not dataset_to_use:
            current_csv_bytes = _build_csv_bytes([r_data])
            current_row_count = 1
            dataset_to_use = [r_data]

        # Build compact 5-row HTML preview table with intelligent column selection
        preview_table_html = ""
        if dataset_to_use:
            preview_rows = dataset_to_use[:5]
            all_keys = [k for k in preview_rows[0].keys() if k not in ("_row_id", "id")]
            
            # Prioritize the most critical columns (Order, Customer, Sales_Amount, Profit, etc.)
            priority_patterns = [
                r'\b(order[_\s]?id|invoice)\b',
                r'\b(customer[_\s]?name|cname|client)\b',
                r'\b(sales[_\s]?amount|amount|invoice[_\s]?value)\b',
                r'\b(profit|margin)\b',
                r'\b(unit[_\s]?price|price)\b',
                r'\b(product|item)\b',
                r'\b(region|state|city)\b',
                r'\b(order[_\s]?date|date)\b'
            ]
            
            selected_cols = []
            for pat in priority_patterns:
                for k in all_keys:
                    if k not in selected_cols and re.search(pat, k, re.I):
                        selected_cols.append(k)
                        break
            
            for k in all_keys:
                if k not in selected_cols:
                    selected_cols.append(k)
                    
            col_keys = selected_cols[:6]
            headers = "".join(f"<th style='padding:6px 10px;background:#1e40af;color:white;text-align:left;border:1px solid #93c5fd;font-size:11px;'>{html.escape(str(k))}</th>" for k in col_keys)
            body_rows = ""
            for i, pr in enumerate(preview_rows):
                bg = "#f8fafc" if i % 2 == 0 else "#ffffff"
                cells = "".join(f"<td style='padding:5px 10px;border:1px solid #e2e8f0;background:{bg};color:#1e293b;font-size:11px;white-space:nowrap;'>{html.escape(str(pr.get(k, '') or ''))}</td>" for k in col_keys)
                body_rows += f"<tr>{cells}</tr>"

            more_note = f"<p style='margin:6px 0 0;font-size:11px;color:#64748b;font-style:italic;'>... and {len(dataset_to_use) - len(preview_rows)} more records attached in CSV</p>" if len(dataset_to_use) > len(preview_rows) else ""

            preview_table_html = f"""
            <div style='margin-top:16px;border:1px solid #cbd5e1;border-radius:8px;overflow:hidden;'>
                <div style='background:#f1f5f9;padding:8px 12px;border-bottom:1px solid #cbd5e1;font-size:12px;font-weight:600;color:#334155;'>
                    📋 Preview of Attached Data ({len(dataset_to_use)} records total)
                </div>
                <div style='overflow-x:auto;'>
                    <table style='width:100%;border-collapse:collapse;'>
                        <thead><tr>{headers}</tr></thead>
                        <tbody>{body_rows}</tbody>
                    </table>
                </div>
            </div>
            {more_note}
            """

        # Attachment callout box
        attachment_box_html = ""
        if current_row_count > 0:
            attachment_box_html = f"""
            <div style='background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;padding:14px 18px;margin-top:16px;'>
                <p style='margin:0 0 4px;color:#065f46;font-weight:700;font-size:14px;'>
                    📎 Attached File: <code style='background:#d1fae5;padding:2px 6px;border-radius:4px;'>sales_data_report.csv</code>
                </p>
                <p style='margin:0;color:#047857;font-size:12px;'>
                    Total <strong>{current_row_count} records</strong> matching your instruction are attached to this email.
                </p>
            </div>
            """

        msg = MIMEMultipart("mixed")
        msg["From"] = MAIL_USERNAME
        msg["To"] = r_email
        msg["Subject"] = "DAgent AI Sales Report Notification"

        text_body = (
            f"Hello {r_name},\n\n"
            f"Notification from DAgent AI.\n"
            f"Instruction: {question}\n\n"
            f"Total records: {current_row_count} (see attached sales_data_report.csv)\n\n"
            f"DAgent AI System"
        )

        html_body = f"""
        <html>
        <body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#f8fafc;padding:24px;margin:0;'>
            <div style='max-width:680px;margin:auto;background:#ffffff;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,0.06);border:1px solid #e2e8f0;overflow:hidden;'>
                <div style='background:linear-gradient(135deg,#1e40af,#3b82f6);padding:22px 24px;'>
                    <h2 style='color:#ffffff;margin:0;font-size:20px;letter-spacing:-0.5px;'>🤖 DAgent AI Report Notification</h2>
                    <p style='color:#dbeafe;margin:4px 0 0;font-size:12px;'>Automated Data Delivery System</p>
                </div>
                <div style='padding:24px;'>
                    <p style='color:#1e293b;font-size:15px;margin:0 0 16px;'>Hello <strong>{html.escape(r_name)}</strong>,</p>
                    <div style='background:#f0fdf4;border-left:4px solid #22c55e;padding:12px 16px;border-radius:4px;margin:0 0 16px;'>
                        <p style='margin:0;color:#166534;font-size:13px;'><strong>Instruction:</strong> {html.escape(question)}</p>
                    </div>
                    {attachment_box_html}
                    {preview_table_html}
                    <p style='color:#94a3b8;font-size:11px;margin-top:24px;border-top:1px solid #f1f5f9;padding-top:12px;text-align:center;'>
                        Generated automatically by DAgent AI System &bull; Workspace Intelligence
                    </p>
                </div>
            </div>
        </body>
        </html>
        """

        # Alternative part for text/html
        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(text_body, "plain", "utf-8"))
        alt_part.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt_part)

        # Attach CSV
        if current_csv_bytes:
            att = MIMEApplication(current_csv_bytes, _subtype="csv")
            att.add_header("Content-Disposition", "attachment", filename="sales_data_report.csv")
            msg.attach(att)

        try:
            if MAIL_USE_TLS or MAIL_PORT == 587:
                with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=15) as server:
                    server.starttls()
                    server.login(MAIL_USERNAME, MAIL_PASSWORD)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(MAIL_SERVER, MAIL_PORT, timeout=15) as server:
                    server.login(MAIL_USERNAME, MAIL_PASSWORD)
                    server.send_message(msg)
            sent.append(f"{r_name} ({r_email})")
            print(f"[EmailActionHandler] Successfully sent to {r_email} with {current_row_count} rows CSV attached")
        except Exception as e:
            print(f"[EmailActionHandler] Failed to send to {r_email}: {e}")
            failed.append(f"{r_name} ({r_email}): {e}")

    return sent, failed


# ── Tool Registry Registration ─────────────────────────────────────────────────
@register_action_tool(
    name="send_email",
    description="Sends emails to users or filtered CSV recipients. Supports bulk + HITL approval.",
    requires_approval=True,
    approval_threshold=BULK_APPROVAL_THRESHOLD,
)
def _email_tool_handler(params: dict) -> dict:
    """Internal handler used by the Tool Registry for direct dispatch."""
    return _do_execute_email(
        question=params.get("question", ""),
        session_id=params.get("session_id", ""),
        user_id=params.get("user_id", 1),
        target_recipients=params.get("target_recipients", []),
        shared_dataset=params.get("shared_dataset", []),
    )


def _do_execute_email(question: str, session_id: str, user_id: int, target_recipients: list, shared_dataset: list = None) -> dict:
    """Performs the actual email sending and returns a result dict."""
    sent_emails, failed_emails = _send_smtp(target_recipients, question, shared_dataset=shared_dataset)

    if sent_emails:
        recipients_list_str = "\n".join([f"- {s}" for s in sent_emails])
        dataset_info = f" with {len(shared_dataset)} records attached as sales_data_report.csv" if shared_dataset else ""
        return {
            "status": "success",
            "statusCode": 200,
            "answer": f"Email Action Executed Successfully!\n\nSent emails to {len(sent_emails)} recipient(s){dataset_info}:\n\n{recipients_list_str}",
            "follow_up_questions": ["Show email action execution summary", "Would you like to send another notification?"],
        }
    else:
        raise Exception("; ".join(failed_emails) if failed_emails else "All email attempts failed")


def execute_email_action(
    question: str,
    session_id: str = "default_session",
    user_id: int = 1,
    get_connection_func=None,
) -> dict:
    """
    Main entry point for Action Agent.
    Executes an autonomous LLM workflow:
    1. LLM plans execution step-by-step
    2. Uses Database SQL and User Lookup skills
    3. Self-reflects and verifies that retrieved data matches user conditions
    4. Handles HITL approval and SMTP delivery
    """
    # ── 1. Universal Autonomous LLM Agent Execution with Multi-Data & Reflection ──
    try:
        agent_res = run_universal_action_agent(
            instruction=question,
            session_id=session_id,
            user_id=user_id,
            get_connection_func=get_connection_func,
            send_smtp_func=_send_smtp,
            build_csv_func=_build_csv_bytes,
            bulk_approval_threshold=BULK_APPROVAL_THRESHOLD
        )
        if agent_res and agent_res.get("status") in ("success", "requires_approval", "failed"):
            return agent_res
    except Exception as e:
        print(f"[EmailActionHandler] Universal agent error fallback: {e}")

    # ── 2. Procedural Fallback if LLM engine encounters unexpected error ──
    target_recipients = []

    # 1. First check DB user lookup: match all mentioned users in question
    if get_connection_func:
        conn = None
        cur = None
        try:
            conn = get_connection_func()
            cur = conn.cursor()
            cur.execute("SELECT id, name, email FROM users")
            u_cols = [d[0] for d in cur.description] if cur.description else ["id", "name", "email"]
            all_users = [dict(zip(u_cols, r)) if isinstance(r, (tuple, list)) else dict(r) for r in cur.fetchall()]

            q_lower = question.lower()
            matched_users = []

            # Match full name first (e.g. "Subhajit Maity", "Anwesha Das")
            for u in all_users:
                u_name = (u.get("name") or "").strip()
                if u_name and u_name.lower() in q_lower:
                    matched_users.append(u)

            # Match first name if not already matched
            if not matched_users:
                for u in all_users:
                    u_name = (u.get("name") or "").strip()
                    first_name = u_name.split()[0].lower() if u_name else ""
                    if len(first_name) > 2 and re.search(r'\b' + re.escape(first_name) + r'\b', q_lower):
                        matched_users.append(u)

            for u in matched_users:
                u_email = (u.get("email") or "").strip()
                u_name = (u.get("name") or "").strip()
                if u_email and "@" in u_email:
                    target_recipients.append({"name": u_name, "email": u_email, "row_data": {}})
        except Exception as e:
            print(f"[EmailActionHandler] User lookup error: {e}")
        finally:
            if cur and hasattr(cur, "close"):
                try: cur.close()
                except: pass
            if conn and hasattr(conn, "close"):
                try: conn.close()
                except: pass

    # 2. If no DB users matched, check CSV dataset recipients
    if not target_recipients:
        csv_recipients = find_csv_recipients_and_filter(question, session_id, get_connection_func)
        if csv_recipients:
            target_recipients.extend(csv_recipients)

    # 3. Direct email addresses in question
    if not target_recipients:
        email_matches = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', question)
        for e in email_matches:
            target_recipients.append({"name": e.split("@")[0], "email": e, "row_data": {}})

    # Deduplicate by email
    unique = {}
    for r in target_recipients:
        unique[r["email"]] = r
    target_recipients = list(unique.values())

    if not target_recipients:
        return {
            "status": "failed",
            "statusCode": 400,
            "answer": "I understood you want to send emails, but no matching recipients were found in the database or dataset.",
        }

    # Fetch dataset rows to attach as CSV file
    shared_dataset = []
    if get_connection_func:
        shared_dataset = fetch_filtered_dataset_rows(question, session_id, get_connection_func)
        print(f"[EmailActionHandler] Fetched {len(shared_dataset)} dataset rows for CSV attachment")

    # 4. HITL gate: bulk emails require approval
    recipient_count = len(target_recipients)
    filter_tuple = parse_numeric_filter(question)
    filter_desc = f"{filter_tuple[0]} {int(filter_tuple[1])}" if filter_tuple else "all matching"

    if recipient_count > BULK_APPROVAL_THRESHOLD:
        recipients_preview = [{"name": r["name"], "email": r["email"]} for r in target_recipients[:10]]
        summary_names = ", ".join(r["name"] for r in target_recipients[:5])
        more_text = f" ...and {recipient_count - 5} more" if recipient_count > 5 else ""

        action_id = log_action(
            session_id=session_id,
            user_id=user_id or 1,
            action_name=f"Bulk Email - {recipient_count} recipients",
            action_type="tool_call",
            action_input={"instruction": question, "recipients_count": recipient_count, "filter": filter_desc, "dataset_count": len(shared_dataset)},
            tool_name="send_email",
            status="requires_approval",
            agent_name="email_action_agent"
        )

        return {
            "status": "requires_approval",
            "action_type": "requires_approval",
            "action_id": action_id,
            "statusCode": 200,
            "answer": f"I found {recipient_count} recipients and {len(shared_dataset)} records matching your criteria. Please review and confirm before sending.",
            "action_card": {
                "action_id": action_id,
                "action_name": f"Send Bulk Email ({recipient_count} recipients)",
                "summary": f"Send emails to {recipient_count} recipients: {summary_names}{more_text} with {len(shared_dataset)} records attached as CSV.",
                "recipients_preview": recipients_preview,
                "filter_condition": filter_desc,
                "instruction": question,
                "session_id": session_id,
                "user_id": user_id,
            }
        }

    # 5. Small batch (<= threshold): send immediately with retry
    summary_target = ", ".join(r["name"] for r in target_recipients)
    action_id = log_action(
        session_id=session_id,
        user_id=user_id or 1,
        action_name=f"Send Email to {summary_target}",
        action_type="tool_call",
        action_input={"instruction": question, "recipients_count": recipient_count, "dataset_count": len(shared_dataset)},
        tool_name="send_email",
        status="pending",
        agent_name="email_action_agent"
    )

    captured_recipients = target_recipients[:]
    captured_dataset = shared_dataset[:]
    captured_question = question

    def do_send():
        result = _do_execute_email(captured_question, session_id, user_id, captured_recipients, shared_dataset=captured_dataset)
        return result

    retry_with_backoff(action_id, do_send, max_retries=3, base_delay=1.0)

    dataset_note = f" with {len(shared_dataset)} records attached as sales_data_report.csv" if shared_dataset else ""
    return {
        "status": "success",
        "statusCode": 200,
        "answer": f"Sending emails to {recipient_count} recipient(s): {summary_target}{dataset_note}. Delivery is in progress with automatic retry support.",
        "follow_up_questions": ["Show email action status", "Would you like to send another notification?"]
    }
