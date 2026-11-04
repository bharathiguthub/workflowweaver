"""Read-only integration status (Phase 5.1).

GET /api/v1/integrations/status
    200 {mode, real_writes_enabled, write_authorization_configured, configuration_valid,
         configuration_error, integrations: {notion|google_drive|teams: {...}}}

Reports only the server mode, booleans and destination ALIASES. Never returns MCP URLs,
destination ids, webhook URLs, tokens or tool arguments. Opens no network connection.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import IntegrationMode, get_settings
from app.integrations.config import DestinationConfig, load_integration_config
from app.integrations.errors import IntegrationConfigError

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])


class DestinationStatus(BaseModel):
    aliases: list[str]
    default: str | None


class IntegrationStatus(BaseModel):
    transport: Literal["mcp", "webhook", "mock"]
    configured: bool  # real mode can use it (server URL / webhooks present)
    destinations: dict[str, DestinationStatus]


class IntegrationsStatusResponse(BaseModel):
    mode: Literal["mock", "dry_run", "real"]
    real_writes_enabled: bool
    write_authorization_configured: bool
    configuration_valid: bool
    configuration_error: str | None = None  # names the setting, never its value
    integrations: dict[str, IntegrationStatus]


def _dest(config: DestinationConfig) -> DestinationStatus:
    return DestinationStatus(aliases=config.aliases, default=config.default_alias)


@router.get("/status", response_model=IntegrationsStatusResponse)
def integration_status() -> IntegrationsStatusResponse:
    settings = get_settings()
    token = settings.real_write_authorization_token
    base = {
        "mode": settings.integration_mode.value,
        "real_writes_enabled": settings.real_writes_enabled,
        "write_authorization_configured": bool(token and token.get_secret_value()),
    }
    if settings.integration_mode is IntegrationMode.MOCK:
        mock = IntegrationStatus(transport="mock", configured=True, destinations={})
        return IntegrationsStatusResponse(
            **base, configuration_valid=True,
            integrations={"notion": mock, "google_drive": mock, "teams": mock},
        )
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        return IntegrationsStatusResponse(**base, configuration_valid=False, configuration_error=str(exc),
                                          integrations={})
    return IntegrationsStatusResponse(
        **base,
        configuration_valid=True,
        integrations={
            "notion": IntegrationStatus(
                transport="mcp", configured=config.notion_mcp is not None,
                destinations={"pages": _dest(config.notion_pages), "tasks": _dest(config.notion_task_database)},
            ),
            "google_drive": IntegrationStatus(
                transport="mcp", configured=config.drive_mcp is not None,
                destinations={"folders": _dest(config.drive_folders)},
            ),
            "teams": IntegrationStatus(
                transport="webhook", configured=bool(config.teams_channels.targets),
                destinations={"channels": _dest(config.teams_channels)},
            ),
        },
    )
