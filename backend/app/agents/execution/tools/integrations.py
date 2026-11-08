"""Real and dry-run adapters for Notion (MCP), Google Drive (MCP) and Teams (webhook).

They implement the same ``ToolAdapter`` contract as the mock adapters, so the execution
graph (retries, Saga compensation, SSE events, tracing) is unchanged. Each call goes:

    check arguments -> idempotency ledger -> write authorization -> resolve approved
    destination -> build the exact request -> send (real) | describe it (dry_run)

DRY_RUN runs every step except the send, so it validates configuration, destinations and
request shapes with no network access at all (no client objects are even created).

Contracts (Phase 5.2): tool names and argument shapes follow app.integrations.contracts -
Notion verified live against @notionhq/notion-mcp-server 2.6.1, Google Drive transcribed
from the official drivemcp.googleapis.com documentation (not live-verified), Teams from
the documented Workflows webhook payload.

Idempotency: results are recorded per idempotency key in an in-memory ledger, so a retry or
duplicate delivery inside this process replays the stored result instead of writing again.
A transient failure AFTER a write request was sent (timeout, 5xx) becomes the permanent
OUTCOME_UNKNOWN: retrying could duplicate the page/document/message. The ledger is
process-local and not durable (Stage 5.1 limitation).

Outputs returned to the API/SSE/traces contain ids, URLs of created resources, aliases and
counts only. Destination ids, webhook URLs, credentials and read document contents are
never included.
"""

import json
import threading
import time
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
from app.core.config import DriveTransport, IntegrationMode, Settings
from app.integrations.config import DestinationConfig, IntegrationConfig, load_integration_config
from app.integrations.contracts import (
    CONTRACTS,
    DRIVE_CREATE_FILE,
    DRIVE_READ_FILE_CONTENT,
    DRIVE_SEARCH_FILES,
    NOTION_CREATE_PAGE,
    NOTION_PATCH_PAGE,
    NOTION_RETRIEVE_PAGE,
)
from app.integrations.destinations import DestinationPolicy, ResolvedDestination
from app.integrations.errors import IntegrationError, unknown_outcome
from app.integrations.http.webhook import WebhookClient, WebhookSender
from app.integrations.mcp.client import McpClient, McpToolResult
from app.agents.validation import TEAMS_MAX_MESSAGE_CHARS
from app.observability import get_telemetry
from app.schemas.plan import Action
from app.schemas.workflow import Integration

NOTION_TEXT_CHUNK = 1900  # Notion rich text limit is 2000 characters per item
NOTION_MAX_BLOCKS = 100
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
    # True when a retried write is de-duplicated REMOTELY (the adapter finds the resource a
    # previous attempt created before writing again), so an ambiguous transient failure may be
    # retried instead of becoming OUTCOME_UNKNOWN. Google Drive REST sets it.
    remote_dedup: bool = False

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
                if exc.retryable and exc.request_sent and effect is not Effect.READ_ONLY and not self.remote_dedup:
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


