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
    mode: str  # server integration mode (IntegrationMode value); never chosen by the client
    requested_mode: str  # approval.mode from the request; must equal ``mode``
    write_authorized: bool  # server-verified operator authorization for real writes
    # Integration target: approval.integration_target, the target in the submitted plan, and the
    # one the server recorded at planning time (None = not recorded / not given).
    approved_target: str | None
    plan_target: str | None
    recorded_target: str | None
    recorded_drive_folders: list[str] | None  # approved Drive folder aliases recorded at planning time
    recorded_teams_channels: list[str] | None  # approved Teams channel aliases recorded at planning time
    recorded_plan_digest: str | None  # digest of the plan returned for review (Phase 7.2)
    recorded_input_digest: str | None  # digest of the plan's inputs (source + instructions), part of it
    approved_plan_digest: str | None  # digest the approver approved (approval.plan_digest)
    verify_resources: bool  # read created resources back (drive_notion_teams target)
    # drive_notion_teams: the final Teams notification failed after every Notion page was created
    # and verified -> keep the pages (no rollback) and finish PARTIALLY_COMPLETED.
    keep_completed: bool
    # Integrations the executor may call (None = unrestricted); set by prepare, re-checked
    # immediately before every tool call.
    allowed_integrations: list[str] | None

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
