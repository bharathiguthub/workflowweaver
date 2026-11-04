"""Phase 2 planning endpoint: natural language -> validated plan via LangGraph.

HTTP status codes:
  200  PLANNED  - plan generated and validated (not executed)
  422  INVALID  - plan generated but rejected by the validator (body: PlanResponse)
  422           - request body failed Pydantic validation (body: FastAPI "detail")
  502  FAILED   - planner could not produce a plan (timeout, API error, bad LLM output)
  503           - planner is not configured (e.g. PLANNER_MODE=claude without ANTHROPIC_MODEL)
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.planners import PlannerConfigError, create_planner
from app.agents.service import PlanningService
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.core.config import get_settings
from app.schemas.plan import PlanRequest, PlanResponse, PlanStatus

router = APIRouter(prefix="/api/v1/workflows", tags=["planning"])

_HTTP_STATUS = {
    PlanStatus.PLANNED: status.HTTP_200_OK,
    PlanStatus.INVALID: status.HTTP_422_UNPROCESSABLE_ENTITY,
    PlanStatus.FAILED: status.HTTP_502_BAD_GATEWAY,
}


@lru_cache(maxsize=1)
def _default_service() -> PlanningService:
    settings = get_settings()
    return PlanningService(
        create_planner(settings),
        max_steps=settings.max_workflow_steps,
        max_planning_attempts=settings.max_planning_attempts,
        # Mock execution reads only synthetic documents, so plans are checked against them.
        available_documents=frozenset(SYNTHETIC_DRIVE_DOCUMENTS) if settings.mock_execution_enabled else None,
    )


def get_planning_service() -> PlanningService:
    try:
        return _default_service()
    except PlannerConfigError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post(
    "/plan",
    response_model=PlanResponse,
    responses={
        422: {"model": PlanResponse, "description": "Plan rejected by validator, or invalid request body"},
        502: {"model": PlanResponse, "description": "Planner failed (timeout, API error, invalid LLM output)"},
        503: {"description": "Planner not configured"},
    },
)
def plan_workflow(
    payload: PlanRequest, service: PlanningService = Depends(get_planning_service)
) -> JSONResponse:
    result = service.plan(payload.instruction, name=payload.name)
    return JSONResponse(status_code=_HTTP_STATUS[result.status], content=result.model_dump(mode="json"))
