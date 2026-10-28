"""
Action Tool Registry
====================
Decorator-based plugin system for registering action tools.
New tools can be added with a single @register_action_tool decorator.
"""

from functools import wraps
from typing import Callable, Dict, Any, Optional

# Global Registry
ACTION_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_action_tool(
    name: str,
    description: str = "",
    requires_approval: bool = False,
    approval_threshold: int = 1,
):
    """
    Decorator to register a function as a named action tool.
    Args:
        name: Unique tool identifier (e.g. "send_email")
        description: Human-readable description for UI/LLM
        requires_approval: If True, HITL confirmation is needed
        approval_threshold: Require approval if recipient_count > this value
    """
    def decorator(fn: Callable):
        ACTION_REGISTRY[name] = {
            "fn": fn,
            "description": description,
            "requires_approval": requires_approval,
            "approval_threshold": approval_threshold,
        }

        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper
    return decorator


def dispatch_action(
    tool_name: str,
    params: Dict[str, Any],
    action_id: Optional[int] = None,
    skip_approval_check: bool = False,
) -> Dict[str, Any]:
    """
    Central dispatcher - routes to the correct registered tool.
    Returns dict with: status, answer, requires_approval, action_id
    """
    if tool_name not in ACTION_REGISTRY:
        return {
            "status": "failed",
            "statusCode": 400,
            "answer": f"Unknown action tool: '{tool_name}'. Available: {list(ACTION_REGISTRY.keys())}",
        }

    tool = ACTION_REGISTRY[tool_name]
    fn = tool["fn"]

    # HITL check - pause before bulk/critical actions
    if not skip_approval_check and tool["requires_approval"]:
        recipient_count = params.get("recipient_count", 1)
        if recipient_count > tool["approval_threshold"]:
            return {
                "status": "pending_approval",
                "requires_approval": True,
                "tool_name": tool_name,
                "action_id": action_id,
                "answer": params.get("approval_summary", f"Action '{tool_name}' requires approval."),
            }

    # Execute tool directly
    try:
        result = fn(params)
        return result
    except Exception as e:
        return {
            "status": "failed",
            "statusCode": 500,
            "answer": f"Tool '{tool_name}' raised an error: {e}",
        }


def list_tools() -> list:
    """Returns metadata list of all registered tools."""
    return [
        {
            "name": k,
            "description": v["description"],
            "requires_approval": v["requires_approval"],
            "approval_threshold": v["approval_threshold"],
        }
        for k, v in ACTION_REGISTRY.items()
    ]
