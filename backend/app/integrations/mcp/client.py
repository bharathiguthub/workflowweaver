"""Reusable synchronous MCP client (JSON-RPC 2.0 over an ``McpTransport``).

Connection management
  * Lazy: nothing connects until the first ``list_tools``/``call_tool``. Building a client
    (e.g. at application start) never touches the network.
  * ``initialize`` handshake -> ``notifications/initialized`` -> ``tools/list`` discovery.
  * One session per client, shared by worker threads under a lock (calls are serialised).
  * An expired session (HTTP 404 on a session) is re-initialised once, transparently.
  * ``close()`` ends the session; the next call reconnects.

Strict allowlist
  * ``call_tool`` refuses any tool not in the server's configured allowlist BEFORE any
    network traffic (PERMISSION ``MCP_TOOL_NOT_ALLOWED``).
  * Discovery keeps only allowlisted tools; an allowlisted tool the server does not offer
    is refused (PERMANENT ``MCP_TOOL_UNAVAILABLE``).

Contract check (Phase 5.2)
  * When ``contracts`` are given, each discovered allowlisted tool's input schema is
    compared with what we send (``check_tool_schema``). A drifted tool is disabled: calls
    fail with PERMANENT ``MCP_CONTRACT_MISMATCH`` before any request is sent.

Results are typed (``McpToolResult``). Tool-level errors (``isError: true``) and JSON-RPC
errors become ``IntegrationError`` with fixed messages; remote error text is never copied
into messages, logs or traces because it can echo argument values.
"""

import itertools
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from app.agents.execution.tools.base import ErrorKind
from app.integrations.config import McpServerConfig
from app.integrations.contracts import ToolContract, check_tool_schema
from app.integrations.errors import IntegrationError
from app.integrations.mcp.transport import McpTransport, SessionExpired, StreamableHttpTransport

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-03-26"})
CLIENT_INFO = {"name": "workflowweaver", "version": "0.5.1"}
MAX_TOOL_PAGES = 50  # pages of tools/list followed before giving up

# JSON-RPC error codes
_INVALID_PARAMS = -32602
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


