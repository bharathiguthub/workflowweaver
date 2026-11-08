"""Phase 5.3 business workflow endpoint: natural language -> AI project plan for Notion.

POST /api/v1/workflows/notion-project/plan   body {"instruction": str, "mode": mock|dry_run|real}
    200  PLANNED   project page + one child page per action item. NOTHING is executed.
    422  INVALID   the AI plan broke a rule (named person as owner, deadline, too many items,
                   unsafe content) or failed the plan validator; see validation.issues
    422            invalid request body
    502  FAILED    the planner failed (timeout, API error, invalid output) after retries
    409            mode exceeds the server's INTEGRATION_MODE
    503            execution disabled, Notion sandbox not ready for the mode, or the Claude
                   planner is not configured (detail names the setting, never its value)

Planner: Claude (PLANNER_MODE=claude), Gemini (PLANNER_MODE=gemini), or PLANNER_MODE=auto:
PRIMARY_PLANNER (default groq) with automatic fallback to FALLBACK_PLANNER (default gemini)
on provider outages only. The response's ``planner``/``model``/``fallback_used`` say which
provider actually produced the plan. With PLANNER_MODE=mock a
deterministic stub (labelled "mock (not AI)") serves mock and dry-run plans; REAL-mode plans
always require an AI planner. ``planner`` and ``model`` in the response say which one. Planning runs through the existing LangGraph planning graph (retries, validator,
Langfuse). Execution uses the normal approval-gated /executions endpoint.
"""

import time
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.execution.modes import allowed_modes
from app.agents.execution.retry import RetryPolicy
from app.agents.planners.base import FallbackPlanner, PlannerConfigError
from app.agents.planners.factory import (
    ai_planner_problem,
    build_auto_chain,
    create_anthropic_client,
    create_gemini_client,
    create_groq_client,
)
from app.agents.project_workflow import (
    AI_PROJECT_PLANNERS,
    ClaudeProjectPlanner,
    GeminiProjectPlanner,
    GroqProjectPlanner,
    MockProjectPlanner,
    ProjectPlanDraft,
    ProjectPlanSource,
    ProjectWorkflowPlanner,
)
from app.agents.plan_registry import get_plan_registry
from app.agents.service import PlanningService
from app.core.config import IntegrationMode, PlannerMode, PlannerProvider, Settings, get_settings
from app.integrations.config import load_integration_config
from app.integrations.contracts import NOTION_CREATE_PAGE, NOTION_PATCH_PAGE
from app.integrations.errors import IntegrationConfigError
from app.integrations.readiness import real_sandbox_issues, sandbox_parent_issues
from app.schemas.execution import ExecutionMode
from app.schemas.plan import IntegrationTarget, PlanResponse, PlanStatus, ValidationResult

router = APIRouter(prefix="/api/v1/workflows/notion-project", tags=["notion-project"])

_HTTP_STATUS = {PlanStatus.PLANNED: 200, PlanStatus.INVALID: 422, PlanStatus.FAILED: 502}


class ProjectPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: str = Field(min_length=10, max_length=5_000,
                             description="Plain-English project requirements (no confidential data).")
    mode: ExecutionMode
    # Notion-only workflow: any other integration target is rejected (422).
    target: Literal["notion"] | None = None


class ProjectPlanResponse(PlanResponse):
    mode: ExecutionMode
    model: str | None  # AI model id (Claude or Gemini); None for the mock stub
    ai_generated: bool
    project: ProjectPlanDraft | None
    destination_alias: str | None  # sandbox parent alias (dry_run/real); never the page id
    page_count: int
    operation: str
    rollback: str
    real_write: bool


def unavailable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)


def project_source(settings: Settings, provider: PlannerProvider) -> ProjectPlanSource:
    """One AI provider's project planner (PlannerConfigError names the missing setting)."""
    if provider is PlannerProvider.GROQ:
        client = create_groq_client(settings)
        assert settings.groq_model
        return GroqProjectPlanner(client=client, model=settings.groq_model, max_tokens=settings.groq_max_tokens)
    if provider is PlannerProvider.GEMINI:
        client = create_gemini_client(settings)
        assert settings.gemini_model
        return GeminiProjectPlanner(client=client, model=settings.gemini_model,
                                    max_output_tokens=settings.gemini_max_output_tokens)
    client = create_anthropic_client(settings)
    assert settings.anthropic_model
    return ClaudeProjectPlanner(client=client, model=settings.anthropic_model, max_tokens=settings.anthropic_max_tokens)


