"""Phase 5.2 contract tests.

* Notion: the arguments WorkflowWeaver builds are validated with jsonschema against the
  tool schemas captured LIVE from @notionhq/notion-mcp-server 2.6.1 (sanitized fixture).
* Google Drive: arguments are checked against the DOCUMENTED schemas of the official
  drivemcp.googleapis.com server (not live-verified; see fixture provenance).
* Teams: the payload matches Microsoft's documented Workflows webhook example.
Plus contract-drift detection, Notion error mapping, allowlist narrowing and the opt-in
live verification script. Fake transports only; nothing connects anywhere.
"""

import copy
import json

import httpx
import jsonschema
import pytest

from app.agents.execution.tools.base import Effect, ErrorKind, ToolCall
from app.agents.execution.tools.integrations import drive_query_literal, drive_search_query, teams_card
from app.core.config import IntegrationMode
from app.integrations.config import load_integration_config
from app.integrations.contracts import CONTRACTS, NOTION_CREATE_PAGE, NOTION_PATCH_PAGE, check_tool_schema
from app.integrations.errors import IntegrationConfigError, IntegrationError
from app.integrations.http.webhook import WebhookClient
from app.observability.sanitize import sanitize
from app.schemas.execution import ExecutionStatus
from app.schemas.plan import Action
from app.schemas.workflow import Integration
from scripts.verify_integrations import verify
from tests.execution_helpers import FOUR_STEPS
from tests.integration_fakes import (
    DRIVE_FIXTURE,
    NOTION_DB_ID,
    NOTION_FIXTURE,
    NOTION_PAGE_ID,
    SECRETS,
    TEAMS_FIXTURE,
    WEBHOOK_SIG,
    WEBHOOK_URL,
    FakeMcpServer,
    FakeWorld,
    RecordingWebhookSender,
    integration_settings,
)
from tests.test_integration_execution import request, single

WRITES = FOUR_STEPS[1:]


def fixture_tool(fixture: dict, name: str) -> dict:
    return next(t for t in fixture["tools"] if t["name"] == name)


def prepared(world: FakeWorld, integration: Integration, action: Action, **arguments):
    adapter = world.registry.adapters[integration]
    return adapter._prepare(ToolCall("wf", 1, integration, action, arguments, write_authorized=True))


# ======================= fixture provenance =======================


def test_notion_fixture_is_the_live_capture_of_the_selected_server():
    p = NOTION_FIXTURE["_provenance"]
    assert p["server"] == "@notionhq/notion-mcp-server" and p["version"] == "2.6.1" and "LIVE" in p["method"]
    assert NOTION_FIXTURE["initialize"]["protocolVersion"] == "2025-06-18"
    assert len(NOTION_FIXTURE["tool_names"]) == 24
    assert {"API-post-page", "API-patch-page"} <= set(NOTION_FIXTURE["tool_names"])


def test_drive_and_teams_fixtures_are_marked_documented_not_live():
    assert DRIVE_FIXTURE["_provenance"]["method"].startswith("DOCUMENTED ONLY")
    assert TEAMS_FIXTURE["_provenance"]["method"].startswith("DOCUMENTED ONLY")
    assert not {"delete_file", "trash_file", "update_file"} & set(DRIVE_FIXTURE["tool_names"])


def test_fixtures_contain_no_secrets():
    text = json.dumps([NOTION_FIXTURE, DRIVE_FIXTURE, TEAMS_FIXTURE])
    assert "Bearer " not in text and "ntn_" not in text and "sig=" not in text


@pytest.mark.parametrize("integration, fixture", [(Integration.NOTION, NOTION_FIXTURE),
                                                  (Integration.GOOGLE_DRIVE, DRIVE_FIXTURE)])
def test_every_contract_matches_its_fixture_schema(integration, fixture):
    for name, contract in CONTRACTS[integration].items():
        assert check_tool_schema(contract, fixture_tool(fixture, name)["inputSchema"]) == [], name


# ======================= Notion: arguments vs LIVE schemas (jsonschema) =======================


def notion_validator(name: str):
    return jsonschema.Draft202012Validator(fixture_tool(NOTION_FIXTURE, name)["inputSchema"])


def test_create_page_arguments_validate_against_live_schema():
    world = FakeWorld("real")
    req = prepared(world, Integration.NOTION, Action.CREATE_PAGE, title="Synthetic", content="x" * 4000)
    notion_validator("API-post-page").validate(req.arguments)
    assert req.arguments["parent"] == {"page_id": NOTION_PAGE_ID}
    assert len(req.arguments["children"]) == 3  # 1900-char chunks


