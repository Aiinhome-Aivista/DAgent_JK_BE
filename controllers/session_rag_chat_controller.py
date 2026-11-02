
# pyrefly: ignore [missing-import]
# controllers/session_rag_chat_controller.py
# ADVANCED HYBRID RAG
#
# Architecture:
#   1. Query Understanding  — extract intent, entities, table hints
#   2. Hybrid Retrieval     — BM25 keyword + semantic vector search + metadata filter
#   3. Re-ranking           — cross-encoder reranking (ms-marco-MiniLM-L-6-v2)
#   4. Answer Generation    — strict grounding, deep business analysis
#
# Optimizations applied:
#   - Global SentenceTransformer model (loaded once at startup)
#   - Global CrossEncoder model (loaded once at startup)
#   - Embedding cache for repeated queries
#   - Batch embedding for multiple queries
#   - Duplicate chunk deduplication before ranking
#   - Cross-encoder reranking top-40 → keep best 20
#   - MAX_CTX_CHARS reduced to 15000 for faster LLM
#   - Auto chat history save after every answer

# controllers/session_rag_chat_controller.py
# ADVANCED HYBRID RAG
#
# Architecture:
#   1. Query Understanding  — extract intent, entities, table hints
#   2. Hybrid Retrieval     — BM25 keyword + semantic vector search + metadata filter
#   3. Re-ranking           — cross-encoder reranking (ms-marco-MiniLM-L-6-v2)
#   4. Answer Generation    — strict grounding, deep business analysis
#
# Optimizations applied:
#   - Global SentenceTransformer model (loaded once at startup)
#   - Global CrossEncoder model (loaded once at startup)
#   - Embedding cache for repeated queries
#   - Batch embedding for multiple queries
#   - Duplicate chunk deduplication before ranking
#   - Cross-encoder reranking top-40 → keep best 20
#   - MAX_CTX_CHARS reduced to 15000 for faster LLM
#   - Auto chat history save after every answer

import re, json, time, hashlib, math, requests, mysql.connector, threading, os
from collections import defaultdict

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("[RAG] psycopg2 not installed - PostgreSQL support disabled")

# pyrefly: ignore [missing-import]
from flask import request, jsonify
from database.config import MISTRAL_API_KEY, MISTRAL_MODEL, MYSQL_CONFIG
from database.prompt_loader import get_prompt
from controllers.kgraph_service import (
    load_kgraph, build_sql_rules, resolve_grouping, detect_drilldown, validate_sql
)
from controllers.intent_router import classify_intent
from controllers.query_branches import execute_hybrid
from helper.email_action_handler import is_email_action_request, execute_email_action
# ChromaDB persistent storage — vectors survive server restarts
CHROMA_PERSIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chroma_store"
)
os.makedirs(CHROMA_PERSIST_DIR, exist_ok=True)

MISTRAL_URL        = "https://api.mistral.ai/v1/chat/completions"
MAX_ROWS           = 300
TOP_K              = 400
CACHE_TTL          = 600
MAX_CTX_CHARS      = 50000

_CACHE   = {}
_CLIENTS = {}
_LOCKS   = {}

_FOLLOWUP_CYCLE = ["What", "Where", "Why"]
_TURN_COUNTER   = {}

def _next_followup_type(session_id):
    turn = _TURN_COUNTER.get(session_id, 0)
    return _FOLLOWUP_CYCLE[turn % 3]

def _advance_turn(session_id):
    _TURN_COUNTER[session_id] = _TURN_COUNTER.get(session_id, 0) + 1

def _followup_instruction(ftype):
    instructions = {
        "What": (
            "follow_up_questions: Generate exactly 5 questions, ALL starting with 'What '. "
            "Focus on WHAT specific data, values, counts, or details exist in the business data."
        ),
        "Where": (
            "follow_up_questions: Generate exactly 5 questions, ALL starting with 'Where '. "
            "Focus on WHERE data comes from, where patterns exist, where in the database."
        ),
        "Why": (
            "follow_up_questions: Generate exactly 5 questions, ALL starting with 'Why '. "
            "Focus on WHY patterns exist, why certain data looks the way it does, business reasoning."
        )
    }
    return instructions.get(ftype, instructions["What"])

GRAPH_KW  = {"graph","chart","plot","visualize","visualise","bar","pie","line","histogram","scatter"}
REPORT_KW = {"report","summary report","generate report","make a report","create a report","write a report"}
GREET_RE  = re.compile(r'^\s*(hi+|hello+|hey+|howdy|greetings|sup|yo+|hiya|good\s*(morning|afternoon|evening|night)|what\'?s\s*up)\s*[!?.]*\s*$', re.I)

def _is_graph(q):  return bool(set(q.lower().split()) & GRAPH_KW)
def _is_report(q): return any(k in q.lower() for k in REPORT_KW)
def _is_greet(q):  return bool(GREET_RE.match(q.strip()))


# ══════════════════════════════════════════════════════
# OPTIMIZATION 1: Global models loaded once at startup
# ══════════════════════════════════════════════════════

_EMBED_MODEL   = None
_CROSS_ENCODER = None
_MODEL_LOCK    = threading.Lock()

# OPTIMIZATION 2: Embedding cache
_EMBED_CACHE   = {}


def _get_embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        with _MODEL_LOCK:
            if _EMBED_MODEL is None:
                from sentence_transformers import SentenceTransformer
                print("[RAG] Loading SentenceTransformer globally...")
                import os
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                model_path = os.path.join(base_dir, 'local_models', 'all-MiniLM-L6-v2')
                _EMBED_MODEL = SentenceTransformer(model_path)
                print("[RAG] SentenceTransformer ready")
    return _EMBED_MODEL


def _get_cross_encoder():
    global _CROSS_ENCODER
    if _CROSS_ENCODER is None:
        with _MODEL_LOCK:
            if _CROSS_ENCODER is None:
                from sentence_transformers.cross_encoder import CrossEncoder
                print("[RAG] Loading CrossEncoder globally...")
                _CROSS_ENCODER = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
                print("[RAG] CrossEncoder ready")
    return _CROSS_ENCODER


def _encode_texts(texts):
    """OPTIMIZATION 2+3: Cache-aware batch embedding."""
    model  = _get_embed_model()
    result = [None] * len(texts)
    uncached_idx   = []
    uncached_texts = []

    for i, t in enumerate(texts):
        if t in _EMBED_CACHE:
            result[i] = _EMBED_CACHE[t]
        else:
            uncached_idx.append(i)
            uncached_texts.append(t)

    if uncached_texts:
        new_embeds = model.encode(uncached_texts, batch_size=128, show_progress_bar=False).tolist()
        for idx, emb, txt in zip(uncached_idx, new_embeds, uncached_texts):
            _EMBED_CACHE[txt] = emb
            result[idx] = emb

    return result


# ══════════════════════════════════════════════════════
# BM25
# ══════════════════════════════════════════════════════

