"""Phase 5.3: controlled real-integration smoke tests.

    python -m scripts.smoke_integrations                     # 1. preflight checklist - OFFLINE
    python -m scripts.smoke_integrations --read              # 2. read-only connectivity checks
    python -m scripts.smoke_integrations --write notion --i-approve-real-writes   # 3. one real write

Phases
  preflight  Configuration checklist per integration. No network. Reports presence/validity
             only - never values. Items that cannot be checked from configuration (sharing,
             Developer Preview membership, sandbox status) are listed as MANUAL.
  --read     Read-only calls only: MCP initialize + tools/list + contract check, Notion
             API-retrieve-a-page on the sandbox parent, Drive get_file_metadata + search_files
             on the sandbox folder. Teams has no read-only operation and is NOT_VERIFIABLE.
  --write    Requires --i-approve-real-writes, INTEGRATION_MODE=real, REAL_WRITES_ENABLED=true,
             passing read checks, and a SMOKE_TEST_* sandbox alias. Writes ONE synthetic item
             to the sandbox destination through the production adapter, then READS IT BACK to
             verify the remote result, then rolls back where the integration supports it and
             verifies the rollback. Unsupported rollback is reported, never claimed.

Output: statuses, aliases, tool names and error codes. Never credentials, server or webhook
URLs, destination ids, or document contents. A created item's own link is printed only when
it must be cleaned up by hand.
"""

import argparse
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.agents.execution.tools.base import CompensationCall, ToolCall, ToolError
from app.agents.execution.tools.integrations import _notion_payload, build_integration_registry
from app.core.config import IntegrationMode, Settings, get_settings
from app.integrations.config import IntegrationConfig, load_integration_config
from app.integrations.contracts import (
    CONTRACTS,
    DRIVE_GET_FILE_METADATA,
    DRIVE_READ_FILE_CONTENT,
    DRIVE_SEARCH_FILES,
    NOTION_RETRIEVE_PAGE,
)
from app.integrations.errors import IntegrationConfigError, IntegrationError
from app.integrations.mcp.client import McpClient
from app.schemas.plan import Action
from app.schemas.workflow import Integration

# Statuses
OK, MISSING, INVALID, MANUAL = "OK", "MISSING", "INVALID", "MANUAL"
PASS, FAIL, SKIPPED, BLOCKED = "PASS", "FAIL", "SKIPPED", "BLOCKED"
NOT_VERIFIABLE, NOT_SUPPORTED = "NOT_VERIFIABLE", "NOT_SUPPORTED"
FOLDER_MIME = "application/vnd.google-apps.folder"

NOTION_NEEDS = {"API-post-page", "API-patch-page", "API-retrieve-a-page"}
DRIVE_NEEDS = {"create_file", "search_files", "read_file_content", "get_file_metadata"}


@dataclass
class Item:
    integration: str
    name: str
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"[{self.status:14}] {self.integration:12} {self.name}" + (f" - {self.detail}" if self.detail else "")


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)

    def add(self, integration: str, name: str, status: str, detail: str = "") -> Item:
        item = Item(integration, name, status, detail)
        self.items.append(item)
        return item

    def ok(self, integration: str) -> bool:
        return not any(i.integration == integration and i.status in (MISSING, INVALID, FAIL, BLOCKED)
                       for i in self.items)


# ======================= 1. preflight (offline) =======================


def _sandbox(report: Report, integration: str, setting: str, alias: str | None, targets: dict[str, str],
             label: str) -> None:
    if not alias:
        report.add(integration, f"sandbox {label} ({setting})", MISSING, "smoke writes need a non-production alias")
    elif alias not in targets:
        report.add(integration, f"sandbox {label} ({setting})", INVALID, "alias is not an approved destination")
    else:
        report.add(integration, f"sandbox {label} ({setting})", OK, f"alias '{alias}'")


