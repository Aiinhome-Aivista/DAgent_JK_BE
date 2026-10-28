import pymysql
from database.db_connection import get_db_connection

def get_prompt(workspace_id, prompt_type):
    """
    Centralized prompt loader (BYPASS MODE).
    Dynamic prompt concept is bypassed to allow local hardcoded code/prompts to run.
    Always returns None.
    """
    # ── Bypassed dynamic DB prompt loading ──
    # prompt = None
    # conn = None
    # try:
    #     conn = get_db_connection()
    #     cur = conn.cursor()
    #     
    #     is_generic_workspace = False
    #     ws_type = None
    # 
    #     if workspace_id is not None and str(workspace_id) != '0':
    #         cur.execute("SELECT workspace_type FROM workspaces WHERE id = %s", (workspace_id,))
    #         ws_row = cur.fetchone()
    #         if ws_row and ws_row.get('workspace_type'):
    #             ws_type = str(ws_row['workspace_type']).strip().lower()
    #             if ws_type == 'generic':
    #                 is_generic_workspace = True
    # 
    #     if workspace_id is not None and not is_generic_workspace:
    #         cur.execute(
    #             "SELECT custom_prompt FROM workspace_prompts WHERE workspace_id = %s AND prompt_type = %s",
    #             (workspace_id, prompt_type)
    #         )
    #         row = cur.fetchone()
    #         if row and row.get("custom_prompt") and str(row["custom_prompt"]).strip():
    #             prompt = str(row["custom_prompt"])
    # 
    #     if not prompt or not prompt.strip():
    #         if is_generic_workspace:
    #             cur.execute(
    #                 "SELECT custom_prompt FROM workspace_prompts WHERE workspace_id = 0 AND prompt_type = %s AND (data_category = 'global' OR data_category IS NULL OR data_category NOT IN ('sales', 'Sales')) LIMIT 1",
    #                 (prompt_type,)
    #             )
    #             row = cur.fetchone()
    #         else:
    #             target_cat = 'sales' if ws_type == 'sales' else 'global'
    #             cur.execute(
    #                 "SELECT custom_prompt FROM workspace_prompts WHERE workspace_id = 0 AND prompt_type = %s AND data_category = %s LIMIT 1",
    #                 (prompt_type, target_cat)
    #             )
    #             row = cur.fetchone()
    #             
    #             if not row or not row.get("custom_prompt") or not str(row["custom_prompt"]).strip():
    #                 cur.execute(
    #                     "SELECT custom_prompt FROM workspace_prompts WHERE workspace_id = 0 AND prompt_type = %s LIMIT 1",
    #                     (prompt_type,)
    #                 )
    #                 row = cur.fetchone()
    # 
    #         if row and row.get("custom_prompt") and str(row["custom_prompt"]).strip():
    #             prompt = str(row["custom_prompt"])
    # except Exception as e:
    #     print(f"[Prompt Loader] Error: {e}")
    # finally:
    #     if conn:
    #         conn.close()
    #         
    # if prompt and prompt.strip():
    #     prompt = prompt.replace("{{", "{").replace("}}", "}")
    #     return prompt

    return None
