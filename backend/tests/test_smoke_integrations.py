"""Phase 5.3: the controlled smoke-test harness (scripts/smoke_integrations.py).

Runs against fake MCP servers and a recording webhook sender only; the conftest network
guard fails any test that tries to reach a real host."""

import json

import pytest

from app.agents.execution.tools.base import ErrorKind
from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.schemas.workflow import Integration
from scripts import smoke_integrations as smoke
from tests.integration_fakes import (
    DRIVE_FOLDER_ID,
    NOTION_PAGE_ID,
    SECRETS,
    WEBHOOK_URL,
    FakeMcpServer,
    RecordingWebhookSender,
    integration_settings,
)

SANDBOX = dict(smoke_test_notion_parent="engineering", smoke_test_drive_folder="reports",
               smoke_test_teams_channel="synthetic-team")


class Fakes:
    def __init__(self) -> None:
        self.notion = FakeMcpServer("notion")
        self.drive = FakeMcpServer("drive")
        self.teams = RecordingWebhookSender()
        self.transports = {Integration.NOTION: lambda: self.notion, Integration.GOOGLE_DRIVE: lambda: self.drive}

    def write_calls(self) -> list[str]:
        writes = {"API-post-page", "API-patch-page", "create_file"}
        return [n for n, _ in self.notion.tool_calls + self.drive.tool_calls if n in writes]


def run(settings, fakes: Fakes, *, read=True, write=(), approved=False):
    return smoke.run(settings, read=read, write=list(write), approved=approved, mcp_transports=fakes.transports,
                     webhook_sender=fakes.teams, run_id="synthrun01")


def statuses(report, integration=None) -> dict[str, str]:
    return {i.name: i.status for i in report.items if integration is None or i.integration == integration}


def assert_no_secrets(report) -> None:
    text = "\n".join(i.line() for i in report.items)
    assert not [s for s in SECRETS if s in text], text


# ======================= preflight =======================


def test_preflight_with_default_settings_reports_everything_missing_and_contacts_nothing():
    report, config = smoke.preflight(Settings(_env_file=None))
    s = statuses(report)
    assert s["INTEGRATION_MODE=real"] == "MISSING" and s["REAL_WRITES_ENABLED=true"] == "MISSING"
    for integration in ("notion", "google_drive", "teams"):
        assert not report.ok(integration)
    assert statuses(report, "notion")["NOTION_MCP_URL"] == "MISSING"
    assert statuses(report, "google_drive")["GOOGLE_DRIVE_MCP_AUTH_TOKEN"] == "MISSING"
    assert statuses(report, "teams")["TEAMS_WEBHOOK_URLS"] == "MISSING"


def test_preflight_fully_configured_lists_only_ok_and_manual_items():
    report, _ = smoke.preflight(integration_settings("real", **SANDBOX))
    assert {i.status for i in report.items} == {"OK", "MANUAL"}
    manual = [i.name for i in report.items if i.status == "MANUAL"]
    assert "Google Workspace Developer Preview Program membership" in manual
    assert "sandbox channel is NOT a production channel" in manual
    assert_no_secrets(report)


def test_preflight_flags_unapproved_sandbox_alias_and_narrowed_allowlist():
    report, _ = smoke.preflight(integration_settings(
        "real", **{**SANDBOX, "smoke_test_teams_channel": "prod-channel"},
        notion_mcp_tool_allowlist="API-post-page"))
    assert statuses(report, "teams")["sandbox channel (SMOKE_TEST_TEAMS_CHANNEL)"] == "INVALID"
    assert statuses(report, "notion")["tool allowlist"] == "INVALID"


def test_preflight_reports_invalid_configuration_by_setting_name():
    report, config = smoke.preflight(integration_settings("real", notion_destinations="{bad"))
    assert config is None and statuses(report)["configuration valid"] == "INVALID"
    assert "NOTION_DESTINATIONS" in report.items[-1].detail


# ======================= read-only connectivity =======================


