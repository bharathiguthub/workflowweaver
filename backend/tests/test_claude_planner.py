"""Claude planner tests. A fake client replaces anthropic.Anthropic: no real API calls."""

import anthropic
import httpx2
import pytest

from app.agents.planners import ClaudePlanner, PlannerConfigError, PlannerError, create_planner
from app.agents.service import PlanningService
from app.core.config import Settings
from app.schemas.plan import DraftPlan, PlanStatus
from tests.conftest import EXAMPLE_INSTRUCTION, FakeAnthropicClient, fake_response, make_step, valid_draft

REQUEST = httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")


def status_error(cls, code):
    return cls("synthetic error", response=httpx2.Response(code, request=REQUEST), body=None)


def planner_with(outcome) -> tuple[ClaudePlanner, FakeAnthropicClient]:
    client = FakeAnthropicClient(outcome)
    return ClaudePlanner(client=client, model="test-model-from-config", max_tokens=1024), client


def test_successful_structured_plan_and_request_shape():
    planner, client = planner_with(fake_response(valid_draft()))
    draft = planner.plan(EXAMPLE_INSTRUCTION, max_steps=7)

    assert draft == valid_draft()
    call = client.messages.calls[0]
    assert call["model"] == "test-model-from-config"
    assert call["output_format"] is DraftPlan
    assert "<instruction>" in call["messages"][0]["content"]
    assert "At most 7 steps" in call["system"]
    assert "action=send_message" in call["system"]  # catalog embedded in the prompt


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (anthropic.APITimeoutError(request=REQUEST), "PLANNER_TIMEOUT", True),
        (anthropic.APIConnectionError(request=REQUEST), "PLANNER_CONNECTION_ERROR", True),
        (status_error(anthropic.RateLimitError, 429), "PLANNER_RATE_LIMITED", True),
        (status_error(anthropic.InternalServerError, 500), "PLANNER_API_ERROR", True),
        (status_error(anthropic.AuthenticationError, 401), "PLANNER_AUTH_ERROR", False),
        (status_error(anthropic.PermissionDeniedError, 403), "PLANNER_AUTH_ERROR", False),
        (status_error(anthropic.BadRequestError, 400), "PLANNER_REQUEST_REJECTED", False),
        (status_error(anthropic.NotFoundError, 404), "PLANNER_REQUEST_REJECTED", False),
    ],
)
def test_api_errors_are_translated(error, code, retryable):
    planner, _ = planner_with(error)
    with pytest.raises(PlannerError) as info:
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert (info.value.code, info.value.retryable) == (code, retryable)


def test_schema_mismatch_is_invalid_llm_output():
    def bad_output():
        return DraftPlan.model_validate({"summary": "x", "steps": "not-a-list"})  # raises ValidationError

    planner, _ = planner_with(bad_output)
    with pytest.raises(PlannerError) as info:
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert info.value.code == "INVALID_LLM_OUTPUT" and info.value.retryable


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (fake_response(None), "INVALID_LLM_OUTPUT", True),
        (fake_response(None, stop_reason="max_tokens"), "INVALID_LLM_OUTPUT", True),
        (fake_response(None, stop_reason="refusal"), "PLANNER_REFUSED", False),
    ],
)
def test_missing_truncated_or_refused_output(response, code, retryable):
    planner, _ = planner_with(response)
    with pytest.raises(PlannerError) as info:
        planner.plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert (info.value.code, info.value.retryable) == (code, retryable)


def test_hallucinated_tool_from_llm_is_rejected_by_graph():
    hallucinated = DraftPlan(
        summary="s",
        steps=[make_step(1, "bash", "run_command", command="rm -rf /")],
    )
    planner, _ = planner_with(fake_response(hallucinated))
    response = PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION)
    assert response.status is PlanStatus.INVALID
    assert response.validation.issues[0].code == "UNSUPPORTED_INTEGRATION"
    assert response.planned_steps == []


def test_claude_timeouts_exhaust_retries_through_graph():
    planner, client = planner_with(anthropic.APITimeoutError(request=REQUEST))
    response = PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION)
    assert response.status is PlanStatus.FAILED
    assert response.planning_attempts == 2
    assert len(client.messages.calls) == 2
    assert {e.code for e in response.errors} == {"PLANNER_TIMEOUT"}


# ---------- factory / configuration ----------


def test_factory_defaults_to_mock():
    assert create_planner(Settings(_env_file=None)).name == "mock"


def test_factory_claude_mode_requires_model():
    with pytest.raises(PlannerConfigError):
        create_planner(Settings(_env_file=None, planner_mode="claude"))


def test_factory_builds_claude_planner_without_calling_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-real-key")
    planner = create_planner(
        Settings(_env_file=None, planner_mode="claude", anthropic_model="test-model-from-config")
    )
    assert isinstance(planner, ClaudePlanner)
