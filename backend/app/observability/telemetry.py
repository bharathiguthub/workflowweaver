"""Telemetry facade used by the planning and execution code.

Code calls ``get_telemetry().observe(...)`` / ``.event(...)``. Two implementations:

* ``NoopTelemetry``     - default; used when LANGFUSE_ENABLED is false or misconfigured.
* ``LangfuseTelemetry`` - wraps a ``langfuse.Langfuse`` client (SDK v5, OpenTelemetry based).

Failure isolation (the important property): every call into the Langfuse SDK is wrapped.
If the SDK raises - at span start, update, end, event, flush or shutdown - the error is
logged (exception type only) and counted in ``failures``, and the workflow continues.
Exceptions raised by the *workflow* inside an ``observe`` block are recorded on the span
and re-raised unchanged. Export to the Langfuse server happens on the SDK's background
thread, so an unreachable server never blocks or fails a request.
"""

import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any, Literal, Protocol

from app.observability.sanitize import langfuse_mask, sanitize

logger = logging.getLogger(__name__)

ObservationType = Literal["span", "generation", "tool", "chain", "agent", "event"]
Level = Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]


class Observation(Protocol):
    def update(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        level: Level | None = None,
        status_message: str | None = None,
        usage_details: dict[str, int] | None = None,
    ) -> None: ...


class Telemetry(Protocol):
    enabled: bool

    def observe(
        self,
        name: str,
        *,
        as_type: ObservationType = "span",
        workflow_id: str | None = None,
        step_number: int | None = None,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        root: bool = False,
    ): ...

    def event(
        self,
        name: str,
        *,
        workflow_id: str | None = None,
        step_number: int | None = None,
        metadata: dict[str, Any] | None = None,
        level: Level | None = None,
    ) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


