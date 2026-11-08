"""Phase 5.4: per-request integration mode under the server ceiling, the Notion Sandbox Test
plan, Notion readiness/connectivity, and the full UI flow (plan -> approval -> execution ->
result) against a fake Notion MCP server. No real service is called."""

import random
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.execution.modes import ModeRoutedExecutionService, allowed_modes
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService, IdempotencyConflict, ModeMismatch
from app.agents.execution.tools import MockToolRegistry
from app.agents.execution.tools.base import ErrorKind
from app.agents.planners.mock import MockPlanner
from app.agents.service import PlanningService
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.api import execution as execution_api
from app.api import integrations as integrations_api
from app.api import planning as planning_api
from app.api import sandbox as sandbox_api
from app.api import streaming as streaming_api
from app.core.config import IntegrationMode, Settings
from app.integrations.errors import IntegrationError
from app.main import app
from app.schemas.execution import ExecutionRequest, ExecutionStatus
from app.streaming.broker import EventBroker
from app.streaming.runner import ExecutionRunner
from tests.integration_fakes import NOTION_PAGE_ID, OPERATOR_TOKEN, SECRETS, FakeWorld, integration_settings

SANDBOX_PLAN = "/api/v1/workflows/notion-sandbox/plan"
EXECUTE = "/api/v1/workflows/execute"
EXECUTIONS = "/api/v1/workflows/executions"
STATUS = "/api/v1/integrations/status"
CONNECTIVITY = "/api/v1/integrations/notion/connectivity"
AUTH = {"X-Integration-Authorization": OPERATOR_TOKEN}


def assert_no_secrets(text: str) -> None:
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"secret values leaked: {leaked}"


def mock_service() -> ExecutionService:
    return ExecutionService(MockToolRegistry(), RetryPolicy(max_attempts=3, base_delay_seconds=0, jitter=False),
                            max_steps=10, sleeper=lambda _: None, rng=random.Random(1))


class Routed:
    """A mode-routed service whose dry_run/real services talk to a fake Notion world."""

    def __init__(self, ceiling: str = "real", **overrides) -> None:
        self.settings = sandbox_settings(ceiling, **overrides)
        self.worlds: dict[str, FakeWorld] = {}
        self.built: list[str] = []
        self.service = ModeRoutedExecutionService(IntegrationMode(ceiling), self._build)

    def _build(self, mode: IntegrationMode) -> ExecutionService:
        self.built.append(mode.value)
        if mode is IntegrationMode.MOCK:
            return mock_service()
        world = self.worlds[mode.value] = FakeWorld(mode.value, smoke_test_notion_parent="engineering",
                                                    real_writes_enabled=self.settings.real_writes_enabled)
        return world.service

    @property
    def notion(self):
        return self.worlds["real"].notion


def sandbox_settings(mode: str = "real", **overrides) -> Settings:
    return integration_settings(mode, **{"smoke_test_notion_parent": "engineering", **overrides})


@pytest.fixture
def api(monkeypatch):
    """Patch every module's settings and wire a routed service into both execution endpoints."""

    def install(ceiling: str = "real", **overrides) -> Routed:
        routed = Routed(ceiling, **overrides)
        for module in (execution_api, integrations_api, sandbox_api, planning_api):
            monkeypatch.setattr(module, "get_settings", lambda: routed.settings)
        runner = ExecutionRunner(routed.service, EventBroker(retention_seconds=60), max_workers=2)
        app.dependency_overrides[execution_api.get_execution_service] = lambda: routed.service
        app.dependency_overrides[streaming_api.get_execution_runner] = lambda: runner
        routed.runner = runner
        return routed

    yield install
    app.dependency_overrides.clear()


