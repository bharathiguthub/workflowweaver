"""End-to-end scenarios for the LangGraph execution graph (mock tools, injected faults)."""

import threading
from uuid import uuid4

import pytest

from app.agents.execution.graph import build_execution_graph
from app.agents.execution.nodes import route_after_execute, route_after_prepare
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import IdempotencyConflict
from app.agents.execution.tools import Fault, MockToolRegistry
from app.schemas.execution import CompensationStatus, ExecutionStatus, StepStatus
from tests.execution_helpers import FOUR_STEPS, make_request, make_service, tool_calls


def statuses(response):
    return [r.status for r in response.step_results]


def comp(response):
    return {c.step_number: c.status for c in response.compensation_results}


# ---------- graph structure ----------


def test_graph_has_expected_nodes():
    graph = build_execution_graph(MockToolRegistry(), RetryPolicy(), sleeper=lambda _: None)
    assert {"prepare", "execute_step", "retry_wait", "compensate", "finalize"} <= set(graph.get_graph().nodes)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"last_outcome": "retry", "journal": [], "current_index": 0, "steps": [{}]}, "retry_wait"),
        ({"last_outcome": "failed", "journal": [{}], "current_index": 0, "steps": [{}]}, "compensate"),
        ({"last_outcome": "failed", "journal": [], "current_index": 0, "steps": [{}]}, "finalize"),
        ({"last_outcome": "succeeded", "journal": [], "current_index": 1, "steps": [{}, {}]}, "execute_step"),
        ({"last_outcome": "succeeded", "journal": [], "current_index": 2, "steps": [{}, {}]}, "finalize"),
    ],
)
def test_route_after_execute(state, expected):
    assert route_after_execute(state) == expected


def test_route_after_prepare():
    assert route_after_prepare({"status": "running"}) == "execute_step"
    assert route_after_prepare({"status": "rejected"}) == "finalize"


# ---------- success ----------


def test_successful_execution():
    service, sleeper = make_service()
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.COMPLETED
    assert statuses(response) == [StepStatus.SUCCEEDED] * 4
    assert response.attempts == {"1": 1, "2": 1, "3": 1, "4": 1}
    assert response.errors == [] and response.compensation_results == []
    assert response.retry_delays_seconds == [] and sleeper.delays == []
    assert response.step_results[0].output["content"].startswith("Alice")
    assert all(r.resource_id for r in response.step_results)
    store = service.registry.store
    assert store.side_effects == 3  # read is not a side effect
    assert len(store.live_resources()) == 2 and len(store.messages) == 1
    assert tool_calls(service) == [1, 2, 3, 4]  # dependency order


# ---------- transient failures ----------


def test_transient_failure_is_retried_then_succeeds():
    service, sleeper = make_service({2: [Fault.TRANSIENT, Fault.TRANSIENT]})
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.COMPLETED
    assert response.attempts["2"] == 3
    assert response.retry_delays_seconds == [0.2, 0.4] == sleeper.delays
    assert [(e.code, e.attempt, e.retryable) for e in response.errors] == [
        ("TOOL_TIMEOUT", 1, True), ("TOOL_TIMEOUT", 2, True)
    ]
    assert service.registry.store.side_effects == 3


def test_timeout_after_effect_retry_is_idempotent():
    service, _ = make_service({2: [Fault.TRANSIENT_AFTER_EFFECT]})
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.COMPLETED
    step2 = response.step_results[1]
    assert step2.attempts == 2 and step2.replayed is True
    notion_pages = [r for r in service.registry.store.resources.values() if r["type"] == "notion_page"]
    assert len(notion_pages) == 1  # retried, but the page was created only once


def test_jittered_delays_are_bounded():
    service, sleeper = make_service({2: [Fault.TRANSIENT, Fault.TRANSIENT]}, jitter=True)
    service.execute(make_request())
    assert 0.1 <= sleeper.delays[0] <= 0.2 and 0.2 <= sleeper.delays[1] <= 0.4


# ---------- permanent failures ----------


def test_permanent_failure_is_not_retried_and_rolls_back():
    service, sleeper = make_service({3: [Fault.PERMANENT]})
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.ROLLED_BACK
    assert statuses(response) == [StepStatus.SUCCEEDED, StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED]
    assert response.attempts == {"1": 1, "2": 1, "3": 1}
    assert response.step_results[2].error.code == "TOOL_REJECTED"
    assert response.step_results[3].attempts == 0
    assert comp(response) == {2: CompensationStatus.COMPENSATED}  # read-only step 1 not journaled
    assert sleeper.delays == []
    assert service.registry.store.live_resources() == {}
    assert 4 not in tool_calls(service)  # skipped step never called


def test_permission_denied_is_not_retried():
    service, _ = make_service({2: [Fault.PERMISSION_DENIED]})
    response = service.execute(make_request())
    assert response.attempts["2"] == 1
    assert response.errors[0].code == "PERMISSION_DENIED"
    assert response.status is ExecutionStatus.FAILED  # nothing to undo: step 1 was a read


