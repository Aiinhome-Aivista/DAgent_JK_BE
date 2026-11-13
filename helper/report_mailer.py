import datetime
import re
import html as html_lib
from database.db_connection import get_db_connection
from database.config import MAIL_SERVER, MAIL_PORT, MAIL_USE_TLS, MAIL_USERNAME, MAIL_PASSWORD
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from helper.pdf_generator import generate_pdf_report
import json

def get_report_data(report_id):
    """
    Placeholder function to get data for a specific report.
    You will replace the logic here with your actual SQL queries.
    """
    # Simulate a generic data fetch based on report ID
    data = [
        {"Category": "North Zone", "Sales": "5.4 Cr", "Target": "5.0 Cr"},
        {"Category": "South Zone", "Sales": "4.1 Cr", "Target": "4.5 Cr"},
        {"Category": "East Zone", "Sales": "2.8 Cr", "Target": "3.0 Cr"},
        {"Category": "West Zone", "Sales": "6.2 Cr", "Target": "5.5 Cr"}
    ]
    return data

def fill_excel_sheet(sheet, data, start_row=2, start_col=1):
    """Dynamically fills an openpyxl sheet with data from a list of dictionaries."""
    if not data:
        return
    
    # Optional: If you want to automatically write headers on row 1, you can do it here.
    # But since you have a template, we assume row 1 has your custom headers.
    
    # Get the column keys from the first row of data
    columns = list(data[0].keys())
    
    for r_idx, row_dict in enumerate(data):
        for c_idx, col_name in enumerate(columns):
            sheet.cell(row=start_row + r_idx, column=start_col + c_idx, value=row_dict.get(col_name))

def generate_excel_report(report_ids_json, workspace_db):
    """
    Executes the SP and creates the Excel file dynamically.
    Returns the path to the generated temp file, or None on failure.
    """
    template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "BW Sales Summary HO x3A 1237422.xlsx")
    if not os.path.exists(template_path):
        print(f"[Mailer Error] Template not found at: {template_path}")
        return None
        
    try:
        import pymysql
        from database.config import MYSQL_CONFIG
        conn = pymysql.connect(
            host=MYSQL_CONFIG["host"],
            user=MYSQL_CONFIG["user"],
            password=MYSQL_CONFIG["password"],
            database=workspace_db,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )
        cursor = conn.cursor()
        
        # 1. Calculate dynamic dates for the current month
        now = datetime.datetime.now()
        p_from = now.strftime('%Y-%m-01')
        p_to = now.strftime('%Y-%m-%d')
        p_prev_from = (now.replace(year=now.year-1)).strftime('%Y-%m-01')
        p_prev_to = (now.replace(year=now.year-1)).strftime('%Y-%m-%d')
        
        # 2. Call the Stored Procedure
        print(f"[Excel Generation] Executing SP for DB {workspace_db}...", flush=True)
        cursor.execute("CALL sp_regionwise_sales_values(%s, %s, %s, %s)", (p_from, p_to, p_prev_from, p_prev_to))
        rows = cursor.fetchall()
        print(f"[Excel Generation] SP finished, returned {len(rows)} rows.", flush=True)
        
        # 3. Load Excel Template
        print(f"[Excel Generation] Loading Excel template from {template_path}...", flush=True)
        wb = openpyxl.load_workbook(template_path)
        
        # 4. Dynamically fill the first sheet (index 0) starting at Row 2, Column A
        print(f"[Excel Generation] Filling first sheet with data...", flush=True)
        sheet1 = wb.worksheets[0]
        fill_excel_sheet(sheet1, rows, start_row=2, start_col=1)
        
        # Note: If you need to fill Sheet 2 or Sheet 3 with different data later, 
        # you can execute another SP and call `fill_excel_sheet(wb.worksheets[1], new_data)`
            
        # 5. Save temp file
        temp_filename = f"Sales_Summary_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
        temp_filepath = os.path.join(os.path.dirname(__file__), "..", "templates", temp_filename)
        print(f"[Excel Generation] Saving temporary Excel file to {temp_filepath}...", flush=True)
        wb.save(temp_filepath)
        
        return temp_filepath
        
    except Exception as e:
        print(f"[Mailer Error] Excel generation failed: {e}")
        return None
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()

