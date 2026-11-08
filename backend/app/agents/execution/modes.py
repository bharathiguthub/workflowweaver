"""Per-request integration mode under a server-controlled ceiling.

INTEGRATION_MODE is the HIGHEST mode this server may run (mock < dry_run < real). A request
selects its mode with ``approval.mode``; any mode at or below the ceiling is allowed, a
higher one never is. Downgrading is safe by construction: mock and dry_run make no network
call and no side effect. Real mode keeps every gate of the execution graph (approval, the
REAL_WRITES_ENABLED kill switch, the server-verified operator token, approved destinations).

Each mode gets its own ExecutionService (its own registry, idempotency store and ledger),
built lazily on first use. A workflow_id is bound to the first mode it was submitted in, so
one plan can never be replayed or re-run across modes; generate a new plan to switch modes.
"""

import threading
from collections.abc import Callable

from app.agents.execution.service import ExecutionService, IdempotencyConflict, ModeMismatch
from app.core.config import IntegrationMode
from app.schemas.execution import ExecutionRequest, ExecutionResponse

_ORDER = {IntegrationMode.MOCK: 0, IntegrationMode.DRY_RUN: 1, IntegrationMode.REAL: 2}


def allowed_modes(ceiling: IntegrationMode) -> list[IntegrationMode]:
    """Modes a client may request on a server whose INTEGRATION_MODE is ``ceiling``."""
    return [m for m in IntegrationMode if _ORDER[m] <= _ORDER[ceiling]]


class ModeRoutedExecutionService:
    """Same interface as ExecutionService (execute / get / check_conflict / registry)."""

    def __init__(self, ceiling: IntegrationMode, factory: Callable[[IntegrationMode], ExecutionService]) -> None:
        self.ceiling = ceiling
        self._factory = factory
        self._services: dict[IntegrationMode, ExecutionService] = {}
        self._claims: dict[str, IntegrationMode] = {}
        self._guard = threading.Lock()

    @property
    def allowed_modes(self) -> list[IntegrationMode]:
        return allowed_modes(self.ceiling)

    def service_for(self, mode: IntegrationMode) -> ExecutionService:
        """Raises IntegrationConfigError if that mode's configuration is invalid."""
        if mode not in self.allowed_modes:
            raise ModeMismatch(f"This server allows modes up to '{self.ceiling.value}'; '{mode.value}' is not allowed.")
        with self._guard:
            service = self._services.get(mode)
            if service is None:
                service = self._services[mode] = self._factory(mode)
            return service

    @property
    def registry(self):
        """The ceiling mode's registry (compatibility with single-mode callers)."""
        return self.service_for(self.ceiling).registry

    def _claim(self, request: ExecutionRequest, mode: IntegrationMode) -> None:
        key = str(request.workflow_id)
        with self._guard:
            previous = self._claims.setdefault(key, mode)
        if previous is not mode:
            raise IdempotencyConflict(
                f"workflow_id {key} was already submitted in '{previous.value}' mode; "
                f"generate a new plan to run it in '{mode.value}' mode."
            )

    def check_conflict(self, request: ExecutionRequest) -> None:
        mode = IntegrationMode(request.approval.mode)
        service = self.service_for(mode)  # ModeMismatch above the ceiling
        self._claim(request, mode)
        service.check_conflict(request)

    def execute(self, request: ExecutionRequest, *, write_authorized: bool = False) -> ExecutionResponse:
        mode = IntegrationMode(request.approval.mode)
        if mode not in self.allowed_modes:
            # Escalation attempt: the ceiling service's graph rejects it (MODE_MISMATCH)
            # before any tool is called, with the usual response, status code and telemetry.
            return self.service_for(self.ceiling).execute(request, write_authorized=write_authorized)
        service = self.service_for(mode)
        self._claim(request, mode)
        return service.execute(request, write_authorized=write_authorized)

    def get(self, workflow_id: str) -> ExecutionResponse | None:
        with self._guard:
            mode = self._claims.get(workflow_id)
            service = self._services.get(mode) if mode is not None else None
        return service.get(workflow_id) if service is not None else None

    def close(self) -> None:
        with self._guard:
            services = list(self._services.values())
            self._services.clear()
        for service in services:
            service.registry.close()
