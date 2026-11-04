"""Phase 4 Stage 2: SSE live execution progress."""

import asyncio
import json
import logging
import threading
import time
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.api import execution as execution_api
from app.api import streaming as streaming_api
from app.agents.execution.tools import Fault
from app.core.config import Settings
from app.main import app
from app.schemas.events import ExecutionEvent, ExecutionEventType as Ev
from app.streaming.broker import BrokerFull, ChannelBusy, EventBroker, EventChannel
from app.streaming.emitter import emit, reset_publisher, set_publisher
from tests.execution_helpers import APPROVAL, FOUR_STEPS, make_request
from tests.sse_helpers import GateSleeper, free_port, make_runner, parse_sse, types, wait_until
from tests.telemetry_helpers import SENSITIVE_MARKERS

START_URL = "/api/v1/workflows/executions"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def body(steps=None, workflow_id=None, **approval):
    return {
        "workflow_id": str(workflow_id or uuid4()),
        "plan": {"summary": "Synthetic plan", "steps": FOUR_STEPS if steps is None else steps},
        "approval": {**APPROVAL, **approval},
    }


def run_to_end(runner, request):
    channel, future = runner.start(request)
    future.result(timeout=10)
    return channel.events()


# ======================= broker =======================


def test_publish_assigns_sequential_ids_and_sanitises_metadata():
    channel = EventChannel(str(uuid4()))
    a = channel.publish(Ev.STEP_STARTED, step_number=1, metadata={"attempt": 1, "content": "private", "api_key": "k"})
    b = channel.publish(Ev.STEP_SUCCEEDED, step_number=1)
    assert (a.event_id, b.event_id) == (1, 2)
    assert a.metadata == {"attempt": 1, "content": {"redacted": True, "length": 7}, "api_key": "[REDACTED]"}
    assert a.timestamp.tzinfo is not None


def test_nothing_is_published_after_terminal_event():
    channel = EventChannel(str(uuid4()))
    channel.publish(Ev.WORKFLOW_COMPLETED, metadata={"status": "COMPLETED"})
    assert channel.publish(Ev.STEP_STARTED, step_number=1) is None
    assert types(channel.events()) == ["workflow_completed"] and channel.terminal


def test_event_cap_keeps_room_for_terminal_event(monkeypatch):
    import app.streaming.broker as broker_module

    monkeypatch.setattr(broker_module, "MAX_EVENTS_PER_WORKFLOW", 3)
    channel = EventChannel(str(uuid4()))
    for _ in range(5):
        channel.publish(Ev.STEP_STARTED, step_number=1)
    assert channel.publish(Ev.WORKFLOW_FAILED, metadata={"status": "FAILED"}) is not None
    assert types(channel.events()) == ["step_started", "step_started", "workflow_failed"]
    assert channel.dropped == 3


def test_broker_tokens_are_per_workflow_and_unguessable():
    broker = EventBroker()
    a, b = broker.create(str(uuid4())), broker.create(str(uuid4()))
    assert a.token != b.token and len(a.token) >= 40
    assert broker.get(a.workflow_id, a.token) is a
    assert broker.get(a.workflow_id, b.token) is None  # another workflow's token
    assert broker.get(a.workflow_id, None) is None
    assert broker.get(a.workflow_id, a.token[:-1] + "x") is None
    assert broker.get(str(uuid4()), a.token) is None


def test_active_channel_cannot_be_replaced_but_finished_one_can():
    broker = EventBroker()
    wf = str(uuid4())
    first = broker.create(wf)
    with pytest.raises(ChannelBusy):
        broker.create(wf)
    first.publish(Ev.WORKFLOW_COMPLETED)
    second = broker.create(wf)
    assert second is not first and broker.get(wf, first.token) is None


def test_retention_evicts_finished_channels_and_capacity_is_bounded():
    now = [0.0]
    broker = EventBroker(retention_seconds=60, max_channels=2, clock=lambda: now[0])
    wa, wb, wc = (str(uuid4()) for _ in range(3))
    a = broker.create(wa)
    broker.create(wb)
    with pytest.raises(BrokerFull):
        broker.create(wc)
    a.publish(Ev.WORKFLOW_COMPLETED)
    now[0] = 59
    assert broker.get(wa, a.token) is a
    now[0] = 61
    assert broker.get(wa, a.token) is None
    broker.create(wc)
    assert len(broker) == 2


