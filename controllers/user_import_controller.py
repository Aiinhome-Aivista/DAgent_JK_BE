import io
import pandas as pd
from flask import request, jsonify, send_file
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from utils.crypto_utils import encrypt_password
from utils.email_service import generate_secure_temporary_password, send_welcome_user_email
from helper.user_import_validator import (
    ensure_hierarchy_tables,
    validate_import_dataframe
)


def import_users_excel(get_db_connection):
    """
    POST /api/admin/import-users-excel
    Uploads Excel/CSV, validates hierarchy against SAP Master Views,
    and atomically creates users with secure auto-generated passwords.
    Stores hierarchy mapping in dedicated `user_hierarchy_access` table without modifying `users`.
    """
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded. Please select an Excel or CSV file."}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"status": "error", "message": "Invalid or empty filename."}), 400

    filename = file.filename.lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xls") or filename.endswith(".csv")):
        return jsonify({"status": "error", "message": "Unsupported file format. Please upload .xlsx, .xls, or .csv."}), 400

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Database connection failed."}), 500

        # 1. Safely ensure master and mapping tables exist (zero touch to `users` schema)
        ensure_hierarchy_tables(conn)

        # 2. Read the file into a DataFrame
        try:
            if filename.endswith(".csv"):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)
        except Exception as read_err:
            return jsonify({
                "status": "error",
                "message": f"Could not parse spreadsheet file: {str(read_err)}"
            }), 400

        # 3. Validate rows (Allows partial success: valid rows are imported, invalid rows are reported and skipped)
        is_valid, errors, cleaned_users = validate_import_dataframe(conn, df)
        if not cleaned_users:
            return jsonify({
                "status": "error",
                "message": f"Validation failed for all rows ({len(errors)} error(s) found). No users were created.",
                "total_rows": len(df),
                "failed_count": len(errors),
                "errors": errors
            }), 400

        # 4. Atomic Database Insert
        cursor = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
        
        # Check default workspace to assign users to
        default_session_id = None
        default_workspace_id = None
        try:
            cursor.execute("SELECT id, session_id FROM workspaces WHERE is_active = 1 OR is_active IS NULL ORDER BY id ASC LIMIT 1")
            ws_row = cursor.fetchone()
            if ws_row:
                default_workspace_id = ws_row.get("id") if isinstance(ws_row, dict) else ws_row[0]
                default_session_id = ws_row.get("session_id") if isinstance(ws_row, dict) else ws_row[1]
        except Exception:
            pass

        created_results = []
        for user_item in cleaned_users:
            raw_password = generate_secure_temporary_password(length=10)
            encrypted_pwd = encrypt_password(raw_password)

            # Insert into core `users` table (existing schema unchanged)
            insert_user_sql = """
                INSERT INTO users (name, email, password, visibility)
                VALUES (%s, %s, %s, 1)
            """
            cursor.execute(insert_user_sql, (
                user_item["name"],
                user_item["email"],
                encrypted_pwd
            ))
            new_uid = cursor.lastrowid

            # Insert into dedicated `user_hierarchy_access` mapping table
            insert_access_sql = """
                INSERT INTO user_hierarchy_access (
                    user_id, rank_id, designation, department,
                    zone_code, region_code, territory_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    rank_id = VALUES(rank_id),
                    designation = VALUES(designation),
                    department = VALUES(department),
                    zone_code = VALUES(zone_code),
                    region_code = VALUES(region_code),
                    territory_code = VALUES(territory_code);
            """
            cursor.execute(insert_access_sql, (
                new_uid,
                user_item["rank_id"],
                user_item["designation"],
                user_item["department"],
                user_item["zone_code"],
                user_item["region_code"],
                user_item["territory_code"]
            ))

            # Attach to default workspace
            if default_workspace_id and default_session_id:
                try:
                    cursor.execute("""
                        INSERT INTO workspace_users (workspace_id, session_id, user_id)
                        VALUES (%s, %s, %s)
                    """, (default_workspace_id, default_session_id, new_uid))
                except Exception:
                    pass

            # Dispatch welcome email (non-blocking)
            send_welcome_user_email(
                to_email=user_item["email"],
                user_name=user_item["name"],
                temp_password=raw_password
            )

            user_item_result = dict(user_item)
            user_item_result["id"] = new_uid
            user_item_result["temp_password"] = raw_password
            created_results.append(user_item_result)

        conn.commit()
        cursor.close()

        status_label = "success" if not errors else "partial_success"
        message = (
            f"Successfully created and onboarded {len(created_results)} user(s)."
            if not errors
            else f"Partially succeeded: {len(created_results)} user(s) created, {len(errors)} invalid row(s) skipped."
        )

        return jsonify({
            "status": status_label,
            "message": message,
            "total_rows": len(df),
            "total_imported": len(created_results),
            "failed_count": len(errors),
            "users": created_results,
            "errors": errors
        }), 201

    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500
    finally:
        if conn and hasattr(conn, "close"):
            try: conn.close()
            except Exception: pass


