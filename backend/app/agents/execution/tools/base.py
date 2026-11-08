"""Tool adapter contract shared by mock adapters now and real MCP/webhook adapters later.

Idempotency contract (every adapter MUST honour it, real adapters included):
  * ``execute`` with an ``idempotency_key`` it has already completed must return the
    original result with ``replayed=True`` and must not create a second side effect.
  * ``compensate`` of an effect that is already undone must succeed (ALREADY_COMPENSATED).
The mock adapters implement this in memory. Real adapters must implement it against the
remote system (e.g. store key -> resource id durably, or use the API's own idempotency
support); it does not come for free.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.schemas.plan import Action
from app.schemas.workflow import Integration


class Effect(str, Enum):
    READ_ONLY = "read_only"  # no side effect; never journaled or compensated
    REVERSIBLE = "reversible"  # side effect with a compensating action
    IRREVERSIBLE = "irreversible"  # side effect that cannot be undone (Teams message)


class ErrorKind(str, Enum):
    TRANSIENT = "transient"  # may succeed if retried (timeout, 429, 5xx)
    PERMANENT = "permanent"  # will fail again (bad arguments, not found)
    PERMISSION = "permission"  # not allowed; never retried


class ToolError(Exception):
    def __init__(self, kind: ErrorKind, code: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.kind is ErrorKind.TRANSIENT


@dataclass(frozen=True)
class ToolCall:
    workflow_id: str
    step_number: int
    integration: Integration
    action: Action
    arguments: dict[str, str]
    attempt: int = 1
    # Server-verified authorization for real side effects (operator token checked by the
    # API). Real adapters refuse writes without it; mock and dry-run adapters ignore it.
    write_authorized: bool = False
    # Resource ids created by THIS run's completed dependency steps (step_number -> id). Set
    # by the executor, never by the client; used by notion create_page "parent_step".
    upstream: dict[int, str] = field(default_factory=dict, compare=False)
    # Phase 7.2: read the created resource back after a successful write (adapters that can).
    verify_after: bool = False

    @property
    def idempotency_key(self) -> str:
        return f"{self.workflow_id}:step-{self.step_number}"


class ToolResult(BaseModel):
    integration: Integration
    action: Action
    effect: Effect
    resource_id: str | None
    resource_type: str | None
    output: dict[str, Any] = Field(default_factory=dict)
    replayed: bool = False


@dataclass(frozen=True)
class CompensationCall:
    workflow_id: str
    step_number: int
    integration: Integration
    action: Action
    resource_id: str
    attempt: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return f"{self.workflow_id}:step-{self.step_number}:compensate"


class ToolAdapter(Protocol):
    integration: Integration

    def effect_of(self, action: Action) -> Effect:
        """How the action affects the outside world; drives journaling and compensation."""
        ...

    def execute(self, call: ToolCall) -> ToolResult:
        """Perform the action. Raise ToolError on failure. Must be idempotent per key."""
        ...

    def compensate(self, call: CompensationCall) -> bool:
        """Undo a REVERSIBLE effect. Return True if undone now, False if already undone.

        Raise ToolError on failure. Must be idempotent.
        """
        ...

    def find_result(self, idempotency_key: str) -> ToolResult | None:
        """Reconcile: return the result of an already-performed call with this key, if any.

        Used after a failure whose outcome is unknown (e.g. a timeout after the effect) so
        the effect can still be journaled and compensated.
        """
        ...
