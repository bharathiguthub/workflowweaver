"""Typed LangGraph state shared by every node in the planning graph.

Each node receives the full state and returns a *partial* dict; LangGraph merges that
partial update into the state. Keys without a reducer are overwritten; ``errors`` uses
``operator.add`` so errors from retried planner attempts accumulate instead of replacing
each other.

Values are plain JSON-serialisable dicts/lists (not Pydantic objects) so the state can be
checkpointed or streamed (SSE) in later phases without custom serialisers.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

GraphStatus = Literal[
    "pending",  # initial
    "validating",  # planner produced a draft, validator next
    "planning_failed",  # planner attempt failed (may be retried)
    "valid",  # validator accepted the draft
    "invalid",  # validator rejected the draft
    "planned",  # reporter finished: final success state
    "failed",  # error node finished: final failure state
]


class WorkflowState(TypedDict):
    # identity / input
    workflow_id: str
    workflow_name: str | None
    user_input: str
    planner_name: str

    # progress
    current_step: str  # name of the node that last updated the state
    status: GraphStatus
    planning_attempts: int
    last_error_retryable: bool

    # plan
    draft_plan: dict[str, Any] | None  # raw planner output (untrusted)
    planned_steps: list[dict[str, Any]]  # draft steps, replaced by validated steps
    plan_summary: str | None

    # outcome
    validation_issues: list[dict[str, Any]]
    errors: Annotated[list[dict[str, Any]], operator.add]
    results: dict[str, Any]

    # limits / metadata
    max_steps: int
    max_planning_attempts: int
    metadata: dict[str, Any]