def preflight(settings: Settings) -> tuple[Report, IntegrationConfig | None]:
    report = Report()
    g = "general"
    report.add(g, "INTEGRATION_MODE=real", OK if settings.integration_mode is IntegrationMode.REAL else MISSING,
               f"currently '{settings.integration_mode.value}' (needed for --write only)")
    report.add(g, "REAL_WRITES_ENABLED=true", OK if settings.real_writes_enabled else MISSING,
               "server kill switch (needed for --write only)")
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        report.add(g, "configuration valid", INVALID, str(exc))
        return report, None
    report.add(g, "configuration valid", OK)

    n = "notion"
    report.add(n, "NOTION_MCP_URL", OK if config.notion_mcp else MISSING,
               "self-hosted @notionhq/notion-mcp-server@2.6.1 --transport http")
    report.add(n, "NOTION_MCP_AUTH_TOKEN", OK if config.notion_mcp and config.notion_mcp.auth_token else MISSING,
               "the MCP server's AUTH_TOKEN (presence checked only)")
    missing = sorted(NOTION_NEEDS - config.notion_allowlist)
    report.add(n, "tool allowlist", OK if not missing else INVALID, f"missing {missing}" if missing else "")
    _sandbox(report, n, "SMOKE_TEST_NOTION_PARENT", settings.smoke_test_notion_parent, config.notion_pages.targets,
             "parent page")
    report.add(n, "NOTION_TOKEN set in the MCP server's environment", MANUAL, "not visible to this backend")
    report.add(n, "sandbox page shared with the Notion integration", MANUAL, "verified by --read (retrieve page)")
    report.add(n, "sandbox page is NOT a production page", MANUAL, "operator responsibility")

    d = "google_drive"
    report.add(d, "GOOGLE_DRIVE_MCP_URL", OK if config.drive_mcp else MISSING, "https://drivemcp.googleapis.com/mcp/v1")
    report.add(d, "GOOGLE_DRIVE_MCP_AUTH_TOKEN", OK if config.drive_mcp and config.drive_mcp.auth_token else MISSING,
               "OAuth 2.0 access token, scopes drive.file + drive.readonly (presence checked only)")
    missing = sorted(DRIVE_NEEDS - config.drive_allowlist)
    report.add(d, "tool allowlist", OK if not missing else INVALID, f"missing {missing}" if missing else "")
    _sandbox(report, d, "SMOKE_TEST_DRIVE_FOLDER", settings.smoke_test_drive_folder, config.drive_folders.targets,
             "folder")
    report.add(d, "Google Workspace Developer Preview Program membership", MANUAL, "required by the Drive MCP server")
    report.add(d, "Drive API + Drive MCP API enabled; OAuth client 'AI-powered agent'", MANUAL,
               "Google Cloud console")
    report.add(d, "access token unexpired (typically ~1 hour)", MANUAL, "verified by --read")
    report.add(d, "sandbox folder is NOT a production folder", MANUAL, "operator responsibility")

    t = "teams"
    report.add(t, "TEAMS_WEBHOOK_URLS", OK if config.teams_channels.targets else MISSING,
               "Workflows webhook(s); value never printed")
    _sandbox(report, t, "SMOKE_TEST_TEAMS_CHANNEL", settings.smoke_test_teams_channel, config.teams_channels.targets,
             "channel")
    report.add(t, "workflow trigger authentication is 'Anyone'", MANUAL, "tenant/user auth is not supported")
    report.add(t, "sandbox channel is NOT a production channel", MANUAL, "operator responsibility")
    return report, config


# ======================= 2. read-only connectivity =======================


def _client(config: IntegrationConfig, integration: Integration, transports: dict) -> McpClient | None:
    server = config.notion_mcp if integration is Integration.NOTION else config.drive_mcp
    if server is None:
        return None
    return McpClient(server, transports.get(integration), contracts=CONTRACTS[integration])