def download_user_template():
    """
    GET /api/admin/download-user-template
    Generates a beautifully styled, standard Excel template with sample data and guidelines.
    """
    wb = openpyxl.Workbook()
    
    # Sheet 1: User Template
    ws_template = wb.active
    ws_template.title = "User Template"

    headers = ["Full Name", "Email", "Rank", "Designation", "Zone Code", "Region Code", "Territory Code"]
    ws_template.append(headers)

    # Style Header
    header_fill = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    for col_idx in range(1, len(headers) + 1):
        cell = ws_template.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Sample Demonstration Rows
    sample_rows = [
        ["Arun Sharma", "arun.sharma@jktyre.com", 1, "VP / National Head", "ALL", "ALL", "ALL"],
        ["Amitabh Roy", "amitabh.roy@jktyre.com", 2, "Zonal Head (North)", "NZ", "ALL", "ALL"],
        ["Sandeep Singh", "sandeep.singh@jktyre.com", 3, "Regional Manager (Jaipur)", "NZ", "1181", "ALL"],
        ["Bikash Kumar", "bikash.kumar@jktyre.com", 4, "Field Executive", "NZ", "1181", "116201"]
    ]

    for row_data in sample_rows:
        ws_template.append(row_data)

    for row in ws_template.iter_rows(min_row=2, max_row=len(sample_rows) + 1, min_col=1, max_col=len(headers)):
        for cell in row:
            cell.border = thin_border
            cell.font = Font(name="Calibri", size=11)
            cell.alignment = Alignment(vertical="center")

    col_widths = {1: 22, 2: 28, 3: 10, 4: 28, 5: 14, 6: 14, 7: 16}
    for col_idx, width in col_widths.items():
        ws_template.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = width

    # Sheet 2: Guidelines
    ws_guide = wb.create_sheet(title="Instructions")
    ws_guide.append(["Field Name", "Requirement", "Allowed Values / Meaning", "Example"])
    for col_idx in range(1, 5):
        cell = ws_guide.cell(row=1, column=col_idx)
        cell.fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    guidelines_data = [
        ["Full Name", "Mandatory", "Full legal name of the user", "Rahul Sharma"],
        ["Email", "Mandatory (Unique)", "Official corporate email (Must not already exist)", "rahul.s@jktyre.com"],
        ["Rank", "Mandatory", "1=National Head, 2=Zonal Head, 3=Regional Manager, 4=Field Officer", "2"],
        ["Designation", "Mandatory", "Official designation title for display in UI", "Zonal Head"],
        ["Zone Code", "Mandatory", "SAP Zone Master (P_ZONE) or 'ALL' for National level", "NZ, WZ, SZ, CZ, EZ, ALL"],
        ["Region Code", "Mandatory", "SAP Region Master or 'ALL' for National/Zonal Head", "1181, 1182, ALL"],
        ["Territory Code", "Optional", "SAP Territory code (BZIRK). If left empty, defaults to 'ALL'", "116201 or empty"]
    ]

    for r in guidelines_data:
        ws_guide.append(r)

    guide_widths = {1: 18, 2: 20, 3: 45, 4: 25}
    for col_idx, width in guide_widths.items():
        ws_guide.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = width

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="JK_User_Import_Template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
