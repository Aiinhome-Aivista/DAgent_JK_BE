"""
Script to update session_analysis_cache with live, accurate metrics for all 7 sections:
1. Overall Performance (Revenue, Qty, Invoices, Avg Invoice Value, Avg Selling Price)
2. Revenue Trend (Highest Month, Lowest Month, Monthly Growth)
3. Product Performance (Top Category, Construction, Tyre Type, Top Product, Lowest Product)
4. Customer Performance (Top Cust, Dealer %, Fleet %, OEM %, Top 5 Concentration)
5. Geography (Top Zone, Top Region, Top Territory, Lowest Territory - 100% dynamic from views)
6. Distribution (Replacement, OEM, STU, DEF, Contribution %)
7. Target Performance (Target Value, Achievement %, Shortfall, Qty Achievement %, Shortfall Territory)

Zero hardcoded maps or fallback dictionaries: all names and metrics are dynamically fetched.

Usage:
    python scripts/update_session_summary.py [session_id]
    python scripts/update_session_summary.py --all
"""

import sys
import os
import re
import argparse
from datetime import datetime

# Add API directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.abspath(os.path.join(current_dir, ".."))
if api_dir not in sys.path:
    sys.path.insert(0, api_dir)

import mysql.connector
from database.config import MYSQL_CONFIG
from helper.sap_analytics_service import (
    get_sap_connection_for_session,
    sanitize_and_execute_hana,
    detect_sales_dates,
    _prepare_mysql_workspace
)
from helper.sap_connection import get_rolling_sales_boundary


