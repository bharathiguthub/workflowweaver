"""Typed Server-Sent Event contract for live execution progress (Phase 4, Stage 2).

Every SSE message is:

    id: <event_id>
    event: <event_type>
    data: <ExecutionEvent JSON>

``event_id`` is a per-workflow sequence number (1, 2, 3, ...) and is also the SSE ``id``,
so a reconnecting EventSource resumes after the last event it saw (``Last-Event-ID``).
``metadata`` contains identifiers, statuses, codes, counts and timings only: never prompts,
tool argument values, document contents, credentials or exception messages.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ExecutionEventType(str, Enum):
    WORKFLOW_STARTED = "workflow_started"  # plan re-validated and approved; steps about to run
    STEP_STARTED = "step_started"  # one tool attempt is starting (metadata.attempt)
    STEP_SUCCEEDED = "step_succeeded"
    STEP_FAILED = "step_failed"  # an attempt failed; metadata.will_retry says if it is retried
    RETRY_SCHEDULED = "retry_scheduled"  # backoff before the next attempt (phase: execution|compensation)
    COMPENSATION_STARTED = "compensation_started"  # undoing one journaled step
    COMPENSATION_COMPLETED = "compensation_completed"  # COMPENSATED | ALREADY_COMPENSATED | NOT_COMPENSATABLE
    COMPENSATION_FAILED = "compensation_failed"
    WORKFLOW_COMPLETED = "workflow_completed"  # terminal: status COMPLETED
    WORKFLOW_FAILED = "workflow_failed"  # terminal: FAILED | ROLLED_BACK | PARTIALLY_ROLLED_BACK |
    #                                      COMPENSATION_FAILED | REJECTED | ERROR


TERMINAL_EVENT_TYPES = frozenset({ExecutionEventType.WORKFLOW_COMPLETED, ExecutionEventType.WORKFLOW_FAILED})


class ExecutionEvent(BaseModel):
    event_id: int = Field(ge=1, description="Per-workflow sequence number; also the SSE id")
    workflow_id: UUID
    event_type: ExecutionEventType
    step_number: int | None = None
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.event_type in TERMINAL_EVENT_TYPES


class ExecutionStartResponse(BaseModel):
    workflow_id: UUID
    status: Literal["ACCEPTED"] = "ACCEPTED"
    mode: Literal["mock", "dry_run", "real"] = "mock"
    stream_url: str = Field(description="SSE stream for this workflow (relative URL, token included)")
    stream_token: str = Field(description="Capability token for this workflow's stream only. Mock-only access model.")
    result_url: str = Field(description="Final ExecutionResponse once the run finishes")
    access_model: Literal["mock-capability-token"] = "mock-capability-token"
