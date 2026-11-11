"""Prometheus metrics (Phase 8.2).

All metrics live in a dedicated registry and are exposed by GET /metrics. They are derived
from the telemetry facade (``MetricsSink``) and the request middleware - workflow code is not
changed.

Cardinality policy: labels are only bounded values - HTTP method, route TEMPLATE (never the raw
URL), status code, integration, action, mode, target, provider, configured model id and
machine error codes (normalised to ``[A-Z0-9_]``, else ``OTHER``). Never workflow ids, user
ids, file ids, URLs or free text.
"""

import re
import threading
from collections import OrderedDict
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)
# Standard runtime metrics (process metrics are only available on Linux; elsewhere a no-op).
GCCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)
ProcessCollector(registry=REGISTRY)

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
LLM_BUCKETS = (0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)
WORKFLOW_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)
STEP_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

# ---------- HTTP ----------
http_requests = Counter("ww_http_requests_total", "HTTP requests handled.", ["method", "route", "status"],
                        registry=REGISTRY)
http_errors = Counter("ww_http_request_errors_total", "HTTP responses with status >= 400.",
                      ["method", "route", "status"], registry=REGISTRY)
http_latency = Histogram("ww_http_request_duration_seconds", "HTTP request latency.", ["method", "route"],
                         buckets=HTTP_BUCKETS, registry=REGISTRY)
http_in_progress = Gauge("ww_http_requests_in_progress", "HTTP requests being handled.", registry=REGISTRY)

# ---------- workflows ----------
plans = Counter("ww_workflow_plans_total", "Planning requests by outcome.", ["target", "status"], registry=REGISTRY)
plan_duration = Histogram("ww_workflow_planning_duration_seconds", "Planning duration.", ["target"],
                          buckets=WORKFLOW_BUCKETS, registry=REGISTRY)
executions_started = Counter("ww_workflow_executions_started_total", "Approved execution requests received.",
                             ["mode", "target"], registry=REGISTRY)
executions_finished = Counter("ww_workflow_executions_finished_total",
                              "Executions finished, by final status (completed, failed, rolled_back, rejected, ...).",
                              ["mode", "target", "status"], registry=REGISTRY)
executions_in_progress = Gauge("ww_workflow_executions_in_progress", "Executions running now.", registry=REGISTRY)
execution_duration = Histogram("ww_workflow_execution_duration_seconds", "Execution duration.", ["mode", "status"],
                               buckets=WORKFLOW_BUCKETS, registry=REGISTRY)
execution_rejections = Counter("ww_workflow_rejections_total",
                               "Executions rejected before any step ran (approval, plan, target, gates).",
                               ["reason"], registry=REGISTRY)

# ---------- steps and integrations ----------
step_duration = Histogram("ww_step_duration_seconds", "Duration of one execution step attempt.",
                          ["integration", "action", "status"], buckets=STEP_BUCKETS, registry=REGISTRY)
step_retries = Counter("ww_step_retries_total", "Step retries scheduled.", ["integration", "action", "error_code"],
                       registry=REGISTRY)
integration_failures = Counter("ww_integration_failures_total", "Failed step attempts by integration.",
                               ["integration", "action", "error_code"], registry=REGISTRY)
integration_requests = Counter("ww_integration_requests_total", "Outbound integration calls (Drive REST, Teams).",
                               ["integration", "operation", "outcome"], registry=REGISTRY)
integration_latency = Histogram("ww_integration_request_duration_seconds", "Outbound integration call latency.",
                                ["integration", "operation"], buckets=STEP_BUCKETS, registry=REGISTRY)
compensations = Counter("ww_compensations_total", "Compensation (rollback) attempts.",
                        ["integration", "action", "outcome"], registry=REGISTRY)

# ---------- LLM ----------
llm_requests = Counter("ww_llm_requests_total", "LLM provider calls.", ["provider", "model", "outcome"],
                       registry=REGISTRY)
llm_errors = Counter("ww_llm_errors_total", "Failed LLM provider calls by error code.",
                     ["provider", "error_code"], registry=REGISTRY)
llm_latency = Histogram("ww_llm_request_duration_seconds", "LLM provider call latency.", ["provider", "model"],
                        buckets=LLM_BUCKETS, registry=REGISTRY)
llm_tokens = Counter("ww_llm_tokens_total", "Tokens reported by the provider.", ["provider", "model", "type"],
                     registry=REGISTRY)
llm_fallbacks = Counter("ww_llm_fallbacks_total", "Planner fallbacks to the next provider.",
                        ["from_provider", "to_provider", "reason"], registry=REGISTRY)

telemetry_events = Counter("ww_telemetry_events_total", "Workflow telemetry events.", ["event", "level"],
                           registry=REGISTRY)

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,47}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_.\-/]{0,63}")
_INTEGRATIONS = frozenset({"notion", "google_drive", "teams"})
_TOKEN_TYPES = frozenset({"input", "output", "total", "reasoning", "cache_read", "cache_creation", "thoughts"})


def code_label(value: Any) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else ("NONE" if value in (None, "") else "OTHER")


def slug_label(value: Any, default: str = "unknown") -> str:
    text = str(value).lower() if value not in (None, "") else ""
    return text if _SLUG.fullmatch(text) else default


