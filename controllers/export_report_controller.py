from helper.dashboard_cache_service import get_cached_component_data, set_cached_component_data
from flask import request, send_file, jsonify

import io

from openpyxl import Workbook

from openpyxl.styles import Font, Alignment, PatternFill, Border, Side



def generate_domestic_sales_excel(conn, session_id, year, month, day, return_worksheets=False):

    cursor = None

    try:

        try:

            # For mysql.connector

            cursor = conn.cursor(dictionary=True)

            is_mysql_connector = True

        except TypeError:

            # For pymysql

            import pymysql

            cursor = conn.cursor(pymysql.cursors.DictCursor)

            is_mysql_connector = False

        print(f"[Excel Export] Generating domestic sales report for {year}-{month}-{day}", flush=True)

        

        rows = []

        rows_ws1 = []

        rows_ws2 = []

        rows_ws3 = []



        # Check if session is connected to SAP HANA

        from helper.sap_analytics_service import (

            get_sap_connection_for_session,

            query_domestic_sales_value_achievements_hana,

            query_sales_numbers_hana,

            query_sales_report_by_values_hana,

            query_sales_summary_in_no_and_values_hana,

            detect_sales_dates,

        )



        hana_conn, tunnel, schema = get_sap_connection_for_session(conn, session_id)

        is_sap_session = bool(hana_conn)

        if hana_conn:

            print(f"[Excel Export] Detected SAP HANA connection for session {session_id}. Querying SAP views in schema '{schema}'...", flush=True)

            try:

                h_cur = hana_conn.cursor()

                year, month, day = detect_sales_dates(h_cur, schema, year, month, day)

                rows = query_domestic_sales_value_achievements_hana(h_cur, schema, year, month, day)

                rows_ws1 = query_sales_numbers_hana(h_cur, schema, year, month, day)

                rows_ws2 = query_sales_report_by_values_hana(h_cur, schema, year, month, day)

                rows_ws3 = query_sales_summary_in_no_and_values_hana(h_cur, schema, year, month, day)

                h_cur.close()

            except Exception as e:

                print(f"[Excel Export] Error executing SAP HANA queries: {e}", flush=True)

            finally:

                hana_conn.close()

                if tunnel:

                    tunnel.stop()

        # Fallback to dedicated MySQL cache tables if empty
        ym_str = f"{year}{str(month).zfill(2)}"
        if not rows and cursor:
            try:
                cursor.execute("SELECT * FROM sales_summary_domestic_sales_value_achievement WHERE session_id=%s AND year_month=%s ORDER BY id ASC", (session_id, ym_str))
                c_rows = cursor.fetchall()
                if c_rows:
                    rows = c_rows
                    print(f"[Excel Export] Sheet 1 loaded {len(rows)} rows from cache table.", flush=True)
            except Exception as e:
                print(f"[Excel Export] Note on cache fallback for sheet 1: {e}", flush=True)

        if not rows_ws1 and cursor:
            try:
                cursor.execute("SELECT * FROM sales_summary_sales_numbers WHERE session_id=%s AND year_month=%s ORDER BY is_total ASC, sort_order ASC, category ASC", (session_id, ym_str))
                c_rows1 = cursor.fetchall()
                if c_rows1:
                    rows_ws1 = c_rows1
                    print(f"[Excel Export] Sheet 2 loaded {len(rows_ws1)} rows from cache table.", flush=True)
            except Exception as e:
                print(f"[Excel Export] Note on cache fallback for sheet 2: {e}", flush=True)

        if not rows_ws2 and cursor:
            try:
                cursor.execute("SELECT * FROM sales_summary_sales_report_by_values WHERE session_id=%s AND year_month=%s ORDER BY is_total ASC, sort_order ASC", (session_id, ym_str))
                c_rows2 = cursor.fetchall()
                if c_rows2:
                    rows_ws2 = c_rows2
                    print(f"[Excel Export] Sheet 3 loaded {len(rows_ws2)} rows from cache table.", flush=True)
            except Exception as e:
                print(f"[Excel Export] Note on cache fallback for sheet 3: {e}", flush=True)



        sync_row = None

        db_to_use = None

        if not rows and not is_sap_session:

            print(f"[Excel Export] Received session_id: {session_id}, year: {year}, month: {month}, day: {day}", flush=True)

            # Handle dynamic database based on session_id and where SP exists

            if session_id:

                cursor.execute("""

                    SELECT new_user_db, external_database

                    FROM external_db_sync_log 

                    WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''

                    ORDER BY id DESC LIMIT 1

                """, (session_id,))

                sync_rows = cursor.fetchall()

                print(f"[Excel Export] DB Sync Log returned {len(sync_rows)} rows for session {session_id}.", flush=True)

                if sync_rows:

                    sync_row = sync_rows[0]

                    new_user_db = sync_row['new_user_db']

                    external_database = sync_row['external_database']

                    

                    # Helper to check if SP exists

                    def sp_exists(db_name, sp_name):

                        if not db_name: return False

                        cursor.execute("""

                            SELECT ROUTINE_NAME 

                            FROM information_schema.routines 

                            WHERE ROUTINE_TYPE='PROCEDURE' 

                              AND ROUTINE_SCHEMA=%s 

                              AND ROUTINE_NAME=%s

                        """, (db_name, sp_name))

                        return bool(cursor.fetchall())

                    

                    if sp_exists(new_user_db, 'sp_domestic_sales_value_achivements'):

                        db_to_use = new_user_db

                    elif sp_exists(external_database, 'sp_domestic_sales_value_achivements'):

                        db_to_use = external_database

                        

                    print(f"[Excel Export] Selected DB: {db_to_use} (Fallback: {external_database})", flush=True)

                    

                    if db_to_use:

                        print(f"[Excel Export] Executing {db_to_use}.sp_domestic_sales_value_achivements...", flush=True)

                        try:

                            cursor.execute(f"CALL `{db_to_use}`.sp_domestic_sales_value_achivements(%s, %s, %s)", (year, month, day))

                        except Exception as e:

                            print(f"[Excel Export] SP execution failed: {e}", flush=True)

                    else:

                        print(f"[Excel Export] SP not found in assigned DBs. Skipping...", flush=True)

                else:

                    try:

                        cursor.execute("CALL sp_domestic_sales_value_achivements(%s, %s, %s)", (year, month, day))

                    except Exception as e:

                        print(f"[Excel Export] SP fallback execution failed: {e}", flush=True)

            else:

                try:

                    cursor.execute("CALL sp_domestic_sales_value_achivements(%s, %s, %s)", (year, month, day))

                except Exception as e:

                    print(f"[Excel Export] SP fallback execution failed: {e}", flush=True)

            

            print(f"[Excel Export] Fetching results...", flush=True)

            if is_mysql_connector:

                try:

                    # Try regular fetchall first

                    if cursor.description is not None:

                        rows = cursor.fetchall()

                except Exception: pass

                

                for result in cursor.stored_results():

                    fetched = result.fetchall()

                    if not rows and fetched:

                        rows = fetched

                

                while True:

                    try:

                        if not cursor.nextset(): break

                        if cursor.description is not None: cursor.fetchall()

                    except Exception: break

            else:

                if cursor.description is not None:

                    rows = cursor.fetchall()

                while True:

                    try:

                        if not cursor.nextset(): break

                        if cursor.description is not None: cursor.fetchall()

                    except Exception: break



        

        print(f"[Excel Export] sp_domestic_sales_value_achivements returned {len(rows)} rows.", flush=True)

        if len(rows) == 0:

            raise Exception("No sales data available. Files may still be processing or SP is missing.")

            

        print(f"[Excel Export] First row sample: {rows[0]}", flush=True)

            

        # Generate Excel

        wb = Workbook()

        ws = wb.active

        ws.title = "Domestic Sales Value Achv."

        

        import calendar

        month_abbr = calendar.month_abbr[month] if 1 <= month <= 12 else str(month)

        curr_year_str = str(year)[-2:]

        prev_year_str = str(year - 1)[-2:]

        

        # Add the 3 other sheets for the other sections requested by the user

        ws1 = wb.create_sheet("Sales Number's")

        ws1['A1'].value = "Sales Number's"

        ws1['A1'].font = Font(bold=True)

        

        ws2 = wb.create_sheet("Sales Report by Values")

        ws2['A1'].value = "Sales Report by Values"

        ws2['A1'].font = Font(bold=True)

        

        ws3 = wb.create_sheet("Sales Summary in No's & Values")

        ws3['A1'].value = "Sales Summary in No's & Values"

        ws3['A1'].font = Font(bold=True)

        

        # Styles

        bold_font = Font(bold=True)

        center_aligned_text = Alignment(horizontal="center", vertical="center", wrap_text=True)

        

        fill_title = PatternFill(start_color="CCFFFF", end_color="CCFFFF", fill_type="solid")

        fill_header = PatternFill(start_color="CCFFFF", end_color="CCFFFF", fill_type="solid")

        fill_total = PatternFill(start_color="E6E6FA", end_color="E6E6FA", fill_type="solid")

        fill_pink = PatternFill(start_color="E4DFEC", end_color="E4DFEC", fill_type="solid")

        fill_yellow = PatternFill(start_color="FFFF99", end_color="FFFF99", fill_type="solid")

        fill_blue = PatternFill(start_color="CCFFFF", end_color="CCFFFF", fill_type="solid")

        

        thin = Side(border_style="thin", color="000000")

        border = Border(top=thin, left=thin, right=thin, bottom=thin)



        # Title Row (Row 1)

        ws.merge_cells('A1:H1')

        cell = ws['A1']

        cell.value = "Domestic Sales Value Achievement"

        cell.font = bold_font

        cell.fill = fill_title

        cell.border = border

        cell.alignment = center_aligned_text

        

        # Headers Group 1 (Row 2)

        ws.merge_cells('A2:A3')

        ws['A2'].value = "Market Segment"

        ws.merge_cells('B2:C2')

        ws['B2'].value = f"{month_abbr}-{prev_year_str} Actual"

        ws.merge_cells('D2:H2')

        ws['D2'].value = f"Actual Sale for {month_abbr}-{curr_year_str}"

        

        for c in range(1, 9):

            ws.cell(row=2, column=c).border = border

            ws.cell(row=2, column=c).font = bold_font

            ws.cell(row=2, column=c).alignment = center_aligned_text

            ws.cell(row=2, column=c).fill = fill_header



        # Headers Group 2 (Row 3)

        h3 = ["Month", "MTD", "Target", "Actual", "% Achvd", f"Sale for\nday {day}", "Grth\nOver\nLYSMT"]

        for i, h in enumerate(h3, start=2):

            cell = ws.cell(row=3, column=i)

            cell.value = h

            cell.font = bold_font

            cell.alignment = center_aligned_text

            cell.fill = fill_header

            cell.border = border

            

        ws.cell(row=3, column=1).border = border

        ws.cell(row=3, column=1).fill = fill_header

        

        row_num = 4

        

        for row in rows:

            label = row.get('REPORT_NAME') or row.get('report_name') or row.get('MARKET_SEGMENT') or row.get('market_segment') or 'Unknown'

            

            # Use '0%' or '%' based on image

            achv = row.get('Achievement') if row.get('Achievement') is not None else row.get('achievement_pct')
            grth = row.get('Growth_Over_LYSMTD') if row.get('Growth_Over_LYSMTD') is not None else row.get('growth_over_lysmt')
            if isinstance(achv, (int, float)):
                achv = f"{round(achv)}%" if achv else "0%"
            elif achv is None:
                achv = '0%'

            if isinstance(grth, (int, float)):
                grth = f"{round(grth)}%" if grth else "0%"
            elif grth is None:
                grth = '0%'

            c1 = ws.cell(row=row_num, column=1, value=label)

            val_month = row.get('Month') if row.get('Month') is not None else row.get('prev_year_actual_month', row.get('month', 0))
            val_mtd = row.get('MTD') if row.get('MTD') is not None else row.get('prev_year_actual_mtd', row.get('mtd', 0))
            val_target = row.get('Target') if row.get('Target') is not None else row.get('target', 0)
            val_actual = row.get('Actual') if row.get('Actual') is not None else row.get('actual', 0)
            val_day = row.get('Sale_For_Day') if row.get('Sale_For_Day') is not None else (row.get('sale_for_the_day') if row.get('sale_for_the_day') is not None else row.get('sale_for_day', 0))

            c2 = ws.cell(row=row_num, column=2, value=val_month)

            c3 = ws.cell(row=row_num, column=3, value=val_mtd)

            c4 = ws.cell(row=row_num, column=4, value=val_target)

            c5 = ws.cell(row=row_num, column=5, value=val_actual)

            c6 = ws.cell(row=row_num, column=6, value=achv)

            c7 = ws.cell(row=row_num, column=7, value=val_day)

            c8 = ws.cell(row=row_num, column=8, value=grth)

            

            # Apply formatting

            is_subtotal = "OEM+STU+DEF" in label or "Repl" in label or "Total" in label

            is_purple = "Total" in label and ("4" in label or "2/3" in label)

            is_grand_total = label.lower() == "domestic"

            

            fill_color = None

            if is_grand_total:

                fill_color = fill_blue

            elif is_purple:

                fill_color = fill_pink

            elif is_subtotal:

                fill_color = fill_yellow

            

            for c in [c1, c2, c3, c4, c5, c6, c7, c8]:

                c.border = border

                if is_subtotal or is_grand_total or fill_color:

                    c.font = bold_font

                if fill_color:

                    c.fill = fill_color

                    

            c6.alignment = center_aligned_text

            c8.alignment = center_aligned_text

            

            row_num += 1





        # Adjust column widths for WS

        from openpyxl.utils import get_column_letter

        for idx, col in enumerate(ws.columns, start=1):

            max_length = 0

            column = get_column_letter(idx)

            for cell in col:

                try:

                    if len(str(cell.value)) > max_length:

                        max_length = len(str(cell.value))

                except:

                    pass

            adjusted_width = (max_length + 2)

            ws.column_dimensions[column].width = adjusted_width



        # ==========================================

        # POPULATE WS1 (Sales Summary)

        # ==========================================

        try:

            if not rows_ws1 and not is_sap_session:

                # Update SP call for WS1

                print(f"[Excel Export] Executing sp_sales_numbers for WS1...", flush=True)

                try:

                    if session_id and sync_row and db_to_use:

                        cursor.execute(f"CALL `{db_to_use}`.sp_sales_numbers(%s, %s, %s)", (year, month, day))

                    else:

                        cursor.execute("CALL sp_sales_numbers(%s, %s, %s)", (year, month, day))

                except Exception as e:

                    print(f"[Excel Export] WS1 SP failed: {e}", flush=True)

                print(f"[Excel Export] sp_sales_numbers finished.", flush=True)

                    

                if is_mysql_connector:

                    try:

                        if cursor.description is not None:

                            rows_ws1 = cursor.fetchall()

                    except Exception: pass

                    

                    for result in cursor.stored_results():

                        fetched = result.fetchall()

                        if not rows_ws1 and fetched:

                            rows_ws1 = fetched

                            

                    while True:

                        try:

                            if not cursor.nextset(): break

                            if cursor.description is not None: cursor.fetchall()

                        except Exception: break

                else:

                    if cursor.description is not None:

                        rows_ws1 = cursor.fetchall()

                    while True:

                        try:

                            if not cursor.nextset(): break

                            if cursor.description is not None: cursor.fetchall()

                        except Exception: break

                            

            print(f"[Excel Export] sp_sales_numbers returned {len(rows_ws1)} rows.", flush=True)

            if len(rows_ws1) > 0:

                print(f"[Excel Export] First row sample: {rows_ws1[0]}", flush=True)

                

            # Row 1 is intentionally left entirely blank and unstyled



            # Merge cells for title

            ws1.merge_cells('A1:S1')

            ws1['A1'].value = "Sales Number's"

            ws1['A1'].font = bold_font

            ws1['A1'].alignment = center_aligned_text

            ws1['A1'].fill = fill_title

            

            for c in range(2, 20):

                ws1.cell(row=1, column=c).border = border

                ws1.cell(row=1, column=c).fill = fill_title

            

            # Headers Group 1 (Row 2)

            fill_cat = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid") # White

            fill_rep = PatternFill(start_color="FFFF99", end_color="FFFF99", fill_type="solid") # Yellow

            fill_oem = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid") # Pink

            fill_stu = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid") # Light Blue

            fill_def = PatternFill(start_color="FFF0F5", end_color="FFF0F5", fill_type="solid") # Light Purple

            fill_dom = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid") # Grey

            

            ws1.merge_cells('A2:A4')

            ws1['A2'].value = "Category"

            ws1['A2'].fill = fill_cat

            ws1.merge_cells('B2:J2')

            ws1['B2'].value = "Replacement"

            ws1['B2'].fill = fill_rep

            ws1.merge_cells('K2:L2')

            ws1['K2'].value = "OEM"

            ws1['K2'].fill = fill_oem

            ws1.merge_cells('M2:N2')

            ws1['M2'].value = "STU"

            ws1['M2'].fill = fill_stu

            ws1.merge_cells('O2:P2')

            ws1['O2'].value = "DEF"

            ws1['O2'].fill = fill_def

            ws1.merge_cells('Q2:S2')

            ws1['Q2'].value = "Domestic"

            ws1['Q2'].fill = fill_dom

            

            for c in range(1, 20):

                ws1.cell(row=2, column=c).border = border

                ws1.cell(row=2, column=c).font = bold_font

                ws1.cell(row=2, column=c).alignment = center_aligned_text



            # Headers Group 2 (Row 3 and Row 4)

            h3_headers = {

                2: f"{month_abbr}-{prev_year_str} Act", 3: "Target", 4: "Total", 5: "% Achv", 6: "Today", 

                7: "Dealer", 8: "Dist.", 9: "Fleet", 10: "Others", 

                11: "Target", 12: "Actual", 13: "Target", 14: "Actual", 15: "Target", 16: "Actual", 

                17: "Target", 18: "Actual", 19: "% Achv."

            }

            

            for col, val in h3_headers.items():

                ws1.merge_cells(start_row=3, start_column=col, end_row=4, end_column=col)

                ws1.cell(row=3, column=col).value = val



            # Apply borders and formatting for rows 3 and 4 (No background fill)

            for r in [3, 4]:

                for c in range(2, 20):

                    cell = ws1.cell(row=r, column=c)

                    cell.border = border

                    cell.font = bold_font

                    cell.alignment = center_aligned_text

                    

            ws1.cell(row=3, column=1).border = border

            ws1.cell(row=4, column=1).border = border

                

            # Data rows

            r_idx = 5

            data_row_counter = 0

            

            fill_light_grey = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

            

            def sv(val):

                if val is None or str(val).strip() == '': return ""

                try:

                    if float(val) == 0: return ""

                except ValueError:

                    pass

                return val

                

            for r in rows_ws1:

                cat = r.get('category', '')

                

                # Compute % Achv row-by-row since SP doesn't provide them

                tgt = float(r.get('target') or 0)
                tot = float(r.get('total') or 0)
                if r.get('achievement_pct') is not None:
                    achv1 = float(r.get('achievement_pct') or 0)
                else:
                    achv1 = round((tot / tgt * 100), 2) if tgt > 0 else 0

                dom_tgt = float(r.get('domestic_target') or 0)
                dom_act = float(r.get('domestic_actual') or 0)
                if r.get('domestic_achv_pct') is not None:
                    achv2 = float(r.get('domestic_achv_pct') or 0)
                else:
                    achv2 = round((dom_act / dom_tgt * 100), 2) if dom_tgt > 0 else 0

                achv1_str = f"{achv1}%" if tgt > 0 else "%"
                achv2_str = f"{achv2}%" if dom_tgt > 0 else "%"

                prev_val = r.get('prev_year_act') if r.get('prev_year_act') is not None else r.get('jul_act')

                c_cells = [
                    ws1.cell(row=r_idx, column=1, value=cat),
                    ws1.cell(row=r_idx, column=2, value=sv(prev_val)),

                    ws1.cell(row=r_idx, column=3, value=sv(r.get('target'))),

                    ws1.cell(row=r_idx, column=4, value=sv(r.get('total'))),

                    ws1.cell(row=r_idx, column=5, value=achv1_str),

                    ws1.cell(row=r_idx, column=6, value=sv(r.get('today'))),

                    ws1.cell(row=r_idx, column=7, value=sv(r.get('dealer'))),

                    ws1.cell(row=r_idx, column=8, value=sv(r.get('distributor'))),

                    ws1.cell(row=r_idx, column=9, value=sv(r.get('fleet'))),

                    ws1.cell(row=r_idx, column=10, value=sv(r.get('others'))),

                    ws1.cell(row=r_idx, column=11, value=sv(r.get('oem_target'))),

                    ws1.cell(row=r_idx, column=12, value=sv(r.get('oem_actual'))),

                    ws1.cell(row=r_idx, column=13, value=sv(r.get('stu_target'))),

                    ws1.cell(row=r_idx, column=14, value=sv(r.get('stu_actual'))),

                    ws1.cell(row=r_idx, column=15, value=sv(r.get('def_target'))),

                    ws1.cell(row=r_idx, column=16, value=sv(r.get('def_actual'))),

                    ws1.cell(row=r_idx, column=17, value=sv(r.get('domestic_target'))),

                    ws1.cell(row=r_idx, column=18, value=sv(r.get('domestic_actual'))),

                    ws1.cell(row=r_idx, column=19, value=achv2_str),

                ]

                

                is_subtotal = "Total" in str(cat)

                

                f_color = None

                if is_subtotal:

                    f_color = fill_yellow

                elif cat in ["Pack Tube", "Treel"]:

                    f_color = fill_pink

                else:

                    data_row_counter += 1

                    if data_row_counter % 2 == 0:

                        f_color = fill_light_grey

                

                for cell in c_cells:

                    cell.border = border

                    

                    if is_subtotal:

                        cell.fill = fill_yellow

                        cell.font = bold_font

                    elif f_color == fill_pink:

                        cell.fill = fill_pink

                        cell.font = bold_font

                    elif f_color:

                        cell.fill = f_color

                        

                r_idx += 1

                

            from openpyxl.utils import get_column_letter

            for idx, col in enumerate(ws1.columns, start=1):

                max_length = 0

                column = get_column_letter(idx)

                for cell in col:

                    try:

                        if len(str(cell.value)) > max_length:

                            max_length = len(str(cell.value))

                    except:

                        pass

                adjusted_width = (max_length + 2)

                ws1.column_dimensions[column].width = adjusted_width

                

        except Exception as e:

            print(f"Error populating WS1 (Sales Summary): {e}")

        # ==========================================

        # POPULATE WS2 (Sales Report by Values)

        # ==========================================

        try:

            if not rows_ws2 and not is_sap_session:

                print(f"[Excel Export] Executing sp_sales_report_by_values for WS2...", flush=True)

                try:

                    if session_id and sync_row and db_to_use:

                        cursor.execute(f"CALL `{db_to_use}`.sp_sales_report_by_values(%s, %s, %s)", (year, month, day))

                    else:

                        cursor.execute("CALL sp_sales_report_by_values(%s, %s, %s)", (year, month, day))

                except Exception as e:

                    print(f"[Excel Export] WS2 SP failed: {e}", flush=True)

                print(f"[Excel Export] sp_sales_report_by_values finished.", flush=True)

                    

                if is_mysql_connector:

                    try:

                        if cursor.description is not None:

                            rows_ws2 = cursor.fetchall()

                    except Exception: pass

                    

                    for result in cursor.stored_results():

                        fetched = result.fetchall()

                        if not rows_ws2 and fetched:

                            rows_ws2 = fetched

                            

                    while True:

                        try:

                            if not cursor.nextset(): break

                            if cursor.description is not None: cursor.fetchall()

                        except Exception: break

                else:

                    if cursor.description is not None:

                        rows_ws2 = cursor.fetchall()

                    while True:

                        try:

                            if not cursor.nextset(): break

                            if cursor.description is not None: cursor.fetchall()

                        except Exception: break

                            

            print(f"[Excel Export] sp_sales_report_by_values returned {len(rows_ws2)} rows.", flush=True)



            fill_greyish = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")



            ws2.merge_cells('A1:P1')

            ws2['A1'].value = "Sales Report by Values ( In Crores / Rs. )"

            ws2['A1'].font = bold_font

            ws2['A1'].alignment = center_aligned_text

            ws2['A1'].fill = fill_greyish

            for c in range(1, 17):

                ws2.cell(row=1, column=c).border = border

                ws2.cell(row=1, column=c).fill = fill_greyish

            

            # Row 2

            ws2.merge_cells('A2:A4')

            ws2['A2'].value = "Zone"

            

            ws2.merge_cells('B2:D3')

            ws2['B2'].value = f"{month_abbr}-{prev_year_str} Actual"

            

            ws2.merge_cells('E2:P2')

            ws2['E2'].value = f"Actual Sale for {month_abbr}-{curr_year_str}"

            

            # Row 3

            ws2.merge_cells('E3:G3')

            ws2['E3'].value = "Target"

            ws2.merge_cells('H3:J3')

            ws2['H3'].value = "Actual Sales"

            ws2.merge_cells('K3:K4')

            ws2['K3'].value = "% Achvd"

            ws2.merge_cells('L3:L4')

            ws2['L3'].value = "Sale for\nthe Day"

            ws2.merge_cells('M3:M4')

            ws2['M3'].value = "Asking\nRate"

            ws2.merge_cells('N3:P3')

            ws2['N3'].value = "% Achvt."

            

            fill_orange = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")

            

            # Row 4 Subheaders

            sub_h = {

                2: "4 Whlrs.", 3: "2/3 Whlrs", 4: "Total",

                5: "4 Whlrs.", 6: "2/3 Whlrs", 7: "Total",

                8: "4 Whlrs", 9: "2/3 Whlrs", 10: "Total",

                14: "4 Whlrs", 15: "2/3 Whlrs", 16: "Total"

            }

            for col, val in sub_h.items():

                ws2.cell(row=4, column=col, value=val)

                

            # Apply styles for rows 2,3,4

            for r in range(2, 5):

                for c in range(1, 17):

                    cell = ws2.cell(row=r, column=c)

                    cell.border = border

                    cell.font = bold_font

                    cell.alignment = center_aligned_text

                    if c >= 14 and r == 3:

                        cell.fill = fill_orange

                    else:

                        cell.fill = fill_header



            # ---- Data rows: fully bound from SP, no Python calculation ----

            r_idx = 5

            fill_magenta = PatternFill(start_color="FFCCFF", end_color="FFCCFF", fill_type="solid")

            fill_light_pink = PatternFill(start_color="FFF0F5", end_color="FFF0F5", fill_type="solid")

            fill_green = PatternFill(start_color="CCFFCC", end_color="CCFFCC", fill_type="solid")



            def _fmt_pct(val):

                """Return as percent string; preserve if already has %, else convert float."""

                if val is None:

                    return "0%"

                s = str(val).strip()

                if s.endswith('%'):

                    return s

                try:

                    f = float(s)

                    return f"{int(round(f))}%" if f else "0%"

                except (ValueError, TypeError):

                    return s if s else "0%"



            for r in rows_ws2:

                # SP returns 16 columns in fixed positional order — use list(values()) for dynamic column names

                vals = list(r.values())   # [Zone, LY_4W, LY_23W, LY_Tot, Tgt_4W, Tgt_23W, Tgt_Tot, Act_4W, Act_23W, Act_Tot, %Achvd, SaleDay, AskRate, %4W, %23W, %Tot]

                zone = str(vals[0]).strip() if vals[0] is not None else ''

                is_total = zone.lower() == 'total'

                is_contribution = zone.lower().startswith('%')



                def _v(idx):

                    """Safely get value by position, return '' if out of range or None."""

                    return vals[idx] if idx < len(vals) and vals[idx] is not None else ''



                if is_contribution:

                    # % Contribution row — merge col 1-4, bind cols 5-16 from SP positionally

                    ws2.merge_cells(start_row=r_idx, start_column=1, end_row=r_idx, end_column=4)

                    ws2.cell(row=r_idx, column=1, value=zone).font = bold_font

                    ws2.cell(row=r_idx, column=1).alignment = center_aligned_text

                    for c in range(1, 5):

                        ws2.cell(row=r_idx, column=c).fill = fill_magenta

                        ws2.cell(row=r_idx, column=c).border = border



                    # Cols 5-16 → SP positions 4-15

                    contrib_fills = [

                        fill_light_pink, fill_light_pink, fill_yellow,   # Target 4W, 23W, Total

                        fill_green,      fill_light_pink, fill_yellow,   # Actual 4W, 23W, Total

                        fill_yellow,     fill_yellow,     fill_yellow,   # %Achvd, SaleDay, AskRate

                        fill_yellow,     fill_yellow,     fill_yellow    # %4W, %23W, %Tot

                    ]

                    for i in range(12):

                        col_num = 5 + i

                        raw = _v(4 + i)

                        val = raw if raw != '' else ''

                        cell = ws2.cell(row=r_idx, column=col_num, value=val)

                        cell.font = bold_font

                        cell.fill = contrib_fills[i]

                        cell.border = border

                        cell.alignment = center_aligned_text

                    r_idx += 1

                    continue



                # Normal zone row or Total row — bind all 16 columns by position

                row_fill = fill_yellow if is_total else None

                p_4w = r.get('prev_4w') if 'prev_4w' in r else _v(1)
                p_23w = r.get('prev_23w') if 'prev_23w' in r else _v(2)
                p_tot = r.get('prev_total') if 'prev_total' in r else _v(3)
                t_4w = r.get('target_4w') if 'target_4w' in r else _v(4)
                t_23w = r.get('target_23w') if 'target_23w' in r else _v(5)
                t_tot = r.get('target_total') if 'target_total' in r else _v(6)
                a_4w = r.get('actual_4w') if 'actual_4w' in r else _v(7)
                a_23w = r.get('actual_23w') if 'actual_23w' in r else _v(8)
                a_tot = r.get('actual_total') if 'actual_total' in r else _v(9)
                achv = r.get('achievement_pct') if 'achievement_pct' in r else _v(10)
                s_day = r.get('sale_for_the_day') if 'sale_for_the_day' in r else _v(11)
                ask = r.get('asking_rate') if 'asking_rate' in r else _v(12)
                ac_4w = r.get('achvt_4w') if 'achvt_4w' in r else _v(13)
                ac_23w = r.get('achvt_23w') if 'achvt_23w' in r else _v(14)
                ac_tot = r.get('achvt_total') if 'achvt_total' in r else _v(15)

                cells_data = [
                    (1,  zone),
                    (2,  sv(p_4w)),
                    (3,  sv(p_23w)),
                    (4,  sv(p_tot)),
                    (5,  sv(t_4w)),
                    (6,  sv(t_23w)),
                    (7,  sv(t_tot)),
                    (8,  sv(a_4w)),
                    (9,  sv(a_23w)),
                    (10, sv(a_tot)),
                    (11, _fmt_pct(achv)),
                    (12, sv(s_day)),
                    (13, sv(ask)),
                    (14, _fmt_pct(ac_4w)),
                    (15, _fmt_pct(ac_23w)),
                    (16, _fmt_pct(ac_tot)),
                ]

                for col_num, val in cells_data:

                    cell = ws2.cell(row=r_idx, column=col_num, value=val)

                    cell.border = border

                    cell.font = bold_font

                    if row_fill:

                        cell.fill = row_fill

                    if col_num in [11, 13, 14, 15, 16]:

                        cell.alignment = center_aligned_text



                r_idx += 1





            from openpyxl.utils import get_column_letter

            for idx, col in enumerate(ws2.columns, start=1):

                max_length = 0

                column = get_column_letter(idx)

                for cell in col:

                    try:

                        if cell.row == 1:

                            continue

                        if len(str(cell.value)) > max_length:

                            max_length = len(str(cell.value))

                    except:

                        pass

                ws2.column_dimensions[column].width = max_length + 2



        except Exception as e:

            print(f"Error populating WS2 (Sales Report by Values): {e}")



        # ==========================================

        # POPULATE WS3 (Sales Numbers)

        # ==========================================

        try:

            if not rows_ws3 and not is_sap_session:

                print(f"[Excel Export] Executing sp_sales_summary_in_no_and_values_for_month for WS3...", flush=True)

                try:

                    if session_id and sync_row and db_to_use:

                        cursor.execute(f"CALL `{db_to_use}`.sp_sales_summary_in_no_and_values_for_month(%s, %s, %s)", (year, month, day))

                    else:

                        cursor.execute("CALL sp_sales_summary_in_no_and_values_for_month(%s, %s, %s)", (year, month, day))

                except Exception as e:

                    print(f"[Excel Export] WS3 SP failed: {e}", flush=True)

                print(f"[Excel Export] sp_sales_summary_in_no_and_values_for_month finished.", flush=True)

                    

                if is_mysql_connector:

                    try:

                        if cursor.description is not None:

                            rows_ws3 = cursor.fetchall()

                    except Exception: pass

                    

                    for result in cursor.stored_results():

                        fetched = result.fetchall()

                        if not rows_ws3 and fetched:

                            rows_ws3 = fetched

                            

                    while True:

                        try:

                            if not cursor.nextset(): break

                            if cursor.description is not None: cursor.fetchall()

                        except Exception: break

                else:

                    if cursor.description is not None:

                        rows_ws3 = cursor.fetchall()

                    while True:

                        try:

                            if not cursor.nextset(): break

                            if cursor.description is not None: cursor.fetchall()

                        except Exception: break



            print(f"[Excel Export] sp_sales_summary_in_no_and_values_for_month returned {len(rows_ws3)} rows.", flush=True)



            ws3.merge_cells('A1:V1') # 22 columns (A to V)

            ws3['A1'].value = f"Sales Summary in No's & Values for {month_abbr}-{curr_year_str}"

            ws3['A1'].font = bold_font

            ws3['A1'].alignment = Alignment(horizontal='center', vertical='center')

            

            ws3_title_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

            for c in range(1, 23):

                ws3.cell(row=1, column=c).border = border

                ws3.cell(row=1, column=c).fill = ws3_title_fill



            if rows_ws3:

                fill_ws3_header = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid") # White

                

                # Row 2: Grouped Headers

                ws3.merge_cells('A2:A3')

                ws3['A2'].value = "Zone"

                ws3.merge_cells('B2:B3')

                ws3['B2'].value = "Type"

                

                ws3.merge_cells('C2:E2')

                ws3['C2'].value = "Sales Values in( In Crores )"

                

                single_headers = {

                    6: "TBB\nTotal", 7: "TBR\nTotal",

                    8: "LCV\nBias", 9: "LCV\nRdl", 10: "SCV\nBias", 11: "SCV\nRdl",

                    12: "Car\nBias", 13: "Car\nRadial", 14: "Jeep\nBias", 15: "Jeep\nRadial",

                    16: "Tr. Fro", 17: "Tr. Rear", 18: "Trail", 19: "ADV", 20: "3 Whlrs",

                    21: "Scooter", 22: "Motor"

                }

                

                for col, val in single_headers.items():

                    ws3.merge_cells(start_row=2, start_column=col, end_row=3, end_column=col)

                    ws3.cell(row=2, column=col).value = val



                for c in range(1, 23):

                    cell = ws3.cell(row=2, column=c)

                    cell.font = bold_font

                    cell.alignment = center_aligned_text

                    cell.fill = fill_ws3_header

                    cell.border = border



                # Row 3: Sub Headers

                sub_h = {

                    3: "4 Whlrs.", 4: "2/3 Whlrs", 5: "Total"

                }

                for col, val in sub_h.items():

                    cell = ws3.cell(row=3, column=col, value=val)

                    cell.font = bold_font

                    cell.alignment = center_aligned_text

                    cell.fill = fill_ws3_header

                    cell.border = border

                

                # Apply borders to Row 3 merged cells

                for c in [1, 2] + list(single_headers.keys()):

                    ws3.cell(row=3, column=c).border = border

                    ws3.cell(row=3, column=c).fill = fill_ws3_header

                    

                # Data rows start at 4

                r_idx = 4

                

                fill_achv = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid") # Light green



                # Bind directly from SP, which already has Actual, Target, Achv % and Grand Total rows

                for r in rows_ws3:

                    vals = list(r.values()) # 22 columns

                    zone_name = str(vals[0]).strip() if vals[0] is not None else ''

                    row_type = str(vals[1]).strip() if len(vals) > 1 and vals[1] is not None else ''

                    

                    is_grand_total = "Grand Total" in zone_name

                    is_target = "rget" in row_type.lower()

                    

                    for c_idx in range(1, 23):

                        val = vals[c_idx - 1] if c_idx - 1 < len(vals) else ''

                        if val is None:

                            val = ''

                            

                        cell = ws3.cell(row=r_idx, column=c_idx, value=val)

                        cell.border = border

                        

                        if is_grand_total or is_target:

                            cell.fill = fill_achv

                            

                        if is_grand_total:

                            cell.font = bold_font

                            

                        if c_idx > 2:

                            cell.alignment = Alignment(horizontal='right')

                        else:

                            cell.alignment = center_aligned_text

                            

                    r_idx += 1

                

                # Merge Zone cells vertically (every 3 rows, stopping before Grand Total)

                start_r = 4

                while start_r < r_idx:

                    cell_val = ws3.cell(row=start_r, column=1).value

                    if cell_val and "Grand Total" in str(cell_val):

                        # Merge Grand total's Zone cell spanning 3 rows

                        if start_r + 2 < r_idx:

                            ws3.merge_cells(start_row=start_r, start_column=1, end_row=start_r+2, end_column=1)

                            cell = ws3.cell(row=start_r, column=1)

                            cell.alignment = Alignment(horizontal='center', vertical='center')

                        break

                    

                    if start_r + 2 < r_idx:

                        ws3.merge_cells(start_row=start_r, start_column=1, end_row=start_r+2, end_column=1)

                        cell = ws3.cell(row=start_r, column=1)

                        cell.alignment = Alignment(horizontal='center', vertical='center')

                        cell.font = bold_font

                    start_r += 3

                    

                from openpyxl.utils import get_column_letter

                for idx, col in enumerate(ws3.columns, start=1):

                    max_length = 0

                    column = get_column_letter(idx)

                    for cell in col:

                        try:

                            if len(str(cell.value)) > max_length:

                                max_length = len(str(cell.value))

                        except:

                            pass

                    ws3.column_dimensions[column].width = max_length + 2



        except Exception as e:

            print(f"Error populating WS3 (Sales Numbers): {e}")



        # === MERGE SHEETS VERTICALLY ===

        if return_worksheets:

            return [ws, ws1, ws2, ws3]

            

        import copy

        import traceback

        def copy_sheet_vertically(source_ws, target_ws, start_row_offset):

            for row in source_ws.iter_rows():

                for cell in row:

                    if cell.value is None and not cell.has_style:

                        continue

                    

                    try:

                        col_idx = getattr(cell, 'col_idx', getattr(cell, 'column', 1))

                        new_cell = target_ws.cell(row=start_row_offset + cell.row - 1, column=col_idx, value=cell.value)

                        if cell.has_style:

                            if cell.font: new_cell.font = copy.copy(cell.font)

                            if cell.border: new_cell.border = copy.copy(cell.border)

                            if cell.fill: new_cell.fill = copy.copy(cell.fill)

                            if cell.alignment: new_cell.alignment = copy.copy(cell.alignment)

                            if cell.number_format: new_cell.number_format = cell.number_format

                    except Exception as e:

                        with open("error_log.txt", "a") as f:

                            f.write(f"Cell copy error: {e}\n")

            

            for merged_cell_range in source_ws.merged_cells.ranges:

                try:

                    min_col, min_row, max_col, max_row = merged_cell_range.bounds

                    target_ws.merge_cells(

                        start_row=min_row + start_row_offset - 1,

                        start_column=min_col,

                        end_row=max_row + start_row_offset - 1,

                        end_column=max_col

                    )

                except Exception as e:

                    with open("error_log.txt", "a") as f:

                        f.write(f"Merge error: {e}\n")



        try:

            current_max_row = ws.max_row

            

            current_max_row += 2

            copy_sheet_vertically(ws1, ws, current_max_row)

            current_max_row = ws.max_row

            

            current_max_row += 2

            copy_sheet_vertically(ws2, ws, current_max_row)

            current_max_row = ws.max_row

            

            current_max_row += 2

            copy_sheet_vertically(ws3, ws, current_max_row)

            

            # Remove old sheets

            wb.remove(ws1)

            wb.remove(ws2)

            wb.remove(ws3)

            

            ws.title = "Summary Revised"

        except Exception as e:

            with open("error_log.txt", "w") as f:

                f.write(f"Error merging sheets: {traceback.format_exc()}\n")



        import os

        file_name = f"Summary Revised_{year}_{month}_{day}.xlsx"

        output_path = os.path.join(os.getcwd(), file_name)

        wb.save(output_path)

        

        return output_path



    except Exception as e:

        print(f"Error generating excel internally: {e}")

        return None

    finally:

        if cursor:

            cursor.close()



