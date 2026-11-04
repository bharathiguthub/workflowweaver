"""Thin service between FastAPI and the LangGraph graph."""

from collections.abc import Collection

from app.agents.graph import build_planning_graph, initial_state, recursion_limit_for
from app.agents.planners.base import Planner
from app.agents.state import WorkflowState
from app.observability import get_telemetry
from app.schemas.plan import (
    PlanningError,
    PlanResponse,
    PlanStatus,
    ValidationIssue,
    ValidationResult,
    WorkflowStep,
)

_FINAL_STATUS = {"planned": PlanStatus.PLANNED, "invalid": PlanStatus.INVALID, "failed": PlanStatus.FAILED}


class PlanningService:
    def __init__(
        self,
        planner: Planner,
        *,
        max_steps: int,
        max_planning_attempts: int,
        available_documents: Collection[str] | None = None,
    ) -> None:
        self.planner = planner
        self.max_steps = max_steps
        self.max_planning_attempts = max_planning_attempts
        self.graph = build_planning_graph(planner, available_documents=available_documents)

    def run(self, instruction: str, *, name: str | None = None) -> WorkflowState:
        state = initial_state(
            instruction,
            planner_name=self.planner.name,
            max_steps=self.max_steps,
            max_planning_attempts=self.max_planning_attempts,
            workflow_name=name,
        )
        with get_telemetry().observe(
            "planning.request",
            as_type="agent",
            workflow_id=state["workflow_id"],
            root=True,
            input={"instruction_chars": len(instruction), "has_name": name is not None},
            metadata={"planner": self.planner.name, "max_steps": self.max_steps,
                      "max_planning_attempts": self.max_planning_attempts},
        ) as observation:
            final = self.graph.invoke(
                state, config={"recursion_limit": recursion_limit_for(self.max_planning_attempts)}
            )
            observation.update(
                output={
                    "status": final["status"],
                    "planning_attempts": final["planning_attempts"],
                    "step_count": len(final["planned_steps"]) if final["status"] == "planned" else 0,
                    "error_codes": [e.get("code") for e in final["errors"]],
                    "validation_issue_codes": [i.get("code") for i in final["validation_issues"]],
                },
                level="DEFAULT" if final["status"] == "planned" else "WARNING",
            )
            return final

    def plan(self, instruction: str, *, name: str | None = None) -> PlanResponse:
        return to_response(self.run(instruction, name=name))


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
    )