def test_paragraph_blocks_validate_against_the_strict_block_definition():
    # children items are loosely typed in the tool schema; the strict paragraphBlockRequest
    # definition (additionalProperties: false) is what Notion actually expects.
    schema = fixture_tool(NOTION_FIXTURE, "API-post-page")["inputSchema"]
    block_schema = {"$ref": "#/$defs/paragraphBlockRequest", "$defs": schema["$defs"]}
    world = FakeWorld("real")
    req = prepared(world, Integration.NOTION, Action.CREATE_PAGE, title="t", content="body")
    for block in req.arguments["children"]:
        jsonschema.validate(block, block_schema)
    with pytest.raises(jsonschema.ValidationError):  # the 5.1 shape (extra "object" key) is rejected
        jsonschema.validate({"object": "block", **req.arguments["children"][0]}, block_schema)


def test_create_task_arguments_validate_against_live_schema():
    world = FakeWorld("real")
    req = prepared(world, Integration.NOTION, Action.CREATE_TASK, title="Task", assignee="a", due_date="2026-12-01")
    notion_validator("API-post-page").validate(req.arguments)
    schema = fixture_tool(NOTION_FIXTURE, "API-post-page")["inputSchema"]
    parent_schema = {"$ref": "#/$defs/dataSourceIdParentRequest", "$defs": schema["$defs"]}
    jsonschema.validate(req.arguments["parent"], parent_schema)
    assert req.arguments["parent"] == {"type": "database_id", "database_id": NOTION_DB_ID}


def test_trash_arguments_validate_against_live_schema():
    notion_validator("API-patch-page").validate({"page_id": NOTION_PAGE_ID, "in_trash": True})
    assert NOTION_PATCH_PAGE.sends == {"page_id", "in_trash"}


# ======================= Notion: error format (verified from 2.6.1 source) =======================


def test_notion_validation_error_is_permanent_and_never_copies_remote_text():
    world = FakeWorld("real")
    world.notion.notion_errors["API-post-page"] = [(400, "validation_error")]
    response = world.service.execute(request(single(WRITES[0])), write_authorized=True)
    assert response.status is ExecutionStatus.FAILED
    error = response.step_results[0].error
    assert error.code == "NOTION_API_ERROR" and not error.retryable and response.attempts == {"1": 1}
    assert "validation_error" in error.message and "SYNTHETIC-REMOTE-ERROR-TEXT" not in response.model_dump_json()