class McpTool(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class McpToolResult(BaseModel):
    tool: str
    content: list[dict[str, Any]] = Field(default_factory=list)
    structured: dict[str, Any] | None = None

    def text(self) -> str:
        return "\n".join(c.get("text", "") for c in self.content if c.get("type") == "text")

    def json_payload(self) -> Any:
        """structuredContent if present, else the first text item parsed as JSON, else None."""
        if self.structured is not None:
            return self.structured
        for item in self.content:
            if item.get("type") == "text":
                try:
                    return json.loads(item.get("text", ""))
                except (TypeError, json.JSONDecodeError):
                    return None
        return None


def _protocol_error(message: str) -> IntegrationError:
    return IntegrationError(ErrorKind.PERMANENT, "MCP_PROTOCOL_ERROR", message, request_sent=True)


class McpClient:
    def __init__(self, config: McpServerConfig, transport_factory: Callable[[], McpTransport] | None = None,
                 *, contracts: dict[str, ToolContract] | None = None) -> None:
        self.config = config
        self.contracts = contracts or {}
        self.contract_problems: dict[str, list[str]] = {}  # tool -> problems (names only)
        self.server = config.integration.value
        self._factory = transport_factory or (lambda: StreamableHttpTransport(
            config.url,
            auth_token=config.auth_token,
            connect_timeout=config.connect_timeout_seconds,
            timeout=config.timeout_seconds,
        ))
        self._transport: McpTransport | None = None
        self._tools: dict[str, McpTool] | None = None
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self.connections = 0  # number of successful handshakes (observability/tests)

    def __repr__(self) -> str:
        return f"McpClient(server={self.server!r}, connected={self.connected})"

    @property
    def connected(self) -> bool:
        return self._transport is not None and self._tools is not None

    # ----- public API -----

    def list_tools(self) -> dict[str, McpTool]:
        """Allowlisted tools the server offers (discovered once per session)."""
        with self._lock:
            self._ensure_connected()
            assert self._tools is not None
            return dict(self._tools)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        if name not in self.config.tool_allowlist:
            raise IntegrationError(ErrorKind.PERMISSION, "MCP_TOOL_NOT_ALLOWED",
                                   f"Tool is not in the {self.server} MCP allowlist.")
        with self._lock:
            self._ensure_connected()
            assert self._tools is not None
            if name in self.contract_problems:
                raise IntegrationError(ErrorKind.PERMANENT, "MCP_CONTRACT_MISMATCH",
                                       f"The {self.server} MCP tool schema no longer matches the verified contract.")
            if name not in self._tools:
                raise IntegrationError(ErrorKind.PERMANENT, "MCP_TOOL_UNAVAILABLE",
                                       f"The {self.server} MCP server does not offer an allowlisted tool.")
            result = self._request("tools/call", {"name": name, "arguments": arguments}, retry_on_expiry=True)
        return self._tool_result(name, result)

    def close(self) -> None:
        with self._lock:
            transport, self._transport, self._tools = self._transport, None, None
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                logger.warning("mcp server=%s close failed error=%s", self.server, type(exc).__name__)

    # ----- connection -----

    def _ensure_connected(self) -> None:
        if self.connected:
            return
        if self._transport is None:
            self._transport = self._factory()
        try:
            self._handshake()
            self._tools = self._discover()
        except Exception:
            self._discard_transport()
            raise
        self.connections += 1
        logger.info("mcp server=%s connected tools=%d", self.server, len(self._tools))

    def _discard_transport(self) -> None:
        transport, self._transport, self._tools = self._transport, None, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _handshake(self) -> None:
        assert self._transport is not None
        self._transport.reset_session()
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},  # no sampling/roots/elicitation: the server cannot call back
            "clientInfo": CLIENT_INFO,
        })
        version = result.get("protocolVersion")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise IntegrationError(ErrorKind.PERMANENT, "MCP_UNSUPPORTED_PROTOCOL",
                                   f"The {self.server} MCP server uses an unsupported protocol version.",
                                   request_sent=True)
        if not isinstance(result.get("capabilities"), dict) or "tools" not in result["capabilities"]:
            raise IntegrationError(ErrorKind.PERMANENT, "MCP_NO_TOOLS",
                                   f"The {self.server} MCP server does not offer tools.", request_sent=True)
        self._transport.set_protocol_version(version)
        self._transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"},
                               timeout=self.config.timeout_seconds)

    def _discover(self) -> dict[str, McpTool]:
        tools: dict[str, McpTool] = {}
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            listed = result.get("tools")
            if not isinstance(listed, list):
                raise _protocol_error("Malformed tools/list response.")
            for raw in listed:
                if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                    raise _protocol_error("Malformed tool in tools/list response.")
                if raw["name"] in self.config.tool_allowlist:  # everything else is ignored
                    tools[raw["name"]] = McpTool(
                        name=raw["name"],
                        description=str(raw.get("description", ""))[:500],
                        input_schema=raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {},
                    )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.contract_problems = {}
        for name, tool in tools.items():
            contract = self.contracts.get(name)
            problems = check_tool_schema(contract, tool.input_schema) if contract else []
            if problems:
                self.contract_problems[name] = problems
                logger.warning("mcp server=%s tool=%s contract_mismatch=%s", self.server, name, problems)
        missing = sorted(self.config.tool_allowlist - set(tools))
        if missing:
            logger.warning("mcp server=%s allowlisted tools not offered count=%d", self.server, len(missing))
        return tools

    # ----- JSON-RPC -----

    def _request(self, method: str, params: dict[str, Any], *, retry_on_expiry: bool = False) -> dict[str, Any]:
        assert self._transport is not None
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            response = self._transport.request(message, timeout=self.config.timeout_seconds)
        except SessionExpired:
            if not retry_on_expiry:
                raise
            # The server did not process the request; start a new session and send it once more.
            logger.info("mcp server=%s session expired; reconnecting", self.server)
            self._tools = None
            self._handshake()
            self._tools = self._discover()
            self.connections += 1
            message["id"] = next(self._ids)
            response = self._transport.request(message, timeout=self.config.timeout_seconds)
        return self._unwrap(response, message["id"], method)

    def _unwrap(self, response: Any, request_id: int, method: str) -> dict[str, Any]:
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise _protocol_error("Invalid JSON-RPC response.")
        if "error" in response:
            error = response["error"] if isinstance(response["error"], dict) else {}
            code = error.get("code")
            logger.warning("mcp server=%s method=%s jsonrpc_error=%s", self.server, method, code)
            if code == _INTERNAL_ERROR:
                raise IntegrationError(ErrorKind.TRANSIENT, "MCP_SERVER_ERROR",
                                       f"The {self.server} MCP server reported an internal error.", request_sent=True)
            if code in (_INVALID_PARAMS, _METHOD_NOT_FOUND):
                raise IntegrationError(ErrorKind.PERMANENT, "MCP_INVALID_REQUEST",
                                       f"The {self.server} MCP server rejected the request.", request_sent=True)
            raise IntegrationError(ErrorKind.PERMANENT, "MCP_ERROR",
                                   f"The {self.server} MCP server returned an error.", request_sent=True)
        result = response.get("result")
        if not isinstance(result, dict):
            raise _protocol_error("JSON-RPC response has no result object.")
        return result

    def _tool_result(self, name: str, result: dict[str, Any]) -> McpToolResult:
        content = result.get("content", [])
        structured = result.get("structuredContent")
        if not isinstance(content, list) or (structured is not None and not isinstance(structured, dict)):
            raise _protocol_error("Malformed tools/call result.")
        if result.get("isError") is True:
            # The tool's own error text may echo arguments; log only that it happened.
            logger.warning("mcp server=%s tool_error", self.server)
            raise IntegrationError(ErrorKind.PERMANENT, "MCP_TOOL_ERROR",
                                   f"The {self.server} MCP tool reported an error.", request_sent=True)
        return McpToolResult(tool=name, content=[c for c in content if isinstance(c, dict)], structured=structured)
