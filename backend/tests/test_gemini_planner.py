"""Gemini planner tests. A fake client replaces google.genai.Client: no real API call is
ever made (and the conftest blocks every non-loopback connection anyway). Responses are
real google.genai.types objects, so the SDK's own response model is exercised."""

import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import errors, types

from app.agents.planners import GeminiPlanner, PlannerConfigError, PlannerError, create_planner
from app.agents.planners.factory import ai_planner_problem, create_gemini_client
from app.agents.planners.gemini import gemini_usage_details
from app.agents.project_workflow import EXAMPLE_INSTRUCTION as PROJECT_INSTRUCTION
from app.agents.project_workflow import GeminiProjectPlanner, ProjectPlanDraft
from app.agents.service import PlanningService
from app.api import integrations as integrations_api
from app.api import project as project_api
from app.core.config import Settings
from app.core.log_redaction import install_secret_redaction
from app.integrations.secrets import scrub_secrets
from app.main import app
from app.observability import set_telemetry
from app.schemas.plan import DraftPlan, PlanStatus
from tests.conftest import EXAMPLE_INSTRUCTION, valid_draft
from tests.integration_fakes import integration_settings
from tests.telemetry_helpers import SENSITIVE_MARKERS, RecordingTelemetry

MODEL = "gemini-synthetic-flash"
KEY = "AIzaSyNOT-A-REAL-KEY-synthetic-0010"


class FakeModels:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeGenaiClient:
    """Stands in for google.genai.Client."""

    def __init__(self, *outcomes) -> None:
        self.models = FakeModels(*outcomes)


def gemini_response(text: str | None, *, finish: str = "STOP", parsed=None, block: str | None = None,
                    usage: bool = True) -> types.GenerateContentResponse:
    parts = [types.Part(text=text)] if text is not None else []
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        parsed=parsed,
        response_id="resp_synthetic",
        model_version=MODEL,
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=block) if block else None,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=900, candidates_token_count=300, thoughts_token_count=120, total_token_count=1320,
        ) if usage else None,
    )


def ok_response(model=None) -> types.GenerateContentResponse:
    return gemini_response((model or valid_draft()).model_dump_json())


def api_error(cls, code: int, status: str, *, reason: str | None = None) -> errors.APIError:
    error: dict = {"code": code, "message": f"synthetic {status} SYNTHETIC-PROVIDER-DETAIL", "status": status}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return cls(code, {"error": error})


def planner_with(*outcomes) -> tuple[GeminiPlanner, FakeGenaiClient]:
    client = FakeGenaiClient(*outcomes)
    return GeminiPlanner(client=client, model=MODEL, max_output_tokens=4096), client


def gemini_settings(**overrides) -> Settings:
    values = dict(_env_file=None, planner_mode="gemini", gemini_model=MODEL, gemini_api_key=KEY)
    values.update(overrides)
    return Settings(**values)


# ======================= success =======================


def test_success_returns_validated_plan_and_request_shape():
    planner, client = planner_with(ok_response())
    assert planner.plan(EXAMPLE_INSTRUCTION, max_steps=7) == valid_draft()
    call = client.models.calls[0]
    assert call["model"] == MODEL
    assert call["contents"].startswith("<instruction>\n") and EXAMPLE_INSTRUCTION in call["contents"]
    config: types.GenerateContentConfig = call["config"]
    assert config.response_mime_type == "application/json" and config.response_schema is DraftPlan
    assert config.max_output_tokens == 4096
    assert "At most 7 steps" in config.system_instruction and "action=send_message" in config.system_instruction


def test_sdk_parsed_object_is_used_when_present():
    planner, _ = planner_with(gemini_response("not json at all", parsed=valid_draft()))
    assert planner.plan(EXAMPLE_INSTRUCTION, max_steps=10) == valid_draft()


