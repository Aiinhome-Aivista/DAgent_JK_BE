import hashlib
import re
import json
# pyrefly: ignore [missing-import]
import mysql.connector
try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("[Analysis] psycopg2 not installed — PostgreSQL support disabled")
from flask import request, jsonify
from database.config import MISTRAL_API_KEY, MISTRAL_MODEL, MYSQL_CONFIG
from model.llm_client import call_llm_chat
from helper.dynamic_profiler import get_table_aggregates
try:
    from utils.logger_service import log_system_error
except Exception:
    def log_system_error(*args, **kwargs): pass




# pyrefly: ignore [missing-import]
from pyvis.network import Network
import os
import uuid
# pyrefly: ignore [missing-import]
from arango import ArangoClient
from database.config import (
    GRAPH_FOLDER, BASE_URL,
    ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB
)


MISTRAL_URL   = "https://api.mistral.ai/v1/chat/completions"
MAX_ROWS      = 100
# [CHANGED] 24000 -> 80000. The deterministic VERIFIED KPI BLOCK is now
# larger and far more important to protect than the old free-form LLM
# insights it replaced — truncation here silently drops Geography/
# Distribution/Pricing/Target sections (they're appended after Overall/
# Product/Customer in the dict, so they're first to get cut).
MAX_CTX_CHARS = 80000




# ══════════════════════════════════════════════════════
# CACHE HELPERS
# ══════════════════════════════════════════════════════


def _hash_context(context: str) -> str:
    """Return SHA-256 hex digest of the raw context string."""
    return hashlib.sha256(context.encode("utf-8")).hexdigest()




def _load_cache(session_id: str, data_hash: str, conn) -> dict | None:
    """
    Return cached {report, graph_url} if session_id exists AND hash matches.
    Returns None otherwise (miss or stale).
    """
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT `report`, `graph_url`, `data_hash`
            FROM `session_analysis_cache`
            WHERE `session_id` = %s
            LIMIT 1
        """, (session_id,))
        row = cursor.fetchone()
        if row and row["data_hash"] == data_hash:
            print(f"[Cache] HIT   session={session_id}")
            return {"report": row["report"], "graph_url": row["graph_url"]}
        if row:
            print(f"[Cache] STALE session={session_id} — data changed, regenerating")
        else:
            print(f"[Cache] MISS  session={session_id} — first time")
        return None
    except Exception as e:
        print(f"[Cache] load error: {e}")
        return None
    finally:
        if cursor:
            cursor.close()




def _save_cache(session_id: str, data_hash: str,
                report: str, graph_url: str,
                topics: list, databases: list, conn) -> None:
    """Upsert cache row for this session."""
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO `session_analysis_cache`
                (`session_id`, `data_hash`, `report`, `graph_url`, `topics`, `databases`)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                `data_hash`  = VALUES(`data_hash`),
                `report`     = VALUES(`report`),
                `graph_url`  = VALUES(`graph_url`),
                `topics`     = VALUES(`topics`),
                `databases`  = VALUES(`databases`),
                `updated_at` = CURRENT_TIMESTAMP
        """, (
            session_id,
            data_hash,
            report,
            graph_url,
            json.dumps(topics),
            json.dumps(databases)
        ))
        conn.commit()
        print(f"[Cache] SAVED session={session_id}  topics={topics}  databases={databases}")
    except Exception as e:
        print(f"[Cache] save error: {e}")
        raise   # re-raise so the caller knows save failed
    finally:
        if cursor:
            cursor.close()




# ══════════════════════════════════════════════════════
# CROSS-SOURCE RELATIONSHIP DETECTION
# ══════════════════════════════════════════════════════


def detect_cross_source_relationships(table_columns: dict, web_data: list, db_data: list) -> dict:
    """
    Ask Mistral to dynamically generate a business-focused Knowledge Graph.
    """
    db_summary = {}
    for db in db_data:
        for tbl in db["tables"]:
            # tables never carry "column_stats" — real per-column sample/frequency
            # data lives in "business_aggregates" (populated by get_table_aggregates).
            # Feed that in instead, so the LLM has actual values to work from
            # rather than inventing SKU/customer codes.
            db_summary[tbl["table_name"]] = {
                "columns": tbl.get("columns", []),
                "business_aggregates": tbl.get("business_aggregates", [])
            }


    web_summary = []
    for w in web_data:
        for item in w["items"]:
            web_summary.append({
                "topic": w["topic"],
                "title": item["title"],
                "brief": (item.get("brief") or "")[:200]
            })


    prompt = f"""
You are an expert Knowledge Graph Builder specializing in tyre and automotive parts distribution data.


Analyze the uploaded sales dataset and generate a RICH, HIERARCHICAL, business-focused Knowledge Graph.
The graph MUST reflect the full product taxonomy AND all business relationships visible in the data from the 11 SAP S/4HANA Master, Planning/Target, and Sales Fact views:
- ZIVF_CustomerMaster_01_MD (Customer / Dealer Master)
- ZIVR_SKUMaster_01_TR (SKU / Material Master)
- ZIVF_CategoryMaster_01_TR (Category Master)
- ZITR_DayAttribute_01_TR (Day / Calendar Attribute)
- ZIVF_TerritoryMaster_01_TR (Territory Master)
- ZIVF_ZoneMaster_01_TR (Zone Master)
- ZIVF_RegionMaster_MD_01 (Region Master)
- ZIVF_SD_Sales_Plan_Target_01_TR (Sales Plan & Target View)
- ZPVF_SD_SalesHistory_01_TR (Sales History Fact)
- ZPVF_SD_CurrentSales_01_TR (Current Sales Fact)
- ZPVF_SD_CancelledSalesInvoice_01_TR (Cancelled Invoices Fact)


## DB Tables and Sample Data:
{json.dumps(db_summary, indent=2)}


## Web Data:
{json.dumps(web_summary, indent=2)}


---


## MANDATORY GRAPH STRUCTURE


### LEVEL 1 — Product Category Nodes (from Category Master ZIVF_CategoryMaster_01_TR)
Create one node per unique category found in the data (CAT_CODE, CAT_DESC, SHORT_DESC, e.g., Tyre, Tube, Flap).
Node type: "ProductCategory"


### LEVEL 2 — Construction Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Create one node per unique construction type (CONSTUCTION, BULD_TYPE, e.g., BIAS, RADIAL).
Node type: "Construction"


### LEVEL 3 — Tyre/Vehicle Type Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Create one node per unique vehicle/application type (TYRE_TYPE, TYRE_TP, TT_TYPE, e.g., TRUCK, LCV, CAR, 3W, SCV, FARM, OTR).
Node type: "TyreType"


### LEVEL 4 — Material / SKU Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Identify the TOP 15 most frequently appearing Materials/SKUs in the sales data (MATNR, MAKTX, PROD_TITLE).
Node type: "Material"


### LEVEL 5 — Customer/Dealer Nodes (from Customer Master ZIVF_CustomerMaster_01_MD)
Identify the TOP 15 most frequently appearing customers/dealers in the sales data (KUNNR, NAME1, BNAME).
Node type: "Customer"


### LEVEL 6 — Customer Class Nodes (from Customer Master ZIVF_CustomerMaster_01_MD)
Create one node for each customer class (KUKLA, KLABC, CLUB_CATG, DLR_CATG, e.g., Champion, Club, Class A, Class B).
Node type: "CustomerClass"


### LEVEL 7 — Account Group Nodes (from Customer Master ZIVF_CustomerMaster_01_MD)
Create one node for each account group (KTOKD, KDGRP, e.g., Dealer, Fleet, OEM, ZOR, Z1).
Node type: "AccountGroup"


### LEVEL 8 — Geography Nodes (from Zone, Region & Territory Masters: ZIVF_ZoneMaster_01_TR, ZIVF_RegionMaster_MD_01, ZIVF_TerritoryMaster_01_TR)
Create nodes for Zone (P_ZONE, NAME), Region (region_code, region_name), and Territory (BZIRK, NAME) based on the data.
Node types: "Zone", "Region", "Territory"


### LEVEL 9 — Billing/Channel Type Nodes (from Fact Views & Customer Master: distribution_channel, BILL_TYPE, FKART, VTWEG, CH_TYPE)
Create nodes for each billing distribution channel (e.g., Replacement, OEM, Institutional, Export, ZOR, ZBCL).
Node type: "BillingChannel"


### LEVEL 10 — Brand Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Create one node per unique Brand (BRAND).
Node type: "Brand"


### LEVEL 11 — Sub Brand Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Create one node per unique Sub Brand (SUB_BRAND).
Node type: "SubBrand"


### LEVEL 12 — Size Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Create one node per unique Size (RIM_SIZE, ZSIZE_CD, DIAMETER).
Node type: "Size"


### LEVEL 13 — Material Group Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Create one node per unique Material Group (MATKL).
Node type: "MaterialGroup"


### LEVEL 14 — Catg Nodes (from SKU Master ZIVR_SKUMaster_01_TR)
Create one node per unique Catg (CATG, DB_CATG, MKTG_CATG).
Node type: "Catg"


### LEVEL 15 — Day / Calendar Nodes (from Day Attribute ZITR_DayAttribute_01_TR)
Create nodes representing distinct days, months or calendar periods (Date_, REP_MONTH, CAL_YEAR, FIN_YEAR).
Node type: "Day"


### LEVEL 16 — Target Plan Nodes (from Sales Plan Target ZIVF_SD_Sales_Plan_Target_01_TR)
Create nodes representing business targets and sales plans (PLAN_VALUE, ORG_PLAN, REV_PLAN, POWER_SKU, PREM_STATUS, ECO_TYPE, DEALER_CNT).
Node type: "TargetPlan"


## MANDATORY EDGES (Only create if supported by data)


1. PRODUCT HIERARCHY
   - (ProductCategory) --[HAS_CONSTRUCTION]--> (Construction)
   - (Construction) --[FITS_TYRE_TYPE]--> (TyreType)
   - (Material) --[BELONGS_TO_CATEGORY]--> (ProductCategory)       (ZIVR_SKUMaster_01_TR.CAT_CODE = ZIVF_CategoryMaster_01_TR.CAT_CODE)
   - (Material) --[HAS_CONSTRUCTION_TYPE]--> (Construction)        (ZIVR_SKUMaster_01_TR.CONSTUCTION)
   - (Material) --[USED_IN]--> (TyreType)                         (ZIVR_SKUMaster_01_TR.TYRE_TYPE / TYRE_TP)
   - (Brand) --[HAS_SUB_BRAND]--> (SubBrand)                      (ZIVR_SKUMaster_01_TR.BRAND -> SUB_BRAND)
   - (Material) --[HAS_BRAND]--> (Brand)                          (ZIVR_SKUMaster_01_TR.BRAND)
   - (Material) --[HAS_SIZE]--> (Size)                            (ZIVR_SKUMaster_01_TR.RIM_SIZE / ZSIZE_CD)
   - (Material) --[BELONGS_TO_MATERIAL_GROUP]--> (MaterialGroup)  (ZIVR_SKUMaster_01_TR.MATKL)
   - (Material) --[BELONGS_TO_CATG]--> (Catg)                     (ZIVR_SKUMaster_01_TR.CATG)


2. CUSTOMER & GEOGRAPHY HIERARCHY
   - (Zone) --[CONTAINS_REGION]--> (Region)                       (ZIVF_ZoneMaster_01_TR.P_ZONE = ZIVF_RegionMaster_MD_01.zone_code)
   - (Region) --[CONTAINS_TERRITORY]--> (Territory)               (ZIVF_RegionMaster_MD_01.region_code = ZIVF_TerritoryMaster_01_TR.REGIO)
   - (Zone) --[HAS_TERRITORY]--> (Territory)                      (ZIVF_ZoneMaster_01_TR.P_ZONE = ZIVF_TerritoryMaster_01_TR.P_ZONE)
   - (Territory) --[HAS_CUSTOMER]--> (Customer)                   (ZIVF_TerritoryMaster_01_TR.BZIRK = ZIVF_CustomerMaster_01_MD.BZIRK)
   - (Region) --[HAS_CUSTOMER]--> (Customer)                      (ZIVF_RegionMaster_MD_01.region_code = ZIVF_CustomerMaster_01_MD.REGIO)
   - (Customer) --[HAS_CLASS]--> (CustomerClass)                  (ZIVF_CustomerMaster_01_MD.KUKLA / KLABC / CLUB_CATG)
   - (Customer) --[BELONGS_TO_ACCOUNT_GROUP]--> (AccountGroup)    (ZIVF_CustomerMaster_01_MD.KTOKD / KDGRP)


3. FACT / TRANSACTION EDGES (Linking Customer to Material via Sales Fact Views: ZPVF_SD_SalesHistory_01_TR, ZPVF_SD_CurrentSales_01_TR, ZPVF_SD_CancelledSalesInvoice_01_TR)
   - (Customer) --[PURCHASED]--> (Material)                       (sd.dealer_code = cm.KUNNR, sd.SKU = sk.MATNR)
   - (Customer) --[PRIMARILY_BUYS]--> (ProductCategory)
   - (ProductCategory) --[SOLD_VIA]--> (BillingChannel)           (sd.distribution_channel / BILL_TYPE)
   - (Customer) --[SERVED_BY_CHANNEL]--> (BillingChannel)
   - (Customer) --[TRANSACTED_ON]--> (Day)                        (sd.invoice_date = day.Date_)


4. TARGET & PLANNING EDGES (from Target View ZIVF_SD_Sales_Plan_Target_01_TR)
   - (Territory) --[HAS_TARGET_PLAN]--> (TargetPlan)              (ZIVF_TerritoryMaster_01_TR.BZIRK = ZIVF_SD_Sales_Plan_Target_01_TR.TERR_CODE)
   - (Region) --[HAS_TARGET_PLAN]--> (TargetPlan)                 (ZIVF_RegionMaster_MD_01.region_code = ZIVF_SD_Sales_Plan_Target_01_TR.REG_CODE)
   - (TargetPlan) --[TARGETS_MATERIAL]--> (Material)              (ZIVF_SD_Sales_Plan_Target_01_TR.MATNR = ZIVR_SKUMaster_01_TR.MATNR)
   - (TargetPlan) --[TARGETS_CATEGORY]--> (ProductCategory)       (ZIVF_SD_Sales_Plan_Target_01_TR.CAT_CODE = ZIVF_CategoryMaster_01_TR.CAT_CODE)
   - (TargetPlan) --[PLANNED_FOR_PERIOD]--> (Day)                 (ZIVF_SD_Sales_Plan_Target_01_TR.YMONTH = ZITR_DayAttribute_01_TR.YMONTH)


---


## NUMERICAL PROPERTIES (store as node/edge properties, NEVER as separate nodes)
- On ProductCategory / Material nodes: total_sales_qty (Tyre_Quantity), total_invoice_value, total_discount, taxable_value
- On Customer nodes: total_invoice_value, total_sales_qty, outstand, exposure
- On TargetPlan nodes: plan_value (PLAN_VALUE), org_plan_qty (ORG_PLAN), rev_plan_val (REV_PLAN_VAL), rev_plan_qty (REV_PLAN), ho_plan (HO_PLAN), dealer_cnt (DEALER_CNT), outstd (OUTSTD), exposure (EXPOSURE)
- On PURCHASED edges: transaction_count, total_invoice_value, total_qty, total_discount


---


## OUTPUT FORMAT
Return EXACTLY this JSON (no markdown, no extra text):
{{
  "nodes": [
    {{"id": "cat_tyre", "label": "Tyre", "type": "ProductCategory", "properties": {{"total_invoice_value": 0}}}},
    {{"id": "const_bias", "label": "BIAS", "type": "Construction", "properties": {{}}}},
    {{"id": "tt_truck", "label": "TRUCK", "type": "TyreType", "properties": {{}}}},
    {{"id": "mat_1001", "label": "1001-TYRE", "type": "Material", "properties": {{}}}},
    {{"id": "cust_99", "label": "CUST 99", "type": "Customer", "properties": {{}}}},
    {{"id": "cls_A", "label": "Class A", "type": "CustomerClass", "properties": {{}}}},
    {{"id": "acc_dealer", "label": "Dealer", "type": "AccountGroup", "properties": {{}}}},
    {{"id": "zone_north", "label": "North Zone", "type": "Zone", "properties": {{}}}},
    {{"id": "reg_jaipur", "label": "JAIPUR", "type": "Region", "properties": {{}}}},
    {{"id": "ter_north", "label": "North Terr", "type": "Territory", "properties": {{}}}},
    {{"id": "ch_repl", "label": "Replacement", "type": "BillingChannel", "properties": {{}}}},
    {{"id": "catg_1", "label": "Catg 1", "type": "Catg", "properties": {{}}}},
    {{"id": "brand_1", "label": "Brand X", "type": "Brand", "properties": {{}}}},
    {{"id": "subbrand_1", "label": "SubBrand Y", "type": "SubBrand", "properties": {{}}}},
    {{"id": "size_1", "label": "10.00-20", "type": "Size", "properties": {{}}}},
    {{"id": "day_1", "label": "2024-01-01", "type": "Day", "properties": {{}}}},
    {{"id": "tgt_1", "label": "TGT-202401", "type": "TargetPlan", "properties": {{"plan_value": 1500000}}}}
  ],
  "edges": [
    {{"from": "cat_tyre", "to": "const_bias", "label": "HAS_CONSTRUCTION", "properties": {{}}}},
    {{"from": "mat_1001", "to": "cat_tyre", "label": "BELONGS_TO_CATEGORY", "properties": {{}}}},
    {{"from": "cust_99", "to": "mat_1001", "label": "PURCHASED", "properties": {{"total_invoice_value": 50000}}}},
    {{"from": "zone_north", "to": "reg_jaipur", "label": "CONTAINS_REGION", "properties": {{}}}},
    {{"from": "reg_jaipur", "to": "ter_north", "label": "CONTAINS_TERRITORY", "properties": {{}}}},
    {{"from": "ter_north", "to": "cust_99", "label": "HAS_CUSTOMER", "properties": {{}}}},
    {{"from": "cust_99", "to": "cls_A", "label": "HAS_CLASS", "properties": {{}}}},
    {{"from": "cust_99", "to": "acc_dealer", "label": "BELONGS_TO_ACCOUNT_GROUP", "properties": {{}}}},
    {{"from": "mat_1001", "to": "brand_1", "label": "HAS_BRAND", "properties": {{}}}},
    {{"from": "brand_1", "to": "subbrand_1", "label": "HAS_SUB_BRAND", "properties": {{}}}},
    {{"from": "ter_north", "to": "tgt_1", "label": "HAS_TARGET_PLAN", "properties": {{}}}},
    {{"from": "tgt_1", "to": "mat_1001", "label": "TARGETS_MATERIAL", "properties": {{}}}}
  ],
  "identified_node_types": ["ProductCategory", "Construction", "TyreType", "Material", "Customer", "CustomerClass", "AccountGroup", "Zone", "Region", "Territory", "BillingChannel", "Brand", "SubBrand", "Size", "MaterialGroup", "Division", "Catg", "Day", "TargetPlan"],
  "identified_relationship_types": ["HAS_CONSTRUCTION", "FITS_TYRE_TYPE", "BELONGS_TO_CATEGORY", "HAS_CONSTRUCTION_TYPE", "USED_IN", "CONTAINS_REGION", "CONTAINS_TERRITORY", "HAS_TERRITORY", "HAS_CUSTOMER", "HAS_CLASS", "BELONGS_TO_ACCOUNT_GROUP", "PURCHASED", "PRIMARILY_BUYS", "SOLD_VIA", "SERVED_BY_CHANNEL", "TRANSACTED_ON", "HAS_SUB_BRAND", "HAS_BRAND", "HAS_SIZE", "BELONGS_TO_MATERIAL_GROUP", "BELONGS_TO_DIVISION", "BELONGS_TO_CATG", "HAS_TARGET_PLAN", "TARGETS_MATERIAL", "TARGETS_CATEGORY", "PLANNED_FOR_PERIOD"],
  "graph_schema": [
    "(Customer)-[:PURCHASED]->(Material)",
    "(Material)-[:BELONGS_TO_CATEGORY]->(ProductCategory)",
    "(Territory)-[:HAS_CUSTOMER]->(Customer)",
    "(Zone)-[:CONTAINS_REGION]->(Region)",
    "(Region)-[:CONTAINS_TERRITORY]->(Territory)",
    "(Territory)-[:HAS_TARGET_PLAN]->(TargetPlan)"
  ],
  "sample_cypher_queries": [
    "MATCH (c:Customer)-[p:PURCHASED]->(s:Material) RETURN c.label, s.label, p.total_invoice_value ORDER BY p.total_invoice_value DESC LIMIT 10",
    "MATCH (z:Zone)-[:CONTAINS_REGION]->(r:Region)-[:CONTAINS_TERRITORY]->(t:Territory)-[:HAS_CUSTOMER]->(c:Customer) RETURN z.label, count(c) as customer_count",
    "MATCH (m:Material)-[:BELONGS_TO_CATEGORY]->(cat:ProductCategory) RETURN cat.label, count(m) as sku_count",
    "MATCH (t:Territory)-[:HAS_TARGET_PLAN]->(tp:TargetPlan) RETURN t.label, tp.properties.plan_value ORDER BY tp.properties.plan_value DESC LIMIT 5"
  ],
  "business_insights": [
    "Identify any insights based on the relationship between Customer Classes and Top Materials",
    "Identify top zones and regions based on Customer purchases",
    "Analyze category distribution and tyre types across dealer network",
    "Evaluate target achievement and sales gaps across territories"
  ],
  "suggested_graphrag_paths": [
    "Start from Customer → PURCHASED → Material → BELONGS_TO_CATEGORY → ProductCategory",
    "Start from Zone → CONTAINS_REGION → Region → CONTAINS_TERRITORY → Territory → HAS_CUSTOMER → Customer",
    "Start from Material → HAS_BRAND → Brand → HAS_SUB_BRAND → SubBrand",
    "Start from Territory → HAS_TARGET_PLAN → TargetPlan → TARGETS_MATERIAL → Material"
  ]
}}


## RULES
1. Use ONLY node IDs you defined in the "nodes" array for "from"/"to" in edges.
2. Extract actual values from the sample data — do NOT invent Material codes or customer IDs.
3. Node IDs must be unique strings with no spaces (use underscores).
4. For nodes like Customer, Material, limit to the top 15 most frequent/significant ones from the sample to avoid overwhelming the graph, but capture all master table reference nodes.
5. CRITICAL: You MUST extract and create at least 2 nodes for Brand, SubBrand, Day, Size, MaterialGroup, Zone, and Catg if they appear in the provided sample data. Do not skip these dimensions.
"""


    messages = [{"role": "user", "content": prompt}]
    try:
        raw = call_llm_chat(messages, json_mode=True, temperature=0.1)
        result = json.loads(raw)
        result.setdefault("nodes", [])
        result.setdefault("edges", [])
        return result


    except Exception as e:
        print("Business Knowledge Graph detection error:", e)
        return {"nodes": [], "edges": []}