def test_notion_rate_limit_is_retried():
    world = FakeWorld("real")
    world.notion.notion_errors["API-post-page"] = [(429, "rate_limited")]
    response = world.service.execute(request(single(WRITES[0])), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED and response.attempts == {"1": 2}


@pytest.mark.parametrize("status", [500, 503])
def test_notion_server_error_on_a_write_is_outcome_unknown(status):
    world = FakeWorld("real")
    world.notion.notion_errors["API-post-page"] = [(status, "internal_server_error")]
    response = world.service.execute(request(single(WRITES[0])), write_authorized=True)
    assert response.step_results[0].error.code == "OUTCOME_UNKNOWN"
    assert len(world.notion.calls_named("API-post-page")) == 1


def test_notion_unauthorized_is_a_permission_error():
    world = FakeWorld("real")
    world.notion.notion_errors["API-post-page"] = [(401, "unauthorized")]
    response = world.service.execute(request(single(WRITES[0])), write_authorized=True)
    assert response.step_results[0].error.code == "NOTION_API_ERROR" and response.status is ExecutionStatus.FAILED


def test_notion_error_during_compensation_is_reported_as_compensation_failure():
    world = FakeWorld("real")
    world.teams.failures = [IntegrationError(ErrorKind.PERMANENT, "WEBHOOK_REJECTED", "x", request_sent=True)]
    world.notion.notion_errors["API-patch-page"] = [(404, "object_not_found")]
    response = world.service.execute(request(), write_authorized=True)
    assert response.status is ExecutionStatus.COMPENSATION_FAILED
    assert "COMPENSATION_NOTION_API_ERROR" in [e.code for e in response.errors]


# ======================= contract drift =======================


def test_drifted_tool_is_disabled_before_anything_is_sent():
    world = FakeWorld("real")
    schema = world.notion.schema("API-post-page")
    schema["required"] = [*schema["required"], "workspace_id"]  # server now requires something we never send
    response = world.service.execute(request(single(WRITES[0])), write_authorized=True)
    assert response.step_results[0].error.code == "MCP_CONTRACT_MISMATCH"
    assert world.notion.calls_named("API-post-page") == []


def test_closed_schema_that_drops_an_argument_we_send_is_a_mismatch():
    contract_schema = copy.deepcopy(fixture_tool(NOTION_FIXTURE, "API-patch-page")["inputSchema"])
    del contract_schema["properties"]["in_trash"]
    contract_schema["additionalProperties"] = False
    assert check_tool_schema(NOTION_PATCH_PAGE, contract_schema) == ["server schema does not accept: ['in_trash']"]
    open_schema = copy.deepcopy(fixture_tool(NOTION_FIXTURE, "API-post-page")["inputSchema"])
    del open_schema["properties"]["children"]
    assert check_tool_schema(NOTION_CREATE_PAGE, open_schema) == ["server schema does not declare: ['children']"]


# ======================= Google Drive (documented contract) =======================


def drive_validator(name: str):
    return jsonschema.Draft202012Validator(fixture_tool(DRIVE_FIXTURE, name)["inputSchema"])


def test_drive_create_and_search_arguments_match_documented_schemas():
    world = FakeWorld("real")
    create = prepared(world, Integration.GOOGLE_DRIVE, Action.CREATE_DOCUMENT, title="Doc", content="text")
    drive_validator("create_file").validate(create.arguments)
    assert "mimeType" not in create.arguments  # documented as DEPRECATED
    search = prepared(world, Integration.GOOGLE_DRIVE, Action.READ_DOCUMENT, document_name="Notes")
    drive_validator("search_files").validate(search.arguments)
    drive_validator("read_file_content").validate({"fileId": "x"})


@pytest.mark.parametrize("name, literal", [
    ("Plain", "'Plain'"),
    ("O'Brien notes", r"'O\'Brien notes'"),
    ("back\\slash", r"'back\\slash'"),
    ("' or title != '", r"'\' or title != \''"),  # query injection stays inside one literal
])
def test_drive_query_literals_are_escaped(name, literal):
    assert drive_query_literal(name) == literal


def test_drive_query_scopes_to_every_approved_folder_and_rejects_control_characters():
    assert drive_search_query("N", ["a", "b"]) == "title = 'N' and (parentId = 'a' or parentId = 'b')"
    with pytest.raises(Exception) as info:
        drive_query_literal("bad\nname")
    assert info.value.code == "INVALID_ARGUMENTS"


def test_drive_create_document_is_irreversible_with_the_official_server():
    world = FakeWorld("real")
    assert world.registry.effect_of(Integration.GOOGLE_DRIVE, Action.CREATE_DOCUMENT) is Effect.IRREVERSIBLE


@pytest.mark.parametrize("structured", [True, False])
def test_drive_results_work_as_structured_content_or_text_json(structured):
    world = FakeWorld("real")
    world.drive.drive_structured = structured
    response = world.service.execute(request([FOUR_STEPS[0], {**WRITES[1], "step_number": 2, "depends_on": [1]}]),
                                     write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED
    assert response.step_results[1].output["url"].startswith("https://docs.google.com/")


def test_drive_search_returning_an_empty_response_means_not_found():
    world = FakeWorld("real")
    steps = [{"step_number": 1, "integration": "google_drive", "action": "read_document",
              "arguments": {"document_name": "Missing document"}}]
    response = world.service.execute(request(steps))
    assert response.step_results[0].error.code == "DOCUMENT_NOT_FOUND"


def test_drive_read_only_allowlist_makes_create_unsupported():
    world = FakeWorld("real", google_drive_mcp_tool_allowlist="search_files,read_file_content")
    response = world.service.execute(request(single(WRITES[1])), write_authorized=True)
    assert [i.code for i in response.validation.issues] == ["UNSUPPORTED_BY_EXECUTOR"]
    assert world.remote_calls() == 0


def test_notion_without_trash_tool_makes_pages_irreversible():
    world = FakeWorld("real", notion_mcp_tool_allowlist="API-post-page")
    assert world.registry.effect_of(Integration.NOTION, Action.CREATE_PAGE) is Effect.IRREVERSIBLE


# ======================= configuration =======================


def test_notion_destinations_must_be_uuids():
    with pytest.raises(IntegrationConfigError, match="NOTION_DESTINATIONS"):
        load_integration_config(integration_settings(notion_destinations=json.dumps({"engineering": "not-a-uuid"}),
                                                     notion_task_database=None))
    compact = integration_settings(notion_destinations=json.dumps({"engineering": NOTION_PAGE_ID.replace("-", ""),
                                                                   "tasks": NOTION_DB_ID}))
    assert load_integration_config(compact).notion_pages.targets["engineering"] == NOTION_PAGE_ID


@pytest.mark.parametrize("host", ["synthetic.webhook.office.com", "outlook.office.com"])
def test_retired_office365_connector_webhooks_are_refused(host):
    settings = integration_settings(teams_webhook_urls=json.dumps({"synthetic-team": f"https://{host}/webhookb2/x"}))
    with pytest.raises(IntegrationConfigError, match="retired"):
        load_integration_config(settings)


# ======================= Teams (documented Workflows webhook) =======================


def test_teams_payload_matches_the_official_workflows_example():
    official = TEAMS_FIXTURE["official_example"]
    ours = teams_card("hello")
    assert ours["type"] == official["type"] == "message"
    ours_att, official_att = ours["attachments"][0], official["attachments"][0]
    assert set(ours_att) == set(official_att) and ours_att["contentUrl"] is None
    assert ours_att["contentType"] == official_att["contentType"]
    for key in ("$schema", "type", "version"):
        assert ours_att["content"][key] == official_att["content"][key]
    assert ours_att["content"]["body"] == [{"type": "TextBlock", "text": "hello", "wrap": True}]


@pytest.mark.parametrize("status", [200, 202])
def test_any_documented_success_status_is_accepted(status):
    client = WebhookClient(connect_timeout=1, timeout=1, http_transport=httpx.MockTransport(
        lambda r: httpx.Response(status)), sleeper=lambda _: None)
    assert client.post_json(WEBHOOK_URL, teams_card("x")).status_code == status


def test_webhook_posts_to_the_same_url_are_spaced_to_four_per_second():
    now, sleeps = [100.0], []
    client = WebhookClient(connect_timeout=1, timeout=1, http_transport=httpx.MockTransport(
        lambda r: httpx.Response(202)), clock=lambda: now[0], sleeper=sleeps.append)
    client.post_json(WEBHOOK_URL, {})
    now[0] += 0.05
    client.post_json(WEBHOOK_URL, {})
    client.post_json("https://other.example.invalid/hook", {})  # other URLs are independent
    assert sleeps == [pytest.approx(0.2)]


def test_workflows_url_signatures_are_redacted_from_telemetry_even_unregistered():
    text = json.dumps(sanitize({"note": f"POST {WEBHOOK_URL}"}))
    assert WEBHOOK_SIG not in text


# ======================= opt-in live verification script =======================


def test_verification_script_contacts_nothing_without_live_flag():
    world = FakeWorld("real")
    checks = verify(world.settings, live=False, teams_alias="synthetic-team",
                    mcp_transports={Integration.NOTION: lambda: world.notion,
                                    Integration.GOOGLE_DRIVE: lambda: world.drive},
                    webhook_sender=world.teams)
    assert {c.name: c.status for c in checks} == {
        "configuration": "PASS", "notion MCP discovery": "PLANNED",
        "google_drive MCP discovery": "PLANNED", "teams webhook post": "PLANNED"}
    assert world.notion.messages == [] and world.drive.messages == [] and world.teams.posts == []


def test_verification_script_live_discovery_never_calls_tools():
    world = FakeWorld("real")
    checks = verify(world.settings, live=True,
                    mcp_transports={Integration.NOTION: lambda: world.notion,
                                    Integration.GOOGLE_DRIVE: lambda: world.drive})
    assert all(c.status in ("PASS", "SKIPPED") for c in checks), [c.line() for c in checks]
    assert world.notion.tool_calls == [] and world.drive.tool_calls == []
    assert world.notion.closed == 1 and world.drive.closed == 1
    assert not [s for s in SECRETS if s in "\n".join(c.line() for c in checks)]


def test_verification_script_reports_contract_drift_by_tool_name_only():
    world = FakeWorld("real")
    world.notion.schema("API-patch-page")["required"] = ["page_id", "secret_new_field"]
    checks = {c.name: c for c in verify(world.settings, live=True,
                                        mcp_transports={Integration.NOTION: lambda: world.notion,
                                                        Integration.GOOGLE_DRIVE: lambda: world.drive})}
    assert checks["notion contract API-patch-page"].status == "FAIL"
    assert checks["notion contract API-post-page"].status == "PASS"


def test_verification_script_teams_post_needs_both_flags():
    sender = RecordingWebhookSender()
    # No MCP servers configured here: without injected fakes, --live discovery would try the
    # (synthetic) MCP URLs, which the conftest network guard blocks.
    settings = integration_settings("real", notion_mcp_url=None, google_drive_mcp_url=None)
    assert {c.name: c.status for c in verify(settings, live=True, teams_alias="synthetic-team",
                                             webhook_sender=sender)}["teams webhook post"] == "PLANNED"
    assert sender.posts == []
    checks = verify(settings, live=True, teams_alias="synthetic-team", approve_teams_post=True,
                    webhook_sender=sender)
    assert {c.name: c.status for c in checks}["teams webhook post"] == "PASS" and len(sender.posts) == 1
    assert {c.name: c.status for c in verify(settings, live=True, teams_alias="unknown",
                                             approve_teams_post=True)}["teams webhook post"] == "FAIL"
