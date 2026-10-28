"""
Action State Machine
====================
Manages lifecycle transitions for agent_actions rows.
Provides:
  - Legal state transition validation
  - Exponential backoff retry (1s -> 2s -> 4s)
  - Dead Letter Queue (DLQ) marking after max retries
"""

import time
import threading
from enum import Enum
from typing import Callable, Optional
from database.agent_action_service import update_action, log_action


class ActionState(str, Enum):
    PENDING            = "pending"
    REQUIRES_APPROVAL  = "requires_approval"
    RUNNING            = "running"
    RETRYING           = "retrying"
    SUCCESS            = "success"
    FAILED             = "failed"
    SKIPPED            = "skipped"
    DLQ                = "dlq"


# Legal transitions map
_TRANSITIONS = {
    ActionState.PENDING:           [ActionState.REQUIRES_APPROVAL, ActionState.RUNNING, ActionState.SKIPPED],
    ActionState.REQUIRES_APPROVAL: [ActionState.RUNNING, ActionState.SKIPPED],
    ActionState.RUNNING:           [ActionState.SUCCESS, ActionState.FAILED, ActionState.RETRYING],
    ActionState.RETRYING:          [ActionState.RUNNING, ActionState.FAILED, ActionState.DLQ],
    ActionState.FAILED:            [ActionState.RETRYING, ActionState.DLQ],
    ActionState.SUCCESS:           [],
    ActionState.SKIPPED:           [],
    ActionState.DLQ:               [],
}


def transition(action_id: int, new_state: ActionState, extra_kwargs: dict = None) -> bool:
    """
    Transitions an action to a new state.
    Validates the transition is legal before persisting.
    Returns True if transition was applied.
    """
    try:
        kwargs = {"status": new_state.value}
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        update_action(action_id, **kwargs)
        print(f"[StateMachine] Action {action_id} -> {new_state.value}")
        return True
    except Exception as e:
        print(f"[StateMachine] transition error: {e}")
        return False


def move_to_dlq(action_id: int, reason: str):
    """Mark an action as Dead-Letter Queue after all retries are exhausted."""
    transition(action_id, ActionState.DLQ, {
        "error_message": f"[DLQ] Max retries exhausted. Last error: {reason}"
    })
    print(f"[StateMachine] Action {action_id} moved to DLQ: {reason}")


def retry_with_backoff(
    action_id: int,
    fn: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
    on_success: Optional[Callable] = None,
    on_dlq: Optional[Callable] = None,
):
    """
    Executes fn() with exponential backoff retry.
    Delays: 1s, 2s, 4s (doubles each time).
    On success: transitions to SUCCESS.
    On all retries exhausted: transitions to DLQ.

    Args:
        action_id: The agent_actions row id
        fn: Callable that performs the action. Should raise on failure.
        max_retries: Max number of retry attempts (default 3)
        base_delay: Initial delay in seconds (default 1.0)
        on_success: Optional callback after success
        on_dlq: Optional callback after DLQ
    """
    def _execute():
        last_error = None
        delay = base_delay

        for attempt in range(1, max_retries + 1):
            try:
                # Mark as running/retrying
                if attempt == 1:
                    transition(action_id, ActionState.RUNNING)
                else:
                    transition(action_id, ActionState.RETRYING, {
                        "error_message": f"Retry attempt {attempt}/{max_retries}. Last: {last_error}"
                    })
                    print(f"[StateMachine] Retry attempt {attempt}/{max_retries} for action {action_id} after {delay}s")
                    time.sleep(delay)
                    delay *= 2   # Exponential backoff
                    transition(action_id, ActionState.RUNNING)

                # Execute the action function
                result = fn()

                # Success
                exec_ms = None
                if isinstance(result, dict):
                    output = result.get("answer") or result.get("action_output")
                else:
                    output = str(result)

                transition(action_id, ActionState.SUCCESS, {
                    "action_output": output
                })

                if on_success:
                    on_success(result)

                print(f"[StateMachine] Action {action_id} succeeded on attempt {attempt}")
                return result

            except Exception as e:
                last_error = str(e)
                print(f"[StateMachine] Action {action_id} attempt {attempt} failed: {last_error}")

        # All retries exhausted -> DLQ
        move_to_dlq(action_id, last_error)
        if on_dlq:
            on_dlq(last_error)

    # Run in a background thread so HTTP response is not blocked
    thread = threading.Thread(target=_execute, daemon=True)
    thread.start()
    return thread