# ══════════════════════════════════════════════════════
# GRAPH GENERATOR
# ══════════════════════════════════════════════════════


def generate_session_graph(session_id, web_data, db_data, target_arango_db=None):


    net = Network(
        height="850px",
        width="100%",
        bgcolor="#222222",
        font_color="white",
        directed=True
    )


    type_colors = {
        "ProductCategory": "#ff4081",   # pink — top-level product
        "Construction":    "#e040fb",   # purple — construction subtype
        "TyreType":        "#2979ff",   # blue — end-use vehicle
        "Material":        "#9ccc65",   # green — SKU/product code
        "Catg":            "#a1887f",   # brown
        "Customer":        "#ffc107",   # amber — customer/dealer
        "CustomerClass":   "#ff9800",   # orange — customer class
        "AccountGroup":    "#ff5722",   # deep orange — account group
        "Zone":            "#e65100",   # dark orange — top geography
        "Region":          "#fb8c00",   # orange — geography
        "Territory":       "#f57c00",   # orange — geography child
        "BillingChannel":  "#00bfa5",   # teal — sales channel
        "Date":            "#00bcd4",
        "Month":           "#18ffff",
        "Brand":           "#ab47bc",   # purple
        "SubBrand":        "#ba68c8",   # lighter purple
        "Size":            "#8d6e63",   # brown
        "MaterialGroup":   "#66bb6a",   # light green
        "Division":        "#26a69a",   # teal
        "Day":             "#4dd0e1",   # cyan
        "TargetPlan":      "#00e676",   # vibrant emerald green — sales target/plan
    }


    table_columns = {}
    for db in db_data:
        for table in db["tables"]:
            table_columns[table["table_name"]] = table.get("columns", [])


    graph_data = detect_cross_source_relationships(table_columns, web_data, db_data)
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])


    existing_ids = set()


    for n in nodes:
        node_id = str(n.get("id"))
        if not node_id: continue
        label = str(n.get("label", node_id))
        ntype = str(n.get("type", "Entity"))
        
        props = n.get("properties", {})
        title_lines = [f"{k}: {v}" for k, v in props.items()]
        title = "\n".join(title_lines) if title_lines else ntype


        color = type_colors.get(ntype, "#b0bec5")
        
        net.add_node(node_id, label=label, title=title, color=color, size=25, type=ntype)
        existing_ids.add(node_id)


    for e in edges:
        from_node = str(e.get("from"))
        to_node = str(e.get("to"))
        label = str(e.get("label", ""))


        if from_node in existing_ids and to_node in existing_ids:
            props = e.get("properties", {})
            title_lines = [f"{k}: {v}" for k, v in props.items()]
            title = "\n".join(title_lines) if title_lines else label
            
            net.add_edge(from_node, to_node, label=label, title=title, color="#ff5252", width=2)
        else:
            print(f"[Graph] Skipped edge due to missing nodes: {from_node} -> {to_node}")


    html_filename = f"graph_{uuid.uuid4().hex[:8]}.html"
    html_path     = os.path.join(GRAPH_FOLDER, html_filename)
    net.save_graph(html_path)


    # --- Sync to ArangoDB ---
    try:
        _sync_to_arango(session_id, net.nodes, net.edges, target_arango_db)
    except Exception as e:
        print(f"[ArangoDB] Sync failed: {e}")


    return f"{BASE_URL}/graphs/{html_filename}"


# ══════════════════════════════════════════════════════
# ARANGODB SYNC
# ══════════════════════════════════════════════════════


def _safe_key(val: str) -> str:
    """ArangoDB _key allows only [a-zA-Z0-9_:.@()-]+"""
    return re.sub(r'[^a-zA-Z0-9_:.@()-]', '_', str(val))


