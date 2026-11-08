"""Integration-specific planning and execution: Google Drive Only / Notion Only / Teams Only /
Multi-Integration.

Covered: the target reaches every planner's prompt (Gemini, Groq, Claude) but is ENFORCED by
the server-side validator; Gemini -> Groq fallback output is held to the same target; the
executor rejects approval/plan target mismatches and relabelled plans (server-side record),
re-validates the allowlist before execution and again before every tool call; the Drive-only
sandbox workflow runs in mock, dry-run and real (fake Google) without touching Notion; Notion
workflows are unchanged. No real Notion or Google Drive call is made.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.execution.nodes import ExecutionNodes
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.tools import MockToolRegistry
from app.agents.plan_registry import PlanTargetRegistry, get_plan_registry
from app.agents.planners import FallbackPlanner, GeminiPlanner, GroqPlanner
from app.agents.planners.claude import build_system_prompt
from app.agents.service import PlanningService
from app.agents.validation import integration_target_issues
from app.api import drive_sandbox as drive_api
from app.api import execution as execution_api
from app.api import integrations as integrations_api
from app.api import planning as planning_api
from app.api import sandbox as notion_sandbox_api
from app.main import app
from app.observability import set_telemetry
from app.schemas.execution import ExecutionRequest, ExecutionStatus
from app.schemas.plan import DraftPlan, IntegrationTarget, PlanStatus, target_integrations
from tests.conftest import EXAMPLE_INSTRUCTION, make_step
from tests.drive_fakes import DriveWorld, drive_settings, write_token
from tests.execution_helpers import APPROVAL, FOUR_STEPS, make_service
from tests.integration_fakes import integration_settings
from tests.telemetry_helpers import RecordingTelemetry
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, ok_response
from tests.test_planner_fallback import GROQ_MODEL, FakeGroqClient, gemini_503, groq_ok

PLAN_URL = "/api/v1/workflows/plan"
EXECUTE_URL = "/api/v1/workflows/execute"
DRIVE_PLAN_URL = "/api/v1/workflows/drive-sandbox/plan"
NOTION_SANDBOX_URL = "/api/v1/workflows/notion-sandbox/plan"

DRIVE_ONLY = DraftPlan(summary="Drive only", steps=[
    make_step(1, "google_drive", "create_document", title="Synthetic report", content="Synthetic"),
])
MIXED = DraftPlan(summary="Mixed", steps=[
    make_step(1, "notion", "create_page", title="Synthetic page", content="Synthetic"),
    make_step(2, "google_drive", "create_document", title="Synthetic report", content="Synthetic"),
])
NOTION_ONLY = DraftPlan(summary="Notion only", steps=[
    make_step(1, "notion", "create_page", title="Synthetic page", content="Synthetic"),
])


@pytest.fixture(autouse=True)
def _clean():
    get_plan_registry().clear()
    yield
    get_plan_registry().clear()
    app.dependency_overrides.clear()


# ======================= target model and validator =======================


@pytest.mark.parametrize("target, allowed", [
    ("google_drive", {"google_drive"}), ("notion", {"notion"}), ("teams", {"teams"}), ("multi", None), (None, None),
])
def test_target_integrations(target, allowed):
    result = target_integrations(target)
    assert (set(result) if result is not None else None) == allowed


def test_validator_names_each_step_outside_the_target():
    steps = [s.model_dump() | {"integration": s.integration} for s in MIXED.steps]
    issues = integration_target_issues(steps, frozenset({"google_drive"}))
    assert [(i.code, i.step_number) for i in issues] == [("INTEGRATION_NOT_ALLOWED", 1)]
    assert integration_target_issues(steps, None) == []


# ======================= prompts: guidance only, filtered per target =======================


def test_prompt_lists_only_the_target_integration_tools():
    prompt = build_system_prompt(10, None, frozenset({"google_drive"}))
    assert "integration=google_drive action=create_folder" in prompt
    assert "integration=google_drive action=list_files" in prompt
    assert "integration=notion" not in prompt and "integration=teams" not in prompt
    assert "may use ONLY google_drive" in prompt
    assert "integration=notion" in build_system_prompt(10)  # unchanged without a target


def test_gemini_receives_the_filtered_prompt():
    client = FakeGenaiClient(ok_response(DRIVE_ONLY))
    planner = GeminiPlanner(client=client, model=GEMINI_MODEL, max_output_tokens=1024)
    result = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION,
                                                                                  target="google_drive")
    assert result.status is PlanStatus.PLANNED and result.integration_target is IntegrationTarget.GOOGLE_DRIVE
    system = client.models.calls[0]["config"].system_instruction
    assert "integration=notion" not in system and "may use ONLY google_drive" in system


# ======================= server-side enforcement for every planner =======================


def test_gemini_plan_outside_the_target_is_rejected_even_though_the_prompt_asked_otherwise():
    planner = GeminiPlanner(client=FakeGenaiClient(ok_response(MIXED)), model=GEMINI_MODEL, max_output_tokens=1024)
    result = PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION,
                                                                                  target="google_drive")
    assert result.status is PlanStatus.INVALID and result.planned_steps == []
    assert [(i.code, i.step_number) for i in result.validation.issues] == [("INTEGRATION_NOT_ALLOWED", 1)]


def _fallback(groq_plan: DraftPlan) -> tuple[FallbackPlanner, FakeGroqClient]:
    groq_client = FakeGroqClient(groq_ok(groq_plan))
    return FallbackPlanner([
        GeminiPlanner(client=FakeGenaiClient(gemini_503()), model=GEMINI_MODEL, max_output_tokens=1024),
        GroqPlanner(client=groq_client, model=GROQ_MODEL, max_tokens=1024),
    ]), groq_client


def test_groq_fallback_plan_outside_the_target_is_rejected():
    planner, groq_client = _fallback(MIXED)
    result = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION,
                                                                                  target="google_drive")
    assert result.planner == "groq" and result.fallback_used is True
    assert result.status is PlanStatus.INVALID
    assert [i.code for i in result.validation.issues] == ["INTEGRATION_NOT_ALLOWED"]
    groq_prompt = groq_client.calls[0]["messages"][0]["content"]
    assert "may use ONLY google_drive" in groq_prompt and "integration=notion" not in groq_prompt


def test_groq_fallback_drive_only_plan_is_planned_for_the_drive_target():
    planner, _ = _fallback(DRIVE_ONLY)
    result = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION,
                                                                                  target="google_drive")
    assert result.status is PlanStatus.PLANNED and result.planner == "groq"
    assert {s.integration.value for s in result.planned_steps} == {"google_drive"}


def test_notion_target_rejects_drive_steps_from_groq():
    planner, _ = _fallback(MIXED)
    result = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION,
                                                                                  target="notion")
    assert [(i.code, i.step_number) for i in result.validation.issues] == [("INTEGRATION_NOT_ALLOWED", 2)]


def test_multi_target_keeps_the_previous_behaviour():
    planner, _ = _fallback(MIXED)
    result = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION,
                                                                                  target="multi")
    assert result.status is PlanStatus.PLANNED and result.integration_target is IntegrationTarget.MULTI


def test_langfuse_records_the_target():
    recorder = RecordingTelemetry()
    set_telemetry(recorder)
    planner, _ = _fallback(MIXED)
    PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION, target="google_drive")
    request = recorder.one("planning.request")
    assert request.input["integration_target"] == "google_drive"
    assert request.output["validation_issue_codes"] == ["INTEGRATION_NOT_ALLOWED"]


# ======================= /plan endpoint (mock planner) =======================


def test_plan_endpoint_drive_only_with_the_mock_planner():
    client = TestClient(app)
    drive = client.post(PLAN_URL, json={"instruction": "Save a summary document to Google Drive.", "mode": "mock",
                                        "target": "google_drive"})
    assert drive.status_code == 200, drive.text
    body = drive.json()
    assert body["integration_target"] == "google_drive"
    assert {s["integration"] for s in body["planned_steps"]} == {"google_drive"}
    assert get_plan_registry().get(body["workflow_id"]) == "google_drive"


def test_plan_endpoint_rejects_notion_steps_for_drive_only():
    response = TestClient(app).post(PLAN_URL, json={
        "instruction": "Create a Notion project page and save a summary to Google Drive.", "target": "google_drive",
        "mode": "mock"})
    assert response.status_code == 422
    assert "INTEGRATION_NOT_ALLOWED" in [i["code"] for i in response.json()["validation"]["issues"]]


def test_plan_endpoint_without_target_is_unchanged():
    response = TestClient(app).post(PLAN_URL, json={
        "instruction": "Create a Notion project page and save a summary to Google Drive."})
    assert response.status_code == 200 and response.json()["integration_target"] is None


def test_unknown_target_is_rejected():
    response = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "target": "jira"})
    assert response.status_code == 422


# ======================= execution: target mismatch and allowlist =======================


@pytest.fixture
def mock_execution():
    service, _ = make_service()
    app.dependency_overrides[execution_api.get_execution_service] = lambda: service
    return TestClient(app), service


def body(steps, *, plan_target=None, approved_target=None, workflow_id=None):
    plan = {"summary": "Synthetic plan", "steps": steps}
    if plan_target:
        plan["integration_target"] = plan_target
    approval = dict(APPROVAL)
    if approved_target:
        approval["integration_target"] = approved_target
    return {"workflow_id": workflow_id or str(uuid4()), "plan": plan, "approval": approval}


DRIVE_STEPS = [
    {"step_number": 1, "integration": "google_drive", "action": "read_document",
     "arguments": {"document_name": "Synthetic standup notes"}},
    {"step_number": 2, "integration": "google_drive", "action": "create_document",
     "arguments": {"title": "Sprint 42 summary", "content": "Synthetic summary"}, "depends_on": [1]},
]


def test_approval_for_another_target_is_rejected_with_403(mock_execution):
    client, service = mock_execution
    response = client.post(EXECUTE_URL, json=body(DRIVE_STEPS, plan_target="google_drive", approved_target="notion"))
    assert response.status_code == 403
    assert [e["code"] for e in response.json()["errors"]] == ["INTEGRATION_TARGET_MISMATCH"]
    assert service.registry.store.calls == []


def test_relabelled_plan_is_rejected_using_the_server_record(mock_execution):
    """Planned as Drive-only; the client adds a Notion step and relabels the plan 'multi'."""
    client, service = mock_execution
    workflow_id = str(uuid4())
    get_plan_registry().record(workflow_id, "google_drive")
    response = client.post(EXECUTE_URL, json=body(FOUR_STEPS, plan_target="multi", approved_target="multi",
                                                  workflow_id=workflow_id))
    assert response.status_code == 403
    assert [e["code"] for e in response.json()["errors"]] == ["INTEGRATION_TARGET_MISMATCH"]
    assert service.registry.store.calls == []


def test_recorded_drive_target_rejects_a_notion_step_even_without_a_label(mock_execution):
    client, service = mock_execution
    workflow_id = str(uuid4())
    get_plan_registry().record(workflow_id, "google_drive")
    response = client.post(EXECUTE_URL, json=body(FOUR_STEPS, workflow_id=workflow_id))
    assert response.status_code == 422
    assert [e["code"] for e in response.json()["errors"]] == ["INVALID_PLAN"]
    assert {i["code"] for i in response.json()["validation"]["issues"]} == {"INTEGRATION_NOT_ALLOWED"}
    assert service.registry.store.calls == []


def test_plan_labelled_drive_only_with_a_notion_step_is_rejected(mock_execution):
    client, service = mock_execution
    response = client.post(EXECUTE_URL, json=body(FOUR_STEPS, plan_target="google_drive",
                                                  approved_target="google_drive"))
    assert response.status_code == 422
    issues = response.json()["validation"]["issues"]
    assert [(i["code"], i["step_number"]) for i in issues] == [("INTEGRATION_NOT_ALLOWED", 2),
                                                                ("INTEGRATION_NOT_ALLOWED", 4)]
    assert service.registry.store.calls == []


def test_target_checks_run_before_the_approval_check(mock_execution):
    client, _ = mock_execution
    payload = body(DRIVE_STEPS, plan_target="google_drive", approved_target="notion")
    payload["approval"]["approved"] = False
    assert client.post(EXECUTE_URL, json=payload).json()["errors"][0]["code"] == "INTEGRATION_TARGET_MISMATCH"


def test_matching_drive_target_executes_drive_steps_only(mock_execution):
    client, service = mock_execution
    response = client.post(EXECUTE_URL, json=body(DRIVE_STEPS, plan_target="google_drive",
                                                  approved_target="google_drive"))
    assert response.status_code == 200 and response.json()["status"] == "COMPLETED"
    assert {r["integration"] for r in response.json()["step_results"]} == {"google_drive"}


def test_legacy_requests_without_targets_are_unchanged(mock_execution):
    client, _ = mock_execution
    assert client.post(EXECUTE_URL, json=body(FOUR_STEPS)).json()["status"] == "COMPLETED"


def test_every_tool_call_is_rechecked_against_the_target():
    """Defence in depth: even if a step outside the target reached execute_step, no tool runs."""
    registry = MockToolRegistry()
    nodes = ExecutionNodes(registry, RetryPolicy(max_attempts=1, base_delay_seconds=0, jitter=False),
                           sleeper=lambda _: None)
    state = {
        "workflow_id": "wf-recheck", "current_index": 0, "attempts": {}, "write_authorized": False,
        "step_results": [], "allowed_integrations": ["google_drive"], "mode": "mock",
        "steps": [{"step_number": 1, "integration": "notion", "action": "create_page", "description": "d",
                   "arguments": {"title": "t", "content": "c"}, "depends_on": []}],
    }
    update = nodes.execute_step_node(state)
    assert update["step_results"][0]["error"]["code"] == "INTEGRATION_NOT_ALLOWED"
    assert registry.store.calls == []


def test_registry_is_bounded():
    registry = PlanTargetRegistry(max_entries=2)
    for n in range(3):
        registry.record(f"wf-{n}", "google_drive")
    assert registry.get("wf-0") is None and registry.get("wf-2") == "google_drive"


# ======================= Drive-only sandbox workflow =======================


@pytest.fixture
def drive_api_settings(monkeypatch, tmp_path):
    def install(mode="real"):
        write_token(tmp_path)
        settings = drive_settings(mode, tmp_path)
        for module in (drive_api, integrations_api, notion_sandbox_api):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
        return settings
    return install


def _request_from(plan: dict, *, approved_target: str | None = "google_drive", mode: str = "real") -> ExecutionRequest:
    return ExecutionRequest.model_validate({
        "workflow_id": plan["workflow_id"],
        "plan": {"summary": plan["summary"], "integration_target": plan["integration_target"], "steps": [
            {k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
            for s in plan["planned_steps"]]},
        "approval": {"approved": True, "mode": mode, "approved_by": "synthetic.reviewer",
                     **({"integration_target": approved_target} if approved_target else {})}})


def test_drive_only_plan_is_recorded_and_has_only_drive_actions(drive_api_settings):
    drive_api_settings("real")
    plan = TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "real", "target": "google_drive"}).json()
    assert plan["integration_target"] == "google_drive"
    assert [s["action"] for s in plan["planned_steps"]] == ["create_folder", "create_document",
                                                             "read_document"]
    assert {s["integration"] for s in plan["planned_steps"]} == {"google_drive"}
    assert get_plan_registry().get(plan["workflow_id"]) == "google_drive"


def test_drive_endpoint_refuses_another_target(drive_api_settings):
    drive_api_settings("real")
    assert TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "real", "target": "notion"}).status_code == 422
    assert TestClient(app).post(NOTION_SANDBOX_URL, json={"mode": "mock", "target": "google_drive"}).status_code == 422


def test_drive_only_real_run_against_fake_google_never_touches_notion(drive_api_settings, tmp_path):
    drive_api_settings("real")
    plan = TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "real", "target": "google_drive"}).json()
    world = DriveWorld(tmp_path)
    response = world.service.execute(_request_from(plan), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED, response.errors
    folder, report, read = response.step_results
    assert report.output["verified"] is True
    assert read.output["md5"] == report.output["md5"] and read.output["content_verified"] is True  # read-back
    assert {r.integration for r in response.step_results} == {"google_drive"}
    assert all(not path.startswith("/v1/") for _, path in world.google.calls)  # only Google endpoints


def test_drive_only_plan_with_a_notion_approval_is_rejected(drive_api_settings, tmp_path):
    drive_api_settings("real")
    plan = TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "real", "target": "google_drive"}).json()
    world = DriveWorld(tmp_path)
    response = world.service.execute(_request_from(plan, approved_target="notion"), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [e.code for e in response.errors] == ["INTEGRATION_TARGET_MISMATCH"] and world.google.calls == []


def test_drive_only_plan_still_needs_approval_token_and_kill_switch(drive_api_settings, tmp_path):
    drive_api_settings("real")
    plan = TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "real", "target": "google_drive"}).json()
    unapproved = _request_from(plan)
    unapproved = unapproved.model_copy(update={"approval": unapproved.approval.model_copy(update={"approved": False})})
    world = DriveWorld(tmp_path)
    assert [e.code for e in world.service.execute(unapproved, write_authorized=True).errors] == ["APPROVAL_REQUIRED"]
    assert [e.code for e in world.service.execute(_request_from(plan), write_authorized=False).errors] == [
        "REAL_WRITE_AUTHORIZATION_REQUIRED"]
    off = DriveWorld(tmp_path, real_writes_enabled=False)
    assert [e.code for e in off.service.execute(_request_from(plan), write_authorized=True).errors] == [
        "REAL_WRITES_DISABLED"]
    assert world.google.calls == [] and off.google.calls == []


def test_drive_only_dry_run_sends_nothing(drive_api_settings, tmp_path):
    drive_api_settings("real")
    plan = TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "dry_run", "target": "google_drive"}).json()
    world = DriveWorld(tmp_path, mode="dry_run", authorize=False)
    response = world.service.execute(_request_from(plan, mode="dry_run"))
    assert response.status is ExecutionStatus.COMPLETED and world.google.calls == []
    assert all(r.output["sent"] is False for r in response.step_results)


def test_drive_only_mock_run_creates_no_notion_resources(drive_api_settings):
    drive_api_settings("real")
    plan = TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "mock", "target": "google_drive"}).json()
    service, _ = make_service()
    response = service.execute(_request_from(plan, mode="mock"))
    assert response.status is ExecutionStatus.COMPLETED
    assert response.step_results[2].output["document_name"] == plan["report_title"]
    assert response.step_results[2].output["content_verified"] is True
    assert {r["integration"] for r in service.registry.store.resources.values()} == {"google_drive"}


def test_drive_only_failure_rolls_back_and_streams_events(drive_api_settings, tmp_path):
    from app.streaming.broker import EventBroker
    from app.streaming.runner import ExecutionRunner

    drive_api_settings("real")
    plan = TestClient(app).post(DRIVE_PLAN_URL, json={"mode": "real", "target": "google_drive"}).json()
    world = DriveWorld(tmp_path)
    world.google.failures[("GET", r"^/drive/v3/files$")] = [None, None, 404]  # dedup x2, then listing fails
    runner = ExecutionRunner(world.service, EventBroker(), max_workers=1)
    try:
        channel, future = runner.start(_request_from(plan), write_authorized=True)
        future.result(timeout=10)
    finally:
        runner.shutdown()
    events = [e.event_type.value for e in channel.events()]
    assert events[0] == "workflow_started" and events[-1] == "workflow_failed"
    assert events.count("compensation_completed") == 2
    assert all(f["trashed"] for fid, f in world.google.files.items() if f["appProperties"].get("workflowweaver_run"))


# ======================= Notion Only is unchanged =======================


def test_notion_sandbox_plan_is_notion_only_and_executes_with_a_notion_approval():
    response = TestClient(app).post(NOTION_SANDBOX_URL, json={"mode": "mock", "target": "notion"})
    plan = response.json()
    assert response.status_code == 200 and plan["integration_target"] == "notion"
    service, _ = make_service()
    result = service.execute(_request_from(plan, approved_target="notion", mode="mock"))
    assert result.status is ExecutionStatus.COMPLETED
    assert {r.integration for r in result.step_results} == {"notion"}


def test_notion_sandbox_without_target_field_still_works():
    plan = TestClient(app).post(NOTION_SANDBOX_URL, json={"mode": "mock"}).json()
    assert plan["integration_target"] == "notion"
    service, _ = make_service()
    legacy = _request_from(plan, approved_target=None, mode="mock")
    assert service.execute(legacy).status is ExecutionStatus.COMPLETED


def test_notion_project_plan_records_the_notion_target(monkeypatch):
    from app.agents.project_workflow import EXAMPLE_INSTRUCTION as PROJECT_INSTRUCTION
    from app.api import project as project_api

    settings = integration_settings("mock")
    monkeypatch.setattr(project_api, "get_settings", lambda: settings)
    plan = TestClient(app).post("/api/v1/workflows/notion-project/plan",
                                json={"instruction": PROJECT_INSTRUCTION, "mode": "mock", "target": "notion"}).json()
    assert plan["integration_target"] == "notion" and get_plan_registry().get(plan["workflow_id"]) == "notion"
    bad = TestClient(app).post("/api/v1/workflows/notion-project/plan",
                               json={"instruction": PROJECT_INSTRUCTION, "mode": "mock", "target": "google_drive"})
    assert bad.status_code == 422


def test_planning_api_service_is_not_affected_by_targets(monkeypatch):
    planning_api._default_service.cache_clear()
    assert planning_api._default_service().planner.name == "mock"