@pytest.mark.anyio
async def test_backlog_then_live_events_without_gaps_or_duplicates():
    channel = EventChannel(str(uuid4()))
    channel.publish(Ev.WORKFLOW_STARTED)
    channel.publish(Ev.STEP_STARTED, step_number=1)
    stream = streaming_api.event_stream(channel, 0, heartbeat_seconds=5)
    assert (await stream.__anext__()).startswith("retry:")
    received = [await stream.__anext__(), await stream.__anext__()]

    def produce():
        for i in range(2, 50):
            channel.publish(Ev.STEP_STARTED, step_number=i)
        channel.publish(Ev.WORKFLOW_COMPLETED)

    threading.Thread(target=produce).start()  # producer in another thread, as in production
    received += [chunk async for chunk in stream]
    ids = [int(c.split("\n")[0][4:]) for c in received]
    assert ids == list(range(1, 52))
    assert channel.subscriber_count == 0


@pytest.mark.anyio
async def test_resume_after_last_event_id():
    channel = EventChannel(str(uuid4()))
    for n in (1, 2, 3):
        channel.publish(Ev.STEP_STARTED, step_number=n)
    channel.publish(Ev.WORKFLOW_COMPLETED)
    chunks = [c async for c in streaming_api.event_stream(channel, 2, heartbeat_seconds=5)]
    assert types(parse_sse("".join(chunks))) == ["step_started", "workflow_completed"]
    assert [e["id"] for e in parse_sse("".join(chunks))] == [3, 4]


@pytest.mark.anyio
async def test_client_disconnect_unsubscribes():
    channel = EventChannel(str(uuid4()))
    channel.publish(Ev.WORKFLOW_STARTED)
    stream = streaming_api.event_stream(channel, 0, heartbeat_seconds=5)
    await stream.__anext__()
    await stream.__anext__()
    assert channel.subscriber_count == 1
    await stream.aclose()  # what Starlette does when the client goes away
    assert channel.subscriber_count == 0
    assert channel.publish(Ev.STEP_STARTED, step_number=1) is not None  # producer unaffected


@pytest.mark.anyio
async def test_heartbeat_comments_while_idle():
    channel = EventChannel(str(uuid4()))
    stream = streaming_api.event_stream(channel, 0, heartbeat_seconds=0.05)
    await stream.__anext__()
    assert await stream.__anext__() == ": keep-alive\n\n"
    await stream.aclose()


def test_publish_to_subscriber_with_closed_loop_drops_it():
    channel = EventChannel(str(uuid4()))
    loop = asyncio.new_event_loop()

    async def sub():
        return channel.subscribe(0)

    loop.run_until_complete(sub())
    loop.close()
    assert channel.subscriber_count == 1
    channel.publish(Ev.STEP_STARTED, step_number=1)
    assert channel.subscriber_count == 0


def test_emit_without_publisher_is_noop_and_emit_never_raises():
    emit(Ev.STEP_STARTED, step_number=1)  # no publisher in context

    class Broken:
        def publish(self, *a, **k):
            raise RuntimeError("boom")

    token = set_publisher(Broken())
    try:
        emit(Ev.STEP_STARTED, step_number=1)
    finally:
        reset_publisher(token)


# ======================= events from real graph runs =======================


def test_success_event_sequence():
    runner = make_runner()
    request = make_request()
    events = run_to_end(runner, request)

    assert types(events) == ["workflow_started"] + ["step_started", "step_succeeded"] * 4 + ["workflow_completed"]
    assert [e.event_id for e in events] == list(range(1, 11))
    assert all(str(e.workflow_id) == str(request.workflow_id) for e in events)
    assert [e.step_number for e in events if e.event_type is Ev.STEP_STARTED] == [1, 2, 3, 4]
    started = events[0].metadata
    assert started["step_count"] == 4 and started["steps"][3]["depends_on"] == [2, 3]
    assert events[2].metadata["resource_id"].startswith("mock-gdrive-fixture-")
    final = events[-1].metadata
    assert final["status"] == "COMPLETED" and final["step_statuses"] == {str(n): "SUCCEEDED" for n in (1, 2, 3, 4)}
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)