def export_domestic_sales_report_controller(get_db_connection):

    conn = get_db_connection()

    if not conn:

        return jsonify({"status": "error", "message": "Database connection failed"}), 500



    try:

        # Check if the request is sending JSON body

        data = request.get_json(silent=True) or {}

        

        from datetime import datetime

        now = datetime.now()

        

        session_id = data.get("session_id") or request.args.get("session_id")

        year = data.get("year") or request.args.get("year")

        month = data.get("month") or request.args.get("month")

        day = data.get("day") or request.args.get("day")

        

        if not year or not month or not day:

            cursor = None

            try:

                try:

                    cursor = conn.cursor(dictionary=True)

                except TypeError:

                    import pymysql

                    cursor = conn.cursor(pymysql.cursors.DictCursor)

                

                db_to_use = None

                if session_id:

                    cursor.execute("""

                        SELECT new_user_db, external_database

                        FROM external_db_sync_log 

                        WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''

                        ORDER BY id DESC LIMIT 1

                    """, (session_id,))

                    row = cursor.fetchone()

                    if row:

                        db_to_use = row.get('new_user_db') or row.get('external_database')

                        

                table_prefix = f"`{db_to_use}`." if db_to_use else ""

                cursor.execute(f"SELECT MAX(billing__doc_date) as max_dt FROM {table_prefix}sales_data WHERE billing__doc_date <= CURDATE()")

                max_dt_row = cursor.fetchone()

                if max_dt_row and max_dt_row.get('max_dt'):

                    max_dt = max_dt_row['max_dt']

                    if not year: year = max_dt.year

                    if not month: month = max_dt.month

                    if not day: day = max_dt.day

            except Exception as e:

                print(f"[Excel Export] Failed to fetch max date: {e}")

            finally:

                if cursor:

                    try: cursor.close()

                    except: pass

                    

        # Fallback to now if DB didn't have data

        if not year: year = now.year

        if not month: month = now.month

        if not day: day = now.day



        # Convert to int just in case they were passed as strings in JSON

        year = int(year)

        month = int(month)

        day = int(day)



        output_path = generate_domestic_sales_excel(conn, session_id, year, month, day)

        

        if not output_path:

            return jsonify({"status": "error", "message": "Failed to generate excel file"}), 500

            

        import os

        from flask import send_file

        file_name = os.path.basename(output_path)

        return send_file(output_path, as_attachment=True, download_name=file_name)

        

    except Exception as e:

        print(f"Error in export_domestic_sales_report_controller: {e}")

        return jsonify({"status": "error", "message": str(e)}), 500

    finally:

        try:

            if conn and conn.is_connected():

                # Consume any trailing results before closing

                try:

                    conn.commit()

                except Exception:

                    pass

                conn.close()

        except Exception:

            try:

                conn.close()

            except: pass





