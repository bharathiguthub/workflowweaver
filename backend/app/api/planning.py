"""Phase 2 planning endpoint: natural language -> validated plan via LangGraph.

HTTP status codes:
  200  PLANNED  - plan generated and validated (not executed)
  422  INVALID  - plan generated but rejected by the validator (body: PlanResponse)
  422           - request body failed Pydantic validation (body: FastAPI "detail")
  502  FAILED   - planner could not produce a plan (timeout, API error, bad LLM output)
  409           - mode above the server's INTEGRATION_MODE (requests with a target)
  503           - planner is not configured (e.g. PLANNER_MODE=claude without ANTHROPIC_MODEL), or
                  a Real-mode Google Drive plan cannot be made (no AI planner, Drive sandbox not ready)

Real-mode custom plans (requests WITH a target): only for target=google_drive or target=teams,
only from an AI planner (Groq with Gemini fallback, or Claude). In dry_run/real every Google Drive
step must target the sandbox folder alias (SMOKE_TEST_DRIVE_FOLDER) or a folder created earlier in
the same plan, and every Teams step must post to the sandbox channel alias (SMOKE_TEST_TEAMS_CHANNEL).
Requests without a target keep the previous behaviour.
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.execution.modes import allowed_modes
from app.agents.execution.retry import RetryPolicy
from app.agents.planners.base import provider_chain
from app.integrations.config import load_integration_config
from app.integrations.errors import IntegrationConfigError
from app.integrations.readiness import (
    drive_sandbox_issues,
    real_drive_issues,
    real_teams_issues,
    sandbox_parent_issues,
    teams_sandbox_issues,
)
from app.agents.plan_registry import get_plan_registry
from app.agents.planners import PlannerConfigError, create_planner
from app.agents.service import PlanningService
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.core.config import IntegrationMode, get_settings, mock_documents_apply
from app.schemas.plan import IntegrationTarget, PlanRequest, PlanResponse, PlanStatus

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
        backoff=RetryPolicy.for_planning(settings),
        # Mock execution reads only synthetic documents, so plans are checked against them.
        # Dry-run/real modes cannot know the document catalog offline: no check.
        available_documents=frozenset(SYNTHETIC_DRIVE_DOCUMENTS) if mock_documents_apply(settings) else None,
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
    settings = get_settings()
    if payload.target is IntegrationTarget.DRIVE_NOTION_TEAMS:
        # Phase 7.2: this target is only for the server-compiled orchestration workflow.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "The Google Drive + Notion + Teams target is planned by POST "
                            "/api/v1/workflows/orchestration/plan (a selected source document), not by free-form plans.")
    target = payload.target.value if payload.target is not None else None
    drive_folders = _target_rules(payload, settings, service) if target is not None else None
    notion_parents = _notion_parents(payload, settings) if target is not None else None
    teams_channels = _teams_channels(payload, settings) if target is not None else None
    if payload.mode is None or IntegrationMode(payload.mode) is settings.integration_mode:
        result = service.plan(payload.instruction, name=payload.name, target=target, drive_folders=drive_folders,
                              notion_parents=notion_parents, teams_channels=teams_channels)
    else:
        # Mock execution reads only the synthetic documents; other modes cannot know the
        # remote catalog offline, so their plans are not checked against it.
        check = payload.mode == IntegrationMode.MOCK.value and settings.mock_execution_enabled
        result = service.plan(payload.instruction, name=payload.name, target=target, drive_folders=drive_folders,
                              notion_parents=notion_parents, teams_channels=teams_channels,
                              available_documents=frozenset(SYNTHETIC_DRIVE_DOCUMENTS) if check else None)
    # The executor checks approvals and every step against the target recorded here.
    get_plan_registry().record(result.workflow_id, target, drive_folders, teams_channels)
    return JSONResponse(status_code=_HTTP_STATUS[result.status], content=result.model_dump(mode="json"))


def _notion_parents(payload: PlanRequest, settings) -> list[str] | None:
    """The approved Notion parent alias for a dry-run Notion plan: the sandbox parent
    (SMOKE_TEST_NOTION_PARENT) when it is a configured destination alias. Used only for the
    planner prompt and to repair `parent_page` safely; the executor's destination allowlist and
    sandbox-only checks still decide. None (nothing approved/known) in every other case, so no
    `parent` is ever filled in. Never raises: this adds no new failure mode to planning."""
    mode = IntegrationMode(payload.mode) if payload.mode is not None else settings.integration_mode
    if mode is not IntegrationMode.DRY_RUN or payload.target not in (IntegrationTarget.NOTION, IntegrationTarget.MULTI):
        return None
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError:
        return None
    if sandbox_parent_issues(settings, config):
        return None
    return [settings.smoke_test_notion_parent]


def _teams_channels(payload: PlanRequest, settings) -> list[str] | None:
    """Approved Teams channel aliases for a dry-run/real Teams Only plan: exactly the sandbox
    channel (SMOKE_TEST_TEAMS_CHANNEL). 503 when it is not configured for the mode; None in mock
    mode and for other targets (mock delivery needs no webhook and makes no network call)."""
    mode = IntegrationMode(payload.mode) if payload.mode is not None else settings.integration_mode
    if payload.target is not IntegrationTarget.TEAMS or mode is IntegrationMode.MOCK:
        return None
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    issues = real_teams_issues(settings, config) if mode is IntegrationMode.REAL else teams_sandbox_issues(settings, config)
    if issues:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, " ".join(issues))
    assert settings.smoke_test_teams_channel
    return [settings.smoke_test_teams_channel]


def _target_rules(payload: PlanRequest, settings, service: PlanningService) -> list[str] | None:
    """Rules for target-aware requests (the UI). Returns the approved Google Drive folder
    aliases the plan must stay inside, or None when destinations are not restricted (mock)."""
    mode = IntegrationMode(payload.mode) if payload.mode is not None else settings.integration_mode
    target = payload.target
    if mode not in allowed_modes(settings.integration_mode):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"This server allows modes up to '{settings.integration_mode.value}'.")
    if mode is IntegrationMode.REAL:
        if target not in (IntegrationTarget.GOOGLE_DRIVE, IntegrationTarget.TEAMS):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Real-mode custom plans are available only for the Google Drive Only and "
                                "Microsoft Teams Only targets.")
        if any(getattr(p, "name", "mock") == "mock" for p in provider_chain(service.planner)):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Real-mode custom plans must come from an AI planner (PLANNER_MODE=auto, gemini "
                                "or claude); PLANNER_MODE=mock is not allowed.")
    if target is not IntegrationTarget.GOOGLE_DRIVE or mode is IntegrationMode.MOCK:
        return None
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    issues = real_drive_issues(settings, config) if mode is IntegrationMode.REAL else drive_sandbox_issues(settings, config)
    if issues:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, " ".join(issues))
    assert settings.smoke_test_drive_folder
    return [settings.smoke_test_drive_folder]
