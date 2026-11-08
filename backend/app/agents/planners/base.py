"""Planner interface shared by the mock, Claude, Gemini and Groq planners."""

from typing import Protocol

from app.schemas.plan import DraftPlan

# HTTP statuses that mean "the provider is temporarily unavailable or failing" (as opposed to
# "this request, key or output is wrong"). Only these, plus connection errors and timeouts,
# may trigger a fallback to another provider: 429 rate limit / quota, provider-side failures
# 500 / 502 / 503, and the timeout statuses 408 / 504.
PROVIDER_UNAVAILABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class PlannerError(Exception):
    """A planning attempt failed.

    ``retryable``: the graph may try the SAME provider again (bounded retry policy).
    ``provider_unavailable``: the provider itself is down / overloaded / unreachable
    (HTTP 429/500/502/503, timeout, connection error). Only these errors allow a fallback to another
    provider - never credential errors, refusals (unsafe content) or schema failures.
    ``http_status``: the provider's HTTP status code, if any (reported to Langfuse).
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 provider_unavailable: bool = False, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.provider_unavailable = provider_unavailable
        self.http_status = http_status


class PlannerConfigError(Exception):
    """The planner cannot be constructed (e.g. Claude mode without a model configured)."""


class Planner(Protocol):
    name: str

    def plan(self, instruction: str, *, max_steps: int) -> DraftPlan:
        """Return a draft plan or raise PlannerError. Must never execute anything."""
        ...


def planner_model(planner: object) -> str | None:
    """The model id a planner uses, if it exposes one (the mock planner does not)."""
    model = getattr(planner, "model", None)
    return model if isinstance(model, str) else None


def provider_chain(planner: object) -> list:
    """The ordered providers to try: ``planner.providers`` for a FallbackPlanner, else [planner]."""
    providers = getattr(planner, "providers", None)
    return list(providers) if providers else [planner]


class FallbackPlanner:
    """An ordered provider chain (primary first). It does not call anything itself: the
    planning graph tries ``providers[0]`` with the bounded retry policy and moves to the next
    provider only after a ``provider_unavailable`` failure. Each draft comes whole from ONE
    provider; outputs are never merged."""

    name = "auto"

    def __init__(self, providers: list) -> None:
        if not providers:
            raise ValueError("FallbackPlanner needs at least one provider.")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate planner providers: {names}")
        self.providers = list(providers)

    @property
    def model(self) -> str | None:
        return planner_model(self.providers[0])

    def plan(self, instruction: str, *, max_steps: int) -> DraftPlan:  # pragma: no cover - graph uses providers
        return self.providers[0].plan(instruction, max_steps=max_steps)
