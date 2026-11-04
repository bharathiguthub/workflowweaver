"""Typed, validated view of the server-side integration settings.

Everything here comes from trusted server configuration (environment / .env). Nothing in
an API request can supply or override a URL, credential, tool name or destination.
Configured secret values are registered with ``app.integrations.secrets`` so they are
redacted from logs and traces even if some library prints them.
"""

import ipaddress
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.errors import IntegrationConfigError
from app.integrations.secrets import register_secret
from app.schemas.workflow import Integration

ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_LOOPBACK_HOSTS = {"localhost"}


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint_url(url: str, setting: str, *, allow_loopback_http: bool) -> str:
    """https only (http allowed for a loopback MCP server during local development).
    No credentials in the URL userinfo. Errors name the setting, never the value."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise IntegrationConfigError(f"{setting} is not a valid URL.") from exc
    host = (parts.hostname or "").lower()
    if not host:
        raise IntegrationConfigError(f"{setting} must be an absolute URL.")
    if parts.username or parts.password:
        raise IntegrationConfigError(f"{setting} must not embed credentials; use the *_AUTH_TOKEN setting.")
    if parts.scheme == "https":
        return url.strip()
    if parts.scheme == "http" and allow_loopback_http and _is_loopback(host):
        return url.strip()
    raise IntegrationConfigError(f"{setting} must use https (http is allowed only for a loopback MCP server).")


def parse_alias_map(raw: str | None, setting: str) -> dict[str, str]:
    """JSON object of alias -> target. Aliases are lower-case slugs, targets non-empty."""
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntegrationConfigError(f"{setting} must be a JSON object of alias -> value.") from exc
    if not isinstance(data, dict):
        raise IntegrationConfigError(f"{setting} must be a JSON object of alias -> value.")
    result: dict[str, str] = {}
    for alias, target in data.items():
        if not isinstance(alias, str) or not ALIAS_PATTERN.match(alias):
            raise IntegrationConfigError(f"{setting}: aliases must match {ALIAS_PATTERN.pattern}.")
        if not isinstance(target, str) or not target.strip():
            raise IntegrationConfigError(f"{setting}: every alias needs a non-empty string value.")
        result[alias] = target.strip()
    return result


def parse_allowlist(raw: str) -> frozenset[str]:
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def _secret(value: SecretStr | None) -> str | None:
    if value is None:
        return None
    text = value.get_secret_value().strip()
    return text or None


@dataclass(frozen=True)
class McpServerConfig:
    integration: Integration
    url: str = field(repr=False)
    auth_token: str | None = field(default=None, repr=False)
    tool_allowlist: frozenset[str] = frozenset()
    connect_timeout_seconds: float = 5.0
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class DestinationConfig:
    """Approved destinations: alias -> target id/URL. ``targets`` may be secret (Teams)."""

    integration: Integration
    targets: dict[str, str] = field(default_factory=dict, repr=False)
    default_alias: str | None = None

    @property
    def aliases(self) -> list[str]:
        return sorted(self.targets)


@dataclass(frozen=True)
class NotionToolNames:
    create_page: str
    archive_page: str


@dataclass(frozen=True)
class DriveToolNames:
    create_document: str
    search_documents: str
    read_document: str
    trash_file: str


@dataclass(frozen=True)
class IntegrationConfig:
    notion_mcp: McpServerConfig | None
    notion_tools: NotionToolNames
    notion_pages: DestinationConfig
    notion_task_database: DestinationConfig
    drive_mcp: McpServerConfig | None
    drive_tools: DriveToolNames
    drive_folders: DestinationConfig
    teams_channels: DestinationConfig
    connect_timeout_seconds: float
    timeout_seconds: float


def _default_alias(alias: str | None, targets: dict[str, str], setting: str) -> str | None:
    if alias is None or not alias.strip():
        return None
    alias = alias.strip()
    if alias not in targets:
        raise IntegrationConfigError(f"{setting} must be one of the configured destination aliases.")
    return alias


def _mcp(settings: Settings, integration: Integration, url: str | None, token: SecretStr | None,
         allowlist: str, prefix: str) -> McpServerConfig | None:
    if not url or not url.strip():
        return None
    checked = validate_endpoint_url(url, f"{prefix}_MCP_URL", allow_loopback_http=True)
    tools = parse_allowlist(allowlist)
    if not tools:
        raise IntegrationConfigError(f"{prefix}_MCP_TOOL_ALLOWLIST must name at least one tool.")
    auth = _secret(token)
    register_secret(checked)
    register_secret(auth)
    return McpServerConfig(
        integration=integration,
        url=checked,
        auth_token=auth,
        tool_allowlist=tools,
        connect_timeout_seconds=settings.integration_connect_timeout_seconds,
        timeout_seconds=settings.integration_timeout_seconds,
    )


def _require_allowlisted(tools: list[str], allowlist: frozenset[str] | None, setting: str) -> None:
    if allowlist is None:
        return
    missing = [t for t in tools if t not in allowlist]
    if missing:
        raise IntegrationConfigError(f"{setting} must include every configured tool name for this server.")


def load_integration_config(settings: Settings) -> IntegrationConfig:
    """Validate and normalise integration settings. Raises IntegrationConfigError.

    Never opens a connection."""
    notion_targets = parse_alias_map(settings.notion_destinations, "NOTION_DESTINATIONS")
    drive_targets = parse_alias_map(settings.google_drive_destinations, "GOOGLE_DRIVE_DESTINATIONS")
    teams_targets = parse_alias_map(_secret(settings.teams_webhook_urls), "TEAMS_WEBHOOK_URLS")
    for alias, url in teams_targets.items():
        teams_targets[alias] = validate_endpoint_url(url, "TEAMS_WEBHOOK_URLS", allow_loopback_http=False)
        register_secret(teams_targets[alias])
    for target in (*notion_targets.values(), *drive_targets.values()):
        register_secret(target)  # resource ids are not credentials, but keep them out of logs/traces too

    notion_mcp = _mcp(settings, Integration.NOTION, settings.notion_mcp_url, settings.notion_mcp_auth_token,
                      settings.notion_mcp_tool_allowlist, "NOTION")
    drive_mcp = _mcp(settings, Integration.GOOGLE_DRIVE, settings.google_drive_mcp_url,
                     settings.google_drive_mcp_auth_token, settings.google_drive_mcp_tool_allowlist, "GOOGLE_DRIVE")

    notion_tools = NotionToolNames(settings.notion_tool_create_page, settings.notion_tool_archive_page)
    drive_tools = DriveToolNames(
        settings.google_drive_tool_create_document,
        settings.google_drive_tool_search_documents,
        settings.google_drive_tool_read_document,
        settings.google_drive_tool_trash_file,
    )
    _require_allowlisted([notion_tools.create_page, notion_tools.archive_page],
                         notion_mcp.tool_allowlist if notion_mcp else None, "NOTION_MCP_TOOL_ALLOWLIST")
    _require_allowlisted(list(vars(drive_tools).values()),
                         drive_mcp.tool_allowlist if drive_mcp else None, "GOOGLE_DRIVE_MCP_TOOL_ALLOWLIST")

    task_alias = _default_alias(settings.notion_task_database, notion_targets, "NOTION_TASK_DATABASE")
    return IntegrationConfig(
        notion_mcp=notion_mcp,
        notion_tools=notion_tools,
        notion_pages=DestinationConfig(
            Integration.NOTION, notion_targets,
            _default_alias(settings.notion_default_page_parent, notion_targets, "NOTION_DEFAULT_PAGE_PARENT"),
        ),
        # Tasks always go to the one configured database; plans cannot pick another.
        notion_task_database=DestinationConfig(
            Integration.NOTION, {task_alias: notion_targets[task_alias]} if task_alias else {}, task_alias
        ),
        drive_mcp=drive_mcp,
        drive_tools=drive_tools,
        drive_folders=DestinationConfig(
            Integration.GOOGLE_DRIVE, drive_targets,
            _default_alias(settings.google_drive_default_folder, drive_targets, "GOOGLE_DRIVE_DEFAULT_FOLDER"),
        ),
        teams_channels=DestinationConfig(
            Integration.TEAMS, teams_targets,
            _default_alias(settings.teams_default_channel, teams_targets, "TEAMS_DEFAULT_CHANNEL"),
        ),
        connect_timeout_seconds=settings.integration_connect_timeout_seconds,
        timeout_seconds=settings.integration_timeout_seconds,
    )