def _discover(report: Report, label: str, client: McpClient) -> bool:
    try:
        offered = client.list_tools()
    except IntegrationError as exc:
        report.add(label, "MCP initialize + tools/list", FAIL, exc.code)
        return False
    report.add(label, "MCP initialize + tools/list", PASS, f"{len(offered)} allowlisted tool(s) offered")
    healthy = True
    for name in sorted(client.config.tool_allowlist):
        if name not in offered:
            report.add(label, f"contract {name}", FAIL, "not offered by the server")
            healthy = False
        elif name in client.contract_problems:
            report.add(label, f"contract {name}", FAIL, "; ".join(client.contract_problems[name]))
            healthy = False
        else:
            report.add(label, f"contract {name}", PASS)
    return healthy


def notion_page(client: McpClient, page_id: str) -> dict[str, Any]:
    return _notion_payload(client.call_tool(NOTION_RETRIEVE_PAGE.name, {"page_id": page_id}))


def _same_id(a: Any, b: Any) -> bool:
    try:
        return uuid.UUID(str(a)) == uuid.UUID(str(b))
    except ValueError:
        return False


def read_checks(settings: Settings, config: IntegrationConfig, report: Report, *,
                mcp_transports: dict | None = None) -> None:
    transports = mcp_transports or {}

    n = "notion"
    if not report.ok(n):
        report.add(n, "read-only connectivity", SKIPPED, "fix the preflight items first")
    else:
        client = _client(config, Integration.NOTION, transports)
        try:
            if _discover(report, n, client):
                page = notion_page(client, config.notion_pages.targets[settings.smoke_test_notion_parent])
                if page.get("object") == "page" and not page.get("in_trash"):
                    report.add(n, "retrieve sandbox parent page", PASS, "reachable, shared with the integration")
                else:
                    report.add(n, "retrieve sandbox parent page", FAIL, "not a live page (trashed or wrong object)")
        except IntegrationError as exc:
            report.add(n, "retrieve sandbox parent page", FAIL,
                       f"{exc.code}: {exc.message}" + (" - share the page with the integration"
                                                       if "404" in exc.message else ""))
        finally:
            client.close()

    d = "google_drive"
    if not report.ok(d):
        report.add(d, "read-only connectivity", SKIPPED, "fix the preflight items first")
    else:
        client = _client(config, Integration.GOOGLE_DRIVE, transports)
        folder = config.drive_folders.targets[settings.smoke_test_drive_folder]
        try:
            if _discover(report, d, client):
                meta = client.call_tool(DRIVE_GET_FILE_METADATA.name,
                                        {"fileId": folder, "excludeContentSnippets": True}).json_payload()
                if isinstance(meta, dict) and meta.get("mimeType") == FOLDER_MIME:
                    report.add(d, "get_file_metadata on sandbox folder", PASS, "folder exists and is readable")
                else:
                    report.add(d, "get_file_metadata on sandbox folder", FAIL, "not a folder or unexpected result")
                listing = client.call_tool(DRIVE_SEARCH_FILES.name, {
                    "query": f"parentId = '{folder}'", "pageSize": 1, "excludeContentSnippets": True,
                }).json_payload()
                report.add(d, "search_files in sandbox folder",
                           PASS if isinstance(listing, dict) else FAIL, "search scoped by parentId works")
        except IntegrationError as exc:
            hint = " - access token expired or missing scopes?" if exc.code == "MCP_UNAUTHORIZED" else ""
            report.add(d, "read-only connectivity", FAIL, exc.code + hint)
        finally:
            client.close()

    report.add("teams", "read-only connectivity", NOT_VERIFIABLE,
               "a webhook has no read-only operation; any POST triggers the workflow")


# ======================= 3. approved writes with remote verification =======================


def _title_text(page: dict[str, Any]) -> str | None:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return "".join(t.get("plain_text") or (t.get("text") or {}).get("content", "")
                           for t in prop.get("title", []))
    return None