def _created_id(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and 0 < len(value) <= 200:
            return value
    return None


def _safe_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") and len(value) <= 500 else None


def _restrict(effects: dict[Action, Effect], needs: dict[Action, set[str]], allowlist: frozenset[str]):
    """Only actions whose MCP tools are all allowlisted are supported."""
    return {action: effect for action, effect in effects.items() if needs[action] <= allowlist}


# ---------------- Notion (MCP): @notionhq/notion-mcp-server, verified live (2.6.1) ----------------

# Notion API error -> classification. The server returns API errors as a NORMAL tool result
# whose text is {"status": <http status>, "object": "error", "code": ..., "message": ...}
# (verified in the 2.6.1 source, proxy.ts) - not as isError and not as a JSON-RPC error.
_NOTION_ERRORS: dict[int, tuple[ErrorKind, bool]] = {  # status -> (kind, request may have applied)
    400: (ErrorKind.PERMANENT, False),
    401: (ErrorKind.PERMISSION, False),
    403: (ErrorKind.PERMISSION, False),
    404: (ErrorKind.PERMANENT, False),
    409: (ErrorKind.TRANSIENT, False),  # conflict_error: Notion asks the client to retry
    429: (ErrorKind.TRANSIENT, False),  # rate_limited
}


def _notion_payload(result: McpToolResult) -> dict[str, Any]:
    payload = result.json_payload()
    if not isinstance(payload, dict):
        raise IntegrationError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Unexpected Notion MCP result.",
                               request_sent=True)
    if payload.get("object") == "error" or payload.get("status") == "error":
        status = payload.get("status") if isinstance(payload.get("status"), int) else 0
        kind, applied = _NOTION_ERRORS.get(status, (ErrorKind.TRANSIENT, True) if status >= 500 or status == 0
                                           else (ErrorKind.PERMANENT, False))
        code = payload.get("code") if isinstance(payload.get("code"), str) else "error"
        # Notion's error code is a fixed identifier (e.g. validation_error); its message is not copied.
        raise IntegrationError(kind, "NOTION_API_ERROR",
                               f"Notion rejected the request (HTTP {status or 'unknown'}, {code[:40]}).",
                               request_sent=applied)
    return payload


def _notion_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[:NOTION_TEXT_CHUNK]}}]


def _notion_paragraphs(text: str) -> list[dict[str, Any]]:
    """Content -> blocks, using ONLY the two block types in the verified API-post-page schema
    (paragraphBlockRequest, bulletedListItemBlockRequest; additionalProperties: false).
    A line starting with "- " becomes a bulleted list item; other non-empty lines become
    paragraphs. Long lines are split into 1900-character chunks."""
    blocks: list[dict[str, Any]] = []
    for line in text.splitlines() or [text]:
        kind = "paragraph"
        if line.startswith("- "):
            kind, line = "bulleted_list_item", line[2:]
        if not line.strip():
            continue
        for i in range(0, len(line), NOTION_TEXT_CHUNK):
            blocks.append({"type": kind, kind: {"rich_text": _notion_text(line[i:i + NOTION_TEXT_CHUNK])}})
    return blocks[:NOTION_MAX_BLOCKS]


SANDBOX_ONLY_MESSAGE = ("Notion writes are restricted to the sandbox parent page (SMOKE_TEST_NOTION_PARENT) "
                        "while NOTION_REAL_WRITES_SANDBOX_ONLY=true.")


def _parent_step(arguments: dict[str, str]) -> int | None:
    raw = arguments.get("parent_step", "").strip()
    return int(raw) if raw.isdigit() else None


