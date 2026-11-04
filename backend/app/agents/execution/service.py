"""Execution service: request idempotency + graph invocation + response mapping.

Request idempotency: ``workflow_id`` is the idempotency key.
  * Same workflow_id + same plan, already finished  -> stored response, ``replayed=True``;
    no tool is called again.
  * Same workflow_id + different plan               -> IdempotencyConflict (HTTP 409).
  * Concurrent duplicates are serialised by a per-key lock; the second caller gets the replay.
  * REJECTED outcomes are not stored, so a corrected request can be resubmitted.

The store is in-memory (per process, lost on restart). Durable persistence is required before
real mode is used beyond controlled testing (Phase 5 later stage).
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
from app.agents.execution.tools.registry import ToolRegistry
from app.observability import get_telemetry
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


class ModeMismatch(Exception):
    """approval.mode differs from the server's integration mode."""


class ExecutionService:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: RetryPolicy,
        *,
        max_steps: int,
        sleeper: Sleeper = time.sleep,
        rng: random.Random | None = None,
        document_precheck: bool = True,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.max_steps = max_steps
        self.graph = build_execution_graph(
            registry, policy, sleeper=sleeper, rng=rng, document_precheck=document_precheck
        )
        self._results: dict[str, tuple[str, ExecutionResponse]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, workflow_id: str) -> ExecutionResponse | None:
        stored = self._results.get(workflow_id)
        return stored[1] if stored else None

    def check_conflict(self, request: ExecutionRequest) -> None:
        """Raise IdempotencyConflict now if workflow_id already finished with another plan.
        (execute() re-checks under the per-key lock; this is the fast pre-check for async starts.)"""
        if request.approval.mode != self.registry.mode.value:
            raise ModeMismatch(f"This server executes in '{self.registry.mode.value}' mode; approval.mode must match.")
        stored = self._results.get(str(request.workflow_id))
        if stored is not None and stored[0] != plan_fingerprint(request):
            raise IdempotencyConflict(f"workflow_id {request.workflow_id} was already executed with a different plan.")

    def execute(self, request: ExecutionRequest, *, write_authorized: bool = False) -> ExecutionResponse:
        """``write_authorized`` must come from a server-side check (the API verifies the
        operator token); it is never read from the request body."""
        key = str(request.workflow_id)
        fingerprint = plan_fingerprint(request)
        telemetry = get_telemetry()
        with telemetry.observe(
            "execution.request",
            as_type="agent",
            workflow_id=key,
            root=True,
            input={
                "step_count": len(request.plan.steps),
                "steps": [
                    {"step_number": s.step_number, "integration": s.integration, "action": s.action,
                     "depends_on": s.depends_on}
                    for s in request.plan.steps
                ],
                "approved": request.approval.approved,
                "mode": request.approval.mode,
                "server_mode": self.registry.mode.value,
                "write_authorized": write_authorized,
            },
            metadata={"plan_fingerprint": fingerprint[:16], "max_attempts": self.policy.max_attempts},
        ) as observation:
            with self._lock_for(key):
                stored = self._results.get(key)
                if stored is not None:
                    if stored[0] != fingerprint:
                        observation.update(level="WARNING", status_message="IDEMPOTENCY_CONFLICT",
                                           output={"outcome": "IDEMPOTENCY_CONFLICT"})
                        raise IdempotencyConflict(f"workflow_id {key} was already executed with a different plan.")
                    replay = stored[1].model_copy(update={"replayed": True})
                    observation.update(output=outcome_summary(replay), metadata={"replayed": True})
                    return replay

                state = self.run(request, write_authorized=write_authorized)
                response = to_response(state)
                if response.status is not ExecutionStatus.REJECTED:
                    self._results[key] = (fingerprint, response)

            summary = outcome_summary(response)
            level = _OUTCOME_LEVEL.get(response.status, "WARNING")
            observation.update(output=summary, level=level, status_message=response.status.value,
                               metadata={"replayed": False})
            telemetry.event("execution.outcome", workflow_id=key, metadata=summary, level=level)
            return response

    def run(self, request: ExecutionRequest, *, write_authorized: bool = False) -> ExecutionState:
        state = initial_execution_state(
            workflow_id=str(request.workflow_id),
            raw_plan=request.plan.model_dump(mode="json"),
            approved=request.approval.approved,
            approved_by=request.approval.approved_by,
            max_steps=self.max_steps,
            max_attempts=self.policy.max_attempts,
            mode=self.registry.mode.value,
            requested_mode=request.approval.mode,
            write_authorized=write_authorized,
        )
        limit = execution_recursion_limit(len(request.plan.steps), self.policy.max_attempts)
        return self.graph.invoke(state, config={"recursion_limit": limit})


_OUTCOME_LEVEL = {
    ExecutionStatus.COMPLETED: "DEFAULT",
    ExecutionStatus.COMPENSATION_FAILED: "ERROR",
}


def outcome_summary(response: ExecutionResponse) -> dict:
    """Final outcome for telemetry: statuses, counts and codes only."""
    return {
        "status": response.status.value,
        "replayed": response.replayed,
        "attempts": response.attempts,
        "total_attempts": sum(response.attempts.values()),
        "retry_count": len(response.retry_delays_seconds),
        "retry_delays_seconds": response.retry_delays_seconds,
        "step_statuses": {str(r.step_number): r.status.value for r in response.step_results},
        "error_codes": [e.code for e in response.errors],
        "compensation_statuses": {str(c.step_number): c.status.value for c in response.compensation_results},
    }


def plan_fingerprint(request: ExecutionRequest) -> str:
    return hashlib.sha256(request.plan.model_dump_json().encode()).hexdigest()


def to_response(state: ExecutionState) -> ExecutionResponse:
    issues = [ValidationIssue.model_validate(i) for i in state["validation_issues"]]
    return ExecutionResponse(
        workflow_id=state["workflow_id"],
        status=ExecutionStatus(state["final_status"]),
        mode=state.get("mode", "mock"),
        validation=ValidationResult(is_valid=not issues, issues=issues),
        step_results=[StepResult.model_validate(r) for r in state["step_results"]],
        attempts=state["attempts"],
        retry_delays_seconds=state["retry_delays"],
        errors=[ExecutionError.model_validate(e) for e in state["errors"]],
        compensation_results=[CompensationResult.model_validate(c) for c in state["compensation_results"]],
        message=state["message"],
    )
