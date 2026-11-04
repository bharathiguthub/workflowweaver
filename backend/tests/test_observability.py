"""Phase 4 Stage 1: Langfuse observability.

* Behaviour: a RecordingTelemetry captures what the instrumented code emits.
* Failure isolation: LangfuseTelemetry wraps fake clients that fail at every SDK call.
* Real SDK: langfuse.Langfuse with an in-memory OpenTelemetry exporter (no network).
"""

import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.agents.execution.tools import Fault
from app.agents.planners.claude import ClaudePlanner, usage_details
from app.agents.planners.mock import MockPlanner
from app.agents.service import PlanningService
from app.api import execution as execution_api
from app.core.config import Settings
from app.main import app
from app.observability import (
    LangfuseTelemetry,
    NoopTelemetry,
    create_telemetry,
    get_telemetry,
    sanitize,
    set_telemetry,
    shutdown_telemetry,
)
from app.observability.sanitize import REDACTED, langfuse_mask
from app.observability.telemetry import langfuse_environment
from app.schemas.execution import ExecutionStatus
from tests.conftest import EXAMPLE_INSTRUCTION, FakeAnthropicClient, valid_draft
from tests.execution_helpers import APPROVAL, FOUR_STEPS, make_request, make_service
from tests.telemetry_helpers import (
    SENSITIVE_MARKERS,
    FakeLangfuseClient,
    RecordingTelemetry,
    failing_propagate,
    fake_propagate,
)

SENSITIVE_INSTRUCTION = (
    f"{EXAMPLE_INSTRUCTION} Contact {SENSITIVE_MARKERS[2]} about {SENSITIVE_MARKERS[0]} key {SENSITIVE_MARKERS[1]}"
)


def sensitive_steps():
    """FOUR_STEPS with synthetic sensitive values in every free-text argument."""
    secret = " ".join(SENSITIVE_MARKERS)
    steps = json.loads(json.dumps(FOUR_STEPS))
    for step in steps:
        for key in step["arguments"]:
            if key != "document_name":
                step["arguments"][key] = f"{step['arguments'][key]} {secret}"
    return steps


@pytest.fixture
def recorder():
    telemetry = RecordingTelemetry()
    set_telemetry(telemetry)
    return telemetry


def assert_no_sensitive_data(text: str) -> None:
    for marker in SENSITIVE_MARKERS:
        assert marker not in text, marker


# ======================= sanitisation =======================


def test_sanitize_redacts_secret_keys_and_content_keys():
    data = {
        "api_key": "abc", "Authorization": "Bearer xyz", "LANGFUSE_SECRET_KEY": "s",
        "content": "private notes", "arguments": {"title": "t"}, "instruction": "do things",
        "workflow_id": "wf-1", "step_number": 2, "error_code": "TOOL_TIMEOUT",
    }
    out = sanitize(data)
    assert out["api_key"] == out["Authorization"] == out["LANGFUSE_SECRET_KEY"] == REDACTED
    assert out["content"] == {"redacted": True, "length": 13}
    assert out["arguments"] == {"redacted": True, "length": 1}
    assert out["instruction"]["redacted"] is True
    assert (out["workflow_id"], out["step_number"], out["error_code"]) == ("wf-1", 2, "TOOL_TIMEOUT")


@pytest.mark.parametrize(
    "text",
    [
        "key sk-ant-api03-abcdefghijklmnop",
        "pk-lf-1234567890abcdef and sk-lf-1234567890abcdef",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpM",
        "post to https://example.invalid/webhookb2/abc/IncomingWebhook/xyz",
    ],
)
def test_sanitize_scrubs_secret_values_in_free_text(text):
    out = sanitize({"note": text})["note"]
    assert REDACTED in out
    for fragment in ("abcdefghijklmnop", "1234567890abcdef", "SflKxwRJSMeKKF2QT4fwpM", "IncomingWebhook"):
        assert fragment not in out


