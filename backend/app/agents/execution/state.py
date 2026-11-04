"""Typed LangGraph state for the Phase 3 execution graph.

As in the planning graph, nodes return partial updates. List fields that accumulate over
the run (results, journal, errors, delays, compensations) use ``operator.add``; everything
else is overwritten. Values are plain JSON-serialisable dicts/lists.

The ``journal`` is the Saga log: one entry per step that produced a side effect, in
execution order. Compensation walks it in reverse.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

ExecutionGraphStatus = Literal[
    "pending",  # initial
    "rejected",  # approval missing or plan invalid; nothing executed
    "running",  # executing steps
    "retrying",  # last attempt failed transiently; backoff then retry
    "step_failed",  # a step failed for good; compensation (if any) next
    "compensating",  # compensation finished
    "finished",  # finalize node ran; see final_status
]

StepOutcome = Literal["none", "succeeded", "retry", "failed"]


class ExecutionState(TypedDict):
    # identity / input
    workflow_id: str
    approved: bool
    approved_by: str
    raw_plan: dict[str, Any]  # ExecutionPlanIn.model_dump()

    # validated plan
    steps: list[dict[str, Any]]  # WorkflowStep.model_dump(mode="json")
    validation_issues: list[dict[str, Any]]

    # progress
    current_step: str  # name of the node that last updated the state
    status: ExecutionGraphStatus
    current_index: int  # index into steps of the next step to run
    last_outcome: StepOutcome
    attempts: dict[str, int]  # step_number (as str) -> attempts made
    failed_step: int | None

    # accumulated outcome
    step_results: Annotated[list[dict[str, Any]], operator.add]
    journal: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
    retry_delays: Annotated[list[float], operator.add]
    compensation_results: Annotated[list[dict[str, Any]], operator.add]

    # final
    final_status: str | None  # ExecutionStatus value, set by finalize
    message: str

    # limits
    max_steps: int
    max_attempts: int
