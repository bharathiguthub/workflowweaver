"""Real and dry-run adapters for Notion (MCP), Google Drive (MCP) and Teams (webhook).

They implement the same ``ToolAdapter`` contract as the mock adapters, so the execution
graph (retries, Saga compensation, SSE events, tracing) is unchanged. Each call goes:

    check arguments -> idempotency ledger -> write authorization -> resolve approved
    destination -> build the exact request -> send (real) | describe it (dry_run)

DRY_RUN runs every step except the send, so it validates configuration, destinations and
request shapes with no network access at all (no client objects are even created).

Idempotency: results are recorded per idempotency key in an in-memory ledger, so a retry or
duplicate delivery inside this process replays the stored result instead of writing again.
A transient failure AFTER a write request was sent (timeout, 5xx) becomes the permanent
OUTCOME_UNKNOWN: retrying could duplicate the page/document/message. The ledger is
process-local and not durable (Stage 5.1 limitation).

Outputs returned to the API/SSE/traces contain ids, URLs of created resources, aliases and
counts only. Destination ids, webhook URLs, credentials and read document contents are
never included.
"""

import threading
from dataclasses import dataclass, field
from typing import Any

from app.agents.execution.tools.base import (
    CompensationCall,
    Effect,
    ErrorKind,
    ToolCall,
    ToolError,
    ToolResult,
)
from app.agents.execution.tools.faults import FailureInjector
from app.agents.execution.tools.mock import check_arguments, synthetic_id
from app.agents.execution.tools.registry import ToolRegistry
from app.core.config import IntegrationMode, Settings
from app.integrations.config import (
    DestinationConfig,
    DriveToolNames,
    IntegrationConfig,
    NotionToolNames,
    load_integration_config,
)
from app.integrations.destinations import DestinationPolicy, ResolvedDestination
from app.integrations.errors import IntegrationError, unknown_outcome
from app.integrations.http.webhook import WebhookClient, WebhookSender
from app.integrations.mcp.client import McpClient, McpToolResult
from app.schemas.plan import Action
from app.schemas.workflow import Integration

NOTION_TEXT_CHUNK = 1900  # Notion rich text limit is 2000 characters per item
NOTION_MAX_BLOCKS = 100
TEAMS_MAX_MESSAGE_CHARS = 4000
MAX_SEARCH_RESULTS = 50


class IdempotencyLedger:
    """Thread-safe, in-memory record of completed calls and compensated resources."""

    def __init__(self) -> None:
        self._results: dict[str, ToolResult] = {}
        self._compensated: set[str] = set()
        self._lock = threading.Lock()

    def get(self, key: str) -> ToolResult | None:
        with self._lock:
            return self._results.get(key)

    def put(self, key: str, result: ToolResult) -> None:
        with self._lock:
            self._results.setdefault(key, result)

    def mark_compensated(self, resource_id: str) -> bool:
        """True if newly marked, False if it already was."""
        with self._lock:
            if resource_id in self._compensated:
                return False
            self._compensated.add(resource_id)
            return True

    def is_compensated(self, resource_id: str) -> bool:
        with self._lock:
            return resource_id in self._compensated


@dataclass(frozen=True)
class PreparedRequest:
    """What the adapter will send. ``arguments`` may hold user content and destination ids:
    it is passed to the client only, never logged, returned or traced."""

    operation: str  # MCP tool name or "teams_webhook"
    destination: ResolvedDestination | None
    arguments: dict[str, Any] = field(repr=False)


