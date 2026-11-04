"""WorkflowWeaver FastAPI application entry point."""

import logging

from fastapi import FastAPI

from app.api.execution import router as execution_router
from app.api.planning import router as planning_router
from app.api.workflows import router as workflows_router
from app.core.config import get_settings

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="WorkflowWeaver API",
    version="0.3.0",
    description="Phase 3: LangGraph planning plus approved MOCK execution with retries and Saga compensation.",
)

app.include_router(workflows_router)
app.include_router(planning_router)
app.include_router(execution_router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "workflowweaver", "version": app.version}
