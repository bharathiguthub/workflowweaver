"""Bounded retry policy with capped exponential backoff and optional jitter.

Only TRANSIENT tool errors are retried; PERMANENT and PERMISSION errors fail immediately.
The policy computes delays only; sleeping is done by an injectable ``sleeper`` so tests
never wait.
"""

import random
from dataclasses import dataclass

from app.agents.execution.tools.base import ToolError
from app.core.config import Settings


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.2
    max_delay_seconds: float = 2.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delays must be >= 0")

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy":
        return cls(
            max_attempts=settings.execution_max_attempts,
            base_delay_seconds=settings.execution_backoff_base_seconds,
            max_delay_seconds=settings.execution_backoff_max_seconds,
            jitter=settings.execution_backoff_jitter,
        )

    @classmethod
    def for_planning(cls, settings: Settings) -> "RetryPolicy":
        """Backoff between planner (LLM) attempts; attempts are bounded by MAX_PLANNING_ATTEMPTS."""
        return cls(
            max_attempts=settings.max_planning_attempts,
            base_delay_seconds=settings.planning_backoff_base_seconds,
            max_delay_seconds=settings.planning_backoff_max_seconds,
            jitter=settings.planning_backoff_jitter,
        )

    def should_retry(self, error: ToolError, attempt: int) -> bool:
        """``attempt`` is the 1-based number of the attempt that just failed."""
        return error.retryable and attempt < self.max_attempts

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay before the next attempt, after ``attempt`` failed.

        base * 2^(attempt-1), capped at max. With jitter ("equal jitter") the delay is
        uniform in [d/2, d], so it is never above the cap and never collapses to zero.
        """
        delay = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))
        if self.jitter and delay > 0:
            delay = delay / 2 + (rng or random).uniform(0, delay / 2)
        return round(delay, 4)