def _fetch_session_analysis_summary(conn, session_id: str = None, workspace_id: int = None) -> str:
    """
    Fetches the latest Executive Analysis Summary markdown.
    Multi-level guaranteed resolution:
      1. session_analysis_cache for session_id
      2. session_chat_history for session_id (default_% or Executive Summary turn)
      3. Auto-resolve session_id from workspace_id or workspaces table
      4. Global fallback to the latest available summary in session_analysis_cache or session_chat_history
      5. Live dynamic generation if no summary exists anywhere
    """
    if not conn:
        return ""
    cur = None
    raw = None
    try:
        cur = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()

        # 1. Direct query on session_analysis_cache by session_id
        if session_id and str(session_id).strip() not in ("", "null", "None"):
            try:
                cur.execute(
                    """SELECT report FROM session_analysis_cache 
                       WHERE session_id = %s AND report IS NOT NULL AND report != '' 
                       ORDER BY id DESC LIMIT 1""",
                    (str(session_id).strip(),)
                )
                row = cur.fetchone()
                if row:
                    raw = row.get("report") if isinstance(row, dict) else row[0]
            except Exception as e1:
                print(f"[Mailer] Cache query error: {e1}", flush=True)

            # 2. Check session_chat_history for this session_id
            if not raw:
                try:
                    cur.execute(
                        """SELECT answer FROM session_chat_history 
                           WHERE session_id = %s 
                             AND (question LIKE 'default_%%' OR answer LIKE '%%Executive Summary%%' OR (visit_number = 1 AND turn_index = 0))
                             AND answer IS NOT NULL AND answer != ''
                           ORDER BY id DESC LIMIT 1""",
                        (str(session_id).strip(),)
                    )
                    row = cur.fetchone()
                    if row:
                        raw = row.get("answer") if isinstance(row, dict) else row[0]
                except Exception as e2:
                    print(f"[Mailer] Chat history query error: {e2}", flush=True)

        # 3. Resolve session_id from workspace_id if raw is still empty
        resolved_sid = None
        if not raw and workspace_id:
            try:
                cur.execute("SELECT session_id FROM workspaces WHERE id = %s AND session_id IS NOT NULL AND session_id != '' LIMIT 1", (workspace_id,))
                w_row = cur.fetchone()
                if w_row:
                    resolved_sid = w_row.get("session_id") if isinstance(w_row, dict) else w_row[0]
                    if resolved_sid and resolved_sid != session_id:
                        cur.execute(
                            """SELECT report FROM session_analysis_cache 
                               WHERE session_id = %s AND report IS NOT NULL AND report != '' 
                               ORDER BY id DESC LIMIT 1""",
                            (resolved_sid,)
                        )
                        row = cur.fetchone()
                        if row:
                            raw = row.get("report") if isinstance(row, dict) else row[0]
                        if not raw:
                            cur.execute(
                                """SELECT answer FROM session_chat_history 
                                   WHERE session_id = %s 
                                     AND (question LIKE 'default_%%' OR answer LIKE '%%Executive Summary%%' OR (visit_number = 1 AND turn_index = 0))
                                     AND answer IS NOT NULL AND answer != ''
                                   ORDER BY id DESC LIMIT 1""",
                                (resolved_sid,)
                            )
                            row = cur.fetchone()
                            if row:
                                raw = row.get("answer") if isinstance(row, dict) else row[0]
            except Exception as w_err:
                print(f"[Mailer] Workspace session lookup note: {w_err}", flush=True)

        # 4. Global fallback: get the latest summary from ANY session in session_analysis_cache
        if not raw:
            try:
                cur.execute(
                    """SELECT report FROM session_analysis_cache 
                       WHERE report IS NOT NULL AND report != '' 
                       ORDER BY id DESC LIMIT 1"""
                )
                row = cur.fetchone()
                if row:
                    raw = row.get("report") if isinstance(row, dict) else row[0]
            except Exception as e4:
                print(f"[Mailer] Global cache query error: {e4}", flush=True)

        # 5. Global fallback: get the latest summary from ANY session in session_chat_history
        if not raw:
            try:
                cur.execute(
                    """SELECT answer FROM session_chat_history 
                       WHERE (question LIKE 'default_%%' OR answer LIKE '%%Sales performance analysis%%' OR answer LIKE '%%Performance Analysis%%' OR answer LIKE '%%Executive Summary%%' OR (visit_number = 1 AND turn_index = 0))
                         AND answer IS NOT NULL AND answer != ''
                       ORDER BY id DESC LIMIT 1"""
                )
                row = cur.fetchone()
                if row:
                    raw = row.get("answer") if isinstance(row, dict) else row[0]
            except Exception as e5:
                print(f"[Mailer] Global chat history query error: {e5}", flush=True)

        # 6. If STILL empty, dynamically generate it!
        if not raw:
            target_sid = session_id or resolved_sid or "default_13_11_2026"
            try:
                from controllers.session_analysis_controller import run_daily_session_analysis_scheduler
                print(f"[Mailer] No summary found anywhere. Generating fresh analysis for session {target_sid}...", flush=True)
                run_daily_session_analysis_scheduler(get_db_connection, session_id=target_sid)
                cur.execute(
                    """SELECT report FROM session_analysis_cache 
                       WHERE session_id = %s AND report IS NOT NULL AND report != '' 
                       ORDER BY id DESC LIMIT 1""",
                    (target_sid,)
                )
                row = cur.fetchone()
                if row:
                    raw = row.get("report") if isinstance(row, dict) else row[0]
            except Exception as gen_err:
                print(f"[Mailer] Dynamic summary generation fallback note: {gen_err}", flush=True)

    except Exception as e:
        print(f"[Mailer] Could not fetch session analysis summary: {e}", flush=True)
    finally:
        if cur:
            try: cur.close()
            except: pass

    if not raw or len(str(raw).strip()) < 50:
        try:
            from helper.pdf_generator import _get_default_verified_summary
            return _get_default_verified_summary()
        except Exception:
            return ""

    # If raw is a dict or stringified JSON, extract the text
    if isinstance(raw, dict):
        raw = raw.get("report") or raw.get("answer") or str(raw)
    elif isinstance(raw, str) and raw.strip().startswith("{") and raw.strip().endswith("}"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                raw = parsed.get("report") or parsed.get("answer") or raw
        except Exception:
            pass

    text = str(raw).strip()

    # Unescape literal backslash-n if escaped as \\n in json
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")

    # Strip Coverage Gaps / Strategic Conclusion if present (matching UI filter)
    text = re.sub(
        r'(?:^|\n)\s*(?:###\s*|\*\*)?(?:\d+\.\s*)?Coverage\s+Gaps\b[^\n]*(?:\*\*)?:?[\s\S]*?(?=(?:\n\s*(?:###|\*\*\d+\.|\*\*Business Risks|\*\*Actionable Recommendations))|$)',
        '\n',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'(?:^|\n)\s*(?:###\s*|\*\*)?Strategic\s+Conclusion(?:\*\*)?:?[\s\S]*?(?=(?:\n\s*###|$))',
        '\n',
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _format_summary_to_email_html(report_text: str) -> str:
    """Converts the analysis summary markdown into clean inline-styled HTML for emails."""
    if not report_text:
        return ""

    def fmt_inline(line: str) -> str:
        s = html_lib.escape(line, quote=False)
        # Protect literal asterisks inside SKUs/identifiers (e.g. E2_***_TL)
        s = re.sub(r'([A-Za-z0-9_])(\*+)(?=[A-Za-z0-9_])', lambda m: m.group(1) + '&#42;' * len(m.group(2)), s)
        s = re.sub(r'_(\*{3,})', lambda m: '_' + '&#42;' * len(m.group(1)), s)
        s = re.sub(r'\*\*([^\s*](?:[^*\n]*?[^\s*])?)\*\*', r'<strong style="color:#0f172a;">\1</strong>', s)
        s = re.sub(r'\*([^\s*](?:[^*\n]*?[^\s*])?)\*', r'<em>\1</em>', s)
        return s

    parts = ['<div style="margin-top:24px;padding-top:20px;border-top:2px solid #e2e8f0;color:#334155;font-size:14px;line-height:1.6;">']
    in_list = False
    in_table = False
    table_rows = []

    def close_list():
        nonlocal in_list
        if in_list:
            parts.append('</ul>')
            in_list = False

    def close_table():
        nonlocal in_table, table_rows
        if in_table and table_rows:
            html_table = ['<table style="width:100%; border-collapse:collapse; margin:14px 0 18px 0; font-size:13px; border:1px solid #cbd5e1; border-radius:6px; overflow:hidden;">']
            for idx, r in enumerate(table_rows):
                cols = [c.strip() for c in r.strip('|').split('|')]
                if not any(cols):
                    continue
                # Skip divider rows like |---|---|
                if all(re.match(r'^:?-+:?$', c) for c in cols if c):
                    continue
                if idx == 0:
                    html_table.append('<tr style="background-color:#1e293b; color:#ffffff; font-weight:600; text-align:left;">')
                    for c in cols:
                        html_table.append(f'<th style="padding:10px 12px; border:1px solid #334155;">{fmt_inline(c)}</th>')
                    html_table.append('</tr>')
                else:
                    bg = '#f8fafc' if idx % 2 == 1 else '#ffffff'
                    html_table.append(f'<tr style="background-color:{bg}; border-bottom:1px solid #e2e8f0;">')
                    for c in cols:
                        html_table.append(f'<td style="padding:8px 12px; border:1px solid #cbd5e1; color:#334155;">{fmt_inline(c)}</td>')
                    html_table.append('</tr>')
            html_table.append('</table>')
            parts.append(''.join(html_table))
            table_rows = []
            in_table = False

    for raw_line in report_text.split('\n'):
        t = raw_line.strip()
        if not t:
            close_list()
            close_table()
            continue

        # Handle Markdown Tables
        if t.startswith('|') and t.endswith('|'):
            close_list()
            in_table = True
            table_rows.append(t)
            continue
        elif in_table:
            close_table()

        if t.upper().startswith('TITLE:'):
            close_list()
            title_txt = fmt_inline(t[6:].strip())
            parts.append(
                f'<div style="background-color:#f1f5f9; border-left:4px solid #2563eb; padding:14px 18px; margin:0 0 16px 0; border-radius:4px;">'
                f'<h2 style="margin:0; font-size:18px; color:#0f172a; font-weight:700; line-height:1.4;">{title_txt}</h2>'
                f'</div>'
            )
        elif t.startswith('#### '):
            close_list()
            parts.append(f'<h4 style="margin:16px 0 8px 0; font-size:14px; color:#1e293b; font-weight:600;">{fmt_inline(t[5:])}</h4>')
        elif t.startswith('### '):
            close_list()
            parts.append(
                f'<h3 style="margin:22px 0 10px 0; font-size:16px; color:#0f172a; border-bottom:2px solid #e2e8f0; padding-bottom:6px; font-weight:700;">'
                f'{fmt_inline(t[4:])}'
                f'</h3>'
            )
        elif t.startswith('## ') or t.startswith('# '):
            close_list()
            heading = t.lstrip('#').strip()
            parts.append(
                f'<h3 style="margin:22px 0 10px 0; font-size:16px; color:#0f172a; border-bottom:2px solid #e2e8f0; padding-bottom:6px; font-weight:700;">'
                f'{fmt_inline(heading)}'
                f'</h3>'
            )
        elif t.startswith('- ') or t.startswith('* ') or t.startswith('• '):
            if not in_list:
                parts.append('<ul style="margin:8px 0 12px 0; padding-left:22px; list-style-type:disc;">')
                in_list = True
            parts.append(f'<li style="margin-bottom:6px; color:#334155; line-height:1.6;">{fmt_inline(t[2:].strip())}</li>')
        else:
            close_list()
            parts.append(f'<p style="margin:8px 0; color:#334155; line-height:1.6;">{fmt_inline(t)}</p>')

    close_list()
    close_table()
    parts.append('</div>')
    return '\n'.join(parts)


def build_html_table(report_name, data, recipient_name="", summary_html=""):
    """Builds a professional HTML table and optional Executive Analysis Summary for the email body."""
    
    rows = ""
    for item in data:
        rows += "<tr>"
        for val in item.values():
            rows += f"<td style='padding: 10px; border-bottom: 1px solid #ddd;'>{val}</td>"
        rows += "</tr>"
        
    headers = ""
    if data:
        headers = "<tr>"
        for key in data[0].keys():
            headers += f"<th style='padding: 12px 10px; background-color: #2c3e50; color: white; text-align: left;'>{key}</th>"
        headers += "</tr>"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8" />
      <style>
        body {{ font-family: Arial, sans-serif; background-color: #f8f9fa; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        h2 {{ color: #2c3e50; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        .footer {{ margin-top: 40px; font-size: 12px; color: #888; text-align: center; }}
      </style>
    </head>
    <body>
      <div class="container">
        <h2>Hi {recipient_name},</h2>
        <p>Your scheduled Dashboard Report is ready. Please find the attached files containing the latest KPIs and Visual Analytics.</p>
        {summary_html}
        <div class="footer">
          <p>This is an automatically generated email from DAgent AI.</p>
        </div>
      </div>
    </body>
    </html>
    """
    return html

def send_report_email(to_email, attachment_paths, recipient_name="", summary_html=""):
    """Sends the actual email with attachments and optional inline analysis summary."""
    try:
        from_email = MAIL_USERNAME or os.getenv("NOREPLY_EMAIL", "test@test.com")
        password = MAIL_PASSWORD or os.getenv("NOREPLY_PASSWORD", "")
        
        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = f"Automated Scheduled Report - Visual Analytics"

        body = build_html_table(report_name="Dashboard Report", data=[], recipient_name=recipient_name, summary_html=summary_html)
        msg.attach(MIMEText(body, "html", "utf-8"))

        # Attach files
        for attachment_path in attachment_paths:
            if attachment_path and os.path.exists(attachment_path):
                with open(attachment_path, "rb") as f:
                    part = MIMEApplication(f.read(), Name=os.path.basename(attachment_path))
                part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_path)}"'
                msg.attach(part)
            else:
                print(f"[Mailer Error] Could not attach file: {attachment_path} not found.")

        # Connection parameters directly from .env config
        port = int(MAIL_PORT or 25)
        host = MAIL_SERVER
        print(f"[Mailer] Connecting to SMTP server {host}:{port} (TLS={MAIL_USE_TLS})...", flush=True)

        # Resolve IPv4 host fallback if needed
        import socket
        resolved_ipv4 = None
        try:
            addr_info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            if addr_info and addr_info[0] and addr_info[0][4]:
                resolved_ipv4 = addr_info[0][4][0]
        except Exception:
            pass

        target_host = host

        def _connect_and_send(target):
            if port == 465:
                # Explicit SSL (SMTPS)
                with smtplib.SMTP_SSL(target, port, timeout=30) as server:
                    if password:
                        server.login(from_email, password)
                    server.send_message(msg)
            else:
                # Port 25 (Standard SMTP), 587 (Submission), or any other port
                with smtplib.SMTP(target, port, timeout=30) as server:
                    server.ehlo()
                    if MAIL_USE_TLS or port == 587:
                        try:
                            server.starttls()
                            server.ehlo()
                        except Exception as tls_err:
                            print(f"[Mailer] STARTTLS note (continuing plain): {tls_err}", flush=True)
                    if password:
                        try:
                            server.login(from_email, password)
                        except Exception as auth_err:
                            print(f"[Mailer] SMTP Auth note (continuing): {auth_err}", flush=True)
                    server.send_message(msg)

        try:
            _connect_and_send(target_host)
        except Exception as conn_err:
            if resolved_ipv4 and resolved_ipv4 != target_host:
                print(f"[Mailer] Retrying connection via resolved IPv4 ({resolved_ipv4})...", flush=True)
                _connect_and_send(resolved_ipv4)
            else:
                raise conn_err
            
        print(f"[Mailer] Successfully sent report to {to_email}")
        return True
    except Exception as e:
        print(f"[Mailer] Failed to send email to {to_email}. Error: {e}")
        return False

def check_and_send_scheduled_reports():
    """
    This function will be called every minute by APScheduler.
    It checks if there are any reports scheduled for the current Day and Minute.
    """
    now = datetime.datetime.now()
    current_day = now.strftime("%A") # e.g., 'Monday'
    current_time = now.strftime("%H:%M:00") # e.g., '09:00:00'
    
    try:
        conn = get_db_connection()
        if not conn: return
        cursor = conn.cursor()
        
        print(f"[Mailer] Checking schedules for {current_day} at {current_time}...", flush=True)
        
        query = """
            SELECT 
                sr.id AS schedule_id,
                sr.workspace_id,
                sr.report_ids,
                w.workspace_db,
                w.workspace_name,
                w.session_id,
                u.email,
                u.name
            FROM scheduled_reports sr
            JOIN scheduled_report_days srd ON sr.id = srd.schedule_id
            JOIN users u ON sr.recipient_id = u.id
            JOIN workspaces w ON sr.workspace_id = w.id
            WHERE sr.is_active = TRUE 
              AND srd.day_of_week = %s 
              AND sr.delivery_time = %s
        """
        cursor.execute(query, (current_day, current_time))
        due_reports = cursor.fetchall()
        
        if not due_reports:
            return # Nothing to send right now
            
        print(f"[Mailer] Found {len(due_reports)} report(s) scheduled for {current_day} at {current_time}. Sending...", flush=True)
        
        for schedule in due_reports:
            raw_rids = schedule.get('report_ids')
            if isinstance(raw_rids, str):
                try:
                    parsed_rids = [str(x) for x in json.loads(raw_rids)]
                except Exception:
                    parsed_rids = []
            elif isinstance(raw_rids, list):
                parsed_rids = [str(x) for x in raw_rids]
            else:
                parsed_rids = []

            # Always include Executive Analysis Summary in the email body
            print(f"[Mailer] Fetching Executive Analysis Summary for {schedule['email']}...", flush=True)
            summary_conn = get_db_connection()
            summary_md = _fetch_session_analysis_summary(
                summary_conn, 
                schedule.get('session_id'),
                workspace_id=schedule.get('workspace_id')
            )
            if summary_conn:
                try:
                    summary_conn.close()
                except Exception:
                    pass
            summary_html = _format_summary_to_email_html(summary_md)
            print(f"[Mailer] Executive Analysis Summary prepared for {schedule['email']} (chars: {len(summary_md)}, html_chars: {len(summary_html)})", flush=True)

            pdf_path = None
            should_gen_pdf = (not parsed_rids) or any(rid in parsed_rids for rid in ('1', '2', '3', '4', '8', '11', '12'))
            if should_gen_pdf:
                print(f"[Mailer] Generating PDF report for {schedule['email']}...", flush=True)
                # 1. Generate PDF Report (with executive summary embedded, no hyperlinks)
                pdf_path = generate_pdf_report(
                    db_conn=conn, 
                    workspace_name=schedule['workspace_name'],
                    session_id=schedule['session_id'],
                    report_ids=schedule['report_ids'],
                    summary_text=summary_md
                )
                if not pdf_path:
                    print(f"[Mailer] Failed to generate PDF for {schedule['email']}", flush=True)
                else:
                    print(f"[Mailer] PDF generated successfully at {pdf_path}", flush=True)

            excel_path = None
            should_gen_excel = (not parsed_rids) or ('9' in parsed_rids)
            if should_gen_excel:
                print(f"[Mailer] Generating Excel report for {schedule['email']}...", flush=True)
                # 1b. Generate Excel Report
                from controllers.export_report_controller import generate_domestic_sales_excel
                now = datetime.datetime.now()
                excel_conn = get_db_connection()
                excel_path = generate_domestic_sales_excel(
                    conn=excel_conn, 
                    session_id=schedule['session_id'],
                    year=now.year,
                    month=now.month,
                    day=now.day
                )
                if excel_conn:
                    excel_conn.close()
                
                if not excel_path:
                    print(f"[Mailer] Failed to generate Excel for {schedule['email']}", flush=True)
                else:
                    print(f"[Mailer] Excel generated successfully at {excel_path}", flush=True)
                
            attachments = []
            if pdf_path: attachments.append(pdf_path)
            if excel_path: attachments.append(excel_path)
            
            if not attachments and not summary_html:
                print(f"[Mailer] No attachments or summary generated for {schedule['email']}, skipping email.", flush=True)
                continue

            # 2. Send the Email
            send_report_email(
                to_email=schedule['email'],
                attachment_paths=attachments,
                recipient_name=schedule['name'],
                summary_html=summary_html
            )
            
            # 3. Cleanup temp files
            if pdf_path and os.path.exists(pdf_path):
                os.remove(pdf_path)
            if excel_path and os.path.exists(excel_path):
                os.remove(excel_path)
                
    except Exception as e:
        print(f"[Mailer] Error checking schedule: {e}")
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()
