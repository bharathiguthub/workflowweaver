"""LangGraph nodes and routing functions for the planning graph.

Contract for every node: receive the full ``WorkflowState``, return a partial dict with
only the keys it changes. Routing functions read the state and return the name of the
next node; they never modify state.

Provider fallback (PLANNER_MODE=auto): the planner node calls the provider selected by
``provider_index`` (primary first). Retryable failures are retried on the SAME provider with
the bounded backoff policy, up to ``max_planning_attempts``. Only when that provider is
still failing with an OUTAGE (HTTP 429/500/502/503, timeout, connection error) does the fallback
node switch to the next provider, which gets its own bounded attempts. Credential errors,
refusals, schema failures and invalid plans never fall back. Each draft comes whole from one
provider and is validated by the same validator; outputs are never combined.

Logs record IDs, counts and codes only - never the instruction text or argument values.
"""

import inspect
import logging
import random
import time
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, Literal

from app.agents.planners.base import Planner, PlannerError, planner_model, provider_chain
from app.agents.state import WorkflowState
from app.agents.validation import (
    document_availability_issues,
    drive_destination_issues,
    integration_target_issues,
    normalize_drive_references,
    normalize_notion_references,
    normalize_teams_references,
    orchestration_issues,
    teams_destination_issues,
    validate_draft,
)
from app.observability import get_telemetry
from app.schemas.plan import DraftPlan

if TYPE_CHECKING:
    from app.agents.execution.retry import RetryPolicy

logger = logging.getLogger(__name__)


