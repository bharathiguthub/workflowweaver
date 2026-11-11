"""Run by tests/test_tracing.py in a SUBPROCESS: enabling OpenTelemetry registers a process-global
tracer provider and instruments httpx globally, which must not leak into the rest of the suite.
Prints one JSON object with the observations. Nothing is exported over the network (in-memory
exporter) and no LLM or integration is called (mock tools only)."""

import json
import logging
import os

for name, value in {"PLANNER_MODE": "mock", "INTEGRATION_MODE": "mock", "LANGFUSE_ENABLED": "false",
                    "OTEL_ENABLED": "false", "LOG_FILE": "", "GROQ_API_KEY": "", "GEMINI_API_KEY": "",
                    "ANTHROPIC_API_KEY": "", "METRICS_AUTH_TOKEN": ""}.items():
    os.environ[name] = value

from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.main import app  # noqa: E402
from app.observability import NoopTelemetry, set_telemetry, tracing  # noqa: E402
from app.observability.composite import CompositeTelemetry, default_sinks  # noqa: E402
from app.observability.logging_setup import recent_logs  # noqa: E402
from tests.execution_helpers import make_request, make_service  # noqa: E402

# Secret-shaped values assembled at runtime (never real).
STREAM_TOKEN = "SYNTHETIC" + "streamtoken0001"
SIG = "SYNTHETIC" + "signature0001"
NOTION = "ntn_" + "SYNTHETICnotARealNotionToken000000000001"

out: dict = {}
mem = InMemorySpanExporter()
settings = Settings(_env_file=None, otel_enabled=True, service_name="workflowweaver-api")
out["enabled_first"] = tracing.setup_tracing(app, settings, exporter=mem)
out["enabled_second"] = tracing.setup_tracing(app, settings, exporter=mem)
provider = tracing._state["provider"]
out["span_processors"] = len(provider._active_span_processor._span_processors)
out["global_is_ours"] = trace.get_tracer_provider() is provider

# 1. FastAPI server span: route template, no query-string secret.
TestClient(app).get(f"/api/v1/workflows/executions/00000000-0000-4000-8000-000000000001?access_token={STREAM_TOKEN}")
TestClient(app).get("/health")
TestClient(app).get("/metrics")

# 2. Workflow spans through the composite facade (Langfuse off -> SpanSink active).
set_telemetry(CompositeTelemetry(NoopTelemetry(), default_sinks(), span_sink=tracing.span_sink()))
service, _ = make_service()
result = service.execute(make_request())
out["execution_status"] = result.status.value

# 3. Langfuse on -> the composite must NOT add its own spans (Langfuse spans are OTel spans).
class _EnabledBase(NoopTelemetry):
    enabled = True

out["span_sink_when_langfuse_enabled"] = CompositeTelemetry(
    _EnabledBase(), [], span_sink=tracing.span_sink())._span_sink is not None

# 4. Langfuse reuses OUR provider (one provider, no duplicate export pipeline).
from langfuse._client.resource_manager import _init_tracer_provider  # noqa: E402

out["langfuse_reuses_provider"] = _init_tracer_provider(environment="test") is provider

# 5. Sanitising exporter: credential attributes and secret values never leave the process.
tracer = provider.get_tracer("probe")
with tracer.start_as_current_span("probe.outbound") as span:
    span.set_attribute("url.full", f"https://prod-00.example.invalid/workflows/x/invoke?api-version=1&sig={SIG}")
    span.set_attribute("http.request.header.authorization", "Bearer SYNTHETICbearer0001")
    span.set_attribute("note", f"token {NOTION}")
    span.add_event("probe.event", {"detail": f"sig={SIG}"})
    logging.getLogger("app.probe").warning("inside a span")
    out["log_trace_id"] = next((r.get("trace_id") for r in recent_logs(20) if r.get("message") == "inside a span"), None)
    out["span_trace_id"] = f"{span.get_span_context().trace_id:032x}"

spans = mem.get_finished_spans()
dump = json.dumps([{"name": s.name, "attributes": dict(s.attributes or {}),
                    "events": [dict(e.attributes or {}) for e in s.events]} for s in spans], default=str)
out["secrets_in_export"] = any(v in dump for v in (STREAM_TOKEN, SIG, NOTION, "SYNTHETICbearer0001"))
out["span_names"] = sorted({s.name for s in spans})
server = [s for s in spans if s.kind == trace.SpanKind.SERVER]
out["server_routes"] = sorted({str((s.attributes or {}).get("http.route")) for s in server})
probe = next(s for s in spans if s.name == "probe.outbound")
out["probe_attributes"] = dict(probe.attributes or {})
by_name = {s.name: s for s in spans}
root = by_name.get("execution.request")
tool_spans = [s for s in spans if s.name.startswith("tool.")]
out["tool_span_count"] = len(tool_spans)
out["workflow_spans_share_trace"] = bool(root) and all(s.context.trace_id == root.context.trace_id for s in tool_spans)
out["tool_attributes"] = dict(tool_spans[0].attributes or {}) if tool_spans else {}
print("PROBE_RESULT " + json.dumps(out, default=str))
