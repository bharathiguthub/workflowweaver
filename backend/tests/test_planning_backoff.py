"""Planner retries: bounded exponential backoff with jitter (HTTP 503 and other transient
errors) and safe HTTP status names in error reports. Uses a fake Gemini client and a
recording sleeper: no test waits and no real API is called."""

import logging
import random

import pytest
from fastapi.testclient import TestClient
from google.genai import errors

from app.agents.execution.retry import RetryPolicy
from app.agents.planners.gemini import GeminiPlanner, http_label, safe_status_name, translate_api_error
from app.agents.project_workflow import EXAMPLE_INSTRUCTION as PROJECT_INSTRUCTION
from app.agents.project_workflow import GeminiProjectPlanner
from app.agents.service import PlanningService
from app.api import integrations as integrations_api
from app.api import planning as planning_api
from app.api import project as project_api
from app.core.config import Settings
from app.main import app
from app.observability import set_telemetry
from app.schemas.plan import PlanStatus
from tests.conftest import EXAMPLE_INSTRUCTION
from tests.integration_fakes import integration_settings
from tests.telemetry_helpers import RecordingTelemetry
from tests.test_gemini_planner import KEY, MODEL, FakeGenaiClient, api_error, gemini_response, ok_response


def unavailable() -> errors.APIError:
    return api_error(errors.ServerError, 503, "UNAVAILABLE")


def service(*outcomes, attempts=3, base=1.0, cap=8.0, jitter=False, rng=None):
    client = FakeGenaiClient(*outcomes)
    slept: list[float] = []
    planner = GeminiPlanner(client=client, model=MODEL, max_output_tokens=4096)
    svc = PlanningService(planner, max_steps=10, max_planning_attempts=attempts,
                          backoff=RetryPolicy(max_attempts=attempts, base_delay_seconds=base,
                                              max_delay_seconds=cap, jitter=jitter),
                          sleeper=slept.append, rng=rng)
    return svc, client, slept


# ======================= policy =======================


def test_planning_policy_comes_from_settings():
    policy = RetryPolicy.for_planning(Settings(_env_file=None, max_planning_attempts=3))
    assert policy == RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=8.0, jitter=True)


def test_delays_double_and_are_capped_at_8_seconds():
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=8.0, jitter=False)
    assert [policy.delay_for(n) for n in range(1, 7)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_jitter_stays_within_half_and_full_delay():
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=8.0, jitter=True)
    rng = random.Random(1234)
    for _ in range(200):
        for n, full in ((1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (6, 8.0)):
            assert full / 2 <= policy.delay_for(n, rng) <= full


# ======================= HTTP 503 retries through the LangGraph planning graph =======================


def test_503_is_retried_with_backoff_then_succeeds():
    svc, client, slept = service(unavailable(), unavailable(), ok_response())
    result = svc.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and result.planning_attempts == 3
    assert slept == [1.0, 2.0] and result.retry_delays_seconds == [1.0, 2.0]
    assert len(client.models.calls) == 3


def test_persistent_503_stops_at_max_attempts_and_reports_each_attempt():
    svc, client, slept = service(unavailable(), attempts=3)
    result = svc.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and result.planning_attempts == 3
    assert len(client.models.calls) == 3  # bounded: never more calls than MAX_PLANNING_ATTEMPTS
    assert slept == [1.0, 2.0]  # no wait after the final attempt
    assert [(e.code, e.retryable) for e in result.errors] == [("PLANNER_API_ERROR", True)] * 3
    assert [e.message for e in result.errors] == [
        f"Attempt {n}: Gemini API error (HTTP 503 UNAVAILABLE)." for n in (1, 2, 3)]
    assert all("SYNTHETIC-PROVIDER-DETAIL" not in e.message for e in result.errors)


def test_long_outage_waits_are_capped_at_8_seconds():
    svc, client, slept = service(unavailable(), attempts=5, base=4.0)
    svc.plan(EXAMPLE_INSTRUCTION)
    assert slept == [4.0, 8.0, 8.0, 8.0] and len(client.models.calls) == 5


def test_jittered_waits_are_bounded():
    svc, _, slept = service(unavailable(), attempts=3, jitter=True, rng=random.Random(7))
    svc.plan(EXAMPLE_INSTRUCTION)
    assert len(slept) == 2 and 0.5 <= slept[0] <= 1.0 and 1.0 <= slept[1] <= 2.0


@pytest.mark.parametrize("error", [
    api_error(errors.ClientError, 401, "UNAUTHENTICATED"),
    api_error(errors.ClientError, 404, "NOT_FOUND"),
])
def test_non_retryable_errors_never_wait(error):
    svc, client, slept = service(error)
    result = svc.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and slept == [] and len(client.models.calls) == 1


def test_rate_limit_also_backs_off():
    svc, client, slept = service(api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), ok_response())
    result = svc.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and slept == [1.0]