def test_failure_before_any_side_effect_is_failed_without_compensation():
    service, _ = make_service({1: [Fault.PERMANENT]})
    response = service.execute(make_request())
    assert response.status is ExecutionStatus.FAILED
    assert response.compensation_results == []
    assert tool_calls(service, "compensate") == []
    assert statuses(response) == [StepStatus.FAILED] + [StepStatus.SKIPPED] * 3


def test_malformed_tool_result_fails_and_orphan_effect_is_compensated():
    service, _ = make_service({2: [Fault.MALFORMED_RESULT]})
    response = service.execute(make_request())
    assert response.step_results[1].error.code == "MALFORMED_TOOL_RESULT"
    assert response.status is ExecutionStatus.ROLLED_BACK
    assert comp(response) == {2: CompensationStatus.COMPENSATED}
    assert service.registry.store.live_resources() == {}


# ---------- retry exhaustion ----------


def test_retry_exhaustion_fails_step_and_rolls_back():
    service, sleeper = make_service({3: [Fault.TRANSIENT] * 3})
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.ROLLED_BACK
    assert response.attempts["3"] == 3
    assert sleeper.delays == [0.2, 0.4]  # bounded: no wait after the final attempt
    assert [e.code for e in response.errors] == ["TOOL_TIMEOUT"] * 3 + ["RETRIES_EXHAUSTED"]
    assert [e.attempt for e in response.errors if e.code == "TOOL_TIMEOUT"] == [1, 2, 3]
    assert 4 not in tool_calls(service)
    assert service.registry.store.live_resources() == {}


def test_exhaustion_after_effect_reconciles_and_compensates_orphan():
    # Every attempt "times out" after writing: the doc exists but no attempt returned it.
    service, _ = make_service({3: [Fault.TRANSIENT_AFTER_EFFECT] * 3})
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.ROLLED_BACK
    assert comp(response) == {3: CompensationStatus.COMPENSATED, 2: CompensationStatus.COMPENSATED}
    assert service.registry.store.live_resources() == {}


def test_max_attempts_one_disables_retries():
    service, sleeper = make_service({2: [Fault.TRANSIENT]}, max_attempts=1)
    response = service.execute(make_request())
    assert response.attempts["2"] == 1 and sleeper.delays == []
    assert response.errors[-1].code == "RETRIES_EXHAUSTED"


# ---------- rollback / compensation ----------


def test_compensation_runs_in_reverse_order():
    steps = [
        {"step_number": 1, "integration": "notion", "action": "create_task", "arguments": {"title": "A"}},
        {"step_number": 2, "integration": "notion", "action": "create_task", "arguments": {"title": "B"}},
        {"step_number": 3, "integration": "google_drive", "action": "create_document",
         "arguments": {"title": "C", "content": "c"}},
        {"step_number": 4, "integration": "notion", "action": "create_page", "arguments": {"title": "D", "content": "d"}},
    ]
    service, _ = make_service({4: [Fault.PERMANENT]})
    response = service.execute(make_request(steps))
    assert response.status is ExecutionStatus.ROLLED_BACK
    assert tool_calls(service, "compensate") == [3, 2, 1]
    assert [c.step_number for c in response.compensation_results] == [3, 2, 1]


def test_irreversible_effect_gives_partial_rollback():
    steps = [
        {**FOUR_STEPS[1], "step_number": 1, "depends_on": []},
        {**FOUR_STEPS[3], "step_number": 2, "depends_on": [1]},
        {**FOUR_STEPS[2], "step_number": 3, "depends_on": [1]},
    ]
    service, _ = make_service({3: [Fault.PERMANENT]})
    response = service.execute(make_request(steps))

    assert response.status is ExecutionStatus.PARTIALLY_ROLLED_BACK
    assert comp(response) == {2: CompensationStatus.NOT_COMPENSATABLE, 1: CompensationStatus.COMPENSATED}
    assert service.registry.store.live_resources() == {}
    assert len(service.registry.store.messages) == 1  # the Teams message remains


def test_transient_compensation_failure_is_retried():
    service, sleeper = make_service({4: [Fault.PERMANENT]}, {2: [Fault.TRANSIENT]})
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.ROLLED_BACK
    by_step = {c.step_number: c for c in response.compensation_results}
    assert by_step[2].status is CompensationStatus.COMPENSATED and by_step[2].attempts == 2
    assert sleeper.delays == [0.2]
    assert "COMPENSATION_TOOL_TIMEOUT" in [e.code for e in response.errors]


