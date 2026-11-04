"""Phase 4 Stage 4: end-to-end check against a REAL uvicorn server (mock tools only).

Starts the FastAPI app on 127.0.0.1 (free port) with the mock planner, mock tools, a real
0.25 s retry backoff, and synthetic fault injection configured in code (never via the API).
Every scenario talks to the server over HTTP + SSE only.

    cd C:\\Projects\\workflowweaver\\backend
    .\\.venv\\Scripts\\python.exe -m scripts.e2e_check

Exit code 0 if every scenario passes. No real Notion, Google Drive or Teams service is used.
"""

import json
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

import httpx
import uvicorn

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools import FailureInjector, Fault, MockToolRegistry
from app.agents.planners.mock import MockPlanner
from app.agents.service import PlanningService
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.api import execution as execution_api
from app.api import planning as planning_api
from app.api import streaming as streaming_api
from app.main import app
from app.streaming.broker import EventBroker
from app.streaming.runner import ExecutionRunner

RETRY_DELAY = 0.25
APPROVAL = {"approved": True, "mode": "mock", "approved_by": "synthetic.reviewer"}
ORIGINAL_FAILING_INSTRUCTION = (
    "Read the synthetic Sprint 42 meeting notes from Google Drive, create Notion tasks for each "
    "action item, and notify attendees in Microsoft Teams."
)


class CheckFailed(AssertionError):
    pass


