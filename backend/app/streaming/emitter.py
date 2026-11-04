"""Run-scoped event emission used by the execution graph nodes.

The publisher is held in a ContextVar that the ExecutionRunner sets in the worker thread
running one specific request. LangGraph propagates the context into node execution, so
nodes of *that* run publish to *that* workflow's channel. Everything else - the synchronous
POST /execute endpoint, tests, scripts, a concurrent run of another workflow - has no
publisher in its context, and ``emit`` is a no-op there.

``emit`` never raises: a streaming problem must not change a workflow's outcome.
"""

import contextvars
import logging
from typing import Any, Protocol

from app.schemas.events import ExecutionEventType

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    def publish(
        self, event_type: ExecutionEventType, *, step_number: int | None = None, metadata: dict[str, Any] | None = None
    ) -> Any: ...


_publisher: contextvars.ContextVar[Publisher | None] = contextvars.ContextVar("execution_event_publisher", default=None)


def set_publisher(publisher: Publisher | None) -> contextvars.Token:
    return _publisher.set(publisher)


def reset_publisher(token: contextvars.Token) -> None:
    _publisher.reset(token)


def emit(event_type: ExecutionEventType, *, step_number: int | None = None, **metadata: Any) -> None:
    publisher = _publisher.get()
    if publisher is None:
        return
    try:
        publisher.publish(event_type, step_number=step_number, metadata=metadata)
    except Exception as exc:
        logger.warning("event emit failed type=%s error=%s (workflow continues)", event_type.value, type(exc).__name__)
