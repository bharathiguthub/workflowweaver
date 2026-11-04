"""WorkflowWeaver FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.execution import router as execution_router
from app.api.execution import shutdown_execution_service
from app.api.integrations import router as integrations_router
from app.api.planning import router as planning_router
from app.api.streaming import router as streaming_router
from app.api.streaming import shutdown_runner
from app.api.workflows import router as workflows_router
from app.core.config import cors_origins, get_settings
from app.core.log_redaction import install_secret_redaction, install_token_redaction
from app.observability import shutdown_telemetry

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# SSE stream tokens travel in query strings; keep them out of access/request logs.
install_token_redaction()
# Configured integration secrets (webhook URLs, tokens) are scrubbed from every log record.
install_secret_redaction()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    shutdown_runner()  # stop accepting queued async executions
    shutdown_execution_service()  # close MCP sessions and HTTP pools (no-op in mock mode)
    # Flush buffered Langfuse observations on shutdown. Telemetry itself is created lazily
    # on first use, and a failure here never affects request handling.
    shutdown_telemetry()


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
        allow_headers=["Content-Type", "Last-Event-ID", "X-Stream-Token"],
        max_age=600,
    )

app.include_router(workflows_router)
app.include_router(planning_router)
app.include_router(execution_router)
app.include_router(streaming_router)
app.include_router(integrations_router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "workflowweaver", "version": app.version}
