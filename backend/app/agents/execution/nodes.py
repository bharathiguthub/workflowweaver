"""LangGraph nodes and routing functions for the execution graph.

Every node does at most ONE tool attempt (or one compensation pass), so retries are
visible as graph transitions: execute_step -> retry_wait -> execute_step.

Logs record IDs, step numbers, attempt counts and error codes only - never argument values.
"""

import logging
import random
from collections.abc import Callable
from typing import Any

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.state import ExecutionState
from app.agents.execution.tools.base import (
    CompensationCall,
    Effect,
    ErrorKind,
    ToolCall,
    ToolError,
    ToolResult,
)
from app.agents.execution.tools.registry import ToolRegistry
from app.agents.validation import (
    document_availability_issues,
    drive_destination_issues,
    integration_target_issues,
    orchestration_issues,
    plan_digest,
    teams_destination_issues,
    validate_draft,
)
from app.core.config import IntegrationMode
from app.observability import get_telemetry
from app.schemas.plan import IntegrationTarget, target_integrations
from app.schemas.events import ExecutionEventType as Ev
from app.streaming.emitter import emit
from app.schemas.execution import (
    CompensationResult,
    CompensationStatus,
    ExecutionError,
    ExecutionStatus,
    StepResult,
    StepStatus,
)
from app.schemas.plan import Action, DraftPlan, DraftStep, StepArgument, ValidationIssue
from app.schemas.workflow import Integration

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], None]


def _error(exc: ToolError, step_number: int | None, attempt: int | None) -> dict[str, Any]:
    return ExecutionError(
        code=exc.code, message=exc.message, step_number=step_number, attempt=attempt, retryable=exc.retryable
    ).model_dump(mode="json")