def test_permanent_compensation_failure_continues_and_reports():
    service, _ = make_service({4: [Fault.PERMANENT]}, {3: [Fault.PERMANENT]})
    response = service.execute(make_request())

    assert response.status is ExecutionStatus.COMPENSATION_FAILED
    assert comp(response) == {3: CompensationStatus.FAILED, 2: CompensationStatus.COMPENSATED}
    live = service.registry.store.live_resources()
    assert [r["type"] for r in live.values()] == ["drive_document"]  # left for manual cleanup
    assert "manual" in response.message


def test_compensation_retry_exhaustion_is_failed():
    service, sleeper = make_service({4: [Fault.PERMANENT]}, {2: [Fault.TRANSIENT] * 3})
    response = service.execute(make_request())
    assert response.status is ExecutionStatus.COMPENSATION_FAILED
    assert {c.step_number: c.attempts for c in response.compensation_results}[2] == 3
    assert sleeper.delays == [0.2, 0.4]


# ---------- rejection ----------


def test_unapproved_request_executes_nothing():
    service, _ = make_service()
    response = service.execute(make_request(approved=False))
    assert response.status is ExecutionStatus.REJECTED
    assert response.errors[0].code == "APPROVAL_REQUIRED"
    assert response.validation.is_valid is True
    assert service.registry.store.calls == []


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ({"step_number": 1, "integration": "jira", "action": "create_issue", "arguments": {}}, "UNSUPPORTED_INTEGRATION"),
        ({"step_number": 1, "integration": "notion", "action": "delete_page", "arguments": {}}, "UNSUPPORTED_ACTION"),
        ({"step_number": 1, "integration": "teams", "action": "send_message",
          "arguments": {"message": "run $(rm -rf /)"}}, "UNSAFE_CONTENT"),
        ({"step_number": 1, "integration": "notion", "action": "create_task", "arguments": {}}, "MISSING_ARGUMENT"),
        ({"step_number": 1, "integration": "notion", "action": "create_task",
          "arguments": {"title": "x"}, "depends_on": [1]}, "INVALID_DEPENDENCY"),
    ],
)
def test_invalid_plan_is_rejected_before_execution(step, code):
    service, _ = make_service()
    response = service.execute(make_request([step]))
    assert response.status is ExecutionStatus.REJECTED
    assert code in [i.code for i in response.validation.issues]
    assert service.registry.store.calls == []


def test_too_many_steps_rejected():
    steps = [{"step_number": n, "integration": "notion", "action": "create_task", "arguments": {"title": "t"}}
             for n in range(1, 12)]
    service, _ = make_service()
    response = service.execute(make_request(steps))
    assert "TOO_MANY_STEPS" in [i.code for i in response.validation.issues]


# ---------- duplicate requests / idempotency ----------


def test_duplicate_request_replays_without_new_tool_calls():
    service, _ = make_service()
    request = make_request()
    first = service.execute(request)
    calls = list(service.registry.store.calls)
    second = service.execute(request)

    assert second.replayed is True and first.replayed is False
    assert second.model_dump(exclude={"replayed"}) == first.model_dump(exclude={"replayed"})
    assert service.registry.store.calls == calls
    assert service.registry.store.side_effects == 3


def test_duplicate_of_failed_request_does_not_rerun():
    service, _ = make_service({3: [Fault.PERMANENT]})
    request = make_request()
    first = service.execute(request)
    calls = len(service.registry.store.calls)
    second = service.execute(request)
    assert second.status is first.status is ExecutionStatus.ROLLED_BACK and second.replayed
    assert len(service.registry.store.calls) == calls


def test_same_workflow_id_different_plan_conflicts():
    service, _ = make_service()
    wf = uuid4()
    service.execute(make_request(workflow_id=wf))
    with pytest.raises(IdempotencyConflict):
        service.execute(make_request(workflow_id=wf, summary="A different plan"))


def test_rejected_request_is_not_stored_and_can_be_resubmitted():
    service, _ = make_service()
    wf = uuid4()
    assert service.execute(make_request(workflow_id=wf, approved=False)).status is ExecutionStatus.REJECTED
    assert service.get(str(wf)) is None
    response = service.execute(make_request(workflow_id=wf))
    assert response.status is ExecutionStatus.COMPLETED and response.replayed is False


def test_concurrent_duplicates_execute_once():
    service, _ = make_service()
    request = make_request()
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.execute(request))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(not r.replayed for r in results) == 1
    assert tool_calls(service) == [1, 2, 3, 4]
    assert service.registry.store.side_effects == 3


def test_tool_level_idempotency_across_different_services_sharing_a_store():
    """Same workflow_id + step -> same idempotency key -> no duplicate effect, even if the
    request-level store was lost (e.g. a process restart with a durable tool system)."""
    service, _ = make_service()
    wf = uuid4()
    service.execute(make_request(workflow_id=wf))
    service._results.clear()  # simulate losing the request store
    replay = service.execute(make_request(workflow_id=wf))
    assert replay.status is ExecutionStatus.COMPLETED
    assert all(r.replayed for r in replay.step_results[1:])
    assert service.registry.store.side_effects == 3
