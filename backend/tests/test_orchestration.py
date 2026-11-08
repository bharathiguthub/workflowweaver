"""Phase 7.2: Google Drive -> Gemini/Groq extraction -> Notion pages -> Microsoft Teams.

Every external client is a fake: Google Drive REST (FakeGoogle over httpx.MockTransport), the
Notion MCP server (FakeMcpServer), the Teams webhook (RecordingWebhookSender) and the Gemini /
Groq SDK clients. The conftest blocks every non-loopback connection. No real write happens.
"""

import json
import logging
import random
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools import FailureInjector, Fault
from app.agents.execution.tools.integrations import build_integration_registry
from app.agents.orchestration import (
    GeminiExtractor,
    GroqExtractor,
    MockExtractor,
    ProjectExtraction,
    review_extraction,
)
from app.agents.plan_registry import get_plan_registry
from app.agents.source_documents import CUSTOMER_REWARDS_PLAN, SOURCE_FIXTURES, SOURCE_NAME, sha256_text
from app.agents.validation import orchestration_issues, plan_digest, validate_draft
from app.api import integrations as integrations_api
from app.api import orchestration as orchestration_api
from app.api import planning as planning_api
from app.core.config import Settings
from app.main import app
from app.observability import set_telemetry
from app.schemas.execution import CompensationStatus, ExecutionRequest, ExecutionStatus, StepStatus
from app.schemas.plan import DraftPlan, DraftStep, StepArgument
from app.schemas.workflow import Integration
from app.streaming.broker import EventBroker
from app.streaming.runner import ExecutionRunner
from tests.drive_fakes import GOOGLE_SECRETS, SANDBOX_ID, FakeGoogle, drive_settings, write_token
from tests.execution_helpers import make_service
from tests.integration_fakes import NOTION_PAGE_ID, SECRETS, WEBHOOK_URL, FakeMcpServer, RecordingWebhookSender
from tests.telemetry_helpers import RecordingTelemetry
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, ok_response
from tests.test_planner_fallback import GROQ_MODEL, FakeGroqClient, gemini_503, groq_ok

PLAN_URL = "/api/v1/workflows/orchestration/plan"
SOURCES_URL = "/api/v1/workflows/orchestration/sources"
TARGET = "drive_notion_teams"
MOCK_SOURCE = "mock-src-crp-plan-v2"
DRAFT_SOURCE = "mock-src-crp-plan-v1"
REAL_SOURCE = "1SyntheticSourceDocument00001"
EXTRACTION = MockExtractor().extract(SOURCE_NAME, CUSTOMER_REWARDS_PLAN, expected_phases=5)
ALL_SECRETS = [*SECRETS, *GOOGLE_SECRETS]