class NotionMcpAdapter(_IntegrationAdapter):
    integration = Integration.NOTION
    effects = {Action.CREATE_PAGE: Effect.REVERSIBLE, Action.CREATE_TASK: Effect.REVERSIBLE}
    _needs = {Action.CREATE_PAGE: {NOTION_CREATE_PAGE.name}, Action.CREATE_TASK: {NOTION_CREATE_PAGE.name}}

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger, *, allowlist: frozenset[str],
                 pages: DestinationConfig, task_database: DestinationConfig, client: McpClient | None,
                 sandbox_parent: str | None = None, sandbox_only: bool = False) -> None:
        super().__init__(mode, ledger)
        # Sandbox-only: top-level pages only under the sandbox alias; child pages only inside
        # pages this run created (parent_step); no task-database writes.
        self.sandbox_only = sandbox_only
        self.sandbox_parent = sandbox_parent
        if mode is IntegrationMode.REAL and client is None:
            raise ValueError("Real Notion adapter needs an MCP client.")
        self.effects = _restrict(type(self).effects, self._needs, allowlist)
        # Without the archive tool, created pages cannot be rolled back.
        if NOTION_PATCH_PAGE.name not in allowlist:
            self.effects = {a: Effect.IRREVERSIBLE for a in self.effects}
        self.client = client
        # Phase 7.2 read-back verification needs the (read-only) retrieve tool in the allowlist.
        self._can_verify = NOTION_RETRIEVE_PAGE.name in allowlist
        self._pages = DestinationPolicy(pages, argument="parent", label="Notion parent")
        self._tasks = DestinationPolicy(task_database, argument=None, label="Notion task database")

    def _policy(self, action: Action) -> DestinationPolicy | None:
        return self._pages if action is Action.CREATE_PAGE else self._tasks

    def _sandbox_problem(self, action: Action, arguments: dict[str, str]) -> str | None:
        if not self.sandbox_only or (action is Action.CREATE_PAGE and _parent_step(arguments) is not None):
            return None
        if action is not Action.CREATE_PAGE or not self.sandbox_parent:
            return SANDBOX_ONLY_MESSAGE
        return None if self._pages.requested_alias(arguments) == self.sandbox_parent else SANDBOX_ONLY_MESSAGE

    def preflight(self, action: Action, arguments: dict[str, str]) -> list[tuple[str, str]]:
        sandbox = self._sandbox_problem(action, arguments)
        if sandbox:
            return [("DESTINATION_NOT_APPROVED", sandbox)]
        if action is Action.CREATE_PAGE and _parent_step(arguments) is not None:
            return []  # the parent is a page this run creates (checked by the validator)
        return super().preflight(action, arguments)

    def _resource_type(self, action: Action) -> str:
        return "notion_page" if action is Action.CREATE_PAGE else "notion_task"

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        args = call.arguments
        sandbox = self._sandbox_problem(call.action, args)
        if sandbox:  # defence in depth: preflight already rejected the plan
            raise ToolError(ErrorKind.PERMISSION, "DESTINATION_NOT_APPROVED", sandbox)
        parent_step = _parent_step(args) if call.action is Action.CREATE_PAGE else None
        if parent_step is not None:
            target = call.upstream.get(parent_step)
            if not target:
                raise ToolError(ErrorKind.PERMANENT, "PARENT_NOT_AVAILABLE",
                                f"Step {parent_step} did not create a page in this run.")
            dest = ResolvedDestination(alias=f"page from step {parent_step}", target=target)
            tool_args = {
                "parent": {"page_id": dest.target},
                "properties": {"title": {"title": _notion_text(args["title"])}},
                "children": _notion_paragraphs(args["content"]),
            }
        elif call.action is Action.CREATE_PAGE:
            dest = self._pages.resolve(args)
            tool_args = {
                "parent": {"page_id": dest.target},
                "properties": {"title": {"title": _notion_text(args["title"])}},
                "children": _notion_paragraphs(args["content"]),
            }
        else:
            dest = self._tasks.resolve(args)
            # The title property of a database is named per workspace ("Name" by default);
            # 5.2 writes "Name" only and records assignee / due date in the page body.
            details = "\n".join(f"{label}: {args[key]}" for key, label in (("assignee", "Assignee"),
                                                                             ("due_date", "Due")) if args.get(key))
            tool_args = {
                "parent": {"type": "database_id", "database_id": dest.target},
                "properties": {"Name": {"title": _notion_text(args["title"])}},
                **({"children": _notion_paragraphs(details)} if details else {}),
            }
        return PreparedRequest(NOTION_CREATE_PAGE.name, dest, tool_args)

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.client is not None and request.destination is not None
        payload = _notion_payload(self.client.call_tool(request.operation, request.arguments))
        output = {"destination": request.destination.alias, "title": call.arguments["title"]}
        if url := _safe_url(payload.get("url")):
            output["url"] = url
        resource_id = _created_id(payload, "id") if payload.get("object") == "page" else None
        return self._result(call, resource_id, self._resource_type(call.action), output)

    def verify(self, call: ToolCall, result: ToolResult) -> str:
        """Read a created page back (API-retrieve-a-page, read-only): it must be a live page with
        the id we were given, inside the expected parent. Returns verified | failed |
        unavailable (retrieve tool not allowlisted) | not_performed (dry run)."""
        if self.mode is IntegrationMode.DRY_RUN:
            return "not_performed"
        if not self._can_verify or call.action is not Action.CREATE_PAGE or not result.resource_id:
            return "unavailable"
        assert self.client is not None
        payload = _notion_payload(self.client.call_tool(NOTION_RETRIEVE_PAGE.name, {"page_id": result.resource_id}))
        parent_step = _parent_step(call.arguments)
        expected_parent = (call.upstream.get(parent_step) if parent_step is not None
                           else self._pages.resolve(call.arguments).target)
        norm = lambda v: str(v or "").replace("-", "").lower()  # noqa: E731
        parent = payload.get("parent") if isinstance(payload.get("parent"), dict) else {}
        ok = (payload.get("object") == "page" and norm(payload.get("id")) == norm(result.resource_id)
              and not payload.get("in_trash") and not payload.get("archived")
              and norm(parent.get("page_id")) == norm(expected_parent))
        return "verified" if ok else "failed"

    def _undo(self, call: CompensationCall) -> None:
        assert self.client is not None
        # in_trash replaces the deprecated "archived" flag (Notion API 2025-09-03, the
        # server's default Notion-Version). Trashing an already-trashed page succeeds.
        _notion_payload(self.client.call_tool(NOTION_PATCH_PAGE.name,
                                              {"page_id": call.resource_id, "in_trash": True}))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