def test_transient_failure_retry_events():
    runner = make_runner({2: [Fault.TRANSIENT, Fault.TRANSIENT]})
    events = run_to_end(runner, make_request())
    step2 = [e for e in events if e.step_number == 2]
    assert types(step2) == [
        "step_started", "step_failed", "retry_scheduled",
        "step_started", "step_failed", "retry_scheduled",
        "step_started", "step_succeeded",
    ]
    failed = [e.metadata for e in step2 if e.event_type is Ev.STEP_FAILED]
    assert [(f["attempt"], f["error_code"], f["will_retry"]) for f in failed] == [(1, "TOOL_TIMEOUT", True), (2, "TOOL_TIMEOUT", True)]
    retries = [e.metadata for e in step2 if e.event_type is Ev.RETRY_SCHEDULED]
    assert [(r["phase"], r["next_attempt"], r["delay_seconds"]) for r in retries] == [("execution", 2, 0.2), ("execution", 3, 0.4)]
    assert events[-1].event_type is Ev.WORKFLOW_COMPLETED


def test_retry_exhaustion_rollback_events():
    runner = make_runner({3: [Fault.TRANSIENT] * 3})
    events = run_to_end(runner, make_request())
    last_fail = [e for e in events if e.event_type is Ev.STEP_FAILED][-1]
    assert last_fail.step_number == 3
    assert last_fail.metadata["will_retry"] is False and last_fail.metadata["retries_exhausted"] is True
    tail = [(e.event_type.value, e.step_number) for e in events[events.index(last_fail) + 1:]]
    assert tail == [
        ("compensation_started", 2), ("compensation_completed", 2), ("workflow_failed", None),
    ]
    assert events[-1].metadata["status"] == "ROLLED_BACK"
    assert "RETRIES_EXHAUSTED" in events[-1].metadata["error_codes"]


def test_partial_rollback_and_compensation_retry_events():
    runner = make_runner({4: [Fault.PERMANENT]}, {3: [Fault.TRANSIENT]})
    events = run_to_end(runner, make_request())
    tail = [(e.event_type.value, e.step_number, e.metadata.get("phase") or e.metadata.get("status"))
            for e in events if e.event_type.value.startswith(("compensation", "retry"))]
    assert tail == [
        ("compensation_started", 3, None), ("retry_scheduled", 3, "compensation"),
        ("compensation_completed", 3, "COMPENSATED"),
        ("compensation_started", 2, None), ("compensation_completed", 2, "COMPENSATED"),
    ]
    assert events[-1].metadata["status"] == "ROLLED_BACK"


def test_compensation_failure_events():
    runner = make_runner({4: [Fault.PERMANENT]}, {2: [Fault.PERMANENT]})
    events = run_to_end(runner, make_request())
    failed = [e for e in events if e.event_type is Ev.COMPENSATION_FAILED]
    assert [(e.step_number, e.metadata["error_codes"]) for e in failed] == [(2, ["COMPENSATION_TOOL_REJECTED"])]
    assert events[-1].event_type is Ev.WORKFLOW_FAILED and events[-1].metadata["status"] == "COMPENSATION_FAILED"


def test_irreversible_step_reports_not_compensatable():
    steps = [
        {**FOUR_STEPS[3], "step_number": 1, "depends_on": []},
        {**FOUR_STEPS[1], "step_number": 2, "depends_on": []},
    ]
    runner = make_runner({2: [Fault.PERMANENT]})
    events = run_to_end(runner, make_request(steps))
    completed = [e for e in events if e.event_type is Ev.COMPENSATION_COMPLETED]
    assert [(e.step_number, e.metadata["status"]) for e in completed] == [(1, "NOT_COMPENSATABLE")]
    assert events[-1].metadata["status"] == "PARTIALLY_ROLLED_BACK"