def test_sanitize_truncates_and_bounds_size():
    out = sanitize({"note": "x" * 1000, "items": list(range(500)), "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}})
    assert len(out["note"]) < 260 and "truncated" in out["note"]
    assert len(out["items"]) == 51
    assert "[MAX_DEPTH]" in json.dumps(out)
    json.dumps(sanitize({"obj": object()}))  # always JSON-serialisable


def test_langfuse_mask_hook_never_raises():
    assert langfuse_mask(data={"secret": "x"}) == {"secret": REDACTED}

    class Boom:
        def items(self):
            raise RuntimeError

    assert langfuse_mask(data={"x": Boom()}) is not None


# ======================= planning instrumentation =======================


def test_planning_request_and_nodes_are_traced(recorder):
    service = PlanningService(MockPlanner(), max_steps=10, max_planning_attempts=2)
    response = service.plan(SENSITIVE_INSTRUCTION)
    wf = str(response.workflow_id)

    root = recorder.one("planning.request")
    assert root.root is True and root.workflow_id == wf and root.parent is None
    assert root.input["instruction_chars"] == len(SENSITIVE_INSTRUCTION)
    assert root.output["status"] == "planned" and root.output["step_count"] == 3

    nodes = [o for o in recorder.observations if o.name.startswith("planning.") and o is not root]
    assert [o.name for o in nodes] == ["planning.planner", "planning.validator", "planning.reporter"]
    assert all(o.workflow_id == wf and o.parent is root for o in nodes)
    validator = recorder.one("planning.validator")
    assert validator.output["status"] == "valid"
    assert [s["integration"] for s in validator.output["planned_steps"]] == ["notion", "google_drive", "teams"]
    assert "latency_ms" not in validator.metadata  # added by LangfuseTelemetry, not the recorder
    assert_no_sensitive_data(recorder.dump())


def test_invalid_plan_validation_is_traced_with_issue_codes(recorder):
    service = PlanningService(MockPlanner(), max_steps=10, max_planning_attempts=2)
    service.plan("Create a Jira issue and notify the team in Teams")
    validator = recorder.one("planning.validator")
    assert validator.output["status"] == "invalid"
    assert "UNSUPPORTED_INTEGRATION" in validator.output["validation_issue_codes"]
    assert validator.level == "WARNING"
    assert recorder.one("planning.request").output["status"] == "invalid"


def _claude_response(input_tokens=1200, output_tokens=345, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=valid_draft(),
        stop_reason=stop_reason,
        _request_id="req_synthetic",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=None, cache_creation_input_tokens=0),
    )


def test_claude_generation_records_model_usage_and_no_prompt(recorder):
    planner = ClaudePlanner(client=FakeAnthropicClient(_claude_response()), model="claude-synthetic", max_tokens=8000)
    service = PlanningService(planner, max_steps=10, max_planning_attempts=2)
    service.plan(SENSITIVE_INSTRUCTION)

    generation = recorder.one("claude.messages.parse")
    assert generation.as_type == "generation" and generation.model == "claude-synthetic"
    assert generation.model_parameters == {"max_tokens": 8000}
    assert generation.parent.name == "planning.planner"
    assert generation.parent.parent.name == "planning.request"
    usage = next(u["usage_details"] for u in generation.updates if u.get("usage_details"))
    assert usage == {"input": 1200, "output": 345, "cache_creation_input_tokens": 0, "total": 1545}
    assert generation.merged_metadata()["stop_reason"] == "end_turn"
    assert set(generation.input) == {"instruction_chars", "system_prompt_chars"}
    assert_no_sensitive_data(recorder.dump())


def test_claude_generation_failure_records_error_code(recorder):
    planner = ClaudePlanner(client=FakeAnthropicClient(_claude_response(stop_reason="refusal")),
                            model="claude-synthetic", max_tokens=8000)
    PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION)
    generation = recorder.one("claude.messages.parse")
    assert generation.error == "PLANNER_REFUSED"


def test_usage_details_only_reports_supplied_fields():
    assert usage_details(SimpleNamespace(usage=SimpleNamespace(output_tokens=42))) == {"output": 42}
    assert usage_details(SimpleNamespace()) == {}


# ======================= execution instrumentation =======================


