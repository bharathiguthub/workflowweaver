"""Phase 8.4: OpenTelemetry tracing. The probe runs in a subprocess (global provider + httpx patch)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def probe() -> dict:
    proc = subprocess.run([sys.executable, "-m", "tests.tracing_probe"], cwd=BACKEND, capture_output=True, text=True,
                          timeout=180)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_RESULT ")), None)
    assert line, f"probe failed (exit {proc.returncode}): {proc.stderr[-2000:]}"
    return json.loads(line.removeprefix("PROBE_RESULT "))


def test_setup_is_idempotent_and_registers_one_global_provider(probe):
    assert probe["enabled_first"] is True and probe["enabled_second"] is True
    assert probe["span_processors"] == 1 and probe["global_is_ours"] is True


def test_fastapi_server_spans_use_route_templates_and_skip_metrics_and_health(probe):
    assert probe["server_routes"] == ["/api/v1/workflows/executions/{workflow_id}"]


def test_workflow_operations_are_traced_as_one_tree_without_argument_values(probe):
    assert probe["execution_status"] == "COMPLETED"
    assert "execution.request" in probe["span_names"] and probe["tool_span_count"] == 4
    assert probe["workflow_spans_share_trace"] is True
    attrs = probe["tool_attributes"]
    assert attrs["ww.integration"] in ("google_drive", "notion", "teams") and "ww.duration_ms" in attrs
    assert not any("Synthetic" in str(v) or "Sprint" in str(v) for v in attrs.values())


def test_no_duplicate_spans_when_langfuse_is_enabled(probe):
    assert probe["span_sink_when_langfuse_enabled"] is False
    assert probe["langfuse_reuses_provider"] is True


def test_secrets_are_scrubbed_from_every_exported_span(probe):
    assert probe["secrets_in_export"] is False
    attrs = probe["probe_attributes"]
    assert attrs["http.request.header.authorization"] == "[REDACTED]"
    assert "[REDACTED]" in attrs["url.full"] and "[REDACTED]" in attrs["note"]


def test_log_records_carry_the_active_trace_id(probe):
    assert probe["log_trace_id"] == probe["span_trace_id"]
