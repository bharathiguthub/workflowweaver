"""Deterministic in-memory mock adapters for every action in ACTION_CATALOG.

No network, files, MCP servers or webhooks are touched. Resource IDs are derived from a
hash of the idempotency key, so the same workflow_id + step always yields the same ID.
"""

import hashlib
from typing import Any

from app.agents.catalog import ACTION_CATALOG
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.agents.execution.tools.base import (
    CompensationCall,
    Effect,
    ErrorKind,
    ToolCall,
    ToolError,
    ToolResult,
)
from app.schemas.plan import Action
from app.schemas.workflow import Integration

# Synthetic documents available to google_drive.read_document (shared with the mock planner).
DRIVE_FIXTURES: dict[str, str] = SYNTHETIC_DRIVE_DOCUMENTS


def synthetic_id(prefix: str, key: str) -> str:
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


class MockToolStore:
    """Shared in-memory 'outside world' for all mock adapters."""

    def __init__(self) -> None:
        self.resources: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.completed: dict[str, ToolResult] = {}  # idempotency key -> first result
        self.side_effects: int = 0  # number of real (non-replayed) writes
        self.calls: list[tuple[str, str, int]] = []  # (operation, idempotency key, step_number)

    def live_resources(self) -> dict[str, dict[str, Any]]:
        return {rid: r for rid, r in self.resources.items() if not r["deleted"]}


def check_arguments(call: ToolCall) -> None:
    """Catalog argument check shared by every adapter (mock, dry-run, real)."""
    spec = ACTION_CATALOG[call.integration][call.action]
    unknown = set(call.arguments) - spec.allowed
    missing = {name for name in spec.required if not call.arguments.get(name, "").strip()}
    if unknown or missing:
        raise ToolError(
            ErrorKind.PERMANENT,
            "INVALID_ARGUMENTS",
            f"{call.integration.value}.{call.action.value}: missing={sorted(missing)} unknown={sorted(unknown)}",
        )


class _MockAdapter:
    integration: Integration
    effects: dict[Action, Effect]

    def __init__(self, store: MockToolStore) -> None:
        self.store = store

    def effect_of(self, action: Action) -> Effect:
        return self.effects[action]

    def execute(self, call: ToolCall) -> ToolResult:
        self.store.calls.append(("execute", call.idempotency_key, call.step_number))
        if call.action not in self.effects:
            raise ToolError(ErrorKind.PERMANENT, "UNSUPPORTED_ACTION", f"{call.action.value} not supported")
        check_arguments(call)

        previous = self.store.completed.get(call.idempotency_key)
        if previous is not None:  # duplicate delivery / retry after a lost response
            return previous.model_copy(update={"replayed": True})

        result = self._perform(call)
        if result.effect is not Effect.READ_ONLY:
            self.store.side_effects += 1
        self.store.completed[call.idempotency_key] = result
        return result

    def compensate(self, call: CompensationCall) -> bool:
        self.store.calls.append(("compensate", call.idempotency_key, call.step_number))
        resource = self.store.resources.get(call.resource_id)
        if resource is None:
            raise ToolError(ErrorKind.PERMANENT, "RESOURCE_NOT_FOUND", f"{call.resource_id} does not exist")
        if resource["deleted"]:
            return False  # idempotent: already undone
        resource["deleted"] = True
        return True

    def find_result(self, idempotency_key: str) -> ToolResult | None:
        return self.store.completed.get(idempotency_key)

    # ----- helpers -----

    def _create(self, call: ToolCall, prefix: str, resource_type: str) -> ToolResult:
        resource_id = synthetic_id(prefix, call.idempotency_key)
        self.store.resources[resource_id] = {
            "type": resource_type,
            "integration": call.integration.value,
            "data": dict(call.arguments),
            "deleted": False,
            "idempotency_key": call.idempotency_key,
        }
        return ToolResult(
            integration=call.integration,
            action=call.action,
            effect=self.effects[call.action],
            resource_id=resource_id,
            resource_type=resource_type,
            output={"url": f"mock://{call.integration.value}/{resource_id}", **_titles(call.arguments)},
        )

    def _perform(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError


def _titles(arguments: dict[str, str]) -> dict[str, str]:
    return {"title": arguments["title"]} if "title" in arguments else {}


class MockNotionAdapter(_MockAdapter):
    integration = Integration.NOTION
    effects = {Action.CREATE_PAGE: Effect.REVERSIBLE, Action.CREATE_TASK: Effect.REVERSIBLE}

    def _perform(self, call: ToolCall) -> ToolResult:
        if call.action is Action.CREATE_PAGE:
            return self._create(call, "mock-notion-page", "notion_page")
        return self._create(call, "mock-notion-task", "notion_task")


class MockGoogleDriveAdapter(_MockAdapter):
    integration = Integration.GOOGLE_DRIVE
    effects = {Action.CREATE_DOCUMENT: Effect.REVERSIBLE, Action.READ_DOCUMENT: Effect.READ_ONLY}

    def _perform(self, call: ToolCall) -> ToolResult:
        if call.action is Action.CREATE_DOCUMENT:
            return self._create(call, "mock-gdrive-doc", "drive_document")
        return self._read(call)

    def _read(self, call: ToolCall) -> ToolResult:
        name = call.arguments["document_name"]
        content = DRIVE_FIXTURES.get(name)
        resource_id = synthetic_id("mock-gdrive-fixture", name)
        if content is None:  # a document created earlier in mock execution can be read too
            match = next(
                (
                    (rid, r) for rid, r in self.store.resources.items()
                    if r["type"] == "drive_document" and not r["deleted"] and r["data"].get("title") == name
                ),
                None,
            )
            if match is None:
                raise ToolError(ErrorKind.PERMANENT, "DOCUMENT_NOT_FOUND", f"No synthetic document named '{name}'")
            resource_id, content = match[0], match[1]["data"]["content"]
        return ToolResult(
            integration=call.integration,
            action=call.action,
            effect=Effect.READ_ONLY,
            resource_id=resource_id,
            resource_type="drive_document",
            output={"document_name": name, "content": content},
        )


class MockTeamsAdapter(_MockAdapter):
    integration = Integration.TEAMS
    effects = {Action.SEND_MESSAGE: Effect.IRREVERSIBLE}

    def _perform(self, call: ToolCall) -> ToolResult:
        message_id = synthetic_id("mock-teams-msg", call.idempotency_key)
        self.store.messages.append(
            {"id": message_id, "channel": call.arguments.get("channel", "general"), "text": call.arguments["message"]}
        )
        return ToolResult(
            integration=call.integration,
            action=call.action,
            effect=Effect.IRREVERSIBLE,
            resource_id=message_id,
            resource_type="teams_message",
            output={"channel": call.arguments.get("channel", "general"), "delivered": True},
        )

    def compensate(self, call: CompensationCall) -> bool:  # pragma: no cover - guarded by registry
        raise ToolError(ErrorKind.PERMANENT, "NOT_COMPENSATABLE", "Teams messages cannot be unsent")
