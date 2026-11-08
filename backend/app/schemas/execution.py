"""Pydantic schemas for workflow execution (mock, dry-run or real integrations).

Final workflow statuses
-----------------------
COMPLETED              every step succeeded and was verified.
FAILED                 a step failed and no side effects had been made (nothing to undo).
ROLLED_BACK            a step failed; every completed side effect was compensated.
PARTIALLY_ROLLED_BACK  a step failed; every reversible effect was compensated, but at least
                       one non-reversible effect (e.g. a Teams message) remains.
COMPENSATION_FAILED    a step failed and at least one compensation also failed, so one or
                       more effects remain that should have been undone. Needs manual review.
REJECTED               the request was refused before execution (no approval, invalid plan).
"""

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.plan import IntegrationTarget, ValidationResult


ExecutionMode = Literal["mock", "dry_run", "real"]


class ExecutionStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    PARTIALLY_ROLLED_BACK = "PARTIALLY_ROLLED_BACK"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    REJECTED = "REJECTED"
    # Phase 7.2 (Drive + Notion + Teams only): every Notion page was created and verified and is
    # kept; only the final, non-compensatable Teams notification failed (or its outcome is unknown).
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"


class StepStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # not run because an earlier step failed


class CompensationStatus(str, Enum):
    COMPENSATED = "COMPENSATED"
    ALREADY_COMPENSATED = "ALREADY_COMPENSATED"  # idempotent repeat: effect was already undone
    NOT_COMPENSATABLE = "NOT_COMPENSATABLE"  # e.g. Teams message: cannot be unsent
    FAILED = "FAILED"


# ---------- request ----------


class ExecutionApproval(BaseModel):
    """Explicit per-request approval. NOT authentication: approved_by is an unverified label."""

    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(description="Must be true to execute.")
    mode: ExecutionMode = Field(
        description="Must equal the server's INTEGRATION_MODE. Confirms which mode the approver "
        "approved; it never selects or changes the mode."
    )
    approved_by: str = Field(min_length=3, max_length=80, examples=["synthetic.reviewer"])
    integration_target: IntegrationTarget | None = Field(
        default=None,
        description="The integration target the approver reviewed. Must equal the plan's target "
        "(INTEGRATION_TARGET_MISMATCH otherwise).",
    )
    plan_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the reviewed plan (plan_digest from the planning response). Required for "
        "plans whose digest the server recorded; must equal it and the digest of the submitted plan "
        "(APPROVAL_PLAN_MISMATCH otherwise).",
    )


class ExecutionStepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_number: int = Field(ge=1, le=100)
    integration: str = Field(max_length=40, examples=["notion"])
    action: str = Field(max_length=40, examples=["create_page"])
    description: str = Field(default="", max_length=500)
    arguments: dict[str, str] = Field(default_factory=dict, max_length=10)
    depends_on: list[int] = Field(default_factory=list, max_length=50)


class ExecutionPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=1000)
    steps: list[ExecutionStepIn] = Field(max_length=50)
    integration_target: IntegrationTarget | None = Field(
        default=None, description="Target the plan was generated for (from the planning response).")


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID = Field(description="Also the request idempotency key.")
    plan: ExecutionPlanIn
    approval: ExecutionApproval


# ---------- response ----------


class ExecutionError(BaseModel):
    code: str
    message: str
    step_number: int | None = None
    attempt: int | None = None
    retryable: bool = False


class StepResult(BaseModel):
    step_number: int
    integration: str
    action: str
    status: StepStatus
    attempts: int
    resource_id: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    replayed: bool = Field(default=False, description="Tool returned a stored result for this idempotency key")
    error: ExecutionError | None = None


class CompensationResult(BaseModel):
    step_number: int
    integration: str
    action: str
    resource_id: str | None
    status: CompensationStatus
    attempts: int
    message: str


class ExecutionResponse(BaseModel):
    workflow_id: UUID
    status: ExecutionStatus
    mode: ExecutionMode = "mock"
    validation: ValidationResult
    step_results: list[StepResult]
    attempts: dict[str, int] = Field(description="Attempts per step_number")
    retry_delays_seconds: list[float]
    errors: list[ExecutionError]
    compensation_results: list[CompensationResult]
    replayed: bool = Field(default=False, description="True if this is a stored result for a duplicate request")
    message: str
