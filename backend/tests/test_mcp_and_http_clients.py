"""Phase 5.1: MCP client, Streamable HTTP transport and webhook client.

The MCP client runs against ``FakeMcpServer`` (in-memory McpTransport); the HTTP layers run
against ``httpx.MockTransport``. No connection is opened."""

import json

import httpx
import pytest

from app.agents.execution.tools.base import ErrorKind
from app.integrations.config import McpServerConfig
from app.integrations.errors import IntegrationError
from app.integrations.http.webhook import WebhookClient
from app.integrations.mcp.client import McpClient
from app.integrations.mcp.transport import SessionExpired, StreamableHttpTransport
from app.schemas.workflow import Integration
from tests.integration_fakes import FakeMcpServer

URL = "https://synthetic-mcp.example.invalid/mcp"
HOOK = "https://synthetic.webhook.office.com/webhookb2/SYNTHETIC-HOOK"
ALLOW = frozenset({"API-post-page", "API-patch-page"})


def client_for(server: FakeMcpServer, allow=ALLOW) -> McpClient:
    config = McpServerConfig(Integration.NOTION, url=URL, auth_token="synthetic-token-xyz", tool_allowlist=allow)
    return McpClient(config, lambda: server)


def methods(server: FakeMcpServer) -> list[str]:
    return [m["method"] for m in server.messages]


# ======================= MCP client =======================


def test_client_is_lazy_then_handshakes_and_discovers_in_order():
    server = FakeMcpServer("notion")
    client = client_for(server)
    assert server.messages == [] and not client.connected
    result = client.call_tool("API-post-page", {"parent": {"page_id": "p"}, "properties": {}})
    assert methods(server) == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    init = server.messages[0]["params"]
    assert init["protocolVersion"] == "2025-06-18" and init["capabilities"] == {}
    assert result.json_payload()["object"] == "page" and result.json_payload()["id"]
    client.call_tool("API-post-page", {"parent": {"page_id": "p"}, "properties": {}})
    assert methods(server).count("initialize") == 1  # session reused
    assert client.connections == 1


def test_discovery_keeps_only_allowlisted_tools_across_pages():
    server = FakeMcpServer("notion", page_size=5)  # the real 2.6.1 list has 24 tools
    assert set(client_for(server).list_tools()) == ALLOW  # e.g. API-delete-a-block is dropped
    assert methods(server).count("tools/list") == 5


def test_non_allowlisted_tool_is_refused_before_any_network_traffic():
    server = FakeMcpServer("notion")
    with pytest.raises(IntegrationError) as info:
        client_for(server).call_tool("API-delete-a-block", {"block_id": "b"})  # offered, not allowlisted
    assert info.value.kind is ErrorKind.PERMISSION and info.value.code == "MCP_TOOL_NOT_ALLOWED"
    assert server.messages == []


def test_allowlisted_tool_missing_on_server_is_unavailable():
    server = FakeMcpServer("notion")
    server.tools = [t for t in server.tools if t["name"] != "API-patch-page"]
    with pytest.raises(IntegrationError) as info:
        client_for(server).call_tool("API-patch-page", {})
    assert info.value.code == "MCP_TOOL_UNAVAILABLE" and server.tool_calls == []


def test_tool_error_result_is_permanent_and_does_not_leak_tool_text():
    server = FakeMcpServer("notion")
    server.tool_errors.add("API-post-page")
    with pytest.raises(IntegrationError) as info:
        client_for(server).call_tool("API-post-page", {"secret_arg": "SYNTHETIC-ARG-VALUE"})
    assert info.value.code == "MCP_TOOL_ERROR" and not info.value.retryable
    assert "SYNTHETIC-ARG-VALUE" not in info.value.message


@pytest.mark.parametrize("code, expected, retryable", [
    (-32603, "MCP_SERVER_ERROR", True),
    (-32602, "MCP_INVALID_REQUEST", False),
    (-32000, "MCP_ERROR", False),
])
def test_jsonrpc_errors_are_classified(code, expected, retryable):
    server = FakeMcpServer("notion")
    client = client_for(server)
    client.list_tools()
    server.request = lambda m, timeout: {"jsonrpc": "2.0", "id": m["id"],
                                         "error": {"code": code, "message": "SYNTHETIC-REMOTE-TEXT"}}
    with pytest.raises(IntegrationError) as info:
        client.call_tool("API-post-page", {})
    assert info.value.code == expected and info.value.retryable is retryable
    assert "SYNTHETIC-REMOTE-TEXT" not in info.value.message


