"""Thin service between FastAPI and the LangGraph graph."""

import random
import time
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING

from app.agents.graph import build_planning_graph, initial_state, recursion_limit_for
from app.agents.planners.base import Planner, planner_model, provider_chain
from app.agents.state import WorkflowState
from app.observability import get_telemetry
from app.schemas.plan import (
    IntegrationTarget,
    PlanningError,
    PlanResponse,
    PlanStatus,
    ProviderAttempt,
    ValidationIssue,
    ValidationResult,
    WorkflowStep,
)

if TYPE_CHECKING:
    from app.agents.execution.retry import RetryPolicy

_FINAL_STATUS = {"planned": PlanStatus.PLANNED, "invalid": PlanStatus.INVALID, "failed": PlanStatus.FAILED}

DEFAULT_DOCUMENTS = object()  # sentinel: use the catalog the service was built with


class PlanningService:
    def __init__(
        self,
        planner: Planner,
        *,
        max_steps: int,
        max_planning_attempts: int,
        available_documents: Collection[str] | None = None,
        backoff: "RetryPolicy | None" = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """``backoff``: wait between retryable planner failures (None = retry immediately,
        used by unit tests). The API passes ``RetryPolicy.for_planning(settings)``."""
        self.planner = planner
        self.max_steps = max_steps
        self.max_planning_attempts = max_planning_attempts
        self.backoff = backoff
        self._graph_options = {"backoff": backoff, "sleeper": sleeper, "rng": rng}
        self.graph = build_planning_graph(planner, available_documents=available_documents, **self._graph_options)
        self._graphs: dict[frozenset[str] | None, object] = {}

    def _graph(self, available_documents):
        """The planning graph for a per-request document catalog (None: no document check)."""
        if available_documents is DEFAULT_DOCUMENTS:
            return self.graph
        key = frozenset(available_documents) if available_documents is not None else None
        if key not in self._graphs:
            self._graphs[key] = build_planning_graph(self.planner, available_documents=key, **self._graph_options)
        return self._graphs[key]

    def run(self, instruction: str, *, name: str | None = None,
            available_documents=DEFAULT_DOCUMENTS, target: str | None = None,
            drive_folders: list[str] | None = None, notion_parents: list[str] | None = None,
            teams_channels: list[str] | None = None, workflow_id: str | None = None) -> WorkflowState:
        providers = provider_chain(self.planner)
        state = initial_state(
            instruction,
            planner_name=self.planner.name,
            planner_model=planner_model(providers[0]),
            provider_count=len(providers),
            integration_target=IntegrationTarget(target).value if target is not None else None,
            drive_folders=drive_folders,
            notion_parents=notion_parents,
            teams_channels=teams_channels,
            workflow_id=workflow_id,
            max_steps=self.max_steps,
            max_planning_attempts=self.max_planning_attempts,
            workflow_name=name,
        )
        with get_telemetry().observe(
            "planning.request",
            as_type="agent",
            workflow_id=state["workflow_id"],
            root=True,
            input={"instruction_chars": len(instruction), "has_name": name is not None,
                   "integration_target": state["integration_target"],
                   "drive_destinations_restricted": drive_folders is not None,
                   "teams_destinations_restricted": teams_channels is not None},
            metadata={"planner": self.planner.name, "providers": [p.name for p in providers],
                      "models": [planner_model(p) for p in providers], "max_steps": self.max_steps,
                      "max_planning_attempts": self.max_planning_attempts},
        ) as observation:
            final = self._graph(available_documents).invoke(
                state, config={"recursion_limit": recursion_limit_for(self.max_planning_attempts, len(providers))}
            )
            observation.update(
                output={
                    "status": final["status"],
                    "planning_attempts": final["planning_attempts"],
                    "provider": final["planner_name"],
                    "model": final.get("planner_model"),
                    "fallback_used": final.get("fallback_used", False),
                    "fallback_reason": final.get("fallback_reason"),
                    # Every provider attempt with its error code / HTTP status (no messages).
                    "provider_attempts": final.get("provider_attempt_log") or [],
                    "step_count": len(final["planned_steps"]) if final["status"] == "planned" else 0,
                    "error_codes": [e.get("code") for e in final["errors"]],
                    "validation_issue_codes": [i.get("code") for i in final["validation_issues"]],
                    "plan_adjustment_count": len(final.get("plan_adjustments") or []),
                },
                level="DEFAULT" if final["status"] == "planned" else "WARNING",
            )
            return final

    def plan(self, instruction: str, *, name: str | None = None,
             available_documents=DEFAULT_DOCUMENTS, target: str | None = None,
             drive_folders: list[str] | None = None, notion_parents: list[str] | None = None,
             teams_channels: list[str] | None = None, workflow_id: str | None = None) -> PlanResponse:
        return to_response(self.run(instruction, name=name, available_documents=available_documents, target=target,
                                    drive_folders=drive_folders, notion_parents=notion_parents,
                                    teams_channels=teams_channels, workflow_id=workflow_id))


def to_response(state: WorkflowState) -> PlanResponse:
    status = _FINAL_STATUS.get(state["status"], PlanStatus.FAILED)
    issues = [ValidationIssue.model_validate(i) for i in state["validation_issues"]]
    return PlanResponse(
        workflow_id=state["workflow_id"],
        status=status,
        planner=state["planner_name"],
        summary=state["plan_summary"],
        # Only validated steps are returned; a rejected draft is described by its issues.
        planned_steps=(
            [WorkflowStep.model_validate(s) for s in state["planned_steps"]]
            if status is PlanStatus.PLANNED
            else []
        ),
        validation=ValidationResult(is_valid=status is PlanStatus.PLANNED, issues=issues),
        errors=[PlanningError.model_validate(e) for e in state["errors"]],
        planning_attempts=state["planning_attempts"],
        retry_delays_seconds=list(state.get("retry_delays") or []),
        planner_model=state.get("planner_model"),
        primary_planner=(log[0]["provider"] if (log := state.get("provider_attempt_log")) else state["planner_name"]),
        fallback_used=bool(state.get("fallback_used")),
        fallback_reason=state.get("fallback_reason"),
        provider_attempts=[ProviderAttempt.model_validate(a) for a in state.get("provider_attempt_log") or []],
        integration_target=state.get("integration_target"),
        plan_adjustments=list(state.get("plan_adjustments") or []),
    )
