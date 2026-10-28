"""
kgraph_ontology_comparator.py

Compares the freshly built / current Knowledge Graph (from imported data)
against the Golden Standard Sales & Distribution Ontology Graph (from index.html).

Features & Fixes:
1. Token-based exact keyword matching (prevents false matches like 'sales_report' matching 'rep',
   'plant_master' matching 'plan', 'statement' matching 'state', etc.).
2. Multi-word keywords handled cleanly without substring collisions.
3. Attribute-level matching: normalizes camelCase and snake_case column names.
4. Edge verification: verifies if matched tables have real join edges in the graph.
5. Contextual completeness note clarifying benchmark is against the 12-node core RtM ontology.
6. Graceful AI gap analysis with fallback.
"""

import os
import re
import json
import traceback
from typing import Dict, Any, List, Set

from model.llm_client import call_llm_chat

# Standard Golden Ontology definition extracted from index.html
# Disambiguated keywords (e.g. dealer assigned to distributor, retailer/store to outlet; common words like 'sales', 'order', 'report' separated)
GOLDEN_VERTICES = [
    {
        "id": "rep",
        "name": "FieldForceRep",
        "type": "Field Force",
        "group": "rep",
        "keywords": ["rep", "salesman", "salesperson", "merchandiser", "field_force", "fieldforce", "salesrep"],
        "def": "The field seller / merchandiser executing sales orders, planograms, and outlet audits.",
        "attributes": ["rep_id", "ff_type", "depot", "call_rate_target", "avg_drop_size", "route_adherence"]
    },
    {
        "id": "geo",
        "name": "GeographyNode / Territory",
        "type": "Territory / Beat",
        "group": "route",
        "keywords": ["territory", "geography", "beat", "zone", "region", "pincode", "postal_code", "sales_area", "district"],
        "def": "Commercial geography: Outlet -> Route/Beat -> Territory -> Area -> Region.",
        "attributes": ["node_id", "hierarchy_level", "sales_area", "urban_rural_flag", "assigned_depot"]
    },
    {
        "id": "coverage",
        "name": "CoveragePlan (TCP / TCR)",
        "type": "Coverage Plan",
        "group": "route",
        "keywords": ["coverage_plan", "beat_plan", "journey_plan", "pjp", "visit_plan", "tcr", "tcp", "roster", "target", "sales_plan", "plan_target", "target_plan"],
        "def": "Decides who visits which outlet how often. Consolidated workload and demand review.",
        "attributes": ["plan_id", "outlet_universe", "call_frequency", "workload_minutes_per_visit"]
    },
    {
        "id": "distributor",
        "name": "Distributor / Wholesaler",
        "type": "Distributor",
        "group": "distributor",
        "keywords": ["distributor", "wholesaler", "dealer", "stockist", "primary_distributor", "depot_master"],
        "def": "The indirect-RtM intermediary carrying primary, secondary, and tertiary sales flows.",
        "attributes": ["distributor_id", "distributor_type", "margin_structure", "served_outlets_count", "credit_limit"]
    },
    {
        "id": "outlet",
        "name": "CustomerOutlet",
        "type": "Customer Outlet",
        "group": "outlet",
        "keywords": ["outlet", "retailer", "customer", "store", "shop", "pos", "retail_account", "counterparty"],
        "def": "The demand-side counterparty: Outlet -> Delivery Point -> Legal Customer -> Retail Account.",
        "attributes": ["customer_code", "channel_type", "volume_class", "ownership_type", "credit_limit", "churn_risk"]
    },
    {
        "id": "product",
        "name": "Product / SKU",
        "type": "Product / SKU",
        "group": "product",
        "keywords": ["product", "sku", "item", "material", "brand", "variant", "pack_size", "item_master", "category"],
        "def": "The sellable item and roll-ups: Category -> Brand Family -> Variant -> Product -> Trade SKU.",
        "attributes": ["sku_id", "gtin", "brand_family", "pack_type", "is_sellable", "abc_classification"]
    },
    {
        "id": "assortment",
        "name": "AssortmentPlan (SAO / SAT)",
        "type": "Assortment Plan",
        "group": "product",
        "keywords": ["assortment", "must_stock", "must_stock_list", "target_distribution", "core_range", "sat", "sao", "msl"],
        "def": "Core range / must-stock lists by outlet cluster, target distribution by SKU; SAT compliance target.",
        "attributes": ["plan_id", "outlet_cluster", "core_range", "must_stock_list", "target_distribution"]
    },
    {
        "id": "call",
        "name": "CallVisit",
        "type": "Visit Execution",
        "group": "execution",
        "keywords": ["call_visit", "beat_visit", "visit_log", "field_visit", "checkin", "call_log", "daily_call"],
        "def": "The atomic execution event: plans, surveys, stock count, and order capture with GPS verification.",
        "attributes": ["call_id", "planned_flag", "call_type", "duration_minutes", "gps_verified", "outcome"]
    },
    {
        "id": "audit",
        "name": "RetailAudit",
        "type": "Retail Audit",
        "group": "execution",
        "keywords": ["retail_audit", "planogram", "oos_audit", "stock_audit", "shelf_share", "facing_audit", "audit_log"],
        "def": "Per product - outlet - date: isDistributed, isAvailable (OOS), facings, planogram compliance, observed RSP.",
        "attributes": ["audit_id", "is_distributed", "is_available", "num_facings", "oos_status", "planogram_compliance"]
    },
    {
        "id": "soq",
        "name": "SuggestedOrder (SOQ)",
        "type": "Order Generation",
        "group": "order",
        "keywords": ["soq", "suggested_order", "recommended_order", "order_recommendation", "forecast_order"],
        "def": "Baseline forecast + order enhancement -> suggested quantity; tracked via acceptance & execution.",
        "attributes": ["soq_id", "baseline_forecast_cases", "suggested_qty_cases", "acceptance_flag", "forecast_accuracy"]
    },
    {
        "id": "orders",
        "name": "SupplyOrders",
        "type": "Fulfillment Order",
        "group": "order",
        "keywords": ["sales_order", "invoice", "order_header", "order_line", "shipment", "delivery_note", "billing_doc", "orders", "sales_history", "current_sales", "cancelled_sales", "sd_sales", "sales"],
        "def": "Transactional movement objects: sales orders, shipments with carrier OTIF, and invoices.",
        "attributes": ["sales_order_no", "movement_type", "delivery_status", "otif_target_met", "invoice_matched"]
    },
    {
        "id": "tci",
        "name": "TradeInvestment (TCI / CIS)",
        "type": "Trade Investment",
        "group": "investment",
        "keywords": ["trade_investment", "scheme", "rebate", "trade_promotion", "cis", "tci", "incentive_scheme", "discount_slab"],
        "def": "Customer front margin + back-margin buckets; outlet-level incentive schemes with PfP/PfC performance.",
        "attributes": ["scheme_id", "front_margin_pct", "back_margin_buckets", "croi_return", "payout_status"]
    }
]

