"""Shared test helpers. All data is synthetic; no test touches the network."""

from types import SimpleNamespace

import pytest

from app.agents.planners.base import PlannerError
from app.schemas.plan import DraftPlan, DraftStep, StepArgument

EXAMPLE_INSTRUCTION = (
    "Read meeting notes, create a Notion project page, save a summary to Google Drive, "
    "and notify the team in Microsoft Teams."
)


def make_step(n, integration, action, depends_on=(), **arguments) -> DraftStep:
    return DraftStep(
        step_number=n,
        integration=integration,
        action=action,
        description=f"Synthetic step {n}",
        arguments=[StepArgument(name=k, value=v) for k, v in arguments.items()],
        depends_on=list(depends_on),
    )


def valid_draft() -> DraftPlan:
    return DraftPlan(
        summary="Synthetic three-step plan.",
        steps=[
            make_step(1, "notion", "create_page", title="Sprint 42 kickoff", content="Synthetic notes"),
            make_step(2, "google_drive", "create_document", title="Sprint 42 summary", content="Synthetic summary"),
            make_step(3, "teams", "send_message", depends_on=[1, 2], message="Plan is ready"),
        ],
    )


class StubPlanner:
    """Returns queued outcomes in order: a DraftPlan is returned, an exception is raised."""

    name = "stub"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def plan(self, instruction: str, *, max_steps: int) -> DraftPlan:
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def timeout_error() -> PlannerError:
    return PlannerError("PLANNER_TIMEOUT", "Claude request timed out.", retryable=True)


class FakeMessages:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome()
        return self.outcome


class FakeAnthropicClient:
    """Stands in for anthropic.Anthropic so tests never make real API calls."""

    def __init__(self, outcome) -> None:
        self.messages = FakeMessages(outcome)


def fake_response(parsed_output, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        parsed_output=parsed_output,
        stop_reason=stop_reason,
        _request_id="req_synthetic",
        usage=SimpleNamespace(output_tokens=42),
    )


@pytest.fixture(autouse=True)
def _no_real_telemetry():
    # Tests never export telemetry, even if the developer's .env sets LANGFUSE_ENABLED=true.
    # Telemetry tests install their own recording/faulty implementations explicitly.
    from app.observability import NoopTelemetry, set_telemetry

    set_telemetry(NoopTelemetry())
    yield
    set_telemetry(NoopTelemetry())


_INTEGRATION_ENV = (
    "INTEGRATION_MODE", "REAL_WRITES_ENABLED", "REAL_WRITE_AUTHORIZATION_TOKEN",
    "NOTION_MCP_URL", "NOTION_MCP_AUTH_TOKEN", "NOTION_DESTINATIONS", "NOTION_DEFAULT_PAGE_PARENT",
    "NOTION_TASK_DATABASE", "GOOGLE_DRIVE_MCP_URL", "GOOGLE_DRIVE_MCP_AUTH_TOKEN",
    "GOOGLE_DRIVE_DESTINATIONS", "GOOGLE_DRIVE_DEFAULT_FOLDER", "TEAMS_WEBHOOK_URLS", "TEAMS_DEFAULT_CHANNEL",
)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})


class ExternalNetworkBlocked(AssertionError):
    """A test tried to reach a non-loopback host."""


def _is_loopback(host) -> bool:
    import ipaddress

    if not isinstance(host, str):
        return False
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_real_integrations(monkeypatch):
    """Phase 5: no test may reach a real service.

    * Integration env vars from the developer's shell are removed (mock mode, no URLs).
    * httpx sync AND async transports refuse non-loopback hosts (fast, clear failure).
    * The socket layer refuses non-loopback connects and DNS lookups, which covers every
      other path (Anthropic SDK, Langfuse, urllib, raw sockets, MCP over any transport).
    Loopback stays allowed for the live-server SSE tests and the asyncio self-pipe.
    Integration tests use injected fake MCP transports and webhook senders instead."""
    import socket

    import httpx

    from app.integrations.secrets import clear_registered_secrets

    for name in _INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)

    def refuse(target) -> None:
        raise ExternalNetworkBlocked(f"Test attempted a real network call ({type(target).__name__}).")

    sync_original = httpx.HTTPTransport.handle_request
    async_original = httpx.AsyncHTTPTransport.handle_async_request

    def guarded_sync(self, request):
        if not _is_loopback(request.url.host):
            refuse(request)
        return sync_original(self, request)

    async def guarded_async(self, request):
        if not _is_loopback(request.url.host):
            refuse(request)
        return await async_original(self, request)

    socket_connect = socket.socket.connect
    socket_connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def _inet(sock, address) -> bool:
        return sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple)

    def guarded_connect(self, address):
        if _inet(self, address) and not _is_loopback(address[0]):
            refuse(address)
        return socket_connect(self, address)

    def guarded_connect_ex(self, address):
        if _inet(self, address) and not _is_loopback(address[0]):
            refuse(address)
        return socket_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback(host.decode() if isinstance(host, bytes) else host):
            refuse(host)
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_async)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
    clear_registered_secrets()


@pytest.fixture(autouse=True)
def _no_real_llm_key(monkeypatch):
    """No test can authenticate against a real LLM API, even if the developer's shell or
    .env has a key. Keys are set to "" rather than deleted: Settings reads ANTHROPIC_API_KEY
    and GEMINI_API_KEY from .env too, and an environment variable always wins over .env.
    PLANNER_MODE is pinned to mock (tests that need another mode set it themselves), and the
    settings cached at app import time (read before any fixture ran) are discarded."""
    from app.api import planning as planning_api
    from app.core.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)  # also read by the google-genai SDK
    monkeypatch.setenv("PLANNER_MODE", "mock")
    get_settings.cache_clear()
    planning_api._default_service.cache_clear()
    yield
    get_settings.cache_clear()
    planning_api._default_service.cache_clear()