class _IntegrationAdapter:
    integration: Integration
    effects: dict[Action, Effect]

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger) -> None:
        if mode is IntegrationMode.MOCK:
            raise ValueError("Integration adapters run in dry_run or real mode only.")
        self.mode = mode
        self.ledger = ledger

    # ----- ToolAdapter -----

    def effect_of(self, action: Action) -> Effect:
        return self.effects[action]

    def preflight(self, action: Action, arguments: dict[str, str]) -> list[tuple[str, str]]:
        policy = self._policy(action)
        problem = policy.problem(arguments) if policy else None
        return [("DESTINATION_NOT_APPROVED", problem)] if problem else []

    def execute(self, call: ToolCall) -> ToolResult:
        if call.action not in self.effects:
            raise ToolError(ErrorKind.PERMANENT, "UNSUPPORTED_ACTION", f"{call.action.value} not supported")
        check_arguments(call)
        previous = self.ledger.get(call.idempotency_key)
        if previous is not None:
            return previous.model_copy(update={"replayed": True})

        effect = self.effects[call.action]
        if effect is not Effect.READ_ONLY and self.mode is IntegrationMode.REAL and not call.write_authorized:
            raise ToolError(ErrorKind.PERMISSION, "REAL_WRITE_NOT_AUTHORIZED", "Real writes are not authorized.")

        request = self._prepare(call)
        if self.mode is IntegrationMode.DRY_RUN:
            result = self._dry_run(call, request)
        else:
            try:
                result = self._send(call, request)
            except IntegrationError as exc:
                if exc.retryable and exc.request_sent and effect is not Effect.READ_ONLY:
                    raise unknown_outcome(exc) from exc
                raise
        if effect is Effect.READ_ONLY or result.resource_id:
            self.ledger.put(call.idempotency_key, result)
        return result

    def compensate(self, call: CompensationCall) -> bool:
        if self.effects.get(call.action) is not Effect.REVERSIBLE:
            raise ToolError(ErrorKind.PERMANENT, "NOT_COMPENSATABLE", "Action has no compensating action")
        if self.ledger.is_compensated(call.resource_id):
            return False
        if self.mode is IntegrationMode.REAL:
            self._undo(call)  # undo operations are idempotent remotely, so retries are safe
        return self.ledger.mark_compensated(call.resource_id)

    def find_result(self, idempotency_key: str) -> ToolResult | None:
        return self.ledger.get(idempotency_key)

    def close(self) -> None:
        pass

    # ----- helpers -----

    def _result(self, call: ToolCall, resource_id: str | None, resource_type: str,
                output: dict[str, Any]) -> ToolResult:
        return ToolResult(
            integration=call.integration,
            action=call.action,
            effect=self.effects[call.action],
            resource_id=resource_id,
            resource_type=resource_type,
            output={"mode": self.mode.value, **output},
        )

    def _dry_run(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        effect = self.effects[call.action]
        resource_id = (
            synthetic_id(f"dryrun-{call.integration.value}", call.idempotency_key)
            if effect is not Effect.READ_ONLY else None
        )
        output: dict[str, Any] = {"would_call": request.operation, "sent": False}
        if request.destination is not None:
            output["destination"] = request.destination.alias
        if "title" in call.arguments:
            output["title"] = call.arguments["title"]
        return self._result(call, resource_id, self._resource_type(call.action), output)

    # ----- integration specific -----

    def _policy(self, action: Action) -> DestinationPolicy | None:
        raise NotImplementedError

    def _resource_type(self, action: Action) -> str:
        raise NotImplementedError

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        raise NotImplementedError

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        raise NotImplementedError

    def _undo(self, call: CompensationCall) -> None:
        raise NotImplementedError


def _payload_dict(result: McpToolResult) -> dict[str, Any]:
    payload = result.json_payload()
    return payload if isinstance(payload, dict) else {}


def _created_id(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and 0 < len(value) <= 200:
            return value
    return None


def _safe_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") and len(value) <= 500 else None


# ---------------- Notion (MCP) ----------------


def _notion_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[:NOTION_TEXT_CHUNK]}}]


def _notion_paragraphs(text: str) -> list[dict[str, Any]]:
    chunks = [text[i:i + NOTION_TEXT_CHUNK] for i in range(0, len(text), NOTION_TEXT_CHUNK)][:NOTION_MAX_BLOCKS]
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _notion_text(c)}} for c in chunks]


