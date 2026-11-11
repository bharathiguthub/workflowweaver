"""Phase 8.3 / 8.6: the committed monitoring configuration stays consistent and safe.

* every ww_* metric used by a Grafana dashboard or Prometheus alert is actually emitted
  (catches typos that would silently show empty panels or never fire);
* the stack is local-only and has no committed secrets;
* the JSON log file the collector tails is valid JSON per line.
"""

import io
import json
import logging
import re
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.observability import metrics
from app.observability.logging_setup import configure_logging, log_fields

ROOT = Path(__file__).resolve().parents[2]
MON = ROOT / "monitoring"
METRIC = re.compile(r"\bww_[a-z0-9_]+")
SUFFIXES = ("_bucket", "_sum", "_count", "_created")


def registered_metric_names() -> set[str]:
    names = set()
    for family in metrics.REGISTRY.collect():
        names.add(family.name)
        names.add(family.name + "_total")  # counters are exposed with the _total suffix
    return names


def referenced(text: str) -> set[str]:
    out = set()
    for name in METRIC.findall(text):
        for suffix in SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        out.add(name)
    return out


@pytest.mark.parametrize("path", sorted((MON / "grafana" / "dashboards").glob("*.json")) +
                         [MON / "prometheus" / "alerts.yml"], ids=lambda p: p.name)
def test_dashboards_and_alerts_only_use_emitted_metrics(path):
    used = referenced(path.read_text(encoding="utf-8"))
    assert used, f"{path.name} references no ww_* metric"
    assert used <= registered_metric_names(), f"unknown metrics in {path.name}: {sorted(used - registered_metric_names())}"


def test_dashboards_are_valid_and_use_provisioned_datasources():
    uids = set()
    for path in (MON / "grafana" / "dashboards").glob("*.json"):
        d = json.loads(path.read_text(encoding="utf-8"))
        uids.add(d["uid"])
        for panel in d["panels"]:
            assert panel["datasource"]["uid"] in {"prometheus", "loki"}
            assert panel["targets"] and all(t["expr"] for t in panel["targets"])
    assert uids == {"ww-api", "ww-workflows", "ww-integrations"}  # matches app.api.monitoring.DASHBOARDS
    from app.api.monitoring import DASHBOARDS

    assert set(DASHBOARDS) == uids


def test_alert_rules_cover_errors_slow_workflows_and_integration_failures():
    rules = {r["alert"]: r for g in yaml.safe_load((MON / "prometheus" / "alerts.yml").read_text())["groups"]
             for r in g["rules"]}
    assert {"HighApiErrorRate", "SlowWorkflowExecutions", "IntegrationFailures"} <= set(rules)
    assert all(r["labels"]["severity"] in {"info", "warning", "critical"} for r in rules.values())


def test_compose_publishes_ports_on_loopback_only_and_requires_a_grafana_password():
    compose = yaml.safe_load((MON / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond loopback"
        assert ":latest" not in service["image"], f"{name} image is not pinned"
    env = compose["services"]["grafana"]["environment"]
    assert env["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false" and env["GF_USERS_ALLOW_SIGN_UP"] == "false"


SECRET_SHAPES = re.compile(r"(gsk_[A-Za-z0-9]{16,}|sk-ant-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|ntn_[A-Za-z0-9]{20,}"
                           r"|GOCSPX-[A-Za-z0-9_-]{10,}|sk-lf-[A-Za-z0-9-]{8,}|sig=[A-Za-z0-9%_-]{10,}"
                           r"|(?i:password)\s*[:=]\s*['\"]?[A-Za-z0-9!@#$%^&*]{6,})")


def test_no_secrets_in_committed_monitoring_configuration():
    for path in MON.rglob("*"):
        if path.is_file() and path.suffix in {".yml", ".yaml", ".json", ".example"} and "secrets" not in path.parts:
            assert not SECRET_SHAPES.search(path.read_text(encoding="utf-8")), path


def test_json_log_file_has_one_object_per_line(tmp_path):
    log_file = tmp_path / "logs" / "workflowweaver.jsonl"
    try:
        configure_logging(Settings(_env_file=None, log_format="console", log_file=str(log_file)), stream=io.StringIO())
        logging.getLogger("app.test").info("file record", extra=log_fields(event="unit.file", duration_ms=1.5))
    finally:
        configure_logging(Settings(_env_file=None), stream=None)  # closes the file handler
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    record = next(r for r in lines if r.get("event") == "unit.file")
    assert record["severity"] == "INFO" and record["service"] == "workflowweaver-api" and record["duration_ms"] == 1.5
