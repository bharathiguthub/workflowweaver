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
    planner_name: str  # the provider that produced the draft (or was tried last); "mock", "gemini", ...
    planner_model: str | None  # model id of that provider, if any
    # Integration target (IntegrationTarget value) and the integrations it allows; None =
    # unrestricted. Enforced by the validator node for every planner, not just the prompt.
    integration_target: str | None
    allowed_integrations: list[str] | None
    # Approved Google Drive folder aliases for dry-run/real custom plans (None = unrestricted).
    allowed_drive_folders: list[str] | None
    # Approved Notion parent page aliases (dry-run custom Notion plans); None = not known/unrestricted.
    # Used only to repair `parent_page` safely and to tell the planner which alias to use.
    allowed_notion_parents: list[str] | None
    # Approved Teams channel aliases for dry-run/real Teams Only plans (None = unrestricted/mock).
    allowed_teams_channels: list[str] | None
    # Drive/Notion references the server repaired deterministically before validation (sentences).
    plan_adjustments: list[str]

    # progress
    current_step: str  # name of the node that last updated the state
    status: GraphStatus
    planning_attempts: int
    retry_delays: list[float]  # seconds waited before each planner retry, in order
    last_error_retryable: bool
    # Provider fallback (PLANNER_MODE=auto). provider_index selects the provider in the chain;
    # provider_attempts counts attempts on THAT provider (bounded by max_planning_attempts).
    provider_index: int
    provider_count: int  # 1 = no fallback configured
    provider_attempts: int
    last_error_provider_unavailable: bool  # last failure was an outage (429/503/timeout/connection)
    fallback_used: bool
    fallback_reason: str | None  # error code that triggered the fallback, e.g. PLANNER_API_ERROR
    # One entry per provider call: {provider, model, attempt, outcome, error_code, http_status, retryable,
    # provider_unavailable}. Codes and statuses only, never messages from the provider.
    provider_attempt_log: Annotated[list[dict[str, Any]], operator.add]

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