# ---------------- Google Drive: official drivemcp.googleapis.com (documented contract) ----------------

_DRIVE_QUOTE = str.maketrans({"\\": "\\\\", "'": "\\'"})


def drive_query_literal(value: str) -> str:
    """Single-quoted string literal for search_files queries ("escape embedded quotes as \\'")."""
    if any(ord(ch) < 32 for ch in value):
        raise ToolError(ErrorKind.PERMANENT, "INVALID_ARGUMENTS", "Document names cannot contain control characters.")
    return "'" + value.translate(_DRIVE_QUOTE) + "'"


def drive_search_query(name: str, folder_ids: list[str]) -> str:
    folders = " or ".join(f"parentId = {drive_query_literal(f)}" for f in folder_ids)
    return f"title = {drive_query_literal(name)} and ({folders})"


DRIVE_FILE_ID_UNSUPPORTED = ("Reading by file_id needs the Google Drive REST integration "
                             "(GOOGLE_DRIVE_TRANSPORT=rest); the MCP transport can only search by name.")


class GoogleDriveMcpAdapter(_IntegrationAdapter):
    integration = Integration.GOOGLE_DRIVE
    # The official server has no delete/trash/update tool, so a created document can NOT be
    # rolled back: create_document is IRREVERSIBLE here (it is REVERSIBLE in mock mode).
    effects = {Action.CREATE_DOCUMENT: Effect.IRREVERSIBLE, Action.READ_DOCUMENT: Effect.READ_ONLY}
    _needs = {Action.CREATE_DOCUMENT: {DRIVE_CREATE_FILE.name},
              Action.READ_DOCUMENT: {DRIVE_SEARCH_FILES.name, DRIVE_READ_FILE_CONTENT.name}}

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger, *, allowlist: frozenset[str],
                 folders: DestinationConfig, client: McpClient | None) -> None:
        super().__init__(mode, ledger)
        if mode is IntegrationMode.REAL and client is None:
            raise ValueError("Real Google Drive adapter needs an MCP client.")
        self.effects = _restrict(type(self).effects, self._needs, allowlist)
        self.client = client
        self._folders = DestinationPolicy(folders, argument="folder", label="Google Drive folder")

    def _policy(self, action: Action) -> DestinationPolicy | None:
        return self._folders if action is Action.CREATE_DOCUMENT else None

    def preflight(self, action: Action, arguments: dict[str, str]) -> list[tuple[str, str]]:
        if arguments.get("file_id"):
            return [("UNSUPPORTED_ARGUMENT", DRIVE_FILE_ID_UNSUPPORTED)]
        if arguments.get("content_sha256"):
            # The Drive MCP server cannot read the document back byte-for-byte, so it cannot verify it.
            return [("UNSUPPORTED_ARGUMENT", "Read-back verification (content_sha256) needs the Google Drive REST "
                                             "integration (GOOGLE_DRIVE_TRANSPORT=rest).")]
        if action is Action.READ_DOCUMENT and not self._folders.all_targets():
            return [("DESTINATION_NOT_APPROVED", "No approved Google Drive folders are configured for reads.")]
        return super().preflight(action, arguments)

    def _resource_type(self, action: Action) -> str:
        return "drive_document"

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        args = call.arguments
        if call.action is Action.CREATE_DOCUMENT:
            dest = self._folders.resolve(args)
            # text/plain content is converted to a Google Doc by default (documented behaviour).
            return PreparedRequest(DRIVE_CREATE_FILE.name, dest, {
                "title": args["title"], "textContent": args["content"],
                "contentMimeType": "text/plain", "parentId": dest.target,
            })
        folders = self._folders.all_targets()
        if not folders:
            raise ToolError(ErrorKind.PERMISSION, "DESTINATION_NOT_APPROVED",
                            "No approved Google Drive folders are configured for reads.")
        # Reads are confined to approved folders: the query is scoped to them, and every
        # match is re-checked (exact title, parentId in the approved set) before it is read.
        return PreparedRequest(DRIVE_SEARCH_FILES.name, None, {
            "query": drive_search_query(args["document_name"], folders),
            "pageSize": MAX_SEARCH_RESULTS,
            "excludeContentSnippets": True,  # never pull content into search results
        })

    def _dry_run(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        result = super()._dry_run(call, request)
        if call.action is Action.READ_DOCUMENT:
            result.output.update(document_name=call.arguments["document_name"],
                                 would_call=[DRIVE_SEARCH_FILES.name, DRIVE_READ_FILE_CONTENT.name])
        return result

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.client is not None
        if call.action is Action.CREATE_DOCUMENT:
            assert request.destination is not None
            payload = self.client.call_tool(request.operation, request.arguments).json_payload()
            payload = payload if isinstance(payload, dict) else {}
            output = {"destination": request.destination.alias, "title": call.arguments["title"]}
            if url := _safe_url(payload.get("viewUrl")):
                output["url"] = url
            return self._result(call, _created_id(payload, "id"), "drive_document", output)
        return self._read(call, request)

    def _read(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.client is not None
        name = call.arguments["document_name"]
        approved = set(self._folders.all_targets())
        payload = self.client.call_tool(request.operation, request.arguments).json_payload()
        files = payload.get("files", []) if isinstance(payload, dict) else None  # empty response = no files
        if not isinstance(files, list):
            raise ToolError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Unexpected search result.")
        matches = [
            f for f in files[:MAX_SEARCH_RESULTS]
            if isinstance(f, dict) and f.get("title") == name and isinstance(f.get("id"), str)
            and f.get("parentId") in approved  # must prove it lives in an approved folder
        ]
        if not matches:
            raise ToolError(ErrorKind.PERMANENT, "DOCUMENT_NOT_FOUND", "No document with that name in approved folders.")
        if len(matches) > 1:
            raise ToolError(ErrorKind.PERMANENT, "DOCUMENT_AMBIGUOUS", "Several documents have that name.")
        document_id = matches[0]["id"]
        content = self.client.call_tool(DRIVE_READ_FILE_CONTENT.name, {"fileId": document_id}).json_payload()
        text = content.get("fileContent") if isinstance(content, dict) else None
        if not isinstance(text, str):
            raise ToolError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Unexpected read_file_content result.")
        # Contents are business data: report size only, never the text itself.
        return self._result(call, document_id, "drive_document",
                            {"document_name": name, "content_length": len(text)})

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


# ---------------- Microsoft Teams: Workflows (Power Automate) incoming webhook ----------------


_NOTION_PAGE_ID = __import__("re").compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")


def notion_page_url(page_id: str) -> str | None:
    """Public link of a Notion page from its id (notion.so/<32 hex> opens the page for anyone
    with access). None for anything that is not a Notion page id (mock/dry-run references)."""
    if not _NOTION_PAGE_ID.fullmatch(page_id or ""):
        return None
    return f"https://www.notion.so/{page_id.replace('-', '').lower()}"


def teams_card(message: str, link: str | None = None) -> dict[str, Any]:
    """Adaptive Card message in the documented Workflows webhook shape (Microsoft Learn,
    'Microsoft Teams Webhook' trigger): contentUrl must be null; card version 1.2 as in
    the official example. ``link`` (Phase 7.2) is appended as a second TextBlock."""
    body: list[dict[str, Any]] = [{"type": "TextBlock", "text": message, "wrap": True}]
    if link:
        body.append({"type": "TextBlock", "text": f"Notion project page: [{link}]({link})", "wrap": True})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.2",
                "body": body,
            },
        }],
    }


