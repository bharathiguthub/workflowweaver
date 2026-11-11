"""Monitoring endpoints (Phase 8.2 / 8.5).

GET /metrics                          Prometheus exposition (optional bearer token: METRICS_AUTH_TOKEN)
GET /api/v1/monitoring/summary        what is configured + real counters from THIS API process
GET /api/v1/monitoring/logs           latest sanitised structured log records (this process)
GET /api/v1/monitoring/traces         latest planning/execution runs with trace ids (this process)

Every number comes from the in-process Prometheus registry, the log ring buffer or the recent
run index - nothing is estimated or synthesised. Values are "since this API process started"
and the responses say so. Service reachability is probed server-side only for URLs configured
in the backend environment (never URLs from the request), and only http(s) URLs are probed;
the probe URLs themselves are not returned.
"""

import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST

from app.core.config import get_settings
from app.observability import metrics
from app.observability.composite import WORKFLOW_INDEX
from app.observability.logging_setup import recent_logs, resolve_format
from app.observability.tracing import tracing_enabled

router = APIRouter(tags=["monitoring"])

PROCESS_STARTED = time.time()
PROBE_TIMEOUT = 1.5
# Dashboards provisioned by monitoring/grafana (uid -> title).
DASHBOARDS = {"ww-api": "API performance", "ww-workflows": "Workflow execution", "ww-integrations": "Integration health"}


# ---------- /metrics ----------


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics(authorization: str | None = Header(default=None)) -> Response:
    settings = get_settings()
    if not settings.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Metrics are disabled (METRICS_ENABLED=false).")
    token = settings.metrics_auth_token.get_secret_value() if settings.metrics_auth_token else ""
    if token:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Metrics require the configured bearer token.",
                                headers={"WWW-Authenticate": "Bearer"})
    return Response(content=metrics.exposition(), media_type=CONTENT_TYPE_LATEST)


# ---------- helpers ----------


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return url.rstrip("/") if parsed.scheme in ("http", "https") and parsed.netloc else None