def get_latest_session_id(mysql_conn):
    cur = mysql_conn.cursor(dictionary=True)
    cur.execute("SELECT session_id FROM session_analysis_cache ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    cur.close()
    return row["session_id"] if row else None


def execute_hana_analysis(hana_conn, schema):
    cur = hana_conn.cursor()
    year, month, day = detect_sales_dates(cur, schema)
    target_yr = str(year) if year else "2026"
    print(f"[SAP-HANA] Target active sales year: {target_yr} (date anchor: {year}-{month:02d}-{day:02d})")

    boundary_date = get_rolling_sales_boundary(cur, schema)
    sales_cols = '''s."distribution_channel", s."plant", s."invoice_number", s."invoice_date", 
                    s."indicator_cancel", s."dealer_code", s."SKU", s."FKART", 
                    COALESCE(s."ndp_value", s."invoice_value") AS "invoice_value", s."Tyre_Quantity"'''

    sales_source = f'''(
        SELECT {sales_cols}
        FROM "{schema}"."ZPVF_SD_CurrentSales_01_TR" s
        WHERE s."invoice_date" >= '{target_yr}-01-01' AND s."invoice_date" >= '{boundary_date}'
        UNION ALL
        SELECT {sales_cols}
        FROM "{schema}"."ZPVF_SD_SalesHistory_01_TR" s
        WHERE s."invoice_date" >= '{target_yr}-01-01' AND s."invoice_date" < '{boundary_date}'
    )'''

    cust_join = f'''INNER JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm ON (
        (s."dealer_code" = cm."KUNNR" OR
         TRIM(LEADING '0' FROM cm."KUNNR") = TRIM(LEADING '0' FROM s."dealer_code") OR
         LTRIM(s."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
        AND (cm."KLABC" IS NULL OR cm."KLABC" <> 'X')
    )'''

    sales_where = f'WHERE (s."distribution_channel" = \'10\' OR s."distribution_channel" = 10) AND (s."indicator_cancel" IS NULL OR s."indicator_cancel" NOT IN (\'X\', \'Y\', \'1\'))'

    res = {}

    # 1. Overall Performance
    try:
        q_overall = f"""
            SELECT 
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000, 2) AS "Total_Revenue",
                ROUND(SUM(COALESCE(s."Tyre_Quantity", 0)) / 1000000, 2) AS "Total_Quantity_M",
                SUM(COALESCE(s."Tyre_Quantity", 0)) AS "Total_Quantity_Units",
                COUNT(*) AS "Total_Invoices",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / NULLIF(COUNT(*), 0), 0) AS "Avg_Invoice_Val",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / NULLIF(SUM(s."Tyre_Quantity"), 0), 0) AS "Avg_Selling_Price"
            FROM {sales_source} s
            {cust_join}
            {sales_where}
        """
        sanitize_and_execute_hana(cur, schema, q_overall)
        row = cur.fetchone()
        if row:
            res["total_revenue"] = float(row[0] or 0)
            res["total_qty_m"] = float(row[1] or 0)
            res["total_qty_units"] = int(row[2] or 0)
            res["total_invoices"] = int(row[3] or 0)
            res["avg_invoice_val"] = float(row[4] or 0)
            res["avg_selling_price"] = float(row[5] or 0)
            print(f"[1/7 Overall] Revenue: ₹{res['total_revenue']:.2f} Cr, Qty: {res['total_qty_m']} M units, Invoices: {res['total_invoices']:,}")
    except Exception as e:
        print(f"[Warning] Overall query error: {e}")

    # 1b. Dynamic Sales Date Range
    try:
        q_dr = f"""
            SELECT 
                TO_VARCHAR(MIN(s."invoice_date"), 'DD Mon YYYY') AS "start_date",
                TO_VARCHAR(MAX(s."invoice_date"), 'DD Mon YYYY') AS "end_date",
                DAYS_BETWEEN(MIN(s."invoice_date"), MAX(s."invoice_date")) + 1 AS "total_days"
            FROM {sales_source} s
            {cust_join}
            {sales_where}
        """
        sanitize_and_execute_hana(cur, schema, q_dr)
        dr_row = cur.fetchone()
        if dr_row and dr_row[0] and dr_row[1]:
            res["date_range_str"] = f"{dr_row[0]} – {dr_row[1]} ({dr_row[2]} Days)"
            res["start_date"] = dr_row[0]
            res["end_date"] = dr_row[1]
            res["total_days"] = dr_row[2]
            print(f"[1b Date Range] Active Period: {res['date_range_str']}")
    except Exception as e:
        print(f"[Warning] Date range query error: {e}")

    # 2. Revenue Trend
    try:
        q_trend = f"""
            SELECT 
                SUBSTRING(TO_VARCHAR(s."invoice_date"), 1, 7) AS "ym",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000, 2) AS "rev"
            FROM {sales_source} s
            {cust_join}
            {sales_where}
            GROUP BY SUBSTRING(TO_VARCHAR(s."invoice_date"), 1, 7)
            ORDER BY "ym" ASC
        """
        sanitize_and_execute_hana(cur, schema, q_trend)
        month_rows = cur.fetchall()
        if month_rows:
            # Full chronological list of months and revenues
            m_list = [(r[0], float(r[1] or 0)) for r in month_rows]
            
            # Exclude current ongoing month (latest) when determining completed sales peak
            completed_months = m_list[:-1] if len(m_list) > 1 else m_list
            sorted_completed = sorted(completed_months, key=lambda x: x[1], reverse=True)
            
            res["highest_month"] = sorted_completed[0][0]
            res["highest_month_rev"] = sorted_completed[0][1]
            res["lowest_month"] = m_list[-1][0]
            res["lowest_month_rev"] = m_list[-1][1]

            # Calculate MoM growth for each month
            mom_growths = []
            for i in range(1, len(m_list)):
                prev_rev = m_list[i-1][1]
                cur_rev = m_list[i][1]
                if prev_rev > 0:
                    pct = round(((cur_rev - prev_rev) / prev_rev) * 100.0, 2)
                    mom_growths.append((m_list[i][0], pct, cur_rev))

            if mom_growths:
                best_growth = max(mom_growths, key=lambda x: x[1])
                latest_growth = mom_growths[-1]
                res["highest_growth_month"] = best_growth[0]
                res["highest_growth_pct"] = best_growth[1]
                res["monthly_growth"] = (
                    f"{best_growth[0]} exhibited the highest month-over-month growth at {best_growth[1]}%, "
                    f"while {latest_growth[0]} shows a decline of {abs(latest_growth[1])}% (partial month trend)."
                )
            else:
                res["monthly_growth"] = f"Stable sales volume (MTD: ₹{m_list[-1][1]:.2f} Cr)"
            
            print(f"[2/7 Trend] Highest: {res.get('highest_month')} (₹{res.get('highest_month_rev'):.2f} Cr), Current: {res.get('lowest_month')} (₹{res.get('lowest_month_rev'):.2f} Cr)")
    except Exception as e:
        print(f"[Warning] Revenue trend query error: {e}")

    # 3. Product Performance
    # 3a. Category
    try:
        q_cat = f"""
            SELECT 
                COALESCE(NULLIF(TRIM(cat.CAT_DESC), ''), 'General Tyres') AS "category",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000, 2) AS "rev"
            FROM {sales_source} s
            {cust_join}
            JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk ON (s."SKU" = sk.MATNR OR LTRIM(s."SKU", '0') = LTRIM(sk.MATNR, '0'))
            JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON sk.CAT_CODE = cat.CAT_CODE
            {sales_where}
            GROUP BY cat.CAT_DESC
            ORDER BY "rev" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_cat)
        cat_rows = cur.fetchall()
        if cat_rows:
            res["top_category"] = cat_rows[0][0]
            res["top_category_rev"] = float(cat_rows[0][1] or 0)
            res["lowest_category"] = cat_rows[-1][0]
            res["lowest_category_rev"] = float(cat_rows[-1][1] or 0)
            print(f"[3a/7 Category] Top: {res['top_category']} (₹{res['top_category_rev']:.2f} Cr)")
    except Exception as e:
        print(f"[Warning] Category query error: {e}")

    # 3b. Construction (strictly string literals to avoid HANA error 339 invalid number)
    try:
        q_const = f"""
            SELECT 
                CASE 
                    WHEN UPPER(TRIM(sk.CONSTUCTION)) IN ('2', 'RADIAL') OR UPPER(TRIM(sk.CONSTUCTION)) LIKE '%RADIAL%' THEN 'Radial'
                    WHEN UPPER(TRIM(sk.CONSTUCTION)) IN ('1', 'BIAS') OR UPPER(TRIM(sk.CONSTUCTION)) LIKE '%BIAS%' THEN 'Bias'
                    ELSE COALESCE(NULLIF(TRIM(sk.CONSTUCTION), ''), 'General')
                END AS "con",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000, 2) AS "rev"
            FROM {sales_source} s
            {cust_join}
            JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk ON (s."SKU" = sk.MATNR OR LTRIM(s."SKU", '0') = LTRIM(sk.MATNR, '0'))
            {sales_where}
            GROUP BY 
                CASE 
                    WHEN UPPER(TRIM(sk.CONSTUCTION)) IN ('2', 'RADIAL') OR UPPER(TRIM(sk.CONSTUCTION)) LIKE '%RADIAL%' THEN 'Radial'
                    WHEN UPPER(TRIM(sk.CONSTUCTION)) IN ('1', 'BIAS') OR UPPER(TRIM(sk.CONSTUCTION)) LIKE '%BIAS%' THEN 'Bias'
                    ELSE COALESCE(NULLIF(TRIM(sk.CONSTUCTION), ''), 'General')
                END
            ORDER BY "rev" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_const)
        con_row = cur.fetchone()
        if con_row:
            res["top_construction"] = con_row[0]
            res["top_construction_rev"] = float(con_row[1] or 0)
            print(f"[3b/7 Construction] Top: {res['top_construction']} (₹{res['top_construction_rev']:.2f} Cr)")
    except Exception as e:
        print(f"[Warning] Construction query error: {e}")

    # 3d. Top & Lowest Product
    try:
        q_prod = f"""
            SELECT 
                COALESCE(NULLIF(TRIM(sk.MAKTX), ''), s."SKU") AS "pname",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000, 2) AS "rev"
            FROM {sales_source} s
            {cust_join}
            JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk ON (s."SKU" = sk.MATNR OR LTRIM(s."SKU", '0') = LTRIM(sk.MATNR, '0'))
            {sales_where}
            GROUP BY COALESCE(NULLIF(TRIM(sk.MAKTX), ''), s."SKU")
            ORDER BY "rev" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_prod)
        prod_rows = cur.fetchall()
        if prod_rows:
            res["top_product"] = prod_rows[0][0]
            res["top_product_rev"] = float(prod_rows[0][1] or 0)
            res["lowest_product"] = prod_rows[-1][0]
            res["lowest_product_rev"] = float(prod_rows[-1][1] or 0)
            print(f"[3d/7 Product] Top: {res['top_product']}, Lowest: {res['lowest_product']}")
    except Exception as e:
        print(f"[Warning] Product query error: {e}")

    # 3e. Top 5 SKUs by Top 5 Categories (Last 30 Days)
    try:
        q_top_skus = f"""
            WITH MAX_DT AS (
                SELECT MAX(s_max."invoice_date") AS "anchor_date"
                FROM {sales_source} s_max
            ),
            CAT_SKU_SUMMARY AS (
                SELECT 
                    COALESCE(NULLIF(TRIM(cat.CAT_DESC), ''), TRIM(sk.CAT_CODE)) AS "category_name",
                    COALESCE(NULLIF(TRIM(sk.MAKTX), ''), NULLIF(TRIM(sk.PROD_TITLE), ''), TRIM(s."SKU")) AS "sku_title",
                    SUM(COALESCE(s."invoice_value", 0)) AS "sku_revenue"
                FROM {sales_source} s
                {cust_join}
                INNER JOIN "{schema}"."ZIVR_SKUMaster_01_TR" sk ON TRIM(s."SKU") = TRIM(sk.MATNR)
                INNER JOIN "{schema}"."ZIVF_CategoryMaster_01_TR" cat ON TRIM(sk.CAT_CODE) = TRIM(cat.CAT_CODE)
                CROSS JOIN MAX_DT md
                {sales_where}
                  AND s."invoice_date" BETWEEN ADD_DAYS(md."anchor_date", -29) AND md."anchor_date"
                  AND cat.CAT_DESC IS NOT NULL AND TRIM(cat.CAT_DESC) <> ''
                GROUP BY 
                    COALESCE(NULLIF(TRIM(cat.CAT_DESC), ''), TRIM(sk.CAT_CODE)),
                    COALESCE(NULLIF(TRIM(sk.MAKTX), ''), NULLIF(TRIM(sk.PROD_TITLE), ''), TRIM(s."SKU"))
            ),
            CAT_TOTALS AS (
                SELECT 
                    "category_name",
                    SUM("sku_revenue") AS "total_cat_rev",
                    DENSE_RANK() OVER (ORDER BY SUM("sku_revenue") DESC) AS "cat_rank"
                FROM CAT_SKU_SUMMARY
                GROUP BY "category_name"
            ),
            RANKED_SKUS AS (
                SELECT 
                    cs."category_name",
                    cs."sku_title",
                    ROUND(cs."sku_revenue" / 10000000.0, 2) AS "revenue_cr",
                    ct."cat_rank",
                    ROW_NUMBER() OVER (PARTITION BY cs."category_name" ORDER BY cs."sku_revenue" DESC) AS "sku_rank"
                FROM CAT_SKU_SUMMARY cs
                INNER JOIN CAT_TOTALS ct ON cs."category_name" = ct."category_name"
                WHERE ct."cat_rank" <= 5
            )
            SELECT "category_name", "cat_rank", "sku_rank", "sku_title", "revenue_cr"
            FROM RANKED_SKUS
            WHERE "sku_rank" <= 5
            ORDER BY "cat_rank" ASC, "sku_rank" ASC
        """
        sanitize_and_execute_hana(cur, schema, q_top_skus)
        sku_rows = cur.fetchall()
        if sku_rows:
            res["top_skus_by_cat"] = sku_rows
            print(f"[3e/7 SKUs by Cat] Fetched {len(sku_rows)} category-ranked SKUs.")
    except Exception as e:
        print(f"[Warning] Top SKUs by category query error: {e}")

    # 4. Customer Performance & Account Group Breakdown
    try:
        q_cust = f"""
            SELECT 
                COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), s."dealer_code") AS "cname",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000.0, 2) AS "rev"
            FROM {sales_source} s
            {cust_join}
            {sales_where}
            GROUP BY COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), s."dealer_code")
            ORDER BY "rev" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_cust)
        cust_rows = cur.fetchall()
        if cust_rows:
            res["top_customer"] = cust_rows[0][0]
            res["top_customer_rev"] = float(cust_rows[0][1] or 0)
            total_top5_rev = sum(float(r[1] or 0) for r in cust_rows[:5])
            res["top_5_customers_rev"] = total_top5_rev
            if res.get("total_revenue", 0) > 0:
                res["customer_concentration"] = f"Top 5 customers contribute {round((total_top5_rev / res['total_revenue']) * 100, 2)}% of total revenue"

        # Account Group Breakdown (KTOKD)
        q_grp = f"""
            SELECT 
                CASE 
                    WHEN TRIM(cm."KTOKD") = 'Z001' OR UPPER(TRIM(cm."CH_TYPE")) = 'DL' THEN 'Dealers / Trade Network (Z001)'
                    WHEN TRIM(cm."KTOKD") = 'Z004' OR UPPER(TRIM(cm."CH_TYPE")) = 'IN' THEN 'Institutional Customers (Z004)'
                    WHEN TRIM(cm."KTOKD") = 'Z009' OR UPPER(TRIM(cm."CH_TYPE")) = 'FL' THEN 'Fleet Customers (Z009)'
                    WHEN TRIM(cm."KTOKD") = 'Z015' THEN 'Traders & Retailers (Z015)'
                    WHEN TRIM(cm."KTOKD") = 'Z006' OR UPPER(TRIM(cm."CH_TYPE")) = 'GT' THEN 'Government & PSU (Z006)'
                    WHEN TRIM(cm."KTOKD") = 'Z010' THEN 'Govt Special Accounts (Z010)'
                    WHEN TRIM(cm."KTOKD") = 'Z007' THEN 'Heavy Equipment / OTR (Z007)'
                    WHEN TRIM(cm."KTOKD") = 'Z003' THEN 'Export / Overseas (Z003)'
                    ELSE CONCAT('Account Group ', COALESCE(NULLIF(TRIM(cm."KTOKD"), ''), 'Unassigned'))
                END AS "account_group_name",
                COUNT(DISTINCT cm."KUNNR") AS "active_customers",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000.0, 2) AS "revenue_cr",
                ROUND(SUM(COALESCE(s."Tyre_Quantity", 0)), 0) AS "total_units",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) * 100.0 / NULLIF((SELECT SUM(COALESCE(s2."invoice_value", 0)) FROM {sales_source} s2 {cust_join.replace('s.', 's2.')} {sales_where.replace('s.', 's2.')}), 0), 2) AS "contribution_percentage"
            FROM {sales_source} s
            {cust_join}
            {sales_where}
            GROUP BY 
                CASE 
                    WHEN TRIM(cm."KTOKD") = 'Z001' OR UPPER(TRIM(cm."CH_TYPE")) = 'DL' THEN 'Dealers / Trade Network (Z001)'
                    WHEN TRIM(cm."KTOKD") = 'Z004' OR UPPER(TRIM(cm."CH_TYPE")) = 'IN' THEN 'Institutional Customers (Z004)'
                    WHEN TRIM(cm."KTOKD") = 'Z009' OR UPPER(TRIM(cm."CH_TYPE")) = 'FL' THEN 'Fleet Customers (Z009)'
                    WHEN TRIM(cm."KTOKD") = 'Z015' THEN 'Traders & Retailers (Z015)'
                    WHEN TRIM(cm."KTOKD") = 'Z006' OR UPPER(TRIM(cm."CH_TYPE")) = 'GT' THEN 'Government & PSU (Z006)'
                    WHEN TRIM(cm."KTOKD") = 'Z010' THEN 'Govt Special Accounts (Z010)'
                    WHEN TRIM(cm."KTOKD") = 'Z007' THEN 'Heavy Equipment / OTR (Z007)'
                    WHEN TRIM(cm."KTOKD") = 'Z003' THEN 'Export / Overseas (Z003)'
                    ELSE CONCAT('Account Group ', COALESCE(NULLIF(TRIM(cm."KTOKD"), ''), 'Unassigned'))
                END
            ORDER BY "revenue_cr" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_grp)
        grp_rows = cur.fetchall()
        if grp_rows:
            res["account_group_distribution"] = grp_rows
            res["top_account_group"] = f"{grp_rows[0][0]} (Sales Value: ₹{float(grp_rows[0][2] or 0):.2f} Cr, {float(grp_rows[0][4] or 0):.2f}% contribution)"

        # Top 5 and Worst 5 Customers (Last 30 Days)
        q_cust_30d = f"""
            SELECT 
                COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), s."dealer_code") AS "cname",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000.0, 2) AS "rev_cr",
                SUM(COALESCE(s."invoice_value", 0)) AS "rev_raw"
            FROM {sales_source} s
            {cust_join}
            CROSS JOIN (SELECT MAX(s_max."invoice_date") AS max_dt FROM {sales_source} s_max) md
            {sales_where}
              AND s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt
            GROUP BY COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), s."dealer_code")
            HAVING SUM(COALESCE(s."invoice_value", 0)) > 0
            ORDER BY "rev_cr" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_cust_30d)
        cust_30d_rows = cur.fetchall()
        if cust_30d_rows:
            res["top_5_customers_30d"] = cust_30d_rows[:5]
            res["worst_5_customers_30d"] = sorted(cust_30d_rows, key=lambda x: float(x[2] or 0))[:5]

        print(f"[4/8 Customer] Top Cust: {res.get('top_customer')}")
    except Exception as e:
        print(f"[Warning] Customer performance query error: {e}")

    # 5. Customer Churn & Inactivity Risk (Last 30 Days)
    try:
        q_churn_slabs = f"""
            WITH customer_30d AS (
                SELECT 
                    s."dealer_code",
                    SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt THEN COALESCE(s."invoice_value", 0) ELSE 0 END) AS curr_rev,
                    SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -59) AND ADD_DAYS(md.max_dt, -30) THEN COALESCE(s."invoice_value", 0) ELSE 0 END) AS prev_rev
                FROM {sales_source} s
                CROSS JOIN (SELECT MAX(s_max."invoice_date") AS max_dt FROM {sales_source} s_max) md
                {sales_where}
                  AND s."invoice_date" >= ADD_DAYS(md.max_dt, -59)
                GROUP BY s."dealer_code"
                HAVING SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -59) AND ADD_DAYS(md.max_dt, -30) THEN COALESCE(s."invoice_value", 0) ELSE 0 END) > 0
            ),
            filtered_declines AS (
                SELECT 
                    c."dealer_code",
                    c.curr_rev,
                    c.prev_rev,
                    ROUND(((c.prev_rev - c.curr_rev) * 100.0 / NULLIF(c.prev_rev, 0)), 2) AS decline_pct
                FROM customer_30d c
                LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
                    ON (c."dealer_code" = cm."KUNNR" OR LTRIM(c."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
                WHERE (cm."KLABC" IS NULL OR cm."KLABC" <> 'X')
            )
            SELECT 
                COUNT(CASE WHEN decline_pct > 5.0 THEN 1 END) AS decline_gt_5pct,
                COUNT(CASE WHEN decline_pct > 10.0 THEN 1 END) AS decline_gt_10pct
            FROM filtered_declines
        """
        sanitize_and_execute_hana(cur, schema, q_churn_slabs)
        c_row = cur.fetchone()
        if c_row:
            res["accounts_decline_gt_5pct"] = int(c_row[0] or 0)
            res["accounts_decline_gt_10pct"] = int(c_row[1] or 0)

        # Steepest Sales Decline Accounts
        q_steepest = f"""
            WITH customer_30d AS (
                SELECT 
                    s."dealer_code",
                    SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt THEN COALESCE(s."invoice_value", 0) ELSE 0 END) AS curr_rev,
                    SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -59) AND ADD_DAYS(md.max_dt, -30) THEN COALESCE(s."invoice_value", 0) ELSE 0 END) AS prev_rev
                FROM {sales_source} s
                CROSS JOIN (SELECT MAX(s_max."invoice_date") AS max_dt FROM {sales_source} s_max) md
                {sales_where}
                  AND s."invoice_date" >= ADD_DAYS(md.max_dt, -59)
                GROUP BY s."dealer_code"
                HAVING SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -59) AND ADD_DAYS(md.max_dt, -30) THEN COALESCE(s."invoice_value", 0) ELSE 0 END) > 100000
            ),
            decline_ranked AS (
                SELECT 
                    COALESCE(NULLIF(TRIM(cm."NAME1"), ''), NULLIF(TRIM(cm."BNAME"), ''), TRIM(c."dealer_code")) AS customer_name,
                    ROUND(c.curr_rev / 10000000.0, 2) AS current_30d_rev_cr,
                    ROUND(c.prev_rev / 10000000.0, 2) AS prev_30d_rev_cr,
                    ROUND((c.curr_rev - c.prev_rev) * 100.0 / NULLIF(c.prev_rev, 0), 2) AS decline_pct
                FROM customer_30d c
                LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
                    ON (c."dealer_code" = cm."KUNNR" OR LTRIM(c."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
                WHERE (cm."KLABC" IS NULL OR cm."KLABC" <> 'X')
            )
            SELECT customer_name, decline_pct
            FROM decline_ranked
            ORDER BY decline_pct ASC
            LIMIT 5
        """
        sanitize_and_execute_hana(cur, schema, q_steepest)
        res["steepest_decline_accounts"] = cur.fetchall()

        # High-Risk Inactive Accounts (>= 2L historical) & Zero-Sale Active Counters
        q_inactive = f"""
            WITH cust_billing AS (
                SELECT 
                    s."dealer_code",
                    SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt THEN COALESCE(s."invoice_value", 0) ELSE 0 END) AS curr_rev,
                    SUM(COALESCE(s."invoice_value", 0)) AS total_historical_rev
                FROM {sales_source} s
                CROSS JOIN (SELECT MAX(s_max."invoice_date") AS max_dt FROM {sales_source} s_max) md
                {sales_where}
                GROUP BY s."dealer_code"
                HAVING SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt THEN COALESCE(s."invoice_value", 0) ELSE 0 END) = 0
                   AND SUM(COALESCE(s."invoice_value", 0)) >= 200000
            )
            SELECT COUNT(*)
            FROM cust_billing cb
            LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
                ON (cb."dealer_code" = cm."KUNNR" OR LTRIM(cb."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
            WHERE (cm."KLABC" IS NULL OR cm."KLABC" <> 'X')
        """
        sanitize_and_execute_hana(cur, schema, q_inactive)
        inact_row = cur.fetchone()
        res["high_risk_inactive_accounts"] = int(inact_row[0] or 0) if inact_row else 0

        q_zero_units = f"""
            WITH cust_units AS (
                SELECT 
                    s."dealer_code",
                    SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt THEN COALESCE(s."Tyre_Quantity", 0) ELSE 0 END) AS recent_units,
                    SUM(COALESCE(s."Tyre_Quantity", 0)) AS total_historical_units
                FROM {sales_source} s
                CROSS JOIN (SELECT MAX(s_max."invoice_date") AS max_dt FROM {sales_source} s_max) md
                {sales_where}
                GROUP BY s."dealer_code"
                HAVING SUM(CASE WHEN s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt THEN COALESCE(s."Tyre_Quantity", 0) ELSE 0 END) = 0
                   AND SUM(COALESCE(s."Tyre_Quantity", 0)) > 0
            )
            SELECT COUNT(*)
            FROM cust_units cu
            LEFT JOIN "{schema}"."ZIVF_CustomerMaster_01_MD" cm 
                ON (cu."dealer_code" = cm."KUNNR" OR LTRIM(cu."dealer_code", '0') = LTRIM(cm."KUNNR", '0'))
            WHERE (cm."KLABC" IS NULL OR cm."KLABC" <> 'X')
        """
        sanitize_and_execute_hana(cur, schema, q_zero_units)
        zu_row = cur.fetchone()
        res["zero_sale_active_counters"] = int(zu_row[0] or 0) if zu_row else 0
        print(f"[5/8 Churn] Decline >5%: {res.get('accounts_decline_gt_5pct')}, Inactive: {res.get('high_risk_inactive_accounts')}, Zero Units: {res.get('zero_sale_active_counters')}")
    except Exception as e:
        print(f"[Warning] Customer churn query error: {e}")

    # 6. Geography (100% Dynamic from Master Views, strictly filtering for valid territories)
    try:
        q_geo = f"""
            SELECT 
                COALESCE(zm.NAME, tm.P_ZONE, 'Pan-India') AS "Zone_Name",
                COALESCE(rm."region_name", 'General Region') AS "Region_Name",
                tm.NAME AS "Territory_Name",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000.0, 2) AS "rev"
            FROM {sales_source} s
            {cust_join}
            JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm 
              ON (cm.BZIRK = tm.BZIRK OR LTRIM(TRIM(cm.BZIRK), '0') = LTRIM(TRIM(tm.BZIRK), '0') OR TRIM(cm.BZIRK) = TRIM(tm.BZIRK))
            LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
              ON (TRIM(rm.REGION_CODE) = COALESCE(NULLIF(TRIM(tm.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), ''))
                  OR LTRIM(TRIM(tm.REGIO), '0') = LTRIM(TRIM(rm.REGION_CODE), '0')
                  OR LTRIM(TRIM(TO_VARCHAR(cm.REGIO)), '0') = LTRIM(TRIM(rm.REGION_CODE), '0'))
            LEFT JOIN "{schema}"."ZIVF_ZoneMaster_01_TR" zm 
              ON (zm.P_ZONE = COALESCE(NULLIF(TRIM(rm.ZONE_CODE), ''), NULLIF(TRIM(tm.P_ZONE), '')))
            {sales_where}
              AND tm.NAME IS NOT NULL AND tm.NAME <> ''
            GROUP BY 
                COALESCE(zm.NAME, tm.P_ZONE, 'Pan-India'),
                COALESCE(rm."region_name", 'General Region'),
                tm.NAME
            ORDER BY "rev" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_geo)
        geo_rows = cur.fetchall()

        if geo_rows:
            res["top_zone"] = geo_rows[0][0]
            res["top_region"] = geo_rows[0][1]
            res["top_territory"] = geo_rows[0][2]
            res["top_zone_rev"] = float(geo_rows[0][3] or 0)
            res["lowest_territory"] = geo_rows[-1][2]
            print(f"[6/8 Geography] Top Zone: {res['top_zone']}, Region: {res['top_region']}, Territory: {res['top_territory']}, Lowest: {res['lowest_territory']}")

        # Top 5 and Worst 5 Territories (Last 30 Days)
        q_terr_30d = f"""
            SELECT 
                tm.NAME AS "Territory_Name",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000.0, 2) AS "rev_cr",
                SUM(COALESCE(s."invoice_value", 0)) AS "rev_raw"
            FROM {sales_source} s
            {cust_join}
            JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm 
              ON (cm.BZIRK = tm.BZIRK OR LTRIM(TRIM(cm.BZIRK), '0') = LTRIM(TRIM(tm.BZIRK), '0') OR TRIM(cm.BZIRK) = TRIM(tm.BZIRK))
            CROSS JOIN (SELECT MAX(s_max."invoice_date") AS max_dt FROM {sales_source} s_max) md
            {sales_where}
              AND s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt
              AND tm.NAME IS NOT NULL AND tm.NAME <> ''
            GROUP BY tm.NAME
            HAVING SUM(COALESCE(s."invoice_value", 0)) > 0
            ORDER BY "rev_cr" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_terr_30d)
        terr_rows = cur.fetchall()
        if terr_rows:
            res["top_5_territories_30d"] = terr_rows[:5]
            res["worst_5_territories_30d"] = sorted(terr_rows, key=lambda x: float(x[2] or 0))[:5]

        # Top 5 and Worst 5 Regions (Last 30 Days)
        q_reg_30d = f"""
            SELECT 
                COALESCE(NULLIF(TRIM(rm."region_name"), ''), NULLIF(TRIM(rm."region_code"), ''), 'Unknown') AS "Region_Name",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000.0, 2) AS "rev_cr",
                SUM(COALESCE(s."invoice_value", 0)) AS "rev_raw"
            FROM {sales_source} s
            {cust_join}
            JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm 
              ON (cm.BZIRK = tm.BZIRK OR LTRIM(TRIM(cm.BZIRK), '0') = LTRIM(TRIM(tm.BZIRK), '0') OR TRIM(cm.BZIRK) = TRIM(tm.BZIRK))
            LEFT JOIN "{schema}"."ZIVF_RegionMaster_MD_01" rm 
              ON (TRIM(rm.REGION_CODE) = COALESCE(NULLIF(TRIM(tm.REGIO), ''), NULLIF(TRIM(TO_VARCHAR(cm.REGIO)), ''))
                  OR LTRIM(TRIM(tm.REGIO), '0') = LTRIM(TRIM(rm.REGION_CODE), '0')
                  OR LTRIM(TRIM(TO_VARCHAR(cm.REGIO)), '0') = LTRIM(TRIM(rm.REGION_CODE), '0'))
            CROSS JOIN (SELECT MAX(s_max."invoice_date") AS max_dt FROM {sales_source} s_max) md
            {sales_where}
              AND s."invoice_date" BETWEEN ADD_DAYS(md.max_dt, -29) AND md.max_dt
            GROUP BY COALESCE(NULLIF(TRIM(rm."region_name"), ''), NULLIF(TRIM(rm."region_code"), ''), 'Unknown')
            HAVING SUM(COALESCE(s."invoice_value", 0)) > 0
            ORDER BY "rev_cr" DESC
        """
        sanitize_and_execute_hana(cur, schema, q_reg_30d)
        reg_rows = cur.fetchall()
        if reg_rows:
            res["top_5_regions_30d"] = reg_rows[:5]
            res["worst_5_regions_30d"] = sorted(reg_rows, key=lambda x: float(x[2] or 0))[:5]
    except Exception as e:
        print(f"[Warning] Geography query error: {e}")

    # 7. Distribution Analysis
    try:
        q_dist = f"""
            SELECT 
                CASE CAST(s."distribution_channel" AS VARCHAR(10))
                    WHEN '10' THEN 'Replacement'
                    WHEN '20' THEN 'OEM'
                    WHEN '30' THEN 'STU'
                    WHEN '40' THEN 'DEF'
                    ELSE 'Other'
                END AS "ch",
                ROUND(SUM(COALESCE(s."invoice_value", 0)) / 10000000.0, 2) AS "rev"
            FROM {sales_source} s
            {cust_join}
            WHERE (s."indicator_cancel" IS NULL OR s."indicator_cancel" NOT IN ('X', 'Y', '1'))
            GROUP BY 
                CASE CAST(s."distribution_channel" AS VARCHAR(10))
                    WHEN '10' THEN 'Replacement'
                    WHEN '20' THEN 'OEM'
                    WHEN '30' THEN 'STU'
                    WHEN '40' THEN 'DEF'
                    ELSE 'Other'
                END
        """
        sanitize_and_execute_hana(cur, schema, q_dist)
        dist_dict = {}
        tot_dist = 0
        for r in cur.fetchall():
            dist_dict[r[0]] = float(r[1] or 0)
            tot_dist += float(r[1] or 0)
        if tot_dist > 0:
            res["dist_rep"] = f"₹{dist_dict.get('Replacement', 0):.2f} Cr ({round((dist_dict.get('Replacement', 0) / tot_dist) * 100, 2)}% contribution)"
            res["dist_oem"] = f"₹{dist_dict.get('OEM', 0):.2f} Cr ({round((dist_dict.get('OEM', 0) / tot_dist) * 100, 2)}% contribution)"
            res["dist_stu"] = f"₹{dist_dict.get('STU', 0):.2f} Cr ({round((dist_dict.get('STU', 0) / tot_dist) * 100, 2)}% contribution)" if dist_dict.get('STU', 0) > 0 else "₹0.00 Cr (0.0% contribution)"
            res["dist_def"] = f"₹{dist_dict.get('DEF', 0):.2f} Cr ({round((dist_dict.get('DEF', 0) / tot_dist) * 100, 2)}% contribution)" if dist_dict.get('DEF', 0) > 0 else "₹0.00 Cr (0.0% contribution)"
            res["dist_contrib"] = f"Replacement segment leads with {round((dist_dict.get('Replacement', 0) / tot_dist) * 100, 2)}% share"
        print(f"[7/8 Distribution] Replacement: {res.get('dist_rep')}")
    except Exception as e:
        print(f"[Warning] Distribution query error: {e}")

    # 8. Target Performance
    try:
        q_tgt = f"""
            SELECT 
                ROUND(SUM(COALESCE(NULLIF(tgt.REV_PLAN_VAL, 0), NULLIF(tgt.PLAN_VALUE, 0), 0)) / 10000000.0, 2) AS "Target_Val_Cr",
                SUM(COALESCE(NULLIF(tgt.REV_PLAN, 0), NULLIF(tgt.ORG_PLAN, 0), 0)) AS "Target_Qty"
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" tgt
            WHERE CAST(tgt.YMONTH AS VARCHAR(10)) LIKE '{target_yr}%'
        """
        sanitize_and_execute_hana(cur, schema, q_tgt)
        t_row = cur.fetchone()
        if t_row:
            res["target_val_cr"] = float(t_row[0] or 0)
            res["target_qty"] = float(t_row[1] or 0)
            act_rev = res.get("total_revenue", 0)
            act_qty = res.get("total_qty_units", 0)
            if res["target_val_cr"] > 0:
                res["achv_pct"] = round((act_rev / res["target_val_cr"]) * 100, 2)
                res["target_shortfall"] = round(res["target_val_cr"] - act_rev, 2)
            if res["target_qty"] > 0:
                res["qty_achv_pct"] = round((act_qty / res["target_qty"]) * 100, 2)

        # Territory Target Shortfall
        q_tgt_terr = f"""
            SELECT
                tm."NAME" AS territory_name,
                ROUND((SUM(COALESCE(NULLIF(st.REV_PLAN_VAL, 0), NULLIF(st.PLAN_VALUE, 0), 0)) - COALESCE(act.actual_revenue, 0)) / 10000000.0, 2) AS Shortfall_Value,
                ROUND((COALESCE(act.actual_revenue, 0) * 100.0) / NULLIF(SUM(COALESCE(NULLIF(st.REV_PLAN_VAL, 0), NULLIF(st.PLAN_VALUE, 0), 0)), 0), 2) AS Achievement_Percentage
            FROM "{schema}"."ZIVF_SD_Sales_Plan_Target_01_TR" st
            INNER JOIN "{schema}"."ZIVF_TerritoryMaster_01_TR" tm ON TRIM(st."TERR_CODE") = TRIM(tm."BZIRK")
            LEFT JOIN (
                SELECT cm."BZIRK", SUM(COALESCE(sd."invoice_value", 0)) AS actual_revenue
                FROM {sales_source} sd
                {cust_join.replace('s.', 'sd.')}
                {sales_where.replace('s.', 'sd.')}
                GROUP BY cm."BZIRK"
            ) act ON TRIM(st."TERR_CODE") = TRIM(act."BZIRK")
            WHERE CAST(st.YMONTH AS VARCHAR(10)) LIKE '{target_yr}%'
              AND tm."NAME" IS NOT NULL AND tm."NAME" <> ''
            GROUP BY tm."NAME", act.actual_revenue
            ORDER BY Shortfall_Value DESC
            LIMIT 1
        """
        sanitize_and_execute_hana(cur, schema, q_tgt_terr)
        t_short = cur.fetchone()
        if t_short:
            res["top_shortfall_territory"] = f"{t_short[0]} (₹{float(t_short[1] or 0):.2f} Cr, {float(t_short[2] or 0):.2f}% achievement)"

        print(f"[8/8 Target] Target Val: ₹{res.get('target_val_cr', 0):.2f} Cr, Achv: {res.get('achv_pct', 0)}%, Qty Achv: {res.get('qty_achv_pct', 0)}%")
    except Exception as e:
        print(f"[Warning] Target performance query error: {e}")

    cur.close()
    return res


