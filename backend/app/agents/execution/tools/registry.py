"""MockToolRegistry: routes a typed ToolCall to the adapter for its integration."""

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
    MockGoogleDriveAdapter,
    MockNotionAdapter,
    MockTeamsAdapter,
    MockToolStore,
)
from app.core.config import Settings
from app.schemas.plan import Action
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


class MockToolRegistry:
    def __init__(self, store: MockToolStore | None = None, faults: FailureInjector | None = None) -> None:
        self.store = store or MockToolStore()
        self.faults = faults
        adapters: list[ToolAdapter] = [
            MockNotionAdapter(self.store),
            MockGoogleDriveAdapter(self.store),
            MockTeamsAdapter(self.store),
        ]
        self.adapters: dict[Integration, ToolAdapter] = {a.integration: a for a in adapters}

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
        return adapter.execute(call)

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


def create_tool_registry(settings: Settings, faults: FailureInjector | None = None) -> MockToolRegistry:
    if faults is not None and settings.app_env.lower() == "production":
        raise RuntimeError("Failure injection is disabled when APP_ENV=production.")
    return MockToolRegistry(faults=faults)
