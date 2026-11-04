"""Phase 3 execution endpoints (MOCK tools only - no real Notion, Drive or Teams calls).

POST /api/v1/workflows/execute
  200  COMPLETED                  all steps succeeded
  403  REJECTED                   approval.approved is false (nothing executed)
  422  REJECTED                   plan failed re-validation (body: ExecutionResponse)
  422                             request body invalid (FastAPI "detail")
  409                             workflow_id already used for a different plan
  502  FAILED | ROLLED_BACK | PARTIALLY_ROLLED_BACK | COMPENSATION_FAILED
                                  a tool step failed; see errors / compensation_results
  503                             mock execution disabled (MOCK_EXECUTION_ENABLED=false)
A duplicate request (same workflow_id + plan) returns the stored response with replayed=true
and the same HTTP status; no tool is called again.

GET /api/v1/workflows/executions/{workflow_id}
  200 stored ExecutionResponse, 404 if unknown.
"""

from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService, IdempotencyConflict
from app.agents.execution.tools import create_tool_registry
from app.core.config import get_settings
from app.schemas.execution import ExecutionRequest, ExecutionResponse, ExecutionStatus

router = APIRouter(prefix="/api/v1/workflows", tags=["execution"])

_HTTP_STATUS = {
    ExecutionStatus.COMPLETED: status.HTTP_200_OK,
    ExecutionStatus.FAILED: status.HTTP_502_BAD_GATEWAY,
    ExecutionStatus.ROLLED_BACK: status.HTTP_502_BAD_GATEWAY,
    ExecutionStatus.PARTIALLY_ROLLED_BACK: status.HTTP_502_BAD_GATEWAY,
    ExecutionStatus.COMPENSATION_FAILED: status.HTTP_502_BAD_GATEWAY,
}


@lru_cache(maxsize=1)
def _default_service() -> ExecutionService:
    settings = get_settings()
    return ExecutionService(
        create_tool_registry(settings),
        RetryPolicy.from_settings(settings),
        max_steps=settings.max_workflow_steps,
    )


def get_execution_service() -> ExecutionService:
    if not get_settings().mock_execution_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Mock execution is disabled."
        )
    return _default_service()


def http_status_for(response: ExecutionResponse) -> int:
    if response.status is ExecutionStatus.REJECTED:
        if any(e.code == "APPROVAL_REQUIRED" for e in response.errors):
            return status.HTTP_403_FORBIDDEN
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return _HTTP_STATUS[response.status]


@router.post(
    "/execute",
    response_model=ExecutionResponse,
    responses={
        403: {"model": ExecutionResponse, "description": "Not approved; nothing executed"},
        409: {"description": "workflow_id already executed with a different plan"},
        422: {"model": ExecutionResponse, "description": "Plan rejected by validator, or invalid body"},
        502: {"model": ExecutionResponse, "description": "A step failed; see compensation_results"},
        503: {"description": "Mock execution disabled"},
    },
)
def execute_workflow(
    payload: ExecutionRequest, service: ExecutionService = Depends(get_execution_service)
) -> JSONResponse:
    try:
        result = service.execute(payload)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return JSONResponse(status_code=http_status_for(result), content=result.model_dump(mode="json"))


@router.get("/executions/{workflow_id}", response_model=ExecutionResponse)
def get_execution(
    workflow_id: UUID, service: ExecutionService = Depends(get_execution_service)
) -> ExecutionResponse:
    result = service.get(str(workflow_id))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No execution for this workflow_id.")
    return result
