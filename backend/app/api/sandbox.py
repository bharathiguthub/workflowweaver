"""Notion Sandbox Test (Phase 5.4): a deterministic, Notion-only plan for the UI.

POST /api/v1/workflows/notion-sandbox/plan   body {"mode": "mock" | "dry_run" | "real"}
    200  PLANNED  one step: notion.create_page with synthetic title/content under the
                  SMOKE_TEST_NOTION_PARENT sandbox alias (dry_run/real). Nothing is executed.
    409           mode exceeds the server's INTEGRATION_MODE
    503           execution disabled, or the sandbox is not configured for that mode
                  (detail names the setting, never its value)

The plan is built by the server, not by an LLM, so the reviewer sees exactly what will run.
It goes through the same validator as every plan, and it is executed only through the
normal approval-gated execution endpoints, which re-validate it and apply every real-mode
gate (REAL_WRITES_ENABLED, operator token, approved destinations). Needs no Google Drive
or Teams configuration.
"""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.agents.execution.modes import allowed_modes
from app.agents.plan_registry import get_plan_registry
from app.agents.validation import validate_draft
from app.core.config import IntegrationMode, get_settings
from app.integrations.config import load_integration_config
from app.integrations.contracts import NOTION_CREATE_PAGE, NOTION_PATCH_PAGE
from app.integrations.errors import IntegrationConfigError
from app.integrations.readiness import real_sandbox_issues, sandbox_parent_issues
from app.schemas.execution import ExecutionMode
from app.schemas.plan import (
    DraftPlan,
    DraftStep,
    IntegrationTarget,
    PlanResponse,
    PlanStatus,
    StepArgument,
    ValidationResult,
)

router = APIRouter(prefix="/api/v1/workflows/notion-sandbox", tags=["notion-sandbox"])

PLANNER_NAME = "notion-sandbox"


class SandboxPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode
    # Notion-only workflow: any other integration target is rejected (422).
    target: Literal["notion"] | None = None


class SandboxPlanResponse(PlanResponse):
    mode: ExecutionMode
    destination_alias: str | None  # sandbox parent alias (dry_run/real); never the page id
    operation: str  # what the executor will do for the step
    rollback: str  # what happens if the run fails after the page was created
    real_write: bool  # True only in real mode: approving creates a real Notion page


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)


@router.post("/plan", response_model=SandboxPlanResponse)
def plan_notion_sandbox(payload: SandboxPlanRequest) -> SandboxPlanResponse:
    settings = get_settings()
    mode = IntegrationMode(payload.mode)
    if mode not in allowed_modes(settings.integration_mode):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"This server allows modes up to '{settings.integration_mode.value}'.")
    if not settings.mock_execution_enabled:
        raise _unavailable("Execution is disabled (MOCK_EXECUTION_ENABLED=false).")

    parent: str | None = None
    if mode is not IntegrationMode.MOCK:
        try:
            config = load_integration_config(settings)
        except IntegrationConfigError as exc:
            raise _unavailable(str(exc)) from exc
        issues = real_sandbox_issues(settings, config) if mode is IntegrationMode.REAL \
            else sandbox_parent_issues(settings, config)
        if issues:
            raise _unavailable(" ".join(issues))
        parent = settings.smoke_test_notion_parent

    workflow_id = uuid4()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    title = f"WorkflowWeaver sandbox test {stamp}"
    content = (
        "Synthetic test page created by the WorkflowWeaver UI Notion Sandbox Test "
        f"(workflow {workflow_id}, mode {mode.value}). It contains no real data and can be deleted."
    )
    arguments = [StepArgument(name="title", value=title), StepArgument(name="content", value=content)]
    if parent:
        arguments.append(StepArgument(name="parent", value=parent))
    draft = DraftPlan(
        summary="Notion Sandbox Test: create one synthetic page in the Notion sandbox.",
        steps=[DraftStep(step_number=1, integration="notion", action="create_page",
                         description="Create one synthetic page under the Notion sandbox parent",
                         arguments=arguments, depends_on=[])],
    )
    plan, issues = validate_draft(draft, max_steps=settings.max_workflow_steps)
    if plan is None:  # pragma: no cover - the plan is fixed; guards against catalog drift
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Sandbox plan failed validation: {[i.code for i in issues]}")

    operation, rollback = {
        IntegrationMode.MOCK: ("In-memory mock create_page (no network)", "Mock compensation (in memory)"),
        IntegrationMode.DRY_RUN: (f"Build the {NOTION_CREATE_PAGE.name} request without sending it",
                                  "Offline only; nothing is sent"),
        IntegrationMode.REAL: (f"Notion MCP {NOTION_CREATE_PAGE.name} under sandbox parent '{parent}'",
                               f"Only if the run fails: {NOTION_PATCH_PAGE.name} in_trash=true. "
                               "A successful run keeps the page; delete it manually."),
    }[mode]
    get_plan_registry().record(workflow_id, IntegrationTarget.NOTION.value)
    return SandboxPlanResponse(
        integration_target=IntegrationTarget.NOTION,
        workflow_id=workflow_id,
        status=PlanStatus.PLANNED,
        planner=PLANNER_NAME,
        summary=plan.summary,
        planned_steps=plan.steps,
        validation=ValidationResult(is_valid=True, issues=[]),
        errors=[],
        planning_attempts=1,
        mode=mode.value,
        destination_alias=parent,
        operation=operation,
        rollback=rollback,
        real_write=mode is IntegrationMode.REAL,
    )
