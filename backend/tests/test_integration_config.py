"""Phase 5.1: integration mode selection, configuration validation, secret redaction and
test network isolation. All values are synthetic; nothing connects anywhere."""

import asyncio
import json
import logging
import socket
import urllib.request

import httpx
import pytest

from app.agents.execution.tools import MockToolRegistry, ToolRegistry, create_tool_registry
from app.core.config import IntegrationMode, Settings, mock_documents_apply
from app.core.log_redaction import TokenRedactingFilter, redact
from app.integrations.config import load_integration_config, parse_alias_map, validate_endpoint_url
from app.integrations.errors import IntegrationConfigError
from app.integrations.mcp.transport import StreamableHttpTransport
from app.integrations.secrets import register_secret, scrub_secrets
from app.observability.sanitize import sanitize
from app.schemas.workflow import Integration
from tests.conftest import ExternalNetworkBlocked
from tests.integration_fakes import NOTION_TOKEN, OPERATOR_TOKEN, SECRETS, WEBHOOK_URL, integration_settings

EXTERNAL = "https://synthetic-external.example.invalid/x"

# ---------- mode selection ----------


def test_mock_is_the_default_mode_and_real_writes_are_off():
    settings = Settings(_env_file=None)
    assert settings.integration_mode is IntegrationMode.MOCK
    assert settings.real_writes_enabled is False
    assert settings.real_write_authorization_token is None
    registry = create_tool_registry(settings)
    assert isinstance(registry, MockToolRegistry) and registry.mode is IntegrationMode.MOCK


def test_mode_comes_from_the_environment_and_unknown_modes_are_refused(monkeypatch):
    monkeypatch.setenv("INTEGRATION_MODE", "dry_run")
    assert Settings(_env_file=None).integration_mode is IntegrationMode.DRY_RUN
    monkeypatch.setenv("INTEGRATION_MODE", "production")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_dry_run_registry_has_every_adapter_and_no_network_clients():
    registry = create_tool_registry(integration_settings("dry_run"))
    assert type(registry) is ToolRegistry and registry.mode is IntegrationMode.DRY_RUN
    assert set(registry.adapters) == set(Integration)
    assert all(getattr(a, "client", None) is None and getattr(a, "sender", None) is None
               for a in registry.adapters.values())
    assert registry.document_catalog() is None


def test_dry_run_needs_no_server_urls_or_credentials():
    settings = integration_settings("dry_run", notion_mcp_url=None, google_drive_mcp_url=None,
                                    notion_mcp_auth_token=None, google_drive_mcp_auth_token=None)
    assert set(create_tool_registry(settings).adapters) == set(Integration)


def test_real_registry_includes_only_configured_integrations_and_does_not_connect():
    settings = integration_settings("real", google_drive_mcp_url=None, teams_webhook_urls=None,
                                    teams_default_channel=None)
    registry = create_tool_registry(settings)
    assert set(registry.adapters) == {Integration.NOTION}
    assert registry.adapters[Integration.NOTION].client.connected is False  # lazy: no handshake yet
    registry.close()


def test_real_registry_with_nothing_configured_supports_nothing():
    assert create_tool_registry(Settings(_env_file=None, integration_mode="real")).supported_actions() == set()


def test_synthetic_document_catalog_applies_only_to_mock_mode():
    assert mock_documents_apply(Settings(_env_file=None))
    assert not mock_documents_apply(Settings(_env_file=None, integration_mode="dry_run"))
    assert not mock_documents_apply(Settings(_env_file=None, mock_execution_enabled=False))


# ---------- configuration validation ----------


