"""Wraps LangGraph node functions so every node transition becomes a Langfuse observation.

The wrapper records *what the node decided* (status, outcome, counts, error codes), taken
from the partial state update the node returns. It never records the state itself, which
contains the instruction text and tool argument values.
"""

import functools
from collections.abc import Callable
from typing import Any

from app.observability.telemetry import get_telemetry

NodeFn = Callable[[dict[str, Any]], dict[str, Any]]

_SCALAR_KEYS = (
    "status", "current_step", "last_outcome", "final_status", "failed_step", "current_index",
    "planning_attempts", "last_error_retryable",
)


def summarize_update(update: dict[str, Any]) -> dict[str, Any]:
    """Safe summary of a node's partial state update: ids, codes, counts and timings only."""
    out: dict[str, Any] = {k: update[k] for k in _SCALAR_KEYS if k in update}
    if "errors" in update:
        out["error_codes"] = [e.get("code") for e in update["errors"]]
    if "validation_issues" in update:
        out["validation_issue_codes"] = [i.get("code") for i in update["validation_issues"]]
    if "planned_steps" in update:
        out["planned_steps"] = [
            {"step_number": s.get("step_number"), "integration": s.get("integration"), "action": s.get("action")}
            for s in update["planned_steps"]
        ]
    if "steps" in update:
        out["step_count"] = len(update["steps"])
    if "step_results" in update:
        out["step_results"] = [
            {"step_number": r["step_number"], "status": r["status"], "attempts": r["attempts"]}
            for r in update["step_results"]
        ]
    if "journal" in update:
        out["journaled_steps"] = [j["step_number"] for j in update["journal"]]
    if "compensation_results" in update:
        out["compensation_results"] = [
            {"step_number": c["step_number"], "status": c["status"], "attempts": c["attempts"]}
            for c in update["compensation_results"]
        ]
    if "retry_delays" in update:
        out["retry_delays_seconds"] = update["retry_delays"]
    return out


def _level_for(update: dict[str, Any]) -> str:
    if update.get("final_status") == "COMPENSATION_FAILED":
        return "ERROR"
    if update.get("errors") or update.get("status") in {"invalid", "failed", "step_failed", "rejected"}:
        return "WARNING"
    return "DEFAULT"


def current_step_number(state: dict[str, Any]) -> int | None:
    """step_number of the step an execution node is working on, if any."""
    steps, index = state.get("steps") or [], state.get("current_index")
    if isinstance(index, int) and 0 <= index < len(steps):
        return steps[index].get("step_number")
    return None


def traced_node(graph: str, node: str, fn: NodeFn) -> NodeFn:
    """Wrap a LangGraph node. Telemetry is looked up per call so it can be swapped at runtime."""

    @functools.wraps(fn)
    def wrapper(state: dict[str, Any]) -> dict[str, Any]:
        telemetry = get_telemetry()
        metadata = {"graph": graph, "node": node, "status_before": state.get("status")}
        if "planning_attempts" in state:
            metadata["planning_attempts_before"] = state["planning_attempts"]
        with telemetry.observe(
            f"{graph}.{node}",
            as_type="chain",
            workflow_id=state.get("workflow_id"),
            step_number=current_step_number(state),
            metadata=metadata,
        ) as observation:
            update = fn(state)
            observation.update(output=summarize_update(update), level=_level_for(update))
            return update

    return wrapper
