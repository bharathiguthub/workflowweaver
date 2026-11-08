"""Execution endpoints. Each request selects its integration mode with ``approval.mode``;
the server's INTEGRATION_MODE is the ceiling (mock < dry_run < real) and a request above it
is rejected with MODE_MISMATCH. Mock is the default and calls no real service.

POST /api/v1/workflows/execute
  Header X-Integration-Authorization: operator token, needed only for real-mode writes.
  200  COMPLETED                  all steps succeeded
  403  REJECTED                   approval.approved is false, real writes disabled, or the
                                  operator authorization is missing/invalid (nothing executed)
  409  REJECTED                   approval.mode exceeds the server's INTEGRATION_MODE
  422  REJECTED                   plan failed re-validation (body: ExecutionResponse)
  422                             request body invalid (FastAPI "detail")
  409                             workflow_id already used for a different plan or mode
  502  FAILED | ROLLED_BACK | PARTIALLY_ROLLED_BACK | COMPENSATION_FAILED
                                  a tool step failed; see errors / compensation_results
  503                             execution disabled (MOCK_EXECUTION_ENABLED=false) or
                                  invalid integration configuration
A duplicate request (same workflow_id + plan) returns the stored response with replayed=true
and the same HTTP status; no tool is called again.

GET /api/v1/workflows/executions/{workflow_id}
  200 stored ExecutionResponse, 404 if unknown.
"""

import hmac
from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.execution.modes import ModeRoutedExecutionService
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService, IdempotencyConflict
from app.agents.execution.tools import create_tool_registry
from app.core.config import IntegrationMode, Settings, get_settings
from app.integrations.errors import IntegrationConfigError
from app.schemas.execution import ExecutionRequest, ExecutionResponse, ExecutionStatus

router = APIRouter(prefix="/api/v1/workflows", tags=["execution"])

_HTTP_STATUS = {
    ExecutionStatus.COMPLETED: status.HTTP_200_OK,
    ExecutionStatus.FAILED: status.HTTP_502_BAD_GATEWAY,
    ExecutionStatus.ROLLED_BACK: status.HTTP_502_BAD_GATEWAY,
    ExecutionStatus.PARTIALLY_ROLLED_BACK: status.HTTP_502_BAD_GATEWAY,
    ExecutionStatus.COMPENSATION_FAILED: status.HTTP_502_BAD_GATEWAY,
    ExecutionStatus.PARTIALLY_COMPLETED: status.HTTP_502_BAD_GATEWAY,
}


def _build_service(mode: IntegrationMode) -> ExecutionService:
    """One ExecutionService per mode; ``mode`` is at or below the server's ceiling."""
    settings = get_settings()
    mode_settings = settings if mode is settings.integration_mode else settings.model_copy(
        update={"integration_mode": mode})
    return ExecutionService(
        create_tool_registry(mode_settings),
        RetryPolicy.from_settings(settings),
        max_steps=settings.max_workflow_steps,
    )


@lru_cache(maxsize=1)
def _default_service() -> ModeRoutedExecutionService:
    return ModeRoutedExecutionService(get_settings().integration_mode, _build_service)


def get_execution_service() -> ModeRoutedExecutionService:
    if not get_settings().mock_execution_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Mock execution is disabled."
        )
    return _default_service()


def config_unavailable(exc: IntegrationConfigError) -> HTTPException:
    # The message names the setting, never its value.
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def shutdown_execution_service() -> None:
    """Close MCP sessions / HTTP pools of the cached services, if any were built."""
    if _default_service.cache_info().currsize:
        _default_service().close()
        _default_service.cache_clear()


def verify_write_authorization(settings: Settings, presented: str | None) -> bool:
    """True only in real mode, with a configured operator token that ``presented`` matches
    (constant-time comparison). The token value is never logged or echoed."""
    if settings.integration_mode is not IntegrationMode.REAL or not presented:
        return False
    expected = settings.real_write_authorization_token
    if expected is None or not expected.get_secret_value():
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.get_secret_value().encode("utf-8"))


def get_write_authorization(
    x_integration_authorization: str | None = Header(default=None, max_length=512),
) -> bool:
    return verify_write_authorization(get_settings(), x_integration_authorization)


_FORBIDDEN_CODES = {"APPROVAL_REQUIRED", "REAL_WRITES_DISABLED", "REAL_WRITE_AUTHORIZATION_REQUIRED",
                    "INTEGRATION_TARGET_MISMATCH"}


def http_status_for(response: ExecutionResponse) -> int:
    if response.status is ExecutionStatus.REJECTED:
        codes = {e.code for e in response.errors}
        if codes & _FORBIDDEN_CODES:
            return status.HTTP_403_FORBIDDEN
        if "MODE_MISMATCH" in codes:
            return status.HTTP_409_CONFLICT
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return _HTTP_STATUS[response.status]


@router.post(
    "/execute",
    response_model=ExecutionResponse,
    responses={
        403: {"model": ExecutionResponse, "description": "Not approved / real writes not authorized"},
        409: {"description": "workflow_id already executed with a different plan, or mode mismatch"},
        422: {"model": ExecutionResponse, "description": "Plan rejected by validator, or invalid body"},
        502: {"model": ExecutionResponse, "description": "A step failed; see compensation_results"},
        503: {"description": "Execution disabled or integration configuration invalid"},
    },
)
def execute_workflow(
    payload: ExecutionRequest,
    service: ModeRoutedExecutionService = Depends(get_execution_service),
    write_authorized: bool = Depends(get_write_authorization),
) -> JSONResponse:
    try:
        result = service.execute(payload, write_authorized=write_authorized)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrationConfigError as exc:
        raise config_unavailable(exc) from exc
    return JSONResponse(status_code=http_status_for(result), content=result.model_dump(mode="json"))


@router.get("/executions/{workflow_id}", response_model=ExecutionResponse)
def get_execution(
    workflow_id: UUID, service: ModeRoutedExecutionService = Depends(get_execution_service)
) -> ExecutionResponse:
    result = service.get(str(workflow_id))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No execution for this workflow_id.")
    return result
