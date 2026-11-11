"""WorkflowWeaver FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.documents import router as documents_router
from app.api.drive_sandbox import router as drive_sandbox_router
from app.api.execution import router as execution_router
from app.api.execution import shutdown_execution_service
from app.api.integrations import router as integrations_router
from app.api.monitoring import router as monitoring_router
from app.api.planning import router as planning_router
from app.api.project import router as project_router
from app.api.orchestration import router as orchestration_router
from app.api.sandbox import router as sandbox_router
from app.api.streaming import router as streaming_router
from app.api.streaming import shutdown_runner
from app.api.workflows import router as workflows_router
from app.core.config import cors_origins, get_settings
from app.observability import shutdown_telemetry
from app.observability.logging_setup import configure_logging
from app.observability.middleware import RequestContextMiddleware
from app.observability.tracing import setup_tracing, shutdown_tracing

# Phase 8.1: structured logging (JSON or human-readable console, LOG_FORMAT). It also installs the
# redaction filters: SSE stream tokens in URLs and configured integration secrets are scrubbed
# from every record, and each rendered line is scrubbed again for secret-looking values.
configure_logging(get_settings())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    shutdown_runner()  # stop accepting queued async executions
    shutdown_execution_service()  # close MCP sessions and HTTP pools (no-op in mock mode)
    # Flush buffered Langfuse observations on shutdown. Telemetry itself is created lazily
    # on first use, and a failure here never affects request handling.
    shutdown_telemetry()
    shutdown_tracing()  # flush buffered OpenTelemetry spans (no-op when tracing is off)


app = FastAPI(
    lifespan=lifespan,
    title="WorkflowWeaver API",
    version="0.3.0",
    description="Phase 3: LangGraph planning plus approved MOCK execution with retries and Saga compensation.",
)

_origins = cors_origins(get_settings())
if _origins:
    # Exact origins only, no cookies/credentials, only the methods and headers the UI uses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID", "X-Stream-Token", "X-Integration-Authorization"],
        max_age=600,
    )

app.include_router(workflows_router)
app.include_router(planning_router)
app.include_router(sandbox_router)
app.include_router(drive_sandbox_router)
app.include_router(documents_router)
app.include_router(project_router)
app.include_router(orchestration_router)
app.include_router(execution_router)
app.include_router(streaming_router)
app.include_router(integrations_router)
app.include_router(monitoring_router)

# Phase 8: outermost middleware - request id, HTTP metrics and the structured access record.
app.add_middleware(RequestContextMiddleware)
# Phase 8.4: OpenTelemetry (FastAPI + httpx + workflow spans) when OTEL_ENABLED=true; registered
# before the lazily created Langfuse client so both share one tracer provider.
setup_tracing(app, get_settings())


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "workflowweaver", "version": app.version}