def default_source(settings: Settings, mode: IntegrationMode) -> "ProjectPlanSource | FallbackPlanner":
    """The configured AI planner (a FallbackPlanner of project sources in auto mode); the mock
    stub only for non-real modes with PLANNER_MODE=mock."""
    try:
        if settings.planner_mode is PlannerMode.AUTO:
            return FallbackPlanner(build_auto_chain(settings, project_source))
        if settings.planner_mode is PlannerMode.CLAUDE:
            client = create_anthropic_client(settings)
            assert settings.anthropic_model
            return ClaudeProjectPlanner(client=client, model=settings.anthropic_model,
                                        max_tokens=settings.anthropic_max_tokens)
        if settings.planner_mode is PlannerMode.GEMINI:
            client = create_gemini_client(settings)
            assert settings.gemini_model
            return GeminiProjectPlanner(client=client, model=settings.gemini_model,
                                        max_output_tokens=settings.gemini_max_output_tokens)
    except PlannerConfigError as exc:
        raise unavailable(str(exc)) from exc
    if mode is IntegrationMode.REAL:
        raise unavailable("Real-mode project plans must come from an AI planner (Claude, Gemini or auto). "
                          f"{ai_planner_problem(settings)}")
    return MockProjectPlanner()


def get_planning_sleeper():
    """time.sleep in production; tests replace it (monkeypatch) so they never wait."""
    return time.sleep


def get_project_source_factory() -> Callable[[Settings, IntegrationMode], ProjectPlanSource]:
    """Tests override this to inject a fake Claude client."""
    return default_source


@router.post(
    "/plan",
    response_model=ProjectPlanResponse,
    responses={409: {"description": "Mode above the server ceiling"},
               422: {"model": ProjectPlanResponse, "description": "Plan rejected, or invalid body"},
               502: {"model": ProjectPlanResponse, "description": "Planner failed"},
               503: {"description": "Not configured for this mode"}},
)
def plan_notion_project(
    payload: ProjectPlanRequest,
    source_factory: Callable[[Settings, IntegrationMode], ProjectPlanSource] = Depends(get_project_source_factory),
) -> JSONResponse:
    settings = get_settings()
    mode = IntegrationMode(payload.mode)
    if mode not in allowed_modes(settings.integration_mode):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"This server allows modes up to '{settings.integration_mode.value}'.")
    if not settings.mock_execution_enabled:
        raise unavailable("Execution is disabled (MOCK_EXECUTION_ENABLED=false).")

    parent: str | None = None
    if mode is not IntegrationMode.MOCK:
        try:
            config = load_integration_config(settings)
        except IntegrationConfigError as exc:
            raise unavailable(str(exc)) from exc
        issues = real_sandbox_issues(settings, config) if mode is IntegrationMode.REAL \
            else sandbox_parent_issues(settings, config)
        if issues:
            raise unavailable(" ".join(issues))
        parent = settings.smoke_test_notion_parent

    source = source_factory(settings, mode)
    # Auto mode: one ProjectWorkflowPlanner per provider so each keeps ONLY its own output.
    sources = list(source.providers) if isinstance(source, FallbackPlanner) else [source]
    adapters = [ProjectWorkflowPlanner(s, parent) for s in sources]
    chain = FallbackPlanner(adapters) if len(adapters) > 1 else adapters[0]
    service = PlanningService(chain, max_steps=settings.max_workflow_steps,
                              max_planning_attempts=settings.max_planning_attempts, available_documents=None,
                              backoff=RetryPolicy.for_planning(settings), sleeper=get_planning_sleeper())
    result = service.plan(payload.instruction, target=IntegrationTarget.NOTION.value)
    get_plan_registry().record(result.workflow_id, IntegrationTarget.NOTION.value)
    # The adapter of the provider that produced the result (fallback or primary); never mixed.
    used = next((a for a in adapters if a.name == result.planner), adapters[0])
    planner = used

    if result.status is PlanStatus.PLANNED and planner.project_issues:
        result = result.model_copy(update={
            "status": PlanStatus.INVALID,
            "planned_steps": [],
            "validation": ValidationResult(is_valid=False, issues=planner.project_issues),
        })
    pages = len(result.planned_steps)
    operation, rollback = {
        IntegrationMode.MOCK: ("In-memory mock pages (no network)", "Mock compensation (in memory)"),
        IntegrationMode.DRY_RUN: (f"Build {pages} {NOTION_CREATE_PAGE.name} request(s) without sending them",
                                  "Offline only; nothing is sent"),
        IntegrationMode.REAL: (f"Notion MCP {NOTION_CREATE_PAGE.name} x{pages}: the project page under sandbox "
                               f"parent '{parent}', then each action item inside it",
                               f"If a step fails, every page created by this run is moved to trash "
                               f"({NOTION_PATCH_PAGE.name} in_trash=true), newest first. A successful run "
                               "keeps the pages."),
    }[mode]
    response = ProjectPlanResponse(
        **result.model_dump(),
        mode=mode.value,
        model=used.model,
        ai_generated=isinstance(sources[adapters.index(used)], AI_PROJECT_PLANNERS),
        project=planner.project,
        destination_alias=parent,
        page_count=pages,
        operation=operation,
        rollback=rollback,
        real_write=mode is IntegrationMode.REAL and result.status is PlanStatus.PLANNED,
    )
    return JSONResponse(status_code=_HTTP_STATUS[response.status], content=response.model_dump(mode="json"))