@pytest.mark.parametrize("url", [
    "http://synthetic-mcp.example.invalid/mcp",  # plain http to a remote host
    "ftp://synthetic.example.invalid/x",
    "https://user:pass@synthetic.example.invalid/mcp",  # credentials embedded in the URL
    "not a url",
])
def test_bad_mcp_urls_are_refused_without_echoing_them(url):
    with pytest.raises(IntegrationConfigError) as info:
        validate_endpoint_url(url, "NOTION_MCP_URL", allow_loopback_http=True)
    assert "NOTION_MCP_URL" in str(info.value) and url not in str(info.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:3333/mcp", "http://localhost:3333/mcp", "https://x.example/mcp"])
def test_https_or_loopback_http_mcp_urls_are_accepted(url):
    assert validate_endpoint_url(url, "NOTION_MCP_URL", allow_loopback_http=True) == url


def test_teams_webhooks_must_be_https_even_on_loopback():
    settings = integration_settings(teams_webhook_urls=json.dumps({"team": "http://127.0.0.1/hook"}),
                                    teams_default_channel="team")
    with pytest.raises(IntegrationConfigError, match="TEAMS_WEBHOOK_URLS"):
        load_integration_config(settings)


@pytest.mark.parametrize("raw", ["[1, 2]", "{not json", '{"Bad Alias": "x"}', '{"ok": ""}', '{"ok": 5}'])
def test_invalid_destination_maps_are_refused(raw):
    with pytest.raises(IntegrationConfigError, match="NOTION_DESTINATIONS"):
        parse_alias_map(raw, "NOTION_DESTINATIONS")


def test_default_destination_must_be_a_configured_alias():
    with pytest.raises(IntegrationConfigError, match="GOOGLE_DRIVE_DEFAULT_FOLDER"):
        load_integration_config(integration_settings(google_drive_default_folder="not-configured"))


def test_allowlist_may_only_contain_verified_contract_tools():
    # Phase 5.2: tool names are fixed by the verified contract; the allowlist can only narrow.
    with pytest.raises(IntegrationConfigError, match="NOTION_MCP_TOOL_ALLOWLIST"):
        load_integration_config(integration_settings(notion_mcp_tool_allowlist="API-post-page,API-delete-a-block"))
    with pytest.raises(IntegrationConfigError, match="GOOGLE_DRIVE_MCP_TOOL_ALLOWLIST"):
        load_integration_config(integration_settings(google_drive_mcp_tool_allowlist="search_files,trash_file"))
    narrowed = load_integration_config(integration_settings(notion_mcp_tool_allowlist="API-post-page"))
    assert narrowed.notion_allowlist == {"API-post-page"}


def test_configuration_errors_never_contain_secret_values():
    bad = json.dumps({"synthetic-team": WEBHOOK_URL.replace("https", "http")})
    with pytest.raises(IntegrationConfigError) as info:
        load_integration_config(integration_settings(teams_webhook_urls=bad))
    assert "SYNTHETIC-SECRET-PATH" not in str(info.value)


def test_config_and_settings_reprs_hide_urls_and_tokens():
    config = load_integration_config(integration_settings())
    assert not [s for s in SECRETS if s in repr(config)]
    assert OPERATOR_TOKEN not in repr(integration_settings())
    assert config.teams_channels.aliases == ["synthetic-team"]  # aliases are public, targets are not


# ---------- secret redaction ----------


def test_loading_config_registers_secrets_for_log_and_trace_redaction():
    load_integration_config(integration_settings())
    line = f"HTTP Request: POST {WEBHOOK_URL} token {NOTION_TOKEN}"
    assert WEBHOOK_URL not in scrub_secrets(line) and NOTION_TOKEN not in scrub_secrets(line)
    assert WEBHOOK_URL not in redact(line)
    assert WEBHOOK_URL not in json.dumps(sanitize({"note": line}))


def test_short_values_are_not_registered_as_secrets():
    register_secret("abc")
    assert scrub_secrets("abc def") == "abc def"


def test_log_filter_scrubs_registered_secrets_in_args():
    register_secret("synthetic-secret-value-123")
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: POST %s",
                               ("https://host.invalid/synthetic-secret-value-123",), None)
    TokenRedactingFilter().filter(record)
    assert "synthetic-secret-value-123" not in record.getMessage()


# ---------- test isolation: every network path to a non-loopback host is blocked ----------


def test_sync_httpx_is_blocked():
    with pytest.raises(ExternalNetworkBlocked):
        httpx.get(EXTERNAL)


def test_async_httpx_is_blocked():
    async def call():
        async with httpx.AsyncClient() as client:
            await client.get(EXTERNAL)

    with pytest.raises(ExternalNetworkBlocked):
        asyncio.run(call())


def test_real_mcp_transport_is_blocked_without_an_injected_fake():
    transport = StreamableHttpTransport(EXTERNAL, auth_token=None, connect_timeout=1, timeout=1)
    with pytest.raises(ExternalNetworkBlocked):
        transport.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=1)
    transport.close()


def test_raw_sockets_dns_and_urllib_are_blocked():
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("synthetic-external.example.invalid", 443)
    with pytest.raises(ExternalNetworkBlocked):
        socket.create_connection(("192.0.2.1", 443), timeout=1)  # TEST-NET-1 address
    with pytest.raises(ExternalNetworkBlocked):
        urllib.request.urlopen(EXTERNAL, timeout=1)


def test_loopback_is_still_allowed():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()