def integration_label(value: Any) -> str:
    return value if value in _INTEGRATIONS else "other"


def exposition() -> bytes:
    return generate_latest(REGISTRY)


class _Bounded(OrderedDict):
    def __init__(self, size: int = 2000) -> None:
        super().__init__()
        self._size = size

    def put(self, key: Any, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self._size:
            self.popitem(last=False)


class MetricsSink:
    """Turns facade observations/events into metrics. Never raises (the composite guards it)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exec_labels: _Bounded = _Bounded()  # workflow_id -> (mode, target)
        self._step_labels: _Bounded = _Bounded()  # (workflow_id, step) -> (integration, action)

    # ----- observations -----

    def on_start(self, rec: Any) -> None:
        if rec.name == "execution.request":
            inp = rec.input if isinstance(rec.input, dict) else {}
            mode, target = slug_label(inp.get("mode")), slug_label(inp.get("approved_target") or inp.get("plan_target"),
                                                                   "none")
            with self._lock:
                self._exec_labels.put(rec.workflow_id, (mode, target))
            executions_started.labels(mode, target).inc()
            executions_in_progress.inc()

    def on_end(self, rec: Any) -> None:
        name = rec.name
        failed = rec.error is not None or rec.level == "ERROR"
        if name == "execution.request":
            executions_in_progress.dec()
            mode, _ = self._exec_labels.get(rec.workflow_id, ("unknown", "none"))
            status = slug_label(rec.output.get("status") or rec.status_message or ("error" if failed else "unknown"))
            execution_duration.labels(mode, status).observe(rec.duration)
        elif name == "planning.request":
            target = slug_label(rec.input.get("integration_target") if isinstance(rec.input, dict) else None, "none")
            status = slug_label(rec.output.get("status") or ("error" if failed else "unknown"))
            plans.labels(target, status).inc()
            plan_duration.labels(target).observe(rec.duration)
        elif rec.as_type == "generation":
            provider = slug_label(name.split(".", 1)[0])
            model = slug_label(rec.model, "unknown")
            llm_requests.labels(provider, model, "error" if failed else "success").inc()
            llm_latency.labels(provider, model).observe(rec.duration)
            if failed:
                llm_errors.labels(provider, code_label(rec.error or rec.status_message)).inc()
            for kind, count in (rec.usage or {}).items():
                if kind in _TOKEN_TYPES and isinstance(count, int) and count >= 0:
                    llm_tokens.labels(provider, model, kind).inc(count)
        elif name.startswith("tool."):
            _, integration, action = (name.split(".") + ["", ""])[:3]
            integration, action = integration_label(integration), slug_label(action)
            error_code = rec.output.get("error_code") or rec.error
            with self._lock:
                self._step_labels.put((rec.workflow_id, rec.step_number), (integration, action))
            step_duration.labels(integration, action, "error" if error_code else "success").observe(rec.duration)
            if error_code:
                integration_failures.labels(integration, action, code_label(error_code)).inc()
        elif name.startswith("compensation."):
            _, integration, action = (name.split(".") + ["", ""])[:3]
            outcome = "failed" if (rec.output.get("error_code") or rec.error) else "compensated"
            compensations.labels(integration_label(integration), slug_label(action), outcome).inc()
        elif name.startswith("google_drive.") or name == "teams.webhook.post":
            integration = "google_drive" if name.startswith("google_drive.") else "teams"
            operation = slug_label(name.split(".", 1)[1])
            bad = failed or rec.output.get("error_code") or (rec.status_message and rec.level in ("WARNING", "ERROR"))
            integration_requests.labels(integration, operation, "error" if bad else "success").inc()
            integration_latency.labels(integration, operation).observe(rec.duration)

    # ----- events -----

    def on_event(self, name: str, workflow_id: str | None, step_number: int | None, metadata: dict[str, Any],
                 level: str | None) -> None:
        telemetry_events.labels(slug_label(name), (level or "DEFAULT").upper()).inc()
        if name == "execution.outcome":
            mode, target = self._exec_labels.get(workflow_id, ("unknown", "none"))
            status = slug_label(metadata.get("status"))
            executions_finished.labels(mode, target, status).inc()
            if status == "rejected":
                codes = metadata.get("error_codes") or []
                execution_rejections.labels(code_label(codes[0] if codes else None)).inc()
        elif name == "execution.retry_scheduled":
            integration, action = self._step_labels.get((workflow_id, step_number), ("other", "unknown"))
            step_retries.labels(integration, action, code_label(metadata.get("error_code"))).inc()
        elif name == "planning.fallback":
            llm_fallbacks.labels(slug_label(metadata.get("from_provider")), slug_label(metadata.get("to_provider")),
                                 code_label(metadata.get("reason"))).inc()


# ---------- reading back (for the UI summary: real values from this process only) ----------


def samples(metric_name: str) -> list[tuple[dict[str, str], float]]:
    """All samples of one metric family from the registry (e.g. 'ww_llm_requests_total')."""
    out: list[tuple[dict[str, str], float]] = []
    for family in REGISTRY.collect():
        for s in family.samples:
            if s.name == metric_name:
                out.append((dict(s.labels), float(s.value)))
    return out


def total(metric_name: str, **match: str) -> float:
    return sum(v for labels, v in samples(metric_name) if all(labels.get(k) == val for k, val in match.items()))