def test_usage_details_include_thinking_tokens_as_output():
    assert gemini_usage_details(ok_response()) == {"input": 900, "output": 420, "output_thinking_tokens": 120,
                                                   "total": 1320}
    assert gemini_usage_details(gemini_response("{}", usage=False)) == {}


# ======================= invalid responses =======================


@pytest.mark.parametrize("response, code, retryable", [
    (gemini_response("this is not json"), "INVALID_LLM_OUTPUT", True),
    (gemini_response('{"summary": "x", "steps": "not-a-list"}'), "INVALID_LLM_OUTPUT", True),
    (gemini_response(None), "INVALID_LLM_OUTPUT", True),
    (gemini_response('{"summary": "x", "st', finish="MAX_TOKENS"), "INVALID_LLM_OUTPUT", True),
    (gemini_response(None, finish="SAFETY"), "PLANNER_REFUSED", False),
    (gemini_response(None, finish="PROHIBITED_CONTENT"), "PLANNER_REFUSED", False),
    (gemini_response(None, block="BLOCKLIST"), "PLANNER_REFUSED", False),
])
def test_invalid_responses_are_planner_errors(response, code, retryable):
    planner, _ = planner_with(response)
    with pytest.raises(PlannerError) as info:
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert (info.value.code, info.value.retryable) == (code, retryable)


def test_truncation_message_names_the_setting():
    planner, _ = planner_with(gemini_response("{", finish="MAX_TOKENS"))
    with pytest.raises(PlannerError, match="GEMINI_MAX_OUTPUT_TOKENS"):
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)


# ======================= API errors and rate limits =======================


@pytest.mark.parametrize("error, code, retryable", [
    (api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), "PLANNER_RATE_LIMITED", True),
    (api_error(errors.ClientError, 401, "UNAUTHENTICATED"), "PLANNER_AUTH_ERROR", False),
    (api_error(errors.ClientError, 403, "PERMISSION_DENIED"), "PLANNER_AUTH_ERROR", False),
    (api_error(errors.ClientError, 400, "INVALID_ARGUMENT", reason="API_KEY_INVALID"), "PLANNER_AUTH_ERROR", False),
    (api_error(errors.ClientError, 400, "INVALID_ARGUMENT"), "PLANNER_REQUEST_REJECTED", False),
    (api_error(errors.ClientError, 404, "NOT_FOUND"), "PLANNER_REQUEST_REJECTED", False),
    (api_error(errors.ServerError, 500, "INTERNAL"), "PLANNER_API_ERROR", True),
    (api_error(errors.ServerError, 503, "UNAVAILABLE"), "PLANNER_API_ERROR", True),
    (api_error(errors.ClientError, 409, "ABORTED"), "PLANNER_API_ERROR", False),
    (httpx.ReadTimeout("synthetic timeout"), "PLANNER_TIMEOUT", True),
    (httpx.ConnectError("synthetic refused"), "PLANNER_CONNECTION_ERROR", True),
])
def test_api_errors_are_translated(error, code, retryable):
    planner, _ = planner_with(error)
    with pytest.raises(PlannerError) as info:
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert (info.value.code, info.value.retryable) == (code, retryable)
    assert "SYNTHETIC-PROVIDER-DETAIL" not in info.value.message  # provider text is never copied


def test_rate_limit_is_retried_by_the_langgraph_planner_then_succeeds():
    planner, client = planner_with(api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), ok_response())
    result = PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.PLANNED and result.planning_attempts == 2 and result.planner == "gemini"
    assert len(client.models.calls) == 2


def test_persistent_rate_limit_fails_after_bounded_attempts():
    planner, client = planner_with(api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"))
    result = PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION)
    # One error per attempt; the graph stops after MAX_PLANNING_ATTEMPTS.
    assert result.status is PlanStatus.FAILED
    assert [e.code for e in result.errors] == ["PLANNER_RATE_LIMITED", "PLANNER_RATE_LIMITED"]
    assert len(client.models.calls) == 2