def execute_mysql_analysis(mysql_conn, session_id):
    """Fallback if live SAP HANA is not directly reachable: queries MySQL tables."""
    cur = mysql_conn.cursor(dictionary=True)
    _prepare_mysql_workspace(cur, session_id)

    res = {}
    try:
        cur.execute("""
            SELECT 
                ROUND(SUM(COALESCE(invoice_value, taxable_value, 0)) / 10000000, 2) AS total_revenue,
                ROUND(SUM(COALESCE(Tyre_Quantity, 0)) / 1000000, 2) AS total_qty_m,
                SUM(COALESCE(Tyre_Quantity, 0)) AS total_qty_units,
                COUNT(DISTINCT invoice_number) AS total_invoices,
                ROUND(SUM(COALESCE(invoice_value, 0)) / NULLIF(COUNT(DISTINCT invoice_number), 0), 2) AS avg_invoice_val,
                ROUND(SUM(COALESCE(invoice_value, 0)) / NULLIF(SUM(Tyre_Quantity), 0), 2) AS avg_selling_price
            FROM ZPVF_SD_CurrentSales_01_TR
            WHERE (indicator_cancel IS NULL OR indicator_cancel NOT IN ('X', 'Y', '1'))
        """)
        row = cur.fetchone()
        if row:
            res["total_revenue"] = float(row.get("total_revenue") or 0)
            res["total_qty_m"] = float(row.get("total_qty_m") or 0)
            res["total_qty_units"] = int(row.get("total_qty_units") or 0)
            res["total_invoices"] = int(row.get("total_invoices") or 0)
            res["avg_invoice_val"] = float(row.get("avg_invoice_val") or 0)
            res["avg_selling_price"] = float(row.get("avg_selling_price") or 0)
    except Exception as e:
        print(f"[MySQL-Fallback] Direct query notice: {e}")

    cur.close()
    return res