class GateableSleeper:
    """Real retry backoff (time.sleep), except when a scenario arms the gate: then the next
    backoff blocks until the scenario releases it. This makes "event delivered while the run
    is still in progress" deterministic instead of a race against the backoff delay."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def arm(self) -> None:
        with self._lock:
            self._armed = True
            self.entered.clear()
            self.release.clear()

    def __call__(self, seconds: float) -> None:
        with self._lock:
            gated, self._armed = self._armed, False
        if gated:
            self.entered.set()
            self.release.wait(timeout=30)  # safety net: never hang a run forever
        else:
            time.sleep(seconds)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


@dataclass
class Live:
    base: str
    runner: ExecutionRunner
    http: httpx.Client
    sleeper: GateableSleeper

    @property
    def store(self):
        return self.runner.service.registry.store

    def inject(self, execute: dict | None = None, compensate: dict | None = None) -> None:
        self.runner.service.registry.faults = FailureInjector(execute or {}, compensate or {})

    def templates(self) -> dict[str, dict]:
        return {t["id"]: t for t in self.http.get("/api/v1/workflows/templates").json()}

    def plan(self, instruction: str) -> httpx.Response:
        return self.http.post("/api/v1/workflows/plan", json={"instruction": instruction})

    def start(self, plan: dict, steps: list | None = None) -> dict:
        body = {"workflow_id": plan["workflow_id"],
                "plan": {"summary": plan.get("summary") or "", "steps": plan["planned_steps"] if steps is None else steps},
                "approval": APPROVAL}
        response = self.http.post("/api/v1/workflows/executions", json=body)
        check(response.status_code == 202, f"start returned {response.status_code}: {response.text[:200]}")
        return response.json()

    def stream(self, start: dict, on_event: Callable[[dict], None] | None = None) -> list[tuple[float, dict]]:
        """Read the SSE stream; returns (arrival time, event) pairs."""
        events: list[tuple[float, dict]] = []
        buffer: list[str] = []
        with self.http.stream("GET", start["stream_url"]) as response:
            check(response.status_code == 200, f"stream returned {response.status_code}")
            check(response.headers["content-type"].startswith("text/event-stream"), "wrong content type")
            for line in response.iter_lines():
                if line:
                    buffer.append(line)
                    continue
                data = next((b[6:] for b in buffer if b.startswith("data: ")), None)
                buffer = []
                if data is None:
                    continue
                event = json.loads(data)
                events.append((time.monotonic(), event))
                if on_event:
                    on_event(event)
                if event["event_type"] in ("workflow_completed", "workflow_failed"):
                    break
        return events

    def result(self, start: dict) -> httpx.Response:
        return self.http.get(start["result_url"])


def _types(events) -> list[str]:
    return [e["event_type"] for _, e in events]


def _ordered(events) -> None:
    ids = [e["event_id"] for _, e in events]
    check(ids == list(range(1, len(ids) + 1)), f"event ids not contiguous/in order: {ids}")
    check(len({e["workflow_id"] for _, e in events}) == 1, "events from more than one workflow")


def _fresh_plan(live: Live, template_id: str) -> dict:
    response = live.plan(live.templates()[template_id]["example_instruction"])
    check(response.status_code == 200, f"plan {template_id}: HTTP {response.status_code}")
    return response.json()


def _live_resources_for(live: Live, workflow_id: str) -> list:
    return [r for r in live.store.live_resources().values() if r["idempotency_key"].startswith(workflow_id)]


# ---------------------------------------------------------------- scenarios


def template_run(template_id: str) -> Callable[[Live], str]:
    def scenario(live: Live) -> str:
        live.inject()
        plan = _fresh_plan(live, template_id)
        check(plan["status"] == "PLANNED", f"plan status {plan['status']}")
        for step in plan["planned_steps"]:
            if step["action"] == "read_document":
                check(step["arguments"]["document_name"] in SYNTHETIC_DRIVE_DOCUMENTS, "read of a non-fixture document")
        start = live.start(plan)
        events = live.stream(start)
        _ordered(events)
        n = len(plan["planned_steps"])
        check(_types(events) == ["workflow_started"] + ["step_started", "step_succeeded"] * n + ["workflow_completed"],
              f"unexpected sequence {_types(events)}")
        report = live.result(start).json()
        check(report["status"] == "COMPLETED", f"report {report['status']}")
        titles = [s["arguments"].get("title") for s in plan["planned_steps"] if "title" in s["arguments"]]
        return f"{n} steps, {len(events)} events, COMPLETED; titles={titles}"

    return scenario


def transient_retry_streams_live(live: Live) -> str:
    plan = _fresh_plan(live, "meeting-to-tasks")
    live.inject({2: [Fault.TRANSIENT]})
    live.sleeper.arm()  # the run will park in its retry backoff until we release it
    start = live.start(plan)
    observed: dict = {}

    def on_event(event: dict) -> None:
        if event["event_type"] == "retry_scheduled":
            # The worker is provably still inside the backoff: the run cannot have finished.
            observed["worker_parked"] = live.sleeper.entered.wait(timeout=10)
            with httpx.Client(base_url=live.base, timeout=5) as other:
                observed["result_status"] = other.get(start["result_url"]).status_code
                t0 = time.monotonic()
                observed["health_status"] = other.get("/health").status_code
                observed["health_seconds"] = time.monotonic() - t0
            live.sleeper.release.set()

    try:
        events = live.stream(start, on_event)
    finally:
        live.sleeper.release.set()  # never leave a worker parked if a check fails
    _ordered(events)
    types = _types(events)
    check(types[3:7] == ["step_started", "step_failed", "retry_scheduled", "step_started"], f"sequence {types}")
    failed = events[4][1]["metadata"]
    check(failed["will_retry"] and failed["error_code"] == "TOOL_TIMEOUT", f"step_failed metadata {failed}")
    check(events[6][1]["metadata"]["attempt"] == 2, "second attempt not reported")
    check(observed.get("worker_parked") is True, "worker was not parked in the backoff when the event arrived")
    check(observed.get("result_status") == 404, f"result already available during retry: {observed}")
    check(observed.get("health_status") == 200 and observed["health_seconds"] < 2,
          f"event loop blocked during backoff: {observed}")
    check(types[-1] == "workflow_completed", f"terminal {types[-1]}")
    report = live.result(start).json()
    check(report["status"] == "COMPLETED" and report["attempts"]["2"] == 2, f"report {report['status']} {report['attempts']}")
    return (f"retry_scheduled delivered while worker parked in backoff (result 404, /health "
            f"{observed['health_seconds'] * 1000:.0f} ms); COMPLETED attempts={report['attempts']}")


def timeout_after_effect_is_idempotent(live: Live) -> str:
    plan = _fresh_plan(live, "meeting-to-tasks")
    live.inject({3: [Fault.TRANSIENT_AFTER_EFFECT]})
    start = live.start(plan)
    events = live.stream(start)
    _ordered(events)
    report = live.result(start).json()
    step3 = report["step_results"][2]
    docs = [r for r in _live_resources_for(live, plan["workflow_id"]) if r["type"] == "drive_document"]
    check(report["status"] == "COMPLETED", f"report {report['status']}")
    check(step3["attempts"] == 2 and step3["replayed"], f"step 3 {step3['attempts']} replayed={step3['replayed']}")
    check(len(docs) == 1, f"{len(docs)} Drive documents created (expected exactly 1)")
    return "write succeeded then timed out; retry replayed the stored result; exactly 1 document"


def retry_exhaustion_rolls_back(live: Live) -> str:
    plan = _fresh_plan(live, "meeting-to-tasks")
    live.inject({3: [Fault.TRANSIENT] * 3})
    start = live.start(plan)
    events = live.stream(start)
    _ordered(events)
    fails = [e["metadata"] for _, e in events if e["event_type"] == "step_failed"]
    check([f["attempt"] for f in fails] == [1, 2, 3], f"attempts {fails}")
    check(fails[-1]["retries_exhausted"] and not fails[-1]["will_retry"], "exhaustion not reported")
    comp = [(e["event_type"], e["step_number"]) for _, e in events if e["event_type"].startswith("compensation")]
    check(comp == [("compensation_started", 2), ("compensation_completed", 2)], f"compensation {comp}")
    report = live.result(start).json()
    check(report["status"] == "ROLLED_BACK" and "RETRIES_EXHAUSTED" in [e["code"] for e in report["errors"]], report["status"])
    check(_live_resources_for(live, plan["workflow_id"]) == [], "resources left behind")
    return "3 attempts, RETRIES_EXHAUSTED, Notion task compensated, ROLLED_BACK, nothing left behind"


def permanent_failure_rolls_back_in_reverse(live: Live) -> str:
    plan = _fresh_plan(live, "meeting-to-tasks")
    live.inject({4: [Fault.PERMANENT]})
    start = live.start(plan)
    events = live.stream(start)
    _ordered(events)
    started = [e["step_number"] for _, e in events if e["event_type"] == "compensation_started"]
    check(started == [3, 2], f"compensation order {started}")
    report = live.result(start).json()
    check(report["status"] == "ROLLED_BACK" and report["attempts"]["4"] == 1, f"{report['status']} {report['attempts']}")
    check(_live_resources_for(live, plan["workflow_id"]) == [], "resources left behind")
    return "permanent error not retried; steps 3 then 2 compensated; ROLLED_BACK"


def irreversible_step_partial_rollback(live: Live) -> str:
    plan = _fresh_plan(live, "incident-response")  # read -> Teams -> create document
    live.inject({3: [Fault.PERMANENT]})
    start = live.start(plan)
    events = live.stream(start)
    _ordered(events)
    done = [(e["step_number"], e["metadata"]["status"]) for _, e in events if e["event_type"] == "compensation_completed"]
    check(done == [(2, "NOT_COMPENSATABLE")], f"compensation {done}")
    report = live.result(start).json()
    check(report["status"] == "PARTIALLY_ROLLED_BACK", report["status"])
    return "Teams message reported NOT_COMPENSATABLE; PARTIALLY_ROLLED_BACK"


def compensation_failure_is_reported(live: Live) -> str:
    plan = _fresh_plan(live, "meeting-to-tasks")
    live.inject({4: [Fault.PERMANENT]}, {2: [Fault.PERMANENT]})
    start = live.start(plan)
    events = live.stream(start)
    _ordered(events)
    check(any(e["event_type"] == "compensation_failed" and e["step_number"] == 2 for _, e in events), "no compensation_failed")
    report = live.result(start).json()
    check(report["status"] == "COMPENSATION_FAILED", report["status"])
    check(len(_live_resources_for(live, plan["workflow_id"])) == 1, "expected the un-compensated task to remain")
    return "step 3 compensated, step 2 compensation failed; COMPENSATION_FAILED (1 resource left for manual cleanup)"


def duplicate_request_is_replayed(live: Live) -> str:
    live.inject()
    plan = _fresh_plan(live, "sprint-planning")
    body = {"workflow_id": plan["workflow_id"], "plan": {"summary": plan["summary"], "steps": plan["planned_steps"]},
            "approval": APPROVAL}
    first = live.http.post("/api/v1/workflows/execute", json=body).json()
    calls = len(live.store.calls)
    second = live.http.post("/api/v1/workflows/execute", json=body).json()
    check(second["replayed"] and not first["replayed"], "duplicate not replayed")
    check(len(live.store.calls) == calls, "duplicate called tools again")
    body["plan"]["summary"] = "different plan"
    check(live.http.post("/api/v1/workflows/execute", json=body).status_code == 409, "conflict not detected")
    replay_start = live.http.post("/api/v1/workflows/executions", json={**body, "plan": {"summary": plan["summary"], "steps": plan["planned_steps"]}})
    events = live.stream(replay_start.json())
    check(_types(events) == ["workflow_completed"] and events[0][1]["metadata"]["replayed"], f"stream replay {_types(events)}")
    return "sync duplicate replayed without tool calls; different plan -> 409; streamed duplicate -> replayed terminal event"


def missing_document_rejected_before_execution(live: Live) -> str:
    live.inject()
    response = live.plan(ORIGINAL_FAILING_INSTRUCTION)
    body = response.json()
    check(response.status_code == 422 and body["status"] == "INVALID", f"plan {response.status_code} {body.get('status')}")
    check([i["code"] for i in body["validation"]["issues"]] == ["DOCUMENT_NOT_FOUND"], str(body["validation"]))
    draft = MockPlanner().plan(ORIGINAL_FAILING_INSTRUCTION, max_steps=10)
    steps = [{"step_number": s.step_number, "integration": s.integration, "action": s.action,
              "description": s.description, "arguments": {a.name: a.value for a in s.arguments}, "depends_on": s.depends_on}
             for s in draft.steps]
    calls = len(live.store.calls)
    start = live.start({"workflow_id": str(uuid4()), "summary": "forced"}, steps=steps)
    events = live.stream(start)
    meta = events[-1][1]["metadata"]
    check(_types(events) == ["workflow_failed"] and meta["status"] == "REJECTED", f"{_types(events)} {meta}")
    check(meta["validation_issue_codes"] == ["DOCUMENT_NOT_FOUND"], str(meta))
    check(len(live.store.calls) == calls, "a tool was called")
    return "plan 422 INVALID DOCUMENT_NOT_FOUND; forced execution REJECTED before any tool call"


SCENARIOS: list[tuple[str, Callable[[Live], str]]] = [
    ("template: Meeting-to-Tasks", template_run("meeting-to-tasks")),
    ("template: Sprint Planning", template_run("sprint-planning")),
    ("template: Incident Response", template_run("incident-response")),
    ("transient failure -> retry (live SSE)", transient_retry_streams_live),
    ("timeout after write -> idempotent retry", timeout_after_effect_is_idempotent),
    ("retry exhaustion -> Saga rollback", retry_exhaustion_rolls_back),
    ("permanent failure -> reverse rollback", permanent_failure_rolls_back_in_reverse),
    ("irreversible step -> partial rollback", irreversible_step_partial_rollback),
    ("compensation failure", compensation_failure_is_reported),
    ("duplicate request -> idempotent replay", duplicate_request_is_replayed),
    ("DOCUMENT_NOT_FOUND negative case", missing_document_rejected_before_execution),
]


# ---------------------------------------------------------------- server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def live_server() -> Iterator[Live]:
    registry = MockToolRegistry(faults=FailureInjector())
    sleeper = GateableSleeper()
    service = ExecutionService(registry, RetryPolicy(max_attempts=3, base_delay_seconds=RETRY_DELAY,
                                                     max_delay_seconds=1.0, jitter=False), max_steps=10,
                               sleeper=sleeper)
    runner = ExecutionRunner(service, EventBroker(), max_workers=2)
    planning = PlanningService(MockPlanner(), max_steps=10, max_planning_attempts=2,
                               available_documents=frozenset(SYNTHETIC_DRIVE_DOCUMENTS))
    app.dependency_overrides[planning_api.get_planning_service] = lambda: planning
    app.dependency_overrides[execution_api.get_execution_service] = lambda: service
    app.dependency_overrides[streaming_api.get_execution_runner] = lambda: runner
    app.dependency_overrides[streaming_api.get_heartbeat_seconds] = lambda: 1.0
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ws="none"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        if not server.started:
            raise RuntimeError("uvicorn did not start")
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=15) as http:
            yield Live(base, runner, http, sleeper)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()
        runner.shutdown()


def run(live: Live, name: str, scenario: Callable[[Live], str]) -> tuple[bool, str]:
    try:
        return True, scenario(live)
    except CheckFailed as exc:
        return False, str(exc)


def main() -> int:
    from app.observability import NoopTelemetry, set_telemetry

    set_telemetry(NoopTelemetry())  # this check never exports telemetry
    import logging

    for name in ("httpx", "app"):  # keep the report readable; failures are reported below
        logging.getLogger(name).setLevel(logging.WARNING)
    failures = 0
    with live_server() as live:
        print(f"Live server: {live.base} (mock tools, retry delay {RETRY_DELAY}s)\n")
        for name, scenario in SCENARIOS:
            started = time.monotonic()
            ok, detail = run(live, name, scenario)
            failures += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] {name:<42} {time.monotonic() - started:5.2f}s  {detail}")
    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
