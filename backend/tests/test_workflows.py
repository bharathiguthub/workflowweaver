from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


SYNTHETIC_NOTES = "Synthetic standup: Alice drafts the roadmap by Friday; Bob reviews the spec."


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_list_templates(client: TestClient) -> None:
    response = client.get("/api/v1/workflows/templates")
    assert response.status_code == 200
    names = {t["name"] for t in response.json()}
    assert names == {"Meeting-to-Tasks", "Sprint Planning", "Incident Response"}


def test_create_workflow_from_template_returns_planned(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflows",
        json={
            "name": "Weekly sync follow-ups",
            "template_id": "meeting-to-tasks",
            "input_text": SYNTHETIC_NOTES,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PLANNED"
    assert UUID(body["id"]).version == 4
    assert len(body["steps"]) == 4
    assert "notion" in body["integrations"]


def test_create_workflow_without_template(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflows",
        json={"name": "Ad hoc plan", "input_text": SYNTHETIC_NOTES, "integrations": ["teams"]},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PLANNED"
    assert body["integrations"] == ["teams"]


def test_ids_are_unique(client: TestClient) -> None:
    payload = {"name": "Repeat", "input_text": SYNTHETIC_NOTES}
    first = client.post("/api/v1/workflows", json=payload).json()["id"]
    second = client.post("/api/v1/workflows", json=payload).json()["id"]
    assert first != second


@pytest.mark.parametrize(
    "payload",
    [
        {"input_text": SYNTHETIC_NOTES},  # missing name
        {"name": "ok name", "input_text": "short"},  # input too short
        {"name": "ok name", "input_text": SYNTHETIC_NOTES, "integrations": ["slack"]},
        {"name": "ok name", "input_text": SYNTHETIC_NOTES, "integrations": ["teams", "teams"]},
        {"name": "ok name", "input_text": SYNTHETIC_NOTES, "unexpected": True},
        {"name": "ok name", "input_text": SYNTHETIC_NOTES, "template_id": "does-not-exist"},
    ],
)
def test_create_workflow_validation_errors(client: TestClient, payload: dict) -> None:
    response = client.post("/api/v1/workflows", json=payload)
    assert response.status_code == 422