def correlation(workflow_id: str | None, step_number: int | None, metadata: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if workflow_id is not None:
        out["workflow_id"] = workflow_id
    if step_number is not None:
        out["step_number"] = step_number
    return {**out, **(metadata or {})}


def error_code_of(exc: BaseException) -> str:
    """Status text for a failed span: the error *code* if the exception has one, else its
    type name. Never the exception message (it may contain user data)."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else type(exc).__name__


# ---------- no-op ----------


class _NoopObservation:
    def update(self, **_: Any) -> None:
        return None


_NOOP_OBSERVATION = _NoopObservation()


class NoopTelemetry:
    enabled = False

    @contextmanager
    def observe(self, name: str, **_: Any) -> Iterator[Observation]:
        yield _NOOP_OBSERVATION

    def event(self, name: str, **_: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


# ---------- Langfuse ----------


class _SafeObservation:
    """Wraps a Langfuse observation; accumulates metadata; never raises."""

    def __init__(self, span: Any, owner: "LangfuseTelemetry", metadata: dict[str, Any]) -> None:
        self._span = span
        self._owner = owner
        self._metadata = dict(metadata)

    def update(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        level: Level | None = None,
        status_message: str | None = None,
        usage_details: dict[str, int] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = sanitize(output)
        if metadata:
            self._metadata.update(metadata)
            kwargs["metadata"] = sanitize(self._metadata)
        if level is not None:
            kwargs["level"] = level
        if status_message is not None:
            kwargs["status_message"] = sanitize(status_message)
        if usage_details:
            kwargs["usage_details"] = {k: int(v) for k, v in usage_details.items() if isinstance(v, int)}
        if not kwargs:
            return
        try:
            self._span.update(**kwargs)
        except Exception as exc:
            self._owner.record_failure("update", exc)


class LangfuseTelemetry:
    enabled = True

    def __init__(self, client: Any, *, propagate: Any = None) -> None:
        self._client = client
        if propagate is None:
            from langfuse import propagate_attributes as propagate
        self._propagate = propagate
        self.failures = 0
        self._lock = threading.Lock()
        self._warned: set[str] = set()

    def record_failure(self, operation: str, exc: BaseException) -> None:
        with self._lock:
            self.failures += 1
            first = operation not in self._warned
            self._warned.add(operation)
        log = logger.warning if first else logger.debug
        log("telemetry operation=%s failed error=%s (workflow continues)", operation, type(exc).__name__)

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: ObservationType = "span",
        workflow_id: str | None = None,
        step_number: int | None = None,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        root: bool = False,
    ) -> Iterator[Observation]:
        meta = correlation(workflow_id, step_number, metadata)
        stack = ExitStack()
        observation: Observation = _NOOP_OBSERVATION
        try:
            if root and workflow_id:
                stack.enter_context(
                    self._propagate(
                        session_id=workflow_id[:200],
                        trace_name=name,
                        metadata={"workflow_id": workflow_id},
                        tags=["workflowweaver"],
                    )
                )
            kwargs: dict[str, Any] = {
                "name": name,
                "as_type": as_type,
                "metadata": sanitize(meta),
            }
            if input is not None:
                kwargs["input"] = sanitize(input)
            if model is not None:
                kwargs["model"] = model
            if model_parameters:
                kwargs["model_parameters"] = sanitize(model_parameters)
            span = stack.enter_context(self._client.start_as_current_observation(**kwargs))
            observation = _SafeObservation(span, self, meta)
        except Exception as exc:
            self.record_failure("start", exc)

        started = time.perf_counter()
        try:
            yield observation
        except BaseException as exc:
            observation.update(level="ERROR", status_message=error_code_of(exc))
            raise
        finally:
            observation.update(metadata={"latency_ms": round((time.perf_counter() - started) * 1000, 2)})
            try:
                stack.close()
            except Exception as exc:
                self.record_failure("end", exc)

    def event(
        self,
        name: str,
        *,
        workflow_id: str | None = None,
        step_number: int | None = None,
        metadata: dict[str, Any] | None = None,
        level: Level | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"name": name, "metadata": sanitize(correlation(workflow_id, step_number, metadata))}
        if level is not None:
            kwargs["level"] = level
        try:
            self._client.create_event(**kwargs)
        except Exception as exc:
            self.record_failure("event", exc)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            self.record_failure("flush", exc)

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception as exc:
            self.record_failure("shutdown", exc)


# ---------- factory / global ----------


def langfuse_environment(app_env: str) -> str:
    """Langfuse environments: lowercase [a-z0-9_-], <= 40 chars, not starting with 'langfuse'."""
    env = re.sub(r"[^a-z0-9_-]", "-", app_env.lower())[:40] or "development"
    return "development" if env.startswith("langfuse") else env


def create_telemetry(settings: Any, *, client_factory: Any = None) -> Telemetry:
    """Build telemetry from settings. Never raises: any problem means NoopTelemetry."""
    if not settings.langfuse_enabled:
        return NoopTelemetry()
    secret = settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else ""
    if not settings.langfuse_public_key or not secret:
        logger.warning("LANGFUSE_ENABLED=true but LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY missing; telemetry disabled")
        return NoopTelemetry()
    try:
        if client_factory is None:
            from langfuse import Langfuse as client_factory
        kwargs: dict[str, Any] = {
            "public_key": settings.langfuse_public_key,
            "secret_key": secret,
            "tracing_enabled": True,
            "environment": langfuse_environment(settings.app_env),
            "mask": langfuse_mask,
        }
        if settings.langfuse_base_url:
            kwargs["base_url"] = settings.langfuse_base_url
        client = client_factory(**kwargs)
    except Exception as exc:
        logger.warning("Langfuse client could not be created (error=%s); telemetry disabled", type(exc).__name__)
        return NoopTelemetry()
    logger.info("Langfuse telemetry enabled base_url=%s", settings.langfuse_base_url or "<sdk default>")
    return LangfuseTelemetry(client)


_telemetry: Telemetry | None = None
_telemetry_lock = threading.Lock()


def get_telemetry() -> Telemetry:
    """Process-wide telemetry, created lazily from settings on first use."""
    global _telemetry
    if _telemetry is None:
        with _telemetry_lock:
            if _telemetry is None:
                from app.core.config import get_settings

                try:
                    base = create_telemetry(get_settings())
                except Exception as exc:  # e.g. invalid settings: never break the app for telemetry
                    logger.warning("telemetry setup failed error=%s; telemetry disabled", type(exc).__name__)
                    base = NoopTelemetry()
                _telemetry = _with_sinks(base)
    return _telemetry


def _with_sinks(base: "Telemetry") -> "Telemetry":
    """Phase 8: wrap the base (Langfuse or no-op) with metrics, structured logs, the recent
    workflow index and - when OpenTelemetry tracing is on and Langfuse is off - workflow spans."""
    try:
        from app.observability.composite import CompositeTelemetry, default_sinks
        from app.observability.tracing import span_sink

        return CompositeTelemetry(base, default_sinks(), span_sink=span_sink())
    except Exception as exc:  # observability extras must never disable the base telemetry
        logger.warning("telemetry sinks unavailable error=%s", type(exc).__name__)
        return base


def set_telemetry(telemetry: Telemetry | None) -> None:
    """Install a telemetry implementation (tests, scripts). None resets to lazy creation."""
    global _telemetry
    with _telemetry_lock:
        _telemetry = telemetry


def shutdown_telemetry() -> None:
    """Flush and stop telemetry if it was created. Safe to call more than once."""
    global _telemetry
    with _telemetry_lock:
        current, _telemetry = _telemetry, None
    if current is not None:
        current.shutdown()
