"""Shared test helpers. All data is synthetic; no test touches the network."""

from types import SimpleNamespace

import pytest

from app.agents.planners.base import PlannerError
from app.schemas.plan import DraftPlan, DraftStep, StepArgument

EXAMPLE_INSTRUCTION = (
    "Read meeting notes, create a Notion project page, save a summary to Google Drive, "
    "and notify the team in Microsoft Teams."
)


def make_step(n, integration, action, depends_on=(), **arguments) -> DraftStep:
    return DraftStep(
        step_number=n,
        integration=integration,
        action=action,
        description=f"Synthetic step {n}",
        arguments=[StepArgument(name=k, value=v) for k, v in arguments.items()],
        depends_on=list(depends_on),
    )


def valid_draft() -> DraftPlan:
    return DraftPlan(
        summary="Synthetic three-step plan.",
        steps=[
            make_step(1, "notion", "create_page", title="Sprint 42 kickoff", content="Synthetic notes"),
            make_step(2, "google_drive", "create_document", title="Sprint 42 summary", content="Synthetic summary"),
            make_step(3, "teams", "send_message", depends_on=[1, 2], message="Plan is ready"),
        ],
    )


class StubPlanner:
    """Returns queued outcomes in order: a DraftPlan is returned, an exception is raised."""

    name = "stub"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def plan(self, instruction: str, *, max_steps: int) -> DraftPlan:
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def timeout_error() -> PlannerError:
    return PlannerError("PLANNER_TIMEOUT", "Claude request timed out.", retryable=True)


class FakeMessages:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome()
        return self.outcome


class FakeAnthropicClient:
    """Stands in for anthropic.Anthropic so tests never make real API calls."""

    def __init__(self, outcome) -> None:
        self.messages = FakeMessages(outcome)


def fake_response(parsed_output, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        parsed_output=parsed_output,
        stop_reason=stop_reason,
        _request_id="req_synthetic",
        usage=SimpleNamespace(output_tokens=42),
    )


@pytest.fixture(autouse=True)
def _no_real_telemetry():
    # Tests never export telemetry, even if the developer's .env sets LANGFUSE_ENABLED=true.
    # Telemetry tests install their own recording/faulty implementations explicitly.
    from app.observability import NoopTelemetry, set_telemetry

    set_telemetry(NoopTelemetry())
    yield
    set_telemetry(NoopTelemetry())


@pytest.fixture(autouse=True)
def _no_real_anthropic_key(monkeypatch):
    # Guarantee no test can authenticate against the real API, even if the developer's
    # shell has a key set.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