def test_no_backoff_configured_means_no_wait():
    client = FakeGenaiClient(unavailable(), ok_response())
    slept: list[float] = []
    svc = PlanningService(GeminiPlanner(client=client, model=MODEL, max_output_tokens=4096), max_steps=10,
                          max_planning_attempts=2, sleeper=slept.append)
    assert svc.plan(EXAMPLE_INSTRUCTION).status is PlanStatus.PLANNED and slept == []


# ======================= production wiring =======================


def test_plan_api_service_uses_the_configured_backoff(monkeypatch):
    settings = Settings(_env_file=None, max_planning_attempts=3, planning_backoff_base_seconds=1.5,
                        planning_backoff_max_seconds=6.0)
    monkeypatch.setattr(planning_api, "get_settings", lambda: settings)
    planning_api._default_service.cache_clear()
    built = planning_api._default_service()
    assert built.backoff == RetryPolicy(max_attempts=3, base_delay_seconds=1.5, max_delay_seconds=6.0, jitter=True)
    assert built.max_planning_attempts == 3


def test_project_endpoint_backs_off_on_gemini_503(monkeypatch):
    settings = integration_settings("real", smoke_test_notion_parent="engineering", planner_mode="gemini",
                                    gemini_model=MODEL, gemini_api_key=KEY, max_planning_attempts=3,
                                    planning_backoff_jitter=False)
    monkeypatch.setattr(project_api, "get_settings", lambda: settings)
    monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
    slept: list[float] = []
    monkeypatch.setattr(project_api, "get_planning_sleeper", lambda: slept.append)
    from tests.test_project_workflow import rewards_project

    client = FakeGenaiClient(unavailable(), unavailable(), gemini_response(rewards_project().model_dump_json()))
    app.dependency_overrides[project_api.get_project_source_factory] = lambda: (
        lambda s, m: GeminiProjectPlanner(client=client, model=MODEL, max_output_tokens=4096))
    try:
        response = TestClient(app).post("/api/v1/workflows/notion-project/plan",
                                        json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    finally:
        app.dependency_overrides.clear()
    body = response.json()
    assert response.status_code == 200 and body["status"] == "PLANNED" and body["planning_attempts"] == 3
    assert slept == [1.0, 2.0] and body["retry_delays_seconds"] == [1.0, 2.0]
    assert [e["message"] for e in body["errors"]] == [
        "Attempt 1: Gemini API error (HTTP 503 UNAVAILABLE).", "Attempt 2: Gemini API error (HTTP 503 UNAVAILABLE)."]
    assert KEY not in response.text and "SYNTHETIC-PROVIDER-DETAIL" not in response.text


# ======================= safe status names in error reports =======================


@pytest.mark.parametrize("error, label", [
    (api_error(errors.ServerError, 503, "UNAVAILABLE"), "HTTP 503 UNAVAILABLE"),
    (api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), "HTTP 429 RESOURCE_EXHAUSTED"),
    (api_error(errors.ServerError, 504, "DEADLINE_EXCEEDED"), "HTTP 504 DEADLINE_EXCEEDED"),
    (errors.ServerError(503, {"error": {"code": 503, "message": "x", "status": "overloaded: key=abc"}}), "HTTP 503"),
    (errors.ServerError(503, {"error": {"code": 503, "message": "x", "status": "Unavailable"}}), "HTTP 503"),
    (errors.ServerError(503, {"error": {"code": 503, "message": "x", "status": "A" * 60}}), "HTTP 503"),
    (errors.ServerError(503, {}), "HTTP 503"),
])
def test_only_canonical_status_names_are_reported(error, label):
    assert http_label(error) == label
    message = translate_api_error(error).message
    assert label in message and "key=abc" not in message and "SYNTHETIC-PROVIDER-DETAIL" not in message


def test_status_name_rejects_free_text():
    assert safe_status_name(errors.ServerError(503, {"error": {"status": "The model is overloaded"}})) is None


def test_status_is_logged_and_traced_without_body_or_key(caplog):
    recorder = RecordingTelemetry()
    set_telemetry(recorder)
    svc, _, _ = service(unavailable(), attempts=1)
    with caplog.at_level(logging.INFO):
        svc.plan(EXAMPLE_INSTRUCTION)
    assert "gemini planner error HTTP 503 UNAVAILABLE code=PLANNER_API_ERROR retryable=True" in caplog.text
    assert "SYNTHETIC-PROVIDER-DETAIL" not in caplog.text and KEY not in caplog.text
    generation = recorder.one("gemini.models.generate_content")
    meta = generation.merged_metadata()
    assert meta["http_status"] == 503 and meta["status_name"] == "UNAVAILABLE"
    assert generation.error == "PLANNER_API_ERROR"
    assert "SYNTHETIC-PROVIDER-DETAIL" not in recorder.dump()


def test_backoff_wait_is_logged(caplog):
    svc, _, _ = service(unavailable(), ok_response(), attempts=2)
    with caplog.at_level(logging.INFO):
        svc.plan(EXAMPLE_INSTRUCTION)
    assert "attempt=2 backoff_seconds=1.000" in caplog.text