def test_mismatched_response_id_is_a_protocol_error():
    server = FakeMcpServer("notion")
    client = client_for(server)
    client.list_tools()
    server.request = lambda m, timeout: {"jsonrpc": "2.0", "id": 999, "result": {}}
    with pytest.raises(IntegrationError, match="Invalid JSON-RPC"):
        client.call_tool("API-post-page", {})


def test_unsupported_protocol_version_fails_and_discards_the_session():
    server = FakeMcpServer("notion", protocol_version="1999-01-01")
    client = client_for(server)
    with pytest.raises(IntegrationError) as info:
        client.list_tools()
    assert info.value.code == "MCP_UNSUPPORTED_PROTOCOL" and not client.connected and server.closed == 1


def test_expired_session_reconnects_once_and_sends_the_call_once():
    server = FakeMcpServer("notion")
    client = client_for(server)
    client.list_tools()
    server.expire_session_once = True
    client.call_tool("API-post-page", {"parent": {"page_id": "p"}, "properties": {}})
    assert client.connections == 2 and len(server.tool_calls) == 1


def test_transient_transport_failure_propagates_with_request_sent_flag():
    server = FakeMcpServer("notion")
    server.failures["API-post-page"] = [IntegrationError(ErrorKind.TRANSIENT, "MCP_TIMEOUT", "t", request_sent=True)]
    with pytest.raises(IntegrationError) as info:
        client_for(server).call_tool("API-post-page", {})
    assert info.value.retryable and info.value.request_sent


def test_close_ends_session_and_next_call_reconnects():
    server = FakeMcpServer("notion")
    client = client_for(server)
    client.list_tools()
    client.close()
    assert server.closed == 1 and not client.connected
    client.list_tools()
    assert client.connections == 2


def test_client_repr_hides_url_and_token():
    text = repr(client_for(FakeMcpServer("notion")))
    assert URL not in text and "synthetic-token-xyz" not in text


# ======================= Streamable HTTP transport (httpx.MockTransport) =======================


def http_transport(handler) -> StreamableHttpTransport:
    return StreamableHttpTransport(URL, auth_token="synthetic-token-xyz", connect_timeout=1, timeout=2,
                                   http_transport=httpx.MockTransport(handler))


def init_message(rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {}}


def test_http_transport_sends_auth_and_session_headers_and_parses_json():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}},
                              headers={"Mcp-Session-Id": "synthetic-session-1"})

    transport = http_transport(handler)
    assert transport.request(init_message(), timeout=2)["result"] == {"ok": True}
    transport.set_protocol_version("2025-06-18")
    transport.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, timeout=2)
    first, second = seen
    assert first.headers["Authorization"] == "Bearer synthetic-token-xyz"
    assert "text/event-stream" in first.headers["Accept"] and "mcp-session-id" not in first.headers
    assert second.headers["Mcp-Session-Id"] == "synthetic-session-1"
    assert second.headers["MCP-Protocol-Version"] == "2025-06-18"
    assert "synthetic-token-xyz" not in repr(transport) and URL not in repr(transport)


def test_http_transport_parses_sse_response_and_skips_other_messages():
    sse = (
        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"value":42}}\n\n'
    )
    transport = http_transport(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    assert transport.request(init_message(7), timeout=2)["result"] == {"value": 42}


@pytest.mark.parametrize("status, code, kind, sent", [
    (401, "MCP_UNAUTHORIZED", ErrorKind.PERMISSION, True),
    (403, "MCP_UNAUTHORIZED", ErrorKind.PERMISSION, True),
    (429, "MCP_RATE_LIMITED", ErrorKind.TRANSIENT, False),
    (503, "MCP_SERVER_ERROR", ErrorKind.TRANSIENT, True),
    (400, "MCP_HTTP_ERROR", ErrorKind.PERMANENT, True),
    (302, "MCP_HTTP_ERROR", ErrorKind.PERMANENT, True),  # redirects are never followed
])
def test_http_transport_status_classification(status, code, kind, sent):
    transport = http_transport(lambda r: httpx.Response(status, headers={"location": "https://elsewhere.invalid/"}))
    with pytest.raises(IntegrationError) as info:
        transport.request(init_message(), timeout=2)
    assert (info.value.code, info.value.kind, info.value.request_sent) == (code, kind, sent)


@pytest.mark.parametrize("exc, code, sent", [
    (httpx.ConnectError("x"), "MCP_UNREACHABLE", False),
    (httpx.ConnectTimeout("x"), "MCP_UNREACHABLE", False),
    (httpx.ReadTimeout("x"), "MCP_TIMEOUT", True),
    (httpx.RemoteProtocolError("x"), "MCP_TRANSPORT_ERROR", True),
])
def test_http_transport_network_errors(exc, code, sent):
    def handler(request):
        raise exc

    with pytest.raises(IntegrationError) as info:
        http_transport(handler).request(init_message(), timeout=2)
    assert info.value.code == code and info.value.retryable and info.value.request_sent is sent


def test_http_transport_404_on_a_session_is_session_expired():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}},
                                  headers={"Mcp-Session-Id": "s1"})
        return httpx.Response(404)

    transport = http_transport(handler)
    transport.request(init_message(), timeout=2)
    with pytest.raises(SessionExpired):
        transport.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, timeout=2)


