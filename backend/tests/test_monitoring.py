"""Phase 8.1 / 8.2: structured logging, redaction, request correlation and Prometheus metrics.

Everything runs in-process against mock tools and fake LLM clients. Metric counters are
process-wide, so tests assert DELTAS.
"""

import io
import json
import logging
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.execution.tools import Fault
from app.agents.planners import FallbackPlanner, GeminiPlanner, GroqPlanner
from app.agents.service import PlanningService
from app.api import monitoring as monitoring_api
from app.core.config import Settings
from app.integrations.secrets import clear_registered_secrets, register_secret
from app.main import app
from app.observability import NoopTelemetry, set_telemetry
from app.observability import metrics
from app.observability.composite import WORKFLOW_INDEX, CompositeTelemetry, default_sinks
from app.observability.context import request_id_var, workflow_id_var
from app.observability.logging_setup import (
    ConsoleFormatter,
    JsonFormatter,
    _ContextFilter,
    configure_logging,
    log_fields,
    recent_logs,
)
from app.core.log_redaction import TokenRedactingFilter
from tests.conftest import EXAMPLE_INSTRUCTION
from tests.execution_helpers import FOUR_STEPS, make_request, make_service
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, ok_response
from tests.test_planner_fallback import GROQ_MODEL, FakeGroqClient, groq_status_error

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Synthetic secret-shaped values, assembled at runtime so the source never contains a
# credential-shaped literal. None of them is real.
SECRETS = {
    "groq": "gsk_" + "SYNTHETICnotARealKey00000000001",
    "notion": "ntn_" + "SYNTHETICnotARealNotionToken000000000001",
    "anthropic": "sk-ant-" + "SYNTHETIC-not-a-real-key-0001",
    "google_secret": "GOCSPX" + "-SYNTHETIC-not-real-0001",
    "bearer": "Bearer " + "SYNTHETICbearerTokenValue0001",
    "webhook": "https://prod-00.westeurope.logic.azure.com/workflows/x/triggers/manual/paths/invoke?api-version=1&sig="
               + "SYNTHETICsignature0001",
    "query_token": "access_token=" + "SYNTHETICstreamToken0001",
}


@pytest.fixture
def composite():
    WORKFLOW_INDEX.clear()
    telemetry = CompositeTelemetry(NoopTelemetry(), default_sinks())
    set_telemetry(telemetry)
    yield telemetry
    set_telemetry(NoopTelemetry())


def render(formatter: logging.Formatter, logger_name: str, message: str, *args, level=logging.INFO, exc_info=None,
           **fields) -> str:
    record = logging.LogRecord(logger_name, level, __file__, 1, message, args, exc_info)
    if fields:
        record.ww_fields = fields
    _ContextFilter().filter(record)
    TokenRedactingFilter().filter(record)
    return formatter.format(record)


# ======================= 8.1 structured logging =======================


def test_json_record_has_every_correlation_and_structured_field():
    token_r, token_w = request_id_var.set("req-123"), workflow_id_var.set("wf-456")
    try:
        line = render(JsonFormatter("workflowweaver-api", "test"), "app.workflow", "step failed",
                      event="step.failed", integration="notion", action="create_page", step_number=2,
                      duration_ms=12.5, error_code="NOTION_RATE_LIMITED", attempt=1)
    finally:
        request_id_var.reset(token_r)
        workflow_id_var.reset(token_w)
    data = json.loads(line)
    assert data["severity"] == "INFO" and data["service"] == "workflowweaver-api" and data["environment"] == "test"
    assert data["timestamp"].endswith("+00:00") and data["message"] == "step failed"
    assert (data["request_id"], data["workflow_id"]) == ("req-123", "wf-456")
    assert (data["event"], data["integration"], data["action"], data["step_number"]) == \
        ("step.failed", "notion", "create_page", 2)
    assert data["duration_ms"] == 12.5 and data["error_code"] == "NOTION_RATE_LIMITED"
    assert data["attributes"] == {"attempt": 1}


def test_trace_id_is_added_when_a_span_is_active():
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("unit") as span:
        data = json.loads(render(JsonFormatter("s", "e"), "x", "inside span"))
        assert data["trace_id"] == f"{span.get_span_context().trace_id:032x}"
        assert data["span_id"] == f"{span.get_span_context().span_id:016x}"


