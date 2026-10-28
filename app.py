
import os
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["MPLBACKEND"] = "Agg" # Force Matplotlib headless mode globally
os.environ["PYTHONUNBUFFERED"] = "1"  # Force unbuffered stdout/stderr — ensures all print() appear immediately in logs
import logging
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore")

# Suppress noisy logs globally BEFORE importing other libraries
logging.getLogger('werkzeug').setLevel(logging.INFO)
for logger_name in ["httpx", "sentence_transformers", "huggingface_hub", "chromadb", "apscheduler", "transformers"]:
    logging.getLogger(logger_name).setLevel(logging.ERROR)

import os
# pyrefly: ignore [missing-import]
import mysql.connector 
from flask import Flask, request
from flask_cors import CORS
from dotenv import load_dotenv
from urllib.parse import quote_plus
from sqlalchemy import create_engine
from controllers.auth_controller import login , register_user_controller, admin_login_auth, change_password_controller, get_admin_password_controller
from controllers.external_db import connect_external_db, apply_external_sync, apply_bulk_sync
from controllers.external_sync_controller import sync_external_csv
from controllers.tracker import get_tracker_data
from concurrent.futures import ThreadPoolExecutor
from controllers.insights import insights_controller
from controllers.analyze_files import analyze_controller
from controllers.view_info import view_analyze_controller
from controllers.login_controller import login_controller
from controllers.visualization import send_from_directory
from controllers.delete_history import delete_session_controller
from controllers.session_controller import fetch_successful_sessions
from controllers.chat import upload_files_controller , rag_chat_controller
from controllers.get_analyze_summary import get_analyze_summary_controller
from controllers.uload_file_count_tablename import upload_files_count_controller
from controllers.active_session_controller import update_active_sessions_controller
from database.config import BASE_URL, MYSQL_CONFIG, MAX_SAMPLE_VALUES, GRAPH_FOLDER, UPLOAD_FOLDER, TEMP_UPLOAD_FOLDER
# --- Database & LLM Services ---
# pyrefly: ignore [missing-import]
from pyvis.network import Network
from database.llm_service import LLMService
from database.database_service import DatabaseService
from controllers.orchestrator_controller import process_books
from controllers.admin_login import staff_login_controller, create_staff_account_controller,get_all_staff_controller
from controllers.kgraph_controller import rollback_kgraph_controller, get_all_kgraphs_controller, get_kgraph_ontology_analysis_controller
load_dotenv()
from controllers.captcha_controller import generate_captcha_controller
from flask import send_from_directory
from controllers.connector_controller import create_connector_controller, agent_query_controller
from controllers.web_search_controller import web_search_controller
from controllers.save_result_controller import (
    save_result_controller,
    get_saved_results_controller,
    delete_saved_result_controller
)
from controllers.saved_content_analysis_controller import (
    saved_content_describe_controller,
    saved_content_chat_controller
)
from controllers.sheet_scan_controller import sheet_scan_controller
from controllers.sheet_content_controller import sheet_describe_controller, sheet_chat_controller
from controllers.connector_controllers import (create_connector_controllers,
                                               agent_query_controllers, create_user_controller, delete_workspace_controller,
                                               get_connection_history_controller,
                                               get_saved_credentials_controller, get_user_workspaces_simple,
                                               get_workspace_history_controller,
                                               create_workspace_controller,
                                                get_user_workspaces_controller, set_active_workspace,
                                                get_all_users_controller,
                                                assign_workspace_users_controller,
                                                get_workspace_users_controller,
                                                remove_workspace_user_controller,
                                                delete_connection_history_controller,
                                                delete_workspace_controller,
                                                edit_user_controller,
                                                delete_user_controller)
from controllers.session_rag_chat_controller import session_rag_chat_controller
from controllers.session_sources_controller import session_sources_controller
from controllers.session_analysis_controller import session_analysis_controller
from controllers.uploads_controller import upload_chunk_controller, upload_universal_dump_controller , upload_csv_controller
from controllers.session_chat_history_controller import session_chat_history_controller
from controllers.admin_knowledge_controller import admin_get_chats_controller, admin_push_knowledge_controller, admin_get_staged_knowledge_controller, admin_trigger_indexing_controller, admin_set_schedule_controller, admin_get_schedule_controller


from controllers.ftp_connector_controller import (
    ftp_connect_controller,
    ftp_fetch_controller,
    ftp_progress_controller,
    ftp_schedule_controller,
    ftp_delete_schedule_controller,
    ftp_get_schedule_controller,
    ftp_fetch_log_controller,
)