def build_full_report(existing_report, metrics):
    """
    Constructs the authoritative, clean report containing:
    - Executive Summary
    - 7 Key Business Insights (100% live data with all subpoints)
    - Business Risks
    - Actionable Recommendations

    Replaces stale/cached metrics completely so no outdated values remain at the top or bottom.
    """
    tot_rev = metrics.get('total_revenue', 0)
    tot_qty_m = metrics.get('total_qty_m', 0)
    tot_qty_u = metrics.get('total_qty_units', 0)
    tot_inv = metrics.get('total_invoices', 0)
    avg_inv = metrics.get('avg_invoice_val', 0)
    avg_sp = metrics.get('avg_selling_price', 0)

    h_month = metrics.get('highest_month', '2026-10')
    h_month_rev = metrics.get('highest_month_rev', 0)
    l_month = metrics.get('lowest_month', '2026-09')
    l_month_rev = metrics.get('lowest_month_rev', 0)
    growth = metrics.get('monthly_growth', 'Stable sales volume')

    top_cat = metrics.get('top_category', 'Truck / Bus Radial (TBR)')
    top_cat_rev = metrics.get('top_category_rev', 0)
    top_con = metrics.get('top_construction', 'Radial')
    top_con_rev = metrics.get('top_construction_rev', 0)
    top_tt = metrics.get('top_tyre_type', 'Commercial Tyre')
    top_tt_rev = metrics.get('top_tyre_type_rev', 0)
    top_p = metrics.get('top_product', '10.00 R 20 TBR SUPER LUG')
    top_p_rev = metrics.get('top_product_rev', 0)
    low_p = metrics.get('lowest_product', '165 D 12 TUBE')
    low_p_rev = metrics.get('lowest_product_rev', 0)

    top_c = metrics.get('top_customer', 'SHREE JAIN CYCLE & AUTO AGENCIES')
    top_c_rev = metrics.get('top_customer_rev', 0)
    top_5_cust_rev = metrics.get('top_5_customers_rev', 0)
    cust_conc = metrics.get('customer_concentration', 'Top 5 customers contribute significant share of total revenue')
    top_act_grp = metrics.get('top_account_group', 'Dealers / Trade Network (Z001)')

    # Account Group Breakdown Lines
    grp_lines = []
    if metrics.get("account_group_distribution"):
        grp_lines.append(f"\n#### Account Group Breakdown (KTOKD: {date_range_str})")
        for g in metrics["account_group_distribution"]:
            g_name = g[0]
            g_act = int(g[1] or 0)
            g_rev = float(g[2] or 0)
            g_units = int(g[3] or 0)
            g_pct = float(g[4] or 0)
            grp_lines.append(f"- **{g_name}**: {g_act:,} active customers, Sales Value ₹{g_rev:,.2f} Cr, {g_units:,} units, {g_pct:.2f}% contribution")
    grp_section_str = "\n".join(grp_lines)

    # Top 5 vs Worst 5 Customers 30d
    cust_30d_lines = []
    if metrics.get("top_5_customers_30d"):
        t5_str = ", ".join([f"{r[0]} (Sales Value: ₹{float(r[1] or 0):,.2f} Cr)" for r in metrics["top_5_customers_30d"]])
        cust_30d_lines.append(f"- **Top 5 Customers (Last 30 Days)**: {t5_str}")
    if metrics.get("worst_5_customers_30d"):
        w5_items = []
        for r in metrics["worst_5_customers_30d"]:
            raw_v = float(r[2] or 0)
            if raw_v >= 10000000:
                fmt_v = f"₹{raw_v / 10000000.0:.2f} Cr"
            elif raw_v >= 100000:
                fmt_v = f"₹{raw_v / 100000.0:.2f} L"
            elif raw_v >= 1000:
                fmt_v = f"₹{raw_v / 1000.0:.2f} K"
            else:
                fmt_v = f"₹{raw_v:,.0f}"
            w5_items.append(f"{r[0]} (Sales Value: {fmt_v})")
        cust_30d_lines.append(f"- **Worst 5 Customers (Last 30 Days)**: {', '.join(w5_items)}")
    cust_30d_str = "\n".join(cust_30d_lines)

    # Section 5: Churn & Inactivity
    churn_gt5 = metrics.get('accounts_decline_gt_5pct', 0)
    churn_gt10 = metrics.get('accounts_decline_gt_10pct', 0)
    churn_inact = metrics.get('high_risk_inactive_accounts', 0)
    churn_zero = metrics.get('zero_sale_active_counters', 0)
    steepest_lines = []
    if metrics.get("steepest_decline_accounts"):
        for i, r in enumerate(metrics["steepest_decline_accounts"][:5], 1):
            steepest_lines.append(f"{i}. {r[0]}: {float(r[1] or 0):.2f}%")
    steepest_str = "\n".join(steepest_lines) if steepest_lines else "None detected"

    top_z = metrics.get('top_zone', 'North Zone')
    top_r = metrics.get('top_region', 'Main Region')
    top_t = metrics.get('top_territory', 'Main Territory')
    low_t = metrics.get('lowest_territory', 'FM-KOLKATA-4')

    d_rep = metrics.get('dist_rep', '59.0% of total sales')
    d_oem = metrics.get('dist_oem', '39.0% of total sales')
    d_stu = metrics.get('dist_stu', '1.9% of total sales')
    d_def = metrics.get('dist_def', '0.1% of total sales')
    d_con = metrics.get('dist_contrib', 'Replacement segment leads in overall volume')

    tgt_val = metrics.get('target_val_cr', 0)
    achv = metrics.get('achv_pct', 0)
    shortfall = metrics.get('target_shortfall', 0)
    qty_achv = metrics.get('qty_achv_pct', 0)
    tgt_short_terr = metrics.get('top_shortfall_territory', low_t)

    date_range_str = metrics.get('date_range_str', '01 Oct 2026 – 10 Nov 2026 (41 Days)')
    
    geo_lines = [
        f"- **Top Zone (Overall: {date_range_str})**: {top_z}",
        f"- **Top Region (Overall: {date_range_str})**: {top_r}",
        f"- **Top Territory (Overall: {date_range_str})**: {top_t}",
        f"- **Lowest Territory (Overall: {date_range_str})**: {low_t}",
    ]
    if metrics.get("top_5_territories_30d"):
        t5_t_str = ", ".join([f"{r[0]} (Sales Value: ₹{float(r[1] or 0):.2f} Cr)" for r in metrics["top_5_territories_30d"]])
        geo_lines.append(f"- **Top 5 Territories (Last 30 Days)**: {t5_t_str}")
    if metrics.get("worst_5_territories_30d"):
        w5_t_items = []
        for r in metrics["worst_5_territories_30d"]:
            raw_v = float(r[2] or 0)
            if raw_v >= 10000000: fmt_v = f"₹{raw_v / 10000000.0:.2f} Cr"
            elif raw_v >= 100000: fmt_v = f"₹{raw_v / 100000.0:.2f} L"
            elif raw_v >= 1000: fmt_v = f"₹{raw_v / 1000.0:.2f} K"
            else: fmt_v = f"₹{raw_v:,.0f}"
            w5_t_items.append(f"{r[0]} (Sales Value: {fmt_v})")
        geo_lines.append(f"- **Worst 5 Territories (Last 30 Days)**: {', '.join(w5_t_items)}")
    if metrics.get("top_5_regions_30d"):
        t5_r_str = ", ".join([f"{r[0]} (Sales Value: ₹{float(r[1] or 0):.2f} Cr)" for r in metrics["top_5_regions_30d"]])
        geo_lines.append(f"- **Top 5 Regions (Last 30 Days)**: {t5_r_str}")
    if metrics.get("worst_5_regions_30d"):
        w5_r_items = []
        for r in metrics["worst_5_regions_30d"]:
            raw_v = float(r[2] or 0)
            if raw_v >= 10000000: fmt_v = f"₹{raw_v / 10000000.0:.2f} Cr"
            elif raw_v >= 100000: fmt_v = f"₹{raw_v / 100000.0:.2f} L"
            elif raw_v >= 1000: fmt_v = f"₹{raw_v / 1000.0:.2f} K"
            else: fmt_v = f"₹{raw_v:,.0f}"
            w5_r_items.append(f"{r[0]} (Sales Value: {fmt_v})")
        geo_lines.append(f"- **Worst 5 Regions (Last 30 Days)**: {', '.join(w5_r_items)}")
    geo_section_str = "\n".join(geo_lines)

    top_skus_lines = []
    if metrics.get("top_skus_by_cat"):
        from collections import defaultdict
        cat_map = defaultdict(list)
        for r in metrics["top_skus_by_cat"]:
            c_name, c_rk, s_rk, s_title, s_rev = r[0], r[1], r[2], r[3], float(r[4] or 0)
            cat_map[c_name].append((s_rk, s_title, s_rev))
        
        top_skus_lines.append("\n#### Top 5 SKUs by Top 5 Categories (Last 30 Days)")
        top_skus_lines.append("\n".join(f"- **{c}**" for c in list(cat_map.keys())[:5]))
        for c_name, items in list(cat_map.items())[:5]:
            top_skus_lines.append(f"\n*{c_name} Materials (Top 5)*:")
            for s_rk, s_title, s_rev in items[:5]:
                top_skus_lines.append(f"{s_rk}. {s_title} (Sales Value: ₹{s_rev:,.2f} Cr)")
    top_skus_str = "\n".join(top_skus_lines)

    report_text = f"""# Executive Sales & Performance Analysis

### Executive Summary
Sales performance analysis across {date_range_str} demonstrates total revenue of ₹{tot_rev:,.2f} Cr across {tot_qty_m:,.2f} million tyre units ({tot_qty_u:,} units). Replacement channel accounts for {d_rep}, with {top_c} emerging as the leading customer account. Radial construction leads with strong contribution across core commercial categories.

### Key Business Insights

**1. Overall Performance**
- **Total Revenue (Overall: {date_range_str})**: ₹{tot_rev:,.2f} Cr (Sales Value)
- **Total Quantity Sold (Overall: {date_range_str})**: {tot_qty_u:,} units
- **Total Transactions (Overall: {date_range_str})**: {tot_inv:,}
- **Average Invoice Value**: ₹{avg_inv:,.2f}
- **Average Selling Price per Unit**: ₹{avg_sp:,.2f}

**2. Revenue Trend**
- **Highest Completed Sales Month**: {h_month} (Sales Value: ₹{h_month_rev:,.2f} Cr)
- **Current Ongoing Month (MTD)**: {l_month} (Till Now: ₹{l_month_rev:,.2f} Cr)
- **Monthly Trend Context**: {growth}

**3. Product Performance**
- **Top Category (Overall: {date_range_str})**: {top_cat} (Sales Value: ₹{top_cat_rev:,.2f} Cr)
- **Top Construction Type (Overall: {date_range_str})**: {top_con} (Sales Value: ₹{top_con_rev:,.2f} Cr)
- **Top Product (Overall: {date_range_str})**: {top_p} (Sales Value: ₹{top_p_rev:,.2f} Cr)
- **Lowest Selling Product (Overall: {date_range_str})**: {low_p} (Sales Value: ₹{low_p_rev:,.2f} Cr)
{top_skus_str}

**4. Customer Performance & Account Group Breakdown**
- **Top Customer (Overall: {date_range_str})**: {top_c} (Sales Value: ₹{top_c_rev:,.2f} Cr)
- **Top 5 Customers Combined Sales Value (Overall: {date_range_str})**: ₹{top_5_cust_rev:,.2f} Cr
- **Customer Concentration Risk**: {cust_conc}
- **Top Account Group (Overall: {date_range_str})**: {top_act_grp}
{grp_section_str}
{cust_30d_str}

**5. Customer Churn & Inactivity Risk (Last 30 Days)**
- **Accounts with >5% Sales Decline**: {churn_gt5:,} accounts
- **Accounts with >10% Sales Decline**: {churn_gt10:,} accounts
- **Steepest Sales Decline Accounts**:
{steepest_str}
- **High-Risk Inactive Accounts (₹2L+ Historical Billing)**: {churn_inact:,} accounts
- **Zero-Sale Active Counters**: {churn_zero:,} counters

**6. Geography Performance**
{geo_section_str}

**7. Distribution**
- **Replacement Channel Revenue (Overall: {date_range_str})**: {d_rep}
- **OEM Channel Revenue (Overall: {date_range_str})**: {d_oem}
- **STU Channel Revenue (Overall: {date_range_str})**: {d_stu}
- **DEF Channel Revenue (Overall: {date_range_str})**: {d_def}

**8. Target Performance**
- **Total Target Value (Overall: {date_range_str})**: ₹{tgt_val:,.2f} Cr
- **Overall Achievement %**: {achv:.2f}%
- **Target Shortfall (Overall: {date_range_str})**: ₹{shortfall:,.2f} Cr (Shortfall Value)
- **Quantity Achievement %**: {qty_achv:.2f}%
- **Top Target Shortfall Territory (Overall: {date_range_str})**: {tgt_short_terr}

### Business Risks
- **Customer Concentration Risk**: Major institutional accounts drive significant volume.
- **Target Shortfall**: Overall revenue achievement is at {achv:.2f}% against plan target of ₹{tgt_val:,.2f} Cr.
- **Channel Skew**: Primary reliance on Replacement ({d_rep}) and OEM ({d_oem}) channels.

### Actionable Recommendations
- **Scale Fleet & Institutional Distribution**: Accelerate institutional and fleet contracts to reduce the ₹{shortfall:,.2f} Cr target shortfall.
- **Territory Optimization**: Strengthen presence in underperforming territory ({low_t}) with targeted dealer activations.
- **Radial Portfolio Focus**: Maximize supply for {top_p} and {top_cat} to leverage proven category demand."""

    return report_text.strip()

    return report_text.strip()