GOLDEN_EDGES = [
    {"from": "coverage", "to": "call", "label": "schedules"},
    {"from": "rep", "to": "call", "label": "executes"},
    {"from": "rep", "to": "geo", "label": "covers"},
    {"from": "assortment", "to": "call", "label": "deploys_via"},
    {"from": "assortment", "to": "outlet", "label": "sets_sat_target"},
    {"from": "assortment", "to": "product", "label": "defines_range"},
    {"from": "call", "to": "audit", "label": "captures"},
    {"from": "call", "to": "soq", "label": "generates"},
    {"from": "call", "to": "outlet", "label": "visits"},
    {"from": "distributor", "to": "outlet", "label": "supplies"},
    {"from": "distributor", "to": "geo", "label": "operates_in"},
    {"from": "soq", "to": "orders", "label": "converts_to"},
    {"from": "tci", "to": "coverage", "label": "incentive_workload"},
    {"from": "tci", "to": "outlet", "label": "enrols"},
    {"from": "audit", "to": "tci", "label": "measures_compliance"},
    {"from": "audit", "to": "product", "label": "audits_sku"},
    {"from": "product", "to": "orders", "label": "fulfilled_in"}
]


def _tokenize(name: str) -> Set[str]:
    """
    Splits camelCase, snake_case, and non-alphanumeric words into clean normalized lowercase tokens.
    e.g. 'salesReport_log' -> {'sales', 'report', 'log'}
    """
    if not name:
        return set()
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    tokens = set(re.split(r"[^a-z0-9]+", s.lower())) - {""}
    return tokens