class NotionMcpAdapter(_IntegrationAdapter):
    integration = Integration.NOTION
    effects = {Action.CREATE_PAGE: Effect.REVERSIBLE, Action.CREATE_TASK: Effect.REVERSIBLE}

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger, *, tools: NotionToolNames,
                 pages: DestinationConfig, task_database: DestinationConfig, client: McpClient | None) -> None:
        super().__init__(mode, ledger)
        if mode is IntegrationMode.REAL and client is None:
            raise ValueError("Real Notion adapter needs an MCP client.")
        self.tools = tools
        self.client = client
        self._pages = DestinationPolicy(pages, argument="parent", label="Notion parent")
        self._tasks = DestinationPolicy(task_database, argument=None, label="Notion task database")

    def _policy(self, action: Action) -> DestinationPolicy | None:
        return self._pages if action is Action.CREATE_PAGE else self._tasks

    def _resource_type(self, action: Action) -> str:
        return "notion_page" if action is Action.CREATE_PAGE else "notion_task"

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        args = call.arguments
        if call.action is Action.CREATE_PAGE:
            dest = self._pages.resolve(args)
            tool_args = {
                "parent": {"page_id": dest.target},
                "properties": {"title": {"title": _notion_text(args["title"])}},
                "children": _notion_paragraphs(args["content"]),
            }
        else:
            dest = self._tasks.resolve(args)
            # Database property names vary per workspace; 5.1 writes the title property only
            # and records assignee / due date in the page body.
            details = "\n".join(f"{label}: {args[key]}" for key, label in (("assignee", "Assignee"),
                                                                             ("due_date", "Due")) if args.get(key))
            tool_args = {
                "parent": {"database_id": dest.target},
                "properties": {"Name": {"title": _notion_text(args["title"])}},
                **({"children": _notion_paragraphs(details)} if details else {}),
            }
        return PreparedRequest(self.tools.create_page, dest, tool_args)

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.client is not None and request.destination is not None
        payload = _payload_dict(self.client.call_tool(request.operation, request.arguments))
        output = {"destination": request.destination.alias, "title": call.arguments["title"]}
        if url := _safe_url(payload.get("url")):
            output["url"] = url
        return self._result(call, _created_id(payload, "id"), self._resource_type(call.action), output)

    def _undo(self, call: CompensationCall) -> None:
        assert self.client is not None
        self.client.call_tool(self.tools.archive_page, {"page_id": call.resource_id, "archived": True})

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


# ---------------- Google Drive (MCP) ----------------


