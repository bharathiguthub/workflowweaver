"""Composite telemetry (Phase 8): one facade call -> Langfuse + metrics + logs + traces.

The planning and execution code keeps calling ``get_telemetry().observe(...)`` / ``.event(...)``.
``CompositeTelemetry`` passes every call to the existing base telemetry (Langfuse or no-op)
UNCHANGED and additionally fans it out to sinks:

* MetricsSink      Prometheus counters/histograms (app.observability.metrics)
* LogSink          structured log records for the workflow lifecycle (planning, provider
                   attempts, fallback, LLM calls, execution start/outcome, approval rejections,
                   steps, retries, compensation, integration calls)
* WorkflowIndex    a bounded in-memory list of recent planning/execution runs (ids, status,
                   duration, trace id) for the UI's Workflow Traces tab
* SpanSink         OpenTelemetry spans for observations - ONLY when Langfuse is off. When
                   Langfuse is on, its spans are already OpenTelemetry spans on the same tracer
                   provider, so a second span would duplicate them.

Failure isolation: a sink error is swallowed (logged once at DEBUG) and never reaches the
workflow; the base telemetry keeps its own isolation. Sinks receive sanitised values only.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.observability.context import bind_workflow, current_trace_ids
from app.observability.logging_setup import log_fields
from app.observability.metrics import MetricsSink
from app.observability.sanitize import sanitize
from app.observability.telemetry import Telemetry, error_code_of

logger = logging.getLogger("app.workflow")
_sink_logger = logging.getLogger(__name__)


@dataclass
class ObservationRecord:
    name: str
    as_type: str
    workflow_id: str | None
    step_number: int | None
    input: Any
    metadata: dict[str, Any]
    model: str | None
    root: bool
    output: dict[str, Any] = field(default_factory=dict)
    level: str | None = None
    status_message: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    duration: float = 0.0
    trace_id: str | None = None
    started_at: str = ""


class _FanoutObservation:
    def __init__(self, base: Any, rec: ObservationRecord, span: Any = None) -> None:
        self._base, self._rec, self._span = base, rec, span

    def update(self, *, output: Any = None, metadata: dict[str, Any] | None = None, level: Any = None,
               status_message: str | None = None, usage_details: dict[str, int] | None = None) -> None:
        self._base.update(output=output, metadata=metadata, level=level, status_message=status_message,
                          usage_details=usage_details)
        try:
            if isinstance(output, dict):
                self._rec.output.update(output)
            if metadata:
                self._rec.metadata.update(metadata)
            if level is not None:
                self._rec.level = level
            if status_message is not None:
                self._rec.status_message = status_message
            if usage_details:
                self._rec.usage.update({k: v for k, v in usage_details.items() if isinstance(v, int)})
        except Exception:
            pass


# ---------- structured workflow logs ----------


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


class LogSink:
    """Structured log records for the workflow lifecycle (ids, codes, counts, timings only)."""

    def on_start(self, rec: ObservationRecord) -> None:
        inp = rec.input if isinstance(rec.input, dict) else {}
        if rec.name == "execution.request":
            logger.info("execution started", extra=log_fields(
                event="execution.started", workflow_id=rec.workflow_id, mode=inp.get("mode"),
                target=inp.get("approved_target") or inp.get("plan_target"), step_count=inp.get("step_count"),
                approved=inp.get("approved"), write_authorized=inp.get("write_authorized")))
        elif rec.name == "planning.request":
            logger.info("planning started", extra=log_fields(
                event="planning.started", workflow_id=rec.workflow_id, target=inp.get("integration_target"),
                planner=rec.metadata.get("planner"), instruction_chars=inp.get("instruction_chars")))

    def on_end(self, rec: ObservationRecord) -> None:
        out, failed = rec.output, rec.error is not None or rec.level == "ERROR"
        common = dict(workflow_id=rec.workflow_id, duration_ms=_ms(rec.duration))
        if rec.name == "planning.request":
            log = logger.info if out.get("status") == "PLANNED" else logger.warning
            log("planning finished", extra=log_fields(
                event="planning.finished", status=out.get("status"), error_code=rec.error,
                attempts=out.get("planning_attempts"), provider=out.get("provider"),
                fallback_used=out.get("fallback_used"), **common))
        elif rec.name == "execution.request":
            log = logger.warning if failed or rec.level == "WARNING" else logger.info
            log("execution request finished", extra=log_fields(
                event="execution.finished", status=out.get("status") or rec.status_message,
                replayed=rec.metadata.get("replayed"), error_code=rec.error, **common))
        elif rec.as_type == "generation":
            (logger.warning if failed else logger.info)("llm request", extra=log_fields(
                event="llm.request", provider=rec.name.split(".", 1)[0], model=rec.model,
                status="error" if failed else "success", error_code=rec.error or (rec.status_message if failed else None),
                input_tokens=rec.usage.get("input"), output_tokens=rec.usage.get("output"),
                total_tokens=rec.usage.get("total"), **common))
        elif rec.name.startswith(("tool.", "compensation.")):
            kind, integration, action = (rec.name.split(".") + ["", ""])[:3]
            error_code = out.get("error_code") or rec.error
            event = ("step" if kind == "tool" else "compensation") + (".failed" if error_code else ".succeeded")
            (logger.warning if error_code else logger.info)(event.replace(".", " "), extra=log_fields(
                event=event, integration=integration, action=action, step_number=rec.step_number,
                attempt=rec.metadata.get("attempt"), mode=rec.metadata.get("mode"), error_code=error_code,
                retryable=out.get("retryable"), **common))
        elif rec.name.startswith("google_drive.") or rec.name == "teams.webhook.post":
            integration = "google_drive" if rec.name.startswith("google_drive.") else "teams"
            bad = failed or bool(out.get("error_code"))
            (logger.warning if bad else logger.debug)("integration call", extra=log_fields(
                event="integration.call", integration=integration, action=rec.name.split(".", 1)[1],
                step_number=rec.step_number, http_status=rec.metadata.get("http_status") or out.get("http_status"),
                error_code=out.get("error_code") or rec.error, **common))

    def on_event(self, name: str, workflow_id: str | None, step_number: int | None, metadata: dict[str, Any],
                 level: str | None) -> None:
        meta = sanitize(metadata)
        log = {"WARNING": logger.warning, "ERROR": logger.error, "DEBUG": logger.debug}.get((level or "").upper(),
                                                                                          logger.info)
        if name == "execution.outcome":
            status = meta.get("status")
            codes = meta.get("error_codes") or []
            if status == "REJECTED":
                # Approval, plan, target, destination or real-write gate refused the run.
                logger.warning("execution rejected before any step ran", extra=log_fields(
                    event="approval.rejected", workflow_id=workflow_id, status=status,
                    error_code=codes[0] if codes else None, error_codes=codes))
                return
            log("execution outcome", extra=log_fields(
                event="execution.outcome", workflow_id=workflow_id, status=status,
                error_code=codes[0] if codes else None, retry_count=meta.get("retry_count"),
                total_attempts=meta.get("total_attempts"), compensation_statuses=meta.get("compensation_statuses")))
            return
        known = {"execution.retry_scheduled": "retry scheduled",
                 "execution.compensation_retry_scheduled": "compensation retry scheduled",
                 "planning.fallback": "planner fallback", "planning.provider_attempt": "planner provider attempt",
                 "execution.effect_reconciled": "effect reconciled"}
        fields = dict(meta)
        error_code = fields.pop("error_code", None) or fields.pop("reason", None)
        provider = fields.pop("provider", None)
        log(known.get(name, name), extra=log_fields(event=name, workflow_id=workflow_id, step_number=step_number,
                                                    error_code=error_code, provider=provider, **fields))


# ---------- recent workflow runs (UI Workflow Traces tab) ----------


class WorkflowIndex:
    """Bounded list of recent root planning/execution observations from this process."""

    def __init__(self, size: int = 200) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()

    def on_start(self, rec: ObservationRecord) -> None:
        return None

    def on_end(self, rec: ObservationRecord) -> None:
        if rec.name not in ("planning.request", "execution.request"):
            return
        inp = rec.input if isinstance(rec.input, dict) else {}
        entry = {
            "kind": "planning" if rec.name == "planning.request" else "execution",
            "workflow_id": rec.workflow_id,
            "trace_id": rec.trace_id,
            "started_at": rec.started_at,
            "duration_ms": _ms(rec.duration),
            "status": rec.output.get("status") or rec.status_message or ("ERROR" if rec.error else None),
            "error_code": rec.error,
            "target": inp.get("integration_target") or inp.get("approved_target") or inp.get("plan_target"),
            "mode": inp.get("mode"),
            "provider": rec.output.get("provider"),
            "fallback_used": rec.output.get("fallback_used"),
            "replayed": rec.metadata.get("replayed"),
        }
        with self._lock:
            self._items.append({k: v for k, v in entry.items() if v is not None})

    def on_event(self, *_: Any) -> None:
        return None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)[-limit:][::-1]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


WORKFLOW_INDEX = WorkflowIndex()


# ---------- composite ----------


class CompositeTelemetry:
    def __init__(self, base: Telemetry, sinks: list[Any], *, span_sink: Any = None) -> None:
        self._base = base
        self._sinks = sinks
        # Own OTel spans only when the base does not already produce them (Langfuse spans are OTel spans).
        self._span_sink = span_sink if not getattr(base, "enabled", False) else None
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._base, "enabled", False))

    @property
    def base(self) -> Telemetry:
        return self._base

    def _each(self, method: str, *args: Any) -> None:
        for sink in self._sinks:
            try:
                getattr(sink, method)(*args)
            except Exception as exc:  # a sink must never affect the workflow
                if not self._warned:
                    self._warned = True
                    _sink_logger.debug("telemetry sink %s.%s failed: %s", type(sink).__name__, method,
                                       type(exc).__name__)

    @contextmanager
    def observe(self, name: str, *, as_type: str = "span", workflow_id: str | None = None,
                step_number: int | None = None, input: Any = None, metadata: dict[str, Any] | None = None,
                model: str | None = None, model_parameters: dict[str, Any] | None = None,
                root: bool = False) -> Iterator[Any]:
        rec = ObservationRecord(name=name, as_type=as_type, workflow_id=workflow_id, step_number=step_number,
                                input=sanitize(input) if input is not None else None, metadata=dict(metadata or {}),
                                model=model, root=root,
                                started_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        with ExitStack() as stack:
            stack.enter_context(bind_workflow(workflow_id if root or workflow_id else None))
            span = None
            if self._span_sink is not None:
                try:
                    span = stack.enter_context(self._span_sink.span(rec))
                except Exception:
                    span = None
            base_obs = stack.enter_context(self._base.observe(
                name, as_type=as_type, workflow_id=workflow_id, step_number=step_number, input=input,
                metadata=metadata, model=model, model_parameters=model_parameters, root=root))
            rec.trace_id = current_trace_ids()[0]
            self._each("on_start", rec)
            started = time.perf_counter()
            try:
                yield _FanoutObservation(base_obs, rec, span)
            except BaseException as exc:
                rec.error = error_code_of(exc)
                raise
            finally:
                rec.duration = time.perf_counter() - started
                if self._span_sink is not None and span is not None:
                    try:
                        self._span_sink.finish(span, rec)
                    except Exception:
                        pass
                self._each("on_end", rec)

    def event(self, name: str, *, workflow_id: str | None = None, step_number: int | None = None,
              metadata: dict[str, Any] | None = None, level: str | None = None) -> None:
        self._base.event(name, workflow_id=workflow_id, step_number=step_number, metadata=metadata, level=level)
        self._each("on_event", name, workflow_id, step_number, dict(metadata or {}), level)
        if self._span_sink is not None:
            try:
                self._span_sink.event(name, metadata or {}, level)
            except Exception:
                pass

    def flush(self) -> None:
        self._base.flush()

    def shutdown(self) -> None:
        self._base.shutdown()


def default_sinks() -> list[Any]:
    return [MetricsSink(), LogSink(), WORKFLOW_INDEX]