def test_read_checks_pass_and_never_write():
    fakes = Fakes()
    report = run(integration_settings("real", **SANDBOX), fakes)
    assert statuses(report, "notion")["retrieve sandbox parent page"] == "PASS"
    assert statuses(report, "notion")["contract API-retrieve-a-page"] == "PASS"
    assert statuses(report, "google_drive")["get_file_metadata on sandbox folder"] == "PASS"
    assert statuses(report, "google_drive")["search_files in sandbox folder"] == "PASS"
    assert statuses(report, "teams")["read-only connectivity"] == "NOT_VERIFIABLE"
    assert fakes.write_calls() == [] and fakes.teams.posts == []
    assert fakes.drive.calls_named("search_files") == [
        {"query": f"parentId = '{DRIVE_FOLDER_ID}'", "pageSize": 1, "excludeContentSnippets": True}]
    assert_no_secrets(report)


def test_read_checks_report_unshared_notion_page():
    fakes = Fakes()
    settings = integration_settings("real", **{**SANDBOX, "smoke_test_notion_parent": "tasks"})  # not shared
    report = run(settings, fakes)
    item = next(i for i in report.items if i.name == "retrieve sandbox parent page")
    assert item.status == "FAIL" and "share the page with the integration" in item.detail


def test_read_checks_report_expired_drive_token():
    fakes = Fakes()
    fakes.drive.failures["get_file_metadata"] = [
        IntegrationError(ErrorKind.PERMISSION, "MCP_UNAUTHORIZED", "x", request_sent=True)]
    report = run(integration_settings("real", **SANDBOX), fakes)
    item = next(i for i in report.items if i.integration == "google_drive" and i.status == "FAIL")
    assert "MCP_UNAUTHORIZED" in item.detail and "expired" in item.detail


def test_read_checks_skip_unconfigured_integrations_without_network():
    report = smoke.run(Settings(_env_file=None), read=True, write=[], approved=False)
    assert statuses(report, "notion")["read-only connectivity"] == "SKIPPED"
    assert statuses(report, "google_drive")["read-only connectivity"] == "SKIPPED"


# ======================= approved writes =======================


@pytest.mark.parametrize("integration", ["notion", "google_drive", "teams"])
def test_writes_are_blocked_without_explicit_approval(integration):
    fakes = Fakes()
    report = run(integration_settings("real", **SANDBOX), fakes, write=[integration], approved=False)
    assert statuses(report, integration)["real write"] == "BLOCKED"
    assert fakes.write_calls() == [] and fakes.teams.posts == []


@pytest.mark.parametrize("overrides", [{"integration_mode": "dry_run"}, {"real_writes_enabled": False}])
def test_writes_are_blocked_unless_real_mode_and_kill_switch_allow_them(overrides):
    fakes = Fakes()
    settings = integration_settings(**{"mode": "real", **SANDBOX, **overrides})
    report = run(settings, fakes, write=["notion"], approved=True)
    assert statuses(report, "notion")["real write"] == "BLOCKED" and fakes.write_calls() == []


def test_writes_are_blocked_when_read_checks_fail():
    fakes = Fakes()
    fakes.notion.tools = [t for t in fakes.notion.tools if t["name"] != "API-retrieve-a-page"]
    report = run(integration_settings("real", **SANDBOX), fakes, write=["notion"], approved=True)
    assert statuses(report, "notion")["real write"] == "BLOCKED" and fakes.write_calls() == []


def test_notion_write_is_verified_remotely_and_rolled_back():
    fakes = Fakes()
    report = run(integration_settings("real", **SANDBOX), fakes, write=["notion"], approved=True)
    s = statuses(report, "notion")
    assert s["create synthetic page"] == "PASS"
    assert s["verify remote page (read back)"] == "PASS"
    assert s["rollback (move page to trash) + verify"] == "PASS"
    created = fakes.notion.calls_named("API-post-page")[0]
    assert created["parent"] == {"page_id": NOTION_PAGE_ID}  # the sandbox parent, nothing else
    assert created["properties"]["title"]["title"][0]["text"]["content"] == "WorkflowWeaver smoke test synthrun01"
    page_id = fakes.notion.calls_named("API-patch-page")[0]["page_id"]
    assert fakes.notion.resources[page_id]["in_trash"] is True
    assert_no_secrets(report)