class ExecutionNodes:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: RetryPolicy,
        sleeper: Sleeper,
        rng: random.Random | None = None,
        document_precheck: bool = True,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._sleep = sleeper
        self._rng = rng
        self._document_precheck = document_precheck

    # ---------- prepare ----------

    def prepare_node(self, state: ExecutionState) -> dict[str, Any]:
        """Re-validation + approval + authorization gate. Execution never trusts a
        client-supplied plan.

        Order (all pure, no network): plan validation, executor support, document precheck
        (mock only), approved destinations (dry-run/real) -> INVALID_PLAN; then approval ->
        APPROVAL_REQUIRED; then the requested mode must equal the server mode ->
        MODE_MISMATCH; then, in real mode, plans with side effects need the server-wide
        switch (REAL_WRITES_DISABLED) and a server-verified operator authorization
        (REAL_WRITE_AUTHORIZATION_REQUIRED). Validation runs first so a rejected response
        always reports issues."""
        update: dict[str, Any] = {"current_step": "prepare"}
        raw = state["raw_plan"]
        draft = DraftPlan(
            summary=raw.get("summary", ""),
            steps=[
                DraftStep(
                    step_number=s["step_number"],
                    integration=s["integration"],
                    action=s["action"],
                    description=s.get("description", ""),
                    arguments=[StepArgument(name=k, value=v) for k, v in s.get("arguments", {}).items()],
                    depends_on=s.get("depends_on", []),
                )
                for s in raw.get("steps", [])
            ],
        )
        plan, issues = validate_draft(draft, max_steps=state["max_steps"])
        if plan is not None:
            supported = self._registry.supported_actions()
            issues = [
                ValidationIssue(
                    code="UNSUPPORTED_BY_EXECUTOR",
                    message=f"No {self._registry.mode.value} adapter for {s.integration.value}.{s.action.value}.",
                    step_number=s.step_number,
                )
                for s in plan.steps
                if (s.integration, s.action) not in supported
            ]
            catalog = self._registry.document_catalog()
            if self._document_precheck and catalog is not None:
                # Reject before any side effect if a read targets a missing document.
                issues += document_availability_issues(plan, catalog)
            supported_steps = [s for s in plan.steps if (s.integration, s.action) in supported]
            issues += self._registry.preflight_issues(supported_steps)
        if plan is None or issues:
            logger.info("node=prepare workflow_id=%s rejected=invalid_plan issues=%d", state["workflow_id"], len(issues))
            return {
                **update,
                "status": "rejected",
                "validation_issues": [i.model_dump(mode="json") for i in issues],
                "errors": [
                    ExecutionError(code="INVALID_PLAN", message="Plan failed validation; nothing was executed.").model_dump()
                ],
            }
        target_problem = _target_problem(state)
        if target_problem is not None:
            code, message = target_problem
            logger.info("node=prepare workflow_id=%s rejected=%s", state["workflow_id"], code)
            return {**update, "status": "rejected", "errors": [ExecutionError(code=code, message=message).model_dump()]}
        effective_target = state.get("recorded_target") or state.get("plan_target") or state.get("approved_target")
        allowed = target_integrations(effective_target)
        if allowed is not None:
            # Re-validate the tool allowlist for the approved target right before execution.
            target_issues = integration_target_issues(plan.steps, allowed)
            if target_issues:
                logger.info("node=prepare workflow_id=%s rejected=integration_not_allowed", state["workflow_id"])
                return {
                    **update,
                    "status": "rejected",
                    "validation_issues": [i.model_dump(mode="json") for i in target_issues],
                    "errors": [ExecutionError(code="INVALID_PLAN",
                                              message="Plan uses tools outside its integration target; "
                                                      "nothing was executed.").model_dump()],
                }
            update["allowed_integrations"] = sorted(allowed)
        folders = state.get("recorded_drive_folders")
        if folders is not None:
            # The Drive destinations approved at planning time are re-checked here, independent
            # of the adapter's own sandbox configuration.
            destination_issues = drive_destination_issues(plan.steps, folders)
            if destination_issues:
                logger.info("node=prepare workflow_id=%s rejected=drive_destination", state["workflow_id"])
                return {
                    **update,
                    "status": "rejected",
                    "validation_issues": [i.model_dump(mode="json") for i in destination_issues],
                    "errors": [ExecutionError(code="INVALID_PLAN",
                                              message="Plan writes outside its approved Google Drive destinations; "
                                                      "nothing was executed.").model_dump()],
                }
        orchestration = effective_target == IntegrationTarget.DRIVE_NOTION_TEAMS.value
        if orchestration:
            # Drive + Notion + Teams: allowed actions, source binding, Notion hierarchy and
            # Teams-last ordering are re-checked on the submitted plan, never trusted.
            order_issues = orchestration_issues(plan.steps)
            if order_issues:
                logger.info("node=prepare workflow_id=%s rejected=orchestration_rules", state["workflow_id"])
                return {
                    **update,
                    "status": "rejected",
                    "validation_issues": [i.model_dump(mode="json") for i in order_issues],
                    "errors": [ExecutionError(code="INVALID_PLAN",
                                              message="Plan breaks the Drive + Notion + Teams workflow rules; "
                                                      "nothing was executed.").model_dump()],
                }
        digest_problem = _digest_problem(state, plan.steps, effective_target, required=orchestration)
        if digest_problem is not None:
            logger.info("node=prepare workflow_id=%s rejected=approval_plan_mismatch", state["workflow_id"])
            return {**update, "status": "rejected",
                    "errors": [ExecutionError(code="APPROVAL_PLAN_MISMATCH", message=digest_problem).model_dump()]}
        update["verify_resources"] = orchestration
        channels = state.get("recorded_teams_channels")
        if channels is not None:
            # The Teams channels approved at planning time are re-checked here, independent of
            # the adapter's own sandbox configuration.
            channel_issues = teams_destination_issues(plan.steps, channels)
            if channel_issues:
                logger.info("node=prepare workflow_id=%s rejected=teams_destination", state["workflow_id"])
                return {
                    **update,
                    "status": "rejected",
                    "validation_issues": [i.model_dump(mode="json") for i in channel_issues],
                    "errors": [ExecutionError(code="INVALID_PLAN",
                                              message="Plan posts outside its approved Teams channels; "
                                                      "nothing was executed.").model_dump()],
                }
        if not state["approved"]:
            logger.info("node=prepare workflow_id=%s rejected=approval_required", state["workflow_id"])
            return {
                **update,
                "status": "rejected",
                "errors": [
                    ExecutionError(code="APPROVAL_REQUIRED", message="approval.approved must be true.").model_dump()
                ],
            }
        rejection = self._authorization_problem(state, plan.steps)
        if rejection is not None:
            code, message = rejection
            logger.info("node=prepare workflow_id=%s rejected=%s", state["workflow_id"], code)
            return {**update, "status": "rejected", "errors": [ExecutionError(code=code, message=message).model_dump()]}
        steps = [s.model_dump(mode="json") for s in plan.steps]
        if state.get("approved_plan_digest"):
            # The approval is bound to exactly this plan (checked above); say so on the stream.
            emit(Ev.APPROVAL_VERIFIED, plan_digest=state["approved_plan_digest"][:16], mode=self._registry.mode.value,
                 write_count=sum(1 for s in plan.steps
                                 if self._registry.effect_of(s.integration, s.action) is not Effect.READ_ONLY))
        emit(
            Ev.WORKFLOW_STARTED,
            mode=self._registry.mode.value,
            step_count=len(steps),
            max_attempts=self._policy.max_attempts,
            steps=[
                {"step_number": s["step_number"], "integration": s["integration"], "action": s["action"],
                 "depends_on": s["depends_on"]}
                for s in steps
            ],
        )
        return {**update, "status": "running", "steps": steps}

    def _authorization_problem(self, state: ExecutionState, plan_steps) -> tuple[str, str] | None:
        mode = self._registry.mode
        if state["requested_mode"] != mode.value:
            return "MODE_MISMATCH", (f"approval.mode '{state['requested_mode']}' is not allowed; this executor runs "
                                     f"'{mode.value}' mode and a client can never escalate the server's mode.")
        if mode is not IntegrationMode.REAL:
            return None
        writes = [s for s in plan_steps if self._registry.effect_of(s.integration, s.action) is not Effect.READ_ONLY]
        if not writes:
            return None
        if not self._registry.real_writes_enabled:
            return "REAL_WRITES_DISABLED", "Real writes are disabled on this server (REAL_WRITES_ENABLED=false)."
        if not state["write_authorized"]:
            return (
                "REAL_WRITE_AUTHORIZATION_REQUIRED",
                "Real writes need a valid X-Integration-Authorization header.",
            )
        return None

    # ---------- execute ----------

    def execute_step_node(self, state: ExecutionState) -> dict[str, Any]:
        index = state["current_index"]
        step = state["steps"][index]
        n = step["step_number"]
        attempt = state["attempts"].get(str(n), 0) + 1
        attempts = {**state["attempts"], str(n): attempt}
        integration, action = Integration(step["integration"]), Action(step["action"])
        call = ToolCall(
            workflow_id=state["workflow_id"],
            step_number=n,
            integration=integration,
            action=action,
            arguments=dict(step["arguments"]),
            attempt=attempt,
            write_authorized=state["write_authorized"],
            upstream=_upstream(state, step),
            verify_after=bool(state.get("verify_resources")),
        )
        effect = self._registry.effect_of(integration, action)
        emit(Ev.STEP_STARTED, step_number=n, attempt=attempt, integration=integration.value, action=action.value)

        allowed = state.get("allowed_integrations")
        with get_telemetry().observe(
            f"tool.{integration.value}.{action.value}",
            as_type="tool",
            workflow_id=call.workflow_id,
            step_number=n,
            # Argument NAMES only; values may contain user content.
            input={"argument_names": sorted(call.arguments)},
            metadata={"attempt": attempt, "effect": effect.value, "mode": self._registry.mode.value},
        ) as observation:
            if allowed is not None and integration.value not in allowed:
                # Defence in depth: never call a tool outside the approved integration target.
                result, error = None, ToolError(ErrorKind.PERMISSION, "INTEGRATION_NOT_ALLOWED",
                                                f"{integration.value} is outside the approved integration target.")
            else:
                result, error = self._call_tool(call, effect)
            if error is None:
                observation.update(output={"resource_id": result.resource_id, "replayed": result.replayed})
            else:
                observation.update(
                    output={"error_code": error.code, "error_kind": error.kind.value, "retryable": error.retryable},
                    level="WARNING" if error.retryable else "ERROR",
                    status_message=error.code,
                )

        if error is None:
            logger.info(
                "node=execute_step workflow_id=%s step=%d attempt=%d ok replayed=%s",
                state["workflow_id"], n, attempt, result.replayed,
            )
            # integration/action/effect are enum values (safe); the UI shows e.g. "Teams message accepted".
            verification = result.output.get("verification")
            if verification in ("verified", "unavailable", "not_performed"):
                emit(Ev.RESOURCE_VERIFIED, step_number=n, integration=integration.value, action=action.value,
                     resource_id=result.resource_id, status=verification)
            emit(Ev.STEP_SUCCEEDED, step_number=n, attempt=attempt, resource_id=result.resource_id,
                 replayed=result.replayed, integration=integration.value, action=action.value, effect=effect.value,
                 **({"verification": verification} if isinstance(verification, str) else {}))
            return {
                "current_step": "execute_step",
                "status": "running",
                "last_outcome": "succeeded",
                "attempts": attempts,
                "current_index": index + 1,
                "step_results": [self._step_result(step, StepStatus.SUCCEEDED, attempt, result=result)],
                "journal": [] if effect is Effect.READ_ONLY else [_journal_entry(step, result, effect)],
            }

        logger.warning(
            "node=execute_step workflow_id=%s step=%d attempt=%d error=%s retryable=%s",
            state["workflow_id"], n, attempt, error.code, error.retryable,
        )
        errors = [_error(error, n, attempt)]
        will_retry = self._policy.should_retry(error, attempt)
        emit(Ev.STEP_FAILED, step_number=n, attempt=attempt, error_code=error.code, error_kind=error.kind.value,
             retryable=error.retryable, will_retry=will_retry,
             retries_exhausted=error.retryable and not will_retry,
             integration=integration.value, action=action.value, effect=effect.value)
        if will_retry:
            return {
                "current_step": "execute_step",
                "status": "retrying",
                "last_outcome": "retry",
                "attempts": attempts,
                "errors": errors,
            }

        if error.retryable:
            errors.append(
                ExecutionError(
                    code="RETRIES_EXHAUSTED",
                    message=f"Step {n} failed after {attempt} attempt(s).",
                    step_number=n,
                    attempt=attempt,
                ).model_dump(mode="json")
            )
        failed = self._step_result(step, StepStatus.FAILED, attempt, error=errors[0])
        skipped = [self._step_result(s, StepStatus.SKIPPED, 0) for s in state["steps"][index + 1 :]]
        if state.get("verify_resources") and integration is Integration.TEAMS and not skipped:
            # Drive + Notion + Teams: the notification is the last step and runs only after every
            # Notion page was created AND read back. A failed (or ambiguous) notification does not
            # undo that verified work: the pages are kept and the run is PARTIALLY_COMPLETED.
            logger.warning("node=execute_step workflow_id=%s step=%d notification failed; keeping verified pages",
                           state["workflow_id"], n)
            return {
                "current_step": "execute_step",
                "status": "step_failed",
                "last_outcome": "failed",
                "attempts": attempts,
                "failed_step": n,
                "errors": errors,
                "step_results": [failed],
                "keep_completed": True,
            }
        return {
            "current_step": "execute_step",
            "status": "step_failed",
            "last_outcome": "failed",
            "attempts": attempts,
            "failed_step": n,
            "errors": errors,
            "step_results": [failed, *skipped],
            "journal": self._reconcile(call, effect, step),
        }

    def _call_tool(self, call: ToolCall, effect: Effect) -> tuple[ToolResult | None, ToolError | None]:
        try:
            result = self._registry.execute(call)
            if effect is not Effect.READ_ONLY and not result.resource_id:
                raise ToolError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Tool returned no resource id.")
        except ToolError as exc:
            return None, exc
        except Exception:  # an adapter bug must not crash the graph
            logger.exception(
                "node=execute_step workflow_id=%s step=%d adapter crashed", call.workflow_id, call.step_number
            )
            return None, ToolError(ErrorKind.PERMANENT, "TOOL_CRASHED", "Unexpected adapter error.")
        return result, None

    def _reconcile(self, call: ToolCall, effect: Effect, step: dict[str, Any]) -> list[dict[str, Any]]:
        """After a failure, check whether the effect happened anyway (e.g. timeout after the
        write, or a malformed response). If so, journal it so it is compensated too."""
        if effect is Effect.READ_ONLY:
            return []
        found = self._registry.find_result(call)
        if found is None or not found.resource_id:
            return []
        logger.warning(
            "node=execute_step workflow_id=%s step=%d reconciled orphan effect", call.workflow_id, call.step_number
        )
        get_telemetry().event(
            "execution.effect_reconciled",
            workflow_id=call.workflow_id,
            step_number=call.step_number,
            metadata={"resource_id": found.resource_id, "effect": effect.value},
            level="WARNING",
        )
        return [_journal_entry(step, found, effect, reconciled=True)]

    @staticmethod
    def _step_result(
        step: dict[str, Any],
        status: StepStatus,
        attempts: int,
        *,
        result: ToolResult | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return StepResult(
            step_number=step["step_number"],
            integration=step["integration"],
            action=step["action"],
            status=status,
            attempts=attempts,
            resource_id=result.resource_id if result else None,
            output=result.output if result else {},
            replayed=result.replayed if result else False,
            error=error,
        ).model_dump(mode="json")

    # ---------- retry ----------

    def retry_wait_node(self, state: ExecutionState) -> dict[str, Any]:
        n = state["steps"][state["current_index"]]["step_number"]
        failed_attempt = state["attempts"][str(n)]
        delay = self._policy.delay_for(failed_attempt, self._rng)
        last_error = next((e for e in reversed(state["errors"]) if e.get("step_number") == n), {})
        get_telemetry().event(
            "execution.retry_scheduled",
            workflow_id=state["workflow_id"],
            step_number=n,
            metadata={
                "failed_attempt": failed_attempt,
                "next_attempt": failed_attempt + 1,
                "max_attempts": self._policy.max_attempts,
                "delay_seconds": delay,
                "error_code": last_error.get("code"),
            },
            level="WARNING",
        )
        emit(Ev.RETRY_SCHEDULED, step_number=n, phase="execution", failed_attempt=failed_attempt,
             next_attempt=failed_attempt + 1, max_attempts=self._policy.max_attempts, delay_seconds=delay,
             error_code=last_error.get("code"))
        self._sleep(delay)
        return {"current_step": "retry_wait", "retry_delays": [delay]}

    # ---------- compensate (Saga) ----------

    def compensate_node(self, state: ExecutionState) -> dict[str, Any]:
        """Undo completed side effects in reverse order. Every entry is attempted even if an
        earlier compensation fails, so as much as possible is rolled back."""
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        delays: list[float] = []
        for entry in reversed(state["journal"]):
            emit(Ev.COMPENSATION_STARTED, step_number=entry["step_number"], integration=entry["integration"],
                 action=entry["action"], effect=entry["effect"], resource_id=entry["resource_id"])
            result, entry_errors, entry_delays = self._compensate_one(state["workflow_id"], entry)
            if result.status is CompensationStatus.FAILED:
                emit(Ev.COMPENSATION_FAILED, step_number=result.step_number, status=result.status.value,
                     attempts=result.attempts, error_codes=[e["code"] for e in entry_errors])
            else:
                emit(Ev.COMPENSATION_COMPLETED, step_number=result.step_number, status=result.status.value,
                     attempts=result.attempts)
            results.append(result.model_dump(mode="json"))
            errors.extend(entry_errors)
            delays.extend(entry_delays)
        return {
            "current_step": "compensate",
            "status": "compensating",
            "compensation_results": results,
            "errors": errors,
            "retry_delays": delays,
        }

    def _compensate_one(self, workflow_id: str, entry: dict[str, Any]):
        base = {
            "step_number": entry["step_number"],
            "integration": entry["integration"],
            "action": entry["action"],
            "resource_id": entry["resource_id"],
        }
        if entry["effect"] != Effect.REVERSIBLE.value:
            return (
                CompensationResult(
                    **base,
                    status=CompensationStatus.NOT_COMPENSATABLE,
                    attempts=0,
                    message="Effect cannot be undone; it remains in the target system.",
                ),
                [],
                [],
            )

        errors: list[dict[str, Any]] = []
        delays: list[float] = []
        attempt = 0
        while True:
            attempt += 1
            call = CompensationCall(
                workflow_id=workflow_id,
                step_number=entry["step_number"],
                integration=Integration(entry["integration"]),
                action=Action(entry["action"]),
                resource_id=entry["resource_id"],
                attempt=attempt,
            )
            with get_telemetry().observe(
                f"compensation.{entry['integration']}.{entry['action']}",
                as_type="tool",
                workflow_id=workflow_id,
                step_number=entry["step_number"],
                input={"resource_id": entry["resource_id"]},
                metadata={"attempt": attempt, "reconciled": entry.get("reconciled", False),
                          "mode": self._registry.mode.value},
            ) as observation:
                undone, error = self._call_compensation(call, workflow_id)
                if error is None:
                    observation.update(output={"undone": undone})
                else:
                    observation.update(
                        output={"error_code": error.code, "retryable": error.retryable},
                        level="WARNING" if self._policy.should_retry(error, attempt) else "ERROR",
                        status_message=error.code,
                    )
            if error is None:
                status = CompensationStatus.COMPENSATED if undone else CompensationStatus.ALREADY_COMPENSATED
                logger.info(
                    "node=compensate workflow_id=%s step=%d attempt=%d status=%s",
                    workflow_id, entry["step_number"], attempt, status.value,
                )
                message = "Effect undone." if undone else "Effect was already undone."
                return CompensationResult(**base, status=status, attempts=attempt, message=message), errors, delays

            logger.warning(
                "node=compensate workflow_id=%s step=%d attempt=%d error=%s",
                workflow_id, entry["step_number"], attempt, error.code,
            )
            errors.append(_error(error, entry["step_number"], attempt) | {"code": f"COMPENSATION_{error.code}"})
            if not self._policy.should_retry(error, attempt):
                return (
                    CompensationResult(
                        **base,
                        status=CompensationStatus.FAILED,
                        attempts=attempt,
                        message=f"Compensation failed ({error.code}); manual cleanup required.",
                    ),
                    errors,
                    delays,
                )
            delay = self._policy.delay_for(attempt, self._rng)
            get_telemetry().event(
                "execution.compensation_retry_scheduled",
                workflow_id=workflow_id,
                step_number=entry["step_number"],
                metadata={"failed_attempt": attempt, "delay_seconds": delay, "error_code": error.code},
                level="WARNING",
            )
            emit(Ev.RETRY_SCHEDULED, step_number=entry["step_number"], phase="compensation",
                 failed_attempt=attempt, next_attempt=attempt + 1, max_attempts=self._policy.max_attempts,
                 delay_seconds=delay, error_code=error.code)
            self._sleep(delay)
            delays.append(delay)

    def _call_compensation(self, call: CompensationCall, workflow_id: str) -> tuple[bool, ToolError | None]:
        try:
            return self._registry.compensate(call), None
        except ToolError as exc:
            return False, exc
        except Exception:
            logger.exception("node=compensate workflow_id=%s step=%d crashed", workflow_id, call.step_number)
            return False, ToolError(ErrorKind.PERMANENT, "COMPENSATION_CRASHED", "Unexpected adapter error.")

    # ---------- finalize ----------

    def finalize_node(self, state: ExecutionState) -> dict[str, Any]:
        status = final_status(state)
        logger.info("node=finalize workflow_id=%s status=%s", state["workflow_id"], status.value)
        emit(
            Ev.WORKFLOW_COMPLETED if status is ExecutionStatus.COMPLETED else Ev.WORKFLOW_FAILED,
            status=status.value,
            replayed=False,
            failed_step=state["failed_step"],
            attempts=state["attempts"],
            step_statuses={str(r["step_number"]): r["status"] for r in state["step_results"]},
            compensation_statuses={str(c["step_number"]): c["status"] for c in state["compensation_results"]},
            error_codes=[e.get("code") for e in state["errors"]],
            validation_issue_codes=[i.get("code") for i in state["validation_issues"]],
        )
        message = _MESSAGES[status]
        if status is ExecutionStatus.COMPLETED:
            message = _COMPLETED_MESSAGES.get(state["mode"], message)
        return {"current_step": "finalize", "status": "finished", "final_status": status.value, "message": message}


def _journal_entry(step: dict[str, Any], result: ToolResult, effect: Effect, *, reconciled: bool = False) -> dict[str, Any]:
    return {
        "step_number": step["step_number"],
        "integration": step["integration"],
        "action": step["action"],
        "resource_id": result.resource_id,
        "effect": effect.value,
        "reconciled": reconciled,
    }


def final_status(state: ExecutionState) -> ExecutionStatus:
    if state["status"] == "rejected":
        return ExecutionStatus.REJECTED
    if state["failed_step"] is None:
        return ExecutionStatus.COMPLETED
    if state.get("keep_completed"):
        return ExecutionStatus.PARTIALLY_COMPLETED
    if not state["journal"]:
        return ExecutionStatus.FAILED
    statuses = {c["status"] for c in state["compensation_results"]}
    if CompensationStatus.FAILED.value in statuses:
        return ExecutionStatus.COMPENSATION_FAILED
    if CompensationStatus.NOT_COMPENSATABLE.value in statuses:
        return ExecutionStatus.PARTIALLY_ROLLED_BACK
    return ExecutionStatus.ROLLED_BACK


_MESSAGES = {
    ExecutionStatus.COMPLETED: "All steps completed (mock execution; no real services were called).",
    ExecutionStatus.FAILED: "A step failed before any side effect was made; nothing to roll back.",
    ExecutionStatus.ROLLED_BACK: "A step failed; all completed side effects were compensated.",
    ExecutionStatus.PARTIALLY_ROLLED_BACK: (
        "A step failed; reversible effects were compensated but non-reversible effects remain."
    ),
    ExecutionStatus.COMPENSATION_FAILED: (
        "A step failed and at least one compensation also failed; manual review required."
    ),
    ExecutionStatus.REJECTED: "Request rejected before execution; nothing was executed.",
    ExecutionStatus.PARTIALLY_COMPLETED: (
        "Every Notion page was created and verified and is kept; the final Microsoft Teams notification failed or its "
        "outcome is unknown (see errors). Teams messages cannot be unsent, so nothing was rolled back; check the channel "
        "before notifying again."
    ),
}

_COMPLETED_MESSAGES = {
    IntegrationMode.MOCK.value: _MESSAGES[ExecutionStatus.COMPLETED],
    IntegrationMode.DRY_RUN.value: "All steps validated (dry run; no request was sent to any service).",
    IntegrationMode.REAL.value: "All steps completed against the configured services.",
}


# ---------- routing (pure; never modifies state) ----------


def route_after_prepare(state: ExecutionState) -> str:
    return "execute_step" if state["status"] == "running" else "finalize"


def route_after_execute(state: ExecutionState) -> str:
    outcome = state["last_outcome"]
    if outcome == "retry":
        return "retry_wait"
    if outcome == "failed":
        if state.get("keep_completed"):
            return "finalize"  # verified work is kept (drive_notion_teams notification failure)
        return "compensate" if state["journal"] else "finalize"
    return "execute_step" if state["current_index"] < len(state["steps"]) else "finalize"


def _upstream(state: ExecutionState, step: dict[str, Any]) -> dict[int, str]:
    """Resource ids created by this run's SUCCEEDED dependency steps (server state only)."""
    deps = set(step.get("depends_on") or [])
    out: dict[int, str] = {}
    for r in state["step_results"]:
        status = r["status"].value if hasattr(r["status"], "value") else r["status"]
        if r["step_number"] in deps and status == StepStatus.SUCCEEDED.value and r.get("resource_id"):
            out[r["step_number"]] = r["resource_id"]
    return out


# ---------- approval binding (Phase 7.2) ----------


def _digest_problem(state: ExecutionState, steps, target: str | None, *, required: bool) -> str | None:
    """The approval must be bound to the exact reviewed plan: the digest recorded when the plan was
    produced, the digest in the approval and the digest of the submitted plan must all agree.
    ``required``: plans of this target cannot run without a recorded, matching digest."""
    recorded, approved = state.get("recorded_plan_digest"), state.get("approved_plan_digest")
    # The recorded input digest (source + instructions) is part of the digest that was approved.
    computed = plan_digest(steps, target, state.get("recorded_input_digest"))
    if recorded is None:
        if required:
            return ("This workflow can only run a plan produced by the server in this session (no recorded plan "
                    "digest); generate the plan again and approve it.")
        if approved is not None and approved != computed:
            return "The submitted plan differs from the plan that was approved."
        return None
    if approved != recorded:
        return "The approval does not reference the plan that was reviewed (plan_digest mismatch)."
    if computed != recorded:
        return "The submitted plan differs from the plan that was reviewed and approved."
    return None


# ---------- integration target ----------


def _target_problem(state: ExecutionState) -> tuple[str, str] | None:
    """The approved target, the submitted plan's target and the target recorded at planning
    time must agree wherever they are given. A client cannot relabel a plan."""
    recorded, plan, approved = state.get("recorded_target"), state.get("plan_target"), state.get("approved_target")
    if recorded is not None and plan is not None and plan != recorded:
        return ("INTEGRATION_TARGET_MISMATCH",
                f"The plan was generated for '{recorded}' but was submitted as '{plan}'.")
    expected = recorded or plan
    if approved is not None and expected is not None and approved != expected:
        return ("INTEGRATION_TARGET_MISMATCH",
                f"The approval is for '{approved}' but the plan's integration target is '{expected}'.")
    return None