def assert_no_secrets(text: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text, secret
    assert "logic.azure.com" not in text and "sig=" not in text


def orchestration_settings(mode: str, tmp_path, **overrides) -> Settings:
    values = dict(notion_real_writes_sandbox_only=True, smoke_test_notion_parent="engineering",
                  teams_webhook_urls=json.dumps({"sandbox": WEBHOOK_URL}), teams_default_channel="sandbox",
                  smoke_test_teams_channel="sandbox", teams_real_writes_sandbox_only=True,
                  planner_mode="gemini", gemini_model=GEMINI_MODEL, gemini_api_key="synthetic-gemini-key-000000000")
    values.update(overrides)
    return drive_settings(mode, tmp_path, **values)


class World:
    """Real/dry-run registry wired to fake Drive, Notion and Teams, plus the planning API."""

    def __init__(self, monkeypatch, tmp_path, mode="real", *, faults=None, **overrides) -> None:
        write_token(tmp_path)
        self.google = FakeGoogle()
        self.google.files[REAL_SOURCE] = {
            "id": REAL_SOURCE, "name": SOURCE_NAME, "mimeType": "text/markdown", "parents": [SANDBOX_ID],
            "trashed": False, "appProperties": {"workflowweaver_source": "true"}, "app_created": True,
            "content": CUSTOMER_REWARDS_PLAN.encode("utf-8")}
        self.notion = FakeMcpServer("notion")
        self.teams = RecordingWebhookSender()
        self.settings = orchestration_settings(mode, tmp_path, **overrides)
        self.registry = build_integration_registry(
            self.settings, faults=faults, mcp_transports={Integration.NOTION: lambda: self.notion},
            webhook_sender=self.teams, drive_http=self.google.client())
        self.sleeps: list[float] = []
        self.service = ExecutionService(
            self.registry, RetryPolicy(max_attempts=3, base_delay_seconds=0.2, max_delay_seconds=2.0, jitter=False),
            max_steps=10, sleeper=self.sleeps.append, rng=random.Random(7))
        monkeypatch.setattr(orchestration_api, "get_settings", lambda: self.settings)
        app.dependency_overrides[orchestration_api.get_orchestration_drive_http] = self.google.client
        self.gemini = FakeGenaiClient(ok_response(EXTRACTION))
        self.groq = FakeGroqClient(groq_ok(EXTRACTION))
        self.use_extractors()

    def use_extractors(self, gemini=None, groq=None) -> None:
        if gemini is not None:
            self.gemini = FakeGenaiClient(*gemini)
        if groq is not None:
            self.groq = FakeGroqClient(*groq)
        sources = [GeminiExtractor(client=self.gemini, model=GEMINI_MODEL, max_output_tokens=8192),
                   GroqExtractor(client=self.groq, model=GROQ_MODEL, max_tokens=8192)]
        app.dependency_overrides[orchestration_api.get_extraction_sources_factory] = lambda: (lambda _s, _m: sources)
        app.dependency_overrides[orchestration_api.get_orchestration_sleeper] = lambda: (lambda _s: None)

    def drive_writes(self):
        return self.google.writes

    def notion_calls(self, name: str):
        return self.notion.calls_named(name)


@pytest.fixture(autouse=True)
def _clean():
    get_plan_registry().clear()
    yield
    get_plan_registry().clear()
    app.dependency_overrides.clear()


def plan(mode="mock", source=MOCK_SOURCE, **body):
    return TestClient(app).post(PLAN_URL, json={"source_file_id": source, "mode": mode, **body})


def execution_request(body: dict, *, mode=None, digest="__plan__", steps=None, approved=True) -> ExecutionRequest:
    approval = {"approved": approved, "mode": mode or body["mode"], "approved_by": "synthetic.reviewer",
                "integration_target": TARGET}
    if digest == "__plan__":
        approval["plan_digest"] = body["plan_digest"]
    elif digest is not None:
        approval["plan_digest"] = digest
    return ExecutionRequest.model_validate({
        "workflow_id": body["workflow_id"],
        "plan": {"summary": body["summary"], "integration_target": TARGET,
                 "steps": steps if steps is not None else [
                     {k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
                     for s in body["planned_steps"]]},
        "approval": approval})


def step_rows(steps):
    return [(s["integration"], s["action"]) for s in steps]


EXPECTED_ROWS = [("google_drive", "read_document")] + [("notion", "create_page")] * 6 + [("teams", "send_message")]


# ======================= mock: complete workflow =======================


def test_mock_sources_are_listed_by_id_and_duplicate_names_are_flagged():
    response = TestClient(app).get(SOURCES_URL, params={"mode": "mock"})
    assert response.status_code == 200
    body = response.json()
    assert body["simulated"] is True and body["folder_alias"] == "sandbox"
    assert [(s["file_id"], s["duplicate_name"]) for s in body["sources"]] == [(MOCK_SOURCE, True), (DRAFT_SOURCE, True)]


def test_mock_plan_shows_source_extraction_destinations_and_checks():
    response = plan()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PLANNED" and body["integration_target"] == TARGET and body["executed"] is False
    assert body["planner"] == "mock (not AI)" and body["ai_generated"] is False
    assert step_rows(body["planned_steps"]) == EXPECTED_ROWS
    assert body["source"] == {"file_id": MOCK_SOURCE, "name": SOURCE_NAME, "mime_type": "text/markdown",
                              "size_bytes": len(CUSTOMER_REWARDS_PLAN.encode()), "modified_time": "2026-11-06T09:30:00Z",
                              "sha256": sha256_text(CUSTOMER_REWARDS_PLAN), "simulated": True}
    ext = body["extraction"]
    assert ext["project_title"] == "Customer Rewards Platform" and len(ext["phases"]) == 5
    first = ext["phases"][0]
    assert first["title"] == "Customer registration" and first["priority_source"] == "document"
    assert first["sections"]["acceptance_criteria"][0] == {
        "text": "A new customer can register and verify their email address", "in_source": True, "origin": "document"}
    assert ext["phases"][1]["owner_source"] == "none"
    assert [s["status"] for s in body["stages"]] == ["simulated", "simulated", "passed", "passed"]
    assert any("synthetic source fixture" in c for c in body["checks"]["simulated"])
    assert body["write_summary"] == {"notion_pages": 6, "teams_messages": 1, "drive_writes": 0}
    assert body["input_mode"] == "document_only" and body["instructions"] is None
    assert body["plan_digest"] == plan_digest(body["planned_steps"], TARGET, body["input_digest"])
    assert get_plan_registry().digest(body["workflow_id"]) == body["plan_digest"]


def test_mock_execution_completes_in_order_with_verification_and_linked_notification():
    body = plan().json()
    service, _ = make_service()
    response = service.execute(execution_request(body))
    assert response.status is ExecutionStatus.COMPLETED, response.errors
    assert [r.status for r in response.step_results] == [StepStatus.SUCCEEDED] * 8
    assert [r.output.get("verification") for r in response.step_results[1:7]] == ["verified"] * 6
    # Executed strictly in order: Drive read (1), six Notion pages (2-7), Teams last (8).
    assert [n for op, _key, n in service.registry.store.calls if op == "execute"] == list(range(1, 9))
    message = service.registry.store.messages[-1]
    assert message["links_page_from_step"] == 2 and f"Execution reference: {body['workflow_id']}" in message["text"]
    assert "Phases created: 5" in message["text"]


def test_sse_event_sequence_covers_approval_verification_and_notification():
    body = plan().json()
    service, _ = make_service()
    runner = ExecutionRunner(service, EventBroker(), max_workers=1)
    try:
        channel, future = runner.start(execution_request(body))
        future.result(timeout=10)
    finally:
        runner.shutdown()
    types = [e.event_type.value for e in channel.events()]
    assert types[:2] == ["approval_verified", "workflow_started"]
    assert types[2:4] == ["step_started", "step_succeeded"]  # Drive read (read-only: no verification)
    assert types.count("resource_verified") == 6
    assert types[-3:] == ["step_started", "step_succeeded", "workflow_completed"]
    verified = [e for e in channel.events() if e.event_type.value == "resource_verified"]
    assert {e.metadata["status"] for e in verified} == {"verified"}
    last = channel.events()[-2]
    assert last.metadata["integration"] == "teams" and last.step_number == 8


# ======================= dry run: no writes =======================


def test_dry_run_plans_with_ai_and_executes_without_any_write(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path, "dry_run")
    response = plan("dry_run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["planner"] == "gemini" and body["ai_generated"] is True and body["source"]["simulated"] is True
    assert body["destinations"] == {"drive_folder": "sandbox", "notion_parent": "engineering", "teams_channel": "sandbox"}
    assert any("dry run never contacts Google Drive" in c for c in body["checks"]["simulated"])
    result = world.service.execute(execution_request(body), write_authorized=False)
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    read = result.step_results[0].output
    assert read["source_check"] == "not performed (dry run)" and read["sent"] is False
    assert all(r.output["sent"] is False for r in result.step_results)
    assert [r.output.get("verification") for r in result.step_results[1:7]] == ["not_performed"] * 6
    assert result.step_results[-1].output["notion_link"].startswith("added at run time")
    assert world.google.calls == [] and world.notion.tool_calls == [] and world.teams.posts == []


# ======================= real (fake clients) =======================


def test_real_sources_list_only_app_accessible_markdown_in_the_sandbox(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    world.google.files["1SyntheticForeignDoc0000001"] = {**world.google.files[REAL_SOURCE],
                                                         "id": "1SyntheticForeignDoc0000001", "app_created": False}
    world.google.files["1SyntheticOutsideDoc0000001"] = {**world.google.files[REAL_SOURCE],
                                                         "id": "1SyntheticOutsideDoc0000001", "parents": ["elsewhere"]}
    world.google.add_folder("1SyntheticSubFolder00000001", "sub", parent=SANDBOX_ID)
    body = TestClient(app).get(SOURCES_URL, params={"mode": "real"}).json()
    assert body["simulated"] is False and [s["file_id"] for s in body["sources"]] == [REAL_SOURCE]
    assert world.drive_writes() == []


def test_real_workflow_reads_source_creates_verified_pages_then_notifies(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    response = plan("real", REAL_SOURCE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["real_write"] is True and body["source"]["simulated"] is False and body["checks"]["simulated"] == []
    assert world.drive_writes() == []  # planning only reads
    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    read = result.step_results[0].output
    assert read["source_verified"] is True and read["sha256"] == sha256_text(CUSTOMER_REWARDS_PLAN)
    pages = world.notion_calls("API-post-page")
    assert len(pages) == 6 and pages[0]["parent"] == {"page_id": NOTION_PAGE_ID}
    project_id = result.step_results[1].resource_id
    assert all(p["parent"] == {"page_id": project_id} for p in pages[1:])
    assert len(world.notion_calls("API-retrieve-a-page")) == 6
    assert [r.output["verification"] for r in result.step_results[1:7]] == ["verified"] * 6
    [(url, card)] = world.teams.posts
    assert url == WEBHOOK_URL
    texts = [b["text"] for b in card["attachments"][0]["content"]["body"]]
    assert f"https://www.notion.so/{project_id.replace('-', '')}" in texts[1]
    assert f"Execution reference: {body['workflow_id']}" in texts[0]
    teams = result.step_results[-1].output
    assert teams["delivery"] == "accepted_by_webhook" and teams["notion_link_included"] is True
    assert "delivered" not in teams
    # Order: every Notion call happened before the Teams post; the source was never modified.
    assert world.notion.tool_calls[-1][0] == "API-retrieve-a-page" and world.drive_writes() == []
    assert_no_secrets(result.model_dump_json() + response.text)


def test_real_plan_requires_an_ai_planner(monkeypatch, tmp_path):
    World(monkeypatch, tmp_path)
    app.dependency_overrides[orchestration_api.get_extraction_sources_factory] = lambda: (
        lambda _s, _m: [MockExtractor()])
    response = plan("real", REAL_SOURCE)
    assert response.status_code == 503 and "AI planner" in response.json()["detail"]


@pytest.mark.parametrize(("overrides", "setting"), [
    ({"real_writes_enabled": False}, "REAL_WRITES_ENABLED"),
    ({"teams_webhook_urls": None, "teams_default_channel": None}, "TEAMS_WEBHOOK_URLS"),
    ({"smoke_test_notion_parent": None}, "SMOKE_TEST_NOTION_PARENT"),
    ({"real_write_authorization_token": None}, "REAL_WRITE_AUTHORIZATION_TOKEN"),
])
def test_real_mode_needs_every_sandbox_and_gate(monkeypatch, tmp_path, overrides, setting):
    World(monkeypatch, tmp_path, **overrides)
    response = plan("real", REAL_SOURCE)
    assert response.status_code == 503 and setting in response.json()["detail"]


# ======================= source restrictions =======================


@pytest.mark.parametrize(("mutate", "status_code", "code"), [
    (lambda g: g.files[REAL_SOURCE].update(parents=["1SyntheticOtherFolder000001"]), 422, "SOURCE_NOT_IN_SANDBOX"),
    (lambda g: g.files[REAL_SOURCE].update(app_created=False), 404, "SOURCE_NOT_ACCESSIBLE"),
    (lambda g: g.files[REAL_SOURCE].update(mimeType="application/pdf", name="plan.pdf"), 422, "SOURCE_NOT_SUPPORTED"),
    (lambda g: g.files[REAL_SOURCE].update(trashed=True), 422, "SOURCE_NOT_SUPPORTED"),
    (lambda g: g.files[SANDBOX_ID]["appProperties"].clear(), 503, "SANDBOX_FOLDER_INVALID"),
])
def test_real_source_access_restrictions(monkeypatch, tmp_path, mutate, status_code, code):
    world = World(monkeypatch, tmp_path)
    mutate(world.google)
    response = plan("real", REAL_SOURCE)
    assert response.status_code == status_code, response.text
    assert response.json()["detail"]["code"] == code
    assert world.drive_writes() == [] and world.notion.tool_calls == []


@pytest.mark.parametrize(("source", "status_code", "code"), [
    ("mock-src-does-not-exist", 404, "SOURCE_NOT_FOUND"),
    ("../../etc/passwd/x", 422, "INVALID_IDENTIFIER"),
    ("https://drive.google.com/file/d/x", 422, "INVALID_IDENTIFIER"),
])
def test_missing_or_untrusted_source_ids(source, status_code, code):
    response = plan(source=source)
    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code


def test_ambiguous_name_is_resolved_by_id_and_the_wrong_file_is_rejected():
    response = plan(source=DRAFT_SOURCE)  # same name, older 4-phase draft
    assert response.status_code == 422
    assert [i["code"] for i in response.json()["validation"]["issues"]] == ["PHASE_COUNT_MISMATCH"]


def test_source_changed_after_approval_stops_before_any_write(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    body = plan("real", REAL_SOURCE).json()
    world.google.files[REAL_SOURCE]["content"] = (CUSTOMER_REWARDS_PLAN + "\nEdited later.").encode()
    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.status is ExecutionStatus.FAILED and result.errors[0].code == "SOURCE_CHANGED"
    assert world.notion_calls("API-post-page") == [] and world.teams.posts == []


def test_drive_mcp_transport_cannot_read_by_file_id():
    from app.agents.execution.tools.integrations import DRIVE_FILE_ID_UNSUPPORTED
    from tests.integration_fakes import FakeWorld

    world = FakeWorld("real")
    step = {"step_number": 1, "integration": "google_drive", "action": "read_document",
            "arguments": {"document_name": "x.md", "folder": "reports", "file_id": "1SyntheticFileId0000001"}}
    response = world.service.execute(ExecutionRequest.model_validate({
        "workflow_id": str(uuid4()), "plan": {"summary": "s", "steps": [step]},
        "approval": {"approved": True, "mode": "real", "approved_by": "synthetic.reviewer"}}), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert response.validation.issues[0].message == DRIVE_FILE_ID_UNSUPPORTED


# ======================= AI extraction: fallback and grounding =======================


def test_gemini_failure_falls_back_to_groq(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path, "dry_run")
    world.use_extractors(gemini=(gemini_503(),), groq=(groq_ok(EXTRACTION),))
    body = plan("dry_run").json()
    assert body["status"] == "PLANNED" and body["planner"] == "groq" and body["fallback_used"] is True
    assert body["stages"][2]["detail"].startswith("groq (openai/gpt-oss-120b) after fallback")
    prompt = world.groq.calls[0]["messages"][0]["content"]
    assert "never split, merge or invent phases" in prompt and "untrusted data inside <document> tags" in prompt


def test_invented_items_owners_and_phases_are_flagged_not_presented_as_facts():
    data = EXTRACTION.model_dump()
    data["phases"][0]["technical_tasks"].append("Migrate the mainframe billing system to quantum storage")
    data["phases"][1]["owner"] = "Growth team"
    review = review_extraction(ProjectExtraction.model_validate(data), CUSTOMER_REWARDS_PLAN, expected_phases=5)
    assert review.issues == []
    assert review.phases[0].items["technical_tasks"][-1] == (
        "Migrate the mainframe billing system to quantum storage", "unverified")
    assert (review.phases[1].owner, review.phases[1].owner_source) == ("Growth team", "suggested")
    assert len(review.warnings) == 2
    data["phases"][4]["title"] = "Blockchain loyalty tokens"
    review = review_extraction(ProjectExtraction.model_validate(data), CUSTOMER_REWARDS_PLAN, expected_phases=5)
    assert [i.code for i in review.issues] == ["PHASE_NOT_IN_SOURCE"]


def test_ungrounded_items_are_marked_on_the_notion_page(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path, "dry_run")
    data = EXTRACTION.model_dump()
    data["phases"][0]["deliverables"].append("A mobile app for smart watches")
    world.use_extractors(gemini=(ok_response(ProjectExtraction.model_validate(data)),))
    body = plan("dry_run").json()
    content = body["planned_steps"][2]["arguments"]["content"]
    assert "- A mobile app for smart watches [not found in source - review]" in content
    assert body["extraction"]["phases"][0]["sections"]["deliverables"][-1]["in_source"] is False
    assert any("smart watches" in w for w in body["warnings"])


# ======================= validation: actions, target, hierarchy, ordering =======================


def compiled_steps():
    body = plan().json()
    return [{k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
            for s in body["planned_steps"]], body


def as_draft(steps) -> DraftPlan:
    return DraftPlan(summary="s", steps=[
        DraftStep(step_number=s["step_number"], integration=s["integration"], action=s["action"],
                  description=s.get("description", ""), depends_on=s["depends_on"],
                  arguments=[StepArgument(name=k, value=v) for k, v in s["arguments"].items()]) for s in steps])


def rule_codes(steps) -> list[str]:
    validated, issues = validate_draft(as_draft(steps), max_steps=10)
    if issues:
        return [i.code for i in issues]
    return [i.code for i in orchestration_issues(validated.steps)]


def test_compiled_plan_satisfies_every_rule():
    steps, _ = compiled_steps()
    assert rule_codes(steps) == []


def _mutated(change):
    steps, _ = compiled_steps()
    change(steps)
    return steps


@pytest.mark.parametrize(("change", "code"), [
    (lambda s: s.insert(7, {"step_number": 8, "integration": "google_drive", "action": "create_document",
                            "arguments": {"title": "x", "content": "y", "folder": "sandbox"}, "depends_on": []}),
     "ACTION_NOT_ALLOWED_FOR_TARGET"),
    (lambda s: s[0]["arguments"].pop("expected_sha256"), "SOURCE_NOT_BOUND"),
    # a child page inside another child page (dependency kept consistent, so only the hierarchy rule fires)
    (lambda s: (s[4]["arguments"].update(parent_step="3"), s[4].update(depends_on=[3])), "NOTION_HIERARCHY_INVALID"),
    (lambda s: s[3]["arguments"].pop("parent_step"), "NOTION_HIERARCHY_INVALID"),
    (lambda s: s[7].update(depends_on=[2, 3, 4]), "TEAMS_BEFORE_NOTION"),
])
def test_orchestration_rules_reject_broken_plans(change, code):
    steps = _mutated(change)
    for i, s in enumerate(steps, start=1):
        s["step_number"] = i
    assert code in rule_codes(steps)


def test_teams_must_be_last():
    steps, _ = compiled_steps()
    teams = steps.pop()
    teams.update(step_number=7, depends_on=[2, 3, 4, 5, 6])
    last = steps.pop()
    last.update(step_number=8, depends_on=[2])
    codes = rule_codes([*steps, teams, last])
    assert "TEAMS_NOT_LAST" in codes and "TEAMS_BEFORE_NOTION" in codes


@pytest.mark.parametrize(("value", "code"), [("9", "INVALID_LINK_STEP"), ("1", "INVALID_LINK_STEP")])
def test_link_step_must_reference_an_earlier_notion_page(value, code):
    steps = _mutated(lambda s: s[7]["arguments"].update(link_step=value))
    assert code in rule_codes(steps)


@pytest.mark.parametrize(("args", "code"), [({"file_id": "../x"}, "INVALID_IDENTIFIER"),
                                            ({"expected_sha256": "abc"}, "INVALID_IDENTIFIER")])
def test_untrusted_identifiers_are_rejected(args, code):
    steps = _mutated(lambda s: s[0]["arguments"].update(args))
    assert code in rule_codes(steps)


def test_free_form_planning_cannot_use_the_orchestration_target():
    response = TestClient(app).post("/api/v1/workflows/plan", json={
        "instruction": "Read the plan from Drive and post it to Notion and Teams.", "mode": "mock", "target": TARGET})
    assert response.status_code == 422 and "orchestration/plan" in response.json()["detail"]


def test_executor_rejects_a_tampered_plan_even_with_the_digest():
    steps, body = compiled_steps()
    steps.append({"step_number": 9, "integration": "notion", "action": "create_task",
                  "arguments": {"title": "extra"}, "depends_on": []})
    service, _ = make_service()
    response = service.execute(execution_request(body, steps=steps))
    assert response.status is ExecutionStatus.REJECTED and service.registry.store.calls == []


# ======================= approval =======================


def test_missing_approval_is_rejected():
    body = plan().json()
    service, _ = make_service()
    response = service.execute(execution_request(body, approved=False))
    assert [e.code for e in response.errors] == ["APPROVAL_REQUIRED"] and service.registry.store.calls == []


@pytest.mark.parametrize("variant", ["no_digest", "wrong_digest", "changed_plan", "unrecorded"])
def test_approval_must_be_bound_to_the_exact_reviewed_plan(variant):
    steps, body = compiled_steps()
    kwargs = {}
    if variant == "no_digest":
        kwargs["digest"] = None
    elif variant == "wrong_digest":
        kwargs["digest"] = "f" * 64
    elif variant == "changed_plan":
        steps[7]["arguments"]["message"] = steps[7]["arguments"]["message"] + " (edited after review)"
        kwargs["steps"] = steps
    else:
        get_plan_registry().clear()
    service, _ = make_service()
    response = service.execute(execution_request(body, **kwargs))
    assert response.status is ExecutionStatus.REJECTED
    assert [e.code for e in response.errors] == ["APPROVAL_PLAN_MISMATCH"]
    assert service.registry.store.calls == []


def test_real_execution_still_needs_token_and_kill_switch(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    body = plan("real", REAL_SOURCE).json()
    response = world.service.execute(execution_request(body), write_authorized=False)
    assert [e.code for e in response.errors] == ["REAL_WRITE_AUTHORIZATION_REQUIRED"]
    assert world.notion.tool_calls == [] and world.teams.posts == []


# ======================= failure recovery =======================


def test_notion_partial_failure_compensates_only_this_runs_pages_and_never_notifies(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path, faults=FailureInjector({5: [Fault.PERMANENT]}))
    body = plan("real", REAL_SOURCE).json()
    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.status is ExecutionStatus.ROLLED_BACK
    statuses = [r.status for r in result.step_results]
    assert statuses[:4] == [StepStatus.SUCCEEDED] * 4 and statuses[4] == StepStatus.FAILED
    assert statuses[5:] == [StepStatus.SKIPPED] * 3
    assert [c.step_number for c in result.compensation_results] == [4, 3, 2]  # pages only, newest first
    assert {c.status for c in result.compensation_results} == {CompensationStatus.COMPENSATED}
    trashed = [a["page_id"] for a in world.notion_calls("API-patch-page")]
    assert trashed == [r.resource_id for r in reversed(result.step_results[1:4])]
    assert world.teams.posts == [] and world.drive_writes() == []  # source untouched, nothing posted


def test_failed_read_back_verification_fails_the_step_and_rolls_back(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    original = world.notion._api_retrieve_a_page
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        if calls["n"] == 3:  # third page reads back as trashed
            world.notion.resources[args["page_id"]]["in_trash"] = True
        return original(args)

    world.notion._api_retrieve_a_page = flaky
    body = plan("real", REAL_SOURCE).json()
    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.step_results[3].error.code == "VERIFICATION_FAILED"
    assert result.status is ExecutionStatus.ROLLED_BACK
    assert [c.step_number for c in result.compensation_results] == [4, 3, 2]
    assert world.teams.posts == []


def test_teams_failure_after_verified_notion_pages_keeps_them_and_reports_partial_success(monkeypatch, tmp_path):
    from app.agents.execution.tools.base import ErrorKind
    from app.integrations.errors import IntegrationError

    world = World(monkeypatch, tmp_path)
    world.teams.failures = [IntegrationError(ErrorKind.PERMISSION, "WEBHOOK_UNAUTHORIZED", "synthetic",
                                             request_sent=True)]
    body = plan("real", REAL_SOURCE).json()
    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.status is ExecutionStatus.PARTIALLY_COMPLETED
    assert [r.status for r in result.step_results[:7]] == [StepStatus.SUCCEEDED] * 7
    assert result.step_results[7].status is StepStatus.FAILED and result.compensation_results == []
    assert world.notion_calls("API-patch-page") == []  # verified pages are kept
    assert "cannot be unsent" in result.message and "kept" in result.message


def test_ambiguous_teams_timeout_is_not_retried_and_pages_are_kept(monkeypatch, tmp_path):
    from app.agents.execution.tools.base import ErrorKind
    from app.integrations.errors import IntegrationError

    world = World(monkeypatch, tmp_path)
    world.teams.failures = [IntegrationError(ErrorKind.TRANSIENT, "WEBHOOK_TIMEOUT", "synthetic", request_sent=True)]
    body = plan("real", REAL_SOURCE).json()
    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.status is ExecutionStatus.PARTIALLY_COMPLETED
    assert result.step_results[7].error.code == "OUTCOME_UNKNOWN" and len(world.teams.posts) == 1


def test_transient_notion_failure_is_retried_with_backoff(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path, faults=FailureInjector({3: [Fault.TRANSIENT]}))
    body = plan("real", REAL_SOURCE).json()
    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.status is ExecutionStatus.COMPLETED and result.attempts["3"] == 2 and world.sleeps == [0.2]


def test_duplicate_execution_replays_without_new_writes(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    body = plan("real", REAL_SOURCE).json()
    request = execution_request(body)
    first = world.service.execute(request, write_authorized=True)
    second = world.service.execute(request, write_authorized=True)
    assert first.status is second.status is ExecutionStatus.COMPLETED and second.replayed is True
    assert len(world.notion_calls("API-post-page")) == 6 and len(world.teams.posts) == 1


# ======================= secrets, Langfuse, status, regressions =======================


def test_langfuse_records_planning_stages_and_tool_outcomes_without_secrets(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    telemetry = RecordingTelemetry()
    set_telemetry(telemetry)
    world = World(monkeypatch, tmp_path)
    body = plan("real", REAL_SOURCE).json()
    world.service.execute(execution_request(body), write_authorized=True)
    names = [e["name"] for e in telemetry.events]
    for stage in ("orchestration.source_validation", "orchestration.document_read", "orchestration.extraction",
                  "orchestration.plan_validation"):
        assert stage in names
    extraction = telemetry.events_named("orchestration.extraction")[0]["metadata"]
    assert extraction["provider"] == "gemini" and extraction["model"] == GEMINI_MODEL and extraction["phase_count"] == 5
    generation = telemetry.one("gemini.models.generate_content")
    assert generation.model == GEMINI_MODEL and generation.input["workflow"] == "drive_notion_teams"
    assert len(telemetry.named("tool.notion.create_page")) == 6
    assert telemetry.one("teams.webhook.post").output["http_status"] == 202
    dump = telemetry.dump()
    assert_no_secrets(dump + caplog.text)
    assert "Build the registration API" not in dump  # document content never reaches traces


def test_status_reports_orchestration_readiness(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations_api, "get_settings", lambda: world.settings)
    response = TestClient(app).get("/api/v1/integrations/status")
    assert response.status_code == 200
    ready = {"mock_ready": True, "dry_run_ready": True, "real_ready": True, "issues": []}
    assert response.json()["orchestration"] == {**ready, "instructions_only": ready}
    assert_no_secrets(response.text)
    monkeypatch.setattr(integrations_api, "get_settings", lambda: Settings(_env_file=None))
    assert TestClient(app).get("/api/v1/integrations/status").json()["orchestration"]["mock_ready"] is True


def test_single_integration_targets_stay_restricted():
    from app.schemas.plan import target_integrations

    assert target_integrations("google_drive") == {"google_drive"}
    assert target_integrations("notion") == {"notion"}
    assert target_integrations("teams") == {"teams"}
    assert target_integrations(TARGET) == {"google_drive", "notion", "teams"}
    assert target_integrations("multi") is None


def test_mock_fixture_ids_are_valid_identifiers():
    from app.agents.validation import DRIVE_FILE_ID

    assert all(DRIVE_FILE_ID.fullmatch(i) for i in SOURCE_FIXTURES)


def test_upload_source_script_needs_confirm_marks_the_source_and_cleanup_keeps_it(tmp_path, capsys):
    from scripts import google_drive_sandbox as script

    write_token(tmp_path)
    google = FakeGoogle()
    settings = drive_settings("real", tmp_path)
    assert script.upload_source(settings, confirm=False, http=google.client()) == 2
    assert google.writes == []
    assert script.upload_source(settings, confirm=True, http=google.client()) == 0
    [uploaded] = [f for f in google.files.values() if f["name"] == SOURCE_NAME]
    assert uploaded["parents"] == [SANDBOX_ID] and uploaded["appProperties"] == {"workflowweaver_source": "true"}
    assert uploaded["content"] == CUSTOMER_REWARDS_PLAN.encode()
    assert script.cleanup(settings, confirm=True, http=google.client()) == 0
    assert uploaded["trashed"] is False  # sources are never cleaned up
    assert_no_secrets(capsys.readouterr().out)