def test_successful_execution_trace_structure(recorder):
    service, _ = make_service()
    request = make_request(sensitive_steps())
    wf = str(request.workflow_id)
    service.execute(request)

    root = recorder.one("execution.request")
    assert root.root and root.workflow_id == wf and root.parent is None
    assert root.output["status"] == "COMPLETED" and root.level == "DEFAULT"
    assert root.input["steps"][1] == {"step_number": 2, "integration": "notion", "action": "create_page", "depends_on": [1]}

    nodes = [o for o in recorder.observations if o.name.startswith("execution.") and o is not root]
    assert [o.name for o in nodes] == ["execution.prepare"] + ["execution.execute_step"] * 4 + ["execution.finalize"]
    assert all(o.parent is root and o.workflow_id == wf for o in nodes)
    assert [o.step_number for o in recorder.named("execution.execute_step")] == [1, 2, 3, 4]

    tools = recorder.named("tool.")
    assert [t.name for t in tools] == [
        "tool.google_drive.read_document", "tool.notion.create_page",
        "tool.google_drive.create_document", "tool.teams.send_message",
    ]
    for tool, step in zip(tools, (1, 2, 3, 4)):
        assert tool.as_type == "tool" and tool.step_number == step and tool.workflow_id == wf
        assert tool.parent.name == "execution.execute_step" and tool.parent.step_number == step
        assert tool.metadata["attempt"] == 1 and tool.metadata["mode"] == "mock"
        assert tool.output["resource_id"]
    assert tools[1].input == {"argument_names": ["content", "title"]}

    finalize = recorder.one("execution.finalize")
    assert finalize.output["final_status"] == "COMPLETED"
    outcome = recorder.events_named("execution.outcome")
    assert len(outcome) == 1 and outcome[0]["metadata"]["status"] == "COMPLETED"
    assert outcome[0]["parent"] == "execution.request"
    assert_no_sensitive_data(recorder.dump())


def test_retry_attempts_and_delays_are_traced(recorder):
    service, _ = make_service({2: [Fault.TRANSIENT, Fault.TRANSIENT]})
    service.execute(make_request())

    attempts = [t for t in recorder.named("tool.notion.create_page")]
    assert [t.metadata["attempt"] for t in attempts] == [1, 2, 3]
    assert [t.level for t in attempts] == ["WARNING", "WARNING", None]
    assert attempts[0].output == {"error_code": "TOOL_TIMEOUT", "error_kind": "transient", "retryable": True}

    retries = recorder.events_named("execution.retry_scheduled")
    assert [(e["step_number"], e["metadata"]["failed_attempt"], e["metadata"]["delay_seconds"]) for e in retries] == [
        (2, 1, 0.2), (2, 2, 0.4)
    ]
    assert all(e["metadata"]["error_code"] == "TOOL_TIMEOUT" and e["parent"] == "execution.retry_wait" for e in retries)
    waits = recorder.named("execution.retry_wait")
    assert [w.step_number for w in waits] == [2, 2]
    assert [w.output["retry_delays_seconds"] for w in waits] == [[0.2], [0.4]]


def test_permanent_failure_and_compensation_are_traced(recorder):
    service, _ = make_service({4: [Fault.PERMANENT]}, {3: [Fault.TRANSIENT]})
    service.execute(make_request())

    failed = recorder.one("tool.teams.send_message")
    assert failed.level == "ERROR" and failed.output["error_code"] == "TOOL_REJECTED"

    comps = recorder.named("compensation.")
    assert [(c.name, c.step_number, c.metadata["attempt"]) for c in comps] == [
        ("compensation.google_drive.create_document", 3, 1),
        ("compensation.google_drive.create_document", 3, 2),
        ("compensation.notion.create_page", 2, 1),
    ]
    assert all(c.parent.name == "execution.compensate" for c in comps)
    assert comps[0].level == "WARNING" and comps[1].output == {"undone": True}
    assert len(recorder.events_named("execution.compensation_retry_scheduled")) == 1

    compensate = recorder.one("execution.compensate")
    assert [c["status"] for c in compensate.output["compensation_results"]] == ["COMPENSATED", "COMPENSATED"]
    root = recorder.one("execution.request")
    assert root.output["status"] == "ROLLED_BACK" and root.level == "WARNING"
    assert root.output["compensation_statuses"] == {"3": "COMPENSATED", "2": "COMPENSATED"}