def test_rejected_run_emits_only_terminal_event():
    runner = make_runner()
    events = run_to_end(runner, make_request(approved=False))
    assert types(events) == ["workflow_failed"]
    assert events[0].metadata["status"] == "REJECTED" and events[0].metadata["error_codes"] == ["APPROVAL_REQUIRED"]


def test_duplicate_run_replays_terminal_outcome_without_rerunning():
    runner = make_runner()
    request = make_request()
    run_to_end(runner, request)
    calls = len(runner.service.registry.store.calls)
    events = run_to_end(runner, request)
    assert types(events) == ["workflow_completed"]
    assert events[0].metadata["replayed"] is True
    assert len(runner.service.registry.store.calls) == calls


def test_unexpected_execution_error_emits_sanitised_terminal_event():
    runner = make_runner()

    def boom(_request):
        raise RuntimeError("internal detail with SYNTHETIC-SENSITIVE-CONTENT")

    runner.service.execute = boom
    events = run_to_end(runner, make_request())
    assert types(events) == ["workflow_failed"]
    assert events[0].metadata == {"status": "ERROR", "error_code": "INTERNAL_ERROR"}


def test_events_contain_no_arguments_or_sensitive_values():
    secret = " ".join(SENSITIVE_MARKERS)
    steps = json.loads(json.dumps(FOUR_STEPS))
    steps[1]["arguments"]["content"] = secret
    steps[3]["arguments"]["message"] = secret
    runner = make_runner({4: [Fault.PERMANENT]})
    events = run_to_end(runner, make_request(steps))
    dumped = json.dumps([e.model_dump(mode="json") for e in events])
    for marker in SENSITIVE_MARKERS:
        assert marker not in dumped
    assert "arguments" not in dumped and "Injected" not in dumped  # no args, no raw error messages


def test_concurrent_workflows_never_mix_events():
    runner = make_runner({2: [Fault.TRANSIENT]})
    requests = [make_request() for _ in range(6)]
    started = [runner.start(r) for r in requests]
    for _, future in started:
        future.result(timeout=10)
    for request, (channel, _) in zip(requests, started):
        events = channel.events()
        assert {str(e.workflow_id) for e in events} == {str(request.workflow_id)}
        assert [e.event_id for e in events] == list(range(1, len(events) + 1))
        assert events[-1].event_type is Ev.WORKFLOW_COMPLETED


def test_synchronous_execution_publishes_nothing():
    runner = make_runner()
    request = make_request()
    channel = runner.broker.create(str(request.workflow_id))  # a stream exists for this id...
    runner.service.execute(request)  # ...but a sync run is not bound to it
    assert channel.events() == []


# ======================= HTTP =======================


@pytest.fixture
def api():
    runners = []

    def factory(*faults, sleeper=None):
        runner = make_runner(*faults, sleeper=sleeper)
        runners.append(runner)
        app.dependency_overrides[streaming_api.get_execution_runner] = lambda: runner
        app.dependency_overrides[streaming_api.get_heartbeat_seconds] = lambda: 0.2
        app.dependency_overrides[execution_api.get_execution_service] = lambda: runner.service
        return TestClient(app), runner

    yield factory
    app.dependency_overrides.clear()
    for runner in runners:
        runner.shutdown()


def test_start_returns_202_with_stream_and_result_urls(api):
    client, runner = api()
    payload = body()
    response = client.post(START_URL, json=payload)
    assert response.status_code == 202
    data = response.json()
    wf = payload["workflow_id"]
    assert data["status"] == "ACCEPTED" and data["access_model"] == "mock-capability-token"
    assert data["stream_url"] == f"/api/v1/workflows/executions/{wf}/events?access_token={data['stream_token']}"
    assert data["result_url"] == f"/api/v1/workflows/executions/{wf}"