def export_domestic_sales_preview_controller(get_db_connection):

    """Returns JSON preview data for the 4 report sections exactly as generated for Excel."""

    conn = get_db_connection()

    if not conn:

        return jsonify({"status": "error", "message": "Database connection failed"}), 500



    try:

        data = request.get_json(silent=True) or {}

        from datetime import datetime

        now = datetime.now()



        session_id = data.get("session_id") or request.args.get("session_id")

        year = data.get("year") or request.args.get("year")

        month = data.get("month") or request.args.get("month")

        day = data.get("day") or request.args.get("day")

        filter_context = f"{year or 'all'}_{month or 'all'}_{day or 'all'}"
        cached_prev = get_cached_component_data(conn, session_id, "domestic_sales_preview", filter_context)
        if not cached_prev:
            cached_prev = get_cached_component_data(conn, session_id, "domestic_sales_preview", "all")
        if cached_prev and isinstance(cached_prev.get("data"), dict) and cached_prev["data"].get("status") == "success":
            return jsonify(cached_prev["data"]), 200

        

        if not year or not month or not day:

            try:

                from helper.sap_analytics_service import get_sap_connection_for_session, detect_sales_dates

                hana_conn, tunnel, schema = get_sap_connection_for_session(conn, session_id)

                if hana_conn:

                    try:

                        h_cur = hana_conn.cursor()

                        year, month, day = detect_sales_dates(h_cur, schema, year, month, day)

                        h_cur.close()

                    finally:

                        hana_conn.close()

                        if tunnel:

                            tunnel.stop()

            except Exception as e:

                print(f"[Excel Export Preview] SAP date auto-detection failed: {e}")



        if not year or not month or not day:

            cursor = None

            try:

                try:

                    cursor = conn.cursor(dictionary=True)

                except TypeError:

                    import pymysql

                    cursor = conn.cursor(pymysql.cursors.DictCursor)

                

                db_to_use = None

                if session_id:

                    cursor.execute("""

                        SELECT new_user_db, external_database

                        FROM external_db_sync_log 

                        WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db != ''

                        ORDER BY id DESC LIMIT 1

                    """, (session_id,))

                    row = cursor.fetchone()

                    if row:

                        db_to_use = row.get('new_user_db') or row.get('external_database')

                        

                table_prefix = f"`{db_to_use}`." if db_to_use else ""

                cursor.execute(f"SELECT MAX(billing__doc_date) as max_dt FROM {table_prefix}sales_data WHERE billing__doc_date <= CURDATE()")

                max_dt_row = cursor.fetchone()

                if max_dt_row and max_dt_row.get('max_dt'):

                    max_dt = max_dt_row['max_dt']

                    if not year: year = max_dt.year

                    if not month: month = max_dt.month

                    if not day: day = max_dt.day

            except Exception as e:

                print(f"[Excel Export Preview] Failed to fetch max date: {e}")

            finally:

                if cursor:

                    try: cursor.close()

                    except: pass



        # Fallback to now if DB didn't have data

        if not year: year = now.year

        if not month: month = now.month

        if not day: day = now.day



        year = int(year)

        month = int(month)

        day = int(day)



        import calendar

        month_abbr    = calendar.month_abbr[month] if 1 <= month <= 12 else str(month)

        curr_year_str = str(year)[-2:]

        prev_year_str = str(year - 1)[-2:]



        # Get the worksheets in memory (this doesn't save to disk)

        worksheets = generate_domestic_sales_excel(conn, session_id, year, month, day, return_worksheets=True)

        if not worksheets or len(worksheets) < 4:

            raise Exception("Failed to generate Excel worksheets for preview.")



        def extract_ws_matrix(worksheet):

            # 1. Identify merged cells

            merged_map = {}

            for merged_range in worksheet.merged_cells.ranges:

                min_col, min_row, max_col, max_row = merged_range.bounds

                # Mark top-left as root

                merged_map[(min_row, min_col)] = {

                    "rowSpan": max_row - min_row + 1,

                    "colSpan": max_col - min_col + 1

                }

                # Mark others as skipped

                for r in range(min_row, max_row + 1):

                    for c in range(min_col, max_col + 1):

                        if r == min_row and c == min_col:

                            continue

                        merged_map[(r, c)] = {"skip": True}



            matrix = []

            max_r = worksheet.max_row

            max_c = worksheet.max_column



            for r in range(1, max_r + 1):

                row_data = []

                for c in range(1, max_c + 1):

                    merge_info = merged_map.get((r, c))

                    if merge_info and merge_info.get("skip"):

                        continue

                    

                    cell = worksheet.cell(row=r, column=c)

                    val = cell.value

                    

                    # Formatter logic matching excel styling visually where possible

                    display_val = ""

                    if isinstance(val, (int, float)) or type(val).__name__ == 'Decimal':

                        is_percent = False

                        if cell.number_format and '%' in cell.number_format:

                            is_percent = True

                        elif cell.value is not None and isinstance(cell.value, str) and '%' in cell.value:

                            is_percent = True

                            

                        if is_percent:

                            # if it's already a string with %, we don't multiply. But val is float here

                            display_val = f"{int(round(float(val) * 100))}%"

                        else:

                            if isinstance(val, float) or type(val).__name__ == 'Decimal':

                                display_val = f"{float(val):.2f}"

                            else:

                                display_val = str(val)

                    elif val is None:

                        display_val = ""

                    else:

                        display_val = str(val)



                    # Try to extract bgColor

                    bgColor = None

                    if cell.fill and cell.fill.start_color and hasattr(cell.fill.start_color, 'index'):

                        idx = cell.fill.start_color.index

                        if isinstance(idx, str) and idx not in ("00000000", "FFFFFFFF", "0"):

                            if len(idx) == 8:

                                bgColor = "#" + idx[2:]

                            elif len(idx) == 6:

                                bgColor = "#" + idx



                    is_bold = bool(cell.font and cell.font.bold)

                    align = cell.alignment.horizontal if cell.alignment and cell.alignment.horizontal else "left"



                    cell_dict = {

                        "value": display_val,

                        "colSpan": merge_info.get("colSpan", 1) if merge_info else 1,

                        "rowSpan": merge_info.get("rowSpan", 1) if merge_info else 1,

                        "bgColor": bgColor,

                        "bold": is_bold,

                        "align": align

                    }

                    row_data.append(cell_dict)

                matrix.append(row_data)



            # Trim trailing empty rows to keep the payload clean

            while matrix and all(cell.get("value") == "" for cell in matrix[-1]):

                matrix.pop()

            return matrix



        # The sheets are returned as: ws (Sheet 1), ws1 (Sheet 2), ws2 (Sheet 3), ws3 (Sheet 4)

        m1 = extract_ws_matrix(worksheets[0])

        m2 = extract_ws_matrix(worksheets[1])

        m3 = extract_ws_matrix(worksheets[2])

        m4 = extract_ws_matrix(worksheets[3])



        result_payload = {
            "status": "success",
            "meta": {
                "year": year, "month": month, "day": day,
                "month_abbr": month_abbr,
                "curr_year": curr_year_str, "prev_year": prev_year_str
            },
            "sections": {
                "s1": {"title": f"Domestic Sales Value Achievement - {month_abbr}-{curr_year_str}", "matrix": m1},
                "s2": {"title": f"Sales Numbers - {month_abbr}-{curr_year_str}", "matrix": m2},
                "s3": {"title": f"Sales Report by Values (Cr) - {month_abbr}-{curr_year_str}", "matrix": m3},
                "s4": {"title": f"Sales Summary in No and Values - {month_abbr}-{curr_year_str}", "matrix": m4}
            }
        }
        set_cached_component_data(conn, session_id, "domestic_sales_preview", result_payload, "all")
        return jsonify(result_payload), 200

    except Exception as e:

        import traceback

        print(f"[Preview] Error: {traceback.format_exc()}")

        return jsonify({"status": "error", "message": str(e)}), 500

    finally:

        try:

            if conn and conn.is_connected():

                # Consume any trailing results before closing

                try: conn.commit()

                except Exception: pass

                conn.close()

        except Exception:

            try: conn.close()

            except: pass







