"""Helpers for Phase 3 execution tests. All data is synthetic; all tools are in-memory mocks."""

import random
from uuid import uuid4

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools import FailureInjector, Fault, MockToolRegistry
from app.schemas.execution import ExecutionRequest

APPROVAL = {"approved": True, "mode": "mock", "approved_by": "synthetic.reviewer"}

# 1 read (read-only) -> 2 Notion page (reversible) -> 3 Drive doc (reversible) -> 4 Teams (irreversible)
FOUR_STEPS = [
    {"step_number": 1, "integration": "google_drive", "action": "read_document",
     "arguments": {"document_name": "Synthetic standup notes"}},
    {"step_number": 2, "integration": "notion", "action": "create_page",
     "arguments": {"title": "Sprint 42 kickoff", "content": "Synthetic notes"}, "depends_on": [1]},
    {"step_number": 3, "integration": "google_drive", "action": "create_document",
     "arguments": {"title": "Sprint 42 summary", "content": "Synthetic summary"}, "depends_on": [1]},
    {"step_number": 4, "integration": "teams", "action": "send_message",
     "arguments": {"message": "Synthetic: plan is ready", "channel": "synthetic-team"}, "depends_on": [2, 3]},
]


def make_request(steps=None, *, approved=True, workflow_id=None, summary="Synthetic plan") -> ExecutionRequest:
    return ExecutionRequest.model_validate(
        {
            "workflow_id": str(workflow_id or uuid4()),
            "plan": {"summary": summary, "steps": FOUR_STEPS if steps is None else steps},
            "approval": {**APPROVAL, "approved": approved},
        }
    )


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_service(
    execute_faults: dict[int, list[Fault]] | None = None,
    compensation_faults: dict[int, list[Fault]] | None = None,
    *,
    max_attempts: int = 3,
    jitter: bool = False,
) -> tuple[ExecutionService, RecordingSleeper]:
    faults = FailureInjector(execute_faults or {}, compensation_faults or {})
    sleeper = RecordingSleeper()
    service = ExecutionService(
        MockToolRegistry(faults=faults),
        RetryPolicy(max_attempts=max_attempts, base_delay_seconds=0.2, max_delay_seconds=2.0, jitter=jitter),
        max_steps=10,
        sleeper=sleeper,
        rng=random.Random(7),
    )
    return service, sleeper


def tool_calls(service: ExecutionService, operation: str = "execute") -> list[int]:
    return [step for op, _, step in service.registry.store.calls if op == operation]