class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.N = len(docs)
        self.tokenized = [self._tok(d) for d in docs]
        self.avgdl = sum(len(t) for t in self.tokenized) / max(self.N, 1)
        self.df = defaultdict(int)
        for td in self.tokenized:
            for w in set(td): self.df[w] += 1

    def _tok(self, text):
        return re.findall(r'\b\w+\b', text.lower())

    def score(self, query, top_k):
        q_terms = self._tok(query)
        scores  = []
        for i, td in enumerate(self.tokenized):
            tf_map = defaultdict(int)
            for w in td: tf_map[w] += 1
            s = 0.0
            for term in q_terms:
                if term not in tf_map: continue
                tf  = tf_map[term]
                idf = math.log((self.N - self.df[term] + 0.5) / (self.df[term] + 0.5) + 1)
                den = tf + self.k1 * (1 - self.b + self.b * len(td) / self.avgdl)
                s  += idf * (tf * (self.k1 + 1)) / den
            if s > 0: scores.append((i, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


# ══════════════════════════════════════════════════════
# DATA LOADERS
# ══════════════════════════════════════════════════════

def _local_conn(fn):
    c = fn()
    if not c: raise RuntimeError("Local DB failed")
    return c


def _dict_cursor(conn):
    """Safely returns a dictionary cursor for both pymysql and mysql.connector."""
    if conn is None:
        return None
    try:
        return conn.cursor(dictionary=True)
    except TypeError:
        return conn.cursor()


def _load_all(session_id, get_fn):
    chunks = []
    chunks += _load_db(session_id, get_fn)
    chunks += _load_sheets(session_id, get_fn)
    chunks += _load_web(session_id, get_fn)
    chunks += _load_analysis_report(session_id, get_fn)
    chunks += _load_hana(session_id, get_fn)
    return chunks


def _get_fallback_cols_for_sap_view(view_name):
    """Authoritative column lists from SYS.VIEW_COLUMNS for SAP S/4HANA views."""
    v = (view_name or "").upper()
    if "CUSTOMERMASTER" in v:
        return [
            "KUNNR", "VTWEG", "COUNC", "KTOKD", "KDGRP", "BRAN1", "VKBUR", "BZIRK",
            "ERDAT", "LEGAL_FLAG", "STATS", "DMBTR_SAS", "EXPOSURE", "OUTSTAND",
            "UPDATED_DT", "KUKLA", "DLR_CNT", "SUR_FLG", "BNAME", "GFORM",
            "DLR_CATG", "AI_FLG", "CLM_FLG", "BAHNE", "BAHNS", "BAHNE_K", "BAHNSK",
            "L3M_NOBILL", "L2M_NOBILL", "LM_NOBILL", "ATTRATION", "CLUB_CATG",
            "CLUB_CATG_CURR", "SAS_CURR", "SALE_TOT", "SALE_AVG", "PSTLZ", "DIST_ID",
            "LONG_1", "LAND_1", "KLABC", "ACTIVE_CM", "REV_STATUS", "REGIO",
            "ACTIVE_FIN", "BILL_STATUS", "J_1IPANNO", "J_1ICSTNO", "OTR_CATG",
            "APP_SYNC", "APP_SYNC_DT", "GRP_KUNNR", "LS_BILL_DT", "FS_BILL_DT",
            "DLR_LF_YR", "DLR_LF_MN", "CH_TYPE", "BRAND_SHOP", "T_TYPE", "VWERK",
            "OPEN_SAS", "DOWN_GRD", "NAME1"
        ]
    elif "SKUMASTER" in v:
        return [
            "MATNR", "CAT_CODE", "MTART", "MATKL", "CATG", "DB_CATG", "PREM_STATUS",
            "MAIN_SKU", "NEW_STATUS", "STATUS", "RIM_SIZE", "BRAND", "SUB_BRAND",
            "TT_TYPE", "ASPECT_RAT", "LOAD_INDEX", "PLY_RATING", "PARENT_SKU",
            "ACTIVE", "POWER_SKU", "COMP_CODE", "MATL_TYPE", "PAT_CODE", "PROD_TITLE",
            "DIAMETER", "CONSTUCTION", "NOM_SAC_WIDHT", "FIR_TYPE", "CATG_STATUS",
            "UPDATED_BY", "ERDAT", "UPDATED_DT", "TYRE_TYPE", "BULD_TYPE",
            "CONST_SKU", "SO_FLAG", "MAKTX", "TYRE_TP", "ZSIZE_CD", "ZVARIANT",
            "ROLL_WEIGHT", "SET_BILL", "PROD_TITLE_RET", "PLYRATE", "TB_TYPE",
            "PLAN_FLAG", "WARRNTY_FLAG", "OTH_FLAG", "SET_FLAG", "MKTG_CATG",
            "PROF_CLASS_10", "SALE_QTY", "PROF_CLASS_20", "PROF_CLASS_50",
            "PROF_CLASS_DM", "PROF_CLASS_AL", "CO_FLH", "SP_CATG", "CAT_CODE_OLD",
            "STEN_LEN_SKU", "TYRE_MATNR", "STEUC", "NTGEW", "SKU_UNIT"
        ]
    elif "CATEGORYMASTER" in v:
        return [
            "MANDT", "CAT_CODE", "CAT_DESC", "SEQ_NO", "MATNR", "SHORT_DESC",
            "AVG_FLAG", "MATNR_VIK", "TGT_CATG", "STEN_LEN", "TTF_RATIO"
        ]
    elif "DAYATTRIBUTE" in v:
        return [
            "WEEK", "YMONTH", "QTR", "FIN_YEAR", "CAL_YEAR", "HOLD_FLG", "ZDAY_NO",
            "WRK_DAY", "ZCNT", "ZDAY_NM", "REP_MONTH", "REP_QTR", "REP_WEEK",
            "REP_MN_NM", "Date_"
        ]
    elif "TERRITORYMASTER" in v:
        return [
            "BZIRK", "NAME", "WERKS", "AREA", "REGIO", "ERNAME", "USER_ID",
            "CURR_EMPNO", "CELL", "TERR_TYPE", "EMAIL_ID", "TERR_POSITION",
            "P_USER", "SAP_ID", "PLAN_STATUS", "AREA_SEQ", "TERR_CAT",
            "ESL_LVL1_ID", "ESL_LVL2_ID", "ESL_LVL3_ID", "TERR_SCOPE",
            "SO_NAME", "SO_DESIG", "UPDATION_DT", "DLR_CNT", "STATUS", "GUID",
            "SEGM", "P_ZONE", "KUNNR", "SE_TERR", "PERNR", "PERNR_DT", "RR_PER"
        ]
    elif "ZONEMASTER" in v:
        return ["P_ZONE", "NAME", "EMAIL_ID", "CELL", "USER_ID", "EMPNO", "HR_ZONE"]
    elif "REGIONMASTER" in v:
        return [
            "region_code", "region_name", "region_employee_number", "SmtpAddr",
            "person_responsible", "zone_code", "region_user_id", "mobile_number", "email_id"
        ]
    elif "CURRENTSALES" in v:
        return [
            "distribution_channel", "company_code", "invoice_type", "plant",
            "invoice_number", "invoice_date", "indicator_cancel", "dealer_code",
            "ship_to_party", "SKU", "batch", "volume", "ndp_value", "old_code",
            "total_discount", "gst", "invoice_value", "taxable_value", "FKART",
            "BILL_TYPE", "ACTIVITY", "Tyre_Quantity"
        ]
    elif "SALESHISTORY" in v or "SALES_HISTORY" in v:
        return [
            "distribution_channel", "company_code", "invoice_type", "plant",
            "invoice_number", "invoice_date", "indicator_cancel", "dealer_code",
            "ship_to_party", "SKU", "batch", "volume", "ndp_value", "old_code",
            "total_discount", "FKART", "gst", "BILL_TYPE", "invoice_value",
            "ACTIVITY", "taxable_value", "Tyre_Quantity"
        ]
    elif "CANCELLEDSALESINVOICE" in v:
        return [
            "distribution_channel", "company_code", "invoice_type", "plant",
            "invoice_number", "invoice_date", "indicator_cancel", "dealer_code",
            "ship_to_party", "SKU", "batch", "volume", "ndp_value", "old_code",
            "total_discount", "gst", "invoice_value", "taxable_value", "FKART",
            "BILL_TYPE", "ACTIVITY", "Tyre_Quantity"
        ]
    elif "SALES_PLAN_TARGET" in v or "PLAN_TARGET" in v:
        return [
            "TERR_CODE", "YMONTH", "MATNR", "T_TYPE", "MAT_TYPE", "ORG_PLAN",
            "REV_PLAN", "OLD_PLAN", "AREA_PLAN", "REG_PLAN", "DELAER_FORE",
            "HO_PLAN", "AREA_CODE", "CLASS", "INS_DT", "INS_TM", "UNAME",
            "CAT_CODE", "REV_PLAN_VAL", "PLAN_VALUE", "CAT_VALUE", "AREA_SEQ",
            "LYSM_SALE", "PM_SALE", "AVG_SALE", "PM_SALE_1", "AVG_SALE_VAL",
            "PLAN_HO", "PLAN_HO_VAL", "REG_CODE", "COMP_CODE", "AVG_FINAL",
            "STAR_PLAN", "POWER_SKU", "ECO_TYPE", "PREM_STATUS", "INCH",
            "DEALER_CNT", "SAS_VAL", "OUTSTD", "EXPOSURE"
        ]
    return []


def _get_default_sap_view_metadata():
    """Returns authoritative schema metadata for all 11 SAP S/4HANA CDS views."""
    views = [
        ("ZIVF_CustomerMaster_01_MD", 50000),
        ("ZIVR_SKUMaster_01_TR", 30000),
        ("ZIVF_CategoryMaster_01_TR", 100),
        ("ZITR_DayAttribute_01_TR", 5000),
        ("ZIVF_TerritoryMaster_01_TR", 1000),
        ("ZPVF_SD_SalesHistory_01_TR", 5000000),
        ("ZPVF_SD_CurrentSales_01_TR", 1000000),
        ("ZIVF_ZoneMaster_01_TR", 10),
        ("ZIVF_RegionMaster_MD_01", 100),
        ("ZPVF_SD_CancelledSalesInvoice_01_TR", 50000),
        ("ZIVF_SD_Sales_Plan_Target_01_TR", 200000)
    ]
    result = []
    for vname, rc in views:
        cols = _get_fallback_cols_for_sap_view(vname)
        result.append({
            "schema_name": "SAP_S4",
            "view_name": vname,
            "row_count": rc,
            "column_count": len(cols),
            "columns_detail": [{"name": c} for c in cols]
        })
    return result


def _load_hana(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = _dict_cursor(local)
        cur.execute("""
            SELECT schema_name, view_name, row_count, column_count, columns_detail
            FROM sap_hana_view_metadata
            WHERE session_id = %s
        """, (session_id,))
        rows = cur.fetchall()

        # If not populated by session_id, check via connection_id in database_credential
        if not rows:
            cur.execute("""
                SELECT connection_id, credential, db_type
                FROM database_credential
                WHERE session_id = %s AND (LOWER(db_type) LIKE '%hana%' OR LOWER(db_type) LIKE '%sap%')
                LIMIT 1
            """, (session_id,))
            c_row = cur.fetchone()
            if c_row and c_row.get("connection_id"):
                cur.execute("""
                    SELECT schema_name, view_name, row_count, column_count, columns_detail
                    FROM sap_hana_view_metadata
                    WHERE connection_id = %s
                """, (c_row["connection_id"],))
                rows = cur.fetchall()
            
            # If still not found, check if session has any SAP credentials registered
            if not rows and c_row:
                rows = _get_default_sap_view_metadata()

        if not rows:
            # Check if this session is an SAP HANA workspace
            cur.execute("""
                SELECT 1 FROM database_credential
                WHERE (session_id = %s OR user_id = (SELECT user_id FROM workspaces WHERE session_id = %s LIMIT 1))
                  AND (LOWER(db_type) LIKE '%hana%' OR LOWER(db_type) LIKE '%sap%')
                LIMIT 1
            """, (session_id, session_id))
            if cur.fetchone():
                rows = _get_default_sap_view_metadata()

        if not rows:
            return chunks

        print(f"[RAG] Loading SAP HANA metadata: {len(rows)} views indexed")
        for r in rows:
            schema = r.get("schema_name") or "SAP_S4"
            view = r.get("view_name") or ""
            row_cnt = int(r.get("row_count") or 0)
            cols_raw = r.get("columns_detail")
            cols = []
            if isinstance(cols_raw, str):
                try:
                    c_json = json.loads(cols_raw)
                    if isinstance(c_json, list):
                        cols = [c["name"] if isinstance(c, dict) and "name" in c else str(c) for c in c_json]
                except Exception:
                    pass
            elif isinstance(cols_raw, list):
                cols = [c["name"] if isinstance(c, dict) and "name" in c else str(c) for c in cols_raw]

            if not cols:
                cols = _get_fallback_cols_for_sap_view(view)

            col_str = ", ".join(cols)  # Include ALL columns so NAME1 and other keys are preserved
            chunks.append(_chunk(
                f"[SCHEMA] db:{schema} table:{view} columns:{col_str} total_rows:{row_cnt}",
                db=schema, table=view, kind="schema"
            ))
            chunks.append(_chunk(
                f"[COUNT] db:{schema} table:{view} has {row_cnt:,} rows total.\n"
                f"Number of {view}: {row_cnt:,}\n"
                f"Columns in {view}: {col_str}",
                db=schema, table=view, kind="count"
            ))

        # Add SAP S4 Business Architecture & Rules chunk
        biz_rules = """[SCHEMA] db:SAP_S4 table:BUSINESS_RELATIONSHIPS_AND_RULES
SAP S/4HANA Sales & Distribution (SD) Architecture and Table Context:
1. Fact Views:
   - ZPVF_SD_SalesHistory_01_TR: Historical Invoices Fact view (~30,044,097 rows, years 2006 to 2025). Use for historical period queries, multi-year trends, and prior period sales.
   - ZPVF_SD_CurrentSales_01_TR: Current Period MTD Invoices Fact view (~1,000,000 rows, year 2026). Use for current period / MTD sales questions, top customers, and general sales analysis.
   - ZPVF_SD_CancelledSalesInvoice_01_TR: Cancelled Invoices Reference view (~50,000 rows). Contains list of cancelled invoices.
   - ZIVF_SD_Sales_Plan_Target_01_TR: Sales Plan & Target view (~200,000 rows). Target Value is PLAN_VALUE (or REV_PLAN_VAL), Target Qty is ORG_PLAN (or REV_PLAN).
   CRITICAL SAP HANA CASING FOR FACT COLUMNS (MUST USE DOUBLE QUOTES):
   "invoice_number", "invoice_date", "plant", "dealer_code", "ship_to_party", "SKU", "batch", "volume", "Tyre_Quantity", "ndp_value", "old_code", "invoice_value", "total_discount", "gst", "taxable_value", "FKART", "BILL_TYPE", "distribution_channel", "indicator_cancel".
   - Customer Master: ZIVF_CustomerMaster_01_MD (~69,104 rows, columns UPPERCASE). When ranking or listing customers/dealers (Top N, Worst N, Dealers), ALWAYS use JOIN (INNER JOIN) on (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0')) to guarantee valid registered customer names. Customer Name is COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code"). Filter: (cm.KLABC IS NULL OR cm.KLABC <> 'X'). Do NOT use cm.KUNNR IS NULL in ranking queries. (NOTE: SAP KUNNR often has 10-digit leading zeros '0001234567' while fact."dealer_code" is '1234567'. LTRIM matches them perfectly).
   - SKU / Material Master: ZIVR_SKUMaster_01_TR (~30,000 rows, columns UPPERCASE). Join on fact."SKU" = sk.MATNR. Description is sk.MAKTX or sk.PROD_TITLE, Brand is sk.BRAND, Sub Brand is sk.SUB_BRAND, Category Code is sk.CAT_CODE, Construction is sk.CONSTUCTION, Tyre Type is sk.TYRE_TYPE (or sk.TYRE_TP: TRUCK, LCV, CAR, etc.).
   - Category Master: ZIVF_CategoryMaster_01_TR (columns UPPERCASE). Join on sk.CAT_CODE = cat.CAT_CODE. Category Description is cat.CAT_DESC (Tyre, Tube, Flap).
   - Territory Master: ZIVF_TerritoryMaster_01_TR (columns UPPERCASE). Join on cm.BZIRK = tm.BZIRK. Territory Name is tm.NAME, Region code is tm.REGIO, Zone code is tm.P_ZONE.
   - Region Master: ZIVF_RegionMaster_MD_01 (columns LOWERCASE). Join on tm.REGIO = rm."region_code" or cm.REGIO = rm."region_code". Region Name is rm."region_name", Zone code is rm."zone_code".
   - Zone Master: ZIVF_ZoneMaster_01_TR (columns UPPERCASE). Join on rm."zone_code" = zm.P_ZONE or tm.P_ZONE = zm.P_ZONE. Zone Name is zm.NAME (NORTH, WEST, EAST, SOUTH, etc.).
   - Date Master: ZITR_DayAttribute_01_TR. Join on fact."invoice_date" = da."Date_". Year-Month is da.YMONTH, Year is da.CAL_YEAR, Qtr is da.QTR.
   - Sales Plan & Target Joins (ZIVF_SD_Sales_Plan_Target_01_TR):
     * Territory: tgt.TERR_CODE = tm.BZIRK
     * Region: tgt.REG_CODE = rm."region_code"
     * Date/Period: tgt.YMONTH = da.YMONTH
     * Product/SKU: tgt.MATNR = sk.MATNR
     * Category: tgt.CAT_CODE = cat.CAT_CODE
     * Target vs Actual Architecture: DO NOT directly join Sales Fact and Target Fact. Pre-aggregate Actual Sales and Target Plans in separate CTEs/subqueries by territory (tm.BZIRK) or region (rm."region_code") or month (da.YMONTH), then join the aggregated CTEs on the dimension key.
3. Filtering & Exclusion Rules:
   - Distribution Channel: ALWAYS filter fact."distribution_channel" = '10' (string literal).
   - Cancelled Invoices Exclusion (CRITICAL FOR PERFORMANCE & MEMORY):
     ALWAYS filter directly on the fact view:
     AND (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
     NOTE: Both CurrentSales and SalesHistory fact views contain the indicator_cancel column ('X' indicates cancelled).
     DO NOT join ZPVF_SD_CancelledSalesInvoice_01_TR across 30M rows, as multi-column string join on 30M rows exhausts HANA memory (GLOBAL_ALLOCATION_LIMIT).
   - Customer Status: ALWAYS filter (cm.KLABC IS NULL OR cm.KLABC <> 'X') on ZIVF_CustomerMaster_01_MD (exclude deleted/test/scrap customers).
4. Business Metrics:
   - Sales / Revenue = SUM(fact."invoice_value")
   - Volume / Quantity = SUM(fact."Tyre_Quantity") or SUM(fact."volume")
   - Net Sales = SUM(fact."invoice_value") - SUM(COALESCE(fact."total_discount", 0))
   - Average Realization = SUM(fact."invoice_value") / NULLIF(SUM(fact."Tyre_Quantity"), 0)
   - Planned Target Value = SUM(tgt.PLAN_VALUE), Planned Qty = SUM(tgt.ORG_PLAN)
   - Target Value Achievement % = (SUM(fact."invoice_value") / NULLIF(SUM(tgt.PLAN_VALUE), 0)) * 100
   - Target Qty Achievement % = (SUM(fact."Tyre_Quantity") / NULLIF(SUM(tgt.ORG_PLAN), 0)) * 100
"""
        chunks.append(_chunk(biz_rules, db="SAP_S4", table="BUSINESS_RULES", kind="schema"))

        # Distinct value enrichments for precise vector matching
        chunks.append(_chunk("[COUNT] db:SAP_S4 table:ZIVF_CategoryMaster_01_TR: All values of CAT_DESC in ZIVF_CategoryMaster_01_TR: Tyre, Tube, Flap", db="SAP_S4", table="ZIVF_CategoryMaster_01_TR", kind="count"))
        chunks.append(_chunk("[COUNT] db:SAP_S4 table:ZIVR_SKUMaster_01_TR: All values of CONSTUCTION in ZIVR_SKUMaster_01_TR: RADIAL, BIAS\nAll values of TYRE_TYPE in ZIVR_SKUMaster_01_TR: TRUCK, LCV, CAR, 3W, SCV, FARM, OTR\nAll values of BRAND in ZIVR_SKUMaster_01_TR: JK TYRE, VIKRANT, BLAZE, ELANZO", db="SAP_S4", table="ZIVR_SKUMaster_01_TR", kind="count"))
        chunks.append(_chunk("[COUNT] db:SAP_S4 table:ZIVF_ZoneMaster_01_TR: All values of NAME in ZIVF_ZoneMaster_01_TR: North Zone, South Zone, East Zone, West Zone, Central Zone", db="SAP_S4", table="ZIVF_ZoneMaster_01_TR", kind="count"))
        chunks.append(_chunk("[COUNT] db:SAP_S4 table:ZIVF_CustomerMaster_01_MD: All values of KTOKD in ZIVF_CustomerMaster_01_MD: Dealer, Fleet, OEM, Institutional\nAll values of CLUB_CATG in ZIVF_CustomerMaster_01_MD: Champion, Club, Star, Retailer", db="SAP_S4", table="ZIVF_CustomerMaster_01_MD", kind="count"))
    except Exception as e:
        print(f"[RAG] _load_hana error: {e}")
    finally:
        if cur: cur.close()
        if local: local.close()
    return chunks


def _load_analysis_report(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = _dict_cursor(local)
        cur.execute(
            """SELECT topic, title, url, brief FROM saved_web_results
               WHERE session_id=%s ORDER BY topic""",
            (session_id,)
        )
        web_rows = cur.fetchall()
        if web_rows:
            from collections import defaultdict as _dd
            by_topic = _dd(list)
            for r in web_rows:
                by_topic[r["topic"]].append(r)
            for topic, items in by_topic.items():
                lines = [f"[ANALYSIS_WEB] Web research topic: {topic} ({len(items)} results)"]
                for item in items:
                    lines.append(f"  Title: {item['title']}")
                    if item.get("brief"):
                        lines.append(f"  Brief: {str(item['brief'])[:400]}")
                chunks.append(_chunk("\n".join(lines), kind="analysis_web", table=topic))
            print(f"[RAG] analysis_web: {len(by_topic)} topics")
        cur.execute(
            """SELECT DISTINCT external_database, new_user_db, table_name
               FROM external_db_sync_log
               WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db!=''
               ORDER BY external_database, table_name""",
            (session_id,)
        )
        sync_rows = cur.fetchall()
        if sync_rows:
            from collections import defaultdict as _dd2
            by_db = _dd2(list)
            for r in sync_rows:
                by_db[r["external_database"]].append(r)
            for ext_db, rows in by_db.items():
                new_db = rows[0]["new_user_db"]
                tables = [r["table_name"] for r in rows if r["table_name"]]
                text = (
                    f"[ANALYSIS_DB_META] Database analyzed: {ext_db} "
                    f"(stored as: {new_db})\n"
                    f"Tables found: {', '.join(tables)}\n"
                    f"Total tables: {len(tables)}"
                )
                chunks.append(_chunk(text, kind="analysis_db_meta", db=new_db))
            print(f"[RAG] analysis_db_meta: {len(by_db)} databases")
    except Exception as e:
        print(f"[RAG] analysis_report load error: {e}")
    finally:
        if cur:   cur.close()
        if local: local.close()
    return chunks


def _load_db(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = _dict_cursor(local)
        cur.execute("""SELECT DISTINCT new_user_db FROM external_db_sync_log
                       WHERE session_id=%s AND new_user_db IS NOT NULL AND new_user_db!=''""",
                    (session_id,))
        dbs = [r["new_user_db"] for r in cur.fetchall()]
    finally:
        if cur:   cur.close()
        if local: local.close()

    for db in dbs:
        if not re.match(r'^\w+$', db): continue
        conn = c2 = None
        try:
            conn = mysql.connector.connect(
                host=MYSQL_CONFIG["host"], port=MYSQL_CONFIG["port"],
                user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"],
                database=db, connection_timeout=10)
            c2 = _dict_cursor(conn)
            c2.execute("SHOW TABLES")
            tables   = [list(r.values())[0] for r in c2.fetchall()]
            all_rows = {}

            for t in tables:
                if not re.match(r'^\w+$', t): continue
                try:
                    c2.execute(f"SELECT * FROM `{t}` LIMIT %s", (MAX_ROWS,))
                    rows = c2.fetchall()
                    if not rows: continue
                    
                    if t == 'workspace_files':
                        for r in rows:
                            try:
                                import json
                                file_data = json.loads(r['file_data']) if isinstance(r.get('file_data'), str) else (r.get('file_data') or {})
                                structured = file_data.get('structured_content')
                                if structured:
                                    paragraphs = structured.get('paragraphs', [])
                                    if paragraphs:
                                        all_rows['Document_Paragraphs'] = paragraphs
                                    for idx, vtable in enumerate(structured.get('tables', [])):
                                        base_vname = vtable.get('table_name', f"PDF_Table_{idx+1}")
                                        vname = base_vname
                                        v_counter = 1
                                        while vname in all_rows:
                                            vname = f"{base_vname} ({v_counter})"
                                            v_counter += 1
                                        vcols = vtable.get('headers', [])
                                        
                                        # Deduplicate headers
                                        seen_cols = {}
                                        dedup_cols = []
                                        for c in vcols:
                                            base = str(c).strip() if c else "Column"
                                            if base in seen_cols:
                                                seen_cols[base] += 1
                                                dedup_cols.append(f"{base} ({seen_cols[base]})")
                                            else:
                                                seen_cols[base] = 1
                                                dedup_cols.append(base)

                                        vrows = []
                                        for row_arr in vtable.get('rows', []):
                                            row_dict = {}
                                            for i, col in enumerate(dedup_cols):
                                                row_dict[col] = row_arr[i] if i < len(row_arr) else ""
                                            vrows.append(row_dict)
                                        all_rows[vname] = vrows
                            except Exception as e:
                                print(f"Error parsing workspace_files json in RAG: {e}")
                    else:
                        all_rows[t] = rows
                except Exception as e:
                    print(f"[RAG] skip {t}: {e}")

            for t, rows in all_rows.items():
                if not rows: continue
                cols = list(rows[0].keys())
                chunks.append(_chunk(
                    f"[SCHEMA] db:{db} table:{t} columns:{','.join(cols)} total_rows:{len(rows)}",
                    db=db, table=t, kind="schema"))
                lines = [
                    f"[COUNT] db:{db} table:{t} has {len(rows)} rows total.",
                    f"Number of {t}: {len(rows)}",
                    f"Total {t} count: {len(rows)}"
                ]
                for col in cols[:10]:
                    vals = list(dict.fromkeys(
                        str(r[col]) for r in rows if r.get(col) is not None and str(r.get(col)).strip()))
                    if vals:
                        lines.append(f"All values of {col} in {t}: {', '.join(vals[:40])}")
                chunks.append(_chunk("\n".join(lines), db=db, table=t, kind="count"))
                for i, row in enumerate(rows, 1):
                    parts = " | ".join(f"{k}:{v}" for k,v in row.items()
                                       if v is not None and str(v).strip())
                    chunks.append(_chunk(f"[ROW] db:{db} table:{t} row{i}: {parts}",
                                         db=db, table=t, kind="row"))
                print(f"[RAG] {db}.{t}: {len(rows)} rows → {len(rows)+2} chunks")

            chunks += _build_joins(db, all_rows)
        except Exception as e:
            print(f"[RAG] db connect {db}: {e}")
        finally:
            if c2:   c2.close()
            if conn: conn.close()
    return chunks


def _build_joins(db, all_rows):
    chunks = []
    user_tables = [t for t in all_rows if re.search(r'\busers?\b', t, re.I)]
    for ut in user_tables:
        u_rows = all_rows[ut]
        if not u_rows: continue
        ucols  = list(u_rows[0].keys())
        id_col = next((c for c in ucols if c in ('id','user_id','uid')), ucols[0])
        nm_col = next((c for c in ucols if re.search(r'\b(name|username)\b', c, re.I)), None)
        for ur in u_rows:
            uid   = str(ur.get(id_col,"")).strip()
            uname = str(ur.get(nm_col, uid)).strip() if nm_col else uid
            if not uid: continue
            for at, a_rows in all_rows.items():
                if at == ut or not a_rows: continue
                acols   = list(a_rows[0].keys())
                ref_col = next((c for c in acols if re.search(r'\buser_id\b|\buid\b|\bauthor\b', c, re.I)), None)
                if not ref_col: continue
                acts = [r for r in a_rows if str(r.get(ref_col,"")).strip() == uid]
                if not acts: continue
                detail = " || ".join(
                    " | ".join(f"{k}:{v}" for k,v in r.items() if v is not None and str(v).strip())
                    for r in acts[:15])
                chunks.append(_chunk(
                    f"[JOIN] db:{db} user:'{uname}' (id:{uid}) from:{ut} "
                    f"has {len(acts)} record(s) in table:{at}. data: {detail}",
                    db=db, table=f"{ut}+{at}", kind="join"))
    return chunks


def _load_sheets(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = _dict_cursor(local)
        cur.execute("SELECT table_name,sheet_url FROM sheet_scans WHERE session_id=%s", (session_id,))
        for sc in cur.fetchall():
            t = sc["table_name"]
            if not re.match(r'^sheet_\w+$', t): continue
            try:
                cur.execute(f"SELECT * FROM `{t}` LIMIT %s", (MAX_ROWS,))
                rows = cur.fetchall()
                if not rows: continue
                cols = [c for c in rows[0].keys() if c != "_row_id"]
                chunks.append(_chunk(f"[SCHEMA] sheet:{t} url:{sc.get('sheet_url','')} columns:{','.join(cols)} rows:{len(rows)}", table=t, kind="schema"))
                lines = [f"[COUNT] sheet:{t} has {len(rows)} rows total."]
                for col in cols[:6]:
                    vals = list(dict.fromkeys(str(r[col]) for r in rows if r.get(col) is not None))
                    lines.append(f"All values of {col}: {', '.join(vals[:20])}")
                chunks.append(_chunk("\n".join(lines), table=t, kind="count"))
                for i, row in enumerate(rows, 1):
                    parts = " | ".join(f"{k}:{v}" for k,v in row.items()
                                       if k!="_row_id" and v is not None and str(v).strip())
                    chunks.append(_chunk(f"[ROW] sheet:{t} row{i}: {parts}", table=t, kind="row"))
            except Exception as e:
                print(f"[RAG] sheet {t}: {e}")
    except Exception as e:
        print(f"[RAG] sheets: {e}")
    finally:
        if cur:   cur.close()
        if local: local.close()
    return chunks


def _load_web(session_id, get_fn):
    chunks = []
    local = cur = None
    try:
        local = _local_conn(get_fn)
        cur   = _dict_cursor(local)
        cur.execute("SELECT title,url,brief,topic FROM saved_web_results WHERE session_id=%s", (session_id,))
        for r in cur.fetchall():
            chunks.append(_chunk(
                f"[WEB] title:{r['title']} url:{r['url']} topic:{r.get('topic','')} content:{r.get('brief','')}",
                kind="web"))
    except Exception as e:
        print(f"[RAG] web: {e}")
    finally:
        if cur:   cur.close()
        if local: local.close()
    return chunks


def _chunk(text, db="", table="", kind="row"):
    return {"text": text, "db": db, "table": table, "kind": kind}


# ══════════════════════════════════════════════════════
# VECTOR STORE (ChromaDB)
# ══════════════════════════════════════════════════════

def _get_or_create_lock(session_id):
    if session_id not in _LOCKS:
        _LOCKS[session_id] = threading.Lock()
    return _LOCKS[session_id]


def _col_safe_count(col):
    try:    return col.count()
    except: return 0


def _build_store(session_id, get_fn):
    import chromadb
    lock = _get_or_create_lock(session_id)
    with lock:
        now = time.time()
        if session_id in _CACHE:
            chunks, bm25, col, ts = _CACHE[session_id]
            if now - ts < CACHE_TTL and _col_safe_count(col) > 0:
                print(f"[RAG] cache hit — {len(chunks)} chunks")
                return chunks, bm25, col
            else:
                _CACHE.pop(session_id, None)

        print(f"[RAG] building store for {session_id[:8]}...")
        all_chunks = _load_all(session_id, get_fn)
        if not all_chunks:
            return None, None, None

        # OPTIMIZATION 4: Deduplicate chunks before indexing
        seen_texts = set()
        deduped    = []
        for c in all_chunks:
            if c["text"] not in seen_texts:
                seen_texts.add(c["text"])
                deduped.append(c)
        removed = len(all_chunks) - len(deduped)
        if removed > 0:
            print(f"[RAG] deduped {removed} duplicates → {len(deduped)} unique chunks")
        all_chunks = deduped

        texts    = [c["text"] for c in all_chunks]
        bm25_idx = BM25(texts)

        # Batch encode using global model + cache
        embeds = _encode_texts(texts)

        col_name = "s_" + hashlib.md5(session_id.encode()).hexdigest()[:12]
        if session_id not in _CLIENTS:
            _CLIENTS[session_id] = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
        client = _CLIENTS[session_id]
        try: client.delete_collection(col_name)
        except: pass
        col = client.create_collection(col_name)

        for i in range(0, len(all_chunks), 500):
            b = all_chunks[i:i+500]
            col.add(
                documents  = [c["text"]  for c in b],
                embeddings = embeds[i:i+500],
                metadatas  = [{"db":c["db"],"table":c["table"],"kind":c["kind"]} for c in b],
                ids        = [f"c{i+j}" for j in range(len(b))]
            )

        _CLIENTS[session_id] = client
        _CACHE[session_id]   = (all_chunks, bm25_idx, col, now)
        print(f"[RAG] ✓ {len(all_chunks)} chunks indexed")
        return all_chunks, bm25_idx, col


# ══════════════════════════════════════════════════════
# QUERY UNDERSTANDING
# ══════════════════════════════════════════════════════

def _understand(question, all_chunks):
    q      = question.lower()
    tokens = set(re.findall(r'\b\w{3,}\b', q))
    known_tables = list(dict.fromkeys(c["table"] for c in all_chunks if c["table"]))

    table_hints = []
    for t in known_tables:
        t_parts = set(re.findall(r'\b\w{3,}\b', t.lower()))
        overlap = tokens & t_parts
        if overlap: table_hints.append((t, len(overlap)))
    table_hints.sort(key=lambda x: -x[1])
    matched_tables = [t for t,_ in table_hints[:5]]

    intent = "lookup"
    if re.search(r'\bhow\s+many\b|\bcount\b|\btotal\b|\bnumber\s+of\b|\bhow\s+much\b', q):
        intent = "count"
    elif re.search(r'\blist\b|\ball\b|\beveryone\b|\bnames?\b|\bshow\s+(me\s+)?all\b', q):
        intent = "list"
    elif re.search(r'\bwho\b|\bwhich\s+user\b|\bwhose\b', q):
        intent = "who"
    elif re.search(r'\bcreated\s+by\b|\bbelongs?\s+to\b|\bby\s+whom\b|\bowned\s+by\b', q):
        intent = "join"
    elif re.search(r'\bwhat\s+is\b|\bwhat\s+are\b|\btell\s+me\b|\bfind\b|\bget\b', q):
        intent = "lookup"

    entities = re.findall(r"'([^']+)'|\"([^\"]+)\"", question)
    entities = [e[0] or e[1] for e in entities]

    queries = [question]
    if intent == "count" and matched_tables:
        for t in matched_tables[:3]:
            queries += [f"COUNT {t} total rows", f"Number of {t}", f"how many {t}", f"[COUNT] {t}"]
    elif intent == "list" and matched_tables:
        for t in matched_tables[:2]:
            queries += [f"All values of", f"list all {t}", f"[COUNT] {t}"]
    elif intent == "join":
        queries += ["[JOIN]"] + [f"user '{e}'" for e in entities]
    elif entities:
        queries += [f"'{e}'" for e in entities] + [f"{e}" for e in entities]

    if not matched_tables:
        for t in known_tables:
            for tok in tokens:
                if tok in t.lower() and len(tok) > 3:
                    matched_tables.append(t); break
        matched_tables = list(dict.fromkeys(matched_tables))[:5]

    return {
        "intent":      intent,
        "table_hints": matched_tables,
        "entities":    entities,
        "queries":     list(dict.fromkeys(queries))
    }


# ══════════════════════════════════════════════════════
# HYBRID RETRIEVAL (BM25 + Vector + Cross-Encoder Rerank)
# ══════════════════════════════════════════════════════

def _retrieve(all_chunks, bm25_idx, col, question, understanding):
    intent   = understanding["intent"]
    hints    = understanding["table_hints"]
    queries  = understanding["queries"]
    entities = understanding["entities"]

    scores = defaultdict(float)

    # BM25
    for q in queries:
        for idx, s in bm25_idx.score(q, top_k=80):
            scores[idx] += s * 1.0

    # OPTIMIZATION 3: Batch encode all queries at once
    q_embeds = _encode_texts(queries)
    per_q    = max(8, TOP_K // len(queries))
    seen     = set()
    for q_emb in q_embeds:
        n = min(per_q, _col_safe_count(col))
        if n == 0: continue
        res = col.query(query_embeddings=[q_emb], n_results=n,
                        include=["documents","metadatas"])
        for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
            key = doc[:100]
            if key in seen: continue
            seen.add(key)
            for i, c in enumerate(all_chunks):
                if c["text"][:100] == key:
                    scores[i] += 2.0; break

    # Boosts
    for i, c in enumerate(all_chunks):
        if c["table"] in hints:                               scores[i] += 5.0
        if intent == "count" and c["kind"] == "count":        scores[i] += 8.0
        elif intent == "list" and c["kind"] == "count":       scores[i] += 6.0
        elif intent == "join" and c["kind"] == "join":        scores[i] += 8.0
        for ent in entities:
            if ent.lower() in c["text"].lower():              scores[i] += 4.0

    ranked = sorted(scores.items(), key=lambda x: -x[1])

    forced = {i for i, c in enumerate(all_chunks)
              if c["kind"] == "count" and c["table"] in hints}

    candidate_indices = list(forced)
    for i, _ in ranked:
        if i not in forced: candidate_indices.append(i)
        if len(candidate_indices) >= TOP_K: break

    # OPTIMIZATION 5: Cross-encoder reranking top-300 → keep best 250
    RERANK_TOP  = 300
    RERANK_KEEP = 250
    rerank_pool = candidate_indices[:RERANK_TOP]

    if len(rerank_pool) > RERANK_KEEP:
        try:
            ce_model  = _get_cross_encoder()
            pairs     = [(question, all_chunks[i]["text"][:512]) for i in rerank_pool]
            ce_scores = ce_model.predict(pairs)
            reranked  = sorted(zip(rerank_pool, ce_scores), key=lambda x: -x[1])
            forced_in = [i for i in rerank_pool if i in forced]
            reranked_nf = [i for i, _ in reranked if i not in forced]
            rerank_pool = forced_in + reranked_nf[:RERANK_KEEP]
            candidate_indices = rerank_pool + candidate_indices[RERANK_TOP:]
            print(f"[RAG] cross-encoder reranked {RERANK_TOP} → kept {len(rerank_pool)}")
        except Exception as e:
            print(f"[RAG] cross-encoder skipped: {e}")

    parts, total = [], 0
    for i in candidate_indices:
        txt   = all_chunks[i]["text"]
        if total + len(txt) > MAX_CTX_CHARS: break
        parts.append(txt)
        total += len(txt)

    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════
# AUTO CHAT HISTORY SAVE
# ══════════════════════════════════════════════════════

# One-time flag — server startup এ একবার UNIQUE drop করবে
_UNIQUE_FIXED = False

def _ensure_no_unique_on_chat_id(get_fn):
    """chat_id column এ UNIQUE constraint থাকলে drop করো (once per server start)."""
    global _UNIQUE_FIXED
    if _UNIQUE_FIXED:
        return
    conn = cur = None
    try:
        conn = get_fn()
        cur  = conn.cursor()
        # Check if UNIQUE index named 'chat_id' exists
        cur.execute("""
            SELECT INDEX_NAME FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'session_chat_history'
              AND INDEX_NAME   = 'chat_id'
              AND NON_UNIQUE   = 0
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            cur.execute("ALTER TABLE session_chat_history DROP INDEX chat_id")
            conn.commit()
            print("[History] ✓ UNIQUE index on chat_id dropped — multiple rows per chat_id now allowed")
        _UNIQUE_FIXED = True
    except Exception as e:
        print(f"[History] unique-fix skipped: {e}")
        _UNIQUE_FIXED = True  # Don't retry on error
    finally:
        if cur:  cur.close()
        if conn: conn.close()


_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS session_chat_history (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    session_id          VARCHAR(100) NOT NULL,
    user_id             INT          NOT NULL,
    turn_index          INT          NOT NULL DEFAULT 0,
    visit_number        INT          NOT NULL DEFAULT 1,
    local_turn_index    INT          NOT NULL DEFAULT 0,
    question            TEXT         NOT NULL,
    answer              LONGTEXT     NOT NULL,
    follow_up_questions JSON         DEFAULT NULL,
    intent              VARCHAR(50)  DEFAULT NULL,
    mode                VARCHAR(30)  DEFAULT 'answer',
    login_token         VARCHAR(255) DEFAULT NULL,
    chat_id             VARCHAR(64)  DEFAULT NULL,
    visualizations      JSON         DEFAULT NULL,
    created_at          DATETIME     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session      (session_id),
    INDEX idx_user         (user_id),
    INDEX idx_session_user (session_id, user_id),
    INDEX idx_chat_id      (chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# SQL to drop UNIQUE constraint on chat_id if it exists (run once)
_FIX_UNIQUE_SQL = """
ALTER TABLE session_chat_history
DROP INDEX chat_id
"""

# ══════════════════════════════════════════════════
# CHAT_ID MANAGER
# ══════════════════════════════════════════════════

def _resolve_chat_id(get_fn, session_id, user_id, source_chat_id, force_new=False, visit_number=None):
    """
    CASE A — source_chat_id আছে + same day → same chat_id continue
    CASE B — source_chat_id আছে + different day → new chat_id + copy source history
    CASE C — source_chat_id নেই + today's chat exists → continue today's chat (unless force_new=True)
    CASE C — source_chat_id নেই + no today's chat → brand new chat_id
    """
    import uuid
    from datetime import date

    conn = cur = None
    try:
        conn = get_fn()
        cur  = _dict_cursor(conn)
        cur.execute(_HISTORY_TABLE_SQL)
        today = date.today()

        if force_new:
            new_id = uuid.uuid4().hex[:32]
            print(f"[ChatID] force_new=True → brand new {new_id[:8]}...")
            return new_id, True

        if visit_number:
            cur.execute("""
                SELECT chat_id FROM session_chat_history
                WHERE session_id = %s AND user_id = %s AND visit_number = %s
                ORDER BY id DESC LIMIT 1
            """, (session_id, int(user_id), int(visit_number)))
            v_row = cur.fetchone()
            if v_row and v_row["chat_id"]:
                print(f"[ChatID] Found by visit_number {visit_number} → {v_row['chat_id'][:8]}...")
                return v_row["chat_id"], False

        if source_chat_id:
            cur.execute("""
                SELECT chat_id, created_at FROM session_chat_history
                WHERE chat_id = %s AND user_id = %s
                ORDER BY id DESC LIMIT 1
            """, (source_chat_id, int(user_id)))
            src = cur.fetchone()

            if not src:
                new_id = uuid.uuid4().hex[:32]
                print(f"[ChatID] source not found → new {new_id[:8]}...")
                return new_id, True

            if src["created_at"].date() == today:
                # CASE A
                print(f"[ChatID] CASE A same day → {source_chat_id[:8]}...")
                return source_chat_id, False

            # CASE B — new day
            new_id = uuid.uuid4().hex[:32]
            print(f"[ChatID] CASE B new day → {new_id[:8]}... (from {source_chat_id[:8]}...)")
            cur.execute("""
                SELECT session_id, user_id, question, answer,
                       follow_up_questions, intent, mode, login_token, visualizations
                FROM session_chat_history
                WHERE chat_id = %s AND user_id = %s ORDER BY turn_index ASC
            """, (source_chat_id, int(user_id)))
            for i, r in enumerate(cur.fetchall()):
                cur.execute("""
                    INSERT INTO session_chat_history
                        (session_id,user_id,turn_index,visit_number,local_turn_index,
                         question,answer,follow_up_questions,intent,mode,
                         login_token,chat_id,visualizations,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """, (r["session_id"] or session_id, int(user_id), i, 1, i,
                      r["question"], r["answer"], r["follow_up_questions"],
                      r["intent"], r["mode"], r["login_token"],
                      new_id, r["visualizations"]))
            conn.commit()
            print(f"[ChatID] copied history → {new_id[:8]}...")
            return new_id, True

        # CASE C — no source_chat_id
        if not force_new:
            cur.execute("""
                SELECT chat_id FROM session_chat_history
                WHERE session_id = %s AND user_id = %s AND DATE(created_at) = %s
                ORDER BY id DESC LIMIT 1
            """, (session_id, int(user_id), today.isoformat()))
            today_row = cur.fetchone()

            if today_row and today_row.get("chat_id"):
                print(f"[ChatID] CASE C today exists → {today_row['chat_id'][:8]}...")
                return today_row["chat_id"], False

        new_id = uuid.uuid4().hex[:32]
        print(f"[ChatID] CASE C brand new → {new_id[:8]}...")
        return new_id, True

    except Exception as e:
        print(f"[ChatID] error: {e}")
        return uuid.uuid4().hex[:32], True
    finally:
        if cur:  cur.close()
        if conn: conn.close()


def _save_history(get_fn, session_id, user_id, question, answer,
                  follow_ups, intent, mode,
                  login_token=None, visualizations=None, chat_id=None):
    if not user_id: return
    from datetime import datetime
    _ensure_no_unique_on_chat_id(get_fn)  # drop UNIQUE once if needed
    conn = cur = None
    try:
        conn = get_fn()
        cur  = _dict_cursor(conn)

        eid  = chat_id or session_id

        cur.execute("""
            SELECT turn_index, visit_number, local_turn_index, created_at, login_token
            FROM session_chat_history
            WHERE chat_id = %s AND user_id = %s
            ORDER BY id DESC LIMIT 1
        """, (eid, int(user_id)))
        last = cur.fetchone()

        if last:
            turn   = last["turn_index"] + 1
            diff   = datetime.now() - last["created_at"]
            new_v  = (login_token is not None and last["login_token"] != login_token) or diff.total_seconds() > 3600
            visit  = last["visit_number"] + (1 if new_v else 0)
            local  = 0 if new_v else last["local_turn_index"] + 1
        else:
            turn = local = 0
            cur.execute("""
                SELECT COALESCE(MAX(visit_number), 0) AS max_v
                FROM session_chat_history
                WHERE session_id = %s AND user_id = %s
            """, (session_id, int(user_id)))
            max_v_row = cur.fetchone()
            visit = (max_v_row["max_v"] + 1) if max_v_row else 1

        cur.execute("""
            INSERT INTO session_chat_history
                (session_id,user_id,turn_index,visit_number,local_turn_index,
                 question,answer,follow_up_questions,intent,mode,
                 login_token,chat_id,visualizations)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (session_id, int(user_id), turn, visit, local,
              question, answer,
              json.dumps(follow_ups)     if follow_ups     else None,
              intent or None, mode, login_token, eid,
              json.dumps(visualizations) if visualizations else None))
        conn.commit()
        print(f"✅ [History] turn={turn} visit={visit} local={local} chat={eid[:8]}...")
    except Exception as e:
        print(f"❌ [History] save error: {e}")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
    return visit

# ══════════════════════════════════════════════════
# SYSTEM PROMPT
# ══════════════════════════════════════════════════

SYS = """You are a senior data analyst and database expert with deep analytical reasoning capabilities.
You have access to the user's actual database records as retrieved chunks.

Chunk types:
  [SCHEMA]           — table structure, column names, total row count
  [COUNT]            — exact row counts AND all distinct values per column — PRIMARY source for counts/lists
  [ROW]              — individual database records with all field values
  [JOIN]             — pre-computed cross-table joins: user X has N records in table Y with details
  [WEB]              — saved web content (raw)
  [ANALYSIS_WEB]     — web research grouped by topic with titles and summaries
  [ANALYSIS_DB_META] — database metadata: which databases and tables were analyzed

DEEP ANALYSIS RULES:
1. Read EVERY chunk exhaustively before forming your answer. Pay STRICT ATTENTION to the exact year, date, or context associated with each value.
2. STRICT DATA INTEGRITY: DO NOT hallucinate or swap values between different rows, years, or entities. Ensure absolute alignment.
3. For COUNT questions: find [COUNT] chunk with "Number of X: N" — this is authoritative.
4. For LIST questions: find [COUNT] chunk "All values of column_name:" — gives complete list.
5. For JOIN/relationship questions: find [JOIN] chunks — they show cross-table activity per user.
6. For WHY questions: analyze patterns, dates, sequences, frequencies across chunks to infer reasons.
7. For TREND questions: compare timestamps, sequences, values across [ROW] chunks.
8. For COMPARISON questions: pull data from multiple tables and compare side by side.
9. For DEEP questions: combine ROW + JOIN + COUNT chunks to give comprehensive multi-part answers.
10. NEVER say "I could not find" if ANY relevant data exists — dig deeper into chunks.
11. Always answer in full sentences with specifics — no vague responses.
12. DO NOT include source citations in the answer text — keep answer clean.
13. follow_up_questions MUST follow the EXACT format specified in the user prompt.
14. Respond ONLY in valid JSON."""

from model.llm_client import call_llm_chat

def _mistral(system, user, retries=2, temperature=0.15):
    if len(user) > 60000:
        print("[LLM] prompt is very large, but proceeding without arbitrary truncation to preserve JSON instructions.")
        
    messages = [
        {"role":"system","content":system},
        {"role":"user","content":user}
    ]
    
    for attempt in range(retries + 1):
        try:
            _t0 = time.time()
            response = call_llm_chat(messages, json_mode=True, temperature=temperature)
            _t1 = time.time()
            print(f"[LLM] call_llm_chat completed in {_t1 - _t0:.3f}s (attempt {attempt+1})")
            if response:
                return json.loads(response)
            return None
        except Exception as e:
            import traceback
            print(f"[LLM] error attempt {attempt+1}: {type(e).__name__}: {e}")
            print(f"[LLM] Traceback:\n{traceback.format_exc()}")
            if attempt == retries: return None
            time.sleep(1)
    return None


def _history(raw):
    if not raw or not isinstance(raw, list): return ""
    lines = [f"{str(t.get('role','user')).capitalize()}: {str(t.get('content',''))}"
             for t in raw[-6:] if isinstance(t, dict)]
    return ("Chat history:\n" + "\n".join(lines) + "\n\n") if lines else ""


def _to_str(val):
    if isinstance(val, str): return val
    if isinstance(val, dict):
        lines = []
        for k, v in val.items():
            if isinstance(v, list):
                lines.append(f"{k}:")
                for item in v:
                    if isinstance(item, dict):
                        lines.append("  • " + " | ".join(f"{ik}: {iv}" for ik, iv in item.items()))
                    else:
                        lines.append(f"  • {item}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)
    if isinstance(val, list):
        lines = []
        for item in val:
            if isinstance(item, dict):
                lines.append("• " + " | ".join(f"{k}: {v}" for k, v in item.items()))
            else:
                lines.append(f"• {item}")
        return "\n".join(lines)
    return str(val) if val else ""


DEFAULT_CANON_SYS = """You are a Query Canonicalizer for Business Intelligence.
Convert the user's natural language question into a structured JSON representation (Canonical Query).
Do not generate SQL yet. Extract the core analytical components.

Return ONLY a JSON object in this format:
{
  "analytical_intent": "e.g., dealer_ranking, sales_trend, total_revenue, category_breakdown",
  "metric": "e.g., sales, volume, discount, quantity",
  "aggregation": "e.g., sum, count, avg",
  "sort": "e.g., desc, asc",
  "limit": 5
}
If a component is missing from the user's question, set it to null.
"""

DEFAULT_SQL_SYS = """You are a Senior Data Analyst, SQL Expert, and Business Intelligence Assistant specializing in SAP S/4HANA Sales & Distribution (SD) analytics.

PRIMARY OBJECTIVE:
Generate clean, highly efficient, and 100% syntactically correct SAP HANA SQL that computes answers from schema "SAP_S4".

SCHEMA & BUSINESS DEFINITIONS:
1. TARGET SCHEMA / VIEWS:
   - Schema: "SAP_S4"
   - Fact Views:
     * "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" (Current Period / Rolling MTD Sales Fact View - FAST, use for current sales, top customers, recent data)
     * "SAP_S4"."ZPVF_SD_SalesHistory_01_TR" (Historical Sales Fact View - use for multi-year trends or historical prior periods)
     * "SAP_S4"."ZPVF_SD_CancelledSalesInvoice_01_TR" (Cancelled Sales Invoices Reference View)
     * "SAP_S4"."ZIVF_SD_Sales_Plan_Target_01_TR" (Sales Plan & Target View: Target Revenue = PLAN_VALUE or REV_PLAN_VAL, Target Qty = ORG_PLAN or REV_PLAN)
   
   - Master Views & Primary Join Keys:
     * Customer Master: "SAP_S4"."ZIVF_CustomerMaster_01_MD"
       JOIN: (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
       Customer Name: COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code") AS customer_name
       Mandatory Filter: (cm.KLABC IS NULL OR cm.KLABC <> 'X')
       (CRITICAL: In SAP S/4HANA, KUNNR is 10 digits with leading zeros '0001105923' while dealer_code in facts is '1105923'. LTRIM matches them reliably. Also fallback to BNAME if NAME1 is empty).
     * SKU / Material Master: "SAP_S4"."ZIVR_SKUMaster_01_TR" (JOIN: fact."SKU" = sk.MATNR)
       Product Name: COALESCE(sk.MAKTX, sk.PROD_TITLE, fact."SKU") AS product_name, Brand: sk.BRAND, Sub Brand: sk.SUB_BRAND, Category Code: sk.CAT_CODE
       Construction: sk.CONSTUCTION (Radial, Bias), Tyre Type: sk.TYRE_TYPE (TRUCK, LCV, CAR, 3W, SCV, FARM, OTR)
     * Category Master: "SAP_S4"."ZIVF_CategoryMaster_01_TR" (JOIN: sk.CAT_CODE = cat.CAT_CODE)
       Category Name: cat.CAT_DESC (Tyre, Tube, Flap)
     * Territory Master: "SAP_S4"."ZIVF_TerritoryMaster_01_TR" (JOIN: cm.BZIRK = tm.BZIRK)
       Territory Name: tm.NAME, Region code: tm.REGIO, Zone code: tm.P_ZONE, Plant: tm.WERKS, Area: tm.AREA
     * Region Master: "SAP_S4"."ZIVF_RegionMaster_MD_01" (JOIN: tm.REGIO = rm."region_code" OR cm.REGIO = rm."region_code")
       Region Name: rm."region_name", Zone code: rm."zone_code"
     * Zone Master: "SAP_S4"."ZIVF_ZoneMaster_01_TR" (JOIN: tm.P_ZONE = zm.P_ZONE OR rm."zone_code" = zm.P_ZONE)
       Zone Name: zm.NAME (North Zone, South Zone, East Zone, West Zone, Central Zone)
     * Date Attribute: "SAP_S4"."ZITR_DayAttribute_01_TR" (JOIN: fact."invoice_date" = da."Date_")
       Month: da.YMONTH, Year: da.CAL_YEAR, Qtr: da.QTR, Fin Year: da.FIN_YEAR
     * Sales Plan & Target: "SAP_S4"."ZIVF_SD_Sales_Plan_Target_01_TR"
       JOIN Keys: tgt.TERR_CODE = tm.BZIRK (Territory), tgt.REG_CODE = rm."region_code" (Region), tgt.YMONTH = da.YMONTH (Month/Period), tgt.MATNR = sk.MATNR (SKU), tgt.CAT_CODE = cat.CAT_CODE (Category)
       Target Revenue: tgt.PLAN_VALUE (or tgt.REV_PLAN_VAL), Target Qty: tgt.ORG_PLAN (or tgt.REV_PLAN)

2. CRITICAL SAP HANA CASE-SENSITIVITY & QUOTING RULES:
   - In SAP HANA, unquoted column names automatically become UPPERCASE!
   - Fact views (CurrentSales & SalesHistory) have LOWERCASE and MIXED-CASE column names.
     YOU MUST ALWAYS ENCLOSE FACT COLUMNS IN DOUBLE QUOTES:
     fact."dealer_code", fact."invoice_value", fact."Tyre_Quantity", fact."invoice_date", fact."distribution_channel", fact."indicator_cancel", fact."plant", fact."SKU", fact."ship_to_party", fact."total_discount", fact."taxable_value", fact."volume"
   - Region Master has LOWERCASE column names: rm."region_code", rm."region_name", rm."zone_code".
   - Date Master date column is MIXED-CASE: da."Date_".
   - Master views (CustomerMaster, SKUMaster, CategoryMaster, TerritoryMaster, ZoneMaster, SalesPlanTarget) have UPPERCASE column names:
     cm.NAME1, cm.KUNNR, cm.KLABC, cm.BZIRK, sk.MATNR, sk.BRAND, sk.MAKTX, sk.CONSTUCTION, sk.TYRE_TYPE, cat.CAT_DESC, tm.NAME, tm.REGIO, zm.NAME, tgt.PLAN_VALUE, tgt.ORG_PLAN.

3. QUERY SIMPLICITY & PERFORMANCE RULES:
   - KEEP QUERIES AS SIMPLE AND DIRECT AS POSSIBLE.
   - For general sales questions, current period, top customers/dealers, or latest performance:
     Query DIRECTLY from "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact (or "SAP_S4"."ZPVF_SD_SalesHistory_01_TR" for historical data).
     DO NOT write complex 31M-row UNION ALL queries unless the user specifically asks for multi-year historical trends or all-time data. Querying fact tables directly is clean, instant, and completely avoids memory limit errors!
   - Distribution channel: If the user explicitly asks for a distribution channel or dealer channel, filter fact."distribution_channel" = '10' (as string literal). If not mentioned in the query, DO NOT filter by distribution channel so all valid sales are included!
   - Cancellation check: AND (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1')).
     DO NOT join ZPVF_SD_CancelledSalesInvoice_01_TR.
   - MASTER TABLES & DESCRIPTIVE NAMES / DESCRIPTIONS MANDATE:
     Whenever the user query asks for Customers, Dealers, Products, Materials, SKUs, Categories, Territories, Regions, or Zones:
     YOU MUST JOIN THE CORRESPONDING MASTER TABLE TO FETCH THE HUMAN-READABLE NAME/DESCRIPTION.
     NEVER return only bare codes/IDs (e.g. dealer_code, MATNR, BZIRK, region_code, zone_code) without their descriptive name!
     * Customer/Dealer Join:
       When ranking or listing customers/dealers (Top N, Worst N, Performers):
       ALWAYS use JOIN "SAP_S4"."ZIVF_CustomerMaster_01_MD" cm (INNER JOIN):
       ON (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
       SELECT: COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code") AS customer_name, fact."dealer_code" AS customer_code
       Filter: AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')
       (CRITICAL: Using JOIN guarantees every customer returned has a verified profile and name. Never allow 'cm.KUNNR IS NULL' in ranking queries, as it pulls unprofiled orphan accounting adjustments like 102425).
     * SKU / Product: JOIN "SAP_S4"."ZIVR_SKUMaster_01_TR" sk ON fact."SKU" = sk.MATNR
       SELECT: COALESCE(sk.MAKTX, sk.PROD_TITLE, fact."SKU") AS product_name, fact."SKU" AS sku_code
     * Category: JOIN "SAP_S4"."ZIVF_CategoryMaster_01_TR" cat ON sk.CAT_CODE = cat.CAT_CODE
       SELECT: cat.CAT_DESC AS category_name
     * Territory: LEFT JOIN "SAP_S4"."ZIVF_TerritoryMaster_01_TR" tm ON cm.BZIRK = tm.BZIRK
       SELECT: tm.NAME AS territory_name, tm.BZIRK AS territory_code
     * Region: LEFT JOIN "SAP_S4"."ZIVF_RegionMaster_MD_01" rm ON (tm.REGIO = rm."region_code" OR cm.REGIO = rm."region_code")
       SELECT: rm."region_name", rm."region_code"
     * Zone: LEFT JOIN "SAP_S4"."ZIVF_ZoneMaster_01_TR" zm ON (tm.P_ZONE = zm.P_ZONE OR rm."zone_code" = zm.P_ZONE)
       SELECT: zm.NAME AS zone_name
   - CRITICAL CASE-INSENSITIVE TEXT FILTERING (SAP HANA RULE):
     SAP HANA string comparisons are strictly case-sensitive! Whenever filtering on names or text attributes (e.g., rm."region_name", zm.NAME, sk.BRAND, cat.CAT_DESC, sk.TYRE_TYPE):
     ALWAYS use case-insensitive matching with UPPER and LIKE or TRIM:
     e.g., UPPER(TRIM(rm."region_name")) LIKE UPPER('%Jaipur%')
     NEVER write rm."region_name" = 'Jaipur' as it returns 0 rows if stored in uppercase (e.g. 'JAIPUR')!
   - NO UNREQUESTED DISTRIBUTION CHANNEL FILTER:
     Filter fact."distribution_channel" = '10' ONLY IF the user explicitly asked for channel 10 or dealer channel. If not mentioned, DO NOT include fact."distribution_channel" = '10' so all valid channel sales are captured!
   - SUBQUERY & CTE JOIN KEY HYGIENE (LEADING ZEROS):
     Whenever comparing dealer/customer codes between CTEs, tables, or subqueries, ALWAYS wrap in LTRIM:
     ON (LTRIM(ad.dealer_code, '0') = LTRIM(rs.dealer_code, '0'))
   - TARGET VS ACTUAL / PLAN ACHIEVEMENT RULES:
     Target view is at monthly planning grain (TERR_CODE, REG_CODE, YMONTH) and does NOT have dealer_code or daily dates.
     NEVER join Sales Fact directly to Target Fact with an INNER/LEFT JOIN — this causes Cartesian product and duplicates numbers!
     ALWAYS use Common Table Expressions (CTEs):
     * CTE 1: actual_sales — Aggregate SUM(fact."invoice_value") AS actual_sales, SUM(fact."Tyre_Quantity") AS actual_qty from ZPVF_SD_CurrentSales_01_TR grouped by territory / region / zone / month.
     * CTE 2: target_plans — Aggregate SUM(tgt.PLAN_VALUE) AS target_sales, SUM(tgt.ORG_PLAN) AS target_qty from ZIVF_SD_Sales_Plan_Target_01_TR grouped by tgt.TERR_CODE / tgt.REG_CODE / tgt.YMONTH.
     * Outer Query: Join CTEs on territory/region/month, calculate % Value ((actual_sales / target_sales) * 100) and % Qty ((actual_qty / target_qty) * 100).

   - YMONTH & DATE FILTERING HYGIENE:
     * In "SAP_S4"."ZITR_DayAttribute_01_TR", da.YMONTH is a 6-digit string formatted as 'YYYYMM' (e.g. '202609', '202410').
     * NEVER compare da.YMONTH = EXTRACT(MONTH FROM ...) because EXTRACT(MONTH) returns a single digit (1-12) while YMONTH is 6 digits!
     * For "current month", "last month", or "recent period", NEVER use CURRENT_DATE because historical datasets may end earlier. Instead, find the latest month dynamically:
       WHERE da.YMONTH = (SELECT MAX(sub_da.YMONTH) FROM "SAP_S4"."ZITR_DayAttribute_01_TR" sub_da WHERE sub_da."Date_" <= (SELECT MAX(sub_f."invoice_date") FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" sub_f))
       or in CTE: WHERE dms.month = (SELECT MAX(m.month) FROM dealer_monthly_sales m)
   - RESULT SET BOUNDING & ACTIONABLE LIMITS:
     * When generating granular reports across multiple dimensions (e.g. region + territory + category + month) without narrow user filters, ALWAYS append an appropriate ORDER BY ... DESC LIMIT 100 (or LIMIT 50) to keep results actionable, fast, and UI-friendly.
4. CANONICAL EXAMPLES:
   - Top N Customers in a Region / Territory / Zone (e.g. Jaipur):
     SELECT 
       COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code") AS customer_name, 
       fact."dealer_code" AS customer_code, 
       rm."region_name",
       SUM(fact."invoice_value") AS total_sales_value
     FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact
     JOIN "SAP_S4"."ZIVF_CustomerMaster_01_MD" cm 
       ON (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
     LEFT JOIN "SAP_S4"."ZIVF_TerritoryMaster_01_TR" tm ON cm.BZIRK = tm.BZIRK
     LEFT JOIN "SAP_S4"."ZIVF_RegionMaster_MD_01" rm ON (tm.REGIO = rm."region_code" OR cm.REGIO = rm."region_code")
     WHERE (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
       AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')
       AND UPPER(TRIM(rm."region_name")) LIKE UPPER('%Jaipur%')
     GROUP BY COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code"), fact."dealer_code", rm."region_name"
     ORDER BY total_sales_value DESC
     LIMIT 5

   - Top 2 Customers by Total Sales:
     SELECT 
       COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code") AS customer_name, 
       fact."dealer_code" AS customer_code, 
       SUM(fact."invoice_value") AS total_sales
     FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact
     JOIN "SAP_S4"."ZIVF_CustomerMaster_01_MD" cm 
       ON (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
     WHERE (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
       AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')
     GROUP BY COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code"), fact."dealer_code"
     ORDER BY total_sales DESC
     LIMIT 2

   - Worst 2 Customers / Lowest Performers:
     SELECT 
       COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code") AS customer_name, 
       fact."dealer_code" AS customer_code, 
       SUM(fact."invoice_value") AS total_sales
     FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact
     JOIN "SAP_S4"."ZIVF_CustomerMaster_01_MD" cm 
       ON (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
     WHERE (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
       AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')
     GROUP BY COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code"), fact."dealer_code"
     ORDER BY total_sales ASC
     LIMIT 2

   - Sales by Brand & Tyre Category:
     SELECT sk.BRAND, sk.TYRE_TYPE, SUM(fact."invoice_value") AS total_sales, SUM(fact."Tyre_Quantity") AS total_qty
     FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact
     JOIN "SAP_S4"."ZIVR_SKUMaster_01_TR" sk ON fact."SKU" = sk.MATNR
     WHERE fact."distribution_channel" = '10'
       AND (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
     GROUP BY sk.BRAND, sk.TYRE_TYPE
     ORDER BY total_sales DESC

   - Sales by Region & Zone:
     SELECT zm.NAME AS zone_name, rm."region_name", SUM(fact."invoice_value") AS total_sales
     FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact
     JOIN "SAP_S4"."ZIVF_CustomerMaster_01_MD" cm 
       ON (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
     JOIN "SAP_S4"."ZIVF_TerritoryMaster_01_TR" tm ON cm.BZIRK = tm.BZIRK
     JOIN "SAP_S4"."ZIVF_RegionMaster_MD_01" rm ON tm.REGIO = rm."region_code"
     JOIN "SAP_S4"."ZIVF_ZoneMaster_01_TR" zm ON tm.P_ZONE = zm.P_ZONE
     WHERE fact."distribution_channel" = '10'
       AND (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
       AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')
     GROUP BY zm.NAME, rm."region_name"
     ORDER BY total_sales DESC

   - Target vs Actual Sales & Achievement % by Territory, Region & Zone:
     WITH actual_sales AS (
       SELECT 
         tm.BZIRK AS terr_code,
         tm.NAME AS territory_name,
         rm."region_name",
         zm.NAME AS zone_name,
         SUM(fact."Tyre_Quantity") AS actual_qty,
         SUM(fact."invoice_value") AS actual_sales
       FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact
       JOIN "SAP_S4"."ZIVF_CustomerMaster_01_MD" cm 
         ON (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
       JOIN "SAP_S4"."ZIVF_TerritoryMaster_01_TR" tm ON cm.BZIRK = tm.BZIRK
       JOIN "SAP_S4"."ZIVF_RegionMaster_MD_01" rm ON tm.REGIO = rm."region_code"
       JOIN "SAP_S4"."ZIVF_ZoneMaster_01_TR" zm ON tm.P_ZONE = zm.P_ZONE
       WHERE fact."distribution_channel" = '10'
         AND (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
         AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')
       GROUP BY tm.BZIRK, tm.NAME, rm."region_name", zm.NAME
     ),
     target_plans AS (
       SELECT 
         tgt.TERR_CODE AS terr_code,
         SUM(tgt.ORG_PLAN) AS target_qty,
         SUM(tgt.PLAN_VALUE) AS target_sales
       FROM "SAP_S4"."ZIVF_SD_Sales_Plan_Target_01_TR" tgt
       GROUP BY tgt.TERR_CODE
     )
     SELECT 
       COALESCE(a.zone_name, 'Unknown') AS zone_name,
       COALESCE(a."region_name", 'Unknown') AS region_name,
       COALESCE(a.territory_name, 'Unknown') AS territory_name,
       COALESCE(a.actual_qty, 0) AS actual_qty,
       COALESCE(t.target_qty, 0) AS target_qty,
       COALESCE(a.actual_sales, 0) AS actual_sales,
       COALESCE(t.target_sales, 0) AS target_sales,
       ROUND((COALESCE(a.actual_sales, 0) / NULLIF(t.target_sales, 0)) * 100, 2) AS value_achv_pct,
       ROUND((COALESCE(a.actual_qty, 0) / NULLIF(t.target_qty, 0)) * 100, 2) AS qty_achv_pct
     FROM actual_sales a
     LEFT JOIN target_plans t ON a.terr_code = t.terr_code
     ORDER BY actual_sales DESC
     LIMIT 10

   - Multi-year Historical Trend (ONLY when explicitly requested):
     SELECT da.CAL_YEAR, da.YMONTH, SUM(fact."invoice_value") AS total_sales
     FROM (
       SELECT "distribution_channel", "invoice_date", "indicator_cancel", "invoice_value"
       FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR"
       WHERE "distribution_channel" = '10'
         AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
       UNION ALL
       SELECT "distribution_channel", "invoice_date", "indicator_cancel", "invoice_value"
       FROM "SAP_S4"."ZPVF_SD_SalesHistory_01_TR"
       WHERE "distribution_channel" = '10'
         AND ("indicator_cancel" IS NULL OR "indicator_cancel" NOT IN ('X', 'Y', '1'))
     ) AS fact
     JOIN "SAP_S4"."ZITR_DayAttribute_01_TR" da ON fact."invoice_date" = da."Date_"
     GROUP BY da.CAL_YEAR, da.YMONTH
     ORDER BY da.CAL_YEAR, da.YMONTH

   - Top N Per Month / Category (e.g. Top 2 Dealers Month-wise):
     NEVER use 'LIMIT 2 BY month' (ClickHouse syntax invalid in SAP HANA).
     ALWAYS use standard SQL window function ROW_NUMBER() OVER (PARTITION BY ...):
     WITH ranked_dealers AS (
       SELECT 
         da.YMONTH AS month,
         COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code") AS dealer_name,
         fact."dealer_code" AS dealer_code,
         SUM(fact."Tyre_Quantity") AS total_quantity,
         SUM(fact."invoice_value") AS total_sales,
         ROW_NUMBER() OVER (PARTITION BY da.YMONTH ORDER BY SUM(fact."Tyre_Quantity") DESC) AS rn
       FROM "SAP_S4"."ZPVF_SD_CurrentSales_01_TR" fact
       JOIN "SAP_S4"."ZIVR_SKUMaster_01_TR" sk ON fact."SKU" = sk.MATNR
       JOIN "SAP_S4"."ZIVF_CustomerMaster_01_MD" cm 
         ON (fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", '0') = LTRIM(cm.KUNNR, '0'))
       JOIN "SAP_S4"."ZITR_DayAttribute_01_TR" da ON fact."invoice_date" = da."Date_"
       WHERE (fact."indicator_cancel" IS NULL OR fact."indicator_cancel" NOT IN ('X', 'Y', '1'))
         AND (cm.KLABC IS NULL OR cm.KLABC <> 'X')
       GROUP BY da.YMONTH, COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact."dealer_code"), fact."dealer_code"
     )
     SELECT month, dealer_name, dealer_code, total_quantity, total_sales
     FROM ranked_dealers
     WHERE rn <= 2
     ORDER BY month DESC, rn ASC

5. OUTPUT FORMAT:
   Return valid JSON only:
   {
     "db": "SAP_S4",
     "sql": "SELECT ... FROM ...",
     "reasoning": "..."
   }
"""

DEFAULT_DEFAULT_TPL = """Business data summary ({len_chunks} total chunks):
{sample}

This is a business intelligence assistant. Generate exactly 5 "What" questions about the actual business data above.
ALL 5 questions MUST start with "What ".
Focus on business-relevant insights: counts, values, names, metrics.
Reference actual table names, column names, and values from the data.

Return ONLY: {"suggested_questions":["What ...?","What ...?","What ...?","What ...?","What ...?"]}
"""

DEFAULT_GRAPH_TPL = """Retrieved business data:
{context}

{hist}Chart request: "{question}"

Extract actual numeric/categorical values ONLY from the chunks.
{followup_ins}
Return ONLY:
{{
  "chart_type":"bar"|"line"|"pie"|"scatter",
  "title":"...",
  "labels":[...],
  "datasets":[{{"label":"...","data":[...]}}],
  "source_note":"...",
  "follow_up_questions":["{ftype} ...?","{ftype} ...?","{ftype} ...?"]
}}
"""

DEFAULT_REPORT_TPL = """You are a senior business analyst. Write a comprehensive report from the business data below.
Retrieved data:
{context}

{hist}Report request: "{question}"

Write an analytical business report using ONLY the chunks above.
Be specific — use actual numbers, names, values from the data.
{followup_ins}
Return ONLY:
{{
  "report_title":"...",
  "sections":[{{"heading":"...","content":"..."}}],
  "key_findings":["Finding 1","Finding 2","Finding 3"],
  "follow_up_questions":["{ftype} ...?","{ftype} ...?","{ftype} ...?"]
}}
"""

DEFAULT_ANSWER_TPL = """You are an advanced business intelligence AI specialized in analyzing actual business database records.
This is NOT a general chatbot. Every answer must be grounded in the business data provided below.

Retrieved data:
{context}

{hist}Business Question: "{question}"

Detected intent: {intent}
Relevant tables: {table_hints}

{multi_hint}
DEEP ANALYSIS PROTOCOL:
1. Exhaustively scan every piece of data.
2. If SQL execution results are present, SQL Results are the ONLY source of truth.
3. Answer strictly from SQL Results and retrieved context. Never invent or hallucinate numbers, names, or metrics not present.
4. If a field is not present in SQL Results or context, state that the information is not available.
5. If SQL Results exist, ignore any conflicting retrieved chunks.
6. Do NOT include "(source:...)" or "[source:...]" tags in the answer text.
7. {followup_ins}
8. ENTITY NAMES & CODES: When discussing entities (customers, dealers, products, territories), ALWAYS present the verified name alongside the code (e.g. "Alok Enterprises (Customer Code: 102425)"). NEVER refer to an entity solely by its numeric code if a customer_name, dealer_name, or product_name is available in the SQL results.

VISUALIZATION RULES:
If the question involves comparison, distribution, ranking, trends, or category breakdown, generate up to 3 visualizations in the "visualizations" array.
CRITICAL: For entity rankings/breakdowns (customers, dealers, products, categories), ALWAYS use the human-readable entity name (e.g. "customer_name", "product_name") as the xKey and display label in charts, NOT bare numeric codes! Ensure both name and code are present in each data object.
Supported types:
1. Table:
   {"type":"table", "title":"...", "columns":[{"key":"colKey","label":"Col Label"}], "data":[{"colKey":"value"}]}
2. Bar Chart:
   {"type":"bar_chart", "title":"...", "xKey":"customer_name", "yKey":"total_sales", "data":[{"customer_name":"ABC Enterprises","customer_code":"102425","total_sales":100}]}
3. Line Chart (for trends over time):
   {"type":"line_chart", "title":"...", "xKey":"month", "yKey":"value", "seriesKey":"series", "data":[{"series":"Radial","month":"Jan","value":100}]}

Return ONLY valid JSON (answer must be a markdown string):
{
  "answer": "...",
  "follow_up_questions": [],
  "visualizations": []
}
"""


# ══════════════════════════════════════════════════════
# SCHEMA GROUNDING — column ownership index + join keys
# ══════════════════════════════════════════════════════
# The SQL model used to *guess* which table a column lived on (e.g. it would
# write `invoice`.`construction` when `construction` actually only exists on a
# dimension table), causing MySQL error 1054. These helpers turn that guess
# into a lookup: we parse the real [SCHEMA] lines and tell the model exactly
# which table owns each column, plus the likely join keys to pull a column in
# from another table. Data-driven, so it works for any schema.

# Generic/structural names that are NOT meaningful join keys even if shared.
_GENERIC_JOIN_COLS = {
    "id", "sl", "sno", "srno", "sr", "no", "index", "idx", "row", "rownum",
    "date", "created_at", "updated_at", "createddate", "timestamp", "ts",
    "month", "year", "day", "status", "type", "name", "description", "value",
}


def _parse_schema_chunks(schema_chunks):
    """Return {table_name: [original_col, ...]} parsed from the [SCHEMA] lines.

    The [SCHEMA] line lists the FULL column set (it is built from row.keys()),
    so it is authoritative. As a safety net we also harvest column names from
    the '[COUNT] ... table:T' / 'All values of C in T:' lines, so a table is
    still indexed even if its [SCHEMA] line is somehow missing from context."""
    table_cols = {}

    def _add(table, cols):
        if not table:
            return
        existing = table_cols.setdefault(table, [])
        seen = {c.lower() for c in existing}
        for c in cols:
            if c and c.lower() not in seen:
                existing.append(c)
                seen.add(c.lower())

    for text in schema_chunks:
        # current table context for "All values of C in T" lines
        for line in str(text).splitlines():
            if "[SCHEMA]" in line:
                m_tbl = re.search(r'(?:table|sheet):(\S+)', line)
                m_cols = re.search(r'columns:(.*?)\s+(?:total_rows|rows):', line)
                if not m_cols:
                    m_cols = re.search(r'columns:(.+)$', line)
                if m_tbl and m_cols:
                    cols = [c.strip() for c in m_cols.group(1).split(',') if c.strip()]
                    _add(m_tbl.group(1), cols)
            else:
                # Fallback: "All values of <col> in <table>: ..."
                mv = re.search(r'All values of (.+?) in (\S+?):', line)
                if mv:
                    _add(mv.group(2), [mv.group(1).strip()])
    return table_cols


def _build_schema_grounding(schema_chunks):
    """Build an authoritative column-location index + join-key hints string
    that is injected into the SQL prompt so the model never mis-attributes a
    column to the wrong table."""
    table_cols = _parse_schema_chunks(schema_chunks)
    if not table_cols:
        return "", {}

    # column (lowercased) -> set of (table, original_col)
    col_to_tables = {}
    for table, cols in table_cols.items():
        for c in cols:
            col_to_tables.setdefault(c.lower(), set()).add((table, c))

    lines = [
        "COLUMN LOCATION INDEX (authoritative). A column may be referenced ONLY "
        "on a table whose list below includes it. If a column you need is not in "
        "the table you are selecting FROM, JOIN the table that owns it.",
    ]
    for table, cols in table_cols.items():
        lines.append(f"- `{table}` owns: " + ", ".join(f"`{c}`" for c in cols))

    # Columns unique to one table are always safe to reference there.
    # Columns shared across tables are the likely join keys.
    join_hints = set()
    for col, locs in col_to_tables.items():
        tabs = sorted({t for t, _ in locs})
        if col in _GENERIC_JOIN_COLS or len(tabs) < 2 or len(tabs) > 4:
            continue
        for i in range(len(tabs)):
            for j in range(i + 1, len(tabs)):
                a, b = tabs[i], tabs[j]
                ca = next(oc for (t, oc) in locs if t == a)
                cb = next(oc for (t, oc) in locs if t == b)
                join_hints.add(f"- `{a}`.`{ca}` = `{b}`.`{cb}`")

    out = "\n".join(lines)
    if join_hints:
        out += ("\n\nLIKELY JOIN KEYS (shared columns — use these to bring a "
                "column in from another table):\n" + "\n".join(sorted(join_hints)))
    return out, col_to_tables


def _unknown_column_hint(error_msg, col_to_tables):
    """If a MySQL 1054 'Unknown column X.Y' error occurred, return a targeted
    correction telling the model which table actually owns column Y."""
    if not col_to_tables:
        return ""
    m = re.search(r"Unknown column '([^']+)'", str(error_msg))
    if not m:
        return ""
    ref = m.group(1)
    col = ref.split(".")[-1].strip("`")            # T.C or just C
    owners = sorted({t for t, _ in col_to_tables.get(col.lower(), set())})
    if not owners:
        return (f"\n\nIMPORTANT: column `{col}` does not exist anywhere in the "
                f"schema. Do not reference it; use only columns from the COLUMN "
                f"LOCATION INDEX.")
    owner_list = ", ".join(f"`{t}`" for t in owners)
    return (f"\n\nIMPORTANT: column `{col}` does NOT exist on the table you "
            f"referenced it on. It exists ONLY on: {owner_list}. Reference "
            f"`{col}` on one of those tables, JOINing it in via a LIKELY JOIN "
            f"KEY if needed.")


def _validate_column_refs(sql, table_cols):
    """Statically check a generated SQL string for column references that point
    at the WRONG base table (the cause of MySQL 1054). Returns a de-duplicated
    list of (qualifier, column) violations.

    Only references whose qualifier is a KNOWN BASE TABLE are checked. CTE names
    and short aliases (wd, cs, i, d, ...) are NOT base tables, so references like
    `wd`.`dealer` are correctly ignored — we cannot and should not validate
    derived columns."""
    if not sql or not table_cols:
        return []
    lower_cols = {t: {c.lower() for c in cols} for t, cols in table_cols.items()}
    base_tables = set(table_cols.keys())
    violations = {}

    # Backticked  `table`.`column`
    for m in re.finditer(r"`([^`]+)`\s*\.\s*`([^`]+)`", sql):
        q, c = m.group(1), m.group(2)
        if q in base_tables and c.lower() not in lower_cols[q]:
            violations[(q, c)] = True

    # Unbackticked  table.column  (qualifier still must be a real base table)
    for m in re.finditer(r"\b(\w+)\s*\.\s*(\w+)\b", sql):
        q, c = m.group(1), m.group(2)
        if q in base_tables and c.lower() not in lower_cols[q]:
            violations[(q, c)] = True

    return list(violations.keys())
# ─────────────────────────────────────────────
# VISUALIZATION SUPPORT
# ─────────────────────────────────────────────

def _normalize_visualizations(viz_list):
    if not isinstance(viz_list, list):
        return []
    normalized = []
    for v in viz_list:
        if not isinstance(v, dict): continue
        vtype = str(v.get("type","")).lower()
        if vtype in ("bar","barchart","bar-chart"):   vtype = "bar_chart"
        elif vtype in ("line","linechart"):            vtype = "line_chart"
        elif vtype in ("pie","piechart"):              vtype = "pie_chart"
        elif vtype in ("table","grid"):                vtype = "table"
        item = {"type": vtype, "title": v.get("title","")}
        if vtype in ("bar_chart","line_chart"):
            item["xKey"] = v.get("xKey","")
            item["yKey"] = v.get("yKey","")
            item["data"] = v.get("data",[])
        elif vtype == "pie_chart":
            item["data"] = v.get("data",[])
        elif vtype == "table":
            item["columns"] = v.get("columns",[])
            item["data"]    = v.get("data",[])
        normalized.append(item)
    return normalized


def _column_ref_correction(violations, col_to_tables):
    """Build a forceful correction message naming each wrong reference and the
    table that actually owns the column."""
    lines = []
    for (q, c) in violations:
        owners = sorted({t for t, _ in col_to_tables.get(c.lower(), set())})
        if owners:
            owner_list = ", ".join(f"`{o}`" for o in owners)
            lines.append(
                f"- `{q}`.`{c}` is INVALID: `{c}` is NOT a column of `{q}`. "
                f"`{c}` exists ONLY on {owner_list}. JOIN that table using a "
                f"LIKELY JOIN KEY and reference `{c}` there."
            )
        else:
            lines.append(
                f"- `{q}`.`{c}` is INVALID: column `{c}` does not exist in any "
                f"table. Remove it / use only columns from the COLUMN LOCATION INDEX."
            )
    return ("Your previous SQL referenced columns on the WRONG table. This will "
            "fail with error 1054. Fix EVERY issue below and return the same JSON "
            "shape:\n" + "\n".join(lines))


# ══════════════════════════════════════════════════════
# BUSINESS SCHEMA MAP  (drill-down + correct dimension joins + word→column)
# ══════════════════════════════════════════════════════
# Fixes three failure modes that produced wrong numbers:
#   1. Product attributes (CATEGORY/CONSTRUCTION/vehicle type) were joined from
#      the WRONG table (a customer-keyed dealer table), causing fan-out and
#      inflated sums. We pin them to the real product dimension joined on
#      Material, and forbid any other source.
#   2. "vehicle" / "vehicle type" was mapped to CATEGORY. We map words to the
#      exact column.
#   3. Drill-down: "top 3 tyre categories" => WHERE CATEGORY='Tyre' GROUP BY the
#      next level (CONSTRUCTION).
#   4. [PATCH] region/zone were falsely claimed as customer_master columns and
#      were resolvable via an ungoverned fallback scan straight to
#      region_master with no join path — this produced `cm.zone` in generated
#      SQL (MySQL error 1054). Geography is now handled by its own dedicated
#      GEOGRAPHY_CHAIN + _build_geography_prompt(), never by DIMENSIONS or the
#      generic synonym fallback.
#
# IMPORTANT: edit the CONFIG below to match your real schema. Tables are matched
# by the columns they contain (robust to munged table names), so you usually
# only need to keep the column/measure names correct.

# ── DYNAMIC CONFIG (Fetched from DB via workspace_config) ───────────────────
# Defaults are used if the workspace_config prompt is not found or is invalid JSON.
# ── END DYNAMIC CONFIG ────────────────────────────────────────────────────────
# ── CONFIG ───────────────────────────────────────────────────────────────────
FACT_TABLE_HINTS = ["sales_data", "invoice"]          # name substring(s) of the fact table
MEASURE_COLUMN   = "Invoice_Value_INR"      # the Sales measure column on the fact table
DATE_COLUMN      = "billing__doc_date"      # the Date column on the fact table used for year/month filtering

# Dimensions: each is auto-located as the (non-fact) table that contains its
# `dim_key` AND the most of its `owns` columns. `fact_key` is the column on the
# fact table that joins to `dim_key` on the dimension.
DIMENSIONS = [
    {
        "label":    "product",
        "fact_key": "material",
        "dim_key":  "MATNR",
        "owns":     ["category", "tyre_type", "construction", "MAKTX", "PROD_TITLE"],
        # [PATCH] NOTE: sku_master.construction is typed BIGINT in the live
        # schema, so alphabetic construction_master codes
        # (A, B, D, E, K, L, M, N, O, P, R, T, Z) can never match. This is a
        # data-load defect upstream, not a mapping bug in this file.
    },
    {
        "label":    "customer",
        "fact_key": "customer",
        "dim_key":  "KUNNR",
        # [PATCH] "region", "zone" REMOVED — customer_master does NOT own
        # them. They live two joins away (territory_master -> region_master).
        # Claiming them here let the synonym resolver treat them as reachable
        # in a single hop, which produced the `cm.zone` bug. See
        # GEOGRAPHY_CHAIN below, which is the only place region/zone resolve.
        "owns":     ["Cname", "acc_grp", "class", "territory", "sales_office"],
    },
]

# [PATCH — NEW] Multi-hop geography chain. `_build_business_map`'s DIMENSIONS
# only support a single fact_key/dim_key hop, but region/zone require
# customer_master -> territory_master -> region_master (two hops). Resolved
# and injected separately by _build_geography_prompt(), never folded into
# DIMENSIONS or COLUMN_SYNONYMS.
GEOGRAPHY_CHAIN = {
    "customer_fk":     "territory",          # customer_master.territory (bigint)
    "territory_table": "territory_master",
    "territory_key":   "territory_code",     # text
    "territory_owns":  ["territory_name"],
    "region_fk":       "region_code",        # territory_master.region_code (text)
    "region_table":    "region_master",
    "region_key":       "region",            # bigint
    "region_owns":      ["region_name", "zone"],
}

# Product hierarchy ROOT → LEAF (column names; matched case/space/underscore-insensitively)
PRODUCT_HIERARCHY = ["category", "tyre_type", "construction", "MAKTX"]

# Natural-language phrase → exact column name. Longest phrase wins.
COLUMN_SYNONYMS = {
    "product category": "category", "category": "category", "categories": "category",
    "construction": "construction", "tyre type": "tyre_type", "tire type": "tyre_type",
    "vehicle": "category", "by vehicle": "category",
    "customer": "Cname",
    # [PATCH] "region": "region", "zone": "zone" REMOVED. Leaving them here let
    # the fallback scan resolve them to `region_master` directly, with no
    # join path — that is exactly what produced `cm.zone` in generated SQL.
    # Geography wording is now handled exclusively by GEOGRAPHY_CHAIN /
    # _build_geography_prompt(), which enforces the mandatory 2-hop join.
}

# Explicit definitions for complex business entities that require specific joins, filters, and grouping.
ENTITY_DEFINITIONS = {
    "dealer": "If the user asks for 'dealer(s)', you MUST JOIN `customer_master` and `account_group_master` (ON `customer_master`.`acc_grp` = `account_group_master`.`KTOKD`), FILTER BY `account_group_master`.`account_group_name` = 'Dealer', and GROUP BY `customer_master`.`KUNNR`, `customer_master`.`Cname`.",
    # [PATCH] class_master's real value for code DB is 'DISTRIBUTOR' (all
    # caps) — matched case-insensitively now so this never silently returns
    # zero rows depending on collation.
    "distributor": "If the user asks for 'distributor(s)', you MUST JOIN `customer_master` and `class_master` (ON `customer_master`.`class` = `class_master`.`class_code`), FILTER BY UPPER(`class_master`.`class_name`) = 'DISTRIBUTOR', and GROUP BY `customer_master`.`KUNNR`, `customer_master`.`Cname`. Do NOT use `distribution_mapping` for distributors.",
    "fleet": "If the user asks for 'fleet(s)', you MUST JOIN `customer_master` and FILTER BY `customer_master`.`acc_grp` = 'Z009', and GROUP BY `customer_master`.`KUNNR`, `customer_master`.`Cname`.",
}
# ── END CONFIG ───────────────────────────────────────────────────────────────

# Words that signal the user wants a ranked breakdown (so we apply the GROUP BY).
_RANK_OR_BREAKDOWN_RE = re.compile(
    r'\b(top|bottom|best|worst|highest|lowest|leading|poor|performing|rank|'
    r'ranking|wise|breakdown|break\s*down|split|distribution|each|per|'
    r'types?|kinds?|categor(?:y|ies)|segments?|variants?|constructions?)\b', re.I)


def _norm_ident(s):
    return re.sub(r'[\s_]+', '', str(s).lower())


def _parse_value_index(schema_chunks):
    """From '[COUNT] ...' lines 'All values of <col> in <table>: v1, v2, ...'
    return {(table, col): [distinct original values]}."""
    out = {}
    for text in schema_chunks:
        for line in str(text).splitlines():
            m = re.search(r'All values of (.+?) in (\S+?):\s*(.+)$', line)
            if not m:
                continue
            col, table, rest = m.group(1).strip(), m.group(2).strip(), m.group(3)
            vals = [v.strip() for v in rest.split(',') if v.strip()]
            if not vals:
                continue
            bucket = out.setdefault((table, col), [])
            seen = {v.lower() for v in bucket}
            for v in vals:
                if v.lower() not in seen:
                    bucket.append(v)
                    seen.add(v.lower())
    return out


def _orig_col(table_cols, table, col_name):
    """Return the original-cased column name on `table` matching col_name."""
    for c in table_cols.get(table, []):
        if _norm_ident(c) == _norm_ident(col_name):
            return c
    return None


def _build_business_map(table_cols, workspace_id):
    """Resolve the CONFIG against the actually-loaded schema. Returns a dict with
    the fact table, each dimension's real table + join keys, the column→source map
    (so product attributes are pinned to the product table), the resolved synonym
    map, and the ordered hierarchy levels as (table, col)."""
    
    import json
    from database.prompt_loader import get_prompt
    
    # Defaults (Fallback if no config found)
    FACT_TABLE_HINTS = ["currentsales", "zpvf_sd_currentsales_01_tr", "invoice", "sales", "saleshistory", "zpvf_sd_saleshistory_01_tr"]
    MEASURE_COLUMN   = "invoice_value"
    DIMENSIONS = [
        {
            "label":    "product",
            "fact_key": "SKU",
            "dim_key":  "MATNR",
            "owns":     ["CAT_CODE", "CATG", "CONSTUCTION", "TYRE_TYPE", "BRAND", "MAKTX", "CATEGORY", "CONSTRUCTION", "vehicle type"],
        },
        {
            "label":    "customer",
            "fact_key": "dealer_code",
            "dim_key":  "KUNNR",
            "owns":     ["NAME1", "BNAME", "BZIRK", "REGIO", "KLABC", "DLR_CATG", "Customer", "Cname"],
        },
        {
            "label":    "territory",
            "fact_key": "BZIRK",
            "dim_key":  "BZIRK",
            "owns":     ["NAME", "AREA", "P_ZONE", "REGIO"],
        },
        {
            "label":    "category",
            "fact_key": "CAT_CODE",
            "dim_key":  "CAT_CODE",
            "owns":     ["CAT_DESC", "SHORT_DESC"],
        },
        {
            "label":    "date",
            "fact_key": "invoice_date",
            "dim_key":  "Date_",
            "owns":     ["WEEK", "YMONTH", "QTR", "FIN_YEAR", "CAL_YEAR"],
        }
    ]
    PRODUCT_HIERARCHY = ["CAT_DESC", "CONSTUCTION", "TYRE_TYPE", "BRAND", "MAKTX", "CATEGORY", "CONSTRUCTION", "vehicle type"]
    COLUMN_SYNONYMS = {
        "product category": "CAT_DESC", "category": "CAT_DESC", "categories": "CAT_DESC", "category name": "CAT_DESC",
        "construction": "CONSTUCTION", "tyre type": "CONSTUCTION", "tire type": "CONSTUCTION",
        "radial": "CONSTUCTION", "bias": "CONSTUCTION",
        "brand": "BRAND", "product": "MAKTX", "product name": "MAKTX", "product description": "MAKTX",
        "material": "MATNR", "material description": "MAKTX", "sku": "SKU",
        "region": "region_name", "region name": "region_name", "zone": "P_ZONE", "zone name": "NAME",
        "territory": "NAME", "territory name": "NAME",
        "dealer": "NAME1", "customer": "NAME1", "dealer name": "NAME1", "customer name": "NAME1",
        "dealer code": "dealer_code", "customer code": "KUNNR",
        "month": "YMONTH", "year": "CAL_YEAR", "quarter": "QTR",
        "sales": "invoice_value", "revenue": "invoice_value", "volume": "Tyre_Quantity",
    }
    
    # Fetch from database
    config_json = get_prompt(workspace_id, 'workspace_config')
    if config_json and config_json.strip():
        try:
            cfg = json.loads(config_json)
            FACT_TABLE_HINTS = cfg.get("fact_table_hints", FACT_TABLE_HINTS)
            MEASURE_COLUMN = cfg.get("measure_column", MEASURE_COLUMN)
            DIMENSIONS = cfg.get("dimensions", DIMENSIONS)
            PRODUCT_HIERARCHY = cfg.get("product_hierarchy", PRODUCT_HIERARCHY)
            COLUMN_SYNONYMS = cfg.get("column_synonyms", COLUMN_SYNONYMS)
        except Exception as e:
            print(f"[RAG] Error parsing workspace_config JSON: {e}")

    norm_tables = {t: {_norm_ident(c) for c in cols} for t, cols in table_cols.items()}

    # Fact table: name hint match, else the table that has the measure column.
    fact = None
    for t in table_cols:
        if any(h in t.lower() for h in FACT_TABLE_HINTS):
            fact = t
            break
    if not fact:
        for t, ncols in norm_tables.items():
            if _norm_ident(MEASURE_COLUMN) in ncols:
                fact = t
                break

    # Locate each dimension as the non-fact table containing dim_key + most owns.
    dim_resolved = []          # list of {table, fact_key, dim_key, owns:[orig...]}
    attr_source = {}           # norm(col) -> (table, orig_col, fact_key, dim_key)
    for d in DIMENSIONS:
        dk = _norm_ident(d["dim_key"])
        owns_norm = [_norm_ident(c) for c in d["owns"]]
        best, best_score = None, -1
        for t, ncols in norm_tables.items():
            if t == fact or dk not in ncols:
                continue
            score = sum(1 for c in owns_norm if c in ncols)
            if score <= 0:
                continue
            # Prefer a leaner table (true dimension) on ties.
            score = score * 100 - len(ncols)
            if any(h in t.lower() for h in [d["label"]]):
                score += 50
            if score > best_score:
                best, best_score = t, score
        if not best:
            continue
        owns_present = [_orig_col(table_cols, best, c) for c in d["owns"]
                        if _orig_col(table_cols, best, c)]
        fk = _orig_col(table_cols, fact, d["fact_key"]) or d["fact_key"]
        dkey = _orig_col(table_cols, best, d["dim_key"]) or d["dim_key"]
        dim_resolved.append({"table": best, "fact_key": fk, "dim_key": dkey,
                             "owns": owns_present, "label": d["label"]})
        for oc in owns_present:
            attr_source[_norm_ident(oc)] = (best, oc, fk, dkey)

    # Hierarchy levels resolved to (table, orig_col), pinned to their source table.
    levels = []
    for h in PRODUCT_HIERARCHY:
        nh = _norm_ident(h)
        if nh in attr_source:
            t, oc, _, _ = attr_source[nh]
            levels.append((t, oc))
        elif fact and nh in norm_tables.get(fact, set()):
            levels.append((fact, _orig_col(table_cols, fact, h)))

    # Resolve synonyms to real (table, col): prefer a pinned source, else fact.
    def _plurals(p):
        out = {p}
        out.add(p + "s")
        if p.endswith("y"):
            out.add(p[:-1] + "ies")
        return out
    syn_resolved = {}
    for phrase, colname in COLUMN_SYNONYMS.items():
        nc = _norm_ident(colname)
        loc = None
        if nc in attr_source:
            t, oc, _, _ = attr_source[nc]
            loc = (t, oc)
        elif fact and nc in norm_tables.get(fact, set()):
            loc = (fact, _orig_col(table_cols, fact, colname))

        if not loc:
            # [PATCH] Restricted fallback: only resolve to a table this map
            # already knows how to JOIN to (a dimension table already
            # resolved above, or the two geography-chain tables). Previously
            # this scanned EVERY table with no join-path check, which let
            # "region"/"zone" resolve straight to region_master with no way
            # to reach it — the root cause of the `cm.zone` bug.
            reachable = {d["table"] for d in dim_resolved}
            reachable.add(GEOGRAPHY_CHAIN["territory_table"])
            reachable.add(GEOGRAPHY_CHAIN["region_table"])
            for tbl, cols in norm_tables.items():
                if tbl in reachable and nc in cols:
                    loc = (tbl, _orig_col(table_cols, tbl, colname))
                    break

        if loc:
            for variant in _plurals(phrase.lower()):
                syn_resolved.setdefault(variant, loc)

    measure = _orig_col(table_cols, fact, MEASURE_COLUMN) if fact else None

    date_col = _orig_col(table_cols, fact, DATE_COLUMN) if fact else None

    return {
        "fact": fact, "measure": measure or MEASURE_COLUMN, "date_column": date_col or DATE_COLUMN,
        "dims": dim_resolved, "attr_source": attr_source,
        "synonyms": syn_resolved, "levels": levels,
        "entity_definitions": ENTITY_DEFINITIONS,
    }


def _business_prompt(biz):
    """Authoritative instruction block: exact dimension joins, the measure, the
    forbidden sources, and word→column mapping."""
    if not biz.get("fact"):
        return ""
    fact = biz["fact"]
    out = [f"AUTHORITATIVE SCHEMA MAP (follow EXACTLY — overrides any guess):",
           f"- Fact table: `{fact}`. Sales / performance / revenue = "
           f"SUM(`{fact}`.`{biz['measure']}`).",
           f"- Date filtering: If the user mentions a year (e.g., '2026') or date, you MUST apply a WHERE clause using `{fact}`.`{biz['date_column']}` (e.g. YEAR({fact}.{biz['date_column']}) = 2026). Do NOT use STR_TO_DATE; assume the column is already a proper DATE type."]
    forbid = []
    for d in biz["dims"]:
        if not d["owns"]:
            continue
        cols = ", ".join(f"`{c}`" for c in d["owns"])
        out.append(
            f"- {cols} live ONLY on `{d['table']}`. To use any of them you MUST "
            f"JOIN `{d['table']}` ON `{fact}`.`{d['fact_key']}` = "
            f"`{d['table']}`.`{d['dim_key']}`. NEVER read these columns from any "
            f"other table, and NEVER join them on a different key (doing so "
            f"multiplies rows and inflates the totals).")
        forbid.append(f"`{d['table']}` only via `{d['fact_key']}`")
    if biz["synonyms"]:
        # Compact, de-duplicated word→column list
        seen = {}
        for phrase, (t, c) in biz["synonyms"].items():
            seen.setdefault((t, c), []).append(phrase)
        word_lines = []
        for (t, c), phrases in seen.items():
            ph = ", ".join(f'"{p}"' for p in sorted(set(phrases), key=len))
            word_lines.append(f"  {ph} -> `{c}` (on `{t}`)")
        out.append("WORD -> COLUMN (map the user's wording to the EXACT column):\n"
                   + "\n".join(word_lines))
    out.append("FAN-OUT GUARD: every dimension JOIN must be on the key above so "
               "each fact row matches at most one dimension row. If a column name "
               "exists on more than one table, use the table named in this map.")
    
    if biz.get("entity_definitions"):
        out.append("\nBUSINESS ENTITY DEFINITIONS (Strictly follow these rules if the user mentions these entities):")
        for entity, rule in biz["entity_definitions"].items():
            out.append(f"- {rule}")
            
    return "\n".join(out)


# [PATCH — NEW] Emits the mandatory 2-hop geography join chain. This is what
# actually fixes the `cm.zone` / Unknown column bug — region/zone are simply
# never reachable in one join, and the model has no way to know that without
# being told explicitly, table by table, with the correct CAST direction.
def _build_geography_prompt(table_cols):
    """Emits a mandatory join chain for region/zone in SAP HANA or MySQL."""
    # Check if SAP S/4HANA CDS views are present in table_cols
    s4_cust = next((t for t in table_cols if "customermaster" in t.lower()), None)
    s4_terr = next((t for t in table_cols if "territorymaster" in t.lower()), None)
    s4_reg  = next((t for t in table_cols if "regionmaster" in t.lower()), None)
    s4_zone = next((t for t in table_cols if "zonemaster" in t.lower()), None)
    s4_tgt  = next((t for t in table_cols if "plan_target" in t.lower() or "sales_plan" in t.lower()), None)

    if s4_cust and s4_terr:
        reg_join = f"- Region Master: LEFT JOIN \"SAP_S4\".\"{s4_reg}\" rm ON (tm.REGIO = rm.\"region_code\" OR cm.REGIO = rm.\"region_code\") (Region Name: rm.\"region_name\", Zone code: rm.\"zone_code\")\n" if s4_reg else ""
        zone_on = "rm.\"zone_code\" = zm.P_ZONE" if s4_reg else "tm.P_ZONE = zm.P_ZONE"
        zone_join = f"- Zone Master: LEFT JOIN \"SAP_S4\".\"{s4_zone}\" zm ON {zone_on} (Zone Name: zm.NAME)\n" if s4_zone else ""
        tgt_hint = (
            f"- Sales Plan & Target: \"SAP_S4\".\"{s4_tgt}\" tgt (Join keys: tgt.TERR_CODE = tm.BZIRK, tgt.REG_CODE = rm.\"region_code\")\n"
            f"  CRITICAL FOR TARGET VS ACTUAL: Use separate CTEs for actual sales and target plan, never direct JOIN.\n"
        ) if s4_tgt else ""
        return (
            f"\n\nSAP S/4HANA GEOGRAPHY CHAIN (MANDATORY):\n"
            f"- Customer Master: \"SAP_S4\".\"{s4_cust}\" cm (JOIN: (fact.\"dealer_code\" = cm.KUNNR OR LTRIM(fact.\"dealer_code\", '0') = LTRIM(cm.KUNNR, '0')), Customer Name: COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact.\"dealer_code\"), FILTER: (cm.KLABC IS NULL OR cm.KLABC <> 'X'))\n"
            f"- Territory Master: LEFT JOIN \"SAP_S4\".\"{s4_terr}\" tm ON cm.BZIRK = tm.BZIRK (Territory Name: tm.NAME, Region code: tm.REGIO, Zone code: tm.P_ZONE)\n"
            f"{reg_join}"
            f"{zone_join}"
            f"{tgt_hint}"
            f"Reference columns ONLY on their owning view: Customer name is cm.NAME1 (fallback cm.BNAME), Territory name is tm.NAME, Region name is rm.\"region_name\", Zone name is zm.NAME.\n"
            f"CRITICAL: Always use LEFT JOIN for Territory, Region, and Zone Master so customers with unassigned territories are never dropped.\n"
            f"CRITICAL STRING FILTER RULE: String matching in SAP HANA is case-sensitive! Always use UPPER(TRIM(col)) LIKE UPPER('%value%') (e.g. UPPER(TRIM(rm.\"region_name\")) LIKE UPPER('%Jaipur%')) instead of strict equality."
        )

    g = GEOGRAPHY_CHAIN
    norm_tables = {t: {_norm_ident(c) for c in cols} for t, cols in table_cols.items()}

    cust_tbl = None
    for t, ncols in norm_tables.items():
        if "customer" in t.lower() and _norm_ident(g["customer_fk"]) in ncols:
            cust_tbl = t
            break
    if not cust_tbl or g["territory_table"] not in table_cols or g["region_table"] not in table_cols:
        return ""

    return (
        f"\n\nGEOGRAPHY CHAIN (MANDATORY — region/zone are NEVER columns on "
        f"`{cust_tbl}` or the fact table; both joins below are required, in order):\n"
        f"1. JOIN `{g['territory_table']}` ON "
        f"CAST(`{cust_tbl}`.`{g['customer_fk']}` AS CHAR) = `{g['territory_table']}`.`{g['territory_key']}`\n"
        f"2. JOIN `{g['region_table']}` ON "
        f"`{g['territory_table']}`.`{g['region_fk']}` = CAST(`{g['region_table']}`.`{g['region_key']}` AS CHAR)\n"
        f"   -- always CAST the BIGINT side to CHAR; never CAST the text side to "
        f"UNSIGNED (silently coerces non-numeric values to 0 and breaks the join).\n"
        f"`region`, `zone`, `region_name` exist ONLY on `{g['region_table']}`, reachable "
        f"only after both joins above. `{cust_tbl}` and any alias of it (e.g. `cm`) "
        f"NEVER owns `zone` or `region` — referencing them there is INVALID."
    )


def _detect_group_columns(question, biz):
    """Map the user's wording to the column(s) they want grouped, via synonyms.
    Longest matching phrase wins on any overlapping span, so 'vehicle categories'
    resolves to `vehicle type` and suppresses the bare 'categories'→CATEGORY."""
    ql = question.lower()
    candidates = []  # (start, end, table, col, phrase)
    for phrase, (t, c) in biz.get("synonyms", {}).items():
        for m in re.finditer(r'\b' + re.escape(phrase) + r'\b', ql):
            candidates.append((m.start(), m.end(), t, c, phrase))
    # Longest phrase first; greedily accept non-overlapping spans.
    candidates.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    taken, out, seen = [], [], set()
    for s, e, t, c, p in candidates:
        if any(not (e <= ts or s >= te) for ts, te in taken):
            continue  # overlaps an already-accepted (longer) phrase
        taken.append((s, e))
        if (t, c) not in seen:
            seen.add((t, c))
            out.append((t, c, p))
    return out


def _detect_drilldown(question, values_by_col, biz):
    """If the question names a value at some hierarchy level (e.g. 'tyre'),
    return {'filter': (table,col,value), 'group': (table,col)|None}."""
    levels = biz.get("levels") or []
    if not levels:
        return None
    ql = question.lower()

    matched = None  # (level_idx, table, col, original_value)
    for idx, (t, c) in enumerate(levels):
        for v in values_by_col.get((t, c), []):
            if len(v) < 2:
                continue
            if re.search(r'\b' + re.escape(v.lower()) + r'\b', ql):
                if matched is None or len(v) > len(matched[3]):
                    matched = (idx, t, c, v)
    if not matched:
        return None
    f_idx, f_t, f_c, f_val = matched

    # Group level: explicit deeper level the user named (via synonyms or name),
    # else the next level down.
    group = None
    named = _detect_group_columns(question, biz)
    for (gt, gc, _ph) in named:
        for idx in range(f_idx + 1, len(levels)):
            if (gt, gc) == levels[idx]:
                group = (gt, gc)
                break
        if group:
            break
    if group is None and f_idx + 1 < len(levels):
        group = levels[f_idx + 1]

    return {"filter": (f_t, f_c, f_val), "group": group}


def _drilldown_hint(spec, want_breakdown, biz):
    f_t, f_c, f_val = spec["filter"]
    fact = biz.get("fact") or "invoice"
    measure = biz.get("measure") or "Invoice_Value"
    s = (f"\n\nHIERARCHY DRILL-DOWN (apply this):\n"
         f"- The question names '{f_val}', a value of `{f_c}` on `{f_t}`. Use it as "
         f"a FILTER: WHERE `{f_t}`.`{f_c}` = '{f_val}' (exact value, case included).")
    grp = spec.get("group")
    if grp and want_breakdown:
        g_t, g_c = grp
        s += (f"\n- Break it down WITHIN '{f_val}': GROUP BY `{g_t}`.`{g_c}` (the "
              f"next level down) and rank by SUM(`{fact}`.`{measure}`) DESC, applying "
              f"the requested Top/Bottom N. Do NOT group by `{f_c}` itself. JOIN the "
              f"dimension table(s) per the AUTHORITATIVE SCHEMA MAP.")
    else:
        s += "\n- Apply this filter; aggregate/rank as the question asks."
    return s


def _grouping_hint(question, biz):
    """For non-drill questions, if the wording names a dimension to break by,
    tell the model exactly which column/table to GROUP BY."""
    named = _detect_group_columns(question, biz)
    if not named or not _RANK_OR_BREAKDOWN_RE.search(question):
        return ""
    fact = biz.get("fact") or "invoice"
    lines = ["\n\nGROUP-BY MAPPING (use these EXACT columns for the breakdown):"]
    for (t, c, ph) in named:
        if t == fact:
            lines.append(f"- '{ph}' -> GROUP BY `{t}`.`{c}`.")
        else:
            lines.append(f"- '{ph}' -> GROUP BY `{t}`.`{c}` (JOIN `{t}` per the "
                         f"AUTHORITATIVE SCHEMA MAP).")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# MAIN CONTROLLER
# ══════════════════════════════════════════════════════
def _safe_visualizations(vizs):
    return [v for v in vizs if isinstance(v,dict) and v.get("type") and v.get("title")]

def _is_unbounded_heavy_comparison(question: str) -> bool:
    """
    Detects if the query is an actual vs target/plan comparison or heavy multi-dimensional
    monthly report that spans all historical data without any time limit or period constraint.
    """
    q = (question or "").lower()
    
    # Check if question asks for actual vs target/plan or multi-dimensional monthly performance
    is_plan_comparison = bool(re.search(
        r'\b(?:actual\s+(?:sales\s+)?(?:vs|against)\s+(?:sales\s+)?plan|target\s+vs\s+actual|actual\s+vs\s+target|achievement\s+percentage|performance\s+report)\b',
        q, re.I
    ))
    
    dim_count = sum(1 for dim in ['region', 'territory', 'category', 'product', 'customer', 'dealer', 'zone', 'sku'] if dim in q)
    is_heavy_monthly = ('monthly' in q or 'by month' in q or 'month-wise' in q or 'month wise' in q) and dim_count >= 2
    
    if not (is_plan_comparison or is_heavy_monthly):
        return False

    # Check if question specifies ANY time range or period constraint
    has_time_limit = bool(re.search(
        r'\b(?:201[89]|202[0-9]|last\s+\d+\s+months?|past\s+\d+\s+months?|last\s+\d+\s+years?|last\s+quarter|past\s+quarter|q[1-4]|fy\s*\d{2,4}|financial\s+year|fiscal\s+year|current\s+year|this\s+year|current\s+month|this\s+month|last\s+month|previous\s+month|mtd|ytd|qtd|today|yesterday|january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b',
        q, re.I
    ))
    
    return not has_time_limit

def session_rag_chat_controller(get_connection_func, status_callback=None):
    data       = request.json or {}
    session_id     = (data.get("session_id")     or "").strip()
    question       = (data.get("question")       or "").strip()
    history        = data.get("chat_history", [])
    user_id        = data.get("user_id")
    visit_number   = data.get("visit_number")
    login_token    = data.get("login_token")
    source_chat_id = (data.get("source_chat_id") or "").strip() or None

    if status_callback:
        status_callback("start")

    if not session_id:
        return jsonify({"status":"failed","statusCode":400,
                        "message":"session_id is required"}), 400

    # if user_id:
    #     from controllers.pricing_controller import get_usage_and_limits
    #     usage_res = get_usage_and_limits(user_id)
    #     if usage_res.get('status') == 'success':
    #         queries_metric = usage_res['usage_stats']['metrics']['queries']
    #         used = queries_metric['used']
    #         total = queries_metric['total']
    #         if total != -1 and used >= total:
    #             return jsonify({"status": "error", "message": "Daily query limit exceeded based on your active plan."}), 403

    # --- FIX: Frontend might pass chat_id as session_id when navigating ---
    try:
        conn_fix = get_connection_func()
        cur_fix = _dict_cursor(conn_fix)
        cur_fix.execute("SELECT id FROM workspaces WHERE session_id = %s", (session_id,))
        if not cur_fix.fetchone():
            cur_fix.execute("SELECT session_id FROM session_chat_history WHERE chat_id = %s LIMIT 1", (session_id,))
            hr = cur_fix.fetchone()
            if hr and hr.get("session_id"):
                if not source_chat_id:
                    source_chat_id = session_id
                session_id = hr["session_id"]
    except Exception:
        pass
    finally:
        if 'cur_fix' in locals() and cur_fix: cur_fix.close()
        if 'conn_fix' in locals() and conn_fix: conn_fix.close()
    # ----------------------------------------------------------------------

    # ── Resolve active chat_id ──────────────────────────────────
    active_chat_id = None
    # Support both bool and string representations
    force_new = str(data.get("is_new_query")).lower() == "true"
    if user_id:
        active_chat_id, _ = _resolve_chat_id(
            get_connection_func, session_id, user_id, source_chat_id, force_new, visit_number
        )

    workspace_id = None
    sql_results = []
    sql_query = ""
    system_prompt = SYS
    try:
        conn = get_connection_func()
        cur = _dict_cursor(conn)

        # 1. Get workspace_id for this session
        cur.execute("SELECT id AS workspace_id FROM workspaces WHERE session_id = %s", (session_id,))
        s_row = cur.fetchone()
        if s_row and s_row.get("workspace_id"):
            workspace_id = s_row["workspace_id"]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[RAG] Session fetch error: {e}")

    # Apply fetched prompts
    system_prompt = get_prompt(workspace_id, 'rag_chat')
    if not system_prompt or not system_prompt.strip():
        system_prompt = SYS

    # Greeting
    if question and _is_greet(question):
        suggested = []
        if session_id in _CACHE:
            chunks, _, _, _ = _CACHE[session_id]
            sample = "\n".join(c["text"] for c in chunks if c["kind"] == "count")[:10000]
            res = _mistral(
                "Respond ONLY in valid JSON.",
                f"Data summary:\n{sample}\n\n"
                "Generate exactly 3 questions. Q1 starts with 'What ', Q2 starts with 'Where ', Q3 starts with 'Why '. "
                "Use actual table names and values from the data. "
                'Return ONLY: {"suggested_questions":["What ...?","Where ...?","Why ...?"]}'
            )
            if res: suggested = res.get("suggested_questions", [])
        return jsonify({
            "status":"success","statusCode":200,
            "answer":"Hi! I'm your advanced business intelligence assistant. I have full access to your session databases. Ask me anything about your business data!",
            "follow_up_questions": suggested,
            "chat_id": active_chat_id
        }), 200

    # Email Action Handler
    if question and is_email_action_request(question):
        email_res = execute_email_action(
            question,
            session_id=session_id,
            user_id=int(user_id) if user_id else 1,
            get_connection_func=get_connection_func
        )
        email_res["visit_number"] = visit_number
        return jsonify(email_res), 200

    # Build/get store
    try:
        all_chunks, bm25_idx, col = _build_store(session_id, get_connection_func)
    except Exception as e:
        return jsonify({"status":"error","statusCode":500,
                        "message":f"Store error: {e}"}), 500

    if not all_chunks:
        return jsonify({"status":"no_data","statusCode":200,
                        "session_id":session_id,
                        "message":"No data found for this session."}), 200

    # Suggest mode
    if not question:
        count_chunks = [c["text"] for c in all_chunks if c["kind"]=="count"]
        sample = "\n".join(count_chunks)[:15000]
        _default_tpl = get_prompt(workspace_id, 'rag_chat_default') or DEFAULT_DEFAULT_TPL
        _default_msg = (
            _default_tpl
            .replace('{sample}', sample)
            .replace('{len_chunks}', str(len(all_chunks)))
        )
        res = _mistral(system_prompt, _default_msg)
        if not res:
            return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
        return jsonify({
            "status":              "success",
            "statusCode":          200,
            "suggested_questions": res.get("suggested_questions",[]),
            "chat_id":             active_chat_id
        }), 200

    # Understand + Retrieve
    if status_callback:
        status_callback("analyzing")
    understanding = _understand(question, all_chunks)
    context       = _retrieve(all_chunks, bm25_idx, col, question, understanding)
    hist          = _history(history)
    print(f"[RAG] intent={understanding['intent']} tables={understanding['table_hints']} entities={understanding['entities']}")

    # NEW ARCHITECTURE: INTENT ROUTER 
    intent = classify_intent(question, workspace_id, get_connection_func())
    print(f"[RAG] Router classified intent: {intent}")
    if status_callback:
        status_callback("retrieving", intent=intent)
    context = ""

    if intent == "HYBRID":
        print("[RAG] HYBRID query detected! AQL/SQL First Then RAG...")
        schema_chunks = [c["text"] for c in all_chunks if c["kind"] in ("schema", "count")]
        schema_context = "\n".join(schema_chunks)
        # 1. AQL First: run execute_hybrid to get specific entity filter
        hybrid_entities = execute_hybrid(question, get_connection_func, schema_context)
        if hybrid_entities:
            print(f"[RAG] Hybrid found targeted entities: {hybrid_entities[:10]}")
            # Inject these entities so Vector Search focuses heavily on them
            understanding["entities"].extend(hybrid_entities)
            
        # 2. Then RAG
        context = _retrieve(all_chunks, bm25_idx, col, question, understanding)

    elif intent == "INSIGHT":
        print("[RAG] INSIGHT query detected! Standard RAG...")
        context = _retrieve(all_chunks, bm25_idx, col, question, understanding)

    # ─────────────────────────────────────────────
    # TEXT-TO-SQL (Bypass VectorDB for aggregations)
    # ─────────────────────────────────────────────
    if intent == "AGGREGATION":
        print(" Bypassing VectorDB. Routing to Structured Database (SQL/AQL)...")

        # Guard: Check for unconstrained heavy multi-dimensional or actual vs plan queries across all history
        if _is_unbounded_heavy_comparison(question):
            print(f"[GUARD] Unbounded heavy comparison detected for question: '{question}' — generating dynamic LLM response & suggestions.")
            
            _guard_sys = (
                "You are an expert enterprise business intelligence advisor. "
                "The user asked an analytical question that requires aggregating massive enterprise transactional data "
                "across multiple dimensions (e.g. regions, territories, categories, products, months) across all historical years without any time constraint. "
                "State politely, clearly, and concisely in 2 sentences that the data volume across all historical years is too large to process, "
                "and ask them to specify a time filter (such as a specific fiscal year, recent months, or a quarter). "
                "Provide 3 tailored, highly relevant follow-up questions specifically adapted to the user's exact business question with concrete time periods "
                "(e.g., 'for FY 2024', 'for last 6 months', 'for current year', etc.).\n"
                "Respond ONLY in valid JSON matching this exact structure:\n"
                "{\n"
                '  "message": "Polite explanation and request for time filter...",\n'
                '  "suggestions": ["Follow-up question 1 with time filter", "Follow-up question 2 with time filter", "Follow-up question 3 with time filter"]\n'
                "}"
            )
            _guard_user = f"User Question: \"{question}\""
            _guard_res = _mistral(_guard_sys, _guard_user, temperature=0.2)

            if _guard_res and isinstance(_guard_res, dict) and _guard_res.get("message"):
                _heavy_msg = _guard_res.get("message")
                raw_sug = _guard_res.get("suggestions", [])
                fuq = [str(s) for s in raw_sug if s]
            else:
                _heavy_msg = (
                    "The data volume is too high to generate an all-time report across all regions, territories, and product categories.\n\n"
                    "Please specify a time range or period in your question, such as:\n"
                    "- **\"for 2024\"** (or **\"for FY 2024\"**)\n"
                    "- **\"for last 6 months\"**\n"
                    "- **\"for current year\"**"
                )
                fuq = [
                    f"{question.rstrip('.')} for 2024",
                    f"{question.rstrip('.')} for last 6 months",
                    f"{question.rstrip('.')} for current year"
                ]

            _advance_turn(session_id)
            actual_visit = _save_history(
                get_connection_func, session_id, user_id,
                question, _heavy_msg, fuq, understanding.get("intent", "AGGREGATION"), "answer",
                login_token=login_token, visualizations=[], chat_id=active_chat_id
            )
            return jsonify({
                "status":              "success",
                "statusCode":          200,
                "answer":              _heavy_msg,
                "sql_query":           "",
                "follow_up_questions": fuq,
                "visualizations":      [],
                "chat_id":             active_chat_id,
                "visit_number":        actual_visit
            }), 200
        schema_chunks = [c["text"] for c in all_chunks if c["kind"] in ("schema", "count")]
        schema_context = "\n".join(schema_chunks)

        # ─────────────────────────────────────────────
        # 1. CANONICALIZATION STEP
        # ─────────────────────────────────────────────
        canon_sys = get_prompt(workspace_id, 'canonicalization')
        if not canon_sys or not canon_sys.strip():
            canon_sys = DEFAULT_CANON_SYS
        canon_json = _mistral(canon_sys, f"Natural Language Question: {question}", temperature=0.0)
        canonical_query_str = json.dumps(canon_json, indent=2) if canon_json else f'{{"raw_question": "{question}"}}'
        print(f"[CANONICAL_QUERY] {canonical_query_str}")

        # ─────────────────────────────────────────────
        # 2. SQL GENERATION STEP
        # ─────────────────────────────────────────────
        sql_sys = get_prompt(workspace_id, 'sql_generation')
        if not sql_sys or not sql_sys.strip():
            sql_sys = DEFAULT_SQL_SYS

        sql_user = f"Schemas available:\n{schema_context}\n\nOriginal Question: {question}\n\nCanonical Query (Structured Intent):\n{canonical_query_str}"
        if "SAP_S4" in schema_context or "ZPVF_" in schema_context:
            sql_user += (
                "\n\nCRITICAL MANDATE FOR NAMES & DESCRIPTIONS:\n"
                "When the question asks for customers, products, categories, territories, regions, or zones, "
                "you MUST join the corresponding Master table to the point and SELECT the human-readable NAME / DESCRIPTION:\n"
                "- For Customer Name: ALWAYS join (fact.\"dealer_code\" = cm.KUNNR OR LTRIM(fact.\"dealer_code\", '0') = LTRIM(cm.KUNNR, '0')) and SELECT COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact.\"dealer_code\") AS customer_name, fact.\"dealer_code\" AS customer_code.\n"
                "- For Product/SKU: Join fact.\"SKU\" = sk.MATNR and SELECT COALESCE(sk.MAKTX, sk.PROD_TITLE, fact.\"SKU\") AS product_name, fact.\"SKU\" AS sku_code, sk.BRAND.\n"
                "- For Category: Join sk.CAT_CODE = cat.CAT_CODE and SELECT cat.CAT_DESC AS category_name, cat.CAT_CODE AS category_code.\n"
                "- For Territory/Region/Zone: SELECT tm.NAME AS territory_name, tm.BZIRK AS territory_code, rm.\"region_name\", rm.\"region_code\", zm.NAME AS zone_name.\n"
                "ALWAYS select BOTH the human-readable Name (for UI display) AND the Code (for safe JOINs across CTEs/subqueries)!\n\n"
                "CRITICAL RULES FOR ACTUAL VS TARGET / PLAN COMPARISONS:\n"
                "1. Target View 'ZIVF_SD_Sales_Plan_Target_01_TR' ALREADY has 'YMONTH' directly on the view! NEVER join 'ZITR_DayAttribute_01_TR' to the target view (DayAttribute has daily rows, so joining on YMONTH creates a catastrophic 30x Cartesian duplicate explosion and takes 200+ seconds)!\n"
                "2. When comparing Actual Sales vs Target Plans using CTEs:\n"
                "   - In CTE 1 (actual_sales): Select and group by BOTH the display name and code (e.g. tm.NAME AS territory_name, tm.BZIRK AS territory_code, rm.\"region_name\", rm.\"region_code\", cat.CAT_DESC AS category_name, cat.CAT_CODE AS category_code, da.YMONTH AS month).\n"
                "   - In CTE 2 (target_plans): Query directly from \"SAP_S4\".\"ZIVF_SD_Sales_Plan_Target_01_TR\" tgt (DO NOT join DayAttribute). Select tgt.TERR_CODE AS territory_code, tgt.REG_CODE AS region_code, tgt.CAT_CODE AS category_code, tgt.YMONTH AS month, SUM(tgt.PLAN_VALUE) AS target_sales_value, SUM(tgt.ORG_PLAN) AS target_qty. Group by tgt.TERR_CODE, tgt.REG_CODE, tgt.CAT_CODE, tgt.YMONTH.\n"
                "   - In Outer Query: JOIN CTEs on CODES (e.g. a.territory_code = t.territory_code AND a.month = t.month AND a.category_code = t.category_code). NEVER join a Name column to a Code column (e.g. NEVER do a.territory_name = t.terr_code)!"
            )
        else:
            sql_user += "\n\nCRITICAL FINAL RULE: NEVER SELECT or GROUP BY `customer_master.KUNNR` or any other dimension's Primary Key! You MUST SELECT and GROUP BY the Fact Table's Foreign Key (e.g. `sales_data.customer`) instead. Selecting dimension keys causes unmatched rows to lump together as NULLs."
        schema_grounding, col_to_tables = _build_schema_grounding(schema_chunks)
        table_cols_map = _parse_schema_chunks(schema_chunks)
        if schema_grounding:
            sql_user += f"\n\n{schema_grounding}"

        # Business schema map: pin product attributes to the correct dimension
        # table/join key, map words→columns, and drill down hierarchies. This is
        # what makes "tyre categories" (=> construction within Tyre) and "vehicle"
        # (=> `vehicle type`, not CATEGORY) resolve correctly and without fan-out.
        try:
            biz = _build_business_map(table_cols_map, workspace_id)
            biz_prompt = _business_prompt(biz)
            # [PATCH] Geography chain is resolved and injected separately —
            # region/zone are never part of DIMENSIONS or biz_prompt.
            geo_prompt = _build_geography_prompt(table_cols_map)
            if biz_prompt:
                sql_user += f"\n\n{biz_prompt}"
                print(f"[BIZMAP] fact={biz['fact']} measure={biz['measure']} "
                      f"dims={[(d['label'], d['table']) for d in biz['dims']]} "
                      f"levels={biz['levels']}")
            if geo_prompt:
                sql_user += geo_prompt
                print("[GEOMAP] geography chain injected")

            values_by_col = _parse_value_index(schema_chunks)
            drill = _detect_drilldown(question, values_by_col, biz)
            if drill:
                want_breakdown = bool(_RANK_OR_BREAKDOWN_RE.search(question))
                sql_user += _drilldown_hint(drill, want_breakdown, biz)
                _f = drill["filter"]; _g = drill.get("group")
                print(f"[DRILLDOWN] filter {_f[0]}.{_f[1]}='{_f[2]}'"
                      + (f" -> group by {_g[0]}.{_g[1]}" if (_g and want_breakdown) else " (filter only)"))
            else:
                gh = _grouping_hint(question, biz)
                if gh:
                    sql_user += gh
                    print(f"[GROUPBY] {[ (c,p) for (_t,c,p) in _detect_group_columns(question, biz)]}")
        except Exception as _e:
            print(f"[BIZMAP] skipped: {_e}")

        sql_json = _mistral(sql_sys, sql_user, temperature=0.0)
        print(f"[DEBUG SQL JSON] {sql_json}")

        # Local LLM fallbacks (Mistral sometimes nests inside 'query' or uses uppercase)
        if isinstance(sql_json, dict):
            if "query" in sql_json and isinstance(sql_json["query"], dict) and "sql" in sql_json["query"]:
                sql_json["sql"] = sql_json["query"]["sql"]
            if "SQL" in sql_json and "sql" not in sql_json:
                sql_json["sql"] = sql_json["SQL"]

        # ── Deterministic pre-execution guard ──────────────────────────────
        # The model sometimes attributes a column to the wrong base table
        # (e.g. `invoice`.`construction`), which fails at execution with 1054.
        # Catch it statically against the real schema and force a correction
        # BEFORE touching the database, so the first DB attempt is already right.
        for _v in range(2):
            cand_sql = (sql_json or {}).get("sql", "") if isinstance(sql_json, dict) else ""
            if not cand_sql:
                break
            violations = _validate_column_refs(cand_sql, table_cols_map)
            if not violations:
                break
            print(f"[SQL_VALIDATE] wrong-table column refs {violations} — regenerating before execution")
            fix_user = (
                sql_user
                + "\n\nPREVIOUS SQL:\n" + cand_sql
                + "\n\n" + _column_ref_correction(violations, col_to_tables)
            )
            sql_json = _mistral(sql_sys, fix_user, temperature=0.0)

        # Track failures so we never fall through to an empty-context LLM answer.
        agg_sql_error = None
        sql_results = []

        if sql_json and sql_json.get("sql"):
            target_db = sql_json.get("db", "").strip()
            
            # Fallback: Extract DB from schema_context if the LLM forgot to include it
            if not target_db:
                m_db = re.search(r'db:([a-zA-Z0-9_]+)', schema_context)
                if m_db:
                    target_db = m_db.group(1)

            sql_query = sql_json.get("sql", "").strip()
            print(f"[SQL_GEN] db: {target_db} | sql: {sql_query}")

            conn_sql = cur_sql = None
            tunnel = None
            is_hana = False
            sql_results = []
            try:
                # 1. Check if session has SAP HANA connection
                try:
                    from controllers.dashboard_visuals import get_sap_connection_for_session
                    temp_conn = get_connection_func()
                    hana_conn, tunnel, effective_schema = get_sap_connection_for_session(temp_conn, session_id, user_id)
                    try:
                        temp_conn.close()
                    except Exception:
                        pass

                    if hana_conn:
                        conn_sql = hana_conn
                        cur_sql = conn_sql.cursor()
                        is_hana = True
                        print(f"[SQL_GEN] Successfully connected to SAP HANA (schema={effective_schema}).")
                        # Authoritative schema for SAP S/4HANA views is 'SAP_S4'
                        clean_schema = "SAP_S4"
                        if effective_schema and "#" not in str(effective_schema):
                            clean_schema = str(effective_schema).strip()
                        try:
                            cur_sql.execute(f'SET SCHEMA "{clean_schema}"')
                            print(f"[SQL_GEN] Active SAP HANA schema set to: {clean_schema}")
                        except Exception as se:
                            print(f"[SQL_GEN] Error setting HANA schema {clean_schema}: {se}")
                except Exception as he:
                    print(f"[SQL_GEN] SAP HANA connection attempt failed: {he}")
                    is_hana = False

                if not is_hana:
                    # Determine connection
                    if not target_db:
                        conn_sql = get_connection_func()
                        cur_sql = _dict_cursor(conn_sql)
                    else:
                        try:
                            conn_sql = mysql.connector.connect(
                                host=MYSQL_CONFIG["host"], port=MYSQL_CONFIG["port"],
                                user=MYSQL_CONFIG["user"], password=MYSQL_CONFIG["password"],
                                database=target_db, connection_timeout=10)
                            cur_sql = _dict_cursor(conn_sql)
                        except Exception as e:
                            print(f"[SQL_GEN] MySQL failed, trying Postgres: {e}")
                            if PSYCOPG2_AVAILABLE:
                                temp_conn = get_connection_func()
                                temp_cur = _dict_cursor(temp_conn)
                                temp_cur.execute("SELECT credential FROM database_credential WHERE session_id=%s AND db_type IN ('postgresql', 'postgres')", (session_id,))
                                pg_rows = temp_cur.fetchall()
                                temp_cur.close()
                                temp_conn.close()
                                
                                for r in pg_rows:
                                    cred = json.loads(r["credential"]) if isinstance(r["credential"], str) else r["credential"]
                                    if cred.get("database") == target_db:
                                        conn_sql = psycopg2.connect(
                                            host=cred.get("host"), port=cred.get("port", 5432),
                                            user=cred.get("username"), password=cred.get("password"),
                                            dbname=target_db, connect_timeout=10)
                                        cur_sql = conn_sql.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                                        break
                
                if cur_sql:
                    for _attempt in range(2):
                        try:
                            run_query = sql_query
                            # Strip markdown code blocks if present
                            if "```" in run_query:
                                run_query = re.sub(r"```(?:sql)?(.*?)```", r"\1", run_query, flags=re.DOTALL).strip()
                            # Strip trailing semicolons (SAP HANA syntax error near ';')
                            run_query = run_query.rstrip("; \t\r\n")

                            if is_hana:
                                if '`' in run_query:
                                    run_query = run_query.replace('`', '"')
                                # Replace any HDI container / user schema prefix like "SAP_S4#AI_BI_EXT" with "SAP_S4"
                                run_query = re.sub(r'\"?SAP_S4#[a-zA-Z0-9_]+\"?', '"SAP_S4"', run_query, flags=re.IGNORECASE)

                                # Ensure SAP_S4 schema prefix on SAP views safely without duplicate quoting
                                for sap_v in [
                                    "ZPVF_SD_CurrentSales_01_TR", "ZPVF_SD_SalesHistory_01_TR",
                                    "ZPVF_SD_CancelledSalesInvoice_01_TR", "ZIVF_SD_Sales_Plan_Target_01_TR",
                                    "ZIVF_CustomerMaster_01_MD", "ZIVR_SKUMaster_01_TR",
                                    "ZIVF_CategoryMaster_01_TR", "ZIVF_TerritoryMaster_01_TR",
                                    "ZIVF_RegionMaster_MD_01", "ZIVF_ZoneMaster_01_TR", "ZITR_DayAttribute_01_TR"
                                ]:
                                    run_query = re.sub(
                                        rf'(?:\"?SAP_S4\"?\.)?\"?{sap_v}\"?(?!\w)',
                                        rf'"SAP_S4"."{sap_v}"',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )

                                # Automatic Column Casing & Quoting for SAP S/4HANA (Strictly case-sensitive CDS views)
                                # Map all lowercase and mixed-case columns to exact double-quoted identifiers
                                sap_col_map = {
                                    'DISTRIBUTION_CHANNEL': '"distribution_channel"',
                                    'COMPANY_CODE': '"company_code"',
                                    'INVOICE_TYPE': '"invoice_type"',
                                    'PLANT': '"plant"',
                                    'INVOICE_NUMBER': '"invoice_number"',
                                    'INVOICE_DATE': '"invoice_date"',
                                    'INDICATOR_CANCEL': '"indicator_cancel"',
                                    'DEALER_CODE': '"dealer_code"',
                                    'SHIP_TO_PARTY': '"ship_to_party"',
                                    'BATCH': '"batch"',
                                    'VOLUME': '"volume"',
                                    'NDP_VALUE': '"ndp_value"',
                                    'OLD_CODE': '"old_code"',
                                    'TOTAL_DISCOUNT': '"total_discount"',
                                    'GST': '"gst"',
                                    'INVOICE_VALUE': '"invoice_value"',
                                    'TAXABLE_VALUE': '"taxable_value"',
                                    'TYRE_QUANTITY': '"Tyre_Quantity"',
                                    'REGION_CODE': '"region_code"',
                                    'REGION_NAME': '"region_name"',
                                    'REGION_EMPLOYEE_NUMBER': '"region_employee_number"',
                                    'PERSON_RESPONSIBLE': '"person_responsible"',
                                    'ZONE_CODE': '"zone_code"',
                                    'REGION_USER_ID': '"region_user_id"',
                                    'MOBILE_NUMBER': '"mobile_number"',
                                    'EMAIL_ID': '"email_id"',
                                    'SMTPADDR': '"SmtpAddr"',
                                    'DATE_': '"Date_"'
                                }

                                for u_col, exact_col in sap_col_map.items():
                                    # Match alias."col" or alias.col cleanly
                                    run_query = re.sub(
                                        rf'\b([a-zA-Z0-9_]+)\.\"?{u_col}\"?(?!\w)',
                                        rf'\1.{exact_col}',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )
                                    # Match standalone "col" or col
                                    run_query = re.sub(
                                        rf'(?<![a-zA-Z0-9_".])\"?{u_col}\"?(?![a-zA-Z0-9_"])',
                                        exact_col,
                                        run_query,
                                        flags=re.IGNORECASE
                                    )

                                # Collapse any accidental double-quotes: ""col"" -> "col"
                                run_query = re.sub(r'\"\"+', '"', run_query)
                                # Collapse any duplicated schema: "SAP_S4"."SAP_S4". -> "SAP_S4".
                                run_query = re.sub(r'(?:\"SAP_S4\"\.)+', '"SAP_S4".', run_query, flags=re.IGNORECASE)

                                # Standardize distribution_channel filter to string '10' (avoid implicit type conversion on NVARCHAR)
                                run_query = re.sub(r'\"distribution_channel\"\s*=\s*10\b', r'"distribution_channel" = \'10\'', run_query)
                                run_query = re.sub(r'\bdistribution_channel\s*=\s*10\b', r'"distribution_channel" = \'10\'', run_query)

                                # Ensure subquery alias has AS keyword for strict HANA parser: ") fact" -> ") AS fact"
                                run_query = re.sub(r'\)\s+(fact|sub|cs|sh|t)\b', r') AS \1', run_query, flags=re.IGNORECASE)

                                # Auto-heal Customer Master join on leading zeros if LTRIM was omitted by LLM
                                if ("cm.kunnr" in run_query.lower() or "zivf_customermaster_01_md" in run_query.lower()) and "ltrim" not in run_query.lower():
                                    run_query = re.sub(
                                        r'(?:fact\.)?\"?dealer_code\"?\s*=\s*(?:cm\.)?\"?KUNNR\"?',
                                        r'(fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", \'0\') = LTRIM(cm.KUNNR, \'0\'))',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )
                                    run_query = re.sub(
                                        r'(?:cm\.)?\"?KUNNR\"?\s*=\s*(?:fact\.)?\"?dealer_code\"?',
                                        r'(fact."dealer_code" = cm.KUNNR OR LTRIM(fact."dealer_code", \'0\') = LTRIM(cm.KUNNR, \'0\'))',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )

                                # Auto-heal single-column COALESCE(cm.NAME1, fact."dealer_code") to include BNAME fallback
                                run_query = re.sub(
                                    r'COALESCE\s*\(\s*(?:cm\.)?\"?NAME1\"?\s*,\s*(?:fact\.)?\"?dealer_code\"?\s*\)',
                                    r'COALESCE(NULLIF(TRIM(cm.NAME1), \'\'), NULLIF(TRIM(cm.BNAME), \'\'), fact."dealer_code")',
                                    run_query,
                                    flags=re.IGNORECASE
                                )

                                # Ensure Territory, Region, and Zone Master use LEFT JOIN so missing territories never drop customer sales
                                run_query = re.sub(
                                    r'(?<!LEFT\s)\bJOIN\s+(\"?SAP_S4\"?\.)?\"?(ZIVF_TerritoryMaster_01_TR|ZIVF_RegionMaster_MD_01|ZIVF_ZoneMaster_01_TR)\"?',
                                    r'LEFT JOIN \1"\2"',
                                    run_query,
                                    flags=re.IGNORECASE
                                )
                                # Collapse any accidental duplicated LEFT JOINs (e.g. "LEFT LEFT JOIN" -> "LEFT JOIN")
                                run_query = re.sub(r'\b(?:LEFT\s+)+JOIN\b', 'LEFT JOIN', run_query, flags=re.IGNORECASE)

                                # Auto-heal exact string equality on names/descriptions to case-insensitive UPPER LIKE in HANA
                                run_query = re.sub(
                                    r'((?:[a-zA-Z0-9_]+\.)?\"?(?:region_name|zone_name|NAME|BRAND|CAT_DESC|TYRE_TYPE)\"?)\s*=\s*\'([^\']+)\'',
                                    lambda m: m.group(0) if m.group(2) in ('10', 'X', 'Y', '1') else f"UPPER(TRIM({m.group(1)})) LIKE UPPER('%{m.group(2)}%')",
                                    run_query,
                                    flags=re.IGNORECASE
                                )

                                # Auto-heal CTE joins missing LTRIM: ad.dealer_code = rs.dealer_code -> LTRIM(...) = LTRIM(...)
                                run_query = re.sub(
                                    r'([a-zA-Z0-9_]+)\.(?:dealer_code|customer_code|kunnr)\s*=\s*([a-zA-Z0-9_]+)\.(?:dealer_code|customer_code|kunnr)',
                                    r"(LTRIM(\1.dealer_code, '0') = LTRIM(\2.dealer_code, '0'))",
                                    run_query,
                                    flags=re.IGNORECASE
                                )

                                # Auto-heal cm.KTOKD = 'Dealer' to case-insensitive match
                                run_query = re.sub(
                                    r'(?:cm\.)?\"?KTOKD\"?\s*=\s*\'([^\']+)\'',
                                    r"UPPER(TRIM(cm.KTOKD)) LIKE UPPER('%\1%')",
                                    run_query,
                                    flags=re.IGNORECASE
                                )

                                # Auto-heal: Prevent 30x Cartesian blow-up in Target Fact if LLM joined DayAttribute on YMONTH
                                if "ZIVF_SD_Sales_Plan_Target_01_TR" in run_query and "ZITR_DayAttribute_01_TR" in run_query:
                                    run_query = re.sub(
                                        r'\bJOIN\s+(?:\"?SAP_S4\"?\.)?\"?ZITR_DayAttribute_01_TR\"?\s+(?:AS\s+)?([a-zA-Z0-9_]+)\s+ON\s+[a-zA-Z0-9_]+\.YMONTH\s*=\s*\1\.YMONTH',
                                        ' ',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )

                                # Auto-heal: Prevent Name = Code join in CTE comparisons (e.g. a.territory_name = t.terr_code)
                                run_query = re.sub(
                                    r'\b([a-zA-Z0-9_]+)\.territory_name\s*=\s*([a-zA-Z0-9_]+)\.(?:terr_code|territory_code)\b',
                                    r'(\1.territory_name = \2.terr_code OR \1.territory_name = \2.territory_code)',
                                    run_query,
                                    flags=re.IGNORECASE
                                )

                                # For ranking queries (Top N / Worst N customers), upgrade LEFT JOIN to JOIN on Customer Master
                                # so unprofiled internal codes without customer names (e.g. 102425) are excluded
                                if re.search(r'ORDER\s+BY\s+.*?(?:total_sales|total_qty|invoice_value)', run_query, re.I) and "customermaster" in run_query.lower():
                                    run_query = re.sub(
                                        r'\bLEFT\s+JOIN\s+(\"?SAP_S4\"?\.)?\"?ZIVF_CustomerMaster_01_MD\"?',
                                        r'JOIN \1"ZIVF_CustomerMaster_01_MD"',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )
                                    run_query = re.sub(
                                        r'(?:cm\.)?\"?KUNNR\"?\s*IS\s*NULL\s*OR\s*',
                                        '',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )
                                    run_query = re.sub(
                                        r'\s*OR\s*(?:cm\.)?\"?KUNNR\"?\s*IS\s*NULL\b',
                                        '',
                                        run_query,
                                        flags=re.IGNORECASE
                                    )

                                print(f"[SQL_EXEC] Executing on SAP HANA:\n{run_query}")

                            _t_exec_start = time.time()
                            cur_sql.execute(run_query)
                            _t_exec_done = time.time()
                            print(f"[SQL_EXEC] cur_sql.execute() finished in {_t_exec_done - _t_exec_start:.3f}s. Fetching all rows...")

                            _t_fetch_start = time.time()
                            rows = cur_sql.fetchall()
                            _t_fetch_done = time.time()
                            print(f"[SQL_EXEC] cur_sql.fetchall() finished in {_t_fetch_done - _t_fetch_start:.3f}s. Rows fetched: {len(rows) if rows else 0}")

                            if is_hana:
                                cols = [desc[0] for desc in cur_sql.description] if cur_sql.description else []
                                sql_results = [dict(zip(cols, r)) for r in rows]
                            else:
                                if rows and isinstance(rows[0], (tuple, list)):
                                    cols = [desc[0] for desc in cur_sql.description] if cur_sql.description else []
                                    sql_results = [dict(zip(cols, r)) for r in rows]
                                else:
                                    sql_results = [dict(r) for r in rows]
                            print(f"[SQL_EXEC] Dictionary mapping done. sql_results count = {len(sql_results)}")
                            if status_callback:
                                status_callback("computing", intent=intent, row_count=len(sql_results) if sql_results else 0)

                            # ── SMART 0-ROW FALLBACK (SAP HANA) ──
                            if is_hana and not sql_results:
                                print("[SQL_EXEC] 0 rows returned from SAP HANA query. Attempting smart fallbacks...")
                                
                                # Fallback 1: If CurrentSales view returned 0 rows, try SalesHistory view
                                if "ZPVF_SD_CurrentSales_01_TR" in run_query:
                                    print("[SQL_EXEC] Fallback 1: Retrying on SalesHistory view instead of CurrentSales...")
                                    alt_query = run_query.replace("ZPVF_SD_CurrentSales_01_TR", "ZPVF_SD_SalesHistory_01_TR")
                                    try:
                                        cur_sql.execute(alt_query)
                                        h_rows = cur_sql.fetchall()
                                        if h_rows:
                                            cols = [desc[0] for desc in cur_sql.description] if cur_sql.description else []
                                            sql_results = [dict(zip(cols, r)) for r in h_rows]
                                            run_query = alt_query
                                            sql_query = alt_query
                                            print(f"[SQL_EXEC] SalesHistory fallback SUCCESS! Returned {len(sql_results)} rows.")
                                    except Exception as h_ex:
                                        print(f"[SQL_EXEC] SalesHistory fallback error: {h_ex}")

                                # Fallback 1b: If SalesHistory view returned 0 rows, try CurrentSales view
                                if not sql_results and "ZPVF_SD_SalesHistory_01_TR" in run_query:
                                    print("[SQL_EXEC] Fallback 1b: Retrying on CurrentSales view instead of SalesHistory...")
                                    alt_query = run_query.replace("ZPVF_SD_SalesHistory_01_TR", "ZPVF_SD_CurrentSales_01_TR")
                                    try:
                                        cur_sql.execute(alt_query)
                                        h_rows = cur_sql.fetchall()
                                        if h_rows:
                                            cols = [desc[0] for desc in cur_sql.description] if cur_sql.description else []
                                            sql_results = [dict(zip(cols, r)) for r in h_rows]
                                            run_query = alt_query
                                            sql_query = alt_query
                                            print(f"[SQL_EXEC] CurrentSales fallback SUCCESS! Returned {len(sql_results)} rows.")
                                    except Exception as h_ex:
                                        print(f"[SQL_EXEC] CurrentSales fallback error: {h_ex}")

                                # Fallback 2: If still 0 rows, check if distribution_channel = '10' is filtering out all records
                                if not sql_results and "distribution_channel" in run_query:
                                    if not re.search(r'\b(?:channel\s*10|dist(?:ribution)?\s*channel)\b', question, re.I):
                                        print("[SQL_EXEC] Fallback 2: Relaxing distribution_channel = '10' constraint...")
                                        relaxed_q = re.sub(r'(?:AND\s+)?(?:[a-zA-Z0-9_]+\.)?\"?distribution_channel\"?\s*=\s*\'?10\'?\s*(?:AND\s+)?', ' ', run_query, flags=re.IGNORECASE)
                                        relaxed_q = re.sub(r'WHERE\s+(?:AND\s+)+', 'WHERE ', relaxed_q, flags=re.IGNORECASE)
                                        relaxed_q = re.sub(r'WHERE\s*(GROUP|ORDER|\Z)', r'\1', relaxed_q, flags=re.IGNORECASE)
                                        try:
                                            cur_sql.execute(relaxed_q)
                                            rel_rows = cur_sql.fetchall()
                                            if rel_rows:
                                                cols = [desc[0] for desc in cur_sql.description] if cur_sql.description else []
                                                sql_results = [dict(zip(cols, r)) for r in rel_rows]
                                                run_query = relaxed_q
                                                sql_query = relaxed_q
                                                print(f"[SQL_EXEC] Relaxed channel filter SUCCESS! Returned {len(sql_results)} rows.")
                                        except Exception as rel_e:
                                            print(f"[SQL_EXEC] Relaxed filter error: {rel_e}")

                                # Fallback 3: Convert exact string equality in WHERE clause to case-insensitive UPPER LIKE
                                if not sql_results and re.search(r"=\s*'([^']+)'", run_query):
                                    print("[SQL_EXEC] Fallback 3: Converting exact string matches to UPPER LIKE in WHERE clause...")
                                    like_q = re.sub(
                                        r"((?:[a-zA-Z0-9_]+\.)?\"?[a-zA-Z0-9_]+\"?)\s*=\s*\'([^\']+)\'",
                                        lambda m: m.group(0) if m.group(2) in ('10', 'X', 'Y', '1') else f"UPPER(TRIM({m.group(1)})) LIKE UPPER('%{m.group(2)}%')",
                                        run_query
                                    )
                                    if like_q != run_query:
                                        try:
                                            cur_sql.execute(like_q)
                                            l_rows = cur_sql.fetchall()
                                            if l_rows:
                                                cols = [desc[0] for desc in cur_sql.description] if cur_sql.description else []
                                                sql_results = [dict(zip(cols, r)) for r in l_rows]
                                                run_query = like_q
                                                sql_query = like_q
                                                print(f"[SQL_EXEC] Case-insensitive UPPER LIKE fallback SUCCESS! Returned {len(sql_results)} rows.")
                                        except Exception as l_ex:
                                            print(f"[SQL_EXEC] UPPER LIKE fallback error: {l_ex}")

                                # Fallback 4: If still 0 rows, relax ALL dimension joins to LEFT JOIN
                                if not sql_results and "JOIN" in run_query:
                                    print("[SQL_EXEC] Fallback 4: Relaxing all master/dimension joins to LEFT JOIN...")
                                    left_q = re.sub(r'\bINNER\s+JOIN\b', 'LEFT JOIN', run_query, flags=re.IGNORECASE)
                                    left_q = re.sub(r'(?<!LEFT\s)\bJOIN\s+(\"?SAP_S4\"?\.)?\"?(ZIV[FR]_[a-zA-Z0-9_]+)\"?', r'LEFT JOIN \1"\2"', left_q, flags=re.IGNORECASE)
                                    # Also relax cm.KLABC or cm.KTOKD if dropping rows
                                    left_q = re.sub(r'(?:AND\s+)?\(?\s*(?:cm\.)?KLABC\s*IS\s*NULL\s*OR\s*(?:cm\.)?KLABC\s*<>\s*\'X\'\s*\)?\s*(?:AND\s+)?', ' ', left_q, flags=re.IGNORECASE)
                                    left_q = re.sub(r'WHERE\s+(?:AND\s+)+', 'WHERE ', left_q, flags=re.IGNORECASE)
                                    left_q = re.sub(r'WHERE\s*(GROUP|ORDER|\Z)', r'\1', left_q, flags=re.IGNORECASE)
                                    try:
                                        cur_sql.execute(left_q)
                                        j_rows = cur_sql.fetchall()
                                        if j_rows:
                                            cols = [desc[0] for desc in cur_sql.description] if cur_sql.description else []
                                            sql_results = [dict(zip(cols, r)) for r in j_rows]
                                            run_query = left_q
                                            sql_query = left_q
                                            print(f"[SQL_EXEC] Relaxed LEFT JOIN fallback SUCCESS! Returned {len(sql_results)} rows.")
                                    except Exception as j_ex:
                                        print(f"[SQL_EXEC] Relaxed LEFT JOIN fallback error: {j_ex}")

                            # Convert Decimal, Date, and non-serializable objects
                            import decimal
                            for row in sql_results:
                                for k, v in row.items():
                                    if isinstance(v, decimal.Decimal):
                                        row[k] = float(v) if '.' in str(v) else int(v)
                                    elif v is not None and not isinstance(v, (str, int, float, bool)):
                                        row[k] = str(v)

                            agg_sql_error = None
                            print(f"[SQL_EXEC] Success! Returned {len(sql_results)} rows.")
                            break
                        except Exception as ex:
                            import traceback
                            agg_sql_error = f"{type(ex).__name__}: {ex}"
                            print(f"[SQL_EXEC] Attempt {_attempt + 1} FAILED! Type: {type(ex).__name__} | Error: {ex}")
                            print(f"[SQL_EXEC] Traceback:\n{traceback.format_exc()}")

                            # Self-healing for HANA memory limits and column reference errors
                            if is_hana and ("cannot allocate enough memory" in str(ex).lower() or "allocation failed" in str(ex).lower()):
                                try:
                                    print("[SQL_EXEC] HANA memory limit hit — attempting auto-repair (lean CurrentSales / no join)...")
                                    repaired_sql = re.sub(r'LEFT\s+JOIN\s+.*?CancelledSalesInvoice.*?(?=WHERE|GROUP|ORDER|\n\s*WHERE|\Z)', ' ', sql_query, flags=re.IGNORECASE | re.DOTALL)
                                    repaired_sql = re.sub(r'\b(?:can|c)\.[a-zA-Z0-9_]+\s*IS\s*NULL', '1=1', repaired_sql, flags=re.IGNORECASE)
                                    # Fallback: if UNION ALL on 30M SalesHistory caused OOM, switch to CurrentSales
                                    repaired_sql = re.sub(r'UNION\s+ALL\s+SELECT\b.*?(?:ZPVF_SD_SalesHistory_01_TR|saleshistory).*?\)\s*(?:AS\s+)?(\w+)', r') AS \1', repaired_sql, flags=re.DOTALL | re.IGNORECASE)
                                    if repaired_sql != sql_query:
                                        sql_query = repaired_sql
                                        continue
                                except Exception as _heal_err:
                                    print(f"[SQL_EXEC] Auto-heal attempt error: {_heal_err}")

                            if _attempt == 1:
                                break
                            # Self-heal: send the error back to the model once.
                            db_engine = "SAP HANA" if is_hana else "MySQL"
                            repair_user = (
                                f"The following {db_engine} query FAILED. Fix it and return the same JSON shape.\n\n"
                                f"SQL:\n{sql_query}\n\n{db_engine} error:\n{ex}\n\n"
                            )
                            if is_hana:
                                repair_user += (
                                    "Common fixes for SAP HANA:\n"
                                    "- For current period / MTD questions, query directly from \"SAP_S4\".\"ZPVF_SD_CurrentSales_01_TR\". For general / historical questions, query \"SAP_S4\".\"ZPVF_SD_SalesHistory_01_TR\".\n"
                                    "- Fact view columns are case-sensitive and must be double-quoted: fact.\"dealer_code\", fact.\"invoice_value\", fact.\"Tyre_Quantity\", fact.\"distribution_channel\".\n"
                                    "- Customer master: For rankings (Top/Worst N), ALWAYS use JOIN (INNER JOIN) on \"SAP_S4\".\"ZIVF_CustomerMaster_01_MD\" cm ON (fact.\"dealer_code\" = cm.KUNNR OR LTRIM(fact.\"dealer_code\", '0') = LTRIM(cm.KUNNR, '0')) and select COALESCE(NULLIF(TRIM(cm.NAME1), ''), NULLIF(TRIM(cm.BNAME), ''), fact.\"dealer_code\") AS customer_name. NEVER allow cm.KUNNR IS NULL in ranking queries.\n"
                                    "- Filter fact.\"distribution_channel\" = '10' ONLY IF explicitly requested by the user.\n"
                                    "- Do NOT join 'ZPVF_SD_CancelledSalesInvoice_01_TR'. Use: AND (fact.\"indicator_cancel\" IS NULL OR fact.\"indicator_cancel\" NOT IN ('X', 'Y', '1')).\n\n"
                                )
                            else:
                                repair_user += (
                                    "Common fixes: a reserved word is used as an alias (rank, order, group, "
                                    "desc, asc, count, sum, rows, range) — rename the row-number alias to `rn` "
                                    "and backtick every identifier; verify MySQL 8.0 window syntax; keep the "
                                    "follow the COLUMN HYGIENE rule in the system prompt for numeric columns.\n\n"
                                )
                            repair_user += f"Schemas:\n{schema_context}"
                            if schema_grounding:
                                repair_user += f"\n\n{schema_grounding}"
                            if not is_hana:
                                repair_user += _unknown_column_hint(ex, col_to_tables)
                            repair_user += f"\n\nOriginal Question: {question}"
                            fix_json = _mistral(sql_sys, repair_user, temperature=0.0)
                            
                            # Fallback for nested SQL in self-heal
                            if isinstance(fix_json, dict):
                                if "query" in fix_json and isinstance(fix_json["query"], dict) and "sql" in fix_json["query"]:
                                    fix_json["sql"] = fix_json["query"]["sql"]
                                if "SQL" in fix_json and "sql" not in fix_json:
                                    fix_json["sql"] = fix_json["SQL"]
                            
                            if fix_json and fix_json.get("sql"):
                                sql_query = fix_json.get("sql", "").strip()
                                print(f"[SQL_REPAIR] Retrying with corrected SQL:\n{sql_query}")
                            else:
                                break
            except Exception as e:
                import traceback
                agg_sql_error = f"{type(e).__name__}: {e}"
                print(f"[SQL_EXEC] Outer execution block FAILED! Type: {type(e).__name__} | Error: {e}")
                print(f"[SQL_EXEC] Traceback:\n{traceback.format_exc()}")
            finally:
                if cur_sql: 
                    try: cur_sql.close() 
                    except: pass
                if conn_sql: 
                    try: conn_sql.close() 
                    except: pass
                if tunnel:
                    try: tunnel.stop()
                    except: pass

            if sql_results:
                total_rows = len(sql_results)
                if total_rows > 25:
                    # Calculate metric totals across all returned rows for the LLM
                    numeric_sums = {}
                    sample_row = sql_results[0]
                    for col, val in sample_row.items():
                        if isinstance(val, (int, float)) and not any(id_w in col.lower() for id_w in ("year", "month", "id", "code", "pct", "percent")):
                            try:
                                col_sum = sum(float(r.get(col) or 0) for r in sql_results if r.get(col) is not None)
                                numeric_sums[col] = f"{col_sum:,.2f}"
                            except Exception:
                                pass

                    summary_header = f"Total Records Returned: {total_rows:,}\n"
                    if numeric_sums:
                        summary_header += "Overall Metric Totals Across All Records:\n" + "\n".join([f"  - Total {k}: {v}" for k, v in numeric_sums.items()]) + "\n"

                    sample_rows = sql_results[:20]
                    context = (
                        f"SQL Query executed: {sql_query}\n\n"
                        f"{summary_header}\n"
                        f"Sample Records (Showing top 20 of {total_rows:,} records):\n"
                        f"{json.dumps(sample_rows, indent=2, default=str)}\n\n"
                        f"IMPORTANT: The database query successfully returned {total_rows:,} records. "
                        f"Provide a clear, high-level executive summary of the overall totals and key highlights "
                        f"from the top records shown above. DO NOT try to list all {total_rows:,} records in text."
                    )
                else:
                    context = f"SQL Query executed: {sql_query}\n\nSQL Results ({len(sql_results)} records):\n" + json.dumps(sql_results, indent=2, default=str)

        # Fallback if no SQL rows were produced
        if intent == "AGGREGATION" and not sql_results:
            _detail = f" (error: {agg_sql_error})" if agg_sql_error else ""
            print(f"[AGGREGATION] No SQL rows returned! agg_sql_error={agg_sql_error!r} — setting clean zero-row context")
            if agg_sql_error and any(w in agg_sql_error.lower() for w in ["memory", "allocation", "timeout", "timed out", "statement"]):
                context = (
                    "CRITICAL INSTRUCTION: State clearly and politely that the data volume is too high to process this query. "
                    "Instruct the user to add a time limit to their question, such as 'for 2024', 'for last 6 months', or 'current year'."
                )
            else:
                context = (
                    f"SQL Execution Note: Direct SQL computation returned 0 rows (no matching records found in the database){_detail}.\n"
                    f"Attempted Query: {sql_query if 'sql_query' in locals() else 'N/A'}\n"
                    f"Question: {question}\n\n"
                    "CRITICAL INSTRUCTION: State clearly, politely, and directly in 1 or 2 sentences that no records or transactions were found matching the specified criteria in the database. "
                    "DO NOT generate lengthy paragraphs, diagnostic checklists, troubleshooting steps, or alternative query approaches. "
                    "DO NOT explain database schemas or internal table architectures. Keep your response direct, concise, and professional."
                )

    # Graph
    if _is_graph(question):
        ftype        = _next_followup_type(session_id)
        followup_ins = _followup_instruction(ftype)
        _graph_tpl = get_prompt(workspace_id, 'rag_chat_graph') or DEFAULT_GRAPH_TPL
        _graph_msg = (
            _graph_tpl
            .replace('{context}', context)
            .replace('{question}', question)
            .replace('{hist}', hist)
            .replace('{followup_ins}', followup_ins)
        )
        res = _mistral(system_prompt, _graph_msg)
        if not res:
            return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
        _advance_turn(session_id)
        fuq = res.get("follow_up_questions",[])
        actual_visit = _save_history(get_connection_func, session_id, user_id,
                      question, json.dumps(res.get("datasets",[])), fuq, understanding["intent"], "graph",
                      login_token=login_token, chat_id=active_chat_id)
        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "chart_data": {
                "chart_type":  res.get("chart_type"),
                "title":       res.get("title",""),
                "labels":      res.get("labels",[]),
                "datasets":    res.get("datasets",[]),
                "source_note": res.get("source_note","")
            },
            "sql_query": sql_query,
            "follow_up_questions": fuq,
            "chat_id": active_chat_id,
            "visit_number": actual_visit
        }), 200

    # Report
    if _is_report(question):
        ftype        = _next_followup_type(session_id)
        followup_ins = _followup_instruction(ftype)
        _report_tpl = get_prompt(workspace_id, 'rag_chat_report') or DEFAULT_REPORT_TPL
        _report_msg = (
            _report_tpl
            .replace('{context}', context)
            .replace('{question}', question)
            .replace('{hist}', hist)
            .replace('{followup_ins}', followup_ins)
        )
        res = _mistral(system_prompt, _report_msg)
        if not res:
            return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500
        _advance_turn(session_id)
        fuq = res.get("follow_up_questions",[])
        actual_visit = _save_history(get_connection_func, session_id, user_id,
                      question, res.get("report_title",""), fuq, understanding["intent"], "report",
                      login_token=login_token, chat_id=active_chat_id)
        return jsonify({
            "status":     "success",
            "statusCode": 200,
            "report": {
                "title":        res.get("report_title",""),
                "sections":     res.get("sections",[]),
                "key_findings": res.get("key_findings",[])
            },
            "sql_query": sql_query,
            "follow_up_questions": fuq,
            "chat_id": active_chat_id,
            "visit_number": actual_visit
        }), 200

    # Answer
    ftype        = _next_followup_type(session_id)
    followup_ins = _followup_instruction(ftype)

    # Detect multi-part questions and add explicit instruction
    q_parts = [p.strip() for p in re.split(r'[?]\s+(?=[A-WY-Z])', question) if len(p.strip()) > 8]
    multi_hint = (
        f"\nNOTE: This question has {len(q_parts)} parts. Address EACH part with a clear numbered heading."
        if len(q_parts) > 1 else ""
    )

    _answer_tpl = get_prompt(workspace_id, 'rag_chat_answer')
    if not _answer_tpl or not _answer_tpl.strip():
        _answer_tpl = DEFAULT_ANSWER_TPL
    _answer_msg = (
        _answer_tpl
        .replace('{context}', context)
        .replace('{question}', question)
        .replace('{hist}', hist)
        .replace('{followup_ins}', followup_ins)
        .replace('{multi_hint}', multi_hint)
        .replace('{intent}', str(understanding.get('intent', '')))
        .replace('{table_hints}', str(understanding.get('table_hints', '')))
    )
    
    if understanding.get("intent") == "AGGREGATION":
        _answer_msg += "\n\nCRITICAL INSTRUCTION FOR VISUALIZATIONS: Because this is an aggregation query, your 'visualizations' array MUST ONLY contain a 'table' (type='table'). DO NOT generate 'line_chart', 'bar_chart', or any other charts. DO NOT hallucinate dates or months!"

    if status_callback:
        status_callback("finalizing")

    res = _mistral(system_prompt, _answer_msg)
    if not res:
        return jsonify({"status":"error","statusCode":500,"message":"LLM failed"}), 500

    clean_answer = _to_str(res.get("answer",""))
    clean_answer = re.sub(r'\s*\(source:[^)]*\)', '', clean_answer).strip()
    clean_answer = re.sub(r'\s*\[source:[^\]]*\]', '', clean_answer).strip()

    raw_fuq = res.get("follow_up_questions", [])
    fuq = []
    if isinstance(raw_fuq, list):
        for q in raw_fuq:
            if isinstance(q, dict):
                fuq.append(str(q.get("question", list(q.values())[0] if q else "")))
            else:
                fuq.append(str(q))

    visualizations = _safe_visualizations(
        _normalize_visualizations(res.get("visualizations", []))
    )
    if status_callback and visualizations:
        status_callback("visualizing")

    if understanding.get("intent") == "AGGREGATION":
        if not sql_results:
            # Zero rows: Drop all hallucinated charts/tables so frontend never renders blank widgets
            visualizations = []
        else:
            keys = list(sql_results[0].keys())
            keys_lower = [k.lower() for k in keys]
            has_time = any(t in k for k in keys_lower for t in ['month', 'date', 'year'])
            
            total_rows = len(sql_results)
            # Read configurable threshold from system_configurations
            csv_threshold = 30
            try:
                _cconn = get_connection_func()
                from helper.system_config_service import get_system_config
                csv_threshold = get_system_config(_cconn, "csv_export_threshold", default=30)
                try: _cconn.close()
                except: pass
            except Exception:
                csv_threshold = 30

            columns = [{"key": k, "label": str(k).replace("_", " ").title()} for k in keys]
            table_vis = None
            if total_rows <= csv_threshold:
                # If row count <= threshold: show ALL rows in chat table
                table_vis = {
                    "type": "table",
                    "title": f"Data Table ({total_rows:,} records)",
                    "columns": columns,
                    "data": sql_results,
                    "total_records": total_rows,
                    "displayed_records": total_rows
                }
            
            # Align and sanitize chart xKey, yKey, and data against actual SQL keys
            # Charts display top 25 data points to guarantee clean readability and instant DOM rendering
            MAX_CHART_POINTS = 25
            chart_data = sql_results[:MAX_CHART_POINTS]
            for v in visualizations:
                if v.get("type") in ("bar_chart", "line_chart"):
                    x_key = v.get("xKey")
                    y_key = v.get("yKey")
                    if x_key not in keys:
                        cand_x = next((k for k in keys if any(m in k.lower() for m in ("name", "title", "code", "desc", "month", "year", "date"))), keys[0] if keys else "")
                        v["xKey"] = cand_x
                    if y_key not in keys:
                        cand_y = next((k for k in keys if any(m in k.lower() for m in ("sales", "value", "qty", "quantity", "total", "count", "achv", "pct"))), keys[-1] if keys else "")
                        v["yKey"] = cand_y
                    v["data"] = chart_data

            if not has_time:
                # Keep only bar_charts; drop hallucinated line/pie charts
                visualizations = [v for v in visualizations if v.get("type") == "bar_chart"]
            else:
                # Keep all non-table charts (like line_chart)
                visualizations = [v for v in visualizations if v.get("type") != "table"]
                
            # ALWAYS append the perfectly formatted Python table ONLY if row count <= threshold
            if table_vis:
                visualizations.append(table_vis)

    # If user explicitly asked for table → keep only table
    if "table" in question.lower():
        visualizations = [v for v in visualizations if v.get("type") == "table"]



    # Universal safety net: If any table visualization has missing/empty columns, 
    # dynamically populate them from the data keys to prevent UI binding failures.
    for v in visualizations:
        if v.get("type") == "table" and not v.get("columns") and v.get("data") and isinstance(v["data"], list) and len(v["data"]) > 0:
            if isinstance(v["data"][0], dict):
                v["columns"] = [{"key": k, "label": str(k).replace("_", " ").title()} for k in v["data"][0].keys()]

    if "table" in question.lower():
        visualizations = [v for v in visualizations if v.get("type") == "table"]

    # ── Configurable CSV Export Check ──
    from helper.system_config_service import get_system_config, generate_relevant_filename
    _conf_th = 30
    try:
        _cconn = get_connection_func()
        _conf_th = get_system_config(_cconn, "csv_export_threshold", default=30)
        try: _cconn.close()
        except: pass
    except Exception:
        _conf_th = 30

    is_csv_export = False
    csv_filename = ""
    total_sql_count = len(sql_results) if sql_results else 0

    if total_sql_count > _conf_th:
        is_csv_export = True
        csv_filename = generate_relevant_filename(question)
        # Suppress table from chat when exceeding threshold
        visualizations = [v for v in visualizations if v.get("type") != "table"]
        csv_vis = {
            "type": "csv_export",
            "title": f"Data Export ({total_sql_count:,} records)",
            "filename": csv_filename,
            "total_records": total_sql_count,
            "threshold": _conf_th,
            "sql_query": sql_query,
            "session_id": session_id
        }
        visualizations.append(csv_vis)
        clean_answer = f"Total {total_sql_count:,} records found matching your query. The complete dataset is available for download as a CSV file below."

    _advance_turn(session_id)
    actual_visit = _save_history(get_connection_func, session_id, user_id,
                  question, clean_answer, fuq, understanding["intent"], "answer",
                  login_token=login_token, visualizations=visualizations,
                  chat_id=active_chat_id)

    return jsonify({
        "status":              "success",
        "statusCode":          200,
        "answer":              clean_answer,
        "sql_query":           sql_query,
        "follow_up_questions": fuq,
        "visualizations":      visualizations,
        "is_csv_export":       is_csv_export,
        "csv_filename":        csv_filename,
        "total_records":       total_sql_count,
        "chat_id":             active_chat_id,
        "visit_number":        actual_visit
    }), 200









