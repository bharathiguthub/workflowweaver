from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.agents.planners import MockPlanner
from app.agents.service import PlanningService
from app.api.planning import _default_service, get_planning_service
from app.core.config import get_settings
from app.main import app
from tests.conftest import EXAMPLE_INSTRUCTION, StubPlanner, timeout_error

PLAN_URL = "/api/v1/workflows/plan"


@pytest.fixture()
def use_planner():
    """Override the planning service with a given planner; independent of any local .env."""

    def _use(planner):
        service = PlanningService(planner, max_steps=10, max_planning_attempts=2)
        app.dependency_overrides[get_planning_service] = lambda: service

    yield _use
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_plan_example_instruction(client, use_planner):
    use_planner(MockPlanner())
    response = client.post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "name": "Sprint sync"})

    assert response.status_code == 200
    body = response.json()
    assert UUID(body["workflow_id"])
    assert body["status"] == "PLANNED"
    assert body["executed"] is False
    assert body["validation"] == {"is_valid": True, "issues": []}
    assert body["errors"] == []
    assert [(s["step_number"], s["integration"], s["action"]) for s in body["planned_steps"]] == [
        (1, "notion", "create_page"),
        (2, "google_drive", "create_document"),
        (3, "teams", "send_message"),
    ]


def test_plan_with_unsupported_integration_returns_422_with_issues(client, use_planner):
    use_planner(MockPlanner())
    response = client.post(PLAN_URL, json={"instruction": "Create a Jira issue and notify the team in Teams"})

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "INVALID"
    assert body["planned_steps"] == []
    assert [i["code"] for i in body["validation"]["issues"]] == ["UNSUPPORTED_INTEGRATION"]


def test_planner_failure_returns_502(client, use_planner):
    use_planner(StubPlanner(timeout_error()))
    response = client.post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION})

    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["planning_attempts"] == 2
    assert body["errors"][0]["code"] == "PLANNER_TIMEOUT"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"instruction": "too short"},
        {"instruction": EXAMPLE_INSTRUCTION, "execute": True},  # unknown fields forbidden
        {"instruction": "x" * 5001},
    ],
)
def test_invalid_request_body_returns_422_detail(client, use_planner, payload):
    use_planner(MockPlanner())
    response = client.post(PLAN_URL, json=payload)
    assert response.status_code == 422
    assert "detail" in response.json()


def test_claude_mode_without_model_returns_503(client, monkeypatch):
    monkeypatch.setenv("PLANNER_MODE", "claude")
    monkeypatch.setenv("ANTHROPIC_MODEL", "")  # env vars take precedence over any local .env
    get_settings.cache_clear()
    _default_service.cache_clear()
    try:
        response = client.post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION})
    finally:
        get_settings.cache_clear()
        _default_service.cache_clear()
    assert response.status_code == 503
    assert "ANTHROPIC_MODEL" in response.json()["detail"]


def test_phase1_endpoints_still_registered(client):
    paths = {route.path for route in app.routes}
    assert {"/health", "/api/v1/workflows", "/api/v1/workflows/templates", PLAN_URL} <= paths
    assert client.post("/api/v1/workflows", json={"name": "Regression", "input_text": EXAMPLE_INSTRUCTION}).json()["status"] == "PLANNED"