def test_compensation_failure_outcome_is_error_level(recorder):
    service, _ = make_service({4: [Fault.PERMANENT]}, {2: [Fault.PERMANENT]})
    service.execute(make_request())
    assert recorder.one("compensation.notion.create_page").level == "ERROR"
    assert recorder.one("execution.request").level == "ERROR"
    assert recorder.one("execution.finalize").level == "ERROR"
    assert recorder.events_named("execution.outcome")[0]["metadata"]["status"] == "COMPENSATION_FAILED"


def test_reconciled_orphan_effect_emits_event(recorder):
    service, _ = make_service({3: [Fault.TRANSIENT_AFTER_EFFECT] * 3})
    service.execute(make_request())
    events = recorder.events_named("execution.effect_reconciled")
    assert [e["step_number"] for e in events] == [3]


def test_duplicate_request_is_traced_as_replay(recorder):
    service, _ = make_service()
    request = make_request()
    service.execute(request)
    service.execute(request)
    roots = recorder.named("execution.request")
    assert [r.output["replayed"] for r in roots] == [False, True]
    assert len(recorder.named("execution.prepare")) == 1  # graph not re-run


def test_rejected_execution_is_traced(recorder):
    service, _ = make_service()
    service.execute(make_request(approved=False))
    assert recorder.one("execution.prepare").output["error_codes"] == ["APPROVAL_REQUIRED"]
    assert recorder.one("execution.request").output["status"] == "REJECTED"
    assert recorder.named("tool.") == []


# ======================= LangfuseTelemetry adapter =======================


def test_adapter_sanitises_before_sending_and_adds_latency():
    client = FakeLangfuseClient()
    telemetry = LangfuseTelemetry(client, propagate=fake_propagate)
    with telemetry.observe("x", workflow_id="wf", step_number=3, input={"content": "secret notes", "api_key": "k"},
                           metadata={"note": "sk-ant-api03-abcdefghijk"}) as obs:
        obs.update(output={"title": "private"}, metadata={"attempt": 2})
    telemetry.event("e", workflow_id="wf", metadata={"token": "t"})

    span = client.spans[0]
    assert span.kwargs["input"] == {"content": {"redacted": True, "length": 12}, "api_key": REDACTED}
    assert span.kwargs["metadata"]["workflow_id"] == "wf" and span.kwargs["metadata"]["step_number"] == 3
    assert REDACTED in span.kwargs["metadata"]["note"]
    assert span.updates[0]["output"] == {"title": {"redacted": True, "length": 7}}
    final_meta = span.updates[-1]["metadata"]
    assert final_meta["attempt"] == 2 and final_meta["latency_ms"] >= 0
    assert client.events[0]["metadata"]["token"] == REDACTED


def test_adapter_records_workflow_exception_and_reraises_it():
    client = FakeLangfuseClient()
    telemetry = LangfuseTelemetry(client, propagate=fake_propagate)

    class CodedError(Exception):
        code = "TOOL_REJECTED"

    with pytest.raises(CodedError):
        with telemetry.observe("x"):
            raise CodedError("message with user data")
    errors = [u for u in client.spans[0].updates if u.get("level") == "ERROR"]
    assert errors[0]["status_message"] == "TOOL_REJECTED"
    assert "user data" not in json.dumps(client.spans[0].updates, default=str)
    assert telemetry.failures == 0


FAILURE_MODES = ["start", "update", "end", "event", "flush", "shutdown", "all", "propagate"]


def _broken_telemetry(mode: str) -> LangfuseTelemetry:
    if mode == "propagate":
        return LangfuseTelemetry(FakeLangfuseClient(), propagate=failing_propagate)
    return LangfuseTelemetry(FakeLangfuseClient(fail_on=mode), propagate=fake_propagate)


