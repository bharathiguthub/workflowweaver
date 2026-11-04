"""Pydantic schemas for Phase 2 LangGraph planning.

Two layers of plan models exist on purpose:

* ``DraftPlan`` / ``DraftStep`` - what a planner (mock or Claude) proposes. Integration and
  action are free-form strings because LLM output is untrusted; the validator decides
  whether they are allowed.
* ``WorkflowPlan`` / ``WorkflowStep`` - a plan that passed validation, with typed enums.
"""

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Integration


class Action(str, Enum):
    CREATE_PAGE = "create_page"
    CREATE_TASK = "create_task"
    CREATE_DOCUMENT = "create_document"
    READ_DOCUMENT = "read_document"
    SEND_MESSAGE = "send_message"


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


class PlanResponse(BaseModel):
    workflow_id: UUID
    status: PlanStatus
    planner: str
    summary: str | None
    planned_steps: list[WorkflowStep]
    validation: ValidationResult
    errors: list[PlanningError]
    planning_attempts: int
    executed: bool = Field(default=False, description="Always false in Phase 2")