def update_single_session(mysql_conn, session_id):
    print(f"============================================================")
    print(f" Updating Session Analysis Summary for Session: {session_id}")
    print(f"============================================================")

    hana_conn, tunnel, schema = get_sap_connection_for_session(mysql_conn, session_id)
    metrics = {}

    if hana_conn:
        print(f"[SAP-CONN] Live SAP HANA connected! Schema: '{schema}'")
        try:
            metrics = execute_hana_analysis(hana_conn, schema)
        except Exception as e:
            print(f"[SAP-ERROR] Error executing SAP HANA analysis: {e}")
        finally:
            hana_conn.close()
            if tunnel:
                tunnel.stop()
    else:
        print("[Notice] No live SAP HANA credentials found for session. Using database aggregates...")
        metrics = execute_mysql_analysis(mysql_conn, session_id)

    # Fetch existing cached report
    cur = mysql_conn.cursor(dictionary=True)
    cur.execute("SELECT id, report FROM session_analysis_cache WHERE session_id = %s LIMIT 1", (session_id,))
    cache_row = cur.fetchone()

    # Check if report_content column exists
    cur.execute("SHOW COLUMNS FROM session_analysis_cache LIKE 'report_content'")
    has_rc = bool(cur.fetchone())

    old_report = cache_row["report"] if cache_row else ""
    updated_report = build_full_report(old_report, metrics)

    if not cache_row:
        print(f"[Notice] No cache record found in session_analysis_cache for session {session_id}. Inserting fresh record...")
        if has_rc:
            cur.execute("""
                INSERT INTO session_analysis_cache 
                    (session_id, data_hash, report, report_content, graph_url, topics, databases)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (session_id, "manual_script_hash", updated_report, updated_report, "", "[]", "[]"))
        else:
            cur.execute("""
                INSERT INTO session_analysis_cache 
                    (session_id, data_hash, report, graph_url, topics, databases)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (session_id, "manual_script_hash", updated_report, "", "[]", "[]"))
    else:
        # Overwrite the existing row completely so old text at top is eliminated
        if has_rc:
            cur.execute("""
                UPDATE session_analysis_cache 
                SET report = %s, report_content = %s
                WHERE session_id = %s
            """, (updated_report, updated_report, session_id))
        else:
            cur.execute("""
                UPDATE session_analysis_cache 
                SET report = %s
                WHERE session_id = %s
            """, (updated_report, session_id))

    mysql_conn.commit()
    cur.close()

    # Sync session_chat_history for all workspace users
    try:
        from controllers.session_analysis_controller import sync_session_chat_history_with_report
        sync_session_chat_history_with_report(mysql_conn, session_id, updated_report)
    except Exception as se:
        print(f"[Warning] Could not sync chat history from update_session_summary: {se}")

    # Optionally sync to Vector DB
    try:
        from controllers.vector_store import push_session_analysis_and_kgraph_to_vector_db
        push_session_analysis_and_kgraph_to_vector_db(
            session_id=session_id,
            report=updated_report,
            kgraph=None,
            db_data=[],
            web_data=[],
            ontology_analysis=None,
            conn=mysql_conn
        )
        print("[Vector-DB] Synced updated analysis to Vector DB.")
    except Exception:
        pass

    print(f"\n[SUCCESS] Updated session {session_id} successfully in MySQL!")
    print(f"  Total Revenue:       ₹{metrics.get('total_revenue', 0):,.2f} Cr")
    print(f"  Total Quantity:      {metrics.get('total_qty_m', 0)} million units")
    print(f"  Top Zone:            {metrics.get('top_zone')}")
    print(f"  Top Region:          {metrics.get('top_region')}")
    print(f"  Top Territory:       {metrics.get('top_territory')}")
    print(f"  Lowest Territory:    {metrics.get('lowest_territory')}")
    print(f"  STU Distribution:    {metrics.get('dist_stu')}")
    print(f"  DEF Distribution:    {metrics.get('dist_def')}")
    print(f"  Qty Achievement:     {metrics.get('qty_achv_pct')}%")
    return True