from controllers.workspace_prompt_controller import (
    get_all_prompt_types,
    set_workspace_prompt,
    get_workspace_prompts,
    get_workspace_prompt_by_type,
    get_all_workspace_prompts,
    delete_workspace_prompt
)
from controllers.scheduled_reports_controller import (
    add_schedule_controller, get_schedules_controller, 
    update_schedule_controller, delete_schedule_controller
)
from helper.report_mailer import check_and_send_scheduled_reports
# pyrefly: ignore [missing-import]
from apscheduler.schedulers.background import BackgroundScheduler

from controllers.agent_actions_controller import (
    log_agent_action_controller,
    update_agent_action_controller,
    get_agent_actions_controller,
    get_agent_action_detail_controller,
    delete_agent_actions_controller,
)
from controllers.action_approval_controller import (
    action_approve_controller,
    action_reject_controller,
    action_pending_controller,
)
# from controllers.pricing_controller import (
#     get_pricing_controller,
#     create_pricing_controller,
#     update_pricing_controller,
#     delete_pricing_controller
# )
from controllers.export_report_controller import export_domestic_sales_report_controller, export_domestic_sales_preview_controller
from controllers.dashboard_visuals import graph_metrics_controller,extract_graph_data_controller, default_dashboard_metrics_controller,available_years_controller,dashboard_filters_controller,year_wise_sales_comparison_controller,sales_by_zone_data_controller,tyre_sales_data_controller
from controllers.sales_dashboard import get_sales_revenue_data_controller, get_sales_by_account_category_controller, get_non_billed_accounts_controller, get_overdue_pct_controller, get_exposure_pct_controller
from controllers.workspace_types_controller import get_workspace_types_controller, create_workspace_type_controller

from controllers.llm_config_controller import (
    get_providers_controller, create_provider_controller, update_provider_controller,
    delete_provider_controller, get_assignments_controller, update_assignments_controller,
    test_provider_controller, delete_assignments_controller
)

from controllers.company_controller import (
    get_companies_controller,
    create_company_controller,
    update_company_controller,
    delete_company_controller
)

from flask_socketio import SocketIO
from controllers.hub_data_controller import store_hub_data_controller, update_hub_chat_controller

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
CORS(app)

def get_db_connection():
    try:
        return mysql.connector.connect(**MYSQL_CONFIG)
    except Exception as e:
        print(f"DB Error: {e}")
        return None

# LLM Config Routes
@app.route('/api/llm/providers', methods=['GET', 'OPTIONS'])
def get_providers(): return get_providers_controller(get_db_connection) if request.method != 'OPTIONS' else ('', 204)
@app.route('/api/llm/providers', methods=['POST', 'OPTIONS'])
def create_provider(): return create_provider_controller(get_db_connection) if request.method != 'OPTIONS' else ('', 204)
@app.route('/api/llm/providers/<int:id>', methods=['PUT', 'OPTIONS'])
def update_provider(id): return update_provider_controller(get_db_connection, id) if request.method != 'OPTIONS' else ('', 204)
@app.route('/api/llm/providers/<int:id>', methods=['DELETE', 'OPTIONS'])
def delete_provider(id): return delete_provider_controller(get_db_connection, id) if request.method != 'OPTIONS' else ('', 204)
@app.route('/api/llm/providers/<int:id>/test', methods=['POST', 'OPTIONS'])
def test_provider(id): return test_provider_controller(get_db_connection, id) if request.method != 'OPTIONS' else ('', 204)
@app.route('/api/llm/assignments', methods=['GET', 'OPTIONS'])
def get_assignments(): return get_assignments_controller(get_db_connection) if request.method != 'OPTIONS' else ('', 204)
@app.route('/api/llm/assignments', methods=['PUT', 'OPTIONS'])
def update_assignments(): return update_assignments_controller(get_db_connection) if request.method != 'OPTIONS' else ('', 204)
@app.route('/api/llm/assignments', methods=['DELETE', 'OPTIONS'])
def delete_assignments(): return delete_assignments_controller(get_db_connection) if request.method != 'OPTIONS' else ('', 204)

# Create necessary folders if not exist
for folder in [GRAPH_FOLDER, UPLOAD_FOLDER, TEMP_UPLOAD_FOLDER]:
    os.makedirs(folder, exist_ok=True)


