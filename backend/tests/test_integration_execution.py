"""Phase 5.1: dry-run and real-mode execution through the unchanged LangGraph execution
graph, against fake MCP servers and a fake webhook sender. Covers authorization, approved
destinations, retries, idempotency, Saga compensation, SSE ordering, telemetry and secret
leakage. No real service is called."""

import json
import logging
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.execution.tools import FailureInjector, Fault
from app.agents.execution.tools.base import ErrorKind
from app.api import execution as execution_api
from app.api import integrations as integrations_api
from app.core.config import Settings
from app.integrations.errors import IntegrationError
from app.main import app
from app.observability import set_telemetry
from app.schemas.execution import ExecutionRequest, ExecutionStatus
from app.streaming.broker import EventBroker
from app.streaming.runner import ExecutionRunner
from tests.execution_helpers import FOUR_STEPS
from tests.integration_fakes import (
    DOCUMENT_TEXT,
    DRIVE_FOLDER_ID,
    NOTION_DB_ID,
    NOTION_PAGE_ID,
    OPERATOR_TOKEN,
    SECRETS,
    WEBHOOK_URL,
    FakeWorld,
    integration_settings,
)
from tests.telemetry_helpers import RecordingTelemetry

URL = "/api/v1/workflows/execute"
WRITES = FOUR_STEPS[1:]  # notion page, drive doc, teams message


def single(step, **arguments):
    """One step as a valid standalone plan (renumbered to 1, no dependencies)."""
    return [{**step, "step_number": 1, "depends_on": [], "arguments": {**step["arguments"], **arguments}}]


def body(steps=None, mode="real", workflow_id=None):
    return {
        "workflow_id": str(workflow_id or uuid4()),
        "plan": {"summary": "Synthetic plan", "steps": FOUR_STEPS if steps is None else steps},
        "approval": {"approved": True, "mode": mode, "approved_by": "synthetic.reviewer"},
    }


def request(steps=None, mode="real", workflow_id=None) -> ExecutionRequest:
    return ExecutionRequest.model_validate(body(steps, mode, workflow_id))


def codes(response) -> list[str]:
    return [e.code for e in response.errors]


def assert_no_secrets(text: str) -> None:
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"secret values leaked: {leaked}"


# ======================= dry run =======================


def test_dry_run_completes_without_creating_any_client_or_sending_anything():
    world = FakeWorld("dry_run")
    response = world.service.execute(request(mode="dry_run"))
    assert response.status is ExecutionStatus.COMPLETED and response.mode == "dry_run"
    assert world.factories_called == [] and world.remote_calls() == 0
    assert "dry run" in response.message
    by_step = {r.step_number: r for r in response.step_results}
    assert by_step[1].output["would_call"] == ["search_files", "read_file_content"]
    assert by_step[2].output == {"mode": "dry_run", "would_call": "API-post-page", "sent": False,
                                 "destination": "engineering", "title": "Sprint 42 kickoff"}
    assert by_step[4].output["channel"] == "synthetic-team" and by_step[4].output["delivered"] is False
    assert by_step[2].resource_id.startswith("dryrun-notion-")
    assert_no_secrets(response.model_dump_json())


def test_dry_run_needs_no_write_authorization_but_still_enforces_destinations():
    world = FakeWorld("dry_run")
    steps = single(WRITES[0], parent="somewhere-else")
    response = world.service.execute(request(steps, mode="dry_run"))
    assert response.status is ExecutionStatus.REJECTED and codes(response) == ["INVALID_PLAN"]
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]


def test_dry_run_failure_still_runs_saga_compensation_offline():
    world = FakeWorld("dry_run")
    world.registry.faults = FailureInjector({4: [Fault.PERMANENT]})
    response = world.service.execute(request(mode="dry_run"))
    # Notion page: compensated offline. Drive document: the official Drive MCP server has no
    # delete/trash tool, so create_document is irreversible in dry-run and real mode alike.
    assert response.status is ExecutionStatus.PARTIALLY_ROLLED_BACK
    assert {c.integration: c.status.value for c in response.compensation_results} == {
        "google_drive": "NOT_COMPENSATABLE", "notion": "COMPENSATED"}
    assert world.remote_calls() == 0


