"""Read-only integration status (Phase 5.1) and Notion sandbox readiness (Phase 5.4).

GET /api/v1/integrations/status
    200 {mode, available_modes, real_writes_enabled, write_authorization_configured,
         configuration_valid, configuration_error, integrations: {notion|google_drive|teams},
         notion_sandbox: {parent_alias, dry_run_ready, real_ready, issues}}
    Opens no network connection.

POST /api/v1/integrations/notion/connectivity
    READ-ONLY probe of the Notion MCP server: initialize + tools/list (contract check) and
    API-retrieve-a-page on the SMOKE_TEST_NOTION_PARENT sandbox page. Never writes.
    200 {status: PASS | FAIL | NOT_AVAILABLE, checks, issues, checked_at}
    NOT_AVAILABLE (no network call made) unless INTEGRATION_MODE=real and the configuration
    is ready for a real sandbox test.

Reports only modes, booleans, setting names and destination ALIASES. Never returns MCP URLs,
destination ids, webhook URLs, tokens or tool arguments.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, model_serializer

from app.agents.execution.modes import allowed_modes
from app.agents.planners.factory import ai_planner_model, ai_planner_problem, fallback_problem, provider_model
from app.core.config import IntegrationMode, PlannerMode, get_settings
from app.integrations.config import DestinationConfig, load_integration_config
from app.integrations.errors import IntegrationConfigError
from app.integrations.readiness import (
    drive_sandbox_issues,
    drive_token_present,
    probe_google_drive,
    probe_notion,
    real_drive_issues,
    real_sandbox_issues,
    real_teams_issues,
    sandbox_parent_issues,
    teams_sandbox_issues,
)

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

ModeName = Literal["mock", "dry_run", "real"]


class DestinationStatus(BaseModel):
    aliases: list[str]
    default: str | None


class IntegrationStatus(BaseModel):
    transport: Literal["mcp", "rest", "webhook", "mock"]
    configured: bool  # real mode can use it (server URL / webhooks present)
    destinations: dict[str, DestinationStatus]


class NotionSandboxStatus(BaseModel):
    parent_alias: str | None = None  # SMOKE_TEST_NOTION_PARENT (an alias, never an id)
    dry_run_ready: bool = False
    real_ready: bool = False  # configuration only; the UI also requires a passing connectivity probe
    issues: list[str] = []  # setting names, never values


class DriveSandboxStatus(BaseModel):
    """Phase 6.1 Google Drive sandbox (REST + OAuth drive.file). Never returns ids or tokens."""

    transport: Literal["mcp", "rest", "mock"] = "mock"
    folder_alias: str | None = None  # SMOKE_TEST_DRIVE_FOLDER (an alias, never the folder id)
    oauth_client_configured: bool = False
    authorized: bool = False  # the token file exists (contents not read)
    scope: str | None = None  # the ONLY scope ever requested
    sandbox_only: bool = True
    dry_run_ready: bool = False
    real_ready: bool = False  # configuration only; the UI also requires a passing connectivity probe
    issues: list[str] = []  # setting names, never values


class TeamsSandboxStatus(BaseModel):
    """Phase 7.1 Teams Only workflow (Workflows webhook). Never returns webhook URLs."""
    transport: Literal["webhook", "mock"] = "mock"
    channel_alias: str | None = None  # SMOKE_TEST_TEAMS_CHANNEL (an alias, never the URL)
    sandbox_only: bool = True
    mock_ready: bool = False  # mock delivery (no network) is available
    dry_run_ready: bool = False  # the request can be built and previewed (nothing is sent)
    real_ready: bool = False  # configuration only: a webhook cannot be probed without posting
    issues: list[str] = []  # setting names, never values


class InstructionsOnlyStatus(BaseModel):
    """The same workflow without a source document (Notion + Teams only; Google Drive not needed)."""
    mock_ready: bool = False
    dry_run_ready: bool = False
    real_ready: bool = False  # configuration only; the UI also requires the Notion check
    issues: list[str] = []


class OrchestrationStatus(BaseModel):
    """Phase 7.2 Google Drive + Notion + Teams workflow readiness per mode (setting names only).
    The top-level fields are for plans WITH a source document; ``instructions_only`` for plans
    made from instructions alone."""
    mock_ready: bool = False
    dry_run_ready: bool = False
    real_ready: bool = False  # configuration only; the UI also requires the Notion and Drive checks
    issues: list[str] = []
    instructions_only: InstructionsOnlyStatus = InstructionsOnlyStatus()


class PlannerStatus(BaseModel):
    mode: Literal["mock", "claude", "gemini", "auto"]
    model: str | None  # model id of the selected (auto: primary) AI planner (not a secret)
    ai_ready: bool  # the selected AI planner (auto: the primary) has its model and API key
    claude_ready: bool  # kept for older clients: ai_ready and mode == "claude"
    issue: str | None = None  # names the setting, never its value
    # PLANNER_MODE=auto only (None otherwise)
    primary: str | None = None  # e.g. "gemini"
    fallback: str | None = None  # e.g. "groq"; None when FALLBACK_PLANNER is empty
    fallback_model: str | None = None
    fallback_ready: bool = False
    fallback_issue: str | None = None  # names the setting, never its value

    @model_serializer(mode="wrap")
    def _auto_fields_only_in_auto_mode(self, handler):
        """Keep the response unchanged for mock/claude/gemini clients: the fallback fields
        are emitted only when PLANNER_MODE=auto."""
        data = handler(self)
        if self.mode != "auto":
            for key in _AUTO_ONLY:
                data.pop(key, None)
        return data


_AUTO_ONLY = ("primary", "fallback", "fallback_model", "fallback_ready", "fallback_issue")


def planner_status(settings) -> PlannerStatus:
    problem = ai_planner_problem(settings)
    status = PlannerStatus(mode=settings.planner_mode.value, model=ai_planner_model(settings),
                           ai_ready=problem is None,
                           claude_ready=problem is None and settings.planner_mode.value == "claude", issue=problem)
    if settings.planner_mode is PlannerMode.AUTO:
        fallback = settings.fallback_planner
        fb_problem = fallback_problem(settings)
        status = status.model_copy(update={
            "primary": settings.primary_planner.value,
            "fallback": fallback.value if fallback else None,
            "fallback_model": provider_model(settings, fallback) if fallback else None,
            "fallback_ready": fallback is not None and fb_problem is None,
            "fallback_issue": fb_problem,
        })
    return status


class IntegrationsStatusResponse(BaseModel):
    mode: ModeName  # the server's ceiling (INTEGRATION_MODE)
    available_modes: list[ModeName]  # modes a request may select (at or below the ceiling)
    real_writes_enabled: bool
    write_authorization_configured: bool
    configuration_valid: bool
    configuration_error: str | None = None  # names the setting, never its value
    integrations: dict[str, IntegrationStatus]
    notion_sandbox: NotionSandboxStatus
    google_drive_sandbox: DriveSandboxStatus = DriveSandboxStatus()
    teams_sandbox: TeamsSandboxStatus = TeamsSandboxStatus()
    orchestration: OrchestrationStatus = OrchestrationStatus()
    planner: PlannerStatus


class ConnectivityCheck(BaseModel):
    name: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    detail: str = ""


class NotionConnectivityResponse(BaseModel):
    status: Literal["PASS", "FAIL", "NOT_AVAILABLE"]
    checks: list[ConnectivityCheck]
    issues: list[str]
    checked_at: datetime


class DriveConnectivityResponse(NotionConnectivityResponse):
    pass


def drive_sandbox_status(settings, config) -> DriveSandboxStatus:
    from app.integrations.google.oauth import DRIVE_FILE_SCOPE

    dry = drive_sandbox_issues(settings, config)
    real = real_drive_issues(settings, config)
    return DriveSandboxStatus(
        transport=config.drive_transport.value,
        folder_alias=settings.smoke_test_drive_folder if not dry else None,
        oauth_client_configured=config.drive_rest is not None,
        authorized=drive_token_present(config),
        scope=DRIVE_FILE_SCOPE,
        sandbox_only=settings.google_drive_real_writes_sandbox_only,
        dry_run_ready=not dry and settings.mock_execution_enabled,
        real_ready=not real and settings.mock_execution_enabled,
        issues=real,
    )


def teams_sandbox_status(settings, config) -> TeamsSandboxStatus:
    dry = teams_sandbox_issues(settings, config)
    real = real_teams_issues(settings, config)
    enabled = settings.mock_execution_enabled
    return TeamsSandboxStatus(
        transport="webhook",
        channel_alias=settings.smoke_test_teams_channel if not dry else None,
        sandbox_only=settings.teams_real_writes_sandbox_only,
        mock_ready=enabled,
        dry_run_ready=not dry and enabled,
        real_ready=not real and enabled,
        issues=real if settings.integration_mode is IntegrationMode.REAL else dry,
    )


def orchestration_status(settings, config) -> OrchestrationStatus:
    from app.api.orchestration import orchestration_issues_for

    enabled = settings.mock_execution_enabled
    planner = ai_planner_problem(settings)

    def readiness(needs_drive: bool) -> tuple[list[str], list[str]]:
        dry = orchestration_issues_for(settings, config, IntegrationMode.DRY_RUN, needs_drive=needs_drive)
        real = orchestration_issues_for(settings, config, IntegrationMode.REAL, needs_drive=needs_drive)
        if planner:
            real = [*real, f"Real orchestration plans need an AI planner: {planner}"]
        return dry, real

    current = (lambda dry, real: real if settings.integration_mode is IntegrationMode.REAL else dry)
    dry, real = readiness(True)
    i_dry, i_real = readiness(False)
    return OrchestrationStatus(
        mock_ready=enabled, dry_run_ready=not dry and enabled, real_ready=not real and enabled, issues=current(dry, real),
        instructions_only=InstructionsOnlyStatus(mock_ready=enabled, dry_run_ready=not i_dry and enabled,
                                                 real_ready=not i_real and enabled, issues=current(i_dry, i_real)))


def _dest(config: DestinationConfig) -> DestinationStatus:
    return DestinationStatus(aliases=config.aliases, default=config.default_alias)


@router.get("/status", response_model=IntegrationsStatusResponse)
def integration_status() -> IntegrationsStatusResponse:
    settings = get_settings()
    token = settings.real_write_authorization_token
    modes = [m.value for m in allowed_modes(settings.integration_mode)] if settings.mock_execution_enabled else []
    base = {
        "mode": settings.integration_mode.value,
        "available_modes": modes,
        "real_writes_enabled": settings.real_writes_enabled,
        "write_authorization_configured": bool(token and token.get_secret_value()),
        "planner": planner_status(settings),
    }
    if settings.integration_mode is IntegrationMode.MOCK:
        mock = IntegrationStatus(transport="mock", configured=True, destinations={})
        return IntegrationsStatusResponse(
            **base, configuration_valid=True,
            integrations={"notion": mock, "google_drive": mock, "teams": mock},
            notion_sandbox=NotionSandboxStatus(
                issues=["INTEGRATION_MODE is 'mock'; set INTEGRATION_MODE=dry_run or real to test Notion."]),
            google_drive_sandbox=DriveSandboxStatus(
                issues=["INTEGRATION_MODE is 'mock'; set INTEGRATION_MODE=dry_run or real to test Google Drive."]),
            # Mock delivery needs no webhook configuration and makes no network call.
            teams_sandbox=TeamsSandboxStatus(
                transport="mock", mock_ready=settings.mock_execution_enabled,
                issues=["INTEGRATION_MODE is 'mock'; set INTEGRATION_MODE=dry_run or real to use a Teams webhook."]),
            orchestration=OrchestrationStatus(
                mock_ready=settings.mock_execution_enabled,
                instructions_only=InstructionsOnlyStatus(mock_ready=settings.mock_execution_enabled)),
        )
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        # Only mock (which needs no integration configuration) remains usable.
        return IntegrationsStatusResponse(
            **{**base, "available_modes": modes[:1]}, configuration_valid=False, configuration_error=str(exc),
            integrations={}, notion_sandbox=NotionSandboxStatus(issues=[str(exc)]),
            google_drive_sandbox=DriveSandboxStatus(issues=[str(exc)]),
            teams_sandbox=TeamsSandboxStatus(mock_ready=settings.mock_execution_enabled, issues=[str(exc)]),
            orchestration=OrchestrationStatus(
                mock_ready=settings.mock_execution_enabled, issues=[str(exc)],
                instructions_only=InstructionsOnlyStatus(mock_ready=settings.mock_execution_enabled, issues=[str(exc)])),
        )
    dry_issues = sandbox_parent_issues(settings, config)
    real_issues = real_sandbox_issues(settings, config)
    return IntegrationsStatusResponse(
        **base,
        configuration_valid=True,
        integrations={
            "notion": IntegrationStatus(
                transport="mcp", configured=config.notion_mcp is not None,
                destinations={"pages": _dest(config.notion_pages), "tasks": _dest(config.notion_task_database)},
            ),
            "google_drive": IntegrationStatus(
                transport=config.drive_transport.value,
                configured=(config.drive_rest is not None) if config.drive_transport.value == "rest"
                else config.drive_mcp is not None,
                destinations={"folders": _dest(config.drive_folders)},
            ),
            "teams": IntegrationStatus(
                transport="webhook", configured=bool(config.teams_channels.targets),
                destinations={"channels": _dest(config.teams_channels)},
            ),
        },
        notion_sandbox=NotionSandboxStatus(
            parent_alias=settings.smoke_test_notion_parent if not dry_issues else None,
            dry_run_ready=not dry_issues and settings.mock_execution_enabled,
            real_ready=not real_issues and settings.mock_execution_enabled,
            issues=real_issues,
        ),
        google_drive_sandbox=drive_sandbox_status(settings, config),
        teams_sandbox=teams_sandbox_status(settings, config),
        orchestration=orchestration_status(settings, config),
    )


def get_notion_transport_factory() -> Callable[[], Any] | None:
    """None: the real Streamable HTTP transport. Tests override this with a fake server."""
    return None


@router.post("/notion/connectivity", response_model=NotionConnectivityResponse)
def notion_connectivity(
    transport_factory: Callable[[], Any] | None = Depends(get_notion_transport_factory),
) -> NotionConnectivityResponse:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        return NotionConnectivityResponse(status="NOT_AVAILABLE", checks=[], issues=[str(exc)], checked_at=now)
    issues = real_sandbox_issues(settings, config)
    if not settings.mock_execution_enabled:
        issues.append("MOCK_EXECUTION_ENABLED is false (execution is disabled).")
    if issues:
        return NotionConnectivityResponse(status="NOT_AVAILABLE", checks=[], issues=issues, checked_at=now)
    result = probe_notion(settings, config, transport_factory)
    return NotionConnectivityResponse(
        status="PASS" if result.passed else "FAIL",
        checks=[ConnectivityCheck(**c) for c in result.checks],
        issues=[],
        checked_at=now,
    )


def get_drive_http() -> Any:
    """None: a real httpx.Client. Tests override this with an httpx.MockTransport client."""
    return None


@router.post("/google-drive/connectivity", response_model=DriveConnectivityResponse)
def google_drive_connectivity(http: Any = Depends(get_drive_http)) -> DriveConnectivityResponse:
    """READ-ONLY: token refresh, sandbox folder metadata + marker, list its children.
    NOT_AVAILABLE (no network call) unless real mode is fully configured for the Drive sandbox."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        return DriveConnectivityResponse(status="NOT_AVAILABLE", checks=[], issues=[str(exc)], checked_at=now)
    issues = real_drive_issues(settings, config)
    if not settings.mock_execution_enabled:
        issues.append("MOCK_EXECUTION_ENABLED is false (execution is disabled).")
    if issues:
        return DriveConnectivityResponse(status="NOT_AVAILABLE", checks=[], issues=issues, checked_at=now)
    result = probe_google_drive(settings, config, http)
    return DriveConnectivityResponse(
        status="PASS" if result.passed else "FAIL",
        checks=[ConnectivityCheck(**c) for c in result.checks],
        issues=[],
        checked_at=now,
    )
