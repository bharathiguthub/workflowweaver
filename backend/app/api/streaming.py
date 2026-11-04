"""Phase 4 Stage 2: start a mock execution asynchronously and stream its progress (SSE).

POST /api/v1/workflows/executions
    Body: ExecutionRequest (same as POST /execute). Starts the run on a worker thread.
    202  ExecutionStartResponse {stream_url, stream_token, result_url}
    409  workflow_id already executed with a different plan, its stream is still active,
         or approval.mode does not match the server's INTEGRATION_MODE
    422  invalid body
    503  streaming disabled (MOCK_EXECUTION_ENABLED=false or APP_ENV=production) / at capacity

GET /api/v1/workflows/executions/{workflow_id}/events?access_token=...
    text/event-stream of ExecutionEvent. Token via ``access_token`` query parameter
    (EventSource cannot send headers) or ``X-Stream-Token`` header. Resume with the
    ``Last-Event-ID`` header (sent automatically by EventSource) or ``last_event_id``.
    200  stream; ends after workflow_completed / workflow_failed
    204  nothing left to send (stream finished and the client already has every event)
         - per the SSE spec, this tells EventSource to stop reconnecting
    404  unknown workflow, expired stream, or wrong token (deliberately indistinguishable)
    503  streaming disabled

Mock-only access model: the token is an unguessable per-workflow capability returned only
to whoever started the run. It is NOT user authentication. Streaming is refused when
APP_ENV=production; a real deployment needs authenticated users and per-user authorization.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.agents.execution.service import IdempotencyConflict, ModeMismatch
from app.api.execution import _default_service, get_write_authorization
from app.core.config import get_settings
from app.integrations.errors import IntegrationConfigError
from app.schemas.events import ExecutionEvent, ExecutionStartResponse
from app.schemas.execution import ExecutionRequest
from app.streaming.broker import BrokerFull, ChannelBusy, EventBroker, EventChannel
from app.streaming.runner import ExecutionRunner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workflows", tags=["streaming"])

SSE_RETRY_MS = 3000
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


@lru_cache(maxsize=1)
def _default_runner() -> ExecutionRunner:
    settings = get_settings()
    broker = EventBroker(retention_seconds=settings.sse_retention_seconds, max_channels=settings.sse_max_channels)
    # Shares the synchronous endpoint's service, so idempotency covers both endpoints.
    return ExecutionRunner(_default_service(), broker, max_workers=settings.execution_worker_threads)


def get_execution_runner() -> ExecutionRunner:
    settings = get_settings()
    if not settings.mock_execution_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Mock execution is disabled.")
    if settings.app_env.lower() == "production":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Streaming uses a mock-only access model and is disabled when APP_ENV=production.",
        )
    try:
        return _default_runner()
    except IntegrationConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


def get_heartbeat_seconds() -> float:
    return get_settings().sse_heartbeat_seconds


def shutdown_runner() -> None:
    if _default_runner.cache_info().currsize:
        _default_runner().shutdown()
        _default_runner.cache_clear()


def format_sse(event: ExecutionEvent) -> str:
    return f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {event.model_dump_json()}\n\n"


async def event_stream(
    channel: EventChannel, after_event_id: int, heartbeat_seconds: float, request: Request | None = None
) -> AsyncIterator[str]:
    """Backlog first, then live events, until the terminal event. Unsubscribes on exit,
    including when the client disconnects (Starlette cancels this generator).

    One pending ``queue.get()`` task is kept across heartbeats and is never cancelled by a
    heartbeat timeout: cancelling a get() that has just been handed an item can drop that
    item on Python < 3.12 (asyncio.wait_for race), which would lose an event.
    """
    subscription = channel.subscribe(after_event_id)
    logger.info("sse subscribe workflow_id=%s after=%d", channel.workflow_id, after_event_id)
    pending: asyncio.Task | None = None
    reason = "closed"  # generator closed by the server (e.g. response aborted)
    try:
        yield f"retry: {SSE_RETRY_MS}\n\n"
        for event in subscription.backlog:
            yield format_sse(event)
            if event.is_terminal:
                reason = "terminal"
                return
        while True:
            if pending is None:
                pending = asyncio.ensure_future(subscription.queue.get())
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                if request is not None and await request.is_disconnected():
                    reason = "client_disconnected"
                    return
                yield ": keep-alive\n\n"  # SSE comment; also surfaces dead connections
                continue
            event, pending = pending.result(), None
            yield format_sse(event)
            if event.is_terminal:
                reason = "terminal"
                return
    except asyncio.CancelledError:
        reason = "cancelled"  # Starlette saw the client disconnect
        raise
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        channel.unsubscribe(subscription.id)
        logger.info("sse unsubscribe workflow_id=%s reason=%s", channel.workflow_id, reason)


@router.post(
    "/executions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExecutionStartResponse,
    responses={409: {"description": "Different plan for this workflow_id, or stream already active"},
               503: {"description": "Streaming disabled or at capacity"}},
)
def start_execution(
    payload: ExecutionRequest,
    runner: ExecutionRunner = Depends(get_execution_runner),
    write_authorized: bool = Depends(get_write_authorization),
) -> ExecutionStartResponse:
    try:
        channel, _ = runner.start(payload, write_authorized=write_authorized)
    except (IdempotencyConflict, ModeMismatch) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ChannelBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "An execution stream for this workflow_id is already active.") from exc
    except BrokerFull as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Too many active execution streams.") from exc
    wf = str(payload.workflow_id)
    return ExecutionStartResponse(
        workflow_id=payload.workflow_id,
        mode=runner.service.registry.mode.value,
        stream_url=f"/api/v1/workflows/executions/{wf}/events?access_token={channel.token}",
        stream_token=channel.token,
        result_url=f"/api/v1/workflows/executions/{wf}",
    )


@router.get(
    "/executions/{workflow_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "ExecutionEvent stream"},
               204: {"description": "Stream finished; nothing after Last-Event-ID"},
               404: {"description": "Unknown workflow, expired stream, or wrong token"}},
)
async def stream_events(
    workflow_id: str,
    request: Request,
    access_token: str | None = Query(default=None, max_length=200),
    last_event_id_query: int | None = Query(default=None, alias="last_event_id", ge=0),
    x_stream_token: str | None = Header(default=None, max_length=200),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID", max_length=20),
    runner: ExecutionRunner = Depends(get_execution_runner),
    heartbeat_seconds: float = Depends(get_heartbeat_seconds),
):
    channel = runner.broker.get(workflow_id, access_token or x_stream_token)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No stream for this workflow_id and token.")

    after = last_event_id_query or 0
    if last_event_id_header and last_event_id_header.isdigit():
        after = max(after, int(last_event_id_header))
    if channel.terminal and not channel.has_events_after(after):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return StreamingResponse(
        event_stream(channel, after, heartbeat_seconds, request),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