# ======================= real mode: authorization gates =======================


def test_real_writes_without_operator_authorization_are_rejected_before_any_call():
    world = FakeWorld("real")
    response = world.service.execute(request(), write_authorized=False)
    assert response.status is ExecutionStatus.REJECTED
    assert codes(response) == ["REAL_WRITE_AUTHORIZATION_REQUIRED"]
    assert world.factories_called == [] and world.remote_calls() == 0


def test_real_writes_disabled_switch_rejects_even_with_authorization():
    world = FakeWorld("real", real_writes_enabled=False)
    response = world.service.execute(request(), write_authorized=True)
    assert codes(response) == ["REAL_WRITES_DISABLED"] and world.remote_calls() == 0


def test_read_only_plan_needs_no_write_authorization():
    world = FakeWorld("real", real_writes_enabled=False)
    response = world.service.execute(request([FOUR_STEPS[0]]), write_authorized=False)
    assert response.status is ExecutionStatus.COMPLETED
    assert [n for n, _ in world.drive.tool_calls] == ["search_files", "read_file_content"]


def test_mode_mismatch_is_rejected():
    world = FakeWorld("real")
    response = world.service.execute(request(mode="mock"), write_authorized=True)
    assert codes(response) == ["MODE_MISMATCH"] and world.remote_calls() == 0


@pytest.mark.parametrize("argument, value", [("parent", "unknown-alias"), ("parent", NOTION_PAGE_ID)])
def test_unapproved_or_raw_destination_is_rejected_before_any_call(argument, value):
    world = FakeWorld("real")
    steps = single(WRITES[0], **{argument: value})
    response = world.service.execute(request(steps), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]
    assert world.remote_calls() == 0


def test_adapter_refuses_unauthorized_write_even_if_called_directly():
    from app.agents.execution.tools.base import ToolCall
    from app.schemas.plan import Action
    from app.schemas.workflow import Integration

    world = FakeWorld("real")
    call = ToolCall("wf", 1, Integration.NOTION, Action.CREATE_PAGE, {"title": "t", "content": "c"})
    with pytest.raises(Exception) as info:
        world.registry.execute(call)
    assert info.value.code == "REAL_WRITE_NOT_AUTHORIZED" and world.remote_calls() == 0


# ======================= real mode: execution =======================