def _sync_to_arango(session_id: str, nodes: list, edges: list, target_arango_db: str = None):
    print(f"[ArangoDB] Connecting to {ARANGO_HOST} ...")
    client = ArangoClient(hosts=ARANGO_HOST)
    sys_db = client.db('_system', username=ARANGO_USER, password=ARANGO_PASS)


    db_to_use = target_arango_db if target_arango_db else ARANGO_DB


    # Ensure database exists
    if not sys_db.has_database(db_to_use):
        sys_db.create_database(db_to_use)
    
    db = client.db(db_to_use, username=ARANGO_USER, password=ARANGO_PASS)


    # Ensure collections exist
    nodes_col_name = "session_nodes"
    edges_col_name = "session_edges"


    if not db.has_collection(nodes_col_name):
        db.create_collection(nodes_col_name)
    if not db.has_collection(edges_col_name):
        db.create_collection(edges_col_name, edge=True)


    print("[DEBUG] _sync_to_arango: REACHED BACKUP BLOCK", flush=True)
    from database.kgraph_backup_service import backup_kgraph_collections
    bkp_result = backup_kgraph_collections(db, nodes_col_name, edges_col_name)
    print(f"[DEBUG] _sync_to_arango: BACKUP DONE -- skipped={bkp_result.get('skipped')}", flush=True)


    nodes_col = db.collection(nodes_col_name)
    edges_col = db.collection(edges_col_name)


    # Sync Nodes
    print(f"[ArangoDB] Syncing {len(nodes)} nodes...")
    for n in nodes:
        key = _safe_key(n["id"])
        doc = {
            "_key": key,
            "session_id": session_id,
            "label": n.get("label", ""),
            "original_id": n["id"],
            "type": n.get("type") or (n["id"].split('_')[0] if '_' in n["id"] else "unknown")
        }
        try:
            if nodes_col.has(key):
                nodes_col.update(doc)
            else:
                nodes_col.insert(doc)
        except Exception as e:
            print(f"[ArangoDB] skip node {key}: {e}")


    # Sync Edges
    print(f"[ArangoDB] Syncing {len(edges)} edges...")
    for e in edges:
        from_key = _safe_key(e["from"])
        to_key   = _safe_key(e["to"])
        edge_key = _safe_key(f"{from_key}_to_{to_key}")
        
        doc = {
            "_key": edge_key,
            "_from": f"{nodes_col_name}/{from_key}",
            "_to": f"{nodes_col_name}/{to_key}",
            "session_id": session_id,
            "label": e.get("label", ""),
            "title": e.get("title", "")
        }
        try:
            if edges_col.has(edge_key):
                edges_col.update(doc)
            else:
                edges_col.insert(doc)
        except Exception as ex:
            print(f"[ArangoDB] skip edge {edge_key}: {ex}")


    print("[ArangoDB] Sync Complete!")




# ══════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════


def _fetch_web_data(session_id: str, topics: list, conn) -> list:
    if not topics:
        return []
    cursor = None
    results = []
    try:
        cursor = conn.cursor(dictionary=True)
        placeholders = ",".join(["%s"] * len(topics))
        cursor.execute(f"""
            SELECT topic, title, url, brief, saved_at
            FROM saved_web_results
            WHERE `session_id` = %s
              AND topic IN ({placeholders})
            ORDER BY topic, saved_at DESC
        """, [session_id] + topics)
        rows = cursor.fetchall()


        grouped = {}
        for r in rows:
            t = r["topic"]
            grouped.setdefault(t, []).append({
                "title":    r["title"],
                "url":      r["url"],
                "brief":    r.get("brief", ""),
                "saved_at": str(r["saved_at"]) if r["saved_at"] else None
            })


        for topic, items in grouped.items():
            results.append({
                "source_type":  "web",
                "topic":        topic,
                "result_count": len(items),
                "items":        items
            })
    except Exception as e:
        print(f"[Analysis] web fetch error: {e}")
    finally:
        if cursor: cursor.close()
    return results