import csv

import re

from flask import Response, make_response



def export_query_csv_controller(get_db_connection):

    """

    On-demand streaming CSV export for query results.

    Executes the SQL query on live database (SAP HANA or MySQL) and streams 

    the result as a CSV attachment with EXACT database column names as headers.

    Zero files are saved on disk.

    """

    if request.method == "OPTIONS":

        resp = make_response("", 200)

        resp.headers["Access-Control-Allow-Origin"] = "*"

        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"

        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept, user-id, user_id, *"

        return resp



    conn = None

    hana_conn = None

    tunnel = None

    cursor = None

    try:

        data = request.json or {}

        session_id = data.get("session_id")

        sql_query = data.get("sql_query", "").strip()

        query_b64 = data.get("query_b64") or data.get("sql_query_b64") or ""
        if not sql_query and query_b64:
            import base64
            try:
                sql_query = base64.b64decode(query_b64).decode("utf-8").strip()
            except Exception as e:
                print(f"[CSV Export] Base64 decode error: {e}", flush=True)

        filename = data.get("filename", "").strip()

        user_id = data.get("user_id") or request.headers.get("user-id") or request.headers.get("user_id")

        # Strip markdown code blocks and trailing semicolons
        sql_query = re.sub(r"^```(?:sql)?\s*", "", sql_query, flags=re.IGNORECASE)
        sql_query = re.sub(r"\s*```$", "", sql_query)
        sql_query = sql_query.strip().rstrip("; \t\n\r")
        print(f"[CSV Export] Prepared query for execution (len {len(sql_query)}): {sql_query[:100]}...", flush=True)



        if not sql_query:

            resp = jsonify({"status": "error", "message": "Missing sql_query"})

            resp.headers["Access-Control-Allow-Origin"] = "*"

            return resp, 400



        if not filename:

            filename = f"export_{session_id or 'data'}.csv"

        if not filename.endswith(".csv"):

            filename += ".csv"



        # Sanitize filename

        safe_filename = re.sub(r'[^a-zA-Z0-9_.-]+', '_', filename)



        conn = get_db_connection()

        if not conn:

            resp = jsonify({"status": "error", "message": "Failed to connect to database"})

            resp.headers["Access-Control-Allow-Origin"] = "*"

            return resp, 500



        from controllers.dashboard_visuals import get_sap_connection_for_session
        hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, session_id, user_id=user_id)
        # Fallback 1: Resolve actual workspace session_id if needed
        if not hana_conn and session_id:
            try:
                cur_ws = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
                cur_ws.execute("SELECT session_id, user_id FROM workspaces WHERE id = %s OR name = %s OR session_id = %s LIMIT 1", (str(session_id), str(session_id), str(session_id)))
                ws_row = cur_ws.fetchone()
                if ws_row and ws_row.get("session_id"):
                    hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, ws_row["session_id"], user_id=ws_row.get("user_id") or user_id)
                try: cur_ws.close()
                except Exception: pass
            except Exception as e:
                print(f"[CSV Export] Workspace lookup note: {e}", flush=True)
        # Fallback 2: Any active SAP HANA credential in database_credential
        if not hana_conn:
            try:
                cur_fb = conn.cursor(dictionary=True) if hasattr(conn, "cursor") else conn.cursor()
                cur_fb.execute("SELECT session_id, user_id FROM database_credential WHERE LOWER(TRIM(db_type)) IN ('hana', 'sap_hana', 'saphana', 'sap', 'sap_datasphere') ORDER BY id DESC LIMIT 1")
                fb_row = cur_fb.fetchone()
                if fb_row:
                    hana_conn, tunnel, sap_schema = get_sap_connection_for_session(conn, fb_row.get("session_id"), user_id=fb_row.get("user_id") or user_id)
                try: cur_fb.close()
                except Exception: pass
            except Exception as e:
                print(f"[CSV Export] Credential fallback note: {e}", flush=True)
        is_hana = bool(hana_conn)
        print(f"[CSV Export] HANA Connection resolved: {is_hana}, schema: {sap_schema}", flush=True)



        if is_hana:

            cursor = hana_conn.cursor()

            schema = sap_schema or "SAP_S4"

            try:

                cursor.execute(f'SET SCHEMA "{schema}"')

            except Exception:

                pass

            cursor.execute(sql_query)

        else:

            cursor = conn.cursor()

            cursor.execute(sql_query)



        # Extract EXACT column names from database cursor
        cols = [desc[0] for desc in cursor.description] if cursor.description else []

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(cols)

        # Write data rows
        rows = cursor.fetchall()
        for r in rows:
            if r is None:
                row_vals = ["" for _ in cols]
            elif isinstance(r, dict):
                row_vals = [str(r.get(c)) if r.get(c) is not None else "" for c in cols]
            else:
                # Handle stringified tuple/list representation if present
                if isinstance(r, str):
                    s = r.strip()
                    if (s.startswith("(") and s.endswith(")")) or (s.startswith("[") and s.endswith("]")):
                        import ast
                        try:
                            parsed = ast.literal_eval(s)
                            if isinstance(parsed, (list, tuple)):
                                r = parsed
                        except Exception:
                            try:
                                import csv as _csv, io as _io
                                parsed = next(_csv.reader(_io.StringIO(s[1:-1]), quotechar="'", skipinitialspace=True))
                                if parsed:
                                    r = parsed
                            except Exception:
                                pass

                # Handle tuple, list, hdbcli ResultRow, pyodbc.Row, or any sequence/iterable
                if isinstance(r, (list, tuple)) or (hasattr(r, "__iter__") and not isinstance(r, (str, bytes, dict))):
                    vals = list(r)
                    # Check if single item in vals was a stringified tuple
                    if len(vals) == 1 and len(cols) > 1 and isinstance(vals[0], str):
                        s = vals[0].strip()
                        if (s.startswith("(") and s.endswith(")")) or (s.startswith("[") and s.endswith("]")):
                            import ast
                            try:
                                parsed = ast.literal_eval(s)
                                if isinstance(parsed, (list, tuple)):
                                    vals = list(parsed)
                            except Exception:
                                try:
                                    import csv as _csv, io as _io
                                    parsed = next(_csv.reader(_io.StringIO(s[1:-1]), quotechar="'", skipinitialspace=True))
                                    if parsed:
                                        vals = list(parsed)
                                except Exception:
                                    pass
                    row_vals = [str(val) if val is not None else "" for val in vals]
                elif hasattr(r, "__getitem__") and hasattr(r, "__len__") and len(r) == len(cols):
                    row_vals = [str(r[i]) if r[i] is not None else "" for i in range(len(cols))]
                else:
                    row_vals = [str(r)]

            writer.writerow(row_vals)

        # Prepend UTF-8 BOM so Microsoft Excel correctly renders all columns and UTF-8 characters
        csv_content = "\ufeff" + output.getvalue()
        output.close()

        print(f"[CSV Export] Streamed {len(rows)} rows with exact columns: {cols} as '{safe_filename}'", flush=True)



        resp = Response(

            csv_content,

            mimetype="text/csv",

            headers={

                "Content-Disposition": f"attachment; filename={safe_filename}",

                "Content-Type": "text/csv; charset=utf-8",

                "Access-Control-Allow-Origin": "*",

                "Access-Control-Allow-Methods": "POST, OPTIONS",

                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With, Accept, user-id, user_id, *",

            }

        )

        return resp



    except Exception as exc:

        print(f"[CSV Export] Query execution failed: {exc}", flush=True)

        resp = jsonify({"status": "error", "message": str(exc)})

        resp.headers["Access-Control-Allow-Origin"] = "*"

        return resp, 500



    finally:

        if cursor:

            try: cursor.close()

            except Exception: pass

        if hana_conn:

            try: hana_conn.close()

            except Exception: pass

        if tunnel:

            try: tunnel.stop()

            except Exception: pass

        if conn and hasattr(conn, "is_connected") and conn.is_connected():

            try: conn.close()

            except Exception: pass

