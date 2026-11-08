"""Readiness of the Notion sandbox test (UI) - offline configuration checks and an
on-demand, READ-ONLY connectivity probe.

The Notion sandbox test writes exactly one synthetic page under SMOKE_TEST_NOTION_PARENT, the
same non-production sandbox alias the Phase 5.3 smoke harness uses. Problems are reported by
setting name only; values (URLs, ids, tokens) never leave this module.

The connectivity probe makes only read-only MCP calls: initialize + tools/list (contract
check) and API-retrieve-a-page on the sandbox parent. It never creates, updates or deletes.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import IntegrationMode, Settings
from app.integrations.config import IntegrationConfig
from app.integrations.contracts import CONTRACTS, NOTION_CREATE_PAGE, NOTION_PATCH_PAGE, NOTION_RETRIEVE_PAGE
from app.integrations.errors import IntegrationError
from app.integrations.mcp.client import McpClient
from app.schemas.workflow import Integration

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"

# create (the test), archive (Saga rollback if the run fails), retrieve (connectivity probe)
NOTION_SANDBOX_TOOLS = (NOTION_CREATE_PAGE.name, NOTION_PATCH_PAGE.name, NOTION_RETRIEVE_PAGE.name)


def sandbox_parent_issues(settings: Settings, config: IntegrationConfig) -> list[str]:
    """Problems that block a dry-run of the Notion sandbox test (setting names only)."""
    alias = settings.smoke_test_notion_parent
    if not alias:
        return ["SMOKE_TEST_NOTION_PARENT is not set (alias of a non-production sandbox page)."]
    if alias not in config.notion_pages.targets:
        return ["SMOKE_TEST_NOTION_PARENT must be an alias listed in NOTION_DESTINATIONS."]
    return []


def real_sandbox_issues(settings: Settings, config: IntegrationConfig) -> list[str]:
    """Problems that block a REAL Notion sandbox test, from configuration alone (no network)."""
    issues = sandbox_parent_issues(settings, config)
    if settings.integration_mode is not IntegrationMode.REAL:
        issues.append(f"INTEGRATION_MODE is '{settings.integration_mode.value}'; real mode needs INTEGRATION_MODE=real.")
    if config.notion_mcp is None:
        issues.append("NOTION_MCP_URL is not set.")
    missing = [t for t in NOTION_SANDBOX_TOOLS if t not in config.notion_allowlist]
    if missing:
        issues.append(f"NOTION_MCP_TOOL_ALLOWLIST is missing {', '.join(missing)}.")
    if not settings.real_writes_enabled:
        issues.append("REAL_WRITES_ENABLED is false (server write kill switch).")
    token = settings.real_write_authorization_token
    if not (token and token.get_secret_value()):
        issues.append("REAL_WRITE_AUTHORIZATION_TOKEN is not set (operator token required for real writes).")
    return issues


@dataclass
class ProbeResult:
    checks: list[dict[str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"name": name, "status": status, "detail": detail})

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c["status"] == PASS for c in self.checks)


def _notion_page(client: McpClient, page_id: str) -> dict[str, Any]:
    # Imported lazily: the adapter module imports the execution tool package.
    from app.agents.execution.tools.integrations import _notion_payload

    return _notion_payload(client.call_tool(NOTION_RETRIEVE_PAGE.name, {"page_id": page_id}))


def probe_notion(settings: Settings, config: IntegrationConfig,
                 transport_factory: Callable[[], Any] | None = None) -> ProbeResult:
    """Read-only connectivity probe. Callers must have checked real_sandbox_issues first."""
    result = ProbeResult()
    assert config.notion_mcp is not None and settings.smoke_test_notion_parent
    client = McpClient(config.notion_mcp, transport_factory, contracts=CONTRACTS[Integration.NOTION])
    try:
        try:
            offered = client.list_tools()
        except IntegrationError as exc:
            result.add("MCP initialize + tools/list", FAIL, exc.code)
            return result
        result.add("MCP initialize + tools/list", PASS, f"{len(offered)} allowlisted tool(s) offered")
        healthy = True
        for name in NOTION_SANDBOX_TOOLS:
            if name not in offered:
                result.add(f"contract {name}", FAIL, "not offered by the server")
                healthy = False
            elif name in client.contract_problems:
                result.add(f"contract {name}", FAIL, "schema differs from the verified contract")
                healthy = False
            else:
                result.add(f"contract {name}", PASS)
        if not healthy:
            result.add("retrieve sandbox parent page", SKIPPED, "contract check failed")
            return result
        try:
            page = _notion_page(client, config.notion_pages.targets[settings.smoke_test_notion_parent])
        except IntegrationError as exc:
            hint = " - is the sandbox page shared with the Notion integration?" if "404" in exc.message else ""
            result.add("retrieve sandbox parent page", FAIL, f"{exc.code}{hint}")
            return result
        if page.get("object") == "page" and not page.get("in_trash"):
            result.add("retrieve sandbox parent page", PASS,
                       f"'{settings.smoke_test_notion_parent}' is reachable and shared with the integration")
        else:
            result.add("retrieve sandbox parent page", FAIL, "not a live page (trashed or wrong object)")
        return result
    finally:
        client.close()


# ---------------- Google Drive sandbox (Phase 6.1, REST + OAuth drive.file) ----------------


def drive_sandbox_issues(settings: Settings, config: IntegrationConfig) -> list[str]:
    """Problems that block a dry run of the Drive sandbox test (setting names only)."""
    from app.core.config import DriveTransport

    issues: list[str] = []
    if config.drive_transport is not DriveTransport.REST:
        issues.append("GOOGLE_DRIVE_TRANSPORT is not 'rest' (the sandbox test needs the Drive REST integration).")
    alias = settings.smoke_test_drive_folder
    if not alias:
        issues.append("SMOKE_TEST_DRIVE_FOLDER is not set (alias of the WorkflowWeaver sandbox folder).")
    elif alias not in config.drive_folders.targets:
        issues.append("SMOKE_TEST_DRIVE_FOLDER must be an alias listed in GOOGLE_DRIVE_DESTINATIONS.")
    return issues


def teams_sandbox_issues(settings: Settings, config: IntegrationConfig) -> list[str]:
    """Problems that block a Teams Only dry run (setting names only, never webhook URLs)."""
    alias = settings.smoke_test_teams_channel
    if not config.teams_channels.targets:
        return ["TEAMS_WEBHOOK_URLS is not set (JSON map of channel alias -> Teams Workflows webhook URL)."]
    if not alias:
        return ["SMOKE_TEST_TEAMS_CHANNEL is not set (alias of the sandbox Teams channel webhook)."]
    if alias not in config.teams_channels.targets:
        return ["SMOKE_TEST_TEAMS_CHANNEL must be an alias listed in TEAMS_WEBHOOK_URLS."]
    return []


def real_teams_issues(settings: Settings, config: IntegrationConfig) -> list[str]:
    """Problems that block a REAL Teams Only run, from configuration alone. There is no
    read-only probe for a Workflows webhook (any POST triggers the flow), so nothing is called."""
    issues = teams_sandbox_issues(settings, config)
    if settings.integration_mode is not IntegrationMode.REAL:
        issues.append(f"INTEGRATION_MODE is '{settings.integration_mode.value}'; real mode needs INTEGRATION_MODE=real.")
    if not settings.teams_real_writes_sandbox_only:
        issues.append("TEAMS_REAL_WRITES_SANDBOX_ONLY is false; the Teams Only workflow requires sandbox confinement.")
    if not settings.real_writes_enabled:
        issues.append("REAL_WRITES_ENABLED is false (server write kill switch).")
    token = settings.real_write_authorization_token
    if not (token and token.get_secret_value()):
        issues.append("REAL_WRITE_AUTHORIZATION_TOKEN is not set (operator token required for real writes).")
    return issues


def drive_token_present(config: IntegrationConfig) -> bool:
    """True if the OAuth token file exists (its contents are not read here)."""
    return config.drive_rest is not None and config.drive_rest.token_path.is_file()


def real_drive_issues(settings: Settings, config: IntegrationConfig) -> list[str]:
    """Problems that block a REAL Drive sandbox test, from configuration alone (no network)."""
    issues = drive_sandbox_issues(settings, config)
    if settings.integration_mode is not IntegrationMode.REAL:
        issues.append(f"INTEGRATION_MODE is '{settings.integration_mode.value}'; real mode needs INTEGRATION_MODE=real.")
    if config.drive_rest is None:
        issues.append("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set (Desktop app OAuth client).")
    elif not drive_token_present(config):
        issues.append("Google Drive is not authorized: run scripts/google_drive_auth.py (GOOGLE_DRIVE_TOKEN_FILE missing).")
    if not settings.google_drive_real_writes_sandbox_only:
        issues.append("GOOGLE_DRIVE_REAL_WRITES_SANDBOX_ONLY is false; the UI sandbox test requires sandbox confinement.")
    if not settings.real_writes_enabled:
        issues.append("REAL_WRITES_ENABLED is false (server write kill switch).")
    token = settings.real_write_authorization_token
    if not (token and token.get_secret_value()):
        issues.append("REAL_WRITE_AUTHORIZATION_TOKEN is not set (operator token required for real writes).")
    return issues


def probe_google_drive(settings: Settings, config: IntegrationConfig, http: Any = None) -> ProbeResult:
    """READ-ONLY probe: refresh an access token (scope must be exactly drive.file), read the
    sandbox folder's metadata (must carry the WorkflowWeaver sandbox marker) and list its
    children. Never creates, updates or trashes. Callers must check real_drive_issues first."""
    from app.agents.execution.tools.integrations import _drive_client
    from app.integrations.google.drive import FOLDER_MIME, SANDBOX_PROPERTY

    result = ProbeResult()
    assert config.drive_rest is not None and settings.smoke_test_drive_folder
    client = _drive_client(config, http)
    try:
        try:
            client._tokens.access_token()
        except IntegrationError as exc:
            result.add("OAuth token refresh (drive.file)", FAIL, exc.code)
            return result
        result.add("OAuth token refresh (drive.file)", PASS, "access token issued; scope is drive.file only")
        try:
            meta = client.get(config.drive_folders.targets[settings.smoke_test_drive_folder])
        except IntegrationError as exc:
            hint = " - create it with scripts/google_drive_sandbox.py create-folder" \
                if exc.code in {"GOOGLE_DRIVE_NOT_FOUND", "GOOGLE_DRIVE_PERMISSION_DENIED"} else ""
            result.add("read sandbox folder", FAIL, f"{exc.code}{hint}")
            return result
        marker = (meta.get("appProperties") or {}).get(SANDBOX_PROPERTY)
        if meta.get("mimeType") != FOLDER_MIME or meta.get("trashed") or marker != "true":
            result.add("read sandbox folder", FAIL, "not a live WorkflowWeaver sandbox folder (marker missing or trashed)")
            return result
        result.add("read sandbox folder", PASS, f"'{settings.smoke_test_drive_folder}' is the WorkflowWeaver sandbox")
        try:
            files = client.list_children(meta["id"])
        except IntegrationError as exc:
            result.add("list sandbox files", FAIL, exc.code)
            return result
        result.add("list sandbox files", PASS, f"{len(files)} item(s) visible to this app")
        return result
    finally:
        client.close()
