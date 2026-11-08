"""ToolRegistry: routes a typed ToolCall to the adapter for its integration.

One registry implementation serves every integration mode; only the adapters differ:
  mock     MockToolRegistry (in-memory adapters, synthetic documents)
  dry_run  real adapters with no sender: they validate and build requests, send nothing
  real     real adapters with MCP / webhook clients
The execution graph, retries, idempotency and Saga compensation are identical in all modes.

Test-only failure injection (``faults``) works in every mode and is refused in production.
"""

from collections.abc import Collection

from app.agents.catalog import ACTION_CATALOG
from app.agents.execution.tools.base import (
    CompensationCall,
    Effect,
    ErrorKind,
    ToolAdapter,
    ToolCall,
    ToolError,
    ToolResult,
)
from app.agents.execution.tools.faults import Fault, FailureInjector
from app.agents.execution.tools.mock import (
    DRIVE_FIXTURES,
    MockGoogleDriveAdapter,
    MockNotionAdapter,
    MockTeamsAdapter,
    MockToolStore,
)
from app.core.config import IntegrationMode, Settings
from app.schemas.plan import Action, ValidationIssue, WorkflowStep
from app.schemas.workflow import Integration

_FAULT_ERRORS = {
    Fault.TRANSIENT: (ErrorKind.TRANSIENT, "TOOL_TIMEOUT", "Injected transient failure"),
    Fault.TRANSIENT_AFTER_EFFECT: (ErrorKind.TRANSIENT, "TOOL_TIMEOUT", "Injected timeout after the effect"),
    Fault.PERMANENT: (ErrorKind.PERMANENT, "TOOL_REJECTED", "Injected permanent failure"),
    Fault.PERMISSION_DENIED: (ErrorKind.PERMISSION, "PERMISSION_DENIED", "Injected permission failure"),
}


def _raise(fault: Fault) -> None:
    kind, code, message = _FAULT_ERRORS[fault]
    raise ToolError(kind, code, message)


class ToolRegistry:
    def __init__(
        self,
        adapters: list[ToolAdapter],
        *,
        mode: IntegrationMode = IntegrationMode.MOCK,
        faults: FailureInjector | None = None,
        documents: Collection[str] | None = None,
        real_writes_enabled: bool = False,
    ) -> None:
        self.mode = mode
        self.faults = faults
        self.adapters: dict[Integration, ToolAdapter] = {a.integration: a for a in adapters}
        self._documents = frozenset(documents) if documents is not None else None
        # Only meaningful in REAL mode: the server-wide kill switch for side effects.
        self.real_writes_enabled = real_writes_enabled

    def supported_actions(self) -> set[tuple[Integration, Action]]:
        supported = set()
        for integration, adapter in self.adapters.items():
            for action in ACTION_CATALOG[integration]:
                try:
                    adapter.effect_of(action)
                except KeyError:
                    continue
                supported.add((integration, action))
        return supported

    def document_catalog(self) -> frozenset[str] | None:
        """Names google_drive.read_document can serve without an earlier create step, or
        None when the catalog is not known offline (real/dry-run: the precheck is skipped)."""
        return self._documents

    def preflight_issues(self, steps: list[WorkflowStep]) -> list[ValidationIssue]:
        """Pure, network-free checks run before anything executes (e.g. approved
        destinations). Adapters opt in by defining ``preflight(integration, action, arguments)``."""
        issues: list[ValidationIssue] = []
        for step in steps:
            adapter = self.adapters.get(step.integration)
            check = getattr(adapter, "preflight", None)
            if check is None:
                continue
            for code, message in check(step.action, step.arguments):
                issues.append(ValidationIssue(code=code, message=message, step_number=step.step_number))
        return issues

    def effect_of(self, integration: Integration, action: Action) -> Effect:
        return self.adapters[integration].effect_of(action)

    def execute(self, call: ToolCall) -> ToolResult:
        adapter = self.adapters.get(call.integration)
        if adapter is None or call.action not in ACTION_CATALOG.get(call.integration, {}):
            raise ToolError(ErrorKind.PERMANENT, "UNSUPPORTED_ACTION", "No adapter for this action")

        fault = self.faults.next_execute_fault(call.step_number) if self.faults else None
        if fault is Fault.TRANSIENT_AFTER_EFFECT:
            adapter.execute(call)
            _raise(fault)
        if fault is Fault.MALFORMED_RESULT:
            return adapter.execute(call).model_copy(update={"resource_id": None})
        if fault is not None:
            _raise(fault)
        return self._verified(adapter, call, adapter.execute(call))

    def _verified(self, adapter, call: ToolCall, result: ToolResult) -> ToolResult:
        """Phase 7.2: when the run asks for it (call.verify_after), read a created resource back
        with the adapter's own read-only check. 'failed' fails the step permanently; the executor
        then journals the created resource (it is in the ledger) and compensates it."""
        verify = getattr(adapter, "verify", None)
        if (not call.verify_after or verify is None or not result.resource_id
                or adapter.effect_of(call.action) is Effect.READ_ONLY):
            return result
        if result.output.get("verification") == "verified":
            return result  # replayed result already verified in this run
        status = verify(call, result)
        result.output["verification"] = status
        if status == "failed":
            raise ToolError(ErrorKind.PERMANENT, "VERIFICATION_FAILED",
                            "The created resource could not be read back as expected.")
        return result

    def find_result(self, call: ToolCall) -> ToolResult | None:
        adapter = self.adapters.get(call.integration)
        return adapter.find_result(call.idempotency_key) if adapter else None

    def compensate(self, call: CompensationCall) -> bool:
        if self.effect_of(call.integration, call.action) is not Effect.REVERSIBLE:
            raise ToolError(ErrorKind.PERMANENT, "NOT_COMPENSATABLE", "Action has no compensating action")
        fault = self.faults.next_compensation_fault(call.step_number) if self.faults else None
        if fault is not None and fault is not Fault.MALFORMED_RESULT:
            _raise(fault)
        return self.adapters[call.integration].compensate(call)

    def close(self) -> None:
        """Release network clients (MCP sessions, HTTP pools). Never raises."""
        for adapter in self.adapters.values():
            close = getattr(adapter, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass


class MockToolRegistry(ToolRegistry):
    def __init__(self, store: MockToolStore | None = None, faults: FailureInjector | None = None) -> None:
        self.store = store or MockToolStore()
        super().__init__(
            [MockNotionAdapter(self.store), MockGoogleDriveAdapter(self.store), MockTeamsAdapter(self.store)],
            mode=IntegrationMode.MOCK,
            faults=faults,
            documents=DRIVE_FIXTURES,
        )


def create_tool_registry(settings: Settings, faults: FailureInjector | None = None, **clients) -> ToolRegistry:
    """Build the registry for ``settings.integration_mode``. Never opens a connection.

    ``clients`` (tests only) injects fake transports into the real adapters; see
    ``app.agents.execution.tools.integrations.build_integration_registry``."""
    if faults is not None and settings.app_env.lower() == "production":
        raise RuntimeError("Failure injection is disabled when APP_ENV=production.")
    if settings.integration_mode is IntegrationMode.MOCK:
        return MockToolRegistry(faults=faults)
    # Imported lazily: the integration layer imports this package's base module.
    from app.agents.execution.tools.integrations import build_integration_registry

    return build_integration_registry(settings, faults=faults, **clients)