def _matches_table(table_name: str, keywords: List[str]) -> bool:
    """
    Exact token matching for single words, and normalized phrase matching for multi-word keywords.
    Prevents false matches like:
      - 'rep' matching 'report_log' or 'representative_notes'
      - 'sat' matching 'saturday_offs'
      - 'plan' matching 'plant_master'
      - 'state' matching 'statement_log'
    """
    clean_name = str(table_name).strip().lower()
    table_tokens = _tokenize(clean_name)

    for kw in keywords:
        kw_clean = kw.strip().lower()
        if "_" in kw_clean or " " in kw_clean:
            # Multi-word keyword: ensure whole token sequence appears
            kw_tokens = [t for t in re.split(r"[_\s]+", kw_clean) if t]
            if len(kw_tokens) > 1:
                # Check if kw_tokens is a sublist of table_tokens or exact sequence in table_name
                kw_str = "_".join(kw_tokens)
                name_norm = "_".join(re.split(r"[^a-z0-9]+", clean_name))
                if kw_str in name_norm:
                    return True
        else:
            # Single word keyword: exact token match
            if kw_clean in table_tokens:
                return True
    return False


def _check_attributes_coverage(table_cols: List[str], expected_attributes: List[str]) -> Dict[str, Any]:
    """
    Matches expected ontology attributes against actual columns in the matched tables.
    Handles snake_case / camelCase token overlaps.
    """
    matched_attrs = []
    missing_attrs = []

    normalized_cols_tokens = [_tokenize(c) for c in table_cols]

    for attr in expected_attributes:
        attr_tokens = _tokenize(attr)
        found = False
        for col_t in normalized_cols_tokens:
            if attr_tokens and (attr_tokens.issubset(col_t) or col_t.issubset(attr_tokens)):
                found = True
                break
        if found:
            matched_attrs.append(attr)
        else:
            missing_attrs.append(attr)

    return {
        "matched_attributes": matched_attrs,
        "missing_attributes": missing_attrs,
        "attribute_coverage_pct": round((len(matched_attrs) / len(expected_attributes)) * 100, 1) if expected_attributes else 100.0
    }