@pytest.mark.parametrize("formatter", [JsonFormatter("s", "e"), ConsoleFormatter()], ids=["json", "console"])
def test_secrets_never_reach_either_log_format(formatter):
    registered = "https://synthetic.example.invalid/webhook-registered-" + "0001"
    register_secret(registered)
    try:
        text = " ".join(SECRETS.values()) + " " + registered
        out = render(formatter, "app.test", "leak %s", text, authorization=SECRETS["bearer"],
                     api_key=SECRETS["groq"], content="SYNTHETIC-DOCUMENT-BODY", note=SECRETS["notion"])
    finally:
        clear_registered_secrets()
    for name, value in SECRETS.items():
        assert value.split("=")[-1].split(" ")[-1] not in out, name
    assert registered not in out and "SYNTHETIC-DOCUMENT-BODY" not in out
    assert "[REDACTED]" in out


def test_exceptions_log_their_type_only_never_the_message():
    try:
        raise ValueError("user document text " + SECRETS["groq"])
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    data = json.loads(render(JsonFormatter("s", "e"), "app.test", "failed", level=logging.ERROR, exc_info=exc_info))
    assert data["exception_type"] == "ValueError" and "user document text" not in json.dumps(data)
    console = render(ConsoleFormatter(), "app.test", "failed", level=logging.ERROR, exc_info=exc_info)
    assert "exception_type=ValueError" in console and "Traceback" not in console


def test_configure_logging_is_idempotent_and_supports_both_formats():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        stream = io.StringIO()
        assert configure_logging(Settings(_env_file=None, log_format="json", app_env="production"), stream=stream) == "json"
        configure_logging(Settings(_env_file=None, log_format="json", app_env="production"), stream=stream)
        owned = [h for h in root.handlers if getattr(h, "_ww_owned", False)]
        assert len(owned) == 2  # one stream handler + the ring buffer, never duplicated
        logging.getLogger("app.test").info("hello", extra=log_fields(event="unit.test"))
        assert json.loads(stream.getvalue().strip().splitlines()[-1])["event"] == "unit.test"
        assert configure_logging(Settings(_env_file=None, log_format="auto", app_env="development"),
                                 stream=io.StringIO()) == "console"
    finally:
        configure_logging(Settings(_env_file=None), stream=None)
        assert len([h for h in root.handlers if getattr(h, "_ww_owned", False)]) == 2
        del before


def test_request_id_is_generated_echoed_and_validated():
    client = TestClient(app)
    generated = client.get("/health").headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    assert client.get("/health", headers={"X-Request-ID": "trace-abc.123"}).headers["x-request-id"] == "trace-abc.123"
    injected = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline" * 5}).headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", injected)


def test_access_records_use_the_route_template_and_never_the_query_string():
    configure_logging(Settings(_env_file=None, log_format="json"), stream=io.StringIO())
    wid = str(uuid4())
    TestClient(app).get(f"/api/v1/workflows/executions/{wid}?{SECRETS['query_token']}",
                        headers={"X-Request-ID": "req-route-test"})
    record = next(r for r in recent_logs(50) if r.get("request_id") == "req-route-test" and r.get("event") == "http.request")
    assert record["route"] == "/api/v1/workflows/executions/{workflow_id}" and record["http_status"] == 404
    dumped = json.dumps(record)
    assert wid not in dumped and "SYNTHETICstreamToken0001" not in dumped and "access_token" not in dumped


# ======================= 8.2 Prometheus metrics =======================


def test_metrics_endpoint_exposes_http_metrics_with_route_templates_only():
    client = TestClient(app)
    wid = str(uuid4())
    before = metrics.total("ww_http_requests_total", route="/api/v1/workflows/executions/{workflow_id}", status="404")
    client.get(f"/api/v1/workflows/executions/{wid}")
    response = client.get("/metrics")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'ww_http_requests_total{method="GET",route="/api/v1/workflows/executions/{workflow_id}",status="404"}' in body
    assert "ww_http_request_duration_seconds_bucket" in body and "ww_http_request_errors_total" in body
    assert metrics.total("ww_http_requests_total", route="/api/v1/workflows/executions/{workflow_id}",
                         status="404") == before + 1
    assert wid not in body and not UUID.search(body)  # no ids in labels


