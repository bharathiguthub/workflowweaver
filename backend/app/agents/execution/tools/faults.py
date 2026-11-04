"""Test-only failure injection for the mock tool registry.

The API never accepts fault configuration. Faults can only be attached by constructing a
registry in code (tests, scripts), and ``create_tool_registry`` refuses to do that when
APP_ENV=production.
"""

from dataclasses import dataclass, field
from enum import Enum


class Fault(str, Enum):
    TRANSIENT = "transient"  # fail before the side effect; retryable
    TRANSIENT_AFTER_EFFECT = "transient_after_effect"  # effect happens, then "timeout"
    PERMANENT = "permanent"  # non-retryable failure
    PERMISSION_DENIED = "permission_denied"  # non-retryable permission failure
    MALFORMED_RESULT = "malformed_result"  # tool "succeeds" but returns no resource id


@dataclass
class FailureInjector:
    """Faults are consumed in order, one per attempt, keyed by step_number."""

    execute_faults: dict[int, list[Fault]] = field(default_factory=dict)
    compensation_faults: dict[int, list[Fault]] = field(default_factory=dict)

    def next_execute_fault(self, step_number: int) -> Fault | None:
        return _pop(self.execute_faults, step_number)

    def next_compensation_fault(self, step_number: int) -> Fault | None:
        return _pop(self.compensation_faults, step_number)


def _pop(faults: dict[int, list[Fault]], step_number: int) -> Fault | None:
    queue = faults.get(step_number)
    return queue.pop(0) if queue else None
