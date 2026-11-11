"""Distributed tracing with OpenTelemetry (Phase 8.4). Off unless OTEL_ENABLED=true.

``setup_tracing(app, settings)``:
* registers ONE global TracerProvider (service.name, service.version, deployment.environment)
  exporting over OTLP/HTTP to the local collector, through ``SanitizingSpanExporter``;
* instruments FastAPI (server spans; /metrics and /health excluded; ASGI receive/send
  sub-spans dropped) and httpx (outbound calls: Groq/Gemini SDKs, Drive REST, Notion MCP,
  Teams webhooks), propagating W3C trace context;
* is idempotent - a second call never instruments twice.

Langfuse coexistence (no duplicates, no leaks):
* Langfuse v5 is itself OpenTelemetry-based. Because our provider is registered first, the
  Langfuse client attaches its span processor to it, so workflow spans created through the
  facade are ONE set of spans: exported to Langfuse (its default filter exports only Langfuse /
  gen_ai spans, never our HTTP spans) and to Tempo (the whole tree, correctly parented).
* The composite facade therefore creates its own workflow spans (``SpanSink``) only when
  Langfuse is OFF. Our span attributes never use the ``gen_ai.*`` namespace, which Langfuse
  would pick up.

Sensitive data: URLs can carry the SSE ``access_token`` query parameter or a Teams webhook
``sig``; headers are never captured. Every string attribute of every exported span (including
span events) is scrubbed with the log/telemetry redaction rules, and credential-named
attributes are replaced entirely, before anything leaves the process.
"""

import logging
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_SECRET_ATTR = re.compile(r"(authorization|cookie|token|secret|password|api[_-]?key|signature|credential)",
                          re.IGNORECASE)
_lock = threading.Lock()
_state: dict[str, Any] = {"provider": None, "httpx": False, "apps": set(), "tracer": None}


def _clean_value(key: str, value: Any) -> Any:
    from app.observability.logging_setup import redact_line

    if _SECRET_ATTR.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_line(value)
    if isinstance(value, (list, tuple)):
        return tuple(redact_line(v) if isinstance(v, str) else v for v in value)
    return value


def clean_attributes(attributes: Any) -> dict[str, Any]:
    return {k: _clean_value(k, v) for k, v in (attributes or {}).items()}


class SanitizingSpanExporter:
    """SpanExporter wrapper: scrubs attributes (and event attributes) of every span before export."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace import Event, ReadableSpan

        cleaned = []
        for s in spans:
            try:
                cleaned.append(ReadableSpan(
                    name=s.name, context=s.context, parent=s.parent, resource=s.resource,
                    attributes=clean_attributes(s.attributes),
                    events=[Event(e.name, clean_attributes(e.attributes), e.timestamp) for e in s.events],
                    links=s.links, kind=s.kind, status=s.status, start_time=s.start_time, end_time=s.end_time,
                    instrumentation_scope=s.instrumentation_scope))
            except Exception:
                continue  # drop a span rather than export it unsanitised
        return self._inner.export(cleaned)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush = getattr(self._inner, "force_flush", None)
        return flush(timeout_millis) if flush else True


def tracing_enabled() -> bool:
    return _state["provider"] is not None


def setup_tracing(app: Any, settings: Any, *, exporter: Any = None) -> bool:
    """Configure tracing if OTEL_ENABLED. ``exporter`` (tests): a SpanExporter to use instead of OTLP."""
    if not getattr(settings, "otel_enabled", False):
        return False
    with _lock:
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
            from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

            if _state["provider"] is None:
                current = trace.get_tracer_provider()
                if not isinstance(current, trace.ProxyTracerProvider):
                    # Someone (e.g. Langfuse, created earlier) registered a provider: reuse it.
                    provider = current
                    logger.warning("OpenTelemetry provider already registered; adding the OTLP exporter to it")
                else:
                    provider = TracerProvider(
                        resource=Resource.create({
                            "service.name": settings.service_name,
                            "service.version": getattr(app, "version", "unknown"),
                            "deployment.environment": settings.app_env,
                        }),
                        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
                    )
                    trace.set_tracer_provider(provider)
                if exporter is None:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
                    processor = BatchSpanProcessor(SanitizingSpanExporter(OTLPSpanExporter(endpoint=endpoint, timeout=5)))
                else:
                    processor = SimpleSpanProcessor(SanitizingSpanExporter(exporter))
                provider.add_span_processor(processor)
                _state["provider"] = provider
                _state["tracer"] = provider.get_tracer("workflowweaver")

            if not _state["httpx"]:
                from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

                instrumentor = HTTPXClientInstrumentor()
                if not instrumentor.is_instrumented_by_opentelemetry:
                    instrumentor.instrument(tracer_provider=_state["provider"])
                _state["httpx"] = True

            if id(app) not in _state["apps"]:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.instrument_app(
                    app, tracer_provider=_state["provider"], excluded_urls="metrics,health",
                    exclude_spans=["receive", "send"])
                _state["apps"].add(id(app))
            logger.info("OpenTelemetry tracing enabled", extra={"ww_fields": {
                "event": "tracing.enabled", "endpoint": settings.otel_exporter_otlp_endpoint}})
            return True
        except Exception as exc:  # tracing must never stop the API from starting
            logger.warning("OpenTelemetry tracing could not be enabled error=%s", type(exc).__name__)
            return False


def shutdown_tracing() -> None:
    provider = _state["provider"]
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:
            pass


class SpanSink:
    """Workflow spans for facade observations (used only while Langfuse is off)."""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    @contextmanager
    def span(self, rec: Any) -> Iterator[Any]:
        attributes: dict[str, Any] = {"ww.observation_type": rec.as_type}
        if rec.workflow_id:
            attributes["ww.workflow_id"] = str(rec.workflow_id)
        if rec.step_number is not None:
            attributes["ww.step_number"] = int(rec.step_number)
        if rec.model:
            attributes["ww.llm.model"] = str(rec.model)
        parts = rec.name.split(".")
        if parts[0] in ("tool", "compensation") and len(parts) >= 3:
            attributes["ww.integration"], attributes["ww.action"] = parts[1], parts[2]
        with self._tracer.start_as_current_span(rec.name, attributes=attributes) as span:
            yield span

    def finish(self, span: Any, rec: Any) -> None:
        from opentelemetry.trace import Status, StatusCode

        span.set_attribute("ww.duration_ms", round(rec.duration * 1000, 1))
        status = rec.output.get("status")
        if isinstance(status, str):
            span.set_attribute("ww.status", status)
        error_code = rec.output.get("error_code") or rec.error
        if error_code:
            span.set_attribute("ww.error_code", str(error_code))
        for kind, count in (rec.usage or {}).items():
            span.set_attribute(f"ww.llm.tokens.{kind}", int(count))
        if rec.error or rec.level == "ERROR":
            span.set_status(Status(StatusCode.ERROR, str(error_code or "error")))

    def event(self, name: str, metadata: dict[str, Any], level: str | None) -> None:
        from opentelemetry import trace

        from app.observability.sanitize import sanitize

        span = trace.get_current_span()
        if not span.get_span_context().is_valid:
            return
        attrs = {"ww.level": level or "DEFAULT"}
        for key, value in sanitize(metadata).items():
            if isinstance(value, (str, bool, int, float)):
                attrs[f"ww.{key}"] = value
        span.add_event(name, attributes=attrs)


def span_sink() -> SpanSink | None:
    return SpanSink(_state["tracer"]) if _state["tracer"] is not None else None
