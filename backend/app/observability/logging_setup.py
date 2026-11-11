"""Structured logging (Phase 8.1).

``configure_logging(settings)`` installs ONE root handler with either:

* ``JsonFormatter``    - one JSON object per line: timestamp, severity, service, environment,
                         logger, message, request_id, workflow_id, trace_id, span_id and, when
                         given, event, integration, action, step_number, duration_ms,
                         error_code, status, plus a sanitised ``attributes`` object.
* ``ConsoleFormatter`` - the familiar human-readable line, with the correlation ids appended.

Structured fields are passed with ``logger.info("...", extra=log_fields(event=..., ...))``.

Redaction is layered and applies to BOTH formats:
1. the existing TokenRedactingFilter on the handler scrubs the message and its arguments
   (registered integration secrets, access/refresh tokens, sig=, Google OAuth shapes);
2. the formatter scrubs the FINAL rendered line again with the telemetry secret patterns
   (API keys, bearer tokens, JWTs, webhook URLs) and sanitises ``attributes`` (credential keys
   redacted, content keys reduced to their length).
Nothing here ever logs request bodies, prompts, document text or headers.

A bounded in-memory ring buffer keeps the latest sanitised records for the UI's Logs tab
(this process only; it is not a log store).
"""

import json
import logging
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.core.log_redaction import TokenRedactingFilter, _scrub, install_secret_redaction, install_token_redaction
from app.observability.context import current_trace_ids, request_id_var, workflow_id_var
from app.observability.sanitize import sanitize, scrub_secret_values

STRUCTURED_FIELDS = ("event", "integration", "action", "step_number", "duration_ms", "error_code", "status",
                     "mode", "provider", "model", "http_status", "method", "route")
_FIELD_ATTR = "ww_fields"
RING_SIZE = 300


def log_fields(**fields: Any) -> dict[str, Any]:
    """``extra=`` payload for a structured record. Unknown keys go to ``attributes`` (sanitised)."""
    return {_FIELD_ATTR: {k: v for k, v in fields.items() if v is not None}}


def redact_line(text: str) -> str:
    """Final, untruncated scrub of a rendered log line."""
    return scrub_secret_values(_scrub(text))


class _ContextFilter(logging.Filter):
    """Adds request_id / workflow_id / trace_id to every record (also from third-party loggers)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        fields = getattr(record, _FIELD_ATTR, None) or {}
        record.workflow_id = fields.get("workflow_id") or workflow_id_var.get()
        record.trace_id, record.span_id = current_trace_ids()
        return True


def _structured(record: logging.LogRecord, service: str, environment: str) -> dict[str, Any]:
    fields = dict(getattr(record, _FIELD_ATTR, None) or {})
    fields.pop("workflow_id", None)
    out: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
        "severity": record.levelname,
        "service": service,
        "environment": environment,
        "logger": record.name,
        "message": redact_line(record.getMessage()),
        "request_id": getattr(record, "request_id", None),
        "workflow_id": getattr(record, "workflow_id", None),
        "trace_id": getattr(record, "trace_id", None),
        "span_id": getattr(record, "span_id", None),
    }
    for key in STRUCTURED_FIELDS:
        if key in fields:
            value = fields.pop(key)
            out[key] = redact_line(value) if isinstance(value, str) else value
    if fields:
        out["attributes"] = sanitize(fields)
    if record.exc_info and record.exc_info[0] is not None:
        # Exception TYPE only: messages and tracebacks can carry user data or secrets.
        out["exception_type"] = record.exc_info[0].__name__
    return {k: v for k, v in out.items() if v is not None}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service, self.environment = service, environment

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_structured(record, self.service, self.environment), default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable line for local development, with correlation ids and structured fields."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        saved_exc, record.exc_info, record.exc_text = record.exc_info, None, None
        try:
            line = super().format(record)
        finally:
            record.exc_info = saved_exc
        fields = getattr(record, _FIELD_ATTR, None) or {}
        extras = [f"{k}={fields[k]}" for k in STRUCTURED_FIELDS if k in fields]
        for key in ("request_id", "workflow_id", "trace_id"):
            if getattr(record, key, None):
                extras.append(f"{key}={getattr(record, key)}")
        if saved_exc and saved_exc[0] is not None:
            extras.append(f"exception_type={saved_exc[0].__name__}")
        return redact_line(line + (f" [{' '.join(str(e) for e in extras)}]" if extras else ""))


class RingBufferHandler(logging.Handler):
    """Keeps the latest sanitised structured records (for GET /api/v1/monitoring/logs)."""

    def __init__(self, service: str, environment: str, size: int = RING_SIZE) -> None:
        super().__init__(level=logging.INFO)
        self.service, self.environment = service, environment
        self._records: deque[dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = _structured(record, self.service, self.environment)
            with self._lock:
                self._records.append(entry)
        except Exception:
            pass  # never let monitoring break logging

    def recent(self, limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._records)
        if level:
            order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            floor = order.index(level) if level in order else 0
            items = [i for i in items if i.get("severity") in order[floor:]]
        return items[-limit:][::-1]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_ring: RingBufferHandler | None = None
_configured_lock = threading.Lock()


def recent_logs(limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
    return _ring.recent(limit, level) if _ring is not None else []


def resolve_format(settings: Any) -> str:
    fmt = getattr(settings, "log_format", "auto")
    if fmt == "auto":
        return "console" if str(getattr(settings, "app_env", "development")).lower() == "development" else "json"
    return fmt


def configure_logging(settings: Any, *, stream: Any = None) -> str:
    """Install the root handler (idempotent: replaces handlers it installed before). Returns the
    format in use. Uvicorn's loggers are routed through the same handler in JSON mode."""
    global _ring
    fmt = resolve_format(settings)
    service = getattr(settings, "service_name", "workflowweaver-api")
    env = str(getattr(settings, "app_env", "development"))
    with _configured_lock:
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_ww_owned", False):
                root.removeHandler(h)
                if isinstance(h, RotatingFileHandler):
                    h.close()
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter(service, env) if fmt == "json" else ConsoleFormatter())
        _ring = RingBufferHandler(service, env)
        handlers: list[logging.Handler] = [handler, _ring]
        log_file = getattr(settings, "log_file", None)
        if log_file:
            # Always JSON (machine-read by the collector), whatever the console format is.
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(JsonFormatter(service, env))
            handlers.append(file_handler)
        for h in handlers:
            h._ww_owned = True  # type: ignore[attr-defined]
            h.addFilter(_ContextFilter())
            h.addFilter(TokenRedactingFilter())
            root.addHandler(h)
        root.setLevel(str(getattr(settings, "log_level", "INFO")).upper())
        if fmt == "json":
            # One format for everything: uvicorn's own handlers would print plain text.
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                lg = logging.getLogger(name)
                lg.handlers = []
                lg.propagate = True
            # RequestContextMiddleware writes one structured access record per request.
            logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    install_token_redaction()
    install_secret_redaction()
    return fmt
