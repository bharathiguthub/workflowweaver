"""PLANNER_MODE=auto with an explicit PRIMARY_PLANNER=gemini / FALLBACK_PLANNER=groq order.

The default order (Groq primary, Gemini fallback) is covered by test_auto_groq_primary.py;
these tests check that the chain machinery honours an explicitly configured order.

Fake clients replace google.genai.Client and groq.Groq: no real API call is made (the
conftest also blocks every non-loopback connection and blanks every LLM key). Responses and
errors are the SDKs' own types, so the real translation code is exercised.

Covered: Gemini success; Gemini 503 / 429 / timeout / connection error -> bounded retries ->
Groq success; both providers failing; invalid credentials and refusals (no fallback);
schema failures and invalid / unsafe plans (no fallback, same validator for Groq); fallback
reporting in the API and Langfuse; and the approval, real-write authorization, kill switch,
sandbox parent, idempotency and rollback protections for a plan that came from the fallback.
"""

import json
import logging

import groq
import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import errors
from groq.types import CompletionUsage
from groq.types.chat import ChatCompletion, ChatCompletionMessage
from groq.types.chat.chat_completion import Choice

from app.agents.execution.retry import RetryPolicy
from app.agents.planners import FallbackPlanner, GeminiPlanner, GroqPlanner, PlannerConfigError, PlannerError
from app.agents.planners.base import PROVIDER_UNAVAILABLE_HTTP_STATUSES
from app.agents.planners.claude import build_system_prompt
from app.agents.planners.factory import (
    ai_planner_problem,
    create_groq_client,
    create_planner,
    fallback_problem,
)
from app.agents.planners.groq import strict_json_schema, translate_api_error
from app.agents.project_workflow import EXAMPLE_INSTRUCTION as PROJECT_INSTRUCTION
from app.agents.project_workflow import GeminiProjectPlanner, GroqProjectPlanner, ProjectPlanDraft
from app.agents.service import PlanningService
from app.api import integrations as integrations_api
from app.api import planning as planning_api
from app.api import project as project_api
from app.core.config import PlannerProvider, Settings
from app.core.log_redaction import install_secret_redaction
from app.integrations.secrets import scrub_secrets
from app.main import app
from app.observability import set_telemetry
from app.observability.sanitize import scrub_text
from app.schemas.execution import ExecutionStatus
from app.schemas.plan import DraftPlan, PlanStatus
from tests.conftest import EXAMPLE_INSTRUCTION, make_step, valid_draft
from tests.integration_fakes import integration_settings
from tests.telemetry_helpers import SENSITIVE_MARKERS, RecordingTelemetry
from tests.test_gemini_planner import KEY as GEMINI_KEY
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, api_error, gemini_response, ok_response
from tests.test_project_workflow import execution_request, real_world, rewards_project

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_KEY = "gsk_SYNTHETICnotARealGroqKey0000000011"
GROQ_REQUEST = httpx.Request("POST", "https://api.groq.invalid/openai/v1/chat/completions")
PROJECT_URL = "/api/v1/workflows/notion-project/plan"


# ======================= fakes =======================


class FakeCompletions:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeGroqClient:
    """Stands in for groq.Groq."""

    def __init__(self, *outcomes) -> None:
        self.completions = FakeCompletions(*outcomes)
        self.chat = self  # client.chat.completions.create

    @property
    def calls(self) -> list[dict]:
        return self.completions.calls


def groq_response(content: str | None, *, finish: str = "stop") -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl_synthetic", object="chat.completion", created=0, model=GROQ_MODEL,
        # model_construct: the SDK does not validate responses strictly, so the API can send
        # finish reasons its Literal does not list (e.g. "content_filter").
        choices=[Choice.model_construct(index=0, finish_reason=finish, logprobs=None,
                                        message=ChatCompletionMessage(role="assistant", content=content))],
        usage=CompletionUsage(prompt_tokens=800, completion_tokens=250, total_tokens=1050),
    )


def groq_ok(model=None) -> ChatCompletion:
    return groq_response((model or valid_draft()).model_dump_json())


def groq_status_error(status: int, code: str | None = None) -> groq.APIStatusError:
    body = {"error": {"message": "synthetic SYNTHETIC-PROVIDER-DETAIL", "type": "x", **({"code": code} if code else {})}}
    response = httpx.Response(status, request=GROQ_REQUEST, json=body)
    cls = {400: groq.BadRequestError, 401: groq.AuthenticationError, 403: groq.PermissionDeniedError,
           429: groq.RateLimitError}.get(status, groq.InternalServerError if status >= 500 else groq.APIStatusError)
    return cls("synthetic SYNTHETIC-PROVIDER-DETAIL", response=response, body=body)