class PlanningNodes:
    def __init__(
        self,
        planner: Planner,
        *,
        available_documents: Collection[str] | None = None,
        backoff: "RetryPolicy | None" = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._planner = planner
        # [planner] for a single provider; [primary, fallback, ...] for a FallbackPlanner.
        self._providers = provider_chain(planner)
        # Set in mock execution mode: read_document targets must exist (see validation.py).
        self._available_documents = available_documents
        # None: retry immediately (unit tests). The API passes RetryPolicy.for_planning(settings).
        self._backoff = backoff
        self._sleep = sleeper
        self._rng = rng

    @property
    def provider_count(self) -> int:
        return len(self._providers)

    def _wait_before_retry(self, state: WorkflowState, provider_attempt: int) -> list[float]:
        """Bounded exponential backoff with jitter before attempt 2, 3, ... on the same provider.
        The first attempt on a fallback provider does not wait: it is a different service."""
        if provider_attempt == 1 or self._backoff is None:
            return list(state.get("retry_delays") or [])
        delay = self._backoff.delay_for(provider_attempt - 1, self._rng)
        logger.info("node=planner workflow_id=%s attempt=%d backoff_seconds=%.3f",
                    state["workflow_id"], provider_attempt, delay)
        self._sleep(delay)
        return [*(state.get("retry_delays") or []), delay]

    def planner_node(self, state: WorkflowState) -> dict[str, Any]:
        index = state.get("provider_index", 0)
        provider = self._providers[index]
        name, model = getattr(provider, "name", "unknown"), planner_model(provider)
        attempt = state["planning_attempts"] + 1  # total across providers
        provider_attempt = state.get("provider_attempts", 0) + 1
        retry_delays = self._wait_before_retry(state, provider_attempt)
        common = {
            "current_step": "planner",
            "planner_name": name,
            "planner_model": model,
            "planning_attempts": attempt,
            "provider_attempts": provider_attempt,
            "retry_delays": retry_delays,
        }
        log_entry = {"provider": name, "model": model, "attempt": provider_attempt}
        try:
            draft = _call_planner(provider, state["user_input"], state["max_steps"],
                                  state.get("allowed_integrations"), state.get("allowed_drive_folders"),
                                  state.get("allowed_notion_parents"), state.get("allowed_teams_channels"))
        except PlannerError as exc:
            logger.warning(
                "node=planner workflow_id=%s provider=%s attempt=%d error=%s retryable=%s provider_unavailable=%s",
                state["workflow_id"], name, provider_attempt, exc.code, exc.retryable, exc.provider_unavailable,
            )
            entry = {**log_entry, "outcome": "error", "error_code": exc.code, "http_status": exc.http_status,
                     "retryable": exc.retryable, "provider_unavailable": exc.provider_unavailable}
            self._record_attempt(state, entry)
            return {
                **common,
                "status": "planning_failed",
                "last_error_retryable": exc.retryable,
                "last_error_provider_unavailable": exc.provider_unavailable,
                "draft_plan": None,
                "provider_attempt_log": [entry],
                "errors": [{"code": exc.code, "message": f"Attempt {attempt}: {exc.message}",
                            "retryable": exc.retryable, "provider": name}],
            }
        except Exception as exc:  # unexpected bug in a planner: fail closed, no retry, no fallback
            logger.exception("node=planner workflow_id=%s unexpected error", state["workflow_id"])
            entry = {**log_entry, "outcome": "error", "error_code": "PLANNER_ERROR", "http_status": None,
                     "retryable": False, "provider_unavailable": False}
            self._record_attempt(state, entry)
            return {
                **common,
                "status": "planning_failed",
                "last_error_retryable": False,
                "last_error_provider_unavailable": False,
                "draft_plan": None,
                "provider_attempt_log": [entry],
                "errors": [{"code": "PLANNER_ERROR", "message": f"Unexpected planner error ({type(exc).__name__}).",
                            "retryable": False, "provider": name}],
            }

        logger.info("node=planner workflow_id=%s provider=%s attempt=%d steps=%d",
                    state["workflow_id"], name, provider_attempt, len(draft.steps))
        entry = {**log_entry, "outcome": "draft", "error_code": None, "http_status": None,
                 "retryable": False, "provider_unavailable": False}
        self._record_attempt(state, entry)
        return {
            **common,
            "status": "validating",
            "last_error_retryable": False,
            "last_error_provider_unavailable": False,
            "provider_attempt_log": [entry],
            # The whole draft of THIS provider replaces any earlier state: never merged.
            "draft_plan": draft.model_dump(),
            "planned_steps": [s.model_dump() for s in draft.steps],
            "plan_summary": draft.summary,
        }

    @staticmethod
    def _record_attempt(state: WorkflowState, entry: dict[str, Any]) -> None:
        """One Langfuse event per provider call: provider, model, attempt, codes. No messages."""
        get_telemetry().event("planning.provider_attempt", workflow_id=state["workflow_id"],
                              metadata=entry, level="DEFAULT" if entry["outcome"] == "draft" else "WARNING")

    def fallback_node(self, state: WorkflowState) -> dict[str, Any]:
        """Switch to the next provider after the current one exhausted its attempts on an outage."""
        failed = self._providers[state["provider_index"]]
        nxt = self._providers[state["provider_index"] + 1]
        reason = (state["errors"][-1].get("code") if state["errors"] else None) or "PLANNER_UNAVAILABLE"
        logger.warning("node=fallback workflow_id=%s from=%s to=%s reason=%s",
                       state["workflow_id"], failed.name, nxt.name, reason)
        get_telemetry().event(
            "planning.fallback", workflow_id=state["workflow_id"], level="WARNING",
            metadata={"from_provider": failed.name, "from_model": planner_model(failed),
                      "to_provider": nxt.name, "to_model": planner_model(nxt), "reason": reason,
                      "attempts_on_failed_provider": state["provider_attempts"]},
        )
        return {
            "current_step": "fallback",
            "provider_index": state["provider_index"] + 1,
            "provider_attempts": 0,
            "fallback_used": True,
            "fallback_reason": reason,
            "last_error_retryable": False,
            "last_error_provider_unavailable": False,
        }

    def validator_node(self, state: WorkflowState) -> dict[str, Any]:
        draft = DraftPlan.model_validate(state["draft_plan"])
        # Dry-run/real Drive plans: fill only unambiguous missing references (reported to the
        # reviewer); the result is still fully validated below.
        draft, adjustments = normalize_drive_references(draft, state.get("allowed_drive_folders"))
        # Notion: repair `parent_page` and drop unsupported read-back steps only when unambiguous
        # (reported to the reviewer); unknown actions/arguments are still rejected below.
        draft, notion_adjustments = normalize_notion_references(draft, state.get("allowed_notion_parents"))
        # Teams: fill a missing channel only when exactly one alias is approved (reported).
        draft, teams_adjustments = normalize_teams_references(draft, state.get("allowed_teams_channels"))
        adjustments = [*adjustments, *notion_adjustments, *teams_adjustments]
        if adjustments:
            logger.info("node=validator workflow_id=%s reference_adjustments=%d",
                        state["workflow_id"], len(adjustments))
        plan, issues = validate_draft(draft, max_steps=state["max_steps"])
        allowed = state.get("allowed_integrations")
        if plan is not None and allowed is not None:
            # Server-side integration target: a step outside it rejects the whole plan.
            issues = integration_target_issues(plan.steps, frozenset(allowed))
            if issues:
                plan = None
        folders = state.get("allowed_drive_folders")
        if plan is not None and folders is not None:
            # Server-side Drive destination allowlist: approved alias or a folder of this plan only.
            issues = drive_destination_issues(plan.steps, folders)
            if issues:
                plan = None
        if plan is not None and state.get("integration_target") == "drive_notion_teams":
            # Phase 7.2: allowed actions, source binding, Notion hierarchy, Teams last.
            issues = orchestration_issues(plan.steps)
            if issues:
                plan = None
        channels = state.get("allowed_teams_channels")
        if plan is not None and channels is not None:
            # Server-side Teams destination allowlist: approved channel aliases only.
            issues = teams_destination_issues(plan.steps, channels)
            if issues:
                plan = None
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
                "plan_adjustments": adjustments,
                "validation_issues": [i.model_dump() for i in issues],
            }
        return {
            "current_step": "validator",
            "status": "valid",
            "plan_adjustments": adjustments,
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
        update: dict[str, Any] = {
            "current_step": "error",
            "status": "failed",
            "results": {
                "execution": "not_executed",
                "message": f"Planning failed after {state['planning_attempts']} attempt(s).",
            },
        }
        if state.get("fallback_used"):
            tried = list(dict.fromkeys(e["provider"] for e in state.get("provider_attempt_log") or []))
            update["errors"] = [{
                "code": "ALL_PLANNERS_FAILED",
                "message": f"No plan was produced: every planner provider failed ({', '.join(tried)}). "
                           "Nothing was executed. Try again later.",
                "retryable": False,
            }]
        return update


def _accepts(provider: Any, name: str) -> bool:
    try:
        return name in inspect.signature(provider.plan).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


def _call_planner(provider: Any, instruction: str, max_steps: int, allowed: list[str] | None,
                  drive_folders: list[str] | None = None, notion_parents: list[str] | None = None,
                  teams_channels: list[str] | None = None):
    """Pass the integration target, approved Drive folders and approved Notion parents to planners
    that put them in their prompt (Claude, Gemini, Groq). Others (mock, project adapters) are called
    as before; the validator and the executor enforce them for every planner either way."""
    kwargs: dict[str, Any] = {}
    if allowed is not None and _accepts(provider, "allowed_integrations"):
        kwargs["allowed_integrations"] = frozenset(allowed)
    if drive_folders is not None and _accepts(provider, "drive_folders"):
        kwargs["drive_folders"] = frozenset(drive_folders)
    if notion_parents is not None and _accepts(provider, "notion_parents"):
        kwargs["notion_parents"] = frozenset(notion_parents)
    if teams_channels is not None and _accepts(provider, "teams_channels"):
        kwargs["teams_channels"] = frozenset(teams_channels)
    return provider.plan(instruction, max_steps=max_steps, **kwargs)


# ---------- conditional edge routers ----------


def route_after_planner(state: WorkflowState) -> Literal["validator", "planner", "fallback", "error"]:
    if state["status"] == "validating":
        return "validator"
    if state["last_error_retryable"] and state.get("provider_attempts", state["planning_attempts"]) \
            < state["max_planning_attempts"]:
        return "planner"  # retry the same provider
    if state.get("last_error_provider_unavailable") and \
            state.get("provider_index", 0) + 1 < state.get("provider_count", 1):
        return "fallback"  # provider still unavailable after its bounded retries
    return "error"


def route_after_validator(state: WorkflowState) -> Literal["reporter", "error"]:
    return "reporter" if state["status"] == "valid" else "error"