# ══════════════════════════════════════════════════════
# HARDCODED CORE KPIs — deterministic, no LLM involved
# ══════════════════════════════════════════════════════
def _compute_core_kpis(cursor) -> dict:
    """
    Runs the fixed set of core business KPIs directly — deterministic, no LLM involved.
    Each query group first checks whether its required tables actually exist.
    
    Supports SAP S/4HANA Views:
      - ZPVF_SD_SalesHistory_01_TR / ZPVF_SD_CurrentSales_01_TR / ZPVF_SD_CancelledSalesInvoice_01_TR
      - ZIVF_CustomerMaster_01_MD
      - ZIVR_SKUMaster_01_TR
      - ZIVF_CategoryMaster_01_TR
      - ZIVF_TerritoryMaster_01_TR
      - ZIVF_RegionMaster_MD_01
      - ZIVF_ZoneMaster_01_TR
      - ZITR_DayAttribute_01_TR
      - ZIVF_SD_Sales_Plan_Target_01_TR
    (and falls back to legacy table names if present).
    """
    kpis = {}


    cursor.execute("SHOW TABLES")
    raw_tables = [list(row.values())[0] for row in cursor.fetchall()]
    table_lookup = {t.lower(): t for t in raw_tables}


    def resolve(*names):
        for n in names:
            if n.lower() in table_lookup:
                return table_lookup[n.lower()]
        return None


    sales_tbl  = resolve("ZPVF_SD_SalesHistory_01_TR", "ZPVF_SD_CurrentSales_01_TR", "ZPVF_SD_CancelledSalesInvoice_01_TR", "sales_data")
    sku_tbl    = resolve("ZIVR_SKUMaster_01_TR", "material_master")
    cat_tbl    = resolve("ZIVF_CategoryMaster_01_TR", "category_master")
    cust_tbl   = resolve("ZIVF_CustomerMaster_01_MD", "customer_master")
    terr_tbl   = resolve("ZIVF_TerritoryMaster_01_TR", "territory_master")
    reg_tbl    = resolve("ZIVF_RegionMaster_MD_01", "region_master")
    zone_tbl   = resolve("ZIVF_ZoneMaster_01_TR", "zone_master")
    day_tbl    = resolve("ZITR_DayAttribute_01_TR", "day_master")
    target_tbl = resolve("ZIVF_SD_Sales_Plan_Target_01_TR", "sales_plan_target", "sales_target", "target_master")


    if not sales_tbl:
        print("[CoreKPI] No sales fact table found in database")
        return kpis


    # Detect exact column names on sales_tbl
    is_sap = True
    col_revenue  = "invoice_value"
    col_qty      = "Tyre_Quantity"
    col_date     = "invoice_date"
    col_cust     = "dealer_code"
    col_sku      = "SKU"
    col_channel  = "distribution_channel"
    col_discount = "total_discount"


    try:
        cursor.execute(f"SELECT * FROM `{sales_tbl}` LIMIT 0")
        cursor.fetchall()
        cols_lower = {desc[0].lower() for desc in cursor.description}
        if "invoice_value_inr" in cols_lower:
            is_sap = False
            col_revenue  = "Invoice_Value_INR"
            col_qty      = "Sales_Qty"
            col_date     = "billing__doc_date"
            col_cust     = "customer"
            col_sku      = "material"
            col_channel  = "distribution__Channel"
            col_discount = "Total_Discount_INR"
        else:
            col_revenue  = "invoice_value" if "invoice_value" in cols_lower else "taxable_value"
            col_qty      = "Tyre_Quantity" if "tyre_quantity" in cols_lower else ("volume" if "volume" in cols_lower else "old_code")
            col_date     = "invoice_date" if "invoice_date" in cols_lower else "Date_"
            col_cust     = "dealer_code" if "dealer_code" in cols_lower else "ship_to_party"
            col_sku      = "SKU" if "sku" in cols_lower else "MATNR"
            col_channel  = "distribution_channel" if "distribution_channel" in cols_lower else "distribution__Channel"
            col_discount = "total_discount" if "total_discount" in cols_lower else "Total_Discount_INR"
    except Exception as e:
        print(f"[CoreKPI] Column detection failed on {sales_tbl}: {e}")


    def run(label, sql, required_tables=None):
        if required_tables:
            missing = [t for t in required_tables if not t]
            if missing:
                print(f"[CoreKPI] '{label}' skipped — missing table(s): {missing}")
                kpis[label] = None
                return
        try:
            cursor.execute(sql)
            kpis[label] = cursor.fetchall()
        except Exception as e:
            print(f"[CoreKPI] '{label}' failed: {e}")
            kpis[label] = None


    # ---------- 1. Overall Performance ----------
    run("total_revenue", f"""
        SELECT ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Total_Revenue
        FROM `{sales_tbl}` sd
    """, required_tables=[sales_tbl])


    run("total_quantity", f"""
        SELECT ROUND(SUM(sd.{col_qty}), 2) AS Total_Quantity
        FROM `{sales_tbl}` sd
    """, required_tables=[sales_tbl])


    run("total_transactions", f"""
        SELECT COUNT(*) AS Total_Transactions
        FROM `{sales_tbl}` sd
    """, required_tables=[sales_tbl])


    run("avg_transaction_value", f"""
        SELECT ROUND(SUM(sd.{col_revenue})/COUNT(*), 2) AS Average_Transaction_Value
        FROM `{sales_tbl}` sd
    """, required_tables=[sales_tbl])


    run("avg_selling_price", f"""
        SELECT ROUND(SUM(sd.{col_revenue})/NULLIF(SUM(sd.{col_qty}), 0), 2) AS Average_Selling_Price
        FROM `{sales_tbl}` sd
    """, required_tables=[sales_tbl])


    # ---------- 2. Revenue Trend ----------
    run("highest_sales_month", f"""
        SELECT
            YEAR(sd.{col_date}) AS Sales_Year,
            MONTHNAME(sd.{col_date}) AS Month_Name,
            ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
        FROM `{sales_tbl}` sd
        WHERE sd.{col_date} IS NOT NULL
        GROUP BY
            YEAR(sd.{col_date}), MONTH(sd.{col_date}), MONTHNAME(sd.{col_date})
        ORDER BY Revenue DESC
        LIMIT 1
    """, required_tables=[sales_tbl])


    run("lowest_sales_month", f"""
        SELECT
            YEAR(sd.{col_date}) AS Sales_Year,
            MONTHNAME(sd.{col_date}) AS Month_Name,
            ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
        FROM `{sales_tbl}` sd
        WHERE sd.{col_date} IS NOT NULL
        GROUP BY
            YEAR(sd.{col_date}), MONTH(sd.{col_date}), MONTHNAME(sd.{col_date})
        ORDER BY Revenue ASC
        LIMIT 1
    """, required_tables=[sales_tbl])


    run("highest_growth_month", f"""
        WITH MonthlyRevenue AS (
            SELECT
                YEAR(sd.{col_date}) AS Sales_Year,
                MONTH(sd.{col_date}) AS Sales_Month,
                MONTHNAME(sd.{col_date}) AS Month_Name,
                (SUM(sd.{col_revenue})/10000000) AS Revenue
            FROM `{sales_tbl}` sd
            WHERE sd.{col_date} IS NOT NULL
            GROUP BY
                YEAR(sd.{col_date}), MONTH(sd.{col_date}), MONTHNAME(sd.{col_date})
        ),
        Growth AS (
            SELECT
                Sales_Year, Month_Name,
                ROUND(Revenue, 2) AS Current_Revenue,
                ROUND(
                    (Revenue - LAG(Revenue) OVER(ORDER BY Sales_Year, Sales_Month))
                    / NULLIF(LAG(Revenue) OVER(ORDER BY Sales_Year, Sales_Month), 0) * 100
                , 2) AS MoM_Growth_Percentage
            FROM MonthlyRevenue
        )
        SELECT * FROM Growth
        WHERE MoM_Growth_Percentage IS NOT NULL
        ORDER BY MoM_Growth_Percentage DESC
        LIMIT 1
    """, required_tables=[sales_tbl])


    run("lowest_growth_month", f"""
        WITH MonthlyRevenue AS (
            SELECT
                YEAR(sd.{col_date}) AS Sales_Year,
                MONTH(sd.{col_date}) AS Sales_Month,
                MONTHNAME(sd.{col_date}) AS Month_Name,
                (SUM(sd.{col_revenue})/10000000) AS Revenue
            FROM `{sales_tbl}` sd
            WHERE sd.{col_date} IS NOT NULL
            GROUP BY
                YEAR(sd.{col_date}), MONTH(sd.{col_date}), MONTHNAME(sd.{col_date})
        ),
        Growth AS (
            SELECT
                Sales_Year, Month_Name,
                ROUND(Revenue, 2) AS Current_Revenue,
                ROUND(
                    (Revenue - LAG(Revenue) OVER(ORDER BY Sales_Year, Sales_Month))
                    / NULLIF(LAG(Revenue) OVER(ORDER BY Sales_Year, Sales_Month), 0) * 100
                , 2) AS MoM_Growth_Percentage
            FROM MonthlyRevenue
        )
        SELECT * FROM Growth
        WHERE MoM_Growth_Percentage IS NOT NULL
        ORDER BY MoM_Growth_Percentage ASC
        LIMIT 1
    """, required_tables=[sales_tbl])


    # ---------- 3. Product Performance ----------
    if is_sap and sku_tbl and cat_tbl:
        run("top_category", f"""
            SELECT
                COALESCE(cat.CAT_DESC, cat.SHORT_DESC, 'Unmapped Category') AS category_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            JOIN `{cat_tbl}` cat ON sk.CAT_CODE = cat.CAT_CODE
            GROUP BY category_name
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, sku_tbl, cat_tbl])


        run("lowest_category", f"""
            SELECT
                COALESCE(cat.CAT_DESC, cat.SHORT_DESC, 'Unmapped Category') AS category_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            JOIN `{cat_tbl}` cat ON sk.CAT_CODE = cat.CAT_CODE
            GROUP BY category_name
            ORDER BY Revenue ASC
            LIMIT 1
        """, required_tables=[sales_tbl, sku_tbl, cat_tbl])
    elif sku_tbl and cat_tbl:
        run("top_category", f"""
            SELECT
                COALESCE(cat.category_name, 'Unmapped Category') AS category_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            JOIN `{cat_tbl}` cat ON sk.category = cat.category_code
            GROUP BY category_name
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, sku_tbl, cat_tbl])


        run("lowest_category", f"""
            SELECT
                COALESCE(cat.category_name, 'Unmapped Category') AS category_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            JOIN `{cat_tbl}` cat ON sk.category = cat.category_code
            GROUP BY category_name
            ORDER BY Revenue ASC
            LIMIT 1
        """, required_tables=[sales_tbl, sku_tbl, cat_tbl])


    # Construction
    if is_sap and sku_tbl:
        run("top_construction", f"""
            SELECT
                COALESCE(sk.CONSTUCTION, sk.BULD_TYPE, 'Unmapped Construction') AS construction_description,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            WHERE (sk.CONSTUCTION IS NOT NULL AND sk.CONSTUCTION != '') OR (sk.BULD_TYPE IS NOT NULL AND sk.BULD_TYPE != '')
            GROUP BY construction_description
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, sku_tbl])
    elif sku_tbl:
        cons_tbl = resolve("construction_master")
        if cons_tbl:
            run("top_construction", f"""
                SELECT
                    COALESCE(cons.construction_description, 'Unmapped Construction') AS construction_description,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
                JOIN `{cons_tbl}` cons ON CAST(sk.construction AS CHAR) = cons.construction_code
                GROUP BY construction_description
                ORDER BY Revenue DESC
                LIMIT 1
            """, required_tables=[sales_tbl, sku_tbl, cons_tbl])


    # Tyre Type
    if is_sap and sku_tbl:
        run("top_tyre_type", f"""
            SELECT
                COALESCE(sk.TYRE_TYPE, sk.TYRE_TP, sk.TT_TYPE, 'Unmapped Tyre Type') AS tyre_type_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            WHERE (sk.TYRE_TYPE IS NOT NULL AND sk.TYRE_TYPE != '') OR (sk.TYRE_TP IS NOT NULL AND sk.TYRE_TP != '')
            GROUP BY tyre_type_name
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, sku_tbl])
    elif sku_tbl:
        tt_tbl = resolve("tyre_type_master")
        if tt_tbl:
            run("top_tyre_type", f"""
                SELECT
                    COALESCE(tt.tyre_type_name, 'Unmapped Tyre Type') AS tyre_type_name,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
                JOIN `{tt_tbl}` tt ON sk.tyre_type = tt.tyre_type_code
                GROUP BY tyre_type_name
                ORDER BY Revenue DESC
                LIMIT 1
            """, required_tables=[sales_tbl, sku_tbl, tt_tbl])


    # Top/Bottom 5 Products
    if sku_tbl:
        col_prod_name = "COALESCE(sk.MAKTX, sk.PROD_TITLE, 'Unmapped Product')" if is_sap else "COALESCE(sk.MAKTX, 'Unmapped Product')"
        run("top_5_products", f"""
            SELECT
                {col_prod_name} AS product_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            GROUP BY product_name
            ORDER BY Revenue DESC
            LIMIT 5
        """, required_tables=[sales_tbl, sku_tbl])


        run("bottom_5_products", f"""
            SELECT
                {col_prod_name} AS product_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{sku_tbl}` sk ON sd.{col_sku} = sk.MATNR
            WHERE sk.MAKTX IS NOT NULL
            GROUP BY product_name
            ORDER BY Revenue ASC
            LIMIT 5
        """, required_tables=[sales_tbl, sku_tbl])


    # ---------- 4. Customer Performance ----------
    if cust_tbl:
        col_cust_name = f"COALESCE(cm.NAME1, cm.BNAME, CONCAT('Unmapped Account (', sd.{col_cust}, ')'))" if is_sap else f"COALESCE(cm.Cname, CONCAT('Unmapped Account (', sd.{col_cust}, ')'))"
        run("top_customer", f"""
            SELECT
                {col_cust_name} AS Customer_Name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            GROUP BY Customer_Name
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, cust_tbl])


        run("top_5_customers", f"""
            SELECT
                {col_cust_name} AS Customer_Name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            GROUP BY Customer_Name
            ORDER BY Revenue DESC
            LIMIT 5
        """, required_tables=[sales_tbl, cust_tbl])


        if is_sap:
            run("top_dealers", f"""
                SELECT
                    {col_cust_name} AS Dealer_Name,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                WHERE cm.KTOKD LIKE '%DEAL%' OR cm.CH_TYPE LIKE '%DEAL%' OR cm.DLR_CATG IS NOT NULL OR cm.NAME1 IS NOT NULL
                GROUP BY Dealer_Name
                ORDER BY Revenue DESC
                LIMIT 5
            """, required_tables=[sales_tbl, cust_tbl])


            run("dealer_contribution_pct", f"""
                SELECT ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Dealer_Contribution_Percentage
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                WHERE cm.KTOKD LIKE '%DEAL%' OR cm.CH_TYPE LIKE '%DEAL%' OR cm.DLR_CATG IS NOT NULL
            """, required_tables=[sales_tbl, cust_tbl])


            run("fleet_contribution_pct", f"""
                SELECT ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Fleet_Contribution_Percentage
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                WHERE cm.KTOKD LIKE '%FLEET%' OR cm.CH_TYPE LIKE '%FLEET%' OR cm.KDGRP LIKE '%FLEET%'
            """, required_tables=[sales_tbl, cust_tbl])


            run("oem_contribution_pct", f"""
                SELECT ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS OEM_Contribution_Percentage
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                WHERE cm.KTOKD LIKE '%OEM%' OR cm.CH_TYPE LIKE '%OEM%' OR cm.KDGRP LIKE '%OEM%'
            """, required_tables=[sales_tbl, cust_tbl])
        else:
            ag_tbl = resolve("account_group_master")
            if ag_tbl:
                run("top_dealers", f"""
                    SELECT
                        {col_cust_name} AS Dealer_Name,
                        ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                    FROM `{sales_tbl}` sd
                    JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                    JOIN `{ag_tbl}` ag ON cm.acc_grp = ag.KTOKD
                    WHERE ag.account_group_name = 'Dealer'
                    GROUP BY Dealer_Name
                    ORDER BY Revenue DESC
                    LIMIT 5
                """, required_tables=[sales_tbl, cust_tbl, ag_tbl])


                run("dealer_contribution_pct", f"""
                    SELECT ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Dealer_Contribution_Percentage
                    FROM `{sales_tbl}` sd
                    JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                    JOIN `{ag_tbl}` ag ON cm.acc_grp = ag.KTOKD
                    WHERE ag.account_group_name = 'Dealer'
                """, required_tables=[sales_tbl, cust_tbl, ag_tbl])


                run("fleet_contribution_pct", f"""
                    SELECT ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Fleet_Contribution_Percentage
                    FROM `{sales_tbl}` sd
                    JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                    JOIN `{ag_tbl}` ag ON cm.acc_grp = ag.KTOKD
                    WHERE ag.account_group_name = 'Fleet'
                """, required_tables=[sales_tbl, cust_tbl, ag_tbl])


                run("oem_contribution_pct", f"""
                    SELECT ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS OEM_Contribution_Percentage
                    FROM `{sales_tbl}` sd
                    JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                    JOIN `{ag_tbl}` ag ON cm.acc_grp = ag.KTOKD
                    WHERE ag.account_group_name = 'OEM'
                """, required_tables=[sales_tbl, cust_tbl, ag_tbl])


        run("customer_concentration", f"""
            SELECT
                {col_cust_name} AS Customer_Name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue,
                ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Contribution_Percentage
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            GROUP BY Customer_Name
            ORDER BY Contribution_Percentage DESC
            LIMIT 5
        """, required_tables=[sales_tbl, cust_tbl])


    # ---------- 5. Geography ----------
    if is_sap and cust_tbl:
        # SAP Geography: cust -> terr (BZIRK) -> reg (REGIO) -> zone (P_ZONE)
        join_terr = f"LEFT JOIN `{terr_tbl}` tm ON cm.BZIRK = tm.BZIRK" if terr_tbl else ""
        join_reg  = f"LEFT JOIN `{reg_tbl}` rm ON COALESCE(tm.REGIO, cm.REGIO) = rm.region_code" if reg_tbl and terr_tbl else (f"LEFT JOIN `{reg_tbl}` rm ON cm.REGIO = rm.region_code" if reg_tbl else "")
        join_zone = f"LEFT JOIN `{zone_tbl}` zm ON COALESCE(tm.P_ZONE, rm.zone_code) = zm.P_ZONE" if zone_tbl and terr_tbl and reg_tbl else (f"LEFT JOIN `{zone_tbl}` zm ON tm.P_ZONE = zm.P_ZONE" if zone_tbl and terr_tbl else "")


        if zone_tbl:
            run("top_zone", f"""
                SELECT
                    COALESCE(zm.NAME, tm.P_ZONE, rm.zone_code, 'Unmapped Zone') AS Zone,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                {join_terr}
                {join_reg}
                {join_zone}
                GROUP BY Zone
                ORDER BY Revenue DESC
                LIMIT 1
            """, required_tables=[sales_tbl, cust_tbl, zone_tbl])


        if reg_tbl:
            run("top_region", f"""
                SELECT
                    COALESCE(rm.region_name, cm.REGIO, 'Unmapped Region') AS region_name,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                {join_terr}
                {join_reg}
                GROUP BY region_name
                ORDER BY Revenue DESC
                LIMIT 1
            """, required_tables=[sales_tbl, cust_tbl, reg_tbl])


            run("region_contribution_pct", f"""
                SELECT
                    COALESCE(rm.region_name, cm.REGIO, 'Unmapped Region') AS region_name,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue,
                    ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Contribution_Percentage
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                {join_terr}
                {join_reg}
                GROUP BY region_name
                ORDER BY Revenue DESC
                LIMIT 5
            """, required_tables=[sales_tbl, cust_tbl, reg_tbl])


        if terr_tbl:
            run("top_territory", f"""
                SELECT
                    COALESCE(tm.NAME, cm.BZIRK, 'Unmapped Territory') AS territory_name,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                JOIN `{terr_tbl}` tm ON cm.BZIRK = tm.BZIRK
                GROUP BY territory_name
                ORDER BY Revenue DESC
                LIMIT 1
            """, required_tables=[sales_tbl, cust_tbl, terr_tbl])


            run("lowest_territory", f"""
                SELECT
                    COALESCE(tm.NAME, cm.BZIRK, 'Unmapped Territory') AS territory_name,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                JOIN `{terr_tbl}` tm ON cm.BZIRK = tm.BZIRK
                WHERE tm.NAME IS NOT NULL
                GROUP BY territory_name
                ORDER BY Revenue ASC
                LIMIT 1
            """, required_tables=[sales_tbl, cust_tbl, terr_tbl])


            run("weak_territory", f"""
                SELECT
                    COALESCE(tm.NAME, cm.BZIRK, 'Unmapped Territory') AS territory_name,
                    ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
                FROM `{sales_tbl}` sd
                JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                JOIN `{terr_tbl}` tm ON cm.BZIRK = tm.BZIRK
                WHERE tm.NAME IS NOT NULL
                GROUP BY territory_name
                ORDER BY Revenue ASC
                LIMIT 5
            """, required_tables=[sales_tbl, cust_tbl, terr_tbl])


    elif cust_tbl and terr_tbl and reg_tbl:
        # Legacy Geography joins
        run("top_zone", f"""
            SELECT
                CASE COALESCE(rm.zone, 'UNMAPPED')
                    WHEN 'EZ' THEN 'East Zone' WHEN 'WZ' THEN 'West Zone'
                    WHEN 'NZ' THEN 'North Zone' WHEN 'SZ' THEN 'South Zone I'
                    WHEN 'TZ' THEN 'South Zone II' WHEN 'CZ' THEN 'Central Zone'
                    WHEN 'NP' THEN 'Nepal' ELSE 'Unmapped Zone'
                END AS Zone,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            JOIN `{terr_tbl}` tm ON CAST(cm.territory AS CHAR) = tm.territory_code
            JOIN `{reg_tbl}` rm ON tm.region_code = CAST(rm.region AS CHAR)
            GROUP BY rm.zone
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, cust_tbl, terr_tbl, reg_tbl])


        run("top_region", f"""
            SELECT
                COALESCE(rm.region_name, 'Unmapped Region') AS region_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            JOIN `{terr_tbl}` tm ON CAST(cm.territory AS CHAR) = tm.territory_code
            JOIN `{reg_tbl}` rm ON tm.region_code = CAST(rm.region AS CHAR)
            GROUP BY region_name
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, cust_tbl, terr_tbl, reg_tbl])


        run("top_territory", f"""
            SELECT
                COALESCE(tm.territory_name, 'Unmapped Territory') AS territory_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            JOIN `{terr_tbl}` tm ON CAST(cm.territory AS CHAR) = tm.territory_code
            GROUP BY territory_name
            ORDER BY Revenue DESC
            LIMIT 1
        """, required_tables=[sales_tbl, cust_tbl, terr_tbl])


        run("lowest_territory", f"""
            SELECT
                COALESCE(tm.territory_name, 'Unmapped Territory') AS territory_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            JOIN `{terr_tbl}` tm ON CAST(cm.territory AS CHAR) = tm.territory_code
            WHERE tm.territory_name IS NOT NULL
            GROUP BY territory_name
            ORDER BY Revenue ASC
            LIMIT 1
        """, required_tables=[sales_tbl, cust_tbl, terr_tbl])


        run("region_contribution_pct", f"""
            SELECT
                COALESCE(rm.region_name, 'Unmapped Region') AS region_name,
                ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue,
                ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Contribution_Percentage
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            JOIN `{terr_tbl}` tm ON CAST(cm.territory AS CHAR) = tm.territory_code
            JOIN `{reg_tbl}` rm ON tm.region_code = CAST(rm.region AS CHAR)
            GROUP BY region_name
            ORDER BY Revenue DESC
            LIMIT 5
        """, required_tables=[sales_tbl, cust_tbl, terr_tbl, reg_tbl])


        run("weak_territory", f"""
            SELECT COALESCE(tm.territory_name, 'Unmapped Territory') AS territory_name, ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
            FROM `{sales_tbl}` sd
            JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
            JOIN `{terr_tbl}` tm ON CAST(cm.territory AS CHAR) = tm.territory_code
            WHERE tm.territory_name IS NOT NULL
            GROUP BY territory_name
            ORDER BY Revenue ASC
            LIMIT 5
        """, required_tables=[sales_tbl, cust_tbl, terr_tbl])


    # ---------- 6. Distribution Analysis ----------
    run("distribution_revenue", f"""
        SELECT COALESCE(sd.{col_channel}, 'Unmapped Channel') AS distribution_name, ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue
        FROM `{sales_tbl}` sd
        GROUP BY distribution_name
        ORDER BY Revenue DESC
    """, required_tables=[sales_tbl])


    run("distribution_quantity", f"""
        SELECT COALESCE(sd.{col_channel}, 'Unmapped Channel') AS distribution_name, ROUND(SUM(sd.{col_qty}), 2) AS Quantity
        FROM `{sales_tbl}` sd
        GROUP BY distribution_name
        ORDER BY Quantity DESC
    """, required_tables=[sales_tbl])


    run("distribution_contribution_pct", f"""
        SELECT
            COALESCE(sd.{col_channel}, 'Unmapped Channel') AS distribution_name,
            ROUND(SUM(sd.{col_revenue})/10000000, 2) AS Revenue,
            ROUND(SUM(sd.{col_revenue}) * 100 / NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0), 2) AS Contribution_Percentage
        FROM `{sales_tbl}` sd
        GROUP BY distribution_name
        ORDER BY Revenue DESC
    """, required_tables=[sales_tbl])


    # ---------- 7. Pricing Analysis ----------
    run("pricing", f"""
        SELECT
            ROUND(SUM(sd.{col_discount}), 2) AS discount_amount,
            ROUND(
                ABS(SUM(sd.{col_discount})) * 100 /
                NULLIF((SELECT SUM({col_revenue}) FROM `{sales_tbl}`), 0)
            , 2) AS discount_percentage
        FROM `{sales_tbl}` sd
    """, required_tables=[sales_tbl])


    # ---------- 8. Business Risks ----------
    run("high_customer_dependency", f"""
        SELECT
            CASE WHEN (
                SELECT SUM(Revenue) FROM (
                    SELECT SUM(sd.{col_revenue}) AS Revenue FROM `{sales_tbl}` sd
                    GROUP BY sd.{col_cust} ORDER BY Revenue DESC LIMIT 10
                ) t
            ) > (SELECT SUM({col_revenue})*0.50 FROM `{sales_tbl}`)
            THEN 'High Customer Dependency' ELSE 'Normal' END AS Risk_Status
    """, required_tables=[sales_tbl])


    if cust_tbl:
        cond_dealer = "cm.KTOKD LIKE '%DEAL%' OR cm.CH_TYPE LIKE '%DEAL%' OR cm.DLR_CATG IS NOT NULL" if is_sap else "ag.account_group_name = 'Dealer'"
        join_ag = "" if is_sap else f"JOIN `{resolve('account_group_master')}` ag ON cm.acc_grp = ag.KTOKD"
        run("high_dealer_dependency", f"""
            SELECT
                CASE WHEN (
                    SELECT SUM(sd.{col_revenue}) FROM `{sales_tbl}` sd
                    JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                    {join_ag}
                    WHERE {cond_dealer}
                ) > (SELECT SUM({col_revenue})*0.60 FROM `{sales_tbl}`)
                THEN 'High Dealer Dependency' ELSE 'Normal' END AS Risk_Status
        """, required_tables=[sales_tbl, cust_tbl])


    # ---------- 9. Target vs Actual Performance (from ZIVF_SD_Sales_Plan_Target_01_TR) ----------
    if target_tbl:
        col_tgt_val  = "PLAN_VALUE"
        col_tgt_qty  = "ORG_PLAN"
        col_tgt_terr = "TERR_CODE"
        col_tgt_cat  = "CAT_CODE"


        try:
            cursor.execute(f"SELECT * FROM `{target_tbl}` LIMIT 0")
            cursor.fetchall()
            tcols_lower = {desc[0].lower() for desc in cursor.description}
            if "plan_value" in tcols_lower:
                col_tgt_val = "PLAN_VALUE"
            elif "target_value" in tcols_lower:
                col_tgt_val = "target_value"
            if "org_plan" in tcols_lower:
                col_tgt_qty = "ORG_PLAN"
            elif "target_qty" in tcols_lower:
                col_tgt_qty = "target_qty"
            if "terr_code" in tcols_lower:
                col_tgt_terr = "TERR_CODE"
            elif "territory_code" in tcols_lower:
                col_tgt_terr = "territory_code"
            if "cat_code" in tcols_lower:
                col_tgt_cat = "CAT_CODE"
            elif "category_code" in tcols_lower:
                col_tgt_cat = "category_code"
        except Exception as e:
            print(f"[CoreKPI] Target column detection failed: {e}")


        run("target_performance", f"""
            SELECT
                ROUND(SUM(st.{col_tgt_val})/10000000, 2) AS Total_Target_Value,
                ROUND((SELECT SUM({col_revenue}) FROM `{sales_tbl}`)/10000000, 2) AS Total_Actual_Revenue,
                ROUND(((SELECT SUM({col_revenue}) FROM `{sales_tbl}`) * 100) / NULLIF(SUM(st.{col_tgt_val}), 0), 2) AS Overall_Achievement_Percentage,
                ROUND((SUM(st.{col_tgt_val}) - (SELECT SUM({col_revenue}) FROM `{sales_tbl}`))/10000000, 2) AS Target_Shortfall_Value
            FROM `{target_tbl}` st
        """, required_tables=[target_tbl, sales_tbl])


        run("target_vs_actual_quantity", f"""
            SELECT
                ROUND(SUM(st.{col_tgt_qty}), 2) AS Total_Target_Quantity,
                ROUND((SELECT SUM({col_qty}) FROM `{sales_tbl}`), 2) AS Total_Actual_Quantity,
                ROUND(((SELECT SUM({col_qty}) FROM `{sales_tbl}`) * 100) / NULLIF(SUM(st.{col_qty}), 0), 2) AS Quantity_Achievement_Percentage
            FROM `{target_tbl}` st
        """, required_tables=[target_tbl, sales_tbl])


        if terr_tbl and cust_tbl:
            run("territory_target_shortfall", f"""
                SELECT
                    COALESCE(tm.NAME, st.{col_tgt_terr}, 'Unmapped Territory') AS territory_name,
                    ROUND(SUM(st.{col_tgt_val})/10000000, 2) AS Target_Value,
                    ROUND(COALESCE(act.Actual_Revenue, 0)/10000000, 2) AS Actual_Revenue,
                    ROUND((COALESCE(act.Actual_Revenue, 0) * 100) / NULLIF(SUM(st.{col_tgt_val}), 0), 2) AS Achievement_Percentage,
                    ROUND((SUM(st.{col_tgt_val}) - COALESCE(act.Actual_Revenue, 0))/10000000, 2) AS Shortfall_Value
                FROM `{target_tbl}` st
                LEFT JOIN `{terr_tbl}` tm ON st.{col_tgt_terr} = tm.BZIRK
                LEFT JOIN (
                    SELECT cm.BZIRK, SUM(sd.{col_revenue}) AS Actual_Revenue
                    FROM `{sales_tbl}` sd
                    JOIN `{cust_tbl}` cm ON sd.{col_cust} = cm.KUNNR
                    GROUP BY cm.BZIRK
                ) act ON st.{col_tgt_terr} = act.BZIRK
                GROUP BY territory_name, act.Actual_Revenue
                ORDER BY Shortfall_Value DESC
                LIMIT 5
            """, required_tables=[target_tbl, sales_tbl, cust_tbl, terr_tbl])
        else:
            run("territory_target_shortfall", f"""
                SELECT
                    COALESCE(st.{col_tgt_terr}, 'Unmapped Territory') AS territory_name,
                    ROUND(SUM(st.{col_tgt_val})/10000000, 2) AS Target_Value
                FROM `{target_tbl}` st
                GROUP BY territory_name
                ORDER BY Target_Value DESC
                LIMIT 5
            """, required_tables=[target_tbl])


        if cat_tbl:
            run("category_target_achievement", f"""
                SELECT
                    COALESCE(cat.CAT_DESC, cat.SHORT_DESC, st.{col_tgt_cat}, 'Unmapped Category') AS category_name,
                    ROUND(SUM(st.{col_tgt_val})/10000000, 2) AS Target_Value,
                    ROUND(SUM(st.{col_tgt_qty}), 2) AS Target_Quantity
                FROM `{target_tbl}` st
                LEFT JOIN `{cat_tbl}` cat ON st.{col_tgt_cat} = cat.CAT_CODE
                GROUP BY category_name
                ORDER BY Target_Value DESC
            """, required_tables=[target_tbl, cat_tbl])


    return kpis




def _fetch_agentic_insights(cursor, tables_info: list, db_type: str) -> list:
    """
    Agentic Workflow:
    1. Sends schema to LLM and asks for 7-10 comprehensive business SQL queries.
    2. Executes the queries.
    3. Returns the successful results as a list of strings.
    NOTE: this is now used only for supplementary/exploratory insights —
    the core numeric backbone (Total Revenue, Top Category/Construction/
    Zone/Region/Customer, Distribution %, Claims, Returns, Target, etc.)
    is computed deterministically by _compute_core_kpis instead, so this
    function no longer needs to (and should not be relied on to) get those
    fields right on its own.
    """
    if not tables_info:
        return []
        
    schema_desc = []
    for t in tables_info:
        schema_desc.append(f"Table: {t['table_name']}")
        schema_desc.append(f"Columns: {', '.join(t['columns'])}")
    schema_str = "\n".join(schema_desc)
    
    system_prompt = f"""You are an expert Data Analyst and {db_type.upper()} DBA specializing in tyre and automotive parts distribution data.
