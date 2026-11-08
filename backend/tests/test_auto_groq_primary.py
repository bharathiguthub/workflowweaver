"""PLANNER_MODE=auto default order: Groq primary, Gemini automatic fallback.

The order comes from the default PRIMARY_PLANNER=groq / FALLBACK_PLANNER=gemini and applies to
normal workflow planning (/workflows/plan), the AI project plan and Phase 7.2 document
extraction (/workflows/orchestration/plan).

Fake clients replace groq.Groq and google.genai.Client: no real API call is made (the conftest
also blocks every non-loopback connection and blanks every LLM key). No real integration write
happens: orchestration runs in dry_run against fake Drive / Notion / Teams.
"""

import groq
import pytest
from fastapi.testclient import TestClient
from google.genai import errors

from app.agents.execution.retry import RetryPolicy
from app.agents.orchestration import GeminiExtractor, GroqExtractor, MockExtractor
from app.agents.planners import FallbackPlanner, GeminiPlanner, GroqPlanner
from app.agents.planners import factory as planner_factory
from app.agents.planners.factory import ai_planner_model, create_planner, fallback_problem
from app.agents.project_workflow import GeminiProjectPlanner, GroqProjectPlanner
from app.agents.service import PlanningService
from app.api import orchestration as orchestration_api
from app.api import planning as planning_api
from app.api import project as project_api
from app.core.config import IntegrationMode, PlannerProvider, Settings
from app.main import app
from app.schemas.execution import ExecutionStatus
from app.schemas.plan import DraftPlan, PlanStatus
from tests.conftest import EXAMPLE_INSTRUCTION, make_step, valid_draft
from tests.test_gemini_planner import KEY as GEMINI_KEY
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, api_error, gemini_response, ok_response
from tests.test_orchestration import EXTRACTION, World, execution_request, plan
from tests.test_planner_fallback import (
    GROQ_KEY,
    GROQ_MODEL,
    GROQ_REQUEST,
    FakeGroqClient,
    gemini_503,
    groq_ok,
    groq_response,
    groq_status_error,
)


@pytest.fixture(autouse=True)
def _default_order(monkeypatch):
    """The developer's shell must not change the order under test."""
    monkeypatch.delenv("PRIMARY_PLANNER", raising=False)
    monkeypatch.delenv("FALLBACK_PLANNER", raising=False)
    yield
    app.dependency_overrides.clear()


def auto_defaults(**overrides) -> Settings:
    """auto mode with the DEFAULT order (no PRIMARY_PLANNER / FALLBACK_PLANNER given)."""
    values = dict(_env_file=None, planner_mode="auto", gemini_model=GEMINI_MODEL, gemini_api_key=GEMINI_KEY,
                  groq_model=GROQ_MODEL, groq_api_key=GROQ_KEY)
    values.update(overrides)
    return Settings(**values)


def groq_first(groq_outcomes, gemini_outcomes, *, attempts=2):
    """The auto chain in its default order, built from fake SDK clients."""
    groq_client = FakeGroqClient(*groq_outcomes)
    gemini = FakeGenaiClient(*gemini_outcomes)
    planner = FallbackPlanner([GroqPlanner(client=groq_client, model=GROQ_MODEL, max_tokens=4096),
                               GeminiPlanner(client=gemini, model=GEMINI_MODEL, max_output_tokens=4096)])
    slept: list[float] = []
    service = PlanningService(planner, max_steps=10, max_planning_attempts=attempts,
                              backoff=RetryPolicy(max_attempts=attempts, base_delay_seconds=1.0,
                                                  max_delay_seconds=8.0, jitter=False),
                              sleeper=slept.append)
    return service, groq_client, gemini, slept


# ======================= configuration: Groq first, Gemini second =======================


def test_default_auto_order_is_groq_then_gemini():
    settings = auto_defaults()
    assert settings.planner_mode.value == "auto"
    assert (settings.primary_planner, settings.fallback_planner) == (PlannerProvider.GROQ, PlannerProvider.GEMINI)
    assert ai_planner_model(settings) == GROQ_MODEL and fallback_problem(settings) is None