MYSQL_URI = (
    f"mysql+pymysql://{MYSQL_CONFIG['user']}:{quote_plus(MYSQL_CONFIG['password'])}"
    f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}"
)

engine = create_engine(MYSQL_URI, pool_recycle=3600, pool_pre_ping=True)



@app.get("/")
def health_check():
    return "API is running"



# Helper function to create a new mysql.connector connection for the controllers
def get_db_connection():
    """Establishes and returns a raw mysql.connector connection."""
    try:
        conn = mysql.connector.connect(
            host=MYSQL_CONFIG['host'],
            port=MYSQL_CONFIG['port'],
            user=MYSQL_CONFIG['user'],
            password=MYSQL_CONFIG['password'],
            database=MYSQL_CONFIG['database']
        )
        return conn
    except mysql.connector.Error as err:
        print(f"Error connecting to MySQL DB: {err}")
        return None
# ------------------- Flask App -------------------
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024 * 1024


UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")  
os.makedirs(UPLOAD_FOLDER, exist_ok=True) 

  
# Global thread pool
executor = ThreadPoolExecutor(max_workers=8) 


# ---------------------------------- API Endpoints ---------------------------------

def execute_dynamic_controller(workspace_id, prompt_type, default_controller_func, get_conn_func):
    conn = get_conn_func()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Prioritize specific category over global
            query = """
                SELECT custom_prompt, data_category 
                FROM workspace_prompts 
                WHERE workspace_id = %s AND prompt_type = %s 
                ORDER BY CASE WHEN data_category = 'global' THEN 1 ELSE 0 END
                LIMIT 1
            """
            cursor.execute(query, (workspace_id, prompt_type))
            res = cursor.fetchone()
            
            # If not found for this workspace, check global fallback (workspace_id = 0)
            if not res:
                cursor.execute(query, (0, prompt_type))
                res = cursor.fetchone()
                
            if res and res.get('custom_prompt'):
                code_str = res['custom_prompt']
                cat = res.get('data_category', 'unknown')
                
                exec_env = {}
                exec_env.update(globals())
                
                try:
                    exec(code_str, exec_env)
                except Exception as e:
                    print(f"Error parsing dynamic code for {prompt_type}: {e}")
                    raise e
                
                func_name = default_controller_func.__name__
                if func_name in exec_env:
                    print(f"Executing dynamic controller for {prompt_type} (Category: {cat})")
                    return exec_env[func_name](get_conn_func)
                else:
                    print(f"Dynamic code did not define {func_name}. Falling back to default.")
        except Exception as e:
            import traceback
            print(f"Error executing dynamic {prompt_type} code: {e}")
            traceback.print_exc()
        finally:
            conn.close()
            
    # Fallback to the default filesystem controller
    return default_controller_func(get_conn_func)

def get_workspace_id_from_req(get_conn_func):
    data = request.get_json(silent=True) or {}
    workspace_id = data.get("workspace_id")
    if workspace_id:
        return workspace_id
        
    session_id = data.get("session_id")
    if not session_id:
        return None
        
    conn = get_conn_func()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id FROM workspaces WHERE session_id = %s", (session_id,))
            row = cursor.fetchone()
            if row:
                return row["id"]
        except Exception as e:
            pass
        finally:
            conn.close()
    return None

# Workspace Custom Prompts
@app.route("/api/prompt-types", methods=["GET"])
def api_get_prompt_types():
    return get_all_prompt_types(get_db_connection)

@app.route("/api/workspace-prompt", methods=["POST"])
def api_set_workspace_prompt():
    return set_workspace_prompt(get_db_connection)

@app.route("/api/delete-workspace-prompt", methods=["DELETE"])
def api_delete_workspace_prompt():
    return delete_workspace_prompt(get_db_connection)

@app.route("/api/workspace-prompt/<string:workspace_id>", methods=["GET"])
def api_get_workspace_prompts(workspace_id):
    return get_workspace_prompts(get_db_connection, workspace_id)

@app.route("/api/workspace-prompt/<string:workspace_id>/<string:prompt_type>", methods=["GET"])
def api_get_workspace_prompt_by_type(workspace_id, prompt_type):
    data_category = request.args.get('data_category', 'global')
    return get_workspace_prompt_by_type(get_db_connection, workspace_id, prompt_type, data_category)

@app.route("/api/workspace-prompts-all", methods=["GET"])
def api_get_all_workspace_prompts():
    return get_all_workspace_prompts(get_db_connection)



# Insight
@app.route("/insight", methods=["POST"])
def insights():
    return insights_controller()

