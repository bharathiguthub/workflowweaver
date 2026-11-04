"""Pydantic schemas for workflow requests, responses and templates."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkflowStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Integration(str, Enum):
    NOTION = "notion"
    GOOGLE_DRIVE = "google_drive"
    TEAMS = "teams"


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=3, max_length=120, examples=["Weekly sync follow-ups"])
    template_id: str | None = Field(default=None, examples=["meeting-to-tasks"])
    input_text: str = Field(
        ...,
        min_length=10,
        max_length=20_000,
        description="Source content for the workflow (synthetic data only in Phase 1).",
        examples=["Alice to draft the Q3 roadmap by Friday. Bob to review the API spec."],
    )
    integrations: list[Integration] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("integrations")
    @classmethod
    def unique_integrations(cls, value: list[Integration]) -> list[Integration]:
        if len(set(value)) != len(value):
            raise ValueError("integrations must not contain duplicates")
        return value


class PlannedStep(BaseModel):
    order: int
    action: str
    integration: Integration | None = None


class WorkflowResponse(BaseModel):
    id: UUID
    name: str
    template_id: str | None
    status: WorkflowStatus
    steps: list[PlannedStep]
    integrations: list[Integration]
    created_at: datetime
    message: str


class WorkflowTemplate(BaseModel):
    id: str
    name: str
    description: str
    steps: list[str]
    integrations: list[Integration]
    example_instruction: str = Field(
        description="Natural-language instruction for POST /plan that yields a valid plan which "
        "executes successfully against the mock tools (synthetic data only)."
    )