def test_metrics_can_be_protected_by_a_bearer_token_or_disabled(monkeypatch):
    token = "synthetic-metrics-token-" + "0001"
    settings = Settings(_env_file=None, metrics_auth_token=token)
    monkeypatch.setattr(monitoring_api, "get_settings", lambda: settings)
    client = TestClient(app)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    monkeypatch.setattr(monitoring_api, "get_settings", lambda: Settings(_env_file=None, metrics_enabled=False))
    assert client.get("/metrics").status_code == 404


def test_planning_and_execution_update_workflow_and_step_metrics(composite):
    plans_before = metrics.total("ww_workflow_plans_total", status="PLANNED".lower())
    started_before = metrics.total("ww_workflow_executions_started_total", mode="mock")
    completed_before = metrics.total("ww_workflow_executions_finished_total", status="completed")
    steps_before = metrics.total("ww_step_duration_seconds_count", integration="notion", status="success")

    response = TestClient(app).post("/api/v1/workflows/plan", json={"instruction": EXAMPLE_INSTRUCTION})
    assert response.status_code == 200
    assert metrics.total("ww_workflow_plans_total", status="planned") == plans_before + 1

    service, _ = make_service()
    result = service.execute(make_request())
    assert result.status.value == "COMPLETED"
    assert metrics.total("ww_workflow_executions_started_total", mode="mock") == started_before + 1
    assert metrics.total("ww_workflow_executions_finished_total", status="completed") == completed_before + 1
    assert metrics.total("ww_step_duration_seconds_count", integration="notion", status="success") == steps_before + 1
    assert metrics.total("ww_workflow_executions_in_progress") == 0
    runs = WORKFLOW_INDEX.recent()
    assert [r["kind"] for r in runs[:2]] == ["execution", "planning"] and runs[0]["status"] == "COMPLETED"


def test_retries_failures_compensation_and_rejections_are_counted(composite, caplog):
    retries_before = metrics.total("ww_step_retries_total", integration="notion")
    failures_before = metrics.total("ww_integration_failures_total", integration="teams")
    comp_before = metrics.total("ww_compensations_total", outcome="compensated")
    rejected_before = metrics.total("ww_workflow_rejections_total", reason="APPROVAL_REQUIRED")

    # Step 2 (Notion) fails once then succeeds; step 4 (Teams) fails permanently -> compensation.
    service, _ = make_service({2: [Fault.TRANSIENT], 4: [Fault.PERMANENT]})
    caplog.set_level(logging.INFO, logger="app.workflow")
    result = service.execute(make_request())
    assert result.status.value != "COMPLETED"
    assert metrics.total("ww_step_retries_total", integration="notion") == retries_before + 1
    assert metrics.total("ww_integration_failures_total", integration="teams") == failures_before + 1
    assert metrics.total("ww_compensations_total", outcome="compensated") >= comp_before + 1

    service2, _ = make_service()
    rejected = service2.execute(make_request(approved=False))
    assert rejected.status.value == "REJECTED"
    assert metrics.total("ww_workflow_rejections_total", reason="APPROVAL_REQUIRED") == rejected_before + 1

    events = {getattr(r, "ww_fields", {}).get("event") for r in caplog.records}
    assert {"execution.started", "step.failed", "execution.retry_scheduled", "compensation.succeeded",
            "approval.rejected"} <= events
    # Structured workflow logs carry codes and ids, never argument values.
    assert "Synthetic notes" not in caplog.text and "Sprint 42" not in caplog.text


def test_llm_requests_latency_tokens_and_fallbacks_are_tracked(composite):
    fallbacks_before = metrics.total("ww_llm_fallbacks_total", from_provider="groq", to_provider="gemini")
    groq_err_before = metrics.total("ww_llm_requests_total", provider="groq", outcome="error")
    gemini_ok_before = metrics.total("ww_llm_requests_total", provider="gemini", outcome="success")
    tokens_before = metrics.total("ww_llm_tokens_total", provider="gemini", type="input")

    planner = FallbackPlanner([
        GroqPlanner(client=FakeGroqClient(groq_status_error(429)), model=GROQ_MODEL, max_tokens=1024),
        GeminiPlanner(client=FakeGenaiClient(ok_response()), model=GEMINI_MODEL, max_output_tokens=1024),
    ])
    result = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan(EXAMPLE_INSTRUCTION)
    assert result.planner == "gemini" and result.fallback_used is True
    assert metrics.total("ww_llm_requests_total", provider="groq", outcome="error") == groq_err_before + 1
    assert metrics.total("ww_llm_requests_total", provider="gemini", outcome="success") == gemini_ok_before + 1
    assert metrics.total("ww_llm_fallbacks_total", from_provider="groq", to_provider="gemini") == fallbacks_before + 1
    assert metrics.total("ww_llm_tokens_total", provider="gemini", type="input") > tokens_before
    assert metrics.total("ww_llm_errors_total", provider="groq", error_code="PLANNER_RATE_LIMITED") >= 1
    assert metrics.total("ww_llm_request_duration_seconds_count", provider="gemini") >= 1


