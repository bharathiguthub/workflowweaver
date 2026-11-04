"""Fakes for Phase 5.1 integration tests. No test opens a real connection.

* ``FakeMcpServer`` implements the ``McpTransport`` protocol in memory: it speaks JSON-RPC
  (initialize, notifications/initialized, tools/list with pagination, tools/call) and
  simulates a Notion- or Drive-like server.
* ``RecordingWebhookSender`` replaces the Teams webhook client.
* ``integration_settings`` builds Settings with synthetic, obviously-fake secrets.
All values are synthetic.
"""

import json
import random
from typing import Any

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools.integrations import build_integration_registry
from app.core.config import Settings
from app.integrations.http.webhook import WebhookResponse
from app.integrations.mcp.transport import SessionExpired
from app.schemas.workflow import Integration

NOTION_TOKEN = "synthetic-notion-mcp-token-0001"
DRIVE_TOKEN = "synthetic-drive-mcp-token-0002"
OPERATOR_TOKEN = "synthetic-operator-token-0003"
WEBHOOK_URL = "https://synthetic.webhook.office.com/webhookb2/SYNTHETIC-SECRET-PATH-0004"
NOTION_PAGE_ID = "synthetic-notion-parent-0005"
NOTION_DB_ID = "synthetic-notion-db-0006"
DRIVE_FOLDER_ID = "synthetic-drive-folder-0007"
OTHER_FOLDER_ID = "synthetic-unapproved-folder-0008"
NOTION_MCP_URL = "https://synthetic-notion-mcp.example.invalid/mcp"
DRIVE_MCP_URL = "https://synthetic-drive-mcp.example.invalid/mcp"
DOCUMENT_TEXT = "SYNTHETIC-CONFIDENTIAL-DOCUMENT-TEXT"

SECRETS = [NOTION_TOKEN, DRIVE_TOKEN, OPERATOR_TOKEN, WEBHOOK_URL, NOTION_PAGE_ID, NOTION_DB_ID,
           DRIVE_FOLDER_ID, NOTION_MCP_URL, DRIVE_MCP_URL, DOCUMENT_TEXT, "SYNTHETIC-SECRET-PATH"]


def integration_settings(mode: str = "real", **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        _env_file=None,
        integration_mode=mode,
        real_writes_enabled=True,
        real_write_authorization_token=OPERATOR_TOKEN,
        notion_mcp_url=NOTION_MCP_URL,
        notion_mcp_auth_token=NOTION_TOKEN,
        notion_destinations=json.dumps({"engineering": NOTION_PAGE_ID, "tasks": NOTION_DB_ID}),
        notion_default_page_parent="engineering",
        notion_task_database="tasks",
        google_drive_mcp_url=DRIVE_MCP_URL,
        google_drive_mcp_auth_token=DRIVE_TOKEN,
        google_drive_destinations=json.dumps({"reports": DRIVE_FOLDER_ID}),
        google_drive_default_folder="reports",
        teams_webhook_urls=json.dumps({"synthetic-team": WEBHOOK_URL}),
        teams_default_channel="synthetic-team",
    )
    values.update(overrides)
    return Settings(**values)


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload) if not isinstance(payload, str) else payload}]}


