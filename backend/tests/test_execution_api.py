"""API tests for POST /api/v1/workflows/execute and GET /executions/{id}. Mock tools only."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import execution as execution_api
from app.agents.execution.tools import Fault
from app.core.config import Settings
from app.main import app
from tests.execution_helpers import APPROVAL, FOUR_STEPS, make_service

URL = "/api/v1/workflows/execute"


def body(steps=None, **approval):
    return {
        "workflow_id": str(uuid4()),
        "plan": {"summary": "Synthetic plan", "steps": FOUR_STEPS if steps is None else steps},
        "approval": {**APPROVAL, **approval},
    }


@pytest.fixture
def client_for():
    def factory(*faults):
        service, _ = make_service(*faults)
        app.dependency_overrides[execution_api.get_execution_service] = lambda: service
        return TestClient(app), service

    yield factory
    app.dependency_overrides.clear()


def test_execute_success(client_for):
    client, _ = client_for()
    response = client.post(URL, json=body())
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "COMPLETED" and data["mode"] == "mock" and data["replayed"] is False
    assert [s["status"] for s in data["step_results"]] == ["SUCCEEDED"] * 4
    assert data["step_results"][1]["resource_id"].startswith("mock-notion-page-")


def test_duplicate_request_returns_stored_result(client_for):
    client, service = client_for()
    payload = body()
    first = client.post(URL, json=payload).json()
    second = client.post(URL, json=payload)
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert second.json()["step_results"] == first["step_results"]
    assert service.registry.store.side_effects == 3


def test_conflicting_duplicate_is_409(client_for):
    client, _ = client_for()
    payload = body()
    client.post(URL, json=payload)
    payload["plan"]["summary"] = "Changed"
    assert client.post(URL, json=payload).status_code == 409


def test_not_approved_is_403(client_for):
    client, service = client_for()
    response = client.post(URL, json=body(approved=False))
    assert response.status_code == 403
    assert response.json()["status"] == "REJECTED"
    assert service.registry.store.calls == []


def test_invalid_plan_is_422_with_issues(client_for):
    client, service = client_for()
    step = {"step_number": 1, "integration": "jira", "action": "create_issue", "arguments": {}}
    response = client.post(URL, json=body([step]))
    assert response.status_code == 422
    assert response.json()["validation"]["issues"][0]["code"] == "UNSUPPORTED_INTEGRATION"
    assert service.registry.store.calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["approval"].update(mode="production"),  # not a mode at all
        lambda b: b["approval"].pop("approved_by"),
        lambda b: b.update(faults={"2": ["permanent"]}),  # fault injection never via API
        lambda b: b.update(workflow_id="not-a-uuid"),
    ],
)
def test_bad_request_bodies_are_422(client_for, mutate):
    client, service = client_for()
    payload = body()
    mutate(payload)
    assert client.post(URL, json=payload).status_code == 422
    assert service.registry.store.calls == []


@pytest.mark.parametrize("mode", ["real", "dry_run"])
def test_mode_other_than_the_servers_is_409_and_nothing_runs(client_for, mode):
    # Phase 5.1: "real"/"dry_run" are valid values, but the mode is chosen by the server
    # (INTEGRATION_MODE, default mock); a client can never escalate it.
    client, service = client_for()
    payload = body()
    payload["approval"]["mode"] = mode
    response = client.post(URL, json=payload)
    assert response.status_code == 409
    assert response.json()["status"] == "REJECTED" and response.json()["mode"] == "mock"
    assert [e["code"] for e in response.json()["errors"]] == ["MODE_MISMATCH"]
    assert service.registry.store.calls == []


def test_step_failure_is_502_with_rollback(client_for):
    client, _ = client_for({3: [Fault.PERMANENT]})
    response = client.post(URL, json=body())
    assert response.status_code == 502
    data = response.json()
    assert data["status"] == "ROLLED_BACK"
    assert data["compensation_results"][0]["status"] == "COMPENSATED"


def test_compensation_failure_is_502(client_for):
    client, _ = client_for({4: [Fault.PERMANENT]}, {2: [Fault.PERMANENT]})
    response = client.post(URL, json=body())
    assert response.status_code == 502 and response.json()["status"] == "COMPENSATION_FAILED"


def test_get_execution(client_for):
    client, _ = client_for()
    payload = body()
    client.post(URL, json=payload)
    stored = client.get(f"/api/v1/workflows/executions/{payload['workflow_id']}")
    assert stored.status_code == 200 and stored.json()["status"] == "COMPLETED"
    assert client.get(f"/api/v1/workflows/executions/{uuid4()}").status_code == 404


def test_execution_disabled_is_503(monkeypatch):
    monkeypatch.setattr(execution_api, "get_settings", lambda: Settings(mock_execution_enabled=False))
    response = TestClient(app).post(URL, json=body())
    assert response.status_code == 503


def test_default_service_wiring(monkeypatch):
    monkeypatch.setattr(execution_api, "get_settings", lambda: Settings(execution_backoff_jitter=False))
    execution_api._default_service.cache_clear()
    try:
        service = execution_api.get_execution_service()
        assert service.registry.faults is None  # the API never injects faults
        response = TestClient(app).post(URL, json=body())
        assert response.status_code == 200
    finally:
        execution_api._default_service.cache_clear()