def _execution_fingerprint(response):
    return response.model_dump(mode="json")


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_telemetry_failures_never_break_execution(mode, caplog):
    wf = uuid4()  # same id -> same deterministic mock resource ids in both (separate) stores
    baseline_service, _ = make_service({2: [Fault.TRANSIENT]})
    baseline = baseline_service.execute(make_request(workflow_id=wf))  # NoopTelemetry (conftest)

    telemetry = _broken_telemetry(mode)
    set_telemetry(telemetry)
    service, _ = make_service({2: [Fault.TRANSIENT]})
    with caplog.at_level(logging.WARNING, logger="app.observability.telemetry"):
        response = service.execute(make_request(workflow_id=wf))
        telemetry.flush()
        telemetry.shutdown()

    assert response.status is ExecutionStatus.COMPLETED
    assert _execution_fingerprint(response) == _execution_fingerprint(baseline)
    assert telemetry.failures > 0
    # failures are logged by exception type only, never by message
    assert "synthetic" not in caplog.text


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_telemetry_failures_never_break_rollback(mode):
    set_telemetry(_broken_telemetry(mode))
    service, _ = make_service({4: [Fault.PERMANENT]}, {3: [Fault.TRANSIENT]})
    response = service.execute(make_request())
    assert response.status is ExecutionStatus.ROLLED_BACK
    assert service.registry.store.live_resources() == {}


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_telemetry_failures_never_break_planning(mode):
    set_telemetry(_broken_telemetry(mode))
    planner = ClaudePlanner(client=FakeAnthropicClient(_claude_response()), model="claude-synthetic", max_tokens=8000)
    response = PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION)
    assert response.status.value == "PLANNED" and len(response.planned_steps) == 3


def test_telemetry_failure_does_not_change_api_responses():
    set_telemetry(_broken_telemetry("all"))
    service, _ = make_service()
    app.dependency_overrides[execution_api.get_execution_service] = lambda: service
    try:
        client = TestClient(app)
        body = {"workflow_id": str(uuid4()), "plan": {"summary": "s", "steps": FOUR_STEPS}, "approval": APPROVAL}
        assert client.post("/api/v1/workflows/execute", json=body).status_code == 200
        plan = client.post("/api/v1/workflows/plan", json={"instruction": EXAMPLE_INSTRUCTION})
        assert plan.status_code == 200
    finally:
        app.dependency_overrides.clear()


# ======================= configuration / factory =======================


def test_disabled_by_default():
    assert isinstance(create_telemetry(Settings(_env_file=None)), NoopTelemetry)


def test_enabled_without_keys_falls_back_to_noop(caplog):
    with caplog.at_level(logging.WARNING):
        telemetry = create_telemetry(Settings(_env_file=None, langfuse_enabled=True, langfuse_public_key="pk-lf-x"))
    assert isinstance(telemetry, NoopTelemetry)
    assert "telemetry disabled" in caplog.text


def test_client_construction_failure_falls_back_to_noop():
    def boom(**kwargs):
        raise ValueError("bad config")

    settings = Settings(_env_file=None, langfuse_enabled=True, langfuse_public_key="pk-lf-x",
                        langfuse_secret_key=SecretStr("sk-lf-x"))
    assert isinstance(create_telemetry(settings, client_factory=boom), NoopTelemetry)


def test_factory_passes_settings_to_client_and_never_logs_secret(caplog):
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeLangfuseClient()

    settings = Settings(_env_file=None, app_env="Staging EU", langfuse_enabled=True,
                        langfuse_public_key="pk-lf-public", langfuse_secret_key=SecretStr("sk-lf-very-secret-value"),
                        langfuse_base_url="https://cloud.langfuse.com")
    with caplog.at_level(logging.DEBUG):
        telemetry = create_telemetry(settings, client_factory=factory)
    assert isinstance(telemetry, LangfuseTelemetry)
    assert captured["secret_key"] == "sk-lf-very-secret-value"
    assert captured["base_url"] == "https://cloud.langfuse.com"
    assert captured["environment"] == "staging-eu"
    assert captured["mask"] is langfuse_mask
    assert "sk-lf-very-secret-value" not in caplog.text
    assert "sk-lf-very-secret-value" not in repr(settings)


def test_langfuse_environment_rules():
    assert langfuse_environment("Production") == "production"
    assert langfuse_environment("langfuse-x") == "development"
    assert langfuse_environment("") == "development"