def test_auth_error_is_not_retried():
    planner, client = planner_with(api_error(errors.ClientError, 401, "UNAUTHENTICATED"))
    result = PlanningService(planner, max_steps=10, max_planning_attempts=3).plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.FAILED and len(client.models.calls) == 1


def test_invalid_plan_from_gemini_goes_through_the_same_validator():
    bad = DraftPlan.model_validate({"summary": "s", "steps": [
        {"step_number": 1, "integration": "jira", "action": "create_issue", "description": "d", "arguments": []}]})
    planner, _ = planner_with(ok_response(bad))
    result = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION)
    assert result.status is PlanStatus.INVALID
    assert [i.code for i in result.validation.issues] == ["UNSUPPORTED_INTEGRATION"]


# ======================= configuration and credentials =======================


@pytest.mark.parametrize("overrides, setting", [
    ({"gemini_api_key": None}, "GEMINI_API_KEY"),
    ({"gemini_api_key": "   "}, "GEMINI_API_KEY"),
    ({"gemini_model": None}, "GEMINI_MODEL"),
])
def test_missing_credentials_or_model_are_explicit_config_errors(overrides, setting):
    settings = gemini_settings(**overrides)
    with pytest.raises(PlannerConfigError, match=setting):
        create_planner(settings)
    assert setting in ai_planner_problem(settings)


def test_factory_builds_gemini_planner_without_any_network_call():
    planner = create_planner(gemini_settings())
    assert isinstance(planner, GeminiPlanner) and planner.name == "gemini" and planner.model == MODEL


def test_key_from_environment_is_used_when_not_in_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    settings = Settings(_env_file=None, planner_mode="gemini", gemini_model=MODEL)
    assert ai_planner_problem(settings) is None


def test_api_key_is_never_logged_or_repr(caplog):
    install_secret_redaction()
    create_gemini_client(gemini_settings())  # registers the key for redaction
    assert KEY not in repr(gemini_settings())
    assert scrub_secrets(f"request failed key={KEY}") == "request failed key=[REDACTED]"
    with caplog.at_level(logging.INFO):
        logging.getLogger("google_genai.synthetic").info("headers x-goog-api-key=%s", KEY)
    assert KEY not in caplog.text