class GoogleDriveMcpAdapter(_IntegrationAdapter):
    integration = Integration.GOOGLE_DRIVE
    effects = {Action.CREATE_DOCUMENT: Effect.REVERSIBLE, Action.READ_DOCUMENT: Effect.READ_ONLY}

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger, *, tools: DriveToolNames,
                 folders: DestinationConfig, client: McpClient | None) -> None:
        super().__init__(mode, ledger)
        if mode is IntegrationMode.REAL and client is None:
            raise ValueError("Real Google Drive adapter needs an MCP client.")
        self.tools = tools
        self.client = client
        self._folders = DestinationPolicy(folders, argument="folder", label="Google Drive folder")

    def _policy(self, action: Action) -> DestinationPolicy | None:
        return self._folders if action is Action.CREATE_DOCUMENT else None

    def preflight(self, action: Action, arguments: dict[str, str]) -> list[tuple[str, str]]:
        if action is Action.READ_DOCUMENT and not self._folders.all_targets():
            return [("DESTINATION_NOT_APPROVED", "No approved Google Drive folders are configured for reads.")]
        return super().preflight(action, arguments)

    def _resource_type(self, action: Action) -> str:
        return "drive_document"

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        args = call.arguments
        if call.action is Action.CREATE_DOCUMENT:
            dest = self._folders.resolve(args)
            return PreparedRequest(self.tools.create_document, dest, {
                "title": args["title"], "content": args["content"], "parent_folder_id": dest.target,
            })
        folders = self._folders.all_targets()
        if not folders:
            raise ToolError(ErrorKind.PERMISSION, "DESTINATION_NOT_APPROVED",
                            "No approved Google Drive folders are configured for reads.")
        # Reads are confined to approved folders: the search is scoped to them, and every
        # match is re-checked against them before it is read.
        return PreparedRequest(self.tools.search_documents, None,
                               {"name": args["document_name"], "folder_ids": folders})

    def _dry_run(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        result = super()._dry_run(call, request)
        if call.action is Action.READ_DOCUMENT:
            result.output.update(document_name=call.arguments["document_name"],
                                 would_call=[self.tools.search_documents, self.tools.read_document])
        return result

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.client is not None
        if call.action is Action.CREATE_DOCUMENT:
            assert request.destination is not None
            payload = _payload_dict(self.client.call_tool(request.operation, request.arguments))
            output = {"destination": request.destination.alias, "title": call.arguments["title"]}
            if url := _safe_url(payload.get("url") or payload.get("webViewLink")):
                output["url"] = url
            return self._result(call, _created_id(payload, "id", "documentId", "fileId"), "drive_document", output)
        return self._read(call, request)

    def _read(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.client is not None
        name = call.arguments["document_name"]
        approved = set(request.arguments["folder_ids"])
        payload = self.client.call_tool(request.operation, request.arguments).json_payload()
        files = payload.get("files") if isinstance(payload, dict) else payload
        if not isinstance(files, list):
            raise ToolError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Unexpected search result.")
        matches = [
            f for f in files[:MAX_SEARCH_RESULTS]
            if isinstance(f, dict) and f.get("name") == name and isinstance(f.get("id"), str)
            and set(f.get("parents") or []) & approved  # must prove it lives in an approved folder
        ]
        if not matches:
            raise ToolError(ErrorKind.PERMANENT, "DOCUMENT_NOT_FOUND", "No document with that name in approved folders.")
        if len(matches) > 1:
            raise ToolError(ErrorKind.PERMANENT, "DOCUMENT_AMBIGUOUS", "Several documents have that name.")
        document_id = matches[0]["id"]
        content = self.client.call_tool(self.tools.read_document, {"document_id": document_id}).text()
        # Contents are business data: report size only, never the text itself.
        return self._result(call, document_id, "drive_document",
                            {"document_name": name, "content_length": len(content)})

    def _undo(self, call: CompensationCall) -> None:
        assert self.client is not None
        self.client.call_tool(self.tools.trash_file, {"file_id": call.resource_id})

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


# ---------------- Microsoft Teams (incoming webhook) ----------------


def teams_card(message: str) -> dict[str, Any]:
    """Adaptive Card message accepted by Teams Workflows webhooks."""
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{"type": "TextBlock", "text": message, "wrap": True}],
            },
        }],
    }