# tracker
@app.route('/tracker', methods=['GET'])
def get_tracker_data_():
    return get_tracker_data()

# Analyze Files
@app.route("/analyze_files", methods=["POST"])
def analyze():
    return analyze_controller()



@app.route('/upload', methods=['POST'])
def process_books_route():
    return process_books()


# RAG Chat (vector database)
@app.route("/rag_chat", methods=["POST"])
def rag_chat_route():
    return rag_chat_controller()

# View Info
@app.route("/view_info", methods=["GET"])
def view_analyze():
    return view_analyze_controller() 


@app.route("/graphs/<filename>")
def send_graph(filename):
    return send_from_directory(GRAPH_FOLDER, filename)

 

@app.route("/upload_files", methods=["POST"])
def upload_files():
    return upload_files_controller()


@app.route("/uploadfile_details", methods=["POST"])
def get_analyze_summary_():
    return get_analyze_summary_controller()

@app.route("/upload_files_count", methods=["POST"])
def upload_files_count():
    return upload_files_count_controller()

@app.route("/delete_session/<string:session_name>", methods=["DELETE"])
def delete_session(session_name):
    return delete_session_controller(session_name)


# --- NEW ROUTE: LOGIN ---

@app.route("/register", methods=["POST"])
def register():
    return register_user_controller(get_db_connection)

@app.route('/login', methods=['POST'])
def login_route():
    return login()


# 🆕 NEW ROUTE HERE
@app.route('/successful_sessions', methods=['GET'])
def get_successful_sessions_route():
    return fetch_successful_sessions()


@app.route('/update_active_sessions', methods=['POST'])
def update_active_sessions_route():
    return update_active_sessions_controller()


# =======================
# EXPERT / ADMIN ROUTES
# =======================

#admin, expert, superadmin login (Staff Table)
@app.route("/admin_expert_login", methods=["POST"])
def staff_login():
    """Route for Super Admin, Admin, and Expert Login"""
    return staff_login_controller()

# Admin login (Users Table)
@app.route("/admin_login", methods=["POST"])
def admin_login_route():
    return admin_login_auth()

#superadmin can ceate multiple admin and expert 
@app.route('/admin_expert_registration', methods=['POST'])
def create_staff():
    return create_staff_account_controller()

# NEW: Route to get list of admins/experts
@app.route("/admin_expert_list", methods=["GET"])
def get_staff_list():
    return get_all_staff_controller()


@app.route("/generate_captcha", methods=["GET"])
def generate_captcha():
    return generate_captcha_controller(get_db_connection)



# ==========================
#      SEO API ROUTES
# ==========================


@app.route("/invoices/<filename>")
def serve_invoice(filename):
    return send_from_directory("invoices", filename)

@app.route("/logo")
def logo():
    return send_from_directory("logo", "newlogo.png")
# --- Database Connector Routes ---

@app.route("/create_connector", methods=["POST"])
def create_db_connector():
    """Saves connection details from the UI you shared"""
    return create_connector_controller()

@app.route("/agent/query_db", methods=["POST"])
def agent_query_db():
    """The endpoint your Agent will call to 'process the db directly'"""
    return agent_query_controller()


@app.route("/connect-external-db", methods=["POST"])
def connect_external_db_controller():
    return connect_external_db()

@app.route("/apply", methods=["POST"])
def apply_external_sync_controller():
    return apply_external_sync()    

# ─────────────────────────────────────────────
# SAP HANA METADATA DISCOVERY (Strictly Metadata, No Data Migration)
# ─────────────────────────────────────────────
from controllers.hana_metadata_controller import (
    discover_hana_metadata_controller,
    get_hana_metadata_controller,
    build_hana_kgraph_controller,
    get_hana_kgraph_status_controller
)

@app.route("/hana/discover-metadata", methods=["POST"])
def hana_discover_metadata_route():
    return discover_hana_metadata_controller()

@app.route("/hana/metadata", methods=["GET"])
def hana_get_metadata_route():
    return get_hana_metadata_controller()

@app.route("/hana/build-kgraph", methods=["POST"])
def hana_build_kgraph_route():
    return build_hana_kgraph_controller()

@app.route("/hana/kgraph-status", methods=["GET"])
def hana_kgraph_status_route():
    return get_hana_kgraph_status_controller()

# ─────────────────────────────────────────────
# WEB SEARCH
# ─────────────────────────────────────────────