def compare_kgraph_with_ontology(imported_graph: Dict[str, Any], db_schema: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Performs deterministic and AI-powered comparison between the imported
    Knowledge Graph and the standard Sales & Distribution Ontology Graph.
    """
    current_nodes = imported_graph.get("nodes", [])
    current_edges = imported_graph.get("edges", [])

    # Extract tables and their available columns
    table_to_columns: Dict[str, List[str]] = {}

    for n in current_nodes:
        name = str(n.get("table_name") or n.get("id") or n.get("name") or "").strip()
        if not name or name == "unknown":
            continue
        cols = n.get("columns", []) or []
        col_names = [str(c.get("name") if isinstance(c, dict) else c) for c in cols]
        table_to_columns.setdefault(name.lower(), []).extend(col_names)

    if db_schema:
        for t, meta in db_schema.items():
            t_low = str(t).strip().lower()
            cols = meta.get("columns", []) if isinstance(meta, dict) else []
            col_names = [str(c.get("name") if isinstance(c, dict) else c) for c in cols]
            table_to_columns.setdefault(t_low, []).extend(col_names)

    imported_table_names = list(table_to_columns.keys())

    # Map imported tables to Golden Vertices using robust token matching
    matched_vertices = []
    missing_vertices = []
    ontology_to_tables: Dict[str, List[str]] = {}

    for v in GOLDEN_VERTICES:
        v_id = v["id"]
        keywords = v["keywords"]
        matched_tables = []

        for name in imported_table_names:
            if _matches_table(name, keywords):
                matched_tables.append(name)

        matched_tables = list(set(matched_tables))

        if matched_tables:
            ontology_to_tables[v_id] = matched_tables
            # Collect all columns for matched tables
            combined_cols = []
            for t in matched_tables:
                combined_cols.extend(table_to_columns.get(t, []))

            attr_eval = _check_attributes_coverage(combined_cols, v["attributes"])

            matched_vertices.append({
                "ontology_id": v_id,
                "name": v["name"],
                "type": v["type"],
                "group": v["group"],
                "matched_imported_tables": matched_tables,
                "matched_attributes": attr_eval["matched_attributes"],
                "missing_attributes": attr_eval["missing_attributes"],
                "attribute_coverage_pct": attr_eval["attribute_coverage_pct"]
            })
        else:
            missing_vertices.append({
                "ontology_id": v_id,
                "name": v["name"],
                "type": v["type"],
                "group": v["group"],
                "definition": v["def"],
                "expected_attributes": v["attributes"]
            })

    total_ontology_nodes = len(GOLDEN_VERTICES)
    matched_count = len(matched_vertices)
    completeness_score = round((matched_count / total_ontology_nodes) * 100, 1)

    # Edge verification using verified edges from the graph
    # Check if a join exists between tables corresponding to edge['from'] and edge['to']
    verified_edge_pairs: Set[tuple] = set()
    for e in current_edges:
        f_tbl = str(e.get("from_table") or e.get("from") or "").lower()
        t_tbl = str(e.get("to_table") or e.get("to") or "").lower()
        if f_tbl and t_tbl:
            verified_edge_pairs.add((f_tbl, t_tbl))
            verified_edge_pairs.add((t_tbl, f_tbl))  # Undirected join accessibility

    verified_relationships = []
    missing_relationships = []

    for edge in GOLDEN_EDGES:
        from_id = edge["from"]
        to_id = edge["to"]
        from_tables = ontology_to_tables.get(from_id, [])
        to_tables = ontology_to_tables.get(to_id, [])

        if not from_tables or not to_tables:
            missing_relationships.append({
                "from": from_id,
                "to": to_id,
                "label": edge["label"],
                "reason": f"Required nodes not mapped ({from_id} or {to_id} missing)"
            })
            continue

        # Check if any join edge links from_tables with to_tables
        has_verified_join = any((f, t) in verified_edge_pairs for f in from_tables for t in to_tables)
        if has_verified_join:
            verified_relationships.append({
                "from": from_id,
                "to": to_id,
                "label": edge["label"],
                "verified": True
            })
        else:
            missing_relationships.append({
                "from": from_id,
                "to": to_id,
                "label": edge["label"],
                "reason": f"Nodes mapped ({from_tables} and {to_tables}), but no verified join relationship found between them."
            })

    comparison_summary = {
        "completeness_score": completeness_score,
        "benchmark_scope": "12-Node Core Sales & Distribution (RtM) Ontology",
        "total_ontology_nodes": total_ontology_nodes,
        "matched_nodes_count": matched_count,
        "missing_nodes_count": len(missing_vertices),
        "matched_vertices": matched_vertices,
        "missing_vertices": missing_vertices,
        "verified_relationships_count": len(verified_relationships),
        "missing_relationships_count": len(missing_relationships),
        "verified_relationships": verified_relationships,
        "missing_relationships": missing_relationships[:10]
    }

    # Generate AI gap analysis via call_llm_chat
    ai_analysis = _generate_ai_gap_analysis(comparison_summary, set(imported_table_names))
    comparison_summary["ai_analysis"] = ai_analysis

    return comparison_summary


def _generate_ai_gap_analysis(summary: Dict[str, Any], imported_tables: Set[str]) -> Dict[str, Any]:
    """
    Calls the LLM to generate domain-expert insights and recommendations for the gaps.
    """
    missing_names = [f"- {v['name']} ({v['type']}): {v['definition']}" for v in summary["missing_vertices"]]
    matched_names = [f"- {v['name']} (Mapped tables: {', '.join(v['matched_imported_tables'])})" for v in summary["matched_vertices"]]

    prompt = f"""
You are a Principal Enterprise Architect specializing in Sales & Distribution Route-to-Market (RtM) Knowledge Graphs and Supply Chain Ontologies.

We compared an imported dataset's Knowledge Graph against our Golden Standard 12-Node Sales & Distribution Ontology Graph.

### BENCHMARK EVALUATION SUMMARY:
- Knowledge Graph Completeness Score: {summary['completeness_score']}% (against 12-Node Core RtM Ontology)
- Matched Entities / Nodes ({summary['matched_nodes_count']}/{summary['total_ontology_nodes']}):
{chr(10).join(matched_names) if matched_names else "None"}

- Missing Ideal Entities / Nodes ({summary['missing_nodes_count']}/{summary['total_ontology_nodes']}):
{chr(10).join(missing_names) if missing_names else "None"}

- Verified Relationships: {summary['verified_relationships_count']} / {len(GOLDEN_EDGES)}
- Imported Source Tables: {list(imported_tables)}

### YOUR TASK:
Provide a comprehensive, high-value Knowledge Graph Gap Analysis report structured as JSON with the following exact keys:
{{
  "overall_assessment": "Short executive summary of the graph's maturity and completeness against standard RtM distribution practices.",
  "missing_nodes_analysis": [
    {{
      "node_name": "Name of the missing node",
      "criticality": "High | Medium | Low",
      "business_impact": "Explain specifically why this missing entity hurts analytics, route planning, or forecasting.",
      "suggested_data_sources": "Specific types of CSV/Excel/database tables the user should import to fulfill this node."
    }}
  ],
  "operational_blind_spots": [
    "List 3-5 operational questions or analyses the business CANNOT perform because these nodes are missing (e.g. OOS rate, Beat Adherence, Trade Margin ROI)."
  ],
  "actionable_recommendations": [
    "Step-by-step guidance on what files or integrations the user should import next to complete the graph."
  ]
}}
Respond ONLY with a valid JSON object.
"""

    try:
        raw_res = call_llm_chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.2)
        if isinstance(raw_res, str):
            clean_str = re.sub(r"^```json\s*", "", raw_res.strip())
            clean_str = re.sub(r"\s*```$", "", clean_str)
            return json.loads(clean_str)
        return raw_res
    except Exception as e:
        print(f"[OntologyComparator] AI Analysis call failed: {e}")
        traceback.print_exc()
        return {
            "overall_assessment": f"Graph matches {summary['completeness_score']}% of the 12-Node Sales & Distribution Core Ontology.",
            "missing_nodes_analysis": [
                {
                    "node_name": v["name"],
                    "criticality": "High",
                    "business_impact": v["definition"],
                    "suggested_data_sources": f"Upload master data or transaction log for {v['name']}."
                } for v in summary["missing_vertices"]
            ],
            "operational_blind_spots": [
                "Route and Beat compliance cannot be analyzed without Geography/FieldForce data.",
                "In-store stockouts (OOS) and planogram adherence require Retail Audit data."
            ],
            "actionable_recommendations": [
                "Import master distribution data (depots, distributors, routes).",
                "Integrate salesman beat or visit execution logs."
            ]
        }