def test_get_telemetry_is_lazy_and_resettable(monkeypatch):
    import app.core.config as config

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None))
    set_telemetry(None)
    first = get_telemetry()
    assert isinstance(first, NoopTelemetry) and get_telemetry() is first
    shutdown_telemetry()
    shutdown_telemetry()  # idempotent


# ======================= real Langfuse SDK (offline) =======================


@pytest.fixture
def real_sdk():
    """A real langfuse.Langfuse client exporting to an in-memory OTel exporter. The base URL
    points at a closed local port, so even an accidental network export goes nowhere."""
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-test-{uuid4().hex}",
        secret_key="sk-lf-test-not-a-real-key",
        base_url="http://127.0.0.1:9",
        tracing_enabled=True,
        environment="test",
        mask=langfuse_mask,
        span_exporter=exporter,
    )
    telemetry = LangfuseTelemetry(client)
    set_telemetry(telemetry)
    yield telemetry, exporter
    client.shutdown()


def _attrs(span) -> dict:
    return dict(span.attributes or {})


def test_real_sdk_execution_trace(real_sdk):
    telemetry, exporter = real_sdk
    service, _ = make_service({3: [Fault.TRANSIENT]}, {})
    request = make_request(sensitive_steps())
    wf = str(request.workflow_id)
    response = service.execute(request)
    telemetry.flush()

    assert response.status is ExecutionStatus.COMPLETED
    assert telemetry.failures == 0
    spans = exporter.get_finished_spans()
    by_name: dict[str, list] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)

    root = by_name["execution.request"][0]
    assert root.parent is None
    assert _attrs(root)["session.id"] == wf
    assert _attrs(root)["langfuse.observation.type"] == "agent"
    trace_id = root.context.trace_id
    assert all(s.context.trace_id == trace_id for s in spans)  # one trace

    ids = {s.context.span_id: s for s in spans}
    for node in by_name["execution.execute_step"]:
        assert node.parent.span_id == root.context.span_id
    tool_spans = by_name["tool.google_drive.create_document"]
    assert len(tool_spans) == 2  # transient failure + successful retry
    for tool in tool_spans:
        assert ids[tool.parent.span_id].name == "execution.execute_step"
        assert _attrs(tool)["langfuse.observation.type"] == "tool"
        assert _attrs(tool)["session.id"] == wf  # propagated from the root
        meta = {k: v for k, v in _attrs(tool).items() if k.startswith("langfuse.observation.metadata.")}
        assert meta["langfuse.observation.metadata.step_number"] == "3"
    assert _attrs(tool_spans[0])["langfuse.observation.level"] == "WARNING"
    assert "execution.retry_scheduled" in by_name  # event observation

    everything = json.dumps([_attrs(s) for s in spans], default=str)
    assert_no_sensitive_data(everything)


def test_real_sdk_claude_generation_usage(real_sdk):
    telemetry, exporter = real_sdk
    planner = ClaudePlanner(client=FakeAnthropicClient(_claude_response()), model="claude-synthetic", max_tokens=8000)
    PlanningService(planner, max_steps=10, max_planning_attempts=2).plan(SENSITIVE_INSTRUCTION)
    telemetry.flush()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    generation = _attrs(spans["claude.messages.parse"])
    assert generation["langfuse.observation.type"] == "generation"
    assert generation["langfuse.observation.model.name"] == "claude-synthetic"
    assert json.loads(generation["langfuse.observation.usage_details"])["input"] == 1200
    assert_no_sensitive_data(json.dumps([_attrs(s) for s in spans.values()], default=str))


def test_real_sdk_unreachable_server_does_not_fail_workflow():
    """A real client with the default HTTP exporter, pointed at a closed port."""
    from langfuse import Langfuse

    client = Langfuse(public_key=f"pk-lf-test-{uuid4().hex}", secret_key="sk-lf-test", base_url="http://127.0.0.1:9",
                      tracing_enabled=True, timeout=1)
    telemetry = LangfuseTelemetry(client)
    set_telemetry(telemetry)
    service, _ = make_service()
    try:
        assert service.execute(make_request()).status is ExecutionStatus.COMPLETED
        telemetry.flush()
    finally:
        telemetry.shutdown()
