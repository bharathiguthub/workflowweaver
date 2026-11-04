"""Test doubles for telemetry. Nothing here touches the network."""

import contextvars
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.observability.telemetry import error_code_of

# Synthetic "sensitive" markers placed in instructions and tool arguments. Tests assert they
# never reach any telemetry payload.
SENSITIVE_MARKERS = ("SYNTHETIC-SENSITIVE-CONTENT", "sk-ant-api03-SYNTHETICKEY123456", "alice@example.invalid")


@dataclass
class RecordedObservation:
    name: str
    as_type: str
    workflow_id: str | None
    step_number: int | None
    input: Any
    metadata: dict[str, Any]
    model: str | None
    model_parameters: Any
    root: bool
    parent: "RecordedObservation | None"
    updates: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def output(self) -> Any:
        outputs = [u["output"] for u in self.updates if u.get("output") is not None]
        return outputs[-1] if outputs else None

    @property
    def level(self) -> str | None:
        levels = [u["level"] for u in self.updates if u.get("level")]
        return levels[-1] if levels else None

    def merged_metadata(self) -> dict[str, Any]:
        merged = dict(self.metadata)
        for u in self.updates:
            merged.update(u.get("metadata") or {})
        return merged


_current: contextvars.ContextVar[RecordedObservation | None] = contextvars.ContextVar("rec_obs", default=None)


class RecordingTelemetry:
    """Records exactly what instrumented code passes to the telemetry facade (pre-sanitising),
    plus parent/child structure via contextvars - the same mechanism OpenTelemetry uses."""

    enabled = True

    def __init__(self) -> None:
        self.observations: list[RecordedObservation] = []
        self.events: list[dict[str, Any]] = []

    @contextmanager
    def observe(self, name, *, as_type="span", workflow_id=None, step_number=None, input=None,
                metadata=None, model=None, model_parameters=None, root=False):
        obs = RecordedObservation(name, as_type, workflow_id, step_number, input, dict(metadata or {}),
                                  model, model_parameters, root, _current.get())
        self.observations.append(obs)
        token = _current.set(obs)

        class _Handle:
            def update(self_inner, **kwargs):
                obs.updates.append(kwargs)

        try:
            yield _Handle()
        except BaseException as exc:
            obs.error = error_code_of(exc)
            raise
        finally:
            _current.reset(token)

    def event(self, name, *, workflow_id=None, step_number=None, metadata=None, level=None):
        parent = _current.get()
        self.events.append({"name": name, "workflow_id": workflow_id, "step_number": step_number,
                            "metadata": dict(metadata or {}), "level": level,
                            "parent": parent.name if parent else None})

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...

    # helpers
    def named(self, prefix: str) -> list[RecordedObservation]:
        return [o for o in self.observations if o.name.startswith(prefix)]

    def one(self, name: str) -> RecordedObservation:
        found = [o for o in self.observations if o.name == name]
        assert len(found) == 1, (name, [o.name for o in self.observations])
        return found[0]

    def events_named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["name"] == name]

    def dump(self) -> str:
        payload = [
            {"name": o.name, "input": o.input, "metadata": o.metadata, "updates": o.updates,
             "model_parameters": o.model_parameters}
            for o in self.observations
        ]
        return json.dumps({"observations": payload, "events": self.events}, default=str)


# ---------- fake Langfuse clients (to test LangfuseTelemetry's failure isolation) ----------


class FakeSpan:
    def __init__(self, client, kwargs) -> None:
        self.client = client
        self.kwargs = kwargs
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs):
        if self.client.fail_on == "update":
            raise RuntimeError("synthetic update failure")
        self.updates.append(kwargs)
        return self


class _FakeCM:
    def __init__(self, client, span) -> None:
        self.client, self.span = client, span

    def __enter__(self):
        return self.span

    def __exit__(self, *exc):
        if self.client.fail_on == "end":
            raise RuntimeError("synthetic end failure")
        return False


class FakeLangfuseClient:
    """Mimics the subset of langfuse.Langfuse that LangfuseTelemetry uses.

    fail_on: None | "start" | "update" | "end" | "event" | "flush" | "shutdown" | "all"
    """

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.spans: list[FakeSpan] = []
        self.events: list[dict[str, Any]] = []

    def _fails(self, op: str) -> bool:
        return self.fail_on in (op, "all")

    def start_as_current_observation(self, **kwargs):
        if self._fails("start"):
            raise RuntimeError("synthetic start failure")
        span = FakeSpan(self, kwargs)
        self.spans.append(span)
        return _FakeCM(self, span)

    def create_event(self, **kwargs):
        if self._fails("event"):
            raise RuntimeError("synthetic event failure")
        self.events.append(kwargs)

    def flush(self):
        if self._fails("flush"):
            raise RuntimeError("synthetic flush failure")

    def shutdown(self):
        if self._fails("shutdown"):
            raise RuntimeError("synthetic shutdown failure")


@contextmanager
def fake_propagate(**kwargs):
    yield


@contextmanager
def failing_propagate(**kwargs):
    raise RuntimeError("synthetic propagate failure")
    yield  # pragma: no cover
