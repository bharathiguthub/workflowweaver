"""Fakes for Phase 5 integration tests. No test opens a real connection.

* ``FakeMcpServer`` implements the ``McpTransport`` protocol in memory and advertises the
  tool schemas from the sanitized fixtures in ``tests/fixtures/mcp``:
    - notion: LIVE-captured tools/list of @notionhq/notion-mcp-server 2.6.1; tool results
      and API errors follow that version's source (errors are normal text results whose
      JSON is {"status": <http status>, "object": "error", "code", "message"}).
    - drive: the DOCUMENTED schemas of the official drivemcp.googleapis.com server.
* ``RecordingWebhookSender`` replaces the Teams webhook client.
* ``integration_settings`` builds Settings with synthetic, obviously-fake secrets.
All values are synthetic.
"""

import copy
import json
import pathlib
import random
from typing import Any

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools.integrations import build_integration_registry
from app.core.config import Settings
from app.integrations.http.webhook import WebhookResponse
from app.integrations.mcp.transport import SessionExpired
from app.schemas.workflow import Integration

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
NOTION_FIXTURE = json.loads((FIXTURES / "mcp" / "notion_mcp_2.6.1_tools_list.json").read_text(encoding="utf-8"))
DRIVE_FIXTURE = json.loads((FIXTURES / "mcp" / "google_drive_mcp_documented_tools.json").read_text(encoding="utf-8"))
TEAMS_FIXTURE = json.loads((FIXTURES / "teams_workflows_webhook.json").read_text(encoding="utf-8"))

NOTION_TOKEN = "synthetic-notion-mcp-token-0001"
DRIVE_TOKEN = "synthetic-drive-oauth-access-token-0002"
OPERATOR_TOKEN = "synthetic-operator-token-0003"
WEBHOOK_SIG = "SYNTHETIC-SECRET-SIG-0004"
WEBHOOK_URL = ("https://synthetic-tenant.westus.logic.azure.com:443/workflows/synthetic/triggers/manual/paths/"
               f"invoke?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig={WEBHOOK_SIG}")
NOTION_PAGE_ID = "5e1f0000-0000-4000-8000-000000000005"
NOTION_DB_ID = "5e1f0000-0000-4000-8000-000000000006"
DRIVE_FOLDER_ID = "synthetic-drive-folder-0007"
OTHER_FOLDER_ID = "synthetic-unapproved-folder-0008"
NOTION_MCP_URL = "http://127.0.0.1:3999/mcp"
DRIVE_MCP_URL = "https://drivemcp.googleapis.com/mcp/v1"
DOCUMENT_TEXT = "SYNTHETIC-CONFIDENTIAL-DOCUMENT-TEXT"

SECRETS = [NOTION_TOKEN, DRIVE_TOKEN, OPERATOR_TOKEN, WEBHOOK_URL, WEBHOOK_SIG, NOTION_PAGE_ID, NOTION_DB_ID,
           DRIVE_FOLDER_ID, NOTION_MCP_URL, DOCUMENT_TEXT]


