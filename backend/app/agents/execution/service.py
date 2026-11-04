"""Execution service: request idempotency + graph invocation + response mapping.

Request idempotency: ``workflow_id`` is the idempotency key.
  * Same workflow_id + same plan, already finished  -> stored response, ``replayed=True``;
    no tool is called again.
  * Same workflow_id + different plan               -> IdempotencyConflict (HTTP 409).
  * Concurrent duplicates are serialised by a per-key lock; the second caller gets the replay.
  * REJECTED outcomes are not stored, so a corrected request can be resubmitted.

The store is in-memory (per process, lost on restart) - adequate for mock execution only.
"""

import hashlib
import random
import threading
import time

from app.agents.execution.graph import (
    build_execution_graph,
    execution_recursion_limit,
    initial_execution_state,
)
from app.agents.execution.nodes import Sleeper
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.state import ExecutionState
from app.agents.execution.tools.registry import MockToolRegistry
from app.schemas.execution import (
    CompensationResult,
    ExecutionError,
    ExecutionRequest,
    ExecutionResponse,
    ExecutionStatus,
    StepResult,
)
from app.schemas.plan import ValidationIssue, ValidationResult


class IdempotencyConflict(Exception):
    """workflow_id was already used for a different plan."""


class ExecutionService:
    def __init__(
        self,
        registry: MockToolRegistry,
        policy: RetryPolicy,
        *,
        max_steps: int,
        sleeper: Sleeper = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.max_steps = max_steps
        self.graph = build_execution_graph(registry, policy, sleeper=sleeper, rng=rng)
        self._results: dict[str, tuple[str, ExecutionResponse]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, workflow_id: str) -> ExecutionResponse | None:
        stored = self._results.get(workflow_id)
        return stored[1] if stored else None

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        key = str(request.workflow_id)
        fingerprint = plan_fingerprint(request)
        with self._lock_for(key):
            stored = self._results.get(key)
            if stored is not None:
                if stored[0] != fingerprint:
                    raise IdempotencyConflict(f"workflow_id {key} was already executed with a different plan.")
                return stored[1].model_copy(update={"replayed": True})

            state = self.run(request)
            response = to_response(state)
            if response.status is not ExecutionStatus.REJECTED:
                self._results[key] = (fingerprint, response)
            return response

    def run(self, request: ExecutionRequest) -> ExecutionState:
        state = initial_execution_state(
            workflow_id=str(request.workflow_id),
            raw_plan=request.plan.model_dump(mode="json"),
            approved=request.approval.approved,
            approved_by=request.approval.approved_by,
            max_steps=self.max_steps,
            max_attempts=self.policy.max_attempts,
        )
        limit = execution_recursion_limit(len(request.plan.steps), self.policy.max_attempts)
        return self.graph.invoke(state, config={"recursion_limit": limit})


def plan_fingerprint(request: ExecutionRequest) -> str:
    return hashlib.sha256(request.plan.model_dump_json().encode()).hexdigest()


def to_response(state: ExecutionState) -> ExecutionResponse:
    issues = [ValidationIssue.model_validate(i) for i in state["validation_issues"]]
    return ExecutionResponse(
        workflow_id=state["workflow_id"],
        status=ExecutionStatus(state["final_status"]),
        validation=ValidationResult(is_valid=not issues, issues=issues),
        step_results=[StepResult.model_validate(r) for r in state["step_results"]],
        attempts=state["attempts"],
        retry_delays_seconds=state["retry_delays"],
        errors=[ExecutionError.model_validate(e) for e in state["errors"]],
        compensation_results=[CompensationResult.model_validate(c) for c in state["compensation_results"]],
        message=state["message"],
    )