def test_env_with_only_existing_provider_variables_selects_groq_primary(monkeypatch):
    for name, value in {"PLANNER_MODE": "auto", "GROQ_MODEL": GROQ_MODEL, "GEMINI_MODEL": GEMINI_MODEL}.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert (settings.primary_planner.value, settings.fallback_planner.value) == ("groq", "gemini")
    # conftest blanks every key: the primary reports the missing Groq key, by name only
    assert "GROQ_API_KEY" in (planner_factory.ai_planner_problem(settings) or "")
    assert "GEMINI_API_KEY" in (fallback_problem(settings) or "")


def test_create_planner_builds_groq_then_gemini():
    planner = create_planner(auto_defaults())
    assert isinstance(planner, FallbackPlanner)
    assert [(p.name, p.model) for p in planner.providers] == [("groq", GROQ_MODEL), ("gemini", GEMINI_MODEL)]
    assert planner.model == GROQ_MODEL


def test_missing_gemini_keeps_groq_usable_and_missing_groq_is_reported():
    planner = create_planner(auto_defaults(gemini_api_key=None))
    assert [p.name for p in planner.providers] == ["groq"]
    with pytest.raises(planner_factory.PlannerConfigError, match="GROQ_MODEL"):
        create_planner(auto_defaults(groq_model=None))


def test_project_plan_and_document_extraction_use_the_same_order(monkeypatch):
    settings = auto_defaults()
    monkeypatch.setattr(project_api, "create_groq_client", lambda _s: FakeGroqClient(groq_ok()))
    monkeypatch.setattr(project_api, "create_gemini_client", lambda _s: FakeGenaiClient(ok_response()))
    source = project_api.default_source(settings, IntegrationMode.DRY_RUN)
    assert [type(p) for p in source.providers] == [GroqProjectPlanner, GeminiProjectPlanner]

    monkeypatch.setattr(orchestration_api, "create_groq_client", lambda _s: FakeGroqClient(groq_ok()))
    monkeypatch.setattr(orchestration_api, "create_gemini_client", lambda _s: FakeGenaiClient(ok_response()))
    for mode in (IntegrationMode.DRY_RUN, IntegrationMode.REAL):
        sources = orchestration_api.default_sources(settings, mode)
        assert [type(s) for s in sources] == [GroqExtractor, GeminiExtractor]
    # mock mode still uses the deterministic parser and never calls a provider
    assert [type(s) for s in orchestration_api.default_sources(settings, IntegrationMode.MOCK)] == [MockExtractor]


# ======================= normal workflow planning =======================


