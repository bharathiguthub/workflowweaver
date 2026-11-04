"""Application settings loaded from environment variables (and an optional .env file).

Secrets such as ANTHROPIC_API_KEY are deliberately NOT modelled here: the Anthropic SDK
reads them from the environment itself, so they never pass through our code or logs.
The Langfuse secret key is the exception (it must be read from .env too); it is a
SecretStr, so it is masked in repr()/logs and only unwrapped when the client is built.
"""

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# C:\Projects\workflowweaver\.env (repo root), next to .env.example
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class PlannerMode(str, Enum):
    MOCK = "mock"
    CLAUDE = "claude"


class IntegrationMode(str, Enum):
    """Server-controlled; a client can never select or escalate the mode.

    mock    in-memory adapters, synthetic data, no network (default)
    dry_run the real adapters validate arguments, destinations and authorization and build
            the exact request they would send, but never open a connection
    real    the real adapters call the configured MCP servers / webhooks
    """

    MOCK = "mock"
    DRY_RUN = "dry_run"
    REAL = "real"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    planner_mode: PlannerMode = PlannerMode.MOCK
    # No default model: the model identifier must come from configuration.
    anthropic_model: str | None = None
    anthropic_max_tokens: int = Field(default=8000, ge=256, le=64000)
    planner_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    max_workflow_steps: int = Field(default=10, ge=1, le=50)
    max_planning_attempts: int = Field(default=2, ge=1, le=5)

    # Phase 3: mock execution engine
    mock_execution_enabled: bool = True
    execution_max_attempts: int = Field(default=3, ge=1, le=5)
    execution_backoff_base_seconds: float = Field(default=0.2, ge=0, le=10)
    execution_backoff_max_seconds: float = Field(default=2.0, ge=0, le=60)
    execution_backoff_jitter: bool = True

    # Phase 4: SSE live progress (in-memory, single process, mock-only access model)
    execution_worker_threads: int = Field(default=4, ge=1, le=32)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0, le=300)
    sse_retention_seconds: float = Field(default=300.0, ge=0, le=86_400)
    sse_max_channels: int = Field(default=1_000, ge=1, le=100_000)

    # Phase 4: browser access. Comma-separated exact origins allowed by CORS. Empty means:
    # in development, the local Vite dev server origins only; otherwise none. The Vite dev
    # proxy (default frontend setup) is same-origin and does not need CORS at all.
    cors_allow_origins: str = ""

    # Phase 4: Langfuse observability (disabled unless explicitly enabled)
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    # e.g. https://cloud.langfuse.com (EU), https://us.cloud.langfuse.com (US), or self-hosted.
    # None -> the SDK default.
    langfuse_base_url: str | None = None

    # ---------- Phase 5: real integrations (server-side only; never sent to a client) ----------
    integration_mode: IntegrationMode = IntegrationMode.MOCK
    # Master kill switch for side effects in real mode. false -> plans with write steps are
    # rejected before anything runs (reads are still allowed).
    real_writes_enabled: bool = False
    # Operator credential for real writes, sent by the caller as X-Integration-Authorization.
    # Empty -> real writes are impossible.
    real_write_authorization_token: SecretStr | None = None
    integration_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    integration_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # Notion MCP server (Streamable HTTP). The bearer token authenticates US to the MCP
    # server; the Notion API key itself lives in the MCP server's own configuration.
    notion_mcp_url: str | None = None
    notion_mcp_auth_token: SecretStr | None = None
    # Comma-separated MCP tool names this server may call. Anything else is refused.
    notion_mcp_tool_allowlist: str = "API-post-page,API-patch-page"
    notion_tool_create_page: str = "API-post-page"
    notion_tool_archive_page: str = "API-patch-page"
    # Approved destinations: JSON object alias -> Notion page/database id. Plans refer to
    # aliases only (create_page "parent" argument); ids never leave the server.
    notion_destinations: str = "{}"
    notion_default_page_parent: str | None = None  # alias used when "parent" is omitted
    notion_task_database: str | None = None  # alias of the database create_task writes to

    # Google Drive MCP server (Streamable HTTP)
    google_drive_mcp_url: str | None = None
    google_drive_mcp_auth_token: SecretStr | None = None
    google_drive_mcp_tool_allowlist: str = "create_document,read_document,search_documents,trash_file"
    google_drive_tool_create_document: str = "create_document"
    google_drive_tool_search_documents: str = "search_documents"
    google_drive_tool_read_document: str = "read_document"
    google_drive_tool_trash_file: str = "trash_file"
    google_drive_destinations: str = "{}"  # JSON alias -> folder id
    google_drive_default_folder: str | None = None

    # Microsoft Teams incoming webhooks: JSON alias -> webhook URL. The URL *is* the
    # credential, so the whole map is a secret. Approved channels are exactly its keys.
    teams_webhook_urls: SecretStr | None = None
    teams_default_channel: str | None = None


DEV_FRONTEND_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def cors_origins(settings: "Settings") -> list[str]:
    explicit = [o.strip().rstrip("/") for o in settings.cors_allow_origins.split(",") if o.strip()]
    if "*" in explicit:
        raise ValueError("CORS_ALLOW_ORIGINS must list exact origins; '*' is not allowed.")
    if explicit:
        return explicit
    return list(DEV_FRONTEND_ORIGINS) if settings.app_env.lower() == "development" else []


def mock_documents_apply(settings: "Settings") -> bool:
    """The synthetic document catalog is authoritative only for mock execution."""
    return settings.mock_execution_enabled and settings.integration_mode is IntegrationMode.MOCK


@lru_cache
def get_settings() -> Settings:
    return Settings()