def execution_body(plan: dict, mode: str, workflow_id: str | None = None, approved: bool = True) -> dict:
    return {
        "workflow_id": workflow_id or plan["workflow_id"],
        "plan": {"summary": plan["summary"], "steps": [
            {k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
            for s in plan["planned_steps"]]},
        "approval": {"approved": approved, "mode": mode, "approved_by": "synthetic.reviewer"},
    }


def sandbox_request(mode: str, workflow_id=None) -> ExecutionRequest:
    step = {"step_number": 1, "integration": "notion", "action": "create_page", "description": "Synthetic",
            "arguments": {"title": "Synthetic sandbox page", "content": "Synthetic content",
                          **({"parent": "engineering"} if mode != "mock" else {})},
            "depends_on": []}
    return ExecutionRequest.model_validate({
        "workflow_id": str(workflow_id or uuid4()), "plan": {"summary": "Synthetic", "steps": [step]},
        "approval": {"approved": True, "mode": mode, "approved_by": "synthetic.reviewer"}})


# ======================= mode routing (unit) =======================


def test_allowed_modes_never_exceed_the_ceiling():
    assert allowed_modes(IntegrationMode.MOCK) == [IntegrationMode.MOCK]
    assert allowed_modes(IntegrationMode.DRY_RUN) == [IntegrationMode.MOCK, IntegrationMode.DRY_RUN]
    assert allowed_modes(IntegrationMode.REAL) == list(IntegrationMode)


def test_real_ceiling_runs_mock_requests_without_building_real_adapters():
    routed = Routed("real")
    response = routed.service.execute(sandbox_request("mock"))
    assert response.status is ExecutionStatus.COMPLETED and response.mode == "mock"
    assert routed.built == ["mock"]  # no real registry, no MCP client


def test_escalation_above_the_ceiling_is_rejected_before_any_tool_call():
    routed = Routed("mock")
    response = routed.service.execute(sandbox_request("real"), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [e.code for e in response.errors] == ["MODE_MISMATCH"]
    assert routed.built == ["mock"]
    with pytest.raises(ModeMismatch):
        routed.service.check_conflict(sandbox_request("dry_run"))


def test_real_mode_still_needs_server_verified_write_authorization():
    routed = Routed("real")
    response = routed.service.execute(sandbox_request("real"), write_authorized=False)
    assert [e.code for e in response.errors] == ["REAL_WRITE_AUTHORIZATION_REQUIRED"]
    assert routed.worlds["real"].remote_calls() == 0


def test_kill_switch_still_applies_to_routed_real_requests():
    routed = Routed("real", real_writes_enabled=False)
    response = routed.service.execute(sandbox_request("real"), write_authorized=True)
    assert [e.code for e in response.errors] == ["REAL_WRITES_DISABLED"]
    assert routed.worlds["real"].remote_calls() == 0


def test_a_workflow_id_is_bound_to_its_first_mode():
    routed = Routed("real")
    wf = uuid4()
    assert routed.service.execute(sandbox_request("dry_run", wf)).status is ExecutionStatus.COMPLETED
    with pytest.raises(IdempotencyConflict, match="dry_run"):
        routed.service.execute(sandbox_request("real", wf), write_authorized=True)
    with pytest.raises(IdempotencyConflict):
        routed.service.check_conflict(sandbox_request("mock", wf))
    assert routed.service.get(str(wf)).mode == "dry_run"
    assert "real" not in routed.worlds or routed.worlds["real"].remote_calls() == 0


def test_close_releases_every_built_service():
    routed = Routed("real")
    routed.service.execute(sandbox_request("mock"))
    routed.service.execute(sandbox_request("dry_run"))
    routed.service.close()
    assert routed.service.get("anything") is None


# ======================= sandbox plan =======================


def test_mock_sandbox_plan_is_notion_only_and_synthetic(api):
    api("mock")
    response = TestClient(app).post(SANDBOX_PLAN, json={"mode": "mock"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "PLANNED" and plan["planner"] == "notion-sandbox" and plan["executed"] is False
    assert plan["mode"] == "mock" and plan["real_write"] is False and plan["destination_alias"] is None
    [step] = plan["planned_steps"]
    assert (step["integration"], step["action"]) == ("notion", "create_page")
    assert set(step["arguments"]) == {"title", "content"}
    assert "Synthetic" in step["arguments"]["content"] and plan["workflow_id"] in step["arguments"]["content"]


def test_real_sandbox_plan_targets_the_sandbox_alias_and_never_its_id(api):
    api("real")
    response = TestClient(app).post(SANDBOX_PLAN, json={"mode": "real"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["real_write"] is True and plan["destination_alias"] == "engineering"
    assert plan["planned_steps"][0]["arguments"]["parent"] == "engineering"
    assert "API-post-page" in plan["operation"] and "API-patch-page" in plan["rollback"]
    assert_no_secrets(response.text)


def test_sandbox_plan_above_the_ceiling_is_refused(api):
    api("mock")
    assert TestClient(app).post(SANDBOX_PLAN, json={"mode": "real"}).status_code == 409
    assert TestClient(app).post(SANDBOX_PLAN, json={"mode": "bogus"}).status_code == 422


@pytest.mark.parametrize("overrides, mode, setting", [
    ({"smoke_test_notion_parent": None}, "real", "SMOKE_TEST_NOTION_PARENT"),
    ({"smoke_test_notion_parent": "not-approved"}, "dry_run", "NOTION_DESTINATIONS"),
    ({"real_writes_enabled": False}, "real", "REAL_WRITES_ENABLED"),
    ({"real_write_authorization_token": None}, "real", "REAL_WRITE_AUTHORIZATION_TOKEN"),
    ({"notion_mcp_url": None}, "real", "NOTION_MCP_URL"),
])
def test_unready_sandbox_is_reported_by_setting_name(api, overrides, mode, setting):
    api("real", **overrides)
    response = TestClient(app).post(SANDBOX_PLAN, json={"mode": mode})
    assert response.status_code == 503 and setting in response.json()["detail"]
    assert_no_secrets(response.text)


def test_dry_run_sandbox_plan_needs_no_write_switch(api):
    api("real", real_writes_enabled=False)
    response = TestClient(app).post(SANDBOX_PLAN, json={"mode": "dry_run"})
    assert response.status_code == 200 and response.json()["real_write"] is False


# ======================= full UI flow (sync + streamed) =======================


def test_real_sandbox_flow_needs_operator_token_then_creates_one_page(api):
    routed = api("real")
    client = TestClient(app)
    plan = client.post(SANDBOX_PLAN, json={"mode": "real"}).json()

    rejected = client.post(EXECUTE, json=execution_body(plan, "real"))  # no operator token
    assert rejected.status_code == 403
    assert [e["code"] for e in rejected.json()["errors"]] == ["REAL_WRITE_AUTHORIZATION_REQUIRED"]
    wrong = client.post(EXECUTE, json=execution_body(plan, "real"), headers={"X-Integration-Authorization": "nope"})
    assert wrong.status_code == 403
    not_approved = client.post(EXECUTE, json=execution_body(plan, "real", approved=False), headers=AUTH)
    assert [e["code"] for e in not_approved.json()["errors"]] == ["APPROVAL_REQUIRED"]
    assert routed.notion.calls_named("API-post-page") == []

    response = client.post(EXECUTE, json=execution_body(plan, "real"), headers=AUTH)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "COMPLETED" and result["mode"] == "real"
    [step] = result["step_results"]
    assert step["resource_id"] and step["output"]["url"].startswith("https://www.notion.so/")
    assert step["output"]["destination"] == "engineering"
    [created] = routed.notion.calls_named("API-post-page")
    assert created["parent"] == {"page_id": NOTION_PAGE_ID}  # resolved server-side only
    assert_no_secrets(response.text)

    # A repeat is replayed, never re-written.
    again = client.post(EXECUTE, json=execution_body(plan, "real"), headers=AUTH)
    assert again.json()["replayed"] is True and len(routed.notion.calls_named("API-post-page")) == 1


def test_streamed_real_sandbox_run_reports_mode_and_stored_result(api):
    routed = api("real")
    client = TestClient(app)
    plan = client.post(SANDBOX_PLAN, json={"mode": "real"}).json()
    start = client.post(EXECUTIONS, json=execution_body(plan, "real"), headers=AUTH)
    assert start.status_code == 202 and start.json()["mode"] == "real"
    result = None
    for _ in range(100):
        got = client.get(start.json()["result_url"])
        if got.status_code == 200:
            result = got.json()
            break
        time.sleep(0.05)
    assert result is not None and result["status"] == "COMPLETED"
    assert result["step_results"][0]["output"]["url"].startswith("https://")
    assert len(routed.notion.calls_named("API-post-page")) == 1


def test_streamed_start_above_the_ceiling_is_409(api):
    api("dry_run")
    client = TestClient(app)
    plan = client.post(SANDBOX_PLAN, json={"mode": "dry_run"}).json()
    assert client.post(EXECUTIONS, json=execution_body(plan, "real"), headers=AUTH).status_code == 409


def test_dry_run_sandbox_flow_sends_nothing(api):
    routed = api("real")
    client = TestClient(app)
    plan = client.post(SANDBOX_PLAN, json={"mode": "dry_run"}).json()
    response = client.post(EXECUTE, json=execution_body(plan, "dry_run"))
    assert response.status_code == 200
    output = response.json()["step_results"][0]["output"]
    assert output["sent"] is False and output["would_call"] == "API-post-page"
    assert routed.worlds["dry_run"].remote_calls() == 0 and "real" not in routed.worlds


def test_mock_mode_and_templates_still_work_on_a_real_ceiling_server(api):
    api("real")
    client = TestClient(app)
    plan = client.post(SANDBOX_PLAN, json={"mode": "mock"}).json()
    response = client.post(EXECUTE, json=execution_body(plan, "mock"))
    assert response.status_code == 200 and response.json()["mode"] == "mock"
    assert len(client.get("/api/v1/workflows/templates").json()) == 3


# ======================= status + connectivity =======================


def test_status_reports_modes_and_sandbox_readiness(api):
    api("real")
    data = TestClient(app).get(STATUS).json()
    assert data["available_modes"] == ["mock", "dry_run", "real"]
    assert data["notion_sandbox"] == {"parent_alias": "engineering", "dry_run_ready": True, "real_ready": True,
                                      "issues": []}


def test_status_in_mock_ceiling_offers_mock_only(api):
    api("mock")
    data = TestClient(app).get(STATUS).json()
    assert data["available_modes"] == ["mock"] and data["notion_sandbox"]["real_ready"] is False


def test_status_with_invalid_configuration_offers_mock_only(api):
    api("real", notion_destinations="{bad")
    data = TestClient(app).get(STATUS).json()
    assert data["available_modes"] == ["mock"] and data["configuration_valid"] is False


def test_status_lists_kill_switch_as_real_blocker(api):
    api("real", real_writes_enabled=False)
    sandbox = TestClient(app).get(STATUS).json()["notion_sandbox"]
    assert sandbox["dry_run_ready"] is True and sandbox["real_ready"] is False
    assert any("REAL_WRITES_ENABLED" in i for i in sandbox["issues"])


def _connect(world_server):
    app.dependency_overrides[integrations_api.get_notion_transport_factory] = lambda: (lambda: world_server)


def test_connectivity_probe_is_read_only_and_passes(api):
    api("real")
    world = FakeWorld("real")
    _connect(world.notion)
    response = TestClient(app).post(CONNECTIVITY)
    data = response.json()
    assert data["status"] == "PASS", data
    assert {c["name"] for c in data["checks"]} >= {"MCP initialize + tools/list", "retrieve sandbox parent page"}
    assert [name for name, _ in world.notion.tool_calls] == ["API-retrieve-a-page"]  # no write tool
    assert_no_secrets(response.text)


def test_connectivity_probe_fails_when_sandbox_page_is_not_shared(api):
    api("real")
    world = FakeWorld("real")
    world.notion.notion_errors["API-retrieve-a-page"] = [(404, "object_not_found")]
    _connect(world.notion)
    data = TestClient(app).post(CONNECTIVITY).json()
    assert data["status"] == "FAIL"
    assert any(c["status"] == "FAIL" and "shared" in c["detail"] for c in data["checks"])


def test_connectivity_probe_reports_unreachable_server(api):
    api("real")

    world = FakeWorld("real")
    world.notion.request = lambda message, *, timeout: (_ for _ in ()).throw(
        IntegrationError(ErrorKind.TRANSIENT, "MCP_CONNECTION_FAILED", "synthetic: refused", request_sent=False))
    _connect(world.notion)
    data = TestClient(app).post(CONNECTIVITY).json()
    assert data["status"] == "FAIL"
    assert data["checks"] == [{"name": "MCP initialize + tools/list", "status": "FAIL",
                               "detail": "MCP_CONNECTION_FAILED"}]


@pytest.mark.parametrize("ceiling, overrides", [("mock", {}), ("dry_run", {}), ("real", {"real_writes_enabled": False})])
def test_connectivity_probe_makes_no_call_unless_real_is_ready(api, ceiling, overrides):
    api(ceiling, **overrides)
    world = FakeWorld("real")
    _connect(world.notion)
    data = TestClient(app).post(CONNECTIVITY).json()
    assert data["status"] == "NOT_AVAILABLE" and data["issues"]
    assert world.notion.messages == []


# ======================= mode-aware planning =======================


def test_plan_mode_selects_the_document_check(api):
    api("real")
    planning = PlanningService(MockPlanner(), max_steps=10, max_planning_attempts=1, available_documents=None)
    app.dependency_overrides[planning_api.get_planning_service] = lambda: planning
    instruction = "Read 'Unknown synthetic memo' from Google Drive and create a Notion page with a summary."
    client = TestClient(app)
    as_mock = client.post("/api/v1/workflows/plan", json={"instruction": instruction, "mode": "mock"})
    assert as_mock.status_code == 422
    assert "DOCUMENT_NOT_FOUND" in [i["code"] for i in as_mock.json()["validation"]["issues"]]
    as_real = client.post("/api/v1/workflows/plan", json={"instruction": instruction, "mode": "real"})
    assert as_real.status_code == 200 and as_real.json()["status"] == "PLANNED"
    default = client.post("/api/v1/workflows/plan", json={"instruction": instruction})
    assert default.status_code == 200  # server default (real): no offline document check
    assert SYNTHETIC_DRIVE_DOCUMENTS  # the mock catalog is non-empty
