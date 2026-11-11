"""Request correlation + HTTP metrics + structured access log (Phase 8.1 / 8.2).

Pure ASGI middleware (not BaseHTTPMiddleware), so streaming responses such as the SSE
execution stream pass through untouched.

* ``X-Request-ID``: a valid caller-supplied id is kept, otherwise a new one is generated; it is
  bound to every log record of the request and returned in the response header.
* Metrics are labelled with the matched ROUTE TEMPLATE (``/api/v1/workflows/executions/{workflow_id}``),
  never the raw path or query string; unmatched paths are labelled ``unmatched``.
* One access record per request: method, route, status, duration_ms. Never the query string,
  headers or bodies (the SSE stream token travels in the query string).
"""

import logging
import time
import uuid
from typing import Any

from app.observability import metrics
from app.observability.context import request_id_var, valid_request_id
from app.observability.logging_setup import log_fields

logger = logging.getLogger("app.access")

# Paths whose requests are not logged at INFO (scraped/probed every few seconds).
QUIET_PATHS = frozenset({"/metrics", "/health"})


def _route_template(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None) or getattr(route, "path_format", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        request_id = valid_request_id(headers.get("x-request-id")) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        method = scope.get("method", "GET")
        status_holder = {"status": 500}
        started = time.perf_counter()
        metrics.http_in_progress.inc()

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode("latin-1"))]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_holder["status"] = 500
            raise
        finally:
            duration = time.perf_counter() - started
            metrics.http_in_progress.dec()
            route = _route_template(scope)
            status = status_holder["status"]
            try:
                metrics.http_requests.labels(method, route, str(status)).inc()
                metrics.http_latency.labels(method, route).observe(duration)
                if status >= 400:
                    metrics.http_errors.labels(method, route, str(status)).inc()
            except Exception:
                pass
            raw_path = scope.get("path", "")
            level = logging.WARNING if status >= 500 else (logging.DEBUG if raw_path in QUIET_PATHS else logging.INFO)
            logger.log(level, "%s %s %d", method, route, status, extra=log_fields(
                event="http.request", method=method, route=route, http_status=status,
                duration_ms=round(duration * 1000, 1)))
            request_id_var.reset(token)