def _probe(name: str, base: str | None, path: str) -> dict[str, Any]:
    url = _safe_url(base)
    if url is None:
        return {"name": name, "configured": False, "status": "not_configured"}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            response = client.get(url + path)
        up = response.status_code < 400
        return {"name": name, "configured": True, "status": "up" if up else "down",
                "http_status": response.status_code, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        return {"name": name, "configured": True, "status": "down", "error": type(exc).__name__}


def _by(metric: str, *keys: str) -> dict[tuple[str, ...], float]:
    out: dict[tuple[str, ...], float] = {}
    for labels, value in metrics.samples(metric):
        k = tuple(labels.get(key, "") for key in keys)
        out[k] = out.get(k, 0.0) + value
    return out


def _avg_ms(metric: str, *keys: str) -> dict[tuple[str, ...], float | None]:
    sums, counts = _by(f"{metric}_sum", *keys), _by(f"{metric}_count", *keys)
    return {k: (round(sums.get(k, 0.0) / c * 1000, 1) if c else None) for k, c in counts.items()}


def grafana_trace_url(grafana: str | None, trace_id: str | None) -> str | None:
    base = _safe_url(grafana)
    if not base or not trace_id:
        return None
    panes = {"t": {"datasource": "tempo", "queries": [{"refId": "A", "datasource": {"type": "tempo", "uid": "tempo"},
                                                        "queryType": "traceql", "query": trace_id}],
                   "range": {"from": "now-24h", "to": "now"}}}
    return f"{base}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps(panes, separators=(',', ':')))}"


# ---------- summary ----------


@router.get("/api/v1/monitoring/summary")
def monitoring_summary(probe: bool = Query(default=True)) -> dict[str, Any]:
    settings = get_settings()
    grafana = _safe_url(settings.grafana_url)
    langfuse_ui = _safe_url(settings.langfuse_ui_url)

    req = _by("ww_http_requests_total", "route", "status")
    total_requests = sum(req.values())
    errors_5xx = sum(v for (route, code), v in req.items() if code.startswith("5"))
    errors_4xx = sum(v for (route, code), v in req.items() if code.startswith("4"))
    lat_sum = sum(_by("ww_http_request_duration_seconds_sum", "route").values())
    lat_count = sum(_by("ww_http_request_duration_seconds_count", "route").values())
    route_counts = _by("ww_http_requests_total", "route")
    route_errors = _by("ww_http_request_errors_total", "route")
    route_avg = _avg_ms("ww_http_request_duration_seconds", "route")
    routes = sorted(
        ({"route": r, "requests": int(n), "errors": int(route_errors.get((r,), 0)), "avg_ms": route_avg.get((r,))}
         for (r,), n in route_counts.items() if r not in ("/metrics",)),
        key=lambda x: -x["requests"])[:10]

    llm_req = _by("ww_llm_requests_total", "provider", "model", "outcome")
    llm_avg = _avg_ms("ww_llm_request_duration_seconds", "provider", "model")
    tokens = _by("ww_llm_tokens_total", "provider", "model", "type")
    providers: dict[tuple[str, str], dict[str, Any]] = {}
    for (provider, model, outcome), n in llm_req.items():
        p = providers.setdefault((provider, model), {"provider": provider, "model": model, "requests": 0, "errors": 0,
                                                     "tokens": {}})
        p["requests"] += int(n)
        if outcome == "error":
            p["errors"] += int(n)
    for (provider, model, kind), n in tokens.items():
        providers.setdefault((provider, model), {"provider": provider, "model": model, "requests": 0, "errors": 0,
                                                 "tokens": {}})["tokens"][kind] = int(n)
    for key, p in providers.items():
        p["avg_latency_ms"] = llm_avg.get(key)

    services = []
    if probe:
        services = [
            _probe("Prometheus", settings.prometheus_url, "/-/ready"),
            _probe("Grafana", settings.grafana_url, "/api/health"),
            _probe("Loki", settings.loki_url, "/ready"),
            _probe("Tempo", settings.tempo_url, "/ready"),
        ]

    return {
        "scope": "since this API process started",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "process_started_at": datetime.fromtimestamp(PROCESS_STARTED, tz=timezone.utc).isoformat(timespec="seconds"),
        "uptime_seconds": round(time.time() - PROCESS_STARTED),
        "config": {
            "service_name": settings.service_name,
            "environment": settings.app_env,
            "log_format": resolve_format(settings),
            "log_level": settings.log_level.upper(),
            "metrics_enabled": settings.metrics_enabled,
            "metrics_protected": bool(settings.metrics_auth_token and settings.metrics_auth_token.get_secret_value()),
            "tracing_enabled": tracing_enabled(),
            "langfuse_enabled": settings.langfuse_enabled,
        },
        "links": {
            "grafana": grafana,
            "langfuse": langfuse_ui,
            "dashboards": [{"uid": uid, "title": title, "url": f"{grafana}/d/{uid}"} for uid, title in DASHBOARDS.items()]
            if grafana else [],
        },
        "services": services,
        "api": {
            "requests": int(total_requests),
            "errors_4xx": int(errors_4xx),
            "errors_5xx": int(errors_5xx),
            "error_rate_5xx": round(errors_5xx / total_requests, 4) if total_requests else None,
            "avg_latency_ms": round(lat_sum / lat_count * 1000, 1) if lat_count else None,
            "in_progress": int(metrics.total("ww_http_requests_in_progress")),
            "routes": routes,
        },
        "workflows": {
            "plans": [{"target": t, "status": s, "count": int(n)} for (t, s), n in _by("ww_workflow_plans_total", "target", "status").items()],
            "executions_started": int(metrics.total("ww_workflow_executions_started_total")),
            "executions_in_progress": int(metrics.total("ww_workflow_executions_in_progress")),
            "executions_finished": [{"status": s, "count": int(n)}
                                    for (s,), n in _by("ww_workflow_executions_finished_total", "status").items()],
            "rejections": [{"reason": r, "count": int(n)} for (r,), n in _by("ww_workflow_rejections_total", "reason").items()],
            "step_retries": int(metrics.total("ww_step_retries_total")),
            "compensations": [{"outcome": o, "count": int(n)}
                              for (o,), n in _by("ww_compensations_total", "outcome").items()],
        },
        "integrations": {
            "step_failures": [{"integration": i, "action": a, "error_code": c, "count": int(n)}
                              for (i, a, c), n in _by("ww_integration_failures_total", "integration", "action",
                                                      "error_code").items()],
            "steps": [{"integration": i, "status": s, "count": int(n)}
                      for (i, s), n in _by("ww_step_duration_seconds_count", "integration", "status").items()],
            "calls": [{"integration": i, "outcome": o, "count": int(n)}
                      for (i, o), n in _by("ww_integration_requests_total", "integration", "outcome").items()],
        },
        "ai": {
            "providers": sorted(providers.values(), key=lambda p: -p["requests"]),
            "fallbacks": [{"from_provider": f, "to_provider": t, "reason": r, "count": int(n)}
                          for (f, t, r), n in _by("ww_llm_fallbacks_total", "from_provider", "to_provider",
                                                  "reason").items()],
        },
    }


@router.get("/api/v1/monitoring/logs")
def monitoring_logs(limit: int = Query(default=100, ge=1, le=300),
                    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None) -> dict[str, Any]:
    settings = get_settings()
    grafana = _safe_url(settings.grafana_url)
    explore = None
    if grafana:
        panes = {"l": {"datasource": "loki", "queries": [{"refId": "A", "datasource": {"type": "loki", "uid": "loki"},
                                                          "expr": '{service_name="workflowweaver-api"}'}],
                       "range": {"from": "now-1h", "to": "now"}}}
        explore = f"{grafana}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps(panes, separators=(',', ':')))}"
    return {"scope": "latest records from this API process (sanitised)", "records": recent_logs(limit, level),
            "loki_explore_url": explore}


@router.get("/api/v1/monitoring/traces")
def monitoring_traces(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    settings = get_settings()
    items = WORKFLOW_INDEX.recent(limit)
    for item in items:
        item["trace_url"] = grafana_trace_url(settings.grafana_url, item.get("trace_id"))
    return {"scope": "latest planning and execution runs in this API process", "tracing_enabled": tracing_enabled(),
            "langfuse_enabled": settings.langfuse_enabled, "langfuse_url": _safe_url(settings.langfuse_ui_url),
            "runs": items}