def test_groq_success_uses_groq_only():
    service, groq_client, gemini, slept = groq_first([groq_ok()], [ok_response()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and result.executed is False
    assert (result.planner, result.planner_model, result.primary_planner) == ("groq", GROQ_MODEL, "groq")
    assert result.fallback_used is False and result.fallback_reason is None
    assert len(groq_client.calls) == 1 and gemini.models.calls == [] and slept == []


def test_transient_groq_failure_recovered_by_retry_does_not_fall_back():
    service, groq_client, gemini, slept = groq_first([groq_status_error(503), groq_ok()], [ok_response()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and result.planner == "groq" and result.fallback_used is False
    assert len(groq_client.calls) == 2 and gemini.models.calls == [] and slept == [1.0]


@pytest.mark.parametrize("failure, reason, status", [
    (lambda: groq_status_error(429), "PLANNER_RATE_LIMITED", 429),
    (lambda: groq_status_error(500), "PLANNER_API_ERROR", 500),
    (lambda: groq_status_error(502), "PLANNER_API_ERROR", 502),
    (lambda: groq_status_error(503), "PLANNER_API_ERROR", 503),
    (lambda: groq_status_error(504), "PLANNER_API_ERROR", 504),
    (lambda: groq.APITimeoutError(request=GROQ_REQUEST), "PLANNER_TIMEOUT", None),
    (lambda: groq.APIConnectionError(request=GROQ_REQUEST), "PLANNER_CONNECTION_ERROR", None),
], ids=["rate_limit_429", "server_500", "bad_gateway_502", "unavailable_503", "gateway_timeout_504",
        "timeout", "connection_error"])
def test_groq_outage_retries_then_falls_back_to_gemini(failure, reason, status):
    service, groq_client, gemini, slept = groq_first([failure()], [ok_response()], attempts=3)
    result = service.plan(EXAMPLE_INSTRUCTION)

    assert result.status is PlanStatus.PLANNED and result.executed is False
    assert len(groq_client.calls) == 3 and len(gemini.models.calls) == 1  # bounded retries, then fallback
    assert slept == [1.0, 2.0]  # backoff between Groq attempts, none before switching provider
    assert (result.planner, result.planner_model, result.primary_planner) == ("gemini", GEMINI_MODEL, "groq")
    assert result.fallback_used is True and result.fallback_reason == reason
    assert [(a.provider, a.outcome, a.http_status) for a in result.provider_attempts] == [
        ("groq", "error", status)] * 3 + [("gemini", "draft", None)]


def test_both_providers_failing_returns_failed_with_nothing_to_execute():
    service, groq_client, gemini, _ = groq_first([groq_status_error(429)], [gemini_503()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and result.planned_steps == [] and result.executed is False
    assert result.errors[-1].code == "ALL_PLANNERS_FAILED"
    assert len(groq_client.calls) == 2 and len(gemini.models.calls) == 2


@pytest.mark.parametrize("failure, code", [
    (lambda: groq_status_error(401), "PLANNER_AUTH_ERROR"),
    (lambda: groq_status_error(403), "PLANNER_AUTH_ERROR"),
    (lambda: groq_status_error(400), "PLANNER_REQUEST_REJECTED"),
    (lambda: groq_response(None, finish="content_filter"), "PLANNER_REFUSED"),
])
def test_non_outage_groq_failures_never_fall_back(failure, code):
    service, groq_client, gemini, _ = groq_first([failure()], [ok_response()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and result.fallback_used is False
    assert result.errors[0].code == code and len(groq_client.calls) == 1 and gemini.models.calls == []


def test_groq_schema_failure_is_retried_on_groq_without_fallback():
    service, groq_client, gemini, _ = groq_first([groq_response("not json")], [ok_response()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and [e.code for e in result.errors] == ["INVALID_LLM_OUTPUT"] * 2
    assert len(groq_client.calls) == 2 and gemini.models.calls == []


def test_invalid_groq_plan_is_rejected_without_fallback():
    bad = DraftPlan.model_validate({"summary": "s", "steps": [
        {"step_number": 1, "integration": "jira", "action": "create_issue", "description": "d", "arguments": []}]})
    service, _, gemini, _ = groq_first([groq_ok(bad)], [ok_response()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.INVALID and gemini.models.calls == []
    assert [i.code for i in result.validation.issues] == ["UNSUPPORTED_INTEGRATION"]


def test_gemini_fallback_plan_passes_the_same_validator():
    unsafe = DraftPlan(summary="s", steps=[make_step(1, "teams", "send_message", message="run rm -rf / now")])
    service, _, gemini, _ = groq_first([groq_status_error(429)], [ok_response(unsafe)])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.INVALID and result.planner == "gemini" and result.fallback_used is True
    assert "UNSAFE_CONTENT" in [i.code for i in result.validation.issues] and result.planned_steps == []
    assert len(gemini.models.calls) == 1  # an invalid plan is not retried and not sent elsewhere


def test_plan_api_wires_the_default_order_from_settings(monkeypatch):
    """End to end through /workflows/plan with the real factory and fake SDK clients."""
    groq_client = FakeGroqClient(groq_status_error(429))
    gemini = FakeGenaiClient(ok_response(valid_draft()))
    monkeypatch.setattr(planner_factory, "create_groq_client", lambda _s: groq_client)
    monkeypatch.setattr(planner_factory, "create_gemini_client", lambda _s: gemini)
    settings = auto_defaults(planning_backoff_base_seconds=0, planning_backoff_max_seconds=0)
    monkeypatch.setattr(planning_api, "get_settings", lambda: settings)
    planning_api._default_service.cache_clear()

    response = TestClient(app).post("/api/v1/workflows/plan", json={"instruction": EXAMPLE_INSTRUCTION})
    planning_api._default_service.cache_clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["planner"], body["primary_planner"], body["fallback_used"]) == ("gemini", "groq", True)
    assert body["fallback_reason"] == "PLANNER_RATE_LIMITED" and body["executed"] is False
    assert [a["provider"] for a in body["provider_attempts"]] == ["groq", "groq", "gemini"]


# ======================= Phase 7.2 document extraction =======================


def extraction_world(monkeypatch, tmp_path, *, groq_outcomes, gemini_outcomes) -> World:
    """A dry-run orchestration world whose extraction chain comes from the REAL default_sources
    (auto, default order) with the SDK clients replaced by fakes."""
    world = World(monkeypatch, tmp_path, "dry_run", planner_mode="auto", groq_model=GROQ_MODEL,
                  groq_api_key=GROQ_KEY)
    assert world.settings.primary_planner is PlannerProvider.GROQ
    app.dependency_overrides.pop(orchestration_api.get_extraction_sources_factory)
    world.groq, world.gemini = FakeGroqClient(*groq_outcomes), FakeGenaiClient(*gemini_outcomes)
    monkeypatch.setattr(orchestration_api, "create_groq_client", lambda _s: world.groq)
    monkeypatch.setattr(orchestration_api, "create_gemini_client", lambda _s: world.gemini)
    return world


def test_extraction_uses_groq_first(monkeypatch, tmp_path):
    world = extraction_world(monkeypatch, tmp_path, groq_outcomes=(groq_ok(EXTRACTION),),
                             gemini_outcomes=(ok_response(EXTRACTION),))
    body = plan("dry_run").json()
    assert body["status"] == "PLANNED" and body["planner"] == "groq" and body["fallback_used"] is False
    assert len(world.groq.calls) == 1 and world.gemini.models.calls == []


@pytest.mark.parametrize("failure", [
    lambda: groq_status_error(429),
    lambda: groq_status_error(500),
    lambda: groq.APITimeoutError(request=GROQ_REQUEST),
], ids=["rate_limit", "provider_failure", "timeout"])
def test_extraction_falls_back_to_gemini_and_keeps_approval_gate(monkeypatch, tmp_path, failure):
    world = extraction_world(monkeypatch, tmp_path, groq_outcomes=(failure(),),
                             gemini_outcomes=(ok_response(EXTRACTION),))
    body = plan("dry_run").json()
    assert body["status"] == "PLANNED" and body["planner"] == "gemini" and body["fallback_used"] is True
    assert body["stages"][2]["detail"].startswith(f"gemini ({GEMINI_MODEL}) after fallback")
    assert len(world.groq.calls) >= 1 and len(world.gemini.models.calls) == 1
    prompt = world.gemini.models.calls[0]
    assert "untrusted data inside <document> tags" in str(prompt)

    # The fallback plan is still bound by the approval gate: unapproved -> rejected, no writes.
    rejected = world.service.execute(execution_request(body, approved=False), write_authorized=False)
    assert [e.code for e in rejected.errors] == ["APPROVAL_REQUIRED"]
    # Approved dry run builds every request but sends none.
    result = world.service.execute(execution_request(body), write_authorized=False)
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    assert all(r.output["sent"] is False for r in result.step_results)
    assert world.google.writes == [] and world.notion.tool_calls == [] and world.teams.posts == []


def test_extraction_auth_failure_on_groq_does_not_fall_back(monkeypatch, tmp_path):
    world = extraction_world(monkeypatch, tmp_path, groq_outcomes=(groq_status_error(401),),
                             gemini_outcomes=(ok_response(EXTRACTION),))
    response = plan("dry_run")
    assert response.status_code == 502 and response.json()["status"] == "FAILED"
    assert world.gemini.models.calls == []


def test_extraction_with_both_providers_down_fails_closed(monkeypatch, tmp_path):
    world = extraction_world(monkeypatch, tmp_path, groq_outcomes=(groq_status_error(503),),
                             gemini_outcomes=(api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"),))
    response = plan("dry_run")
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "FAILED" and body["planned_steps"] == []
    assert world.google.writes == [] and world.notion.tool_calls == [] and world.teams.posts == []


def test_extraction_invalid_gemini_output_is_not_accepted(monkeypatch, tmp_path):
    world = extraction_world(monkeypatch, tmp_path, groq_outcomes=(groq_status_error(429),),
                             gemini_outcomes=(gemini_response("{not json"),))
    response = plan("dry_run")
    assert response.status_code == 502 and response.json()["planned_steps"] == []
    assert world.notion.tool_calls == [] and world.teams.posts == []