@app.route("/search", methods=["POST"])
def search():
    return web_search_controller(get_db_connection)

@app.route("/save-result", methods=["POST"])
def save_result():
    return save_result_controller(get_db_connection)

@app.route("/saved-results", methods=["GET"])
def get_saved_results():
    return get_saved_results_controller(get_db_connection)

@app.route("/saved-results/<string:saved_id>", methods=["DELETE"])
def delete_saved_result(saved_id):
    return delete_saved_result_controller(get_db_connection, saved_id)

@app.route("/saved-content/describe", methods=["POST"])
def saved_content_describe():
    return saved_content_describe_controller(get_db_connection)

@app.route("/saved-content/chat", methods=["POST"])
def saved_content_chat():
    return saved_content_chat_controller(get_db_connection)

@app.route("/sheet/scan", methods=["POST"])
def sheet_scan():
    return sheet_scan_controller(get_db_connection)

@app.route("/sheet/describe", methods=["POST"])
def sheet_describe():
    return sheet_describe_controller(get_db_connection)

@app.route("/sheet/chat", methods=["POST"])
def sheet_chat():
    return sheet_chat_controller(get_db_connection)

# --- Database Connector Routes ---
@app.route('/create_workspace', methods=['POST'])
def create_workspace_route():
    return create_workspace_controller(get_db_connection)


# 1. Update your existing POST route to pass the get_db_connection helper
@app.route("/create_connectors", methods=["POST"])
def create_db_connectors():
    """Saves connection details from the UI you shared"""
    return create_connector_controllers(get_db_connection)


# 2. Add the NEW GET route to fetch the history
@app.route('/connection_history', methods=['GET', 'DELETE'])
def connection_history():
    if request.method == 'DELETE':
        return delete_connection_history_controller(get_db_connection)
    return get_connection_history_controller(get_db_connection)

@app.route("/workspace_history", methods=["GET"])
def workspace_history():
    """Returns connection history for a specific workspace/session"""
    return get_workspace_history_controller(get_db_connection)

@app.route('/workspaces', methods=['GET'])
def get_user_workspaces_route():
    """Returns a list of all workspaces for a specific user"""
    return get_user_workspaces_controller(get_db_connection)


# 3. Your agent query route stays exactly the same
@app.route("/agent/query_dbs", methods=["POST"])
def agent_query_dbs():
    """The endpoint your Agent will call to 'process the db directly'"""
    return agent_query_controllers()

@app.route("/saved_credentials", methods=["GET"])
def get_saved_credentials():
    """Fetches a user's saved database credentials"""
    return get_saved_credentials_controller(get_db_connection)

@app.route("/session-chat", methods=["POST"])
def session_chat():
    # Dynamic DB controller execution is bypassed to always run local controller code
    return session_rag_chat_controller(get_db_connection)

@app.route("/api/store-chat-session", methods=["POST"])
def store_chat_session():
    return store_hub_data_controller(get_db_connection)

@app.route("/api/update-chat-session", methods=["POST"])
def update_chat_session():
    return update_hub_chat_controller(get_db_connection)

@app.route('/api/apply_bulk_sync', methods=['POST']) 
def apply_bulk_sync_route():
    return apply_bulk_sync()

@app.route("/session-sources", methods=["GET"])
def session_sources():
    return session_sources_controller(get_db_connection)

from controllers.session_analysis_controller import session_analysis_controller
from controllers.session_history_controller import get_session_analysis_history

@app.route("/session-analysis", methods=["POST"])
def session_analysis():
    # Dynamic DB controller execution is bypassed to always run local controller code
    # workspace_id = get_workspace_id_from_req(get_db_connection)
    # if workspace_id is not None:
    #     return execute_dynamic_controller(workspace_id, "session_analysis_controller_code", session_analysis_controller, get_db_connection)
    return session_analysis_controller(get_db_connection)

@app.route("/session-analysis-history", methods=["GET"])
def session_analysis_history():
    return get_session_analysis_history(get_db_connection)

@app.route("/set-active-workspace", methods=["POST"])
def set_active_workspace_controller():
    return set_active_workspace(get_db_connection)

@app.route("/upload_universal_dump", methods=["POST"])
def upload_universal_dump_route():
    return upload_universal_dump_controller(get_db_connection)

@app.route("/upload_csv", methods=["POST"])
def upload_csv_route():
    return upload_csv_controller(get_db_connection)


@app.route('/api/admin/chats', methods=['GET'])
def admin_chats():
    return admin_get_chats_controller(get_db_connection)

