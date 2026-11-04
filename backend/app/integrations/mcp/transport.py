"""MCP transports.

``McpTransport`` is the seam the client talks to: send one JSON-RPC request and get the
matching response, or send a notification. Tests implement it in memory (a fake MCP
server) or drive ``StreamableHttpTransport`` through ``httpx.MockTransport``; no test opens
a real connection.

``StreamableHttpTransport`` implements the client side of the MCP Streamable HTTP
transport with httpx (already a dependency): POST per message, ``Accept: application/json,
text/event-stream``, the ``Mcp-Session-Id`` header after initialisation, and either a JSON
body or an SSE stream carrying the response. Redirects are never followed (a redirect could
leak the bearer token to another host).

Error classification (``request_sent`` = the server may have received the request):
  connect error / connect timeout     TRANSIENT  request_sent=False
  read/write timeout, broken stream   TRANSIENT  request_sent=True
  HTTP 429                            TRANSIENT  request_sent=False
  HTTP 5xx                            TRANSIENT  request_sent=True
  HTTP 401 / 403                      PERMISSION
  HTTP 404 with a session             SESSION_EXPIRED (client re-initialises once)
  other HTTP status / bad body        PERMANENT
"""

import json
import logging
from typing import Any, Protocol

import httpx

from app.agents.execution.tools.base import ErrorKind
from app.integrations.errors import IntegrationError

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"


class McpTransport(Protocol):
    def request(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Send a JSON-RPC request and return the JSON-RPC response with the same id."""
        ...

    def notify(self, message: dict[str, Any], *, timeout: float) -> None: ...

    def set_protocol_version(self, version: str) -> None: ...

    def reset_session(self) -> None: ...

    def close(self) -> None: ...


class SessionExpired(IntegrationError):
    def __init__(self) -> None:
        super().__init__(ErrorKind.TRANSIENT, "MCP_SESSION_EXPIRED", "MCP session expired.", request_sent=False)


def _err(kind: ErrorKind, code: str, message: str, *, sent: bool) -> IntegrationError:
    return IntegrationError(kind, code, message, request_sent=sent)


class StreamableHttpTransport:
    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None,
        connect_timeout: float,
        timeout: float,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = url
        self._connect_timeout = connect_timeout
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            follow_redirects=False,
            transport=http_transport,
        )
        self._session_id: str | None = None
        self._protocol_version: str | None = None

    def __repr__(self) -> str:  # never show the URL or headers
        return "StreamableHttpTransport(<redacted>)"

    # ----- McpTransport -----

    def set_protocol_version(self, version: str) -> None:
        self._protocol_version = version

    def reset_session(self) -> None:
        self._session_id = None
        self._protocol_version = None

    def request(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        response = self._post(message, timeout)
        session = response.headers.get(SESSION_HEADER)
        if session and message.get("method") == "initialize":
            if len(session) > 256 or not session.isascii():
                raise _err(ErrorKind.PERMANENT, "MCP_PROTOCOL_ERROR", "Invalid MCP session id.", sent=True)
            self._session_id = session
        return self._parse(response, message.get("id"))

    def notify(self, message: dict[str, Any], *, timeout: float) -> None:
        self._post(message, timeout)

    def close(self) -> None:
        try:
            if self._session_id:  # best-effort session termination
                self._client.delete(self._url, headers=self._headers(), timeout=2.0)
        except Exception:
            pass
        finally:
            self._session_id = None
            self._client.close()

    # ----- internals -----

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._session_id:
            headers[SESSION_HEADER] = self._session_id
        if self._protocol_version:
            headers[PROTOCOL_HEADER] = self._protocol_version
        return headers

    def _post(self, message: dict[str, Any], timeout: float) -> httpx.Response:
        try:
            response = self._client.post(
                self._url, content=json.dumps(message), headers=self._headers(),
                timeout=httpx.Timeout(timeout, connect=min(timeout, self._connect_timeout)),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise _err(ErrorKind.TRANSIENT, "MCP_UNREACHABLE", "MCP server unreachable.", sent=False) from exc
        except httpx.TimeoutException as exc:
            raise _err(ErrorKind.TRANSIENT, "MCP_TIMEOUT", "MCP server timed out.", sent=True) from exc
        except httpx.HTTPError as exc:
            raise _err(ErrorKind.TRANSIENT, "MCP_TRANSPORT_ERROR", "MCP transport error.", sent=True) from exc

        code = response.status_code
        if 200 <= code < 300:
            return response
        if code == 404 and self._session_id:
            raise SessionExpired()
        if code in (401, 403):
            raise _err(ErrorKind.PERMISSION, "MCP_UNAUTHORIZED", f"MCP server refused credentials (HTTP {code}).",
                       sent=True)
        if code == 429:
            raise _err(ErrorKind.TRANSIENT, "MCP_RATE_LIMITED", "MCP server rate limited (HTTP 429).", sent=False)
        if code >= 500:
            raise _err(ErrorKind.TRANSIENT, "MCP_SERVER_ERROR", f"MCP server error (HTTP {code}).", sent=True)
        raise _err(ErrorKind.PERMANENT, "MCP_HTTP_ERROR", f"MCP server returned HTTP {code}.", sent=True)

    def _parse(self, response: httpx.Response, request_id: Any) -> dict[str, Any]:
        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            raise _err(ErrorKind.PERMANENT, "MCP_RESPONSE_TOO_LARGE", "MCP response exceeded the size limit.",
                       sent=True)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        try:
            if content_type == "text/event-stream":
                return _message_from_sse(body.decode("utf-8"), request_id)
            if content_type == "application/json":
                data = json.loads(body)
                if isinstance(data, list):  # JSON-RPC batch: pick our response
                    data = next((m for m in data if isinstance(m, dict) and m.get("id") == request_id), None)
                if isinstance(data, dict):
                    return data
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise _err(ErrorKind.PERMANENT, "MCP_PROTOCOL_ERROR", "Unreadable MCP response.", sent=True)


def _message_from_sse(text: str, request_id: Any) -> dict[str, Any]:
    """Return the JSON-RPC response for ``request_id`` from an SSE body. Server requests
    and notifications on the stream are ignored (this client advertises no capabilities)."""
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.split("\n") if line.startswith("data:"))
        if not data:
            continue
        message = json.loads(data)
        if isinstance(message, dict) and message.get("id") == request_id and ("result" in message or "error" in message):
            return message
    raise json.JSONDecodeError("no response for request id", text, 0)