def test_real_execution_resolves_destinations_server_side_and_completes():
    world = FakeWorld("real")
    response = world.service.execute(request(), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED and response.mode == "real"

    page_args = world.notion.calls_named("API-post-page")[0]
    assert page_args["parent"] == {"page_id": NOTION_PAGE_ID}
    assert page_args["properties"]["title"]["title"][0]["text"]["content"] == "Sprint 42 kickoff"
    created = world.drive.calls_named("create_file")[0]
    assert created == {"title": "Sprint 42 summary", "textContent": "Synthetic summary",
                       "contentMimeType": "text/plain", "parentId": DRIVE_FOLDER_ID}
    search = world.drive.calls_named("search_files")[0]
    assert search == {"query": f"title = 'Synthetic standup notes' and (parentId = '{DRIVE_FOLDER_ID}')",
                      "pageSize": 50, "excludeContentSnippets": True}
    assert world.drive.calls_named("read_file_content") == [{"fileId": "synthetic-doc-standup"}]
    url, card = world.teams.posts[0]
    assert url == WEBHOOK_URL
    assert card["attachments"][0]["content"]["body"][0]["text"] == "Synthetic: plan is ready"

    by_step = {r.step_number: r for r in response.step_results}
    assert by_step[1].output == {"mode": "real", "document_name": "Synthetic standup notes",
                                 "content_length": len(DOCUMENT_TEXT)}  # contents withheld
    assert by_step[2].output["url"].startswith("https://www.notion.so/")
    assert by_step[3].output["url"].startswith("https://docs.google.com/")
    # 2xx from a Workflows webhook is documented only as "request received", so: accepted.
    teams_output = dict(by_step[4].output)
    assert isinstance(teams_output.pop("latency_ms"), float)  # Phase 7.1: measured, value varies
    assert teams_output == {"mode": "real", "channel": "synthetic-team", "accepted": True,
                            "delivery": "accepted_by_webhook", "http_status": 202}
    assert_no_secrets(response.model_dump_json())


def test_notion_task_goes_to_the_configured_database():
    world = FakeWorld("real")
    steps = [{"step_number": 1, "integration": "notion", "action": "create_task",
              "arguments": {"title": "Synthetic task", "assignee": "synthetic.user", "due_date": "2026-12-01"}}]
    assert world.service.execute(request(steps), write_authorized=True).status is ExecutionStatus.COMPLETED
    args = world.notion.calls_named("API-post-page")[0]
    assert args["parent"] == {"type": "database_id", "database_id": NOTION_DB_ID}
    assert "synthetic.user" in json.dumps(args["children"])


def test_document_outside_approved_folders_is_not_read():
    world = FakeWorld("real")
    steps = [{"step_number": 1, "integration": "google_drive", "action": "read_document",
              "arguments": {"document_name": "Outside notes"}}]
    response = world.service.execute(request(steps))
    assert response.status is ExecutionStatus.FAILED
    assert response.step_results[0].error.code == "DOCUMENT_NOT_FOUND"
    assert world.drive.calls_named("read_file_content") == []


def test_transient_mcp_failure_before_send_is_retried_then_succeeds():
    world = FakeWorld("real")
    world.notion.failures["API-post-page"] = [
        IntegrationError(ErrorKind.TRANSIENT, "MCP_UNREACHABLE", "x", request_sent=False)]
    response = world.service.execute(request(single(WRITES[0])), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED and response.attempts == {"1": 2}
    assert world.sleeps == [0.2]


def test_write_timeout_after_send_is_outcome_unknown_and_not_retried():
    world = FakeWorld("real")
    world.teams.failures = [IntegrationError(ErrorKind.TRANSIENT, "WEBHOOK_TIMEOUT", "x", request_sent=True)]
    response = world.service.execute(request(), write_authorized=True)
    assert len(world.teams.posts) == 1  # no duplicate Teams message
    failed = next(r for r in response.step_results if r.status.value == "FAILED")
    assert failed.step_number == 4 and failed.error.code == "OUTCOME_UNKNOWN" and failed.attempts == 1
    # The Notion page is trashed through the verified API-patch-page contract; the Drive
    # document cannot be rolled back (no delete tool on the official server) and remains.
    assert response.status is ExecutionStatus.PARTIALLY_ROLLED_BACK
    page_id = response.step_results[1].resource_id
    assert world.notion.calls_named("API-patch-page") == [{"page_id": page_id, "in_trash": True}]
    drive = next(c for c in response.compensation_results if c.integration == "google_drive")
    assert drive.status.value == "NOT_COMPENSATABLE" and drive.attempts == 0


def test_compensation_is_idempotent_and_retried_on_transient_errors():
    world = FakeWorld("real")
    world.teams.failures = [IntegrationError(ErrorKind.PERMANENT, "WEBHOOK_REJECTED", "x", request_sent=True)]
    world.notion.failures["API-patch-page"] = [
        IntegrationError(ErrorKind.TRANSIENT, "MCP_SERVER_ERROR", "x", request_sent=True)]
    response = world.service.execute(request(), write_authorized=True)
    assert response.status is ExecutionStatus.PARTIALLY_ROLLED_BACK  # Drive doc is irreversible
    notion = next(c for c in response.compensation_results if c.integration == "notion")
    assert notion.attempts == 2 and notion.status.value == "COMPENSATED"
    # A second compensation of the same resource is a no-op.
    from app.agents.execution.tools.base import CompensationCall
    from app.schemas.plan import Action
    from app.schemas.workflow import Integration

    again = CompensationCall(str(response.workflow_id), 2, Integration.NOTION, Action.CREATE_PAGE,
                             notion.resource_id)
    calls = len(world.notion.calls_named("API-patch-page"))
    assert world.registry.compensate(again) is False
    assert len(world.notion.calls_named("API-patch-page")) == calls


def test_duplicate_request_replays_without_new_remote_calls():
    world = FakeWorld("real")
    wid = uuid4()
    first = world.service.execute(request(workflow_id=wid), write_authorized=True)
    calls = world.remote_calls()
    second = world.service.execute(request(workflow_id=wid), write_authorized=False)
    assert second.replayed and second.status is first.status and world.remote_calls() == calls


def test_adapter_ledger_replays_a_repeated_tool_call():
    from app.agents.execution.tools.base import ToolCall
    from app.schemas.plan import Action
    from app.schemas.workflow import Integration

    world = FakeWorld("real")
    call = ToolCall("wf", 1, Integration.NOTION, Action.CREATE_PAGE, {"title": "t", "content": "c"},
                    write_authorized=True)
    first = world.registry.execute(call)
    second = world.registry.execute(call)
    assert second.replayed and second.resource_id == first.resource_id
    assert len(world.notion.calls_named("API-post-page")) == 1


def test_registry_close_closes_mcp_sessions_and_webhook_client():
    world = FakeWorld("real")
    world.service.execute(request(), write_authorized=True)
    world.registry.close()
    assert world.notion.closed == 1 and world.drive.closed == 1 and world.teams.closed


# ======================= SSE, telemetry and logs =======================


def test_sse_order_and_payloads_are_unchanged_and_secret_free_in_real_mode():
    world = FakeWorld("real")
    world.teams.failures = [IntegrationError(ErrorKind.PERMANENT, "WEBHOOK_REJECTED", "x", request_sent=True)]
    runner = ExecutionRunner(world.service, EventBroker(), max_workers=1)
    try:
        channel, future = runner.start(request(), write_authorized=True)
        future.result(timeout=10)
    finally:
        runner.shutdown()
    events = channel.events()
    assert [e.event_type.value for e in events] == [
        "workflow_started",
        "step_started", "step_succeeded",
        "step_started", "step_succeeded",
        "step_started", "step_succeeded",
        "step_started", "step_failed",
        "compensation_started", "compensation_completed",
        "compensation_started", "compensation_completed",
        "workflow_failed",
    ]
    assert [e.event_id for e in events] == list(range(1, len(events) + 1))
    assert events[0].metadata["mode"] == "real"
    assert_no_secrets(json.dumps([e.model_dump(mode="json") for e in events]))


def test_telemetry_reports_mode_and_never_secrets():
    telemetry = RecordingTelemetry()
    set_telemetry(telemetry)
    world = FakeWorld("real")
    world.service.execute(request(), write_authorized=True)
    tools = telemetry.named("tool.")
    assert tools and all(o.metadata["mode"] == "real" for o in tools)
    assert telemetry.one("execution.request").input["server_mode"] == "real"
    assert_no_secrets(telemetry.dump())


def test_logs_contain_no_secrets(caplog):
    caplog.set_level(logging.DEBUG)
    world = FakeWorld("real")
    world.teams.failures = [IntegrationError(ErrorKind.TRANSIENT, "WEBHOOK_TIMEOUT", "x", request_sent=True)]
    world.service.execute(request(), write_authorized=True)
    assert_no_secrets(caplog.text)


# ======================= API =======================


@pytest.fixture
def real_api(monkeypatch):
    world = FakeWorld("real")
    monkeypatch.setattr(execution_api, "get_settings", lambda: world.settings)
    app.dependency_overrides[execution_api.get_execution_service] = lambda: world.service
    yield TestClient(app), world
    app.dependency_overrides.clear()


def test_api_real_write_without_header_is_403(real_api):
    client, world = real_api
    response = client.post(URL, json=body())
    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "REAL_WRITE_AUTHORIZATION_REQUIRED"
    assert world.remote_calls() == 0


def test_api_real_write_with_wrong_header_is_403(real_api):
    client, world = real_api
    response = client.post(URL, json=body(), headers={"X-Integration-Authorization": "wrong-token-value"})
    assert response.status_code == 403 and world.remote_calls() == 0


def test_api_real_write_with_operator_token_executes(real_api):
    client, world = real_api
    response = client.post(URL, json=body(), headers={"X-Integration-Authorization": OPERATOR_TOKEN})
    assert response.status_code == 200 and response.json()["mode"] == "real"
    assert_no_secrets(response.text)


def test_operator_token_is_only_honoured_in_real_mode_with_a_configured_token():
    verify = execution_api.verify_write_authorization
    assert verify(integration_settings("real"), OPERATOR_TOKEN)
    assert not verify(integration_settings("real"), None)
    assert not verify(integration_settings("dry_run"), OPERATOR_TOKEN)
    assert not verify(integration_settings("real", real_write_authorization_token=None), OPERATOR_TOKEN)
    assert not verify(Settings(_env_file=None), OPERATOR_TOKEN)


def test_api_invalid_integration_config_is_503_without_secret(monkeypatch):
    bad = integration_settings("real", teams_webhook_urls=json.dumps({"t": WEBHOOK_URL.replace("https", "http")}))
    monkeypatch.setattr(execution_api, "get_settings", lambda: bad)
    execution_api._default_service.cache_clear()
    try:
        response = TestClient(app).post(URL, json=body())
    finally:
        execution_api._default_service.cache_clear()
    assert response.status_code == 503 and "TEAMS_WEBHOOK_URLS" in response.text
    assert_no_secrets(response.text)


# ----- GET /api/v1/integrations/status -----

STATUS = "/api/v1/integrations/status"


def test_status_in_default_mock_mode(monkeypatch):
    monkeypatch.setattr(integrations_api, "get_settings", lambda: Settings(_env_file=None))
    data = TestClient(app).get(STATUS).json()
    assert data["mode"] == "mock" and data["real_writes_enabled"] is False
    assert data["write_authorization_configured"] is False and data["configuration_valid"] is True
    assert {v["transport"] for v in data["integrations"].values()} == {"mock"}


@pytest.mark.parametrize("mode", ["dry_run", "real"])
def test_status_lists_aliases_only_and_no_secrets(monkeypatch, mode):
    monkeypatch.setattr(integrations_api, "get_settings", lambda: integration_settings(mode))
    response = TestClient(app).get(STATUS)
    data = response.json()
    assert data["mode"] == mode and data["configuration_valid"] is True
    assert data["integrations"]["teams"] == {
        "transport": "webhook", "configured": True,
        "destinations": {"channels": {"aliases": ["synthetic-team"], "default": "synthetic-team"}}}
    assert data["integrations"]["notion"]["destinations"]["pages"]["aliases"] == ["engineering", "tasks"]
    assert_no_secrets(response.text)


def test_status_reports_invalid_configuration_by_setting_name(monkeypatch):
    monkeypatch.setattr(integrations_api, "get_settings",
                        lambda: integration_settings("real", notion_destinations="{bad"))
    data = TestClient(app).get(STATUS).json()
    assert data["configuration_valid"] is False and "NOTION_DESTINATIONS" in data["configuration_error"]
