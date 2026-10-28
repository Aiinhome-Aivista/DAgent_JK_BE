import datetime
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

def build_html_table(report_name, data, recipient_name=""):
    """Builds a professional HTML table for the email body."""
    
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
        <p>Your scheduled Dashboard Report is ready. Please find the attached PDF containing the latest KPIs and Visual Analytics.</p>
        
        <div class="footer">
          <p>This is an automatically generated email from DAgent AI.</p>
        </div>
      </div>
    </body>
    </html>
    """
    return html

def send_report_email(to_email, attachment_paths, recipient_name=""):
    """Sends the actual email with attachments."""
    try:
        from_email = MAIL_USERNAME or os.getenv("NOREPLY_EMAIL", "test@test.com")
        password = MAIL_PASSWORD or os.getenv("NOREPLY_PASSWORD", "")
        
        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = f"Automated Scheduled Report - Visual Analytics"

        body = build_html_table(report_name="Dashboard Report", data=[], recipient_name=recipient_name)
        msg.attach(MIMEText(body, "html"))

        # Attach files
        for attachment_path in attachment_paths:
            if attachment_path and os.path.exists(attachment_path):
                with open(attachment_path, "rb") as f:
                    part = MIMEApplication(f.read(), Name=os.path.basename(attachment_path))
                part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_path)}"'
                msg.attach(part)
            else:
                print(f"[Mailer Error] Could not attach file: {attachment_path} not found.")

        # Send using the dynamic SMTP server from config
        if MAIL_USE_TLS or MAIL_PORT == 587:
            with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as server:
                server.starttls()
                server.login(from_email, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(MAIL_SERVER, MAIL_PORT) as server:
                server.login(from_email, password)
                server.send_message(msg)
            
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
            print(f"[Mailer] Generating PDF report for {schedule['email']}...", flush=True)
            # 1. Generate PDF Report
            pdf_path = generate_pdf_report(
                db_conn=conn, 
                workspace_name=schedule['workspace_name'],
                session_id=schedule['session_id'],
                report_ids=schedule['report_ids']
            )
            
            if not pdf_path:
                print(f"[Mailer] Failed to generate PDF for {schedule['email']}", flush=True)
            else:
                print(f"[Mailer] PDF generated successfully at {pdf_path}", flush=True)
                
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
            
            if not attachments:
                print(f"[Mailer] No attachments generated for {schedule['email']}, skipping email.", flush=True)
                continue

            # 2. Send the Email
            send_report_email(
                to_email=schedule['email'],
                attachment_paths=attachments,
                recipient_name=schedule['name']
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