def _write_notion(registry, config, settings, report: Report, run_id: str, title: str, content: str) -> None:
    n = "notion"
    adapter = registry.adapters[Integration.NOTION]
    parent_id = config.notion_pages.targets[settings.smoke_test_notion_parent]
    call = ToolCall(f"smoke-{run_id}", 1, Integration.NOTION, Action.CREATE_PAGE,
                    {"title": title, "content": content, "parent": settings.smoke_test_notion_parent},
                    write_authorized=True)
    try:
        result = registry.execute(call)
    except ToolError as exc:
        report.add(n, "create synthetic page", FAIL, exc.code)
        return
    if not result.resource_id:
        report.add(n, "create synthetic page", FAIL, "no page id returned; check the sandbox page manually")
        return
    report.add(n, "create synthetic page", PASS, "API-post-page returned a page id")
    try:
        page = notion_page(adapter.client, result.resource_id)
        parent = page.get("parent") or {}
        checks = {"is a live page": page.get("object") == "page" and not page.get("in_trash"),
                  "under the sandbox parent": _same_id(parent.get("page_id"), parent_id),
                  "title matches": _title_text(page) == title}
        bad = [k for k, ok in checks.items() if not ok]
        report.add(n, "verify remote page (read back)", FAIL if bad else PASS,
                   f"failed: {bad}" if bad else "; ".join(checks))
    except ToolError as exc:
        report.add(n, "verify remote page (read back)", FAIL, exc.code)
    try:
        registry.compensate(CompensationCall(f"smoke-{run_id}", 1, Integration.NOTION, Action.CREATE_PAGE,
                                             result.resource_id))
        trashed = notion_page(adapter.client, result.resource_id).get("in_trash") is True
        report.add(n, "rollback (move page to trash) + verify", PASS if trashed else FAIL,
                   "page is in trash" if trashed else f"page NOT in trash - delete manually: {result.output.get('url')}")
    except ToolError as exc:
        report.add(n, "rollback (move page to trash) + verify", FAIL,
                   f"{exc.code} - delete manually: {result.output.get('url')}")


def _write_drive(registry, config, settings, report: Report, run_id: str, title: str, content: str) -> None:
    d = "google_drive"
    adapter = registry.adapters[Integration.GOOGLE_DRIVE]
    folder = config.drive_folders.targets[settings.smoke_test_drive_folder]
    call = ToolCall(f"smoke-{run_id}", 1, Integration.GOOGLE_DRIVE, Action.CREATE_DOCUMENT,
                    {"title": title, "content": content, "folder": settings.smoke_test_drive_folder},
                    write_authorized=True)
    try:
        result = registry.execute(call)
    except ToolError as exc:
        report.add(d, "create synthetic document", FAIL, exc.code)
        return
    if not result.resource_id:
        report.add(d, "create synthetic document", FAIL, "no file id returned; check the sandbox folder manually")
        return
    report.add(d, "create synthetic document", PASS, "create_file returned a file id")
    try:
        meta = adapter.client.call_tool(DRIVE_GET_FILE_METADATA.name,
                                        {"fileId": result.resource_id, "excludeContentSnippets": True}).json_payload()
        meta = meta if isinstance(meta, dict) else {}
        body = adapter.client.call_tool(DRIVE_READ_FILE_CONTENT.name, {"fileId": result.resource_id}).json_payload()
        text = body.get("fileContent") if isinstance(body, dict) else None
        checks = {"title matches": meta.get("title") == title,
                  "in the sandbox folder": meta.get("parentId") == folder,
                  "content contains the run marker": isinstance(text, str) and run_id in text}  # content not printed
        bad = [k for k, ok in checks.items() if not ok]
        report.add(d, "verify remote document (read back)", FAIL if bad else PASS,
                   f"failed: {bad}" if bad else "; ".join(checks))
    except ToolError as exc:
        report.add(d, "verify remote document (read back)", FAIL, exc.code)
    report.add(d, "rollback", NOT_SUPPORTED,
               f"the official Drive MCP server has no delete/trash tool - delete '{title}' from sandbox folder "
               f"'{settings.smoke_test_drive_folder}' manually: {result.output.get('url', '(no link returned)')}")


