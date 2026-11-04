"""Helpers for SSE tests. Mock tools only; no external network (the live-server tests bind
127.0.0.1 on a free port)."""

import json
import socket
import threading
import time

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools import FailureInjector, MockToolRegistry
from app.streaming.broker import EventBroker
from app.streaming.runner import ExecutionRunner


class GateSleeper:
    """Retry 'sleep' that blocks the worker thread until released - lets a test observe the
    stream while the execution is provably still running (parked in a retry backoff)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.entered.set()
        assert self.release.wait(timeout=20), "test never released the gate"


def make_runner(execute_faults=None, compensation_faults=None, *, sleeper=None, broker=None):
    service = ExecutionService(
        MockToolRegistry(faults=FailureInjector(execute_faults or {}, compensation_faults or {})),
        RetryPolicy(max_attempts=3, base_delay_seconds=0.2, max_delay_seconds=2.0, jitter=False),
        max_steps=10,
        sleeper=sleeper or (lambda _: None),
    )
    return ExecutionRunner(service, broker or EventBroker(), max_workers=2)


def parse_sse(text: str) -> list[dict]:
    """Parse an SSE body into [{id, event, data}] (comments and retry lines skipped)."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            fields[key] = value[1:] if value.startswith(" ") else value
        if "data" in fields:
            events.append({"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])})
    return events


def types(events) -> list[str]:
    return [e["event"] if isinstance(e, dict) else e.event_type.value for e in events]


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