def test_notion_verification_detects_a_wrong_remote_result():
    fakes = Fakes()
    original = fakes.notion._page

    def wrong_title(pid):
        page = original(pid)
        if pid != NOTION_PAGE_ID:
            page["properties"]["title"]["title"] = [{"plain_text": "something else"}]
        return page

    fakes.notion._page = wrong_title
    report = run(integration_settings("real", **SANDBOX), fakes, write=["notion"], approved=True)
    item = next(i for i in report.items if i.name == "verify remote page (read back)")
    assert item.status == "FAIL" and "title matches" in item.detail


def test_notion_rollback_failure_is_reported_with_manual_cleanup():
    fakes = Fakes()
    fakes.notion.notion_errors["API-patch-page"] = [(403, "restricted_resource")]
    report = run(integration_settings("real", **SANDBOX), fakes, write=["notion"], approved=True)
    item = next(i for i in report.items if i.name.startswith("rollback"))
    assert item.status == "FAIL" and "delete manually: https://www.notion.so/" in item.detail


def test_drive_write_is_verified_remotely_and_rollback_is_reported_unsupported():
    fakes = Fakes()
    report = run(integration_settings("real", **SANDBOX), fakes, write=["google_drive"], approved=True)
    s = statuses(report, "google_drive")
    assert s["create synthetic document"] == "PASS"
    assert s["verify remote document (read back)"] == "PASS"
    rollback = next(i for i in report.items if i.integration == "google_drive" and i.name == "rollback")
    assert rollback.status == "NOT_SUPPORTED" and "no delete/trash tool" in rollback.detail
    assert "WorkflowWeaver smoke test synthrun01" in rollback.detail
    assert fakes.drive.calls_named("create_file")[0]["parentId"] == DRIVE_FOLDER_ID
    assert_no_secrets(report)


def test_drive_verification_detects_a_document_outside_the_sandbox_folder():
    fakes = Fakes()
    original = fakes.drive._create_file
    fakes.drive._create_file = lambda args: original({**args, "parentId": "somewhere-else"})
    report = run(integration_settings("real", **SANDBOX), fakes, write=["google_drive"], approved=True)
    item = next(i for i in report.items if i.name == "verify remote document (read back)")
    assert item.status == "FAIL" and "in the sandbox folder" in item.detail


def test_teams_write_is_reported_accepted_but_not_verifiable():
    fakes = Fakes()
    report = run(integration_settings("real", **SANDBOX), fakes, write=["teams"], approved=True)
    s = statuses(report, "teams")
    assert s["post synthetic message"] == "PASS" and s["verify remote message"] == "NOT_VERIFIABLE"
    assert s["rollback"] == "NOT_SUPPORTED"
    url, card = fakes.teams.posts[0]
    assert url == WEBHOOK_URL and "synthrun01" in json.dumps(card)
    assert_no_secrets(report)


def test_writes_go_only_to_the_sandbox_alias_not_the_default_destination():
    fakes = Fakes()
    sandbox_id = "5e1f0000-0000-4000-8000-0000000000aa"
    settings = integration_settings(
        "real", **{**SANDBOX, "smoke_test_notion_parent": "sandbox"},
        notion_destinations=json.dumps({"engineering": NOTION_PAGE_ID, "tasks": "5e1f0000-0000-4000-8000-000000000006",
                                        "sandbox": sandbox_id}))
    fakes.notion.resources[sandbox_id] = {"in_trash": False, "args": {"parent": {"type": "workspace"},
                                                                      "properties": {}}}
    run(settings, fakes, write=["notion"], approved=True)
    assert fakes.notion.calls_named("API-post-page")[0]["parent"] == {"page_id": sandbox_id}


# ======================= CLI =======================


def test_cli_preflight_exits_zero_and_write_without_approval_exits_two(capsys):
    assert smoke.main([]) == 0
    assert "NOTION_MCP_URL" in capsys.readouterr().out
    assert smoke.main(["--write", "notion"]) == 2