TEAMS_SANDBOX_ONLY_MESSAGE = ("Teams messages are restricted to the sandbox channel (SMOKE_TEST_TEAMS_CHANNEL) "
                              "while TEAMS_REAL_WRITES_SANDBOX_ONLY=true.")


class TeamsWebhookAdapter(_IntegrationAdapter):
    """Microsoft Teams via a Workflows ("When a Teams webhook request is received") webhook.

    Delivery semantics (documented limits, no stronger claim): a 2xx means the webhook ACCEPTED
    the request; Microsoft does not document whether the message was posted, and webhooks return
    no message id and offer no deduplication, edit or delete. So: send_message is IRREVERSIBLE
    (never compensated), retries happen only when the request was provably NOT sent (connect
    error, HTTP 429), an ambiguous failure (timeout, 5xx) becomes OUTCOME_UNKNOWN instead of a
    retry, and the local idempotency ledger replays a stored result for the same key. That is
    at-most-once per idempotency key within this process, not exactly-once delivery.
    """

    integration = Integration.TEAMS
    effects = {Action.SEND_MESSAGE: Effect.IRREVERSIBLE}

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger, *, channels: DestinationConfig,
                 sender: WebhookSender | None, sandbox_channel: str | None = None,
                 sandbox_only: bool = False) -> None:
        super().__init__(mode, ledger)
        if mode is IntegrationMode.REAL and sender is None:
            raise ValueError("Real Teams adapter needs a webhook sender.")
        self.sender = sender
        self.sandbox_channel = sandbox_channel
        self.sandbox_only = sandbox_only
        self._channels = DestinationPolicy(channels, argument="channel", label="Teams channel")

    def _policy(self, action: Action) -> DestinationPolicy | None:
        return self._channels

    def _sandbox_problem(self, arguments: dict[str, str]) -> str | None:
        if not self.sandbox_only:
            return None
        alias = self._channels.requested_alias(arguments)
        return None if self.sandbox_channel and alias == self.sandbox_channel else TEAMS_SANDBOX_ONLY_MESSAGE

    def preflight(self, action: Action, arguments: dict[str, str]) -> list[tuple[str, str]]:
        sandbox = self._sandbox_problem(arguments)
        if sandbox:
            return [("DESTINATION_NOT_APPROVED", sandbox)]
        issues = super().preflight(action, arguments)
        if len(arguments.get("message", "")) > TEAMS_MAX_MESSAGE_CHARS:
            issues.append(("MESSAGE_TOO_LONG", f"Teams messages are limited to {TEAMS_MAX_MESSAGE_CHARS} characters."))
        return issues

    def _resource_type(self, action: Action) -> str:
        return "teams_message"

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        sandbox = self._sandbox_problem(call.arguments)
        if sandbox:  # defence in depth: preflight already rejected the plan
            raise ToolError(ErrorKind.PERMISSION, "DESTINATION_NOT_APPROVED", sandbox)
        message = call.arguments["message"]
        if len(message) > TEAMS_MAX_MESSAGE_CHARS:
            raise ToolError(ErrorKind.PERMANENT, "MESSAGE_TOO_LONG",
                            f"Teams messages are limited to {TEAMS_MAX_MESSAGE_CHARS} characters.")
        return PreparedRequest("teams_webhook", self._channels.resolve(call.arguments),
                               teams_card(message, teams_link(call)))

    def _dry_run(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        result = super()._dry_run(call, request)
        result.output.update(channel=result.output.pop("destination"), delivered=False,
                             payload_format="adaptive_card_1.2", payload_bytes=_payload_bytes(request.arguments),
                             message_chars=len(call.arguments["message"]))
        if call.arguments.get("link_step"):
            # Dry-run page references are not real pages: the link is added in a real run.
            result.output["notion_link"] = "added at run time from the created project page"
        return result

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.sender is not None and request.destination is not None
        alias = request.destination.alias
        # Langfuse: alias, sizes, HTTP status, outcome code and latency only. Never the webhook
        # URL (the target), the message text or the response body.
        with get_telemetry().observe(
            "teams.webhook.post", as_type="span", workflow_id=call.workflow_id, step_number=call.step_number,
            input={"destination_alias": alias, "payload_bytes": _payload_bytes(request.arguments),
                   "message_chars": len(call.arguments["message"]), "attempt": call.attempt},
        ) as observation:
            started = time.perf_counter()
            try:
                response = self.sender.post_json(request.destination.target, request.arguments)
            except IntegrationError as exc:
                observation.update(
                    output={"error_code": exc.code, "request_sent": exc.request_sent, "retryable": exc.retryable,
                            "latency_ms": round((time.perf_counter() - started) * 1000, 1)},
                    level="WARNING" if exc.retryable else "ERROR", status_message=exc.code)
                raise
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            observation.update(output={"http_status": response.status_code, "accepted": True,
                                       "latency_ms": latency_ms})
        # Microsoft does not document whether a 2xx means "posted" or only "workflow
        # triggered", so the result says "accepted", not "delivered". Webhooks return no
        # message id; a stable reference is derived from the idempotency key.
        output = {"channel": alias, "accepted": True, "delivery": "accepted_by_webhook",
                  "http_status": response.status_code, "latency_ms": latency_ms}
        if call.arguments.get("link_step"):
            output["notion_link_included"] = len(request.arguments["attachments"][0]["content"]["body"]) > 1
        return self._result(call, synthetic_id("teams-msg", call.idempotency_key), "teams_message", output)

    def compensate(self, call: CompensationCall) -> bool:  # pragma: no cover - guarded by registry
        raise ToolError(ErrorKind.PERMANENT, "NOT_COMPENSATABLE", "Teams messages cannot be unsent")

    def close(self) -> None:
        if self.sender is not None:
            self.sender.close()


def teams_link(call: ToolCall) -> str | None:
    """The Notion page link for ``link_step``: taken ONLY from this run's own result for that
    step (call.upstream, set by the executor), never from the plan or the client."""
    raw = call.arguments.get("link_step", "").strip()
    if not raw:
        return None
    page_id = call.upstream.get(int(raw)) if raw.isdigit() else None
    if not page_id:
        raise ToolError(ErrorKind.PERMANENT, "LINK_NOT_AVAILABLE", f"Step {raw} did not create a page in this run.")
    return notion_page_url(page_id)


def _payload_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload).encode("utf-8"))