def test_plan_api_returns_503_naming_the_missing_key(monkeypatch):
    monkeypatch.setenv("PLANNER_MODE", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", MODEL)  # GEMINI_API_KEY is "" (conftest), overriding any .env value
    from app.api import planning as planning_api
    from app.core.config import get_settings

    get_settings.cache_clear()
    planning_api._default_service.cache_clear()
    response = TestClient(app).post("/api/v1/workflows/plan", json={"instruction": EXAMPLE_INSTRUCTION})
    assert response.status_code == 503 and "GEMINI_API_KEY" in response.json()["detail"]


# ======================= Langfuse =======================


def test_generation_records_model_usage_finish_reason_and_no_prompt():
    recorder = RecordingTelemetry()
    set_telemetry(recorder)
    sensitive = f"{EXAMPLE_INSTRUCTION} {' '.join(SENSITIVE_MARKERS)}"
    planner, _ = planner_with(ok_response())
    PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(sensitive)

    generation = recorder.one("gemini.models.generate_content")
    assert generation.as_type == "generation" and generation.model == MODEL
    assert generation.model_parameters == {"max_output_tokens": 4096}
    assert generation.parent.name == "planning.planner" and generation.parent.parent.name == "planning.request"
    usage = next(u["usage_details"] for u in generation.updates if u.get("usage_details"))
    assert usage == {"input": 900, "output": 420, "output_thinking_tokens": 120, "total": 1320}
    meta = generation.merged_metadata()
    assert meta["finish_reason"] == "STOP" and meta["response_id"] == "resp_synthetic"
    assert set(generation.input) == {"instruction_chars", "system_prompt_chars"}
    dump = recorder.dump()
    assert not [m for m in SENSITIVE_MARKERS if m in dump]


def test_failed_generation_is_still_recorded():
    recorder = RecordingTelemetry()
    set_telemetry(recorder)
    planner, _ = planner_with(api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"))
    PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION)
    generation = recorder.one("gemini.models.generate_content")
    assert generation.error == "PLANNER_RATE_LIMITED"


# ======================= Notion project workflow =======================


def _project() -> ProjectPlanDraft:
    from tests.test_project_workflow import rewards_project

    return rewards_project()


def test_gemini_project_planner_uses_the_project_schema():
    client = FakeGenaiClient(gemini_response(_project().model_dump_json()))
    planner = GeminiProjectPlanner(client=client, model=MODEL, max_output_tokens=4096)
    assert planner.project_plan(PROJECT_INSTRUCTION, max_items=9) == _project()
    config = client.models.calls[0]["config"]
    assert config.response_schema is ProjectPlanDraft and "TEAM, never a person" in config.system_instruction


@pytest.fixture
def project_endpoint(monkeypatch):
    def install(outcome, **overrides):
        settings = integration_settings("real", smoke_test_notion_parent="engineering", planner_mode="gemini",
                                        gemini_model=MODEL, gemini_api_key=KEY, **overrides)
        monkeypatch.setattr(project_api, "get_settings", lambda: settings)
        monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
        client = FakeGenaiClient(outcome)
        app.dependency_overrides[project_api.get_project_source_factory] = lambda: (
            lambda s, m: GeminiProjectPlanner(client=client, model=s.gemini_model, max_output_tokens=4096))
        return client

    yield install
    app.dependency_overrides.clear()


def test_real_mode_project_plan_from_gemini_is_labelled(project_endpoint):
    project_endpoint(gemini_response(_project().model_dump_json()))
    response = TestClient(app).post("/api/v1/workflows/notion-project/plan",
                                    json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["planner"] == "gemini" and body["model"] == MODEL and body["ai_generated"] is True
    assert body["real_write"] is True and body["destination_alias"] == "engineering" and body["page_count"] == 5
    assert KEY not in response.text


def test_gemini_named_person_owner_is_still_rejected(project_endpoint):
    project = _project()
    project.action_items[0] = project.action_items[0].model_copy(update={"owner": "Synthetic Person"})
    project_endpoint(gemini_response(project.model_dump_json()))
    response = TestClient(app).post("/api/v1/workflows/notion-project/plan",
                                    json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    assert response.status_code == 422
    assert [i["code"] for i in response.json()["validation"]["issues"]] == ["OWNER_NOT_A_TEAM"]


def test_default_source_builds_gemini_and_real_mode_needs_its_key(monkeypatch):
    ready = integration_settings("real", smoke_test_notion_parent="engineering", planner_mode="gemini",
                                 gemini_model=MODEL, gemini_api_key=KEY)
    from app.core.config import IntegrationMode

    assert isinstance(project_api.default_source(ready, IntegrationMode.REAL), GeminiProjectPlanner)
    missing = integration_settings("real", smoke_test_notion_parent="engineering", planner_mode="gemini",
                                   gemini_model=MODEL, gemini_api_key=None)
    monkeypatch.setattr(project_api, "get_settings", lambda: missing)
    response = TestClient(app).post("/api/v1/workflows/notion-project/plan",
                                    json={"instruction": PROJECT_INSTRUCTION, "mode": "real"})
    assert response.status_code == 503 and "GEMINI_API_KEY" in response.json()["detail"]


def test_status_reports_gemini_without_the_key(monkeypatch):
    settings = integration_settings("real", planner_mode="gemini", gemini_model=MODEL, gemini_api_key=KEY)
    monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
    response = TestClient(app).get("/api/v1/integrations/status")
    assert response.json()["planner"] == {"mode": "gemini", "model": MODEL, "ai_ready": True, "claude_ready": False,
                                          "issue": None}
    assert KEY not in response.text
