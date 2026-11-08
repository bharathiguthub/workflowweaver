"""Pydantic schemas for Phase 2 LangGraph planning.

Two layers of plan models exist on purpose:

* ``DraftPlan`` / ``DraftStep`` - what a planner (mock or Claude) proposes. Integration and
  action are free-form strings because LLM output is untrusted; the validator decides
  whether they are allowed.
* ``WorkflowPlan`` / ``WorkflowStep`` - a plan that passed validation, with typed enums.
"""

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Integration


class Action(str, Enum):
    CREATE_PAGE = "create_page"
    CREATE_TASK = "create_task"
    CREATE_DOCUMENT = "create_document"
    READ_DOCUMENT = "read_document"
    CREATE_FOLDER = "create_folder"
    LIST_FILES = "list_files"
    SEND_MESSAGE = "send_message"


class IntegrationTarget(str, Enum):
    """Which integrations a plan may use. Enforced server-side by the planning validator and
    again by the executor; prompts only help the model stay inside it."""

    GOOGLE_DRIVE = "google_drive"
    NOTION = "notion"
    TEAMS = "teams"
    MULTI = "multi"  # any supported integration (the pre-target behaviour)
    # Phase 7.2: exactly Google Drive + Notion + Teams, for the server-compiled orchestration
    # workflow (Drive source -> Notion pages -> Teams notification). Not offered to free planning.
    DRIVE_NOTION_TEAMS = "drive_notion_teams"


_TARGET_INTEGRATIONS: dict["IntegrationTarget", frozenset[str]] = {
    IntegrationTarget.DRIVE_NOTION_TEAMS: frozenset({"google_drive", "notion", "teams"}),
}


def target_integrations(target: "IntegrationTarget | str | None") -> frozenset[str] | None:
    """Integration values a target allows; None = no restriction (multi / unspecified)."""
    if target is None:
        return None
    value = IntegrationTarget(target)
    if value is IntegrationTarget.MULTI:
        return None
    return _TARGET_INTEGRATIONS.get(value, frozenset({value.value}))


class PlanStatus(str, Enum):
    PLANNED = "PLANNED"  # valid plan, NOT executed
    INVALID = "INVALID"  # planner produced a plan that failed validation
    FAILED = "FAILED"  # planner could not produce a plan (timeout, API error, bad output)


# ---------- planner output (untrusted) ----------


class StepArgument(BaseModel):
    name: str = Field(description="Argument name, e.g. 'title'")
    value: str = Field(description="Argument value as plain text")


class DraftStep(BaseModel):
    step_number: int = Field(description="1-based position of the step in the plan")
    integration: str = Field(description="One of: notion, google_drive, teams")
    action: str = Field(description="An action supported by the chosen integration")
    description: str = Field(description="One sentence describing what the step does")
    arguments: list[StepArgument] = Field(default_factory=list)
    depends_on: list[int] = Field(
        default_factory=list, description="step_numbers of earlier steps this step needs"
    )


class DraftPlan(BaseModel):
    summary: str = Field(description="One or two sentence summary of the workflow")
    steps: list[DraftStep]


# ---------- validated plan ----------


class WorkflowStep(BaseModel):
    step_number: int
    integration: Integration
    action: Action
    description: str
    arguments: dict[str, str]
    depends_on: list[int]


class WorkflowPlan(BaseModel):
    summary: str
    steps: list[WorkflowStep]


# ---------- errors ----------


class ValidationIssue(BaseModel):
    code: str = Field(examples=["UNSUPPORTED_INTEGRATION"])
    message: str
    step_number: int | None = None
    field: str | None = None


class PlanningError(BaseModel):
    code: str = Field(examples=["PLANNER_TIMEOUT"])
    message: str
    retryable: bool = False
    provider: str | None = Field(default=None, description="Planner provider that raised it (gemini, groq, ...)")


class ProviderAttempt(BaseModel):
    """One call to one planner provider. Codes and statuses only, never provider messages."""

    provider: str
    model: str | None = None
    attempt: int = Field(description="1-based attempt number on this provider")
    outcome: Literal["draft", "error"]
    error_code: str | None = None
    http_status: int | None = None
    retryable: bool = False
    provider_unavailable: bool = False


class ValidationResult(BaseModel):
    is_valid: bool
    issues: list[ValidationIssue]


# ---------- API ----------


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: str = Field(
        ...,
        min_length=10,
        max_length=5_000,
        description="Natural-language workflow instruction (synthetic data only).",
        examples=[
            "Read meeting notes, create a Notion project page, save a summary to "
            "Google Drive, and notify the team in Microsoft Teams."
        ],
    )
    name: str | None = Field(default=None, min_length=3, max_length=120)
    mode: Literal["mock", "dry_run", "real"] | None = Field(
        default=None,
        description="Integration mode the plan is meant for (default: the server's INTEGRATION_MODE). "
        "Only mock plans are checked against the synthetic document catalog. Planning never executes.",
    )
    target: IntegrationTarget | None = Field(
        default=None,
        description="Integration target: the plan may use ONLY these tools (rejected otherwise). "
        "Omitted = multi (any supported integration).",
    )


class PlanResponse(BaseModel):
    workflow_id: UUID
    status: PlanStatus
    planner: str
    summary: str | None
    planned_steps: list[WorkflowStep]
    validation: ValidationResult
    errors: list[PlanningError]
    planning_attempts: int
    retry_delays_seconds: list[float] = Field(
        default_factory=list, description="Seconds waited (backoff with jitter) before each planner retry")
    planner_model: str | None = Field(default=None, description="Model id of the provider in `planner`")
    primary_planner: str | None = Field(default=None, description="First provider tried (auto mode: PRIMARY_PLANNER)")
    fallback_used: bool = Field(default=False, description="The plan (or last error) came from the fallback provider")
    fallback_reason: str | None = Field(default=None, description="Error code that triggered the fallback")
    provider_attempts: list[ProviderAttempt] = Field(default_factory=list)
    integration_target: IntegrationTarget | None = Field(
        default=None, description="Target the plan was restricted to (None: unrestricted legacy request)")
    plan_adjustments: list[str] = Field(
        default_factory=list,
        description="Google Drive / Notion references the server repaired because only one value was possible, "
        "and unsupported Notion read-back steps it removed (shown to the reviewer; the adjusted plan was "
        "fully validated)")
    executed: bool = Field(default=False, description="Always false in Phase 2")
