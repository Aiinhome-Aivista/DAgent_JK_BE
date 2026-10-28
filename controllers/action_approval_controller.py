"""
Action Approval Controller
===========================
HITL (Human-in-the-Loop) endpoints:
  POST /action-approve   - Approve and execute a pending action
  POST /action-reject    - Reject (skip) a pending action
  GET  /action-pending   - List actions awaiting approval for a session
"""

import threading
from flask import request, jsonify
from database.agent_action_service import update_action, get_pending_approvals
from helper.action_state_machine import retry_with_backoff, transition, ActionState


def action_approve_controller(get_db_connection):
    """
    POST /action-approve
    Body: { action_id: int, session_id: str, user_id: int }
    Triggers background execution of an approved action.
    """
    data = request.json or {}
    action_id = data.get("action_id")
    session_id = data.get("session_id", "")
    user_id = data.get("user_id", 1)
    instruction = data.get("instruction", "")
    recipients = data.get("recipients", [])      # list of {name, email, row_data}

    if not action_id:
        return jsonify({"status": "failed", "message": "action_id is required"}), 400

    # Move to RUNNING state
    transition(action_id, ActionState.RUNNING)

    # Execute email sending in background thread with retry
    def do_approved_email():
        from helper.email_action_handler import _do_execute_email, fetch_filtered_dataset_rows
        dataset = []
        if get_db_connection:
            dataset = fetch_filtered_dataset_rows(instruction, session_id, get_db_connection)
        result = _do_execute_email(instruction, session_id, user_id, recipients, shared_dataset=dataset)
        return result

    retry_with_backoff(
        action_id=action_id,
        fn=do_approved_email,
        max_retries=3,
        base_delay=1.0,
    )

    return jsonify({
        "status": "success",
        "statusCode": 200,
        "message": f"Action {action_id} approved and executing in background. Check /agent-actions/{action_id} for live status."
    }), 200


def action_reject_controller(get_db_connection):
    """
    POST /action-reject
    Body: { action_id: int }
    Marks the action as skipped.
    """
    data = request.json or {}
    action_id = data.get("action_id")

    if not action_id:
        return jsonify({"status": "failed", "message": "action_id is required"}), 400

    transition(action_id, ActionState.SKIPPED, {"error_message": "Rejected by user via HITL Cancel."})

    return jsonify({
        "status": "success",
        "statusCode": 200,
        "message": f"Action {action_id} has been rejected and skipped."
    }), 200


def action_pending_controller(get_db_connection):
    """
    GET /action-pending?session_id=...
    Returns all pending-approval actions for a session.
    """
    session_id = request.args.get("session_id", "")
    if not session_id:
        return jsonify({"status": "failed", "message": "session_id is required"}), 400

    try:
        actions = get_pending_approvals(session_id)
        return jsonify({"status": "success", "statusCode": 200, "actions": actions}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