def _write_teams(registry, settings, report: Report, run_id: str, title: str) -> None:
    t = "teams"
    call = ToolCall(f"smoke-{run_id}", 1, Integration.TEAMS, Action.SEND_MESSAGE,
                    {"message": f"{title}. Synthetic smoke-test message; safe to delete.",
                     "channel": settings.smoke_test_teams_channel}, write_authorized=True)
    try:
        result = registry.execute(call)
    except ToolError as exc:
        report.add(t, "post synthetic message", FAIL, exc.code)
        return
    report.add(t, "post synthetic message", PASS, f"webhook accepted (HTTP {result.output.get('http_status')})")
    report.add(t, "verify remote message", NOT_VERIFIABLE,
               f"a 2xx only means the workflow was triggered; look for run id '{run_id}' in sandbox channel "
               f"'{settings.smoke_test_teams_channel}' and check the workflow's run history")
    report.add(t, "rollback", NOT_SUPPORTED, "webhook messages cannot be deleted; remove it manually if needed")


def write_checks(settings: Settings, config: IntegrationConfig, report: Report, integrations: list[str], *,
                 approved: bool, mcp_transports: dict | None = None, webhook_sender=None,
                 run_id: str | None = None) -> None:
    if not approved:
        for name in integrations:
            report.add(name, "real write", BLOCKED, "requires --i-approve-real-writes")
        return
    general_ok = settings.integration_mode is IntegrationMode.REAL and settings.real_writes_enabled
    run_id = run_id or uuid.uuid4().hex[:10]
    title = f"WorkflowWeaver smoke test {run_id}"
    content = f"Synthetic smoke-test content, run {run_id}. Safe to delete."
    registry = None
    try:
        for name in integrations:
            if not general_ok:
                report.add(name, "real write", BLOCKED, "needs INTEGRATION_MODE=real and REAL_WRITES_ENABLED=true")
                continue
            if not report.ok(name) or not any(i.integration == name and i.status in (PASS, NOT_VERIFIABLE)
                                              for i in report.items):
                report.add(name, "real write", BLOCKED, "preflight and read-only checks must pass first")
                continue
            if registry is None:
                registry = build_integration_registry(settings, config=config, mcp_transports=mcp_transports,
                                                      webhook_sender=webhook_sender)
            report.add(name, "synthetic run id", PASS, run_id)
            if name == "notion":
                _write_notion(registry, config, settings, report, run_id, title, content)
            elif name == "google_drive":
                _write_drive(registry, config, settings, report, run_id, title, content)
            else:
                _write_teams(registry, settings, report, run_id, title)
    finally:
        if registry is not None:
            registry.close()


def run(settings: Settings, *, read: bool, write: list[str], approved: bool,
        mcp_transports: dict | None = None, webhook_sender=None, run_id: str | None = None) -> Report:
    report, config = preflight(settings)
    if config is None or not (read or write):
        return report
    read_checks(settings, config, report, mcp_transports=mcp_transports)
    if write:
        write_checks(settings, config, report, write, approved=approved, mcp_transports=mcp_transports,
                     webhook_sender=webhook_sender, run_id=run_id)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--read", action="store_true", help="run read-only connectivity checks")
    parser.add_argument("--write", action="append", default=[], choices=["notion", "google_drive", "teams"],
                        help="perform ONE real synthetic write to this integration's sandbox (repeatable)")
    parser.add_argument("--i-approve-real-writes", action="store_true", dest="approved")
    args = parser.parse_args(argv)
    report = run(get_settings(), read=args.read or bool(args.write), write=args.write, approved=args.approved)
    for item in report.items:
        print(item.line())
    statuses = {i.status for i in report.items}
    if FAIL in statuses:
        return 1
    if args.write and BLOCKED in statuses:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
