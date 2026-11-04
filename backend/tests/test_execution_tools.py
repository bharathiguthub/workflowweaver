"""Unit tests for mock tool adapters, the registry, failure injection and the retry policy."""

import random

import pytest

from app.agents.catalog import ACTION_CATALOG
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.tools import (
    CompensationCall,
    Effect,
    ErrorKind,
    FailureInjector,
    Fault,
    MockToolRegistry,
    ToolCall,
    ToolError,
    create_tool_registry,
)
from app.core.config import Settings
from app.schemas.plan import Action
from app.schemas.workflow import Integration

WF = "00000000-0000-4000-8000-000000000001"


def page_call(step=1, **overrides) -> ToolCall:
    args = {"title": "Synthetic page", "content": "Synthetic content"}
    return ToolCall(WF, step, Integration.NOTION, Action.CREATE_PAGE, {**args, **overrides})


def test_every_catalog_action_has_a_mock_adapter():
    expected = {(i, a) for i, actions in ACTION_CATALOG.items() for a in actions}
    assert MockToolRegistry().supported_actions() == expected


def test_create_is_deterministic_and_idempotent_per_key():
    registry = MockToolRegistry()
    first = registry.execute(page_call())
    second = registry.execute(page_call())
    assert first.resource_id == second.resource_id
    assert first.resource_id.startswith("mock-notion-page-")
    assert (first.replayed, second.replayed) == (False, True)
    assert registry.store.side_effects == 1
    assert len(registry.store.live_resources()) == 1


def test_read_document_is_read_only_and_reads_fixtures():
    registry = MockToolRegistry()
    call = ToolCall(WF, 1, Integration.GOOGLE_DRIVE, Action.READ_DOCUMENT, {"document_name": "Synthetic sprint backlog"})
    result = registry.execute(call)
    assert result.effect is Effect.READ_ONLY
    assert "WW-1" in result.output["content"]
    assert registry.store.side_effects == 0


def test_read_unknown_document_is_permanent_error():
    call = ToolCall(WF, 1, Integration.GOOGLE_DRIVE, Action.READ_DOCUMENT, {"document_name": "nope"})
    with pytest.raises(ToolError) as info:
        MockToolRegistry().execute(call)
    assert (info.value.kind, info.value.code) == (ErrorKind.PERMANENT, "DOCUMENT_NOT_FOUND")


def test_adapter_rechecks_arguments():
    with pytest.raises(ToolError) as info:
        MockToolRegistry().execute(page_call(content=""))
    assert info.value.code == "INVALID_ARGUMENTS"
    assert not info.value.retryable


def test_compensation_is_idempotent():
    registry = MockToolRegistry()
    rid = registry.execute(page_call()).resource_id
    comp = CompensationCall(WF, 1, Integration.NOTION, Action.CREATE_PAGE, rid)
    assert registry.compensate(comp) is True
    assert registry.compensate(comp) is False  # already undone
    assert registry.store.live_resources() == {}


def test_teams_message_is_not_compensatable():
    registry = MockToolRegistry()
    call = ToolCall(WF, 1, Integration.TEAMS, Action.SEND_MESSAGE, {"message": "Synthetic"})
    rid = registry.execute(call).resource_id
    with pytest.raises(ToolError) as info:
        registry.compensate(CompensationCall(WF, 1, Integration.TEAMS, Action.SEND_MESSAGE, rid))
    assert info.value.code == "NOT_COMPENSATABLE"
    assert len(registry.store.messages) == 1


@pytest.mark.parametrize(
    ("fault", "kind", "code"),
    [
        (Fault.TRANSIENT, ErrorKind.TRANSIENT, "TOOL_TIMEOUT"),
        (Fault.PERMANENT, ErrorKind.PERMANENT, "TOOL_REJECTED"),
        (Fault.PERMISSION_DENIED, ErrorKind.PERMISSION, "PERMISSION_DENIED"),
    ],
)
def test_injected_faults_happen_before_the_effect(fault, kind, code):
    registry = MockToolRegistry(faults=FailureInjector({1: [fault]}))
    with pytest.raises(ToolError) as info:
        registry.execute(page_call())
    assert (info.value.kind, info.value.code) == (kind, code)
    assert registry.store.side_effects == 0
    assert registry.execute(page_call()).replayed is False  # fault consumed; next call works


def test_transient_after_effect_leaves_effect_and_find_result_sees_it():
    registry = MockToolRegistry(faults=FailureInjector({1: [Fault.TRANSIENT_AFTER_EFFECT]}))
    with pytest.raises(ToolError):
        registry.execute(page_call())
    assert registry.store.side_effects == 1
    assert registry.find_result(page_call()).resource_id is not None
    assert registry.execute(page_call()).replayed is True


def test_failure_injection_refused_in_production():
    with pytest.raises(RuntimeError):
        create_tool_registry(Settings(app_env="production"), faults=FailureInjector())
    assert create_tool_registry(Settings(app_env="production")).faults is None


# ---------- retry policy ----------


def test_backoff_is_exponential_and_capped_without_jitter():
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.5, max_delay_seconds=2.0, jitter=False)
    assert [policy.delay_for(a) for a in range(1, 6)] == [0.5, 1.0, 2.0, 2.0, 2.0]


def test_jitter_stays_within_half_and_full_delay():
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.4, max_delay_seconds=1.0, jitter=True)
    rng = random.Random(1)
    for attempt in range(1, 6):
        full = min(1.0, 0.4 * 2 ** (attempt - 1))
        for _ in range(50):
            assert full / 2 <= policy.delay_for(attempt, rng) <= full


def test_only_transient_errors_are_retried_and_attempts_are_bounded():
    policy = RetryPolicy(max_attempts=3)
    transient = ToolError(ErrorKind.TRANSIENT, "T", "t")
    assert policy.should_retry(transient, 1) and policy.should_retry(transient, 2)
    assert not policy.should_retry(transient, 3)
    assert not policy.should_retry(ToolError(ErrorKind.PERMANENT, "P", "p"), 1)
    assert not policy.should_retry(ToolError(ErrorKind.PERMISSION, "D", "d"), 1)


def test_policy_from_settings():
    settings = Settings(execution_max_attempts=4, execution_backoff_base_seconds=0.1,
                        execution_backoff_max_seconds=1.5, execution_backoff_jitter=False)
    assert RetryPolicy.from_settings(settings) == RetryPolicy(4, 0.1, 1.5, False)