def gemini_503() -> errors.APIError:
    return api_error(errors.ServerError, 503, "UNAVAILABLE")


def gemini_429() -> errors.APIError:
    return api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED")


def chain(gemini_outcomes, groq_outcomes, *, attempts=2, documents=None):
    gemini = FakeGenaiClient(*gemini_outcomes)
    groq_client = FakeGroqClient(*groq_outcomes)
    planner = FallbackPlanner([
        GeminiPlanner(client=gemini, model=GEMINI_MODEL, max_output_tokens=4096, available_documents=documents),
        GroqPlanner(client=groq_client, model=GROQ_MODEL, max_tokens=4096, available_documents=documents),
    ])
    slept: list[float] = []
    service = PlanningService(planner, max_steps=10, max_planning_attempts=attempts,
                              backoff=RetryPolicy(max_attempts=attempts, base_delay_seconds=1.0,
                                                  max_delay_seconds=8.0, jitter=False),
                              sleeper=slept.append)
    return service, gemini, groq_client, slept


def auto_settings(**overrides) -> Settings:
    values = dict(_env_file=None, planner_mode="auto", primary_planner="gemini", fallback_planner="groq",
                  gemini_model=GEMINI_MODEL, gemini_api_key=GEMINI_KEY, groq_model=GROQ_MODEL, groq_api_key=GROQ_KEY)
    values.update(overrides)
    return Settings(**values)


# ======================= Gemini success: Groq is never called =======================