def test_stream_over_http_success(api):
    client, runner = api()
    start = client.post(START_URL, json=body()).json()
    response = client.get(start["stream_url"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = parse_sse(response.text)
    assert types(events)[0] == "workflow_started" and types(events)[-1] == "workflow_completed"
    assert [e["id"] for e in events] == list(range(1, len(events) + 1))
    for e in events:
        ExecutionEvent.model_validate(e["data"])
        assert e["data"]["event_type"] == e["event"] and e["data"]["event_id"] == e["id"]
    result = client.get(start["result_url"]).json()
    assert result["status"] == "COMPLETED"


def test_stream_over_http_rollback(api):
    client, _ = api({3: [Fault.PERMANENT]})
    start = client.post(START_URL, json=body()).json()
    events = parse_sse(client.get(start["stream_url"]).text)
    assert "compensation_started" in types(events)
    assert events[-1]["event"] == "workflow_failed" and events[-1]["data"]["metadata"]["status"] == "ROLLED_BACK"


def test_token_in_header_and_resume_with_last_event_id(api):
    client, _ = api()
    start = client.post(START_URL, json=body()).json()
    url = start["stream_url"].split("?")[0]
    full = parse_sse(client.get(url, headers={"X-Stream-Token": start["stream_token"]}).text)
    resumed = parse_sse(client.get(url, headers={"X-Stream-Token": start["stream_token"], "Last-Event-ID": "5"}).text)
    assert [e["id"] for e in resumed] == [e["id"] for e in full if e["id"] > 5]


def test_finished_stream_returns_204_so_eventsource_stops(api):
    client, _ = api()
    start = client.post(START_URL, json=body()).json()
    last = parse_sse(client.get(start["stream_url"]).text)[-1]["id"]
    response = client.get(start["stream_url"], headers={"Last-Event-ID": str(last)})
    assert response.status_code == 204


def test_cross_workflow_access_is_denied(api):
    client, _ = api()
    a = client.post(START_URL, json=body()).json()
    b = client.post(START_URL, json=body()).json()
    a_url = a["stream_url"].split("?")[0]
    assert client.get(f"{a_url}?access_token={b['stream_token']}").status_code == 404
    assert client.get(a_url).status_code == 404  # no token
    assert client.get(f"/api/v1/workflows/executions/{uuid4()}/events?access_token={a['stream_token']}").status_code == 404


def test_conflicting_plan_is_409(api):
    client, runner = api()
    payload = body()
    client.post("/api/v1/workflows/execute", json=payload)  # finished synchronously
    payload["plan"]["summary"] = "different"
    assert client.post(START_URL, json=payload).status_code == 409


def test_second_start_while_running_is_409(api):
    gate = GateSleeper()
    client, _ = api({2: [Fault.TRANSIENT]}, sleeper=gate)
    payload = body()
    assert client.post(START_URL, json=payload).status_code == 202
    assert gate.entered.wait(5)
    try:
        assert client.post(START_URL, json=payload).status_code == 409
    finally:
        gate.release.set()


def test_invalid_body_is_422_and_nothing_starts(api):
    client, runner = api()
    payload = body()
    payload["approval"]["mode"] = "production"  # not a mode at all
    assert client.post(START_URL, json=payload).status_code == 422
    assert len(runner.broker) == 0


@pytest.mark.parametrize("mode", ["real", "dry_run"])
def test_mode_mismatch_is_409_and_nothing_starts(api, mode):
    client, runner = api()
    payload = body()
    payload["approval"]["mode"] = mode  # the server runs in mock mode
    assert client.post(START_URL, json=payload).status_code == 409
    assert len(runner.broker) == 0
    assert runner.service.registry.store.calls == []


@pytest.mark.parametrize("settings", [Settings(_env_file=None, mock_execution_enabled=False),
                                      Settings(_env_file=None, app_env="production")])
def test_streaming_disabled_is_503(monkeypatch, settings):
    monkeypatch.setattr(streaming_api, "get_settings", lambda: settings)
    client = TestClient(app)
    assert client.post(START_URL, json=body()).status_code == 503
    assert client.get(f"/api/v1/workflows/executions/{uuid4()}/events?access_token=x").status_code == 503


def test_sync_execute_endpoint_unchanged(api):
    client, _ = api()
    response = client.post("/api/v1/workflows/execute", json=body())
    assert response.status_code == 200 and response.json()["status"] == "COMPLETED"


def test_default_runner_shares_sync_service(monkeypatch):
    monkeypatch.setattr(streaming_api, "get_settings", lambda: Settings(_env_file=None))
    streaming_api.shutdown_runner()
    try:
        assert streaming_api.get_execution_runner().service is execution_api._default_service()
    finally:
        streaming_api.shutdown_runner()


# ======================= real server (true streaming over a socket) =======================


@pytest.fixture
def live_server():
    gate = GateSleeper()
    runner = make_runner({2: [Fault.TRANSIENT]}, sleeper=gate)
    app.dependency_overrides[streaming_api.get_execution_runner] = lambda: runner
    app.dependency_overrides[streaming_api.get_heartbeat_seconds] = lambda: 0.2
    app.dependency_overrides[execution_api.get_execution_service] = lambda: runner.service
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ws="none"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_until(lambda: server.started, timeout=10)
    try:
        yield f"http://127.0.0.1:{port}", runner, gate
    finally:
        gate.release.set()
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()
        runner.shutdown()


def _read_events(lines, until):
    """Read SSE lines until an event of type ``until`` has been parsed."""
    buffer, events = [], []
    for line in lines:
        if line == "":
            parsed = parse_sse("\n".join(buffer) + "\n\n")
            buffer = []
            events += parsed
            if parsed and parsed[-1]["event"] == until:
                return events
        else:
            buffer.append(line)
    return events


def test_events_arrive_while_execution_is_running_and_loop_stays_responsive(live_server):
    base, runner, gate = live_server
    with httpx.Client(base_url=base, timeout=10) as client:
        start = client.post(START_URL, json=body()).json()
        with client.stream("GET", start["stream_url"]) as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            early = _read_events(lines, until="retry_scheduled")
            # The worker thread is parked in the retry backoff: the run is NOT finished,
            # yet these events were already delivered over the socket.
            assert gate.entered.is_set() and not gate.release.is_set()
            assert types(early)[-3:] == ["step_started", "step_failed", "retry_scheduled"]
            assert not runner.broker.get(start["workflow_id"], start["stream_token"]).terminal

            # The event loop is not blocked by the sleeping execution.
            t0 = time.monotonic()
            assert client.get("/health").status_code == 200
            assert time.monotonic() - t0 < 2

            gate.release.set()
            rest = _read_events(lines, until="workflow_completed")
    events = early + rest
    assert [e["id"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["event"] == "workflow_completed"


def test_client_disconnect_mid_run_is_safe(live_server, caplog):
    base, runner, gate = live_server
    with httpx.Client(base_url=base, timeout=10) as client, caplog.at_level(logging.INFO, logger="app.api.streaming"):
        start = client.post(START_URL, json=body()).json()
        channel = runner.broker.get(start["workflow_id"], start["stream_token"])
        with client.stream("GET", start["stream_url"]) as response:
            assert response.status_code == 200
            # Keep a reference: a discarded iter_lines() generator can be garbage-collected
            # at any time, and closing it closes the connection (a real client disconnect).
            lines = response.iter_lines()
            received = _read_events(lines, until="retry_scheduled")
            assert types(received)[-1] == "retry_scheduled", f"stream ended early: {types(received)}"
            assert gate.entered.is_set() and not channel.terminal
            # The server may still be finishing the yield of this event; wait briefly
            # for it to be parked on the queue again rather than racing it.
            assert wait_until(lambda: channel.subscriber_count == 1, timeout=2), (
                f"subscriber gone while connected; server log: {caplog.text[-500:]}"
            )
        # connection closed by the client while the run is parked in a retry
        assert wait_until(lambda: channel.subscriber_count == 0, timeout=5)
        gate.release.set()
        assert wait_until(lambda: channel.terminal, timeout=5)  # execution unaffected
        assert client.get(start["result_url"]).json()["status"] == "COMPLETED"
        # reconnect after completion: the remaining events are replayed
        replay = parse_sse(client.get(start["stream_url"], headers={"Last-Event-ID": "1"}).text)
        assert replay[0]["id"] == 2 and replay[-1]["event"] == "workflow_completed"