class FakeMcpServer:
    """In-memory MCP server behind the McpTransport protocol."""

    def __init__(self, kind: str, *, protocol_version: str = "2025-06-18", page_size: int = 0,
                 extra_tools: tuple[str, ...] = ("dangerous_delete_everything",)) -> None:
        self.kind = kind
        self.protocol_version = protocol_version
        self.page_size = page_size
        if kind == "notion":
            names = ["API-post-page", "API-patch-page"]
        else:
            names = ["create_document", "search_documents", "read_document", "trash_file"]
        self.tool_names = [*names, *extra_tools]
        self.messages: list[dict[str, Any]] = []  # every request/notification received
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, list[Exception]] = {}  # tool name -> exceptions raised in order
        self.tool_errors: set[str] = set()  # tools that answer isError: true
        self.expire_session_once = False
        self.closed = 0
        self.resources: dict[str, dict[str, Any]] = {}
        self.drive_files = [
            {"id": "synthetic-doc-standup", "name": "Synthetic standup notes", "parents": [DRIVE_FOLDER_ID]},
            {"id": "synthetic-doc-outside", "name": "Outside notes", "parents": [OTHER_FOLDER_ID]},
        ]
        self._next = 0

    # ----- McpTransport -----

    def set_protocol_version(self, version: str) -> None:
        self.negotiated = version

    def reset_session(self) -> None:
        pass

    def notify(self, message: dict[str, Any], *, timeout: float) -> None:
        self.messages.append(message)

    def close(self) -> None:
        self.closed += 1

    def request(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        self.messages.append(message)
        method, params, rid = message["method"], message.get("params", {}), message["id"]
        if method == "tools/call" and self.expire_session_once:
            self.expire_session_once = False
            raise SessionExpired()
        if method == "initialize":
            return self._ok(rid, {"protocolVersion": self.protocol_version, "capabilities": {"tools": {}},
                                  "serverInfo": {"name": f"fake-{self.kind}", "version": "0"}})
        if method == "tools/list":
            return self._ok(rid, self._list(params.get("cursor")))
        if method == "tools/call":
            return self._call(rid, params["name"], params["arguments"])
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}

    # ----- behaviour -----

    @staticmethod
    def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _list(self, cursor: str | None) -> dict[str, Any]:
        tools = [{"name": n, "description": f"synthetic {n}", "inputSchema": {"type": "object"}}
                 for n in self.tool_names]
        if not self.page_size:
            return {"tools": tools}
        start = int(cursor or 0)
        page = tools[start:start + self.page_size]
        nxt = start + self.page_size
        return {"tools": page, **({"nextCursor": str(nxt)} if nxt < len(tools) else {})}

    def _call(self, rid: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.tool_calls.append((name, args))
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)
        if name in self.tool_errors:
            return self._ok(rid, {"isError": True, **_text(f"tool failed for {json.dumps(args)}")})
        self._next += 1
        if name == "API-post-page":
            pid = f"synthetic-page-{self._next}"
            self.resources[pid] = {"archived": False, "args": args}
            return self._ok(rid, _text({"object": "page", "id": pid, "url": f"https://www.notion.so/{pid}"}))
        if name == "API-patch-page":
            self.resources.setdefault(args["page_id"], {})["archived"] = args.get("archived")
            return self._ok(rid, _text({"object": "page", "id": args["page_id"], "archived": True}))
        if name == "create_document":
            did = f"synthetic-created-doc-{self._next}"
            self.resources[did] = {"trashed": False, "args": args}
            return self._ok(rid, {"content": [], "structuredContent": {
                "id": did, "webViewLink": f"https://docs.google.com/document/d/{did}"}})
        if name == "search_documents":
            files = [f for f in self.drive_files if f["name"] == args["name"]]
            return self._ok(rid, _text({"files": files}))
        if name == "read_document":
            return self._ok(rid, _text(DOCUMENT_TEXT))
        if name == "trash_file":
            self.resources.setdefault(args["file_id"], {})["trashed"] = True
            return self._ok(rid, _text({"ok": True}))
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "bad"}}

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [a for n, a in self.tool_calls if n == name]


class RecordingWebhookSender:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.failures: list[Exception] = []
        self.closed = False

    def post_json(self, url: str, payload: dict[str, Any]) -> WebhookResponse:
        self.posts.append((url, payload))
        if self.failures:
            raise self.failures.pop(0)
        return WebhookResponse(status_code=202)

    def close(self) -> None:
        self.closed = True


class FakeWorld:
    """Fake Notion MCP + Drive MCP + Teams webhook, plus a service wired to them."""

    def __init__(self, mode: str = "real", **overrides: Any) -> None:
        self.notion = FakeMcpServer("notion")
        self.drive = FakeMcpServer("drive")
        self.teams = RecordingWebhookSender()
        self.factories_called: list[str] = []
        self.settings = integration_settings(mode, **overrides)
        self.registry = build_integration_registry(
            self.settings,
            mcp_transports={
                Integration.NOTION: self._factory("notion", self.notion),
                Integration.GOOGLE_DRIVE: self._factory("drive", self.drive),
            },
            webhook_sender=self.teams,
        )
        self.sleeps: list[float] = []
        self.service = ExecutionService(
            self.registry,
            RetryPolicy(max_attempts=3, base_delay_seconds=0.2, max_delay_seconds=2.0, jitter=False),
            max_steps=10,
            sleeper=self.sleeps.append,
            rng=random.Random(7),
        )

    def _factory(self, name: str, server: FakeMcpServer):
        def build():
            self.factories_called.append(name)
            return server
        return build

    def remote_calls(self) -> int:
        return len(self.notion.tool_calls) + len(self.drive.tool_calls) + len(self.teams.posts)