def test_gemini_success_uses_gemini_only():
    service, gemini, groq_client, slept = chain([ok_response()], [groq_ok()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED
    assert (result.planner, result.planner_model) == ("gemini", GEMINI_MODEL)
    assert result.fallback_used is False and result.fallback_reason is None and result.primary_planner == "gemini"
    assert len(gemini.models.calls) == 1 and groq_client.calls == [] and slept == []
    assert [(a.provider, a.outcome) for a in result.provider_attempts] == [("gemini", "draft")]


def test_gemini_transient_503_recovered_by_retry_does_not_fall_back():
    service, gemini, groq_client, slept = chain([gemini_503(), ok_response()], [groq_ok()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and result.planner == "gemini" and result.fallback_used is False
    assert len(gemini.models.calls) == 2 and groq_client.calls == [] and slept == [1.0]


# ======================= outage -> bounded retries -> Groq =======================


@pytest.mark.parametrize("outage, reason, status", [
    (gemini_503, "PLANNER_API_ERROR", 503),
    (gemini_429, "PLANNER_RATE_LIMITED", 429),
    (lambda: api_error(errors.ServerError, 504, "DEADLINE_EXCEEDED"), "PLANNER_API_ERROR", 504),
    (lambda: httpx.ReadTimeout("synthetic timeout"), "PLANNER_TIMEOUT", None),
    (lambda: httpx.ConnectError("synthetic refused"), "PLANNER_CONNECTION_ERROR", None),
])
def test_gemini_outage_retries_with_backoff_then_groq_succeeds(outage, reason, status):
    service, gemini, groq_client, slept = chain([outage()], [groq_ok()], attempts=3)
    result = service.plan(EXAMPLE_INSTRUCTION)

    assert result.status is PlanStatus.PLANNED
    assert len(gemini.models.calls) == 3  # the existing bounded retry policy ran first
    assert slept == [1.0, 2.0]  # backoff between Gemini attempts; no wait before switching provider
    assert len(groq_client.calls) == 1
    assert (result.planner, result.planner_model, result.primary_planner) == ("groq", GROQ_MODEL, "gemini")
    assert result.fallback_used is True and result.fallback_reason == reason
    assert [(a.provider, a.attempt, a.outcome, a.error_code, a.http_status) for a in result.provider_attempts] == [
        ("gemini", 1, "error", reason, status), ("gemini", 2, "error", reason, status),
        ("gemini", 3, "error", reason, status), ("groq", 1, "draft", None, None)]
    assert all(e.provider == "gemini" for e in result.errors)  # Gemini's errors are kept for the report
    assert result.planned_steps and result.executed is False


def test_groq_also_gets_bounded_retries_after_fallback():
    service, gemini, groq_client, slept = chain([gemini_503()], [groq_status_error(503), groq_ok()], attempts=2)
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and result.planner == "groq"
    assert len(gemini.models.calls) == 2 and len(groq_client.calls) == 2
    assert slept == [1.0, 1.0]  # one backoff per provider retry, restarting for the new provider


def test_groq_output_is_not_merged_with_gemini_output():
    """Gemini never produced a draft; the plan is exactly Groq's, step for step."""
    groq_plan = DraftPlan(summary="Groq-only plan.", steps=[
        make_step(1, "teams", "send_message", message="Synthetic standup reminder")])
    service, _, _, _ = chain([gemini_503()], [groq_ok(groq_plan)])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.summary == "Groq-only plan."
    assert [(s.integration.value, s.action.value) for s in result.planned_steps] == [("teams", "send_message")]


# ======================= both providers fail =======================


@pytest.mark.parametrize("groq_error, code", [
    (groq_status_error(503), "PLANNER_API_ERROR"),
    (groq_status_error(429), "PLANNER_RATE_LIMITED"),
    (groq.APITimeoutError(request=GROQ_REQUEST), "PLANNER_TIMEOUT"),
    (groq_status_error(401), "PLANNER_AUTH_ERROR"),
])
def test_both_providers_failing_is_a_clear_planning_error(groq_error, code):
    service, gemini, groq_client, _ = chain([gemini_503()], [groq_error], attempts=2)
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED
    assert result.planned_steps == [] and result.executed is False and result.validation.is_valid is False
    assert result.fallback_used is True and result.planner == "groq"
    assert result.errors[-1].code == "ALL_PLANNERS_FAILED"
    assert "gemini, groq" in result.errors[-1].message and "Nothing was executed" in result.errors[-1].message
    assert code in [e.code for e in result.errors if e.provider == "groq"]
    assert len(gemini.models.calls) == 2
    assert len(groq_client.calls) == (2 if groq_error_retryable(groq_error) else 1)
    assert all("SYNTHETIC-PROVIDER-DETAIL" not in e.message for e in result.errors)


def groq_error_retryable(exc) -> bool:
    return not isinstance(exc, groq.AuthenticationError)


def test_plan_api_returns_502_when_both_providers_fail(monkeypatch):
    service, _, _, _ = chain([gemini_503()], [groq_status_error(503)])
    app.dependency_overrides[planning_api.get_planning_service] = lambda: service
    try:
        response = TestClient(app).post("/api/v1/workflows/plan", json={"instruction": EXAMPLE_INSTRUCTION})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "FAILED" and body["planned_steps"] == [] and body["executed"] is False
    assert body["errors"][-1]["code"] == "ALL_PLANNERS_FAILED" and body["fallback_used"] is True


# ======================= no fallback: credentials, refusals, schema, invalid plans =======================


@pytest.mark.parametrize("error", [
    api_error(errors.ClientError, 401, "UNAUTHENTICATED"),
    api_error(errors.ClientError, 403, "PERMISSION_DENIED"),
    api_error(errors.ClientError, 400, "INVALID_ARGUMENT", reason="API_KEY_INVALID"),
    api_error(errors.ClientError, 404, "NOT_FOUND"),
])
def test_invalid_credentials_or_request_never_fall_back(error):
    service, gemini, groq_client, slept = chain([error], [groq_ok()], attempts=3)
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and result.fallback_used is False
    assert len(gemini.models.calls) == 1 and groq_client.calls == [] and slept == []
    assert result.errors[-1].code in {"PLANNER_AUTH_ERROR", "PLANNER_REQUEST_REJECTED"}


@pytest.mark.parametrize("response", [
    gemini_response(None, finish="SAFETY"),
    gemini_response(None, block="BLOCKLIST"),
])
def test_unsafe_refusal_never_falls_back(response):
    service, gemini, groq_client, _ = chain([response], [groq_ok()], attempts=3)
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and result.errors[-1].code == "PLANNER_REFUSED"
    assert len(gemini.models.calls) == 1 and groq_client.calls == []


def test_schema_failure_is_retried_on_gemini_but_never_falls_back():
    service, gemini, groq_client, _ = chain([gemini_response("this is not json")], [groq_ok()], attempts=2)
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and result.fallback_used is False
    assert [e.code for e in result.errors] == ["INVALID_LLM_OUTPUT", "INVALID_LLM_OUTPUT"]
    assert len(gemini.models.calls) == 2 and groq_client.calls == []


def test_last_gemini_error_decides_rate_limit_then_schema_failure_does_not_fall_back():
    service, _, groq_client, _ = chain([gemini_429(), gemini_response("{bad")], [groq_ok()], attempts=2)
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and groq_client.calls == []


def test_provider_server_failure_falls_back_after_bounded_retries():
    """HTTP 500 is a provider failure: retried (bounded), then the fallback is used."""
    service, gemini, groq_client, _ = chain([api_error(errors.ServerError, 500, "INTERNAL")], [groq_ok()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and result.planner == "groq" and result.fallback_used is True
    assert len(gemini.models.calls) == 2 and len(groq_client.calls) == 1
    assert PROVIDER_UNAVAILABLE_HTTP_STATUSES == {408, 429, 500, 502, 503, 504}


def test_non_outage_status_does_not_fall_back():
    """A non-availability status (e.g. 501 Not Implemented) is retried but never falls back."""
    service, gemini, groq_client, _ = chain([api_error(errors.ServerError, 501, "NOT_IMPLEMENTED")], [groq_ok()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and len(gemini.models.calls) == 2 and groq_client.calls == []


def test_invalid_plan_from_gemini_is_rejected_without_fallback():
    bad = DraftPlan.model_validate({"summary": "s", "steps": [
        {"step_number": 1, "integration": "jira", "action": "create_issue", "description": "d", "arguments": []}]})
    service, _, groq_client, _ = chain([ok_response(bad)], [groq_ok()])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.INVALID and groq_client.calls == []
    assert [i.code for i in result.validation.issues] == ["UNSUPPORTED_INTEGRATION"]


@pytest.mark.parametrize("draft, code", [
    (DraftPlan.model_validate({"summary": "s", "steps": [
        {"step_number": 1, "integration": "jira", "action": "create_issue", "description": "d", "arguments": []}]}),
     "UNSUPPORTED_INTEGRATION"),
    (DraftPlan.model_validate({"summary": "s", "steps": [
        {"step_number": 1, "integration": "notion", "action": "delete_database", "description": "d",
         "arguments": []}]}), "UNSUPPORTED_ACTION"),
    (DraftPlan(summary="s", steps=[make_step(1, "teams", "send_message", message="run rm -rf / now")]),
     "UNSAFE_CONTENT"),
])
def test_groq_plans_pass_the_same_validator_and_allowlist(draft, code):
    service, _, groq_client, _ = chain([gemini_503()], [groq_ok(draft)])
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.INVALID and result.planner == "groq" and result.fallback_used is True
    assert code in [i.code for i in result.validation.issues] and result.planned_steps == []
    assert len(groq_client.calls) == 1  # an invalid plan is not retried and not sent elsewhere


def test_groq_unknown_document_is_rejected_in_mock_mode():
    draft = DraftPlan(summary="s", steps=[make_step(1, "google_drive", "read_document",
                                                   document_name="Not In The Catalog")])
    service, _, _, _ = chain([gemini_503()], [groq_ok(draft)], documents=frozenset({"Synthetic notes"}))
    service = PlanningService(service.planner, max_steps=10, max_planning_attempts=2,
                              available_documents=frozenset({"Synthetic notes"}))
    result = service.plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.INVALID
    assert [i.code for i in result.validation.issues] == ["DOCUMENT_NOT_FOUND"]


# ======================= Groq planner unit =======================


def test_groq_request_uses_strict_schema_of_the_same_pydantic_model_and_same_prompt():
    client = FakeGroqClient(groq_ok())
    planner = GroqPlanner(client=client, model=GROQ_MODEL, max_tokens=2048)
    assert planner.plan(EXAMPLE_INSTRUCTION, max_steps=7) == valid_draft()
    call = client.calls[0]
    assert call["model"] == GROQ_MODEL and call["max_completion_tokens"] == 2048 and call["temperature"] == 0
    assert call["messages"][0] == {"role": "system", "content": build_system_prompt(7)}  # same prompt/allow-list
    assert call["messages"][1]["content"].startswith("<instruction>\n")
    fmt = call["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == strict_json_schema(DraftPlan)
    step = fmt["json_schema"]["schema"]["properties"]["steps"]["items"]
    assert step["additionalProperties"] is False and set(step["required"]) == set(step["properties"])
    assert "$defs" not in json.dumps(fmt["json_schema"]["schema"])


def test_strict_schema_keeps_fields_named_title():
    schema = strict_json_schema(ProjectPlanDraft)
    item = schema["properties"]["action_items"]["items"]
    assert item["required"] == ["title", "description", "priority", "owner"]
    assert item["properties"]["priority"]["enum"] == ["High", "Medium", "Low"]


@pytest.mark.parametrize("response, code, retryable", [
    (groq_response("not json"), "INVALID_LLM_OUTPUT", True),
    (groq_response('{"summary": "x", "steps": "nope"}'), "INVALID_LLM_OUTPUT", True),
    (groq_response(None), "INVALID_LLM_OUTPUT", True),
    (groq_response('{"summary": "x"', finish="length"), "INVALID_LLM_OUTPUT", True),
    (groq_response(None, finish="content_filter"), "PLANNER_REFUSED", False),
])
def test_groq_invalid_responses(response, code, retryable):
    planner = GroqPlanner(client=FakeGroqClient(response), model=GROQ_MODEL, max_tokens=1024)
    with pytest.raises(PlannerError) as info:
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert (info.value.code, info.value.retryable, info.value.provider_unavailable) == (code, retryable, False)


@pytest.mark.parametrize("error, code, retryable, unavailable", [
    (groq_status_error(429), "PLANNER_RATE_LIMITED", True, True),
    (groq_status_error(503), "PLANNER_API_ERROR", True, True),
    (groq_status_error(500), "PLANNER_API_ERROR", True, True),
    (groq_status_error(502), "PLANNER_API_ERROR", True, True),
    (groq_status_error(501), "PLANNER_API_ERROR", True, False),
    (groq_status_error(401), "PLANNER_AUTH_ERROR", False, False),
    (groq_status_error(403), "PLANNER_AUTH_ERROR", False, False),
    (groq_status_error(400, "json_validate_failed"), "INVALID_LLM_OUTPUT", True, False),
    (groq_status_error(400), "PLANNER_REQUEST_REJECTED", False, False),
    (groq_status_error(404), "PLANNER_REQUEST_REJECTED", False, False),
    (groq.APITimeoutError(request=GROQ_REQUEST), "PLANNER_TIMEOUT", True, True),
    (groq.APIConnectionError(request=GROQ_REQUEST), "PLANNER_CONNECTION_ERROR", True, True),
])
def test_groq_errors_are_translated_without_provider_text(error, code, retryable, unavailable):
    planner = GroqPlanner(client=FakeGroqClient(error), model=GROQ_MODEL, max_tokens=1024)
    with pytest.raises(PlannerError) as info:
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert (info.value.code, info.value.retryable, info.value.provider_unavailable) == (code, retryable, unavailable)
    assert "SYNTHETIC-PROVIDER-DETAIL" not in info.value.message


def test_translate_reports_http_status():
    assert translate_api_error(groq_status_error(503)).http_status == 503


# ======================= configuration =======================


def test_auto_mode_builds_gemini_then_groq_without_network():
    planner = create_planner(auto_settings())
    assert isinstance(planner, FallbackPlanner)
    assert [(p.name, p.model) for p in planner.providers] == [("gemini", GEMINI_MODEL), ("groq", GROQ_MODEL)]
    assert ai_planner_problem(auto_settings()) is None and fallback_problem(auto_settings()) is None


def test_groq_client_has_sdk_retries_disabled():
    client = create_groq_client(auto_settings())
    assert isinstance(client, groq.Groq) and client.max_retries == 0


@pytest.mark.parametrize("overrides, setting", [({"groq_api_key": None}, "GROQ_API_KEY"),
                                                ({"groq_model": None}, "GROQ_MODEL")])
def test_missing_groq_config_keeps_gemini_and_reports_the_setting(overrides, setting):
    settings = auto_settings(**overrides)
    planner = create_planner(settings)
    assert [p.name for p in planner.providers] == ["gemini"]
    assert ai_planner_problem(settings) is None and setting in fallback_problem(settings)


@pytest.mark.parametrize("overrides, setting", [({"gemini_api_key": None}, "GEMINI_API_KEY"),
                                                ({"gemini_model": None}, "GEMINI_MODEL")])
def test_missing_primary_config_is_an_explicit_error(overrides, setting):
    settings = auto_settings(**overrides)
    with pytest.raises(PlannerConfigError, match=setting):
        create_planner(settings)
    assert setting in ai_planner_problem(settings)


def test_fallback_can_be_disabled_and_must_differ_from_primary():
    assert auto_settings(fallback_planner="").fallback_planner is None
    assert [p.name for p in create_planner(auto_settings(fallback_planner="none")).providers] == ["gemini"]
    with pytest.raises(ValueError, match="FALLBACK_PLANNER must differ"):
        auto_settings(fallback_planner="gemini")
    assert auto_settings().primary_planner is PlannerProvider.GEMINI


def test_env_variables_select_auto_mode(monkeypatch):
    for name, value in {"PLANNER_MODE": "auto", "PRIMARY_PLANNER": "gemini", "FALLBACK_PLANNER": "groq",
                        "GROQ_MODEL": GROQ_MODEL}.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert (settings.planner_mode.value, settings.primary_planner.value, settings.fallback_planner.value,
            settings.groq_model) == ("auto", "gemini", "groq", GROQ_MODEL)
    assert "GROQ_API_KEY" in fallback_problem(settings)  # conftest blanks every LLM key


def test_status_reports_primary_and_fallback_without_keys(monkeypatch):
    settings = integration_settings("mock", planner_mode="auto", gemini_model=GEMINI_MODEL, gemini_api_key=GEMINI_KEY,
                                    groq_model=GROQ_MODEL, groq_api_key=GROQ_KEY)
    monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
    response = TestClient(app).get("/api/v1/integrations/status")
    assert response.json()["planner"] == {
        "mode": "auto", "model": GROQ_MODEL, "ai_ready": True, "claude_ready": False, "issue": None,
        "primary": "groq", "fallback": "gemini", "fallback_model": GEMINI_MODEL, "fallback_ready": True,
        "fallback_issue": None}  # default order: Groq primary, Gemini fallback
    assert GEMINI_KEY not in response.text and GROQ_KEY not in response.text


def test_groq_key_is_redacted_from_logs_and_traces(caplog):
    install_secret_redaction()
    create_groq_client(auto_settings())  # registers the key
    assert GROQ_KEY not in repr(auto_settings())
    assert scrub_secrets(f"auth {GROQ_KEY}") == "auth [REDACTED]"
    assert GROQ_KEY not in scrub_text(f"Authorization: {GROQ_KEY}")
    assert "gsk_" not in scrub_text("leaked gsk_abcdefghijklmnopqrstuv1234")  # pattern, even if unregistered
    with caplog.at_level(logging.INFO):
        logging.getLogger("groq.synthetic").info("header authorization=%s", GROQ_KEY)
    assert GROQ_KEY not in caplog.text


# ======================= Langfuse =======================


def test_langfuse_records_both_provider_attempts_and_the_fallback_without_secrets():
    recorder = RecordingTelemetry()
    set_telemetry(recorder)
    install_secret_redaction()
    sensitive = f"{EXAMPLE_INSTRUCTION} {' '.join(SENSITIVE_MARKERS)}"
    service, _, _, _ = chain([gemini_503()], [groq_ok()], attempts=2)
    service.plan(sensitive)

    gemini_gens = recorder.named("gemini.models.generate_content")
    assert [g.error for g in gemini_gens] == ["PLANNER_API_ERROR", "PLANNER_API_ERROR"]
    assert gemini_gens[0].merged_metadata()["http_status"] == 503
    groq_gen = recorder.one("groq.chat.completions.create")
    assert groq_gen.as_type == "generation" and groq_gen.model == GROQ_MODEL and groq_gen.error is None
    assert next(u["usage_details"] for u in groq_gen.updates if u.get("usage_details")) == {
        "input": 800, "output": 250, "total": 1050}
    assert set(groq_gen.input) == {"instruction_chars", "system_prompt_chars"}

    attempts = [e["metadata"] for e in recorder.events_named("planning.provider_attempt")]
    assert [(a["provider"], a["model"], a["attempt"], a["outcome"], a["error_code"], a["http_status"])
            for a in attempts] == [("gemini", GEMINI_MODEL, 1, "error", "PLANNER_API_ERROR", 503),
                                   ("gemini", GEMINI_MODEL, 2, "error", "PLANNER_API_ERROR", 503),
                                   ("groq", GROQ_MODEL, 1, "draft", None, None)]
    (fallback,) = recorder.events_named("planning.fallback")
    assert fallback["metadata"]["from_provider"] == "gemini" and fallback["metadata"]["to_provider"] == "groq"
    assert fallback["metadata"]["reason"] == "PLANNER_API_ERROR" and fallback["level"] == "WARNING"
    assert recorder.one("planning.fallback").name == "planning.fallback"  # the graph node is traced too

    request = recorder.one("planning.request")
    assert request.metadata["providers"] == ["gemini", "groq"]
    assert request.output["provider"] == "groq" and request.output["fallback_used"] is True
    assert len(request.output["provider_attempts"]) == 3

    dump = recorder.dump()
    assert not [m for m in SENSITIVE_MARKERS if m in dump]
    assert GEMINI_KEY not in dump and GROQ_KEY not in dump and "SYNTHETIC-PROVIDER-DETAIL" not in dump


def test_langfuse_records_both_failures_when_everything_fails():
    recorder = RecordingTelemetry()
    set_telemetry(recorder)
    service, _, _, _ = chain([gemini_429()], [groq_status_error(503)], attempts=1)
    service.plan(EXAMPLE_INSTRUCTION)
    attempts = [(e["metadata"]["provider"], e["metadata"]["error_code"], e["metadata"]["http_status"])
                for e in recorder.events_named("planning.provider_attempt")]
    assert attempts == [("gemini", "PLANNER_RATE_LIMITED", 429), ("groq", "PLANNER_API_ERROR", 503)]
    assert recorder.one("groq.chat.completions.create").error == "PLANNER_API_ERROR"
    assert recorder.one("planning.request").output["error_codes"][-1] == "ALL_PLANNERS_FAILED"


# ======================= HTTP API reporting =======================


def test_plan_api_reports_fallback_provider_and_model():
    service, _, _, _ = chain([gemini_503()], [groq_ok()])
    app.dependency_overrides[planning_api.get_planning_service] = lambda: service
    try:
        response = TestClient(app).post("/api/v1/workflows/plan", json={"instruction": EXAMPLE_INSTRUCTION})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["planner"], body["planner_model"], body["primary_planner"]) == ("groq", GROQ_MODEL, "gemini")
    assert body["fallback_used"] is True and body["fallback_reason"] == "PLANNER_API_ERROR"
    assert [a["provider"] for a in body["provider_attempts"]] == ["gemini", "gemini", "groq"]
    assert body["executed"] is False


def test_plan_api_production_wiring_builds_the_auto_chain(monkeypatch):
    settings = auto_settings()
    monkeypatch.setattr(planning_api, "get_settings", lambda: settings)
    planning_api._default_service.cache_clear()
    built = planning_api._default_service()
    assert isinstance(built.planner, FallbackPlanner)
    assert [p.name for p in built.planner.providers] == ["gemini", "groq"]


# ======================= Notion project workflow + approval protections =======================


@pytest.fixture
def auto_project(monkeypatch):
    """Real-mode Notion project endpoint in auto mode with fake Gemini and Groq clients."""

    def install(gemini_outcomes, groq_outcomes, **overrides):
        settings = integration_settings(
            "real", smoke_test_notion_parent="engineering", notion_real_writes_sandbox_only=True,
            planner_mode="auto", gemini_model=GEMINI_MODEL, gemini_api_key=GEMINI_KEY, groq_model=GROQ_MODEL,
            groq_api_key=GROQ_KEY, max_planning_attempts=2, **overrides)
        monkeypatch.setattr(project_api, "get_settings", lambda: settings)
        monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
        monkeypatch.setattr(project_api, "get_planning_sleeper", lambda: (lambda _s: None))
        gemini = FakeGenaiClient(*gemini_outcomes)
        groq_client = FakeGroqClient(*groq_outcomes)
        app.dependency_overrides[project_api.get_project_source_factory] = lambda: (lambda s, m: FallbackPlanner([
            GeminiProjectPlanner(client=gemini, model=GEMINI_MODEL, max_output_tokens=4096),
            GroqProjectPlanner(client=groq_client, model=GROQ_MODEL, max_tokens=4096)]))
        return gemini, groq_client

    yield install
    app.dependency_overrides.clear()


def _project_json(project=None) -> str:
    return (project or rewards_project()).model_dump_json()


def test_project_plan_falls_back_to_groq_and_is_labelled(auto_project):
    gemini, groq_client = auto_project([gemini_503()], [groq_response(_project_json())])
    response = TestClient(app).post(PROJECT_URL, json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["planner"], body["model"], body["planner_model"]) == ("groq", GROQ_MODEL, GROQ_MODEL)
    assert body["fallback_used"] is True and body["primary_planner"] == "gemini" and body["ai_generated"] is True
    assert body["destination_alias"] == "engineering" and body["page_count"] == 5 and body["executed"] is False
    assert body["project"]["project_title"] == rewards_project().project_title
    assert len(gemini.models.calls) == 2 and len(groq_client.calls) == 1
    fmt = groq_client.calls[0]["response_format"]["json_schema"]
    assert fmt["name"] == "ProjectPlanDraft" and fmt["schema"] == strict_json_schema(ProjectPlanDraft)
    assert GEMINI_KEY not in response.text and GROQ_KEY not in response.text


def test_project_plan_from_gemini_when_available(auto_project):
    _, groq_client = auto_project([gemini_response(_project_json())], [groq_response(_project_json())])
    body = TestClient(app).post(PROJECT_URL, json={"instruction": PROJECT_INSTRUCTION, "mode": "real"}).json()
    assert body["planner"] == "gemini" and body["model"] == GEMINI_MODEL and body["fallback_used"] is False
    assert groq_client.calls == []


def test_groq_named_person_owner_is_rejected_like_gemini(auto_project):
    project = rewards_project()
    project.action_items[0] = project.action_items[0].model_copy(update={"owner": "Synthetic Person"})
    auto_project([gemini_503()], [groq_response(_project_json(project))])
    response = TestClient(app).post(PROJECT_URL, json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    assert response.status_code == 422 and response.json()["planner"] == "groq"
    assert [i["code"] for i in response.json()["validation"]["issues"]] == ["OWNER_NOT_A_TEAM"]
    assert response.json()["real_write"] is False and response.json()["planned_steps"] == []


def test_project_plan_both_providers_fail_returns_502_and_nothing_to_execute(auto_project):
    auto_project([gemini_429()], [groq.APIConnectionError(request=GROQ_REQUEST)])
    response = TestClient(app).post(PROJECT_URL, json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "FAILED" and body["planned_steps"] == [] and body["real_write"] is False
    assert body["page_count"] == 0 and body["errors"][-1]["code"] == "ALL_PLANNERS_FAILED"


def test_project_auth_error_on_gemini_does_not_fall_back(auto_project):
    _, groq_client = auto_project([api_error(errors.ClientError, 401, "UNAUTHENTICATED")],
                                  [groq_response(_project_json())])
    response = TestClient(app).post(PROJECT_URL, json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    assert response.status_code == 502 and groq_client.calls == []
    assert response.json()["errors"][-1]["code"] == "PLANNER_AUTH_ERROR"


@pytest.fixture
def groq_plan(auto_project):
    """A real-mode project plan that came from the Groq fallback (nothing executed yet)."""
    auto_project([gemini_503()], [groq_response(_project_json())])
    body = TestClient(app).post(PROJECT_URL, json={"instruction": PROJECT_INSTRUCTION, "mode": "real"}).json()
    assert body["status"] == "PLANNED" and body["planner"] == "groq"
    return body


def test_planning_via_fallback_writes_nothing(groq_plan):
    world = real_world()
    assert world.remote_calls() == 0 and world.factories_called == []


def test_fallback_plan_still_requires_human_approval(groq_plan):
    world = real_world()
    request = execution_request(groq_plan)
    request = request.model_copy(update={"approval": request.approval.model_copy(update={"approved": False})})
    response = world.service.execute(request, write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED and response.errors[0].code == "APPROVAL_REQUIRED"
    assert world.remote_calls() == 0


def test_fallback_plan_still_requires_real_write_authorization_and_kill_switch(groq_plan):
    no_token = real_world()
    assert [e.code for e in no_token.service.execute(execution_request(groq_plan), write_authorized=False).errors] \
        == ["REAL_WRITE_AUTHORIZATION_REQUIRED"]
    off = real_world(real_writes_enabled=False)
    assert [e.code for e in off.service.execute(execution_request(groq_plan), write_authorized=True).errors] \
        == ["REAL_WRITES_DISABLED"]
    assert no_token.remote_calls() == 0 and off.remote_calls() == 0


def test_fallback_plan_is_confined_to_the_sandbox_parent(groq_plan):
    tampered = json.loads(json.dumps(groq_plan))
    tampered["planned_steps"][0]["arguments"]["parent"] = "tasks"
    world = real_world()
    response = world.service.execute(execution_request(tampered), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]
    assert world.remote_calls() == 0


def test_fallback_plan_executes_once_idempotently_and_rolls_back(groq_plan):
    from tests.integration_fakes import notion_error

    world = real_world()
    request = execution_request(groq_plan)
    original = world.notion._api_post_page
    calls = {"n": 0}

    def post(args):
        calls["n"] += 1
        return notion_error(400, "validation_error") if calls["n"] == 3 else original(args)

    world.notion._api_post_page = post
    first = world.service.execute(request, write_authorized=True)
    assert first.status is ExecutionStatus.ROLLED_BACK
    assert [c.step_number for c in first.compensation_results] == [2, 1]
    posts = len(world.notion.calls_named("API-post-page"))
    second = world.service.execute(request, write_authorized=True)  # duplicate request: replay, no new calls
    assert second.replayed is True and len(world.notion.calls_named("API-post-page")) == posts


@pytest.mark.parametrize("mode", ["mock", "dry_run"])
def test_mock_and_dry_run_modes_still_work_in_auto_mode(auto_project, mode):
    auto_project([gemini_503()], [groq_response(_project_json())])
    response = TestClient(app).post(PROJECT_URL, json={"instruction": PROJECT_INSTRUCTION, "mode": mode})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == mode and body["planner"] == "groq" and body["real_write"] is False