def integration_settings(mode: str = "real", **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        _env_file=None,
        integration_mode=mode,
        # These fixtures exercise arbitrary approved destinations; sandbox confinement has
        # its own tests (test_project_workflow.py).
        notion_real_writes_sandbox_only=False,
        teams_real_writes_sandbox_only=False,  # Teams sandbox confinement: tests/test_teams_workflow.py
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
    return {"content": [{"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload)}]}


def notion_error(status: int, code: str) -> dict[str, Any]:
    """Exactly what notion-mcp-server 2.6.1 returns for a Notion API error (proxy.ts):
    a normal tool result, text = JSON.stringify({status: 'error', ...notionErrorBody})."""
    body = {"object": "error", "status": status, "code": code,
            "message": "SYNTHETIC-REMOTE-ERROR-TEXT echoing an argument"}
    return _text({"status": "error", **body} | {"status": status})


class FakeMcpServer:
    """In-memory MCP server behind the McpTransport protocol, advertising fixture schemas."""

    def __init__(self, kind: str, *, protocol_version: str = "2025-06-18", page_size: int = 0,
                 drive_structured: bool = True) -> None:
        self.kind = kind
        self.protocol_version = protocol_version
        self.page_size = page_size
        self.drive_structured = drive_structured  # Drive docs do not say text vs structuredContent
        fixture = NOTION_FIXTURE if kind == "notion" else DRIVE_FIXTURE
        detailed = {t["name"]: copy.deepcopy(t) for t in fixture["tools"]}
        self.tools = [detailed.get(n, {"name": n, "inputSchema": {"type": "object"}}) for n in fixture["tool_names"]]
        self.messages: list[dict[str, Any]] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, list[Exception]] = {}  # tool name -> exceptions raised in order
        self.notion_errors: dict[str, list[tuple[int, str]]] = {}  # tool -> Notion API errors in order
        self.tool_errors: set[str] = set()  # tools that answer isError: true
        self.expire_session_once = False
        self.closed = 0
        self.resources: dict[str, dict[str, Any]] = {}
        self.drive_files = [
            {"id": "synthetic-doc-standup", "title": "Synthetic standup notes", "parentId": DRIVE_FOLDER_ID,
             "mimeType": "application/vnd.google-apps.document"},
            {"id": "synthetic-doc-outside", "title": "Outside notes", "parentId": OTHER_FOLDER_ID,
             "mimeType": "application/vnd.google-apps.document"},
        ]
        self._next = 0

    @property
    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.tools]

    def schema(self, name: str) -> dict[str, Any]:
        return next(t for t in self.tools if t["name"] == name)["inputSchema"]

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
            info = NOTION_FIXTURE["initialize"] if self.kind == "notion" else {
                "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-drive", "version": "0"}}
            return self._ok(rid, {**info, "protocolVersion": self.protocol_version})
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
        if not self.page_size:
            return {"tools": self.tools}
        start = int(cursor or 0)
        nxt = start + self.page_size
        return {"tools": self.tools[start:nxt], **({"nextCursor": str(nxt)} if nxt < len(self.tools) else {})}

    def _call(self, rid: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.tool_calls.append((name, args))
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)
        if self.notion_errors.get(name):
            return self._ok(rid, notion_error(*self.notion_errors[name].pop(0)))
        if name in self.tool_errors:
            return self._ok(rid, {"isError": True, **_text(f"tool failed for {json.dumps(args)}")})
        if name not in self.tool_names:  # notion-mcp-server: throw new Error(`Method ${name} not found`)
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"Method {name} not found"}}
        self._next += 1
        handler = getattr(self, "_" + name.replace("-", "_").lower(), None)
        if handler is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unsupported in fake"}}
        return self._ok(rid, handler(args))

    # Notion (Notion API page objects, JSON.stringify'd into one text item)
    def _page(self, pid: str) -> dict[str, Any]:
        """A Notion API page object (shape per the Notion API reference)."""
        stored = self.resources[pid]
        title_prop = next(iter(stored["args"].get("properties", {}).values()), {"title": []})
        parent = dict(stored["args"].get("parent", {}))
        parent.setdefault("type", "database_id" if "database_id" in parent else "page_id")
        return {"object": "page", "id": pid, "url": f"https://www.notion.so/{pid.replace('-', '')}",
                "in_trash": stored["in_trash"], "parent": parent,
                "properties": {"title": {"id": "title", "type": "title", "title": [
                    {"type": "text", "text": t["text"], "plain_text": t["text"]["content"]}
                    for t in title_prop["title"]]}}}

    def _api_post_page(self, args):
        pid = f"5e1f0000-0000-4000-9000-{self._next:012d}"
        self.resources[pid] = {"in_trash": False, "args": args}
        return _text(self._page(pid))

    def _api_patch_page(self, args):
        if args["page_id"] not in self.resources:
            return notion_error(404, "object_not_found")
        self.resources[args["page_id"]]["in_trash"] = bool(args.get("in_trash"))
        return _text(self._page(args["page_id"]))

    def _api_retrieve_a_page(self, args):
        page_id = args["page_id"]
        if page_id == NOTION_PAGE_ID and page_id not in self.resources:  # the shared sandbox parent page
            self.resources[page_id] = {"in_trash": False, "args": {"parent": {"type": "workspace", "workspace": True},
                                                                   "properties": {"title": {"title": [
                                                                       {"text": {"content": "Sandbox"}}]}}}}
        if page_id not in self.resources:
            return notion_error(404, "object_not_found")  # not shared with the integration
        return _text(self._page(page_id))

    # Google Drive (documented output shapes)
    def _drive(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.drive_structured:
            return {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload}
        return _text(payload)

    def _create_file(self, args):
        did = f"synthetic-created-doc-{self._next}"
        self.resources[did] = {"args": args}
        return self._drive({"id": did, "title": args.get("title"), "parentId": args.get("parentId"),
                            "mimeType": "application/vnd.google-apps.document",
                            "viewUrl": f"https://docs.google.com/document/d/{did}/edit"})

    def _search_files(self, args):
        self.last_query = args["query"]
        files = [f for f in self.drive_files if f"title = '{f['title']}'" in args["query"]
                 and f"parentId = '{f['parentId']}'" in args["query"]]
        # A loose server might also return near-misses; include the outside doc to prove filtering.
        files += [f for f in self.drive_files if f["parentId"] == OTHER_FOLDER_ID and f["title"] in args["query"]]
        return self._drive({"files": files} if files else {})

    def _get_file_metadata(self, args):
        if args["fileId"] == DRIVE_FOLDER_ID:
            return self._drive({"id": DRIVE_FOLDER_ID, "title": "Sandbox", "mimeType": "application/vnd.google-apps.folder"})
        created = self.resources.get(args["fileId"])
        if created is None:
            return {"isError": True, **_text("File not found")}
        a = created["args"]
        return self._drive({"id": args["fileId"], "title": a.get("title"), "parentId": a.get("parentId"),
                            "mimeType": "application/vnd.google-apps.document",
                            "viewUrl": f"https://docs.google.com/document/d/{args['fileId']}/edit"})

    def _read_file_content(self, args):
        created = self.resources.get(args["fileId"])
        text = created["args"].get("textContent", "") if created else DOCUMENT_TEXT
        return self._drive({"fileContent": text, "contentAnchoredComments": [],
                            "unanchoredComments": [], "textFormattingNotSupported": False,
                            "commentsNotSupported": False})

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [a for n, a in self.tool_calls if n == name]


class RecordingWebhookSender:
    def __init__(self, status_code: int = 202) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.failures: list[Exception] = []
        self.closed = False
        self.status_code = status_code

    def post_json(self, url: str, payload: dict[str, Any]) -> WebhookResponse:
        self.posts.append((url, payload))
        if self.failures:
            raise self.failures.pop(0)
        return WebhookResponse(status_code=self.status_code)

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