@app.route('/api/admin/chats/toggle-public', methods=['POST'])
def admin_chats_toggle_public():
    from controllers.admin_knowledge_controller import admin_toggle_public_controller
    return admin_toggle_public_controller(get_db_connection)

@app.route('/api/admin/chats/share', methods=['POST'])
def admin_chats_share():
    from controllers.admin_knowledge_controller import admin_share_chat_controller
    return admin_share_chat_controller(get_db_connection)

@app.route('/api/admin/push_to_kg', methods=['POST'])
def admin_push_kg():
    return admin_push_knowledge_controller(get_db_connection)


@app.route('/api/admin/staged_knowledge', methods=['GET'])
def admin_staged_knowledge():
    return admin_get_staged_knowledge_controller(get_db_connection)

@app.route('/api/admin/trigger_indexing', methods=['POST'])
def admin_trigger_indexing():
    return admin_trigger_indexing_controller(get_db_connection)


@app.route('/api/admin/kg_schedule', methods=['POST'])
def admin_set_kg_schedule():
    return admin_set_schedule_controller(get_db_connection)

@app.route('/api/admin/kg_schedule', methods=['GET'])
def admin_get_kg_schedule():
    return admin_get_schedule_controller(get_db_connection)

@app.route('/users', methods=['GET'])
def get_all_users():
    return get_all_users_controller(get_db_connection)

@app.route('/assign_workspace_users', methods=['POST'])
def assign_workspace_users():
    return assign_workspace_users_controller(get_db_connection)

@app.route('/workspace_users', methods=['POST'])
def get_workspace_users():
    return get_workspace_users_controller(get_db_connection)

@app.route('/remove_workspace_user', methods=['DELETE'])
def remove_workspace_user():
    return remove_workspace_user_controller(get_db_connection)

@app.route("/get-workspace", methods=["GET"])
def get_user_workspaces_simple_controller():
    return get_user_workspaces_simple(get_db_connection)


@app.route("/create_user", methods=["POST"])
def create_user():
    return create_user_controller(get_db_connection)


@app.route("/edit_user/<int:user_id>", methods=["POST"])
def edit_user(user_id):
    return edit_user_controller(get_db_connection, user_id)

@app.route("/change_password", methods=["POST"])
def change_password():
    return change_password_controller()

@app.route("/api/user/password", methods=["GET"])
def get_admin_password():
    return get_admin_password_controller()

@app.route("/api/kgraphs", methods=["GET"])
def get_all_kgraphs():
    return get_all_kgraphs_controller(get_db_connection)