Your task is to write advanced SQL queries that extract SUPPLEMENTARY business insights
beyond the core KPIs, which are already computed separately and provided verbatim
elsewhere in the context (look for "VERIFIED KPI BLOCK"). Do NOT recompute Total Revenue,
Total Quantity, Total Transactions, Top Category/Construction/Tyre Type, Top Customer,
Top Zone/Region/Territory, Distribution %, Claims, Returns, or Target Performance —
those are already handled. Focus instead on open-ended, exploratory angles using the SAP S/4HANA views. You MUST generate specific queries to uncover:
1. SEASONALITY & PERIODIC TRENDS: A month-over-month revenue trend (grouping by YEAR and MONTH of invoice_date or joining ZITR_DayAttribute_01_TR.REP_MONTH) to identify massive seasonal spikes or slumps.
2. SKU CONCENTRATION & PARETO: A query to find the top 5 SKUs (by MAKTX or PROD_TITLE in ZIVR_SKUMaster_01_TR) and their exact revenue contribution (invoice_value), as the business might be heavily reliant on a few materials.
3. ZONE-WISE PERFORMANCE: A full breakdown of revenue by zone (using ZIVF_CustomerMaster_01_MD -> ZIVF_TerritoryMaster_01_TR -> ZIVF_ZoneMaster_01_TR or ZIVF_RegionMaster_MD_01) to highlight high-performing vs underperforming zones.
4. TERRITORY & REGION PERFORMANCE: Detailed revenue and volume distribution across regions (ZIVF_RegionMaster_MD_01.region_name) and territories (ZIVF_TerritoryMaster_01_TR.NAME).
5. DISCOUNT IMPACT: Analyze the relationship between total_discount and invoice_value by category (ZIVF_CategoryMaster_01_TR.CAT_DESC) or customer class to reveal heavily discounted revenue sources.
6. CUSTOMER CHANNEL MIX & SEGMENTATION: Break down revenue by Customer Account Group (ZIVF_CustomerMaster_01_MD.KTOKD), Dealer Category (DLR_CATG), and Channel Type (CH_TYPE / distribution_channel) to reveal the split between Dealers, Fleets, and OEMs.
7. TARGET VS ACTUAL ACHIEVEMENT GAP: Join ZIVF_SD_Sales_Plan_Target_01_TR with sales fact and master views to pinpoint territories, regions, or categories where actual sales severely lag behind targets (PLAN_VALUE vs invoice_value) or where target volumes (ORG_PLAN / REV_PLAN) are unmet.