def test_metric_labels_never_contain_ids_or_free_text(composite):
    service, _ = make_service()
    wid = uuid4()
    service.execute(make_request(workflow_id=wid))
    body = metrics.exposition().decode()
    assert str(wid) not in body and not UUID.search(body)
    for step in FOUR_STEPS:
        for value in step["arguments"].values():
            assert f'"{value}"' not in body


def test_a_failing_sink_never_breaks_the_workflow():
    class Broken:
        def on_start(self, rec): raise RuntimeError("boom")
        def on_end(self, rec): raise RuntimeError("boom")
        def on_event(self, *a): raise RuntimeError("boom")

    set_telemetry(CompositeTelemetry(NoopTelemetry(), [Broken()]))
    try:
        service, _ = make_service()
        assert service.execute(make_request()).status.value == "COMPLETED"
    finally:
        set_telemetry(NoopTelemetry())


# ======================= monitoring API (real data only) =======================


def test_summary_reports_only_real_counters_and_unconfigured_services(composite, monkeypatch):
    monkeypatch.setattr(monitoring_api, "get_settings", lambda: Settings(_env_file=None))
    service, _ = make_service()
    service.execute(make_request())
    body = TestClient(app).get("/api/v1/monitoring/summary").json()
    assert body["scope"] == "since this API process started"
    assert body["links"] == {"grafana": None, "langfuse": None, "dashboards": []}
    assert {s["name"]: s["status"] for s in body["services"]} == {
        "Prometheus": "not_configured", "Grafana": "not_configured", "Loki": "not_configured", "Tempo": "not_configured"}
    assert body["workflows"]["executions_started"] >= 1 and body["api"]["requests"] >= 1
    assert body["config"]["tracing_enabled"] is False and body["config"]["metrics_enabled"] is True


def test_unreachable_configured_services_are_reported_down_not_fabricated(monkeypatch):
    settings = Settings(_env_file=None, prometheus_url="http://127.0.0.1:9", grafana_url="http://127.0.0.1:9",
                        loki_url="ftp://127.0.0.1:9", tempo_url=None)
    monkeypatch.setattr(monitoring_api, "get_settings", lambda: settings)
    services = {s["name"]: s for s in TestClient(app).get("/api/v1/monitoring/summary").json()["services"]}
    assert services["Prometheus"]["status"] == "down" and services["Grafana"]["status"] == "down"
    assert services["Loki"]["status"] == "not_configured"  # only http(s) URLs are probed
    assert "http://127.0.0.1:9" not in json.dumps(services)  # probe URLs are never returned


def test_logs_and_traces_endpoints_return_sanitised_recent_data(composite, monkeypatch):
    monkeypatch.setattr(monitoring_api, "get_settings", lambda: Settings(_env_file=None, grafana_url="http://127.0.0.1:3000"))
    configure_logging(Settings(_env_file=None, log_format="json"), stream=io.StringIO())
    logging.getLogger("app.test").warning("token %s", SECRETS["groq"], extra=log_fields(event="unit.leak"))
    service, _ = make_service()
    service.execute(make_request())
    client = TestClient(app)
    logs = client.get("/api/v1/monitoring/logs", params={"level": "WARNING"}).json()
    assert all(r["severity"] in ("WARNING", "ERROR", "CRITICAL") for r in logs["records"])
    assert "SYNTHETICnotARealKey" not in json.dumps(logs) and logs["loki_explore_url"].startswith("http://127.0.0.1:3000/explore")
    traces = client.get("/api/v1/monitoring/traces").json()
    assert traces["runs"][0]["kind"] == "execution" and traces["runs"][0]["status"] == "COMPLETED"
