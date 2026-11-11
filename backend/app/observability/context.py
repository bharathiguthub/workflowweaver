"""Correlation context for logs, metrics and traces (Phase 8).

``request_id`` is set per HTTP request by RequestContextMiddleware; ``workflow_id`` by the
telemetry facade when a root planning/execution observation starts. Both are contextvars, so
they follow the request (and any code it calls in the same thread or task) automatically.
Background execution threads set ``workflow_id`` themselves through the same facade.
"""

import contextvars
import re
from collections.abc import Iterator
from contextlib import contextmanager

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ww_request_id", default=None)
workflow_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ww_workflow_id", default=None)

# A caller-supplied X-Request-ID is accepted only if it is short and plain; otherwise a fresh
# id is generated (prevents log injection and unbounded values).
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def valid_request_id(value: str | None) -> str | None:
    return value if value and _REQUEST_ID.fullmatch(value) else None


@contextmanager
def bind_workflow(workflow_id: str | None) -> Iterator[None]:
    if not workflow_id:
        yield
        return
    token = workflow_id_var.set(str(workflow_id)[:64])
    try:
        yield
    finally:
        workflow_id_var.reset(token)


def current_trace_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) of the active OpenTelemetry span, if any (Langfuse spans included)."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx is not None and ctx.is_valid:
            return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    except Exception:
        pass
    return None, None
