"""LangGraph nodes and routing functions for the planning graph.

Contract for every node: receive the full ``WorkflowState``, return a partial dict with
only the keys it changes. Routing functions read the state and return the name of the
next node; they never modify state.

Logs record IDs, counts and codes only - never the instruction text or argument values.
"""

import logging
from typing import Any, Literal

from app.agents.planners.base import Planner, PlannerError
from app.agents.state import WorkflowState
from collections.abc import Collection

from app.agents.validation import document_availability_issues, validate_draft
from app.schemas.plan import DraftPlan

logger = logging.getLogger(__name__)


class PlanningNodes:
    def __init__(self, planner: Planner, *, available_documents: Collection[str] | None = None) -> None:
        self._planner = planner
        # Set in mock execution mode: read_document targets must exist (see validation.py).
        self._available_documents = available_documents

    def planner_node(self, state: WorkflowState) -> dict[str, Any]:
        attempt = state["planning_attempts"] + 1
        try:
            draft = self._planner.plan(state["user_input"], max_steps=state["max_steps"])
        except PlannerError as exc:
            logger.warning(
                "node=planner workflow_id=%s attempt=%d error=%s retryable=%s",
                state["workflow_id"], attempt, exc.code, exc.retryable,
            )
            return {
                "current_step": "planner",
                "status": "planning_failed",
                "planning_attempts": attempt,
                "last_error_retryable": exc.retryable,
                "draft_plan": None,
                "errors": [
                    {"code": exc.code, "message": f"Attempt {attempt}: {exc.message}", "retryable": exc.retryable}
                ],
            }
        except Exception as exc:  # unexpected bug in a planner: fail closed, no retry
            logger.exception("node=planner workflow_id=%s unexpected error", state["workflow_id"])
            return {
                "current_step": "planner",
                "status": "planning_failed",
                "planning_attempts": attempt,
                "last_error_retryable": False,
                "draft_plan": None,
                "errors": [
                    {"code": "PLANNER_ERROR", "message": f"Unexpected planner error ({type(exc).__name__}).", "retryable": False}
                ],
            }

        logger.info(
            "node=planner workflow_id=%s attempt=%d steps=%d", state["workflow_id"], attempt, len(draft.steps)
        )
        return {
            "current_step": "planner",
            "status": "validating",
            "planning_attempts": attempt,
            "last_error_retryable": False,
            "draft_plan": draft.model_dump(),
            "planned_steps": [s.model_dump() for s in draft.steps],
            "plan_summary": draft.summary,
        }

    def validator_node(self, state: WorkflowState) -> dict[str, Any]:
        draft = DraftPlan.model_validate(state["draft_plan"])
        plan, issues = validate_draft(draft, max_steps=state["max_steps"])
        if plan is not None and self._available_documents is not None:
            issues = document_availability_issues(plan, self._available_documents)
            if issues:
                plan = None
        logger.info(
            "node=validator workflow_id=%s valid=%s issues=%s",
            state["workflow_id"], plan is not None, [i.code for i in issues],
        )
        if plan is None:
            return {
                "current_step": "validator",
                "status": "invalid",
                "validation_issues": [i.model_dump() for i in issues],
            }
        return {
            "current_step": "validator",
            "status": "valid",
            "validation_issues": [],
            "planned_steps": [s.model_dump(mode="json") for s in plan.steps],
            "plan_summary": plan.summary,
        }

    def reporter_node(self, state: WorkflowState) -> dict[str, Any]:
        steps = state["planned_steps"]
        logger.info("node=reporter workflow_id=%s steps=%d", state["workflow_id"], len(steps))
        return {
            "current_step": "reporter",
            "status": "planned",
            "results": {
                "summary": state["plan_summary"],
                "step_count": len(steps),
                "integrations": sorted({s["integration"] for s in steps}),
                "execution": "not_executed",
                "message": "Plan validated. Nothing was executed (Phase 2 plans only).",
            },
        }

    def error_node(self, state: WorkflowState) -> dict[str, Any]:
        issues = state["validation_issues"]
        logger.info("node=error workflow_id=%s validation_issues=%d", state["workflow_id"], len(issues))
        if issues:
            return {
                "current_step": "error",
                "status": "invalid",
                "errors": [
                    {
                        "code": "PLAN_VALIDATION_FAILED",
                        "message": f"The generated plan failed validation with {len(issues)} issue(s).",
                        "retryable": False,
                    }
                ],
                "results": {"execution": "not_executed", "message": "Plan rejected by validator."},
            }
        return {
            "current_step": "error",
            "status": "failed",
            "results": {
                "execution": "not_executed",
                "message": f"Planning failed after {state['planning_attempts']} attempt(s).",
            },
        }


# ---------- conditional edge routers ----------


def route_after_planner(state: WorkflowState) -> Literal["validator", "planner", "error"]:
    if state["status"] == "validating":
        return "validator"
    if state["last_error_retryable"] and state["planning_attempts"] < state["max_planning_attempts"]:
        return "planner"  # retry
    return "error"


def route_after_validator(state: WorkflowState) -> Literal["reporter", "error"]:
    return "reporter" if state["status"] == "valid" else "error"