@app.route("/delete_user/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    return delete_user_controller(get_db_connection, user_id)

@app.route("/session-chat-history", methods=["GET", "POST", "DELETE"])
def session_chat_history():
    return session_chat_history_controller(get_db_connection)

@app.route("/upload_chunk", methods=["POST"])
def upload_chunk_controller_route():
    return upload_chunk_controller(get_db_connection)
       


from controllers.import_csv_controller import import_csv_data
from controllers.import_sql_controller import import_sql_data

@app.route("/import-csv-data", methods=["POST"])
def import_csv_data_controller():
    return import_csv_data(get_db_connection)

@app.route("/import-sql-data", methods=["POST"])
def import_sql_data_controller():
    return import_sql_data(get_db_connection)


# FTP Connector — test & save credentials
@app.route("/ftp/connect", methods=["POST"])
def ftp_connect():
    return ftp_connect_controller(get_db_connection)

# FTP Connector — trigger an immediate fetch (returns job_id)
@app.route("/ftp/fetch", methods=["POST"])
def ftp_fetch():
    return ftp_fetch_controller(get_db_connection)

# FTP Connector — SSE stream for incremental fetch progress
@app.route("/ftp/progress/<string:job_id>", methods=["GET"])
def ftp_progress(job_id):
    return ftp_progress_controller(job_id)

# FTP Connector — create / update a recurring schedule
@app.route("/ftp/schedule", methods=["POST"])
def ftp_schedule():
    return ftp_schedule_controller(get_db_connection)

# FTP Connector — delete / pause a schedule
@app.route("/ftp/schedule", methods=["DELETE"])
def ftp_delete_schedule():
    return ftp_delete_schedule_controller(get_db_connection)

# FTP Connector — get current schedule info
@app.route("/ftp/schedule", methods=["GET"])
def ftp_get_schedule():
    return ftp_get_schedule_controller(get_db_connection)

# FTP Connector — historical fetch log
@app.route("/ftp/fetch_log", methods=["GET"])
def ftp_fetch_log():
    return ftp_fetch_log_controller(get_db_connection)

# Workspace Delete
@app.route("/workspaces/<int:workspace_id>", methods=["DELETE"])
def delete_workspace(workspace_id):
    return delete_workspace_controller(workspace_id, get_db_connection)

@app.route("/workspace-types", methods=["GET"])
def get_workspace_types():
    return get_workspace_types_controller(get_db_connection)

@app.route("/workspace-types", methods=["POST"])
def create_workspace_type():
    return create_workspace_type_controller(get_db_connection)




# ==========================================
# Agent Actions API
# ==========================================

@app.route("/agent-actions", methods=["POST"])
def log_agent_action():
    """Log a new agent action step into agent_actions table."""
    return log_agent_action_controller()

@app.route("/agent-actions", methods=["GET"])
def get_agent_actions():
    """List agent actions filtered by session_id or user_id."""
    return get_agent_actions_controller()

@app.route("/agent-actions/<int:action_id>", methods=["GET"])
def get_agent_action_detail(action_id):
    """Fetch a single agent action by its id."""
    return get_agent_action_detail_controller(action_id)

@app.route("/agent-actions/<int:action_id>", methods=["PATCH"])
def update_agent_action(action_id):
    """Update status/output of an existing agent action."""
    return update_agent_action_controller(action_id)

@app.route("/agent-actions", methods=["DELETE"])
def delete_agent_actions():
    """Delete all agent actions for a given session_id."""
    return delete_agent_actions_controller()

# # ==========================================
# # Pricing API
# # ==========================================
# 
# @app.route("/api/pricing", methods=["GET"])
# def get_pricing():
#     return get_pricing_controller()
# 
# @app.route("/api/pricing", methods=["POST"])
# def create_pricing():
#     return create_pricing_controller()
# 
# @app.route("/api/pricing/<int:plan_id>", methods=["PUT"])
# def update_pricing(plan_id):
#     return update_pricing_controller(plan_id)
# 
# @app.route("/api/pricing/<int:plan_id>", methods=["DELETE"])
# def delete_pricing(plan_id):
#     return delete_pricing_controller(plan_id)
# 
# @app.route("/api/user/usage-stats", methods=["GET"])
# def get_user_usage_stats():
#     from controllers.pricing_controller import get_user_usage_stats_controller
#     return get_user_usage_stats_controller()

# ==========================================
# Company API
# ==========================================

@app.route('/companies', methods=['GET'])
def get_companies():
    return get_companies_controller(get_db_connection)

@app.route('/companies', methods=['POST'])
def create_company():
    return create_company_controller(get_db_connection)

@app.route('/companies/<int:company_id>', methods=['PUT'])
def update_company(company_id):
    return update_company_controller(get_db_connection, company_id)

@app.route('/companies/<int:company_id>', methods=['DELETE'])
def delete_company(company_id):
    return delete_company_controller(get_db_connection, company_id)


# ==========================================
# Action Approval (HITL) API
# ==========================================

@app.route("/action-approve", methods=["POST"])
def action_approve():
    """Approve a pending HITL action and trigger background execution."""
    return action_approve_controller(get_db_connection)

@app.route("/action-reject", methods=["POST"])
def action_reject():
    """Reject (skip) a pending HITL action."""
    return action_reject_controller(get_db_connection)

@app.route("/action-pending", methods=["GET"])
def action_pending():
    """List all pending-approval actions for a session."""
    return action_pending_controller(get_db_connection)

# ---------------------------------------------


@app.route("/default-dashboard-metrics", methods=["POST"])
def default_dashboard_metrics():
    return default_dashboard_metrics_controller(get_db_connection)

@app.route("/tyre-sales-data", methods=["POST"])
def tyre_sales_data():
    return tyre_sales_data_controller(get_db_connection)

@app.route("/dashboard-filters/", methods=["GET"])
def dashboard_filters():
    return dashboard_filters_controller(get_db_connection)

@app.route("/sales-by-zone", methods=["POST"])
def sales_by_zone():
    return sales_by_zone_data_controller(get_db_connection)    


@app.route("/year-wise-sales-comparison", methods=["POST"])
def year_wise_sales_comparison():
    return year_wise_sales_comparison_controller(get_db_connection)

@app.route("/available-years", methods=["GET"])
def available_years():
    return available_years_controller(get_db_connection)    

from controllers.category_sales import get_category_sales_controller

@app.route("/category-sales", methods=["GET"])
def category_sales():
    return get_category_sales_controller(get_db_connection)

@app.route("/sales-revenue", methods=["GET", "POST"])
def sales_revenue():
    return get_sales_revenue_data_controller(get_db_connection)

@app.route("/sales-by-account-category", methods=["GET", "POST"])
def sales_by_account_category():
    return get_sales_by_account_category_controller(get_db_connection)

@app.route("/non-billed-accounts-pct", methods=["GET", "POST"])
def non_billed_accounts_pct():
    return get_non_billed_accounts_controller(get_db_connection)

@app.route("/overdue-pct", methods=["GET", "POST"])
def overdue_pct():
    return get_overdue_pct_controller(get_db_connection)

@app.route("/exposure-pct", methods=["GET", "POST"])
def exposure_pct():
    return get_exposure_pct_controller(get_db_connection)



@app.route("/export-domestic-sales-report", methods=["GET", "POST"])
def export_domestic_sales_report():
    return export_domestic_sales_report_controller(get_db_connection)

@app.route("/export-domestic-sales-preview", methods=["GET", "POST"])
def export_domestic_sales_preview():
    return export_domestic_sales_preview_controller(get_db_connection)

from controllers.master_data_controller import (
    get_data_categories, create_data_category, update_data_category, delete_data_category,
    get_prompt_types_crud, create_prompt_type, update_prompt_type, delete_prompt_type
)

# --- Data Categories CRUD ---
@app.route("/api/data-categories", methods=["GET"])
def api_get_data_categories():
    return get_data_categories(get_db_connection)

@app.route("/api/data-categories", methods=["POST"])
def api_create_data_category():
    return create_data_category(get_db_connection)

@app.route("/api/data-categories/<int:id>", methods=["PUT"])
def api_update_data_category(id):
    return update_data_category(get_db_connection, id)

@app.route("/api/data-categories/<int:id>", methods=["DELETE"])
def api_delete_data_category(id):
    return delete_data_category(get_db_connection, id)

# --- Prompt Types CRUD ---
@app.route("/api/prompt-types-crud", methods=["GET"])
def api_get_prompt_types_crud():
    return get_prompt_types_crud(get_db_connection)

@app.route("/api/prompt-types-crud", methods=["POST"])
def api_create_prompt_type():
    return create_prompt_type(get_db_connection)

@app.route("/api/prompt-types-crud/<int:id>", methods=["PUT"])
def api_update_prompt_type(id):
    return update_prompt_type(get_db_connection, id)

@app.route("/api/prompt-types-crud/<int:id>", methods=["DELETE"])
def api_delete_prompt_type(id):
    return delete_prompt_type(get_db_connection, id)

# ==========================================
# Report Recipients API
# ==========================================


# ==========================================
# Scheduled Reports API
# ==========================================
# from controllers.pricing_controller import cleanup_audit_memory_chats

@app.route("/api/scheduled-reports", methods=["GET"])
def get_scheduled_reports():
    return get_schedules_controller()

@app.route("/api/scheduled-reports", methods=["POST"])
def add_scheduled_report():
    return add_schedule_controller()

@app.route("/api/scheduled-reports/<int:schedule_id>", methods=["PUT"])
def update_scheduled_report(schedule_id):
    return update_schedule_controller(schedule_id)

@app.route("/api/scheduled-reports/<int:schedule_id>", methods=["DELETE"])
def delete_scheduled_report(schedule_id):
    return delete_schedule_controller(schedule_id)



@app.route("/kgraph-rollback", methods=["POST"])
def rollback_kgraph():
    return rollback_kgraph_controller(get_db_connection)

@app.route("/api/kgraph/ontology-analysis", methods=["GET", "POST"])
def get_kgraph_ontology_analysis():
    return get_kgraph_ontology_analysis_controller(get_db_connection)

@app.route("/api/store-chat-session", methods=["POST"])
def api_store_hub_data():
    return store_hub_data_controller(get_db_connection)



if __name__ == '__main__':
    # Start APScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=check_and_send_scheduled_reports, trigger="interval", minutes=1)
    
    # Run the chat cleanup job every day at midnight
    # scheduler.add_job(func=cleanup_audit_memory_chats, trigger="cron", hour=0, minute=0)
    
    scheduler.start()


    app.run(host="0.0.0.0", port=3019, debug=True, use_reloader=False)