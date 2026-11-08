"""Opt-in live verification of the configured integrations (Phase 5.2).

Default (no flags): prints the PLAN only - which checks would run - and contacts nothing.

    python -m scripts.verify_integrations                 # plan only, no network
    python -m scripts.verify_integrations --live          # MCP discovery only (read-only)
    python -m scripts.verify_integrations --live --teams-post-test synthetic-team --i-approve-a-real-teams-post

--live              For each configured MCP server: initialize + tools/list, then compare every
                    contracted tool's live schema with app.integrations.contracts. NEVER calls
                    tools/call, so nothing is read from or written to Notion / Drive.
--teams-post-test   Posts ONE fixed synthetic message to the named Teams alias. This is a real,
                    irreversible write; it additionally requires --i-approve-a-real-teams-post.

Output: integration names, tool names, statuses and error codes only. URLs, tokens,
destination ids and document contents are never printed. Notion/Drive writes are not
offered here at all; they only run through the authorized execution API.
"""

import argparse
import sys
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.integrations.config import load_integration_config
from app.integrations.contracts import CONTRACTS
from app.integrations.errors import IntegrationConfigError, IntegrationError
from app.integrations.http.webhook import WebhookClient, WebhookSender
from app.integrations.mcp.client import McpClient
from app.schemas.workflow import Integration

TEAMS_TEST_MESSAGE = "WorkflowWeaver integration check (synthetic test message; safe to delete)."


@dataclass
class Check:
    name: str
    status: str  # PLANNED | PASS | FAIL | SKIPPED
    detail: str = ""

    def line(self) -> str:
        return f"[{self.status:7}] {self.name}" + (f" - {self.detail}" if self.detail else "")


def verify(
    settings: Settings,
    *,
    live: bool,
    teams_alias: str | None = None,
    approve_teams_post: bool = False,
    mcp_transports: dict[Integration, Any] | None = None,
    webhook_sender: WebhookSender | None = None,
) -> list[Check]:
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        return [Check("configuration", "FAIL", str(exc))]
    checks = [Check("configuration", "PASS", f"mode={settings.integration_mode.value}")]
    transports = mcp_transports or {}

    for integration, server in ((Integration.NOTION, config.notion_mcp), (Integration.GOOGLE_DRIVE, config.drive_mcp)):
        label = f"{integration.value} MCP discovery"
        if server is None:
            checks.append(Check(label, "SKIPPED", "not configured"))
            continue
        if not live:
            checks.append(Check(label, "PLANNED", "initialize + tools/list only (needs --live)"))
            continue
        client = McpClient(server, transports.get(integration), contracts=CONTRACTS[integration])
        try:
            offered = client.list_tools()
        except IntegrationError as exc:
            checks.append(Check(label, "FAIL", exc.code))
            continue
        finally:
            client.close()
        checks.append(Check(label, "PASS", f"{len(offered)} allowlisted tool(s) offered"))
        for name in sorted(server.tool_allowlist):
            tool_check = f"{integration.value} contract {name}"
            if name not in offered:
                checks.append(Check(tool_check, "FAIL", "tool not offered by server"))
            elif name in client.contract_problems:
                checks.append(Check(tool_check, "FAIL", "; ".join(client.contract_problems[name])))
            else:
                checks.append(Check(tool_check, "PASS", CONTRACTS[integration][name].verification))

    if teams_alias is None:
        checks.append(Check("teams webhook post", "SKIPPED", "no --teams-post-test alias given"))
    elif teams_alias not in config.teams_channels.targets:
        checks.append(Check("teams webhook post", "FAIL", "alias is not an approved channel"))
    elif not (live and approve_teams_post):
        checks.append(Check("teams webhook post", "PLANNED",
                            "real post needs --live and --i-approve-a-real-teams-post"))
    else:
        from app.agents.execution.tools.integrations import teams_card

        sender = webhook_sender or WebhookClient(connect_timeout=config.connect_timeout_seconds,
                                                 timeout=config.timeout_seconds)
        try:
            response = sender.post_json(config.teams_channels.targets[teams_alias], teams_card(TEAMS_TEST_MESSAGE))
            checks.append(Check("teams webhook post", "PASS",
                                f"HTTP {response.status_code} accepted; confirm the message in the channel"))
        except IntegrationError as exc:
            checks.append(Check("teams webhook post", "FAIL", exc.code))
        finally:
            sender.close()
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="contact configured MCP servers (discovery only)")
    parser.add_argument("--teams-post-test", metavar="ALIAS", help="post one synthetic message to this alias")
    parser.add_argument("--i-approve-a-real-teams-post", action="store_true", dest="approve_teams_post")
    args = parser.parse_args(argv)
    checks = verify(get_settings(), live=args.live, teams_alias=args.teams_post_test,
                    approve_teams_post=args.approve_teams_post)
    for check in checks:
        print(check.line())
    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