Always use the following joins when you do write queries. Each line below includes
a brief on what business question that join unlocks:


ZPVF_SD_SalesHistory_01_TR.dealer_code = ZIVF_CustomerMaster_01_MD.KUNNR
    -- Brief: links every sales transaction to its dealer / customer master record in ZIVF_CustomerMaster_01_MD (the primary customer entry point).
    -- Alternate ship-to join: ZPVF_SD_SalesHistory_01_TR.ship_to_party = ZIVF_CustomerMaster_01_MD.KUNNR.


ZPVF_SD_SalesHistory_01_TR.SKU = ZIVR_SKUMaster_01_TR.MATNR
    -- Brief: links a transaction to its product master in ZIVR_SKUMaster_01_TR (the entry point for SKU/product-level queries).


ZIVR_SKUMaster_01_TR.CAT_CODE = ZIVF_CategoryMaster_01_TR.CAT_CODE
    -- Brief: resolves product category description (CAT_DESC / SHORT_DESC: Tyre, Tube, Flap, etc.) from ZIVF_CategoryMaster_01_TR.


ZIVF_CustomerMaster_01_MD.BZIRK = ZIVF_TerritoryMaster_01_TR.BZIRK
    -- Brief: links customer in ZIVF_CustomerMaster_01_MD to territory in ZIVF_TerritoryMaster_01_TR using territory code BZIRK.


ZIVF_TerritoryMaster_01_TR.REGIO = ZIVF_RegionMaster_MD_01.region_code (or ZIVF_CustomerMaster_01_MD.REGIO = ZIVF_RegionMaster_MD_01.region_code)
    -- Brief: rolls territory or customer up to region in ZIVF_RegionMaster_MD_01 (region_name).


ZIVF_TerritoryMaster_01_TR.P_ZONE = ZIVF_ZoneMaster_01_TR.P_ZONE (or ZIVF_RegionMaster_MD_01.zone_code = ZIVF_ZoneMaster_01_TR.P_ZONE)
    -- Brief: rolls territory or region up to PAN-India Zone in ZIVF_ZoneMaster_01_TR (NAME: East, West, North, South, Central).


ZPVF_SD_SalesHistory_01_TR.invoice_date = ZITR_DayAttribute_01_TR.Date_
    -- Brief: aligns actual transaction date to calendar attributes in ZITR_DayAttribute_01_TR (WEEK, YMONTH, QTR, FIN_YEAR, CAL_YEAR, REP_MONTH).


ZIVF_SD_Sales_Plan_Target_01_TR.TERR_CODE = ZIVF_TerritoryMaster_01_TR.BZIRK
    -- Brief: links sales target / plan to territory master in ZIVF_TerritoryMaster_01_TR (NAME).


ZIVF_SD_Sales_Plan_Target_01_TR.MATNR = ZIVR_SKUMaster_01_TR.MATNR
    -- Brief: links sales target / plan to SKU master in ZIVR_SKUMaster_01_TR (MAKTX, PROD_TITLE, BRAND).


ZIVF_SD_Sales_Plan_Target_01_TR.CAT_CODE = ZIVF_CategoryMaster_01_TR.CAT_CODE
    -- Brief: links sales target / plan to category master in ZIVF_CategoryMaster_01_TR (CAT_DESC).


ZIVF_SD_Sales_Plan_Target_01_TR.REG_CODE = ZIVF_RegionMaster_MD_01.region_code
    -- Brief: links sales target / plan to region master in ZIVF_RegionMaster_MD_01 (region_name).


Never display IDs or codes. Always return descriptive names from the master views.


GENERAL RULES:
- Always use invoice_value for revenue calculations.
- Always use Tyre_Quantity (or volume) for quantity calculations.
- Always use invoice_date for all date filtering (already a DATE column — no parsing needed).
- Always return names from master views instead of IDs (e.g., NAME1 / BNAME from ZIVF_CustomerMaster_01_MD, MAKTX / PROD_TITLE from ZIVR_SKUMaster_01_TR, CAT_DESC from ZIVF_CategoryMaster_01_TR, NAME from ZIVF_TerritoryMaster_01_TR, region_name from ZIVF_RegionMaster_MD_01, NAME from ZIVF_ZoneMaster_01_TR).
- Use JOIN (not INNER JOIN) when resolving names, and use COALESCE() to label unmatched codes explicitly (e.g. "Unmapped Account") rather than dropping those rows.
- GROUP BY CORRECTNESS (critical): every non-aggregated column you SELECT (e.g. ZIVR_SKUMaster_01_TR.MAKTX, ZIVF_CustomerMaster_01_MD.NAME1, ZIVF_ZoneMaster_01_TR.NAME) MUST also appear in that query's GROUP BY clause. Never SELECT a descriptive name column next to SUM(...)/COUNT(...) without grouping by that same column.
- COLUMN NAMES (critical): Use ONLY the exact column names as listed in the schema:
  * Fact view: invoice_value, Tyre_Quantity, volume, invoice_date, invoice_number, dealer_code, ship_to_party, SKU, total_discount, distribution_channel, plant, BILL_TYPE, FKART.
  * Customer Master: KUNNR, NAME1, BNAME, BZIRK, REGIO, KTOKD, KDGRP, KUKLA, KLABC, CLUB_CATG, DLR_CATG, CH_TYPE, OUTSTAND, EXPOSURE, VTWEG.
  * SKU Master: MATNR, MAKTX, PROD_TITLE, CAT_CODE, CONSTUCTION, BULD_TYPE, TYRE_TYPE, TYRE_TP, TT_TYPE, BRAND, SUB_BRAND, RIM_SIZE, ZSIZE_CD, MATKL, CATG, DB_CATG.
  * Category Master: CAT_CODE, CAT_DESC, SHORT_DESC.
  * Territory Master: BZIRK, NAME, REGIO, P_ZONE, AREA, WERKS.
  * Region Master: region_code, region_name, zone_code, person_responsible.
  * Zone Master: P_ZONE, NAME, HR_ZONE.
  * Day Attribute: Date_, WEEK, YMONTH, QTR, FIN_YEAR, CAL_YEAR, REP_MONTH, REP_MN_NM.
  * Sales Plan Target: TERR_CODE, YMONTH, MATNR, T_TYPE, MAT_TYPE, ORG_PLAN, REV_PLAN, OLD_PLAN, AREA_PLAN, REG_PLAN, DELAER_FORE, HO_PLAN, AREA_CODE, CLASS, INS_DT, INS_TM, UNAME, CAT_CODE, REV_PLAN_VAL, PLAN_VALUE, CAT_VALUE, AREA_SEQ, LYSM_SALE, PM_SALE, AVG_SALE, PM_SALE_1, AVG_SALE_VAL, PLAN_HO, PLAN_HO_VAL, REG_CODE, COMP_CODE, AVG_FINAL, STAR_PLAN, POWER_SKU, ECO_TYPE, PREM_STATUS, INCH, DEALER_CNT, SAS_VAL, OUTSTD, EXPOSURE.
- STRICT LIMITS: Every query MUST include an ORDER BY clause (usually on revenue or quantity) and a LIMIT 5 or LIMIT 10 to prevent overloading the context window.
- PERCENTAGE CONTEXT: Where possible, include a percentage calculation (e.g. (Revenue / Total_Revenue) * 100) so the final report knows how significant a trend is relative to the whole business.
- Generate optimized MySQL 8+ queries.
- Write 6 to 10 queries total (to cover all the required specific angles above).


