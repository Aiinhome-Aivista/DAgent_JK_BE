"""
Agent Actions Controller
========================
API endpoints to log, retrieve, and delete entries in the `agent_actions` table.

Routes (registered in app.py)
------------------------------
POST   /agent-actions          → log a new action
GET    /agent-actions          → list actions (by session_id or user_id)
GET    /agent-actions/<id>     → get one action by id
DELETE /agent-actions          → delete all actions for a session
"""

import json
from flask import request, jsonify
from database.agent_action_service import (
    log_action,
    update_action,
    get_actions_by_session,
    get_actions_by_user,
    get_action_by_id,
    delete_actions_by_session,
)


# ─────────────────────────────────────────────────────────────
# POST /agent-actions  —  Log a new agent action
# ─────────────────────────────────────────────────────────────
def log_agent_action_controller():
    """
    Payload (JSON):
    {
        "session_id"        : "uuid-...",     required
        "user_id"           : 1,              required
        "action_name"       : "query_db",     required
        "action_type"       : "tool_call",    required

        "workspace_id"      : 3,              optional
        "action_input"      : {...},          optional (dict)
        "action_output"     : "...",          optional
        "tool_name"         : "sql_executor", optional
        "tool_args"         : {...},          optional (dict)
        "tool_result"       : "...",          optional
        "status"            : "success",      optional (default: pending)
        "error_message"     : "...",          optional
        "execution_time_ms" : 240,            optional
        "step_index"        : 0,              optional
        "agent_name"        : "rag_agent",    optional
        "model_used"        : "gemini-2.5"    optional
    }
    """
    data = request.get_json(silent=True) or {}

    # ── Required fields ──────────────────────────────────────
    session_id   = data.get("session_id")
    user_id      = data.get("user_id")
    action_name  = data.get("action_name")
    action_type  = data.get("action_type")

    if not session_id:
        return jsonify({"status": "error", "message": "session_id is required"}), 400
    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required"}), 400
    if not action_name:
        return jsonify({"status": "error", "message": "action_name is required"}), 400
    if not action_type:
        return jsonify({"status": "error", "message": "action_type is required"}), 400

    # ── Optional fields ───────────────────────────────────────
    new_id = log_action(
        session_id   = session_id,
        user_id      = user_id,
        action_name  = action_name,
        action_type  = action_type,
        workspace_id      = data.get("workspace_id"),
        action_input      = data.get("action_input"),
        action_output     = data.get("action_output"),
        tool_name         = data.get("tool_name"),
        tool_args         = data.get("tool_args"),
        tool_result       = data.get("tool_result"),
        status            = data.get("status", "pending"),
        error_message     = data.get("error_message"),
        execution_time_ms = data.get("execution_time_ms"),
        step_index        = data.get("step_index"),
        agent_name        = data.get("agent_name"),
        model_used        = data.get("model_used"),
    )

    if new_id is None:
        return jsonify({"status": "error", "message": "Failed to log action"}), 500

    return jsonify({
        "status"    : "success",
        "statuscode": 201,
        "message"   : "Action logged successfully",
        "action_id" : new_id,
    }), 201


# ─────────────────────────────────────────────────────────────
# PATCH /agent-actions/<action_id>  —  Update an existing action
# ─────────────────────────────────────────────────────────────
def update_agent_action_controller(action_id):
    """
    Payload (JSON) — all optional, only provided fields are updated:
    {
        "status"            : "success",
        "action_output"     : "...",
        "tool_result"       : "...",
        "error_message"     : "...",
        "execution_time_ms" : 340
    }
    """
    data = request.get_json(silent=True) or {}

    update_action(
        action_id,
        status            = data.get("status"),
        action_output     = data.get("action_output"),
        tool_result       = data.get("tool_result"),
        error_message     = data.get("error_message"),
        execution_time_ms = data.get("execution_time_ms"),
    )

    return jsonify({
        "status"    : "success",
        "statuscode": 200,
        "message"   : f"Action {action_id} updated",
    }), 200


# ─────────────────────────────────────────────────────────────
# GET /agent-actions  —  List actions
# Query params:
#   session_id=<str>  → filter by session
#   user_id=<int>     → filter by user  (used if session_id absent)
#   limit=<int>       → default 100
#   offset=<int>      → default 0
# ─────────────────────────────────────────────────────────────
def get_agent_actions_controller():
    session_id = request.args.get("session_id")
    user_id    = request.args.get("user_id")
    limit      = int(request.args.get("limit",  100))
    offset     = int(request.args.get("offset", 0))

    if not session_id and not user_id:
        return jsonify({
            "status" : "error",
            "message": "Either session_id or user_id is required as a query parameter",
        }), 400

    if session_id:
        actions = get_actions_by_session(session_id, limit=limit, offset=offset)
        filter_key = "session_id"
        filter_val = session_id
    else:
        actions = get_actions_by_user(int(user_id), limit=limit, offset=offset)
        filter_key = "user_id"
        filter_val = user_id

    return jsonify({
        "status"    : "success",
        "statuscode": 200,
        filter_key  : filter_val,
        "total"     : len(actions),
        "limit"     : limit,
        "offset"    : offset,
        "actions"   : actions,
    }), 200


# ─────────────────────────────────────────────────────────────
# GET /agent-actions/<action_id>  —  Single action detail
# ─────────────────────────────────────────────────────────────
def get_agent_action_detail_controller(action_id):
    action = get_action_by_id(action_id)

    if action is None:
        return jsonify({
            "status" : "error",
            "message": f"Action with id {action_id} not found",
        }), 404

    return jsonify({
        "status"    : "success",
        "statuscode": 200,
        "action"    : action,
    }), 200


# ─────────────────────────────────────────────────────────────
# DELETE /agent-actions  —  Delete all actions for a session
# Query param: session_id=<str>
# ─────────────────────────────────────────────────────────────
def delete_agent_actions_controller():
    session_id = request.args.get("session_id")

    if not session_id:
        return jsonify({
            "status" : "error",
            "message": "session_id is required as a query parameter",
        }), 400

    deleted = delete_actions_by_session(session_id)

    return jsonify({
        "status"        : "success",
        "statuscode"    : 200,
        "message"       : f"Deleted {deleted} action(s) for session {session_id}",
        "deleted_count" : deleted,
    }), 200