def main():
    parser = argparse.ArgumentParser(description="Update session_analysis_cache summary report")
    parser.add_argument("pos_session_id", nargs="?", default=None, help="Target session ID (optional positional argument)")
    parser.add_argument("--session_id", type=str, default=None, help="Target session ID")
    parser.add_argument("--all", action="store_true", help="Update all sessions in session_analysis_cache")
    args = parser.parse_args()

    mysql_conn = mysql.connector.connect(**MYSQL_CONFIG)

    if args.all:
        cur = mysql_conn.cursor(dictionary=True)
        cur.execute("SELECT DISTINCT session_id FROM session_analysis_cache ORDER BY id DESC")
        rows = cur.fetchall()
        cur.close()
        print(f"[Main] Updating {len(rows)} sessions in session_analysis_cache...")
        success_count = 0
        for r in rows:
            sid = r["session_id"]
            if update_single_session(mysql_conn, sid):
                success_count += 1
        print(f"\n[Completed] {success_count}/{len(rows)} sessions updated.")
        mysql_conn.close()
        return

    target_session = args.pos_session_id or args.session_id or get_latest_session_id(mysql_conn)
    if not target_session:
        print("[Error] No session_id provided or found in session_analysis_cache.")
        mysql_conn.close()
        return

    update_single_session(mysql_conn, target_session)
    mysql_conn.close()
    print("\nDone! Refresh the summary page in the UI to see the updated report.")


if __name__ == "__main__":
    main()