def test_http_transport_rejects_unreadable_and_oversized_bodies(monkeypatch):
    transport = http_transport(lambda r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"}))
    with pytest.raises(IntegrationError, match="Unreadable"):
        transport.request(init_message(), timeout=2)
    monkeypatch.setattr("app.integrations.mcp.transport.MAX_RESPONSE_BYTES", 10)
    big = http_transport(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"x": "y" * 50}}))
    with pytest.raises(IntegrationError) as info:
        big.request(init_message(), timeout=2)
    assert info.value.code == "MCP_RESPONSE_TOO_LARGE"


def test_mcp_client_over_http_transport_end_to_end():
    """Full handshake + discovery + call through the real HTTP transport code, fake server."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        result = {
            "initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
            "tools/list": {"tools": [{"name": "API-post-page"}, {"name": "API-patch-page"}, {"name": "evil"}]},
            "tools/call": {"content": [{"type": "text", "text": '{"id": "synthetic-page-1"}'}]},
        }[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                              headers={"Mcp-Session-Id": "synthetic-session"})

    config = McpServerConfig(Integration.NOTION, url=URL, auth_token="synthetic-token-xyz", tool_allowlist=ALLOW)
    client = McpClient(config, lambda: http_transport(handler))
    assert set(client.list_tools()) == ALLOW
    assert client.call_tool("API-post-page", {}).json_payload() == {"id": "synthetic-page-1"}
    client.close()


# ======================= webhook client =======================


def webhook(handler) -> WebhookClient:
    return WebhookClient(connect_timeout=1, timeout=2, http_transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [200, 202])
def test_webhook_success(status):
    seen = []
    client = webhook(lambda r: seen.append(r) or httpx.Response(status))
    assert client.post_json(HOOK, {"text": "hi"}).status_code == status
    assert json.loads(seen[0].content) == {"text": "hi"}
    assert HOOK not in repr(client)


@pytest.mark.parametrize("status, code, kind, sent", [
    (429, "WEBHOOK_RATE_LIMITED", ErrorKind.TRANSIENT, False),
    (500, "WEBHOOK_SERVER_ERROR", ErrorKind.TRANSIENT, True),
    (403, "WEBHOOK_UNAUTHORIZED", ErrorKind.PERMISSION, True),
    (404, "WEBHOOK_NOT_FOUND", ErrorKind.PERMANENT, True),
    (400, "WEBHOOK_REJECTED", ErrorKind.PERMANENT, True),
    (301, "WEBHOOK_REJECTED", ErrorKind.PERMANENT, True),
])
def test_webhook_status_classification_never_echoes_the_url(status, code, kind, sent):
    with pytest.raises(IntegrationError) as info:
        webhook(lambda r: httpx.Response(status)).post_json(HOOK, {"text": "hi"})
    assert (info.value.code, info.value.kind, info.value.request_sent) == (code, kind, sent)
    assert "SYNTHETIC-HOOK" not in info.value.message


@pytest.mark.parametrize("exc, code, sent", [
    (httpx.ConnectError("x"), "WEBHOOK_UNREACHABLE", False),
    (httpx.ReadTimeout("x"), "WEBHOOK_TIMEOUT", True),
])
def test_webhook_network_errors(exc, code, sent):
    def handler(request):
        raise exc

    with pytest.raises(IntegrationError) as info:
        webhook(handler).post_json(HOOK, {})
    assert info.value.code == code and info.value.request_sent is sent


def test_webhook_payload_size_limit_is_checked_before_sending():
    seen = []
    with pytest.raises(IntegrationError) as info:
        webhook(lambda r: seen.append(r) or httpx.Response(200)).post_json(HOOK, {"text": "x" * 40_000})
    assert info.value.code == "WEBHOOK_PAYLOAD_TOO_LARGE" and seen == []