class TeamsWebhookAdapter(_IntegrationAdapter):
    integration = Integration.TEAMS
    effects = {Action.SEND_MESSAGE: Effect.IRREVERSIBLE}

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger, *, channels: DestinationConfig,
                 sender: WebhookSender | None) -> None:
        super().__init__(mode, ledger)
        if mode is IntegrationMode.REAL and sender is None:
            raise ValueError("Real Teams adapter needs a webhook sender.")
        self.sender = sender
        self._channels = DestinationPolicy(channels, argument="channel", label="Teams channel")

    def _policy(self, action: Action) -> DestinationPolicy | None:
        return self._channels

    def preflight(self, action: Action, arguments: dict[str, str]) -> list[tuple[str, str]]:
        issues = super().preflight(action, arguments)
        if len(arguments.get("message", "")) > TEAMS_MAX_MESSAGE_CHARS:
            issues.append(("MESSAGE_TOO_LONG", f"Teams messages are limited to {TEAMS_MAX_MESSAGE_CHARS} characters."))
        return issues

    def _resource_type(self, action: Action) -> str:
        return "teams_message"

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        message = call.arguments["message"]
        if len(message) > TEAMS_MAX_MESSAGE_CHARS:
            raise ToolError(ErrorKind.PERMANENT, "MESSAGE_TOO_LONG",
                            f"Teams messages are limited to {TEAMS_MAX_MESSAGE_CHARS} characters.")
        return PreparedRequest("teams_webhook", self._channels.resolve(call.arguments), teams_card(message))

    def _dry_run(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        result = super()._dry_run(call, request)
        result.output.update(channel=result.output.pop("destination"), delivered=False)
        return result

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.sender is not None and request.destination is not None
        self.sender.post_json(request.destination.target, request.arguments)
        # Webhooks return no message id; derive a stable one from the idempotency key.
        return self._result(call, synthetic_id("teams-msg", call.idempotency_key), "teams_message",
                            {"channel": request.destination.alias, "delivered": True})

    def compensate(self, call: CompensationCall) -> bool:  # pragma: no cover - guarded by registry
        raise ToolError(ErrorKind.PERMANENT, "NOT_COMPENSATABLE", "Teams messages cannot be unsent")

    def close(self) -> None:
        if self.sender is not None:
            self.sender.close()


# ---------------- factory ----------------


def build_integration_registry(
    settings: Settings,
    *,
    faults: FailureInjector | None = None,
    config: IntegrationConfig | None = None,
    mcp_transports: dict[Integration, Any] | None = None,
    webhook_sender: WebhookSender | None = None,
) -> ToolRegistry:
    """Registry for DRY_RUN or REAL mode. Opens no connection (MCP clients connect lazily).

    ``mcp_transports`` maps an integration to an ``McpTransport`` factory and
    ``webhook_sender`` replaces the webhook client: tests use them to inject fakes.
    Raises IntegrationConfigError for invalid configuration."""
    mode = settings.integration_mode
    if mode is IntegrationMode.MOCK:
        raise ValueError("Use MockToolRegistry for mock mode.")
    config = config or load_integration_config(settings)
    ledger = IdempotencyLedger()
    transports = mcp_transports or {}
    adapters: list[Any] = []

    if mode is IntegrationMode.DRY_RUN:
        adapters = [
            NotionMcpAdapter(mode, ledger, tools=config.notion_tools, pages=config.notion_pages,
                             task_database=config.notion_task_database, client=None),
            GoogleDriveMcpAdapter(mode, ledger, tools=config.drive_tools, folders=config.drive_folders, client=None),
            TeamsWebhookAdapter(mode, ledger, channels=config.teams_channels, sender=None),
        ]
    else:
        # Only configured integrations get an adapter; plans using others are rejected by
        # prepare (UNSUPPORTED_BY_EXECUTOR) before anything runs.
        if config.notion_mcp is not None:
            client = McpClient(config.notion_mcp, transports.get(Integration.NOTION))
            adapters.append(NotionMcpAdapter(mode, ledger, tools=config.notion_tools, pages=config.notion_pages,
                                             task_database=config.notion_task_database, client=client))
        if config.drive_mcp is not None:
            client = McpClient(config.drive_mcp, transports.get(Integration.GOOGLE_DRIVE))
            adapters.append(GoogleDriveMcpAdapter(mode, ledger, tools=config.drive_tools,
                                                  folders=config.drive_folders, client=client))
        if config.teams_channels.targets:
            sender = webhook_sender or WebhookClient(connect_timeout=config.connect_timeout_seconds,
                                                     timeout=config.timeout_seconds)
            adapters.append(TeamsWebhookAdapter(mode, ledger, channels=config.teams_channels, sender=sender))

    return ToolRegistry(adapters, mode=mode, faults=faults, documents=None,
                        real_writes_enabled=settings.real_writes_enabled)