Respond ONLY with a valid JSON array of strings containing the SQL queries."""


    user_prompt = f"Schema:\n{schema_str}\nGenerate the JSON array of queries."
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    insights = []
    try:
        content_str = call_llm_chat(messages, json_mode=True, temperature=0.1)
        queries = json.loads(content_str)
        if not isinstance(queries, list):
            return insights


        BLOCKED_KEYWORDS = ("into outfile", "into dumpfile", "load_file", "load data")


        for i, query in enumerate(queries, 1):
            query = query.strip().rstrip(";")
            q_lower = query.lower()


            if not q_lower.startswith("select"):
                continue
            if ";" in query:                          # reject any embedded multi-statement
                print(f"[Agentic Helper] Rejected multi-statement query: {query}")
                continue
            if any(kw in q_lower for kw in BLOCKED_KEYWORDS):
                print(f"[Agentic Helper] Rejected unsafe query: {query}")
                continue


            try:
                cursor.execute(query)
                rows = cursor.fetchall()[:MAX_ROWS]   # cap rows fed into context
                if rows:
                    insights.append(f"--- Insight Query {i} ---")
                    insights.append(f"Query: {query}")
                    for row in rows:
                        row_str = " | ".join(
                            f"{k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}"
                            for k, v in row.items()
                        )
                        insights.append(f"  {row_str}")
            except Exception as e:
                print(f"[Agentic Helper] Query failed: {query}. Error: {e}")
                
    except Exception as e:
        print(f"[Agentic Helper] LLM call failed: {e}")
        
    return insights




def _fetch_db_data(session_id: str, databases: list, conn) -> list:
    """
    Fetch DB data for analysis — supports MySQL and PostgreSQL.
    For PostgreSQL:
      - schema provided → only that schema's tables
      - no schema → all non-system schemas (public + custom)
    """
    results = []


    # ── 1. MySQL / MSSQL (sync log approach) ──
    cursor = None
    db_map = {}
    try:
        cursor = conn.cursor(dictionary=True)
        if databases:
            placeholders = ",".join(["%s"] * len(databases))
            cursor.execute(f"""
                SELECT DISTINCT external_database, new_user_db, table_name
                FROM external_db_sync_log
                WHERE `session_id` = %s
                  AND external_database IN ({placeholders})
                  AND new_user_db IS NOT NULL
                  AND new_user_db != ''
            """, [session_id] + databases)
            rows = cursor.fetchall()
            for r in rows:
                new_user_db = r["new_user_db"]
                if new_user_db not in db_map:
                    db_map[new_user_db] = {"external_databases": [], "tables": []}
                ext_db = r["external_database"]
                if ext_db not in db_map[new_user_db]["external_databases"]:
                    db_map[new_user_db]["external_databases"].append(ext_db)
                if r.get("table_name"):
                    db_map[new_user_db]["tables"].append(r["table_name"])
            print(f"[Analysis] MySQL DB MAP -> {db_map}")
    except Exception as e:
        print(f"[Analysis] db_map error: {e}")
    finally:
        if cursor: cursor.close()


    for new_db, db_info in db_map.items():
        allowed_tables = db_info["tables"]
        ext_dbs_str = ", ".join(db_info["external_databases"])


        db_result = {
            "source_type":       "database",
            "external_database": ext_dbs_str,
            "new_user_db":       new_db,
            "tables":            []
        }


        ext_conn = ext_cur = None
        try:
            print(f"[Analysis] Connecting MySQL -> {new_db}")
            ext_conn = mysql.connector.connect(
                host=MYSQL_CONFIG["host"],
                port=MYSQL_CONFIG["port"],
                user=MYSQL_CONFIG["user"],
                password=MYSQL_CONFIG["password"],
                database=new_db,
                connection_timeout=10
            )
            ext_cur = ext_conn.cursor(dictionary=True)
            
            # Filter tables: only use those explicitly synced for this database
            if allowed_tables:
                tables = allowed_tables
            else:
                ext_cur.execute("SHOW TABLES")
                tables = [list(r.values())[0] for r in ext_cur.fetchall()]
                
            print(f"[Analysis] Tables in {new_db} -> {tables}")


            for t in tables:
                try:
                    ext_cur.execute(f"SELECT * FROM `{t}` LIMIT 0")
                    ext_cur.fetchall()
                    cols = [desc[0] for desc in ext_cur.description]
                    
                    db_result["tables"].append({
                        "table_name":   t,
                        "columns":      cols,
                        "business_aggregates": get_table_aggregates(ext_cur, t)
                    })
                except Exception as table_error:
                    print(f"[Analysis] MySQL table {t} error -> {table_error}")
            
            # [NEW] Deterministic core KPIs — computed first, always the same
            db_result["core_kpis"] = _compute_core_kpis(ext_cur)


            # Supplementary/exploratory LLM-generated insights
            db_result["executed_insights"] = _fetch_agentic_insights(ext_cur, db_result["tables"], "mysql")


        except Exception as db_error:
            print(f"[Analysis] MySQL connect {new_db} error -> {db_error}")
        finally:
            if ext_cur:
                try: ext_cur.close()
                except: pass
            if ext_conn:
                try: ext_conn.close()
                except: pass


        results.append(db_result)


    # ── 2. PostgreSQL (from database_credential table) ──
    if not PSYCOPG2_AVAILABLE:
        return results


    pg_cursor = None
    pg_cred_rows = []
    try:
        pg_cursor = conn.cursor(dictionary=True)
        pg_cursor.execute("""
            SELECT credential, db_type
            FROM database_credential
            WHERE session_id = %s AND db_type IN ('postgresql', 'postgres')
            ORDER BY connection_id DESC
        """, (session_id,))
        pg_cred_rows = pg_cursor.fetchall()
    except Exception as e:
        print(f"[Analysis] PG credential fetch error: {e}")
    finally:
        if pg_cursor: pg_cursor.close()


    seen_pg = set()
    for cred_row in pg_cred_rows:
        try:
            cred = cred_row["credential"]
            if isinstance(cred, str):
                cred = json.loads(cred)


            pg_host     = cred.get("host", "localhost")
            pg_port     = int(cred.get("port", 5432))
            pg_user     = cred.get("username", "")
            pg_password = cred.get("password", "")
            pg_database = cred.get("database", "")
            pg_schema   = cred.get("schema")  # None/empty → all schemas


            dedup_key = f"{pg_host}:{pg_port}/{pg_database}/{pg_schema or '__all__'}"
            if dedup_key in seen_pg:
                continue
            seen_pg.add(dedup_key)
            


            # Filter: only process if this DB was requested (or no filter given)
            if databases and pg_database not in databases:
                print(f"[Analysis] PG {pg_database} not in requested list — skipping")
                continue


            print(f"[Analysis] Connecting PostgreSQL: {pg_host}:{pg_port}/{pg_database} schema={pg_schema or 'ALL'}")


            pg_conn = None
            pg_cur = None
            try:
                pg_conn = psycopg2.connect(
                    host=pg_host, port=pg_port,
                    user=pg_user, password=pg_password,
                    dbname=pg_database,
                    connect_timeout=10
                )
                pg_cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


                db_result = {
                    "source_type":       "database",
                    "external_database": pg_database,
                    "new_user_db":       pg_database,
                    "tables":            []
                }


                # Determine schemas
                if pg_schema and pg_schema.strip():
                    schemas_to_fetch = [pg_schema.strip()]
                else:
                    pg_cur.execute("""
                        SELECT schema_name
                        FROM information_schema.schemata
                        WHERE schema_name NOT IN ('pg_catalog', 'information_schema',
                                                  'pg_toast', 'pg_temp_1', 'pg_toast_temp_1')
                          AND schema_name NOT LIKE 'pg_temp_%'
                          AND schema_name NOT LIKE 'pg_toast_temp_%'
                        ORDER BY schema_name
                    """)
                    schemas_to_fetch = [r["schema_name"] for r in pg_cur.fetchall()]
                    print(f"[Analysis] PG schemas: {schemas_to_fetch}")


                for schema in schemas_to_fetch:
                    pg_cur.execute("""
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = %s AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                    """, (schema,))
                    tables = [r["table_name"] for r in pg_cur.fetchall()]
                    print(f"[Analysis] PG schema '{schema}' tables: {tables}")


                    for t in tables:
                        qualified = f"{schema}.{t}"
                        try:
                            pg_cur.execute(f'SELECT * FROM "{schema}"."{t}" LIMIT 0')
                            pg_cur.fetchall()
                            cols = [desc.name for desc in pg_cur.description]


                            db_result["tables"].append({
                                "table_name":   qualified,
                                "columns":      cols,
                                "business_aggregates": get_table_aggregates(pg_cur, qualified)
                            })
                            print(f"[Analysis] PG {pg_database}.{qualified}: schema loaded")


                        except Exception as te:
                            print(f"[Analysis] PG skip {qualified}: {te}")
                            pg_conn.rollback()


                # NOTE: _compute_core_kpis above is written in MySQL dialect
                # (DATE_FORMAT, CAST(...AS UNSIGNED), MONTHNAME, backtick-quoted
                # tables). It is NOT wired in here for PostgreSQL — doing so
                # would need TO_CHAR/::integer equivalents first. Left as
                # LLM-generated insights only for the PG path for now.
                db_result["executed_insights"] = _fetch_agentic_insights(pg_cur, db_result["tables"], "postgresql")


                results.append(db_result)


            except Exception as e:
                print(f"[Analysis] PostgreSQL connect error: {e}")
            finally:
                if pg_cur:
                    try: pg_cur.close()
                    except: pass
                if pg_conn:
                    try: pg_conn.close()
                    except: pass
        except Exception as outer_e:
            print(f"[Analysis] Outer PostgreSQL block error: {outer_e}")

    # ── 3. SAP HANA (Stored Metadata & Trigger K-Graph Build) ──
    hana_cursor = None
    try:
        hana_cursor = conn.cursor(dictionary=True)
        hana_cursor.execute("""
            SELECT DISTINCT connection_id, schema_name, view_name, row_count, column_count, columns_detail
            FROM sap_hana_view_metadata
            WHERE session_id = %s
        """, (session_id,))
        hana_rows = hana_cursor.fetchall()
        if hana_rows:
            # Trigger HANA K-Graph build synchronously from stored metadata only
            from database.hana_kgraph_service import build_hana_kgraph_from_metadata, get_hana_kgraph_status
            unique_cids = list({r["connection_id"] for r in hana_rows if r.get("connection_id")})
            last_ontology_analysis = None
            last_hana_kgraph = None
            for cid in unique_cids:
                status_rows = get_hana_kgraph_status(session_id=session_id, connection_id=cid, include_graph=True)
                status_info = status_rows[0] if status_rows else None
                cur_status = status_info.get("status") if status_info else None
                if cur_status != "ready":
                    print(f"[Analysis] Building HANA K-Graph on Continue to Process for connection {cid} from stored metadata...")
                    build_res = build_hana_kgraph_from_metadata(cid, session_id)
                    if build_res and isinstance(build_res, dict):
                        if build_res.get("ontology_analysis"):
                            last_ontology_analysis = build_res["ontology_analysis"]
                        if build_res.get("graph"):
                            last_hana_kgraph = build_res["graph"]
                elif status_info and status_info.get("graph") and isinstance(status_info["graph"], dict):
                    last_hana_kgraph = status_info["graph"]
                    last_ontology_analysis = status_info["graph"].get("ontology_analysis")

            hana_tables = []
            for hr in hana_rows:
                cols = []
                cols_raw = hr.get("columns_detail")
                if isinstance(cols_raw, str):
                    try:
                        cols_json = json.loads(cols_raw)
                        cols = [c["name"] for c in cols_json if "name" in c]
                    except Exception:
                        pass
                elif isinstance(cols_raw, list):
                    cols = [c["name"] for c in cols_raw if "name" in c]

                view_full_name = f"{hr.get('schema_name', '')}.{hr['view_name']}" if hr.get('schema_name') else hr['view_name']
                hana_tables.append({
                    "table_name": view_full_name,
                    "columns": cols,
                    "business_aggregates": [f"Total Rows: {int(hr.get('row_count') or 0):,}"]
                })

            results.append({
                "source_type": "database",
                "external_database": "SAP HANA",
                "new_user_db": "sap_hana_metadata",
                "tables": hana_tables,
                "ontology_analysis": last_ontology_analysis,
                "hana_kgraph": last_hana_kgraph
            })
            print(f"[Analysis] SAP HANA metadata added to analysis context: {len(hana_tables)} views.")
    except Exception as hana_err:
        print(f"[Analysis] SAP HANA data fetch/build error: {hana_err}")
    finally:
        if hana_cursor:
            try: hana_cursor.close()
            except Exception: pass

    return results




# ══════════════════════════════════════════════════════
# CONTEXT BUILDER
# ══════════════════════════════════════════════════════


def _build_context(web_data: list, db_data: list) -> str:
    parts = []


    # [NEW] Verified KPI block goes first — deterministic, never truncated
    # away, and explicitly labeled so the report LLM knows to copy these
    # numbers rather than recompute or estimate them.
    for d in db_data:
        if d.get("core_kpis"):
            kpi_lines = ["=== VERIFIED KPI BLOCK (exact, pre-computed — copy these numbers, do not recompute) ==="]
            for name, rows in d["core_kpis"].items():
                if rows:
                    kpi_lines.append(f"{name}: {rows}")
                else:
                    kpi_lines.append(f"{name}: no data available")
            parts.append("\n".join(kpi_lines))

        oa = d.get("ontology_analysis")
        if oa:
            oa_lines = ["=== ROUTE-TO-MARKET (RtM) GOLDEN ONTOLOGY BENCHMARK & GAP ANALYSIS ==="]
            oa_lines.append(f"Completeness Score: {oa.get('completeness_score', 0)}% (against 12-Node Core RtM Ontology)")
            oa_lines.append(f"Matched Nodes ({oa.get('matched_nodes_count', 0)}/{oa.get('total_ontology_nodes', 12)}):")
            for mv in oa.get("matched_vertices", []):
                oa_lines.append(f"  - {mv['name']} ({mv['type']}): Mapped tables -> {', '.join(mv.get('matched_imported_tables', []))}")
            
            ai_an = oa.get("ai_analysis") or {}
            mna_map = {m.get("node_name"): m for m in ai_an.get("missing_nodes_analysis", [])}

            oa_lines.append(f"Missing Nodes ({oa.get('missing_nodes_count', 0)}/{oa.get('total_ontology_nodes', 12)}):")
            for mis in oa.get("missing_vertices", []):
                m_info = mna_map.get(mis['name']) or mna_map.get(mis.get('ontology_id')) or {}
                crit = m_info.get("criticality") or "Medium"
                sug = m_info.get("suggested_data_sources") or ""
                sug_str = f" (Suggested Data: {sug})" if sug else ""
                oa_lines.append(f"  - {mis['name']} ({mis['type']}) [Criticality: {crit}]: {mis.get('definition', '')}{sug_str}")

            if oa.get("missing_relationships"):
                oa_lines.append(f"Missing Relationships ({oa.get('missing_relationships_count', len(oa['missing_relationships']))} unmapped join paths):")
                for rel in oa["missing_relationships"]:
                    oa_lines.append(f"  - {rel.get('from')} -> {rel.get('label')} -> {rel.get('to')} (Reason: {rel.get('reason', 'Missing required nodes')})")

            if ai_an.get("overall_assessment"):
                oa_lines.append(f"Overall Assessment: {ai_an['overall_assessment']}")
            if ai_an.get("operational_blind_spots"):
                oa_lines.append("Operational Blind Spots:")
                for obs in ai_an["operational_blind_spots"]:
                    oa_lines.append(f"  - {obs}")
            if ai_an.get("actionable_recommendations"):
                oa_lines.append("Actionable Recommendations:")
                for rec in ai_an["actionable_recommendations"]:
                    oa_lines.append(f"  - {rec}")
            parts.append("\n".join(oa_lines))


    for w in web_data:
        lines = [f"=== WEB TOPIC: {w['topic']} ({w['result_count']} results) ==="]
        for item in w["items"]:
            lines.append(f"  Title : {item['title']}")
            lines.append(f"  URL   : {item['url']}")
            if item.get("brief"):
                lines.append(f"  Brief : {item['brief'][:300]}")
        parts.append("\n".join(lines))


    for d in db_data:
        lines = [f"=== DATABASE: {d['external_database']} (stored as: {d['new_user_db']}) ==="]
        for tbl in d["tables"]:
            lines.append(f"\n  Table: {tbl['table_name']}")
            
            if tbl.get("business_aggregates"):
                lines.append("  [CRITICAL ACTUAL BUSINESS TOTALS (ENTIRE TABLE)]:")
                for agg in tbl["business_aggregates"]:
                    lines.append(f"    - {agg}")


            lines.append(f"  Columns: {', '.join(tbl['columns'])}")
                
        if d.get("executed_insights"):
            lines.append("\n=== SUPPLEMENTARY EXECUTED INSIGHTS (exploratory only — do not use these for core KPIs already in the VERIFIED KPI BLOCK) ===")
            lines.extend(d["executed_insights"])
            
        parts.append("\n".join(lines))


    ctx = "\n\n".join(parts)
    if len(ctx) > MAX_CTX_CHARS:
        ctx = ctx[:MAX_CTX_CHARS] + "\n\n[... truncated ...]"
    return ctx




# ══════════════════════════════════════════════════════
# MISTRAL — REPORT GENERATION
# ══════════════════════════════════════════════════════


def _call_mistral(context: str, topics: list, databases: list) -> dict:
    source_desc = []
    if topics:    source_desc.append(f"web topics: {', '.join(topics)}")
    if databases: source_desc.append(f"databases: {', '.join(databases)}")


    system = """You are an expert business analyst and strategist.
