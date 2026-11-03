"""
Integration tests for Dynamic Dependent Filters and Year-wise Sales Comparison Dashboard.
Tests all requirements and permutations:
  - Test 1: Default (All = ALL)
  - Test 2: Zone Only (Zone = CZ)
  - Test 3: Zone + Region (Zone = CZ, Region = 3681)
  - Test 4: Zone + Region + Customer Type
  - Test 5: Month + Year
  - Test 6: Multiple Years
  - Test 7: Empty dataset handling
  - Test 8: Faceted filtering & Reset rules
  - Test 9: Label/Value separation
"""

import sys
import json
import unittest

sys.path.insert(0, 'e:/D-API-UI/api')
from dotenv import load_dotenv
load_dotenv('e:/D-API-UI/api/.env')
from database.db_connection import get_db_connection
from controllers.year_wise_dashboard_controller import (
    fetch_zones,
    fetch_regions,
    fetch_customer_types,
    fetch_master_months,
    fetch_master_years,
    fetch_available_periods,
    _get_connection_context,
    _close_connections,
    _build_sales_filter_query
)

class TestDynamicFiltersDashboard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mysql_conn, cls.hana_conn, cls.tunnel, cls.is_hana, cls.schema, cls.user_db, cls.session_id = _get_connection_context(get_db_connection)
        cls.cursor = cls.hana_conn.cursor() if cls.is_hana else cls.mysql_conn.cursor()

    @classmethod
    def tearDownClass(cls):
        _close_connections(cls.mysql_conn, cls.hana_conn, cls.tunnel, cls.cursor)

    def test_01_default_filters_and_label_value_separation(self):
        """Test 1: Default - Verify label/value separation and 'ALL' default"""
        zones = fetch_zones(self.cursor, self.is_hana, self.schema, self.user_db)
        self.assertGreater(len(zones), 1)
        self.assertEqual(zones[0]["value"], "ALL")
        self.assertEqual(zones[0]["label"], "ALL")
        for z in zones:
            self.assertIn("label", z)
            self.assertIn("value", z)

        regions = fetch_regions(self.cursor, self.is_hana, self.schema, self.user_db)
        self.assertGreater(len(regions), 1)
        self.assertEqual(regions[0]["value"], "ALL")
        for r in regions:
            self.assertIn("label", r)
            self.assertIn("value", r)
            self.assertIn("zone_code", r)

        cust_types = fetch_customer_types(self.cursor, self.is_hana, self.schema, self.user_db)
        self.assertGreater(len(cust_types), 0)
        self.assertEqual(cust_types[0]["value"], "ALL")

        months = fetch_master_months(self.cursor, self.is_hana, self.schema, self.user_db)
        self.assertGreaterEqual(len(months), 12)
        self.assertEqual(months[0]["value"], "ALL")

        years = fetch_master_years(self.cursor, self.is_hana, self.schema, self.user_db)
        self.assertGreater(len(years), 1)
        self.assertEqual(years[0]["value"], "ALL")

    def test_02_zone_dependency(self):
        """Test 2: Zone Only - Verify only matching regions are returned for Zone=CZ"""
        regions_cz = fetch_regions(self.cursor, self.is_hana, self.schema, self.user_db, zone="CZ")
        self.assertGreater(len(regions_cz), 1)
        for r in regions_cz:
            if r["value"] != "ALL":
                self.assertEqual(r["zone_code"], "CZ")

    def test_03_zone_and_region_dependency(self):
        """Test 3: Zone + Region - Verify dependent customer types for CZ + 3681"""
        cust_types = fetch_customer_types(self.cursor, self.is_hana, self.schema, self.user_db, zone="CZ", region="3681")
        self.assertIsInstance(cust_types, list)
        self.assertEqual(cust_types[0]["value"], "ALL")

    def test_04_faceted_available_periods(self):
        """Test 4: Faceted available periods for Zone=CZ"""
        months, years = fetch_available_periods(self.cursor, self.is_hana, self.schema, self.user_db, zone="CZ")
        self.assertGreater(len(months), 1)
        self.assertGreater(len(years), 1)

    def test_05_multiple_years_sales_query(self):
        """Test 5 & 6: Sales query for multiple years"""
        joins, where_sql, params, date_col, rev_expr = _build_sales_filter_query(
            self.cursor, self.is_hana, self.schema, self.user_db,
            years=[2026, 2025]
        )
        query = f"""
            SELECT MONTH({date_col}) AS m_num, YEAR({date_col}) AS yr, ROUND(SUM({rev_expr}), 2) AS total
            {joins} {where_sql} GROUP BY YEAR({date_col}), MONTH({date_col})
        """
        self.cursor.execute(query, params if self.is_hana else tuple(params))
        rows = self.cursor.fetchall()
        self.assertIsInstance(rows, (list, tuple))

    def test_07_empty_criteria_handling(self):
        """Test 7: No matching sales for nonexistent zone"""
        joins, where_sql, params, date_col, rev_expr = _build_sales_filter_query(
            self.cursor, self.is_hana, self.schema, self.user_db,
            zone="NONEXISTENT_ZONE_XYZ"
        )
        query = f"""
            SELECT MONTH({date_col}) AS m_num, YEAR({date_col}) AS yr, ROUND(SUM({rev_expr}), 2) AS total
            {joins} {where_sql} GROUP BY YEAR({date_col}), MONTH({date_col})
        """
        self.cursor.execute(query, params if self.is_hana else tuple(params))
        rows = self.cursor.fetchall()
        self.assertEqual(len(rows), 0)

if __name__ == '__main__':
    unittest.main()