# ---------------- factory ----------------


def build_integration_registry(
    settings: Settings,
    *,
    faults: FailureInjector | None = None,
    config: IntegrationConfig | None = None,
    mcp_transports: dict[Integration, Any] | None = None,
    webhook_sender: WebhookSender | None = None,
    drive_http: Any = None,
) -> ToolRegistry:
    """Registry for DRY_RUN or REAL mode. Opens no connection (MCP clients connect lazily).

    ``mcp_transports`` maps an integration to an ``McpTransport`` factory and
    ``webhook_sender`` replaces the webhook client and ``drive_http`` the httpx.Client used for
    Google OAuth + Drive REST calls: tests use them to inject fakes.
    Raises IntegrationConfigError for invalid configuration."""
    mode = settings.integration_mode
    if mode is IntegrationMode.MOCK:
        raise ValueError("Use MockToolRegistry for mock mode.")
    config = config or load_integration_config(settings)
    ledger = IdempotencyLedger()
    transports = mcp_transports or {}
    sandbox = {"sandbox_parent": settings.smoke_test_notion_parent,
               "sandbox_only": settings.notion_real_writes_sandbox_only}
    teams_sandbox = {"sandbox_channel": settings.smoke_test_teams_channel,
                     "sandbox_only": settings.teams_real_writes_sandbox_only}
    adapters: list[Any] = []

    drive_rest = config.drive_transport is DriveTransport.REST
    if mode is IntegrationMode.DRY_RUN:
        adapters = [
            NotionMcpAdapter(mode, ledger, allowlist=config.notion_allowlist, pages=config.notion_pages,
                             task_database=config.notion_task_database, client=None, **sandbox),
            _drive_rest_adapter(settings, config, ledger, None) if drive_rest else
            GoogleDriveMcpAdapter(mode, ledger, allowlist=config.drive_allowlist, folders=config.drive_folders,
                                  client=None),
            TeamsWebhookAdapter(mode, ledger, channels=config.teams_channels, sender=None, **teams_sandbox),
        ]
    else:
        # Only configured integrations get an adapter; plans using others are rejected by
        # prepare (UNSUPPORTED_BY_EXECUTOR) before anything runs.
        if config.notion_mcp is not None:
            client = McpClient(config.notion_mcp, transports.get(Integration.NOTION),
                               contracts=CONTRACTS[Integration.NOTION])
            adapters.append(NotionMcpAdapter(mode, ledger, allowlist=config.notion_allowlist,
                                             pages=config.notion_pages,
                                             task_database=config.notion_task_database, client=client,
                                             **sandbox))
        if drive_rest:
            if config.drive_rest is not None:
                adapters.append(_drive_rest_adapter(settings, config, ledger,
                                                    _drive_client(config, drive_http)))
        elif config.drive_mcp is not None:
            client = McpClient(config.drive_mcp, transports.get(Integration.GOOGLE_DRIVE),
                               contracts=CONTRACTS[Integration.GOOGLE_DRIVE])
            adapters.append(GoogleDriveMcpAdapter(mode, ledger, allowlist=config.drive_allowlist,
                                                  folders=config.drive_folders, client=client))
        if config.teams_channels.targets:
            sender = webhook_sender or WebhookClient(connect_timeout=config.connect_timeout_seconds,
                                                     timeout=config.timeout_seconds)
            adapters.append(TeamsWebhookAdapter(mode, ledger, channels=config.teams_channels, sender=sender,
                                                **teams_sandbox))

    return ToolRegistry(adapters, mode=mode, faults=faults, documents=None,
                        real_writes_enabled=settings.real_writes_enabled)


def _drive_client(config: IntegrationConfig, http: Any = None):
    """Drive REST client. Opens no connection: the token file is read and the access token
    fetched lazily on the first request."""
    import httpx

    from app.integrations.google.drive import DriveClient
    from app.integrations.google.oauth import TokenProvider

    rest = config.drive_rest
    assert rest is not None
    http = http or httpx.Client(timeout=httpx.Timeout(rest.timeout_seconds, connect=rest.connect_timeout_seconds),
                                follow_redirects=False)
    tokens = TokenProvider(client_id=rest.client_id, client_secret=rest.client_secret,
                           token_path=rest.token_path, http=http)
    return DriveClient(tokens, http)


def _drive_rest_adapter(settings: Settings, config: IntegrationConfig, ledger: IdempotencyLedger, client):
    from app.agents.execution.tools.google_drive import GoogleDriveRestAdapter

    return GoogleDriveRestAdapter(settings.integration_mode, ledger, folders=config.drive_folders, client=client,
                                  sandbox_folder=settings.smoke_test_drive_folder,
                                  sandbox_only=settings.google_drive_real_writes_sandbox_only)