Your task is to analyze the provided sales data of different types of tyres, tubes, Ret read Belt, Vul Solutions, flap  and extract purely business-focused insights and context.
CRITICAL INSTRUCTIONS:
1. Do NOT include ANY technical details (e.g., table names, column names, row counts, distinct values, data types, schema info, missing values, database structure).
2. STRICT ANTI-HALLUCINATION RULE: Use ONLY actual numbers and facts explicitly provided in the context. If you see a specific entity name in the sample rows, DO NOT invent transaction counts or revenue for them unless those specific numbers are explicitly written next to their name in the CRITICAL CROSS-TABLE INSIGHTS or Aggregates. If you don't have the exact number, state the trend generally or omit the number.
3. Pay SPECIAL ATTENTION to the "CRITICAL EXECUTED INSIGHTS" block. This contains the exact mathematical results of dynamic SQL queries executed directly against the database. Use ONLY these results to form the quantitative basis of your summary.
4. Identify the key business trends, top performers, and overall performance metrics explicitly found in the executed insights or table totals.
5. The report must dynamically adapt to the executed queries and focus purely on actionable business insights, performance, and trends.
6. Use Descriptive Names: ALWAYS map and use descriptive names instead of raw IDs or codes (e.g., use CAT_DESC instead of CAT_CODE, customer NAME1 / BNAME instead of KUNNR, region_name instead of region_code, territory NAME instead of BZIRK, zone NAME instead of P_ZONE, SKU MAKTX / PROD_TITLE instead of MATNR). Raw IDs make the report difficult to read for business users.
7. Date Formatting: The 'Month' column or any period formatted as YYYYMM (e.g., 202601, 202512) must be translated into readable month names (e.g., 'January 2026', 'December 2025') in your report.
8. Respond ONLY in valid JSON with a single key: "report".
9. If a "VERIFIED KPI BLOCK" appears in the data, those numbers are pre-computed and exact — copy them into the report verbatim. Do NOT recompute, re-derive, estimate, or override them using anything from the "SUPPLEMENTARY EXECUTED INSIGHTS" section. The supplementary insights are for color/context only (e.g. the Executive Summary, Business Risks, Actionable Recommendations) — never for the numeric fields already present in the VERIFIED KPI BLOCK.
10. In the VERIFIED KPI BLOCK, map these field names directly to report labels: claims_amount → "Claims", returns_amount → "Returns", discount_percentage → "Discount %", fleet_contribution_pct → "Fleet Contribution", oem_contribution_pct → "OEM Contribution", target_performance → "Target vs Actual Performance" (Total Target Value, Overall Achievement %, Target Shortfall), target_vs_actual_quantity → "Quantity Achievement %", territory_target_shortfall → "Territory Target Shortfall".
11. UNIT RULE (critical): The monetary values in the VERIFIED KPI BLOCK are ALREADY IN CRORES. You MUST append " Cr" to them. Display them EXACTLY as given, formatted with a ₹ symbol and " Cr" (e.g. if the value is 995.04, write "₹995.04 Cr"). Do NOT divide or multiply them further.
"""


    user = f"""
Analyze the strictly provided business data ({'; '.join(source_desc)}):


{context}


Generate a detailed, purely business-focused summary highlighting key insights. 
ALSO, generate exactly 5 "What" critical questions about the data.
ALSO, generate a category-wise trend report as a "line_chart" visualization extracting numeric/categorical trend values.


Return ONLY this JSON:
{{
  "report": "TITLE: <Create a descriptive business-focused title based on the data>\\n\\n### Executive Summary\\n<Write exactly 5 to 8 lines summarizing overall business performance, key trends, and main takeaways based purely on the data.>\\n\\n### Key Business Insights\\n\\n**1. Overall Performance**\\n- **Total Revenue**: <Value>\\n- **Total Quantity**: <Value>\\n- **Total Invoices**: <Value>\\n- **Average Invoice Value**: <Value>\\n\\n**2. Revenue Trend**\\n- **Highest Sales Month**: <Value>\\n- **Lowest Sales Month**: <Value>\\n- **Monthly Growth**: <Value>\\n\\n**3. Product Performance**\\n- **Top Category**: <Value>\\n- **Top Construction**: <Value>\\n- **Top Tyre Type**: <Value>\\n- **Top Product**: <Value>\\n- **Lowest Selling Product**: <Value>\\n\\n**4. Customer Performance**\\n- **Top Customers**: <Value>\\n- **Dealer Contribution**: <Value>\\n- **Fleet Contribution**: <Value>\\n- **OEM Contribution**: <Value>\\n- **Customer Concentration**: <Value>\\n\\n**5. Geography**\\n- **Top Zone**: <Value>\\n- **Top Region**: <Value>\\n- **Top Territory**: <Value>\\n- **Lowest Territory**: <Value>\\n\\n**6. Distribution**\\n- **Replacement**: <Value>\\n- **OEM**: <Value>\\n- **STU**: <Value>\\n- **DEF**: <Value>\\n- **Contribution %**: <Value>\\n\\n**7. Target Performance**\\n- **Total Target Value**: <Value>\\n- **Overall Achievement %**: <Value>\\n- **Target Shortfall**: <Value>\\n- **Quantity Achievement %**: <Value>\\n- **Top Target Shortfall Territory**: <Value>\\n\\n**8. Route-to-Market (RtM) Ontology Benchmark**\\n- **Ontology Completeness**: <Completeness Score from RtM Ontology Gap Analysis or N/A>\\n- **Mapped Enterprise Dimensions**: <Summary of matched entities, e.g. Coverage Plan (TCP/TCR), Fulfillment Orders (SupplyOrders)>\\n- **Missing Nodes**: <List all missing nodes with their criticality, e.g., 10 unmapped entities: FieldForceRep (High), GeographyNode/Territory (High), Distributor/Wholesaler (High), CustomerOutlet (High), Product/SKU (High), AssortmentPlan (Medium), CallVisit (Medium), RetailAudit (Medium), SuggestedOrder (Medium), TradeInvestment (Medium)>\\n- **Missing Relationships**: <List key unmapped join relationships from analysis, e.g., 17 unmapped join relationships including FieldForceRep -> covers -> GeographyNode, FieldForceRep -> executes -> CallVisit, CoveragePlan -> schedules -> CallVisit, Distributor -> supplies -> CustomerOutlet, CallVisit -> visits -> CustomerOutlet, etc.>\\n- **Coverage Gaps in Route-to-Market Architecture**: <Key strategic coverage gaps and missing capabilities>\\n- **Operational Blind Spots**: <Key blind spots from ontology analysis>\\n\\n### Business Risks\\n- <Data-driven Risk 1 (e.g. High customer concentration)>\\n- <Data-driven Risk 2 (e.g. High discount dependency)>\\n- <Data-driven Risk 3>\\n\\n### Actionable Recommendations\\n- <Data-driven Recommendation 1 (e.g. Improve sales in low-performing territories)>\\n- <Data-driven Recommendation 2 (e.g. Reduce dependency on top customers)>\\n- <Data-driven Recommendation 3>\\n\\n### Strategic Conclusion\\n<A final strategic conclusion summarizing the path forward for the business.>",
  "follow_up_questions": ["What ...?", "What ...?", "What ...?", "What ...?", "What ...?"],
  "visualizations": [
    {{
      "type": "line_chart",
      "title": "Category-wise Trend Report",
      "xKey": "category",
      "yKey": "value",
      "data": [
        {{"category": "A", "value": 100}},
        {{"category": "B", "value": 200}}
      ]
    }}
  ]
}}


RULES:
- Replace all <...> with REAL business insights and metrics from the actual data provided.
- DO NOT mention tables, rows, columns, data types, nulls, or database schema. Keep it 100% business-focused.
- If Route-to-Market (RtM) Golden Ontology Benchmark appears in the provided context, accurately reflect the completeness score, mapped dimensions, missing entities, and blind spots in section 8.
- In Section 8 (Route-to-Market (RtM) Ontology Benchmark), you MUST explicitly include both '- **Missing Nodes**:' (listing the unmapped nodes with criticality) and '- **Missing Relationships**:' (listing the unmapped join relationships) as separate text bullets, exactly like the other bullets.
- NEVER mention the word 'template', 'golden copy', or 'sample' anywhere in the report. Present missing entities purely as 'Unmapped Strategic Business Dimensions' or 'Coverage Gaps in Route-to-Market Architecture'.
- If a specific metric (e.g., Target Performance, OEM Contribution, Highest/Lowest Sales Month, YTD) is not explicitly calculated and available in the provided data, write "N/A based on available data" rather than hallucinating or guessing based on overall date ranges. Keep the bullet structure intact.
- Use \\n for newlines inside the JSON string.
- Every point must reference a specific value, name, or number from the actual data. DO NOT INVENT NUMBERS for entities just to fulfill this rule.
- Do NOT use generic filler sentences.
"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user}
    ]
    try:
        content_str = call_llm_chat(messages, json_mode=True, temperature=0.2)
        return json.loads(content_str)
    except Exception as e:
        print(f"[LLM] session analysis error: {e}")
        return None




# ══════════════════════════════════════════════════════
# MAIN CONTROLLER  —  POST /session-analysis
# ══════════════════════════════════════════════════════


def session_analysis_controller(get_connection_func):
    data       = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    topics     = [t.strip() for t in (data.get("topics")    or []) if str(t).strip()]
    databases  = [d.strip() for d in (data.get("databases") or []) if str(d).strip()]


    if not session_id:
        return jsonify({
            "status": "failed", "statusCode": 400,
            "message": "Field 'session_id' is required."
        }), 400


    if not topics and not databases:
        # Check if this session has SAP HANA metadata before rejecting
        has_hana = False
        try:
            chk_conn = get_connection_func()
            with chk_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM sap_hana_view_metadata WHERE session_id = %s LIMIT 1", (session_id,))
                if cur.fetchone():
                    has_hana = True
                    databases = ["SAP HANA"]
            chk_conn.close()
        except Exception:
            pass

        if not has_hana:
            return jsonify({
                "status": "failed", "statusCode": 400,
                "message": "At least one of 'topics' or 'databases' must be provided."
            }), 400


    conn = None
    try:
        conn = get_connection_func()
        
        target_arango_db = None
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT workspace_arango_db FROM workspaces WHERE session_id = %s", (session_id,))
            row = cursor.fetchone()
            if row and row.get("workspace_arango_db"):
                target_arango_db = row["workspace_arango_db"]
            cursor.close()
        except Exception as e:
            print(f"[Analysis] DB fetch target_arango_db error: {e}")


        # 1. Fetch raw data
        web_data = _fetch_web_data(session_id, topics, conn)
        db_data  = _fetch_db_data(session_id, databases, conn)


        has_web = bool(web_data)
        has_db = any(len(d.get("tables", [])) > 0 for d in db_data)


        if not has_web and not has_db:
            return jsonify({
                "status":     "no_data",
                "statusCode": 200,
                "message":    "আপনার ডাটাবেসে কোনো টেবিল বা ডেটা নেই, দয়া করে আগে ডেটা আপলোড করুন।"
            }), 200




        # 2. Build context + hash
        context   = _build_context(web_data, db_data)
        data_hash = _hash_context(context)


        # 3. Raw summary (always returned in response)
        raw_summary = {
            "web_sources": web_data,
            "database_sources": [
                {
                    "external_database": d["external_database"],
                    "new_user_db":       d["new_user_db"],
                    "tables": [
                        {
                            "table_name":  t["table_name"],
                            "columns":     t["columns"]
                        }
                        for t in d["tables"]
                    ]
                }
                for d in db_data
            ]
        }
        import json
        raw_summary = json.loads(json.dumps(raw_summary, default=str))


        # 4. Check cache
        cached = _load_cache(session_id, data_hash, conn)
        ontology_analysis = next((d.get("ontology_analysis") for d in db_data if d.get("ontology_analysis")), None)

        if cached:
            # Cache HIT — return immediately, no LLM call
            return jsonify({
                "status":     "success",
                "statusCode": 200,
                "report":     cached["report"],
                "graph_url":  cached["graph_url"],
                "ontology_analysis": ontology_analysis,
                "vector_db":  {"status": "cached", "message": "Vectors already indexed for this session"},
            }), 200


        # 5. Cache MISS or STALE — generate fresh
        analysis  = _call_mistral(context, topics, databases)
        graph_url = generate_session_graph(session_id, web_data, db_data, target_arango_db)


        if not analysis:
            log_system_error(session_id, "session_analysis", "LLM analysis failed or returned empty result", error_type="analysis_llm_error")
            return jsonify({
                "status":     "partial",
                "statusCode": 200,
                "message":    "LLM analysis failed.",
                "graph_url":  graph_url,
                "ontology_analysis": ontology_analysis,
            }), 200


        report = analysis.get("report", "")
        # Guard: LLM may return report as a dict instead of string — serialize it
        if isinstance(report, dict):
            report = json.dumps(report, ensure_ascii=False)
        elif not isinstance(report, str):
            report = str(report)
        follow_up_questions = analysis.get("follow_up_questions", [])
        visualizations = analysis.get("visualizations", [])


        # 6. Save to cache
        _save_cache(session_id, data_hash, report, graph_url, topics, databases, conn)

        # 7. Step 4: Store in Vector DB (ChromaDB)
        vector_res = None
        try:
            from controllers.vector_store import push_session_analysis_and_kgraph_to_vector_db
            hana_kgraph = next((d.get("hana_kgraph") for d in db_data if d.get("hana_kgraph")), None)
            vector_res = push_session_analysis_and_kgraph_to_vector_db(
                session_id=session_id,
                report=report,
                kgraph=hana_kgraph,
                db_data=db_data,
                web_data=web_data,
                ontology_analysis=ontology_analysis,
                conn=conn
            )
            print(f"[Analysis] Vector DB push result: {vector_res}")
        except Exception as v_err:
            print(f"[Analysis] Vector DB push error (non-fatal): {v_err}")


        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "report":     report,
            "graph_url":  graph_url,
            "ontology_analysis": ontology_analysis,
            "vector_db":  vector_res,
            "follow_up_questions": follow_up_questions,
            "visualizations": visualizations
        }), 200


    except Exception as e:
        log_system_error(session_id, "session_analysis", str(e), error_type="analysis_error")
        return jsonify({
            "status": "error", "statusCode": 500,
            "message": str(e)
        }), 500
    finally:
        if conn: conn.close()