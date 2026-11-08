"""Application settings loaded from environment variables (and an optional .env file).

Secrets (ANTHROPIC_API_KEY, the Langfuse secret key, integration tokens) are SecretStr:
masked in repr()/logs and only unwrapped where a client is built. ANTHROPIC_API_KEY may come
from the shell or from .env; when neither sets it the SDK's own environment lookup applies.
"""

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# C:\Projects\workflowweaver\.env (repo root), next to .env.example
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class PlannerMode(str, Enum):
    MOCK = "mock"
    CLAUDE = "claude"
    GEMINI = "gemini"
    # Primary planner with automatic fallback (PRIMARY_PLANNER -> FALLBACK_PLANNER, default
    # Groq -> Gemini) on provider outages only: rate limits (429), provider failures
    # (500/502/503, plus 408/504), connection errors and timeouts.
    AUTO = "auto"


class PlannerProvider(str, Enum):
    """AI planner providers usable as PRIMARY_PLANNER / FALLBACK_PLANNER in auto mode."""

    CLAUDE = "claude"
    GEMINI = "gemini"
    GROQ = "groq"


class IntegrationMode(str, Enum):
    """Server-controlled CEILING: a request may select this mode or a lower one
    (mock < dry_run < real) with approval.mode, never a higher one. Lower modes make no
    network call and no side effect, so downgrading cannot weaken any real-mode gate.

    mock    in-memory adapters, synthetic data, no network (default)
    dry_run the real adapters validate arguments, destinations and authorization and build
            the exact request they would send, but never open a connection
    real    the real adapters call the configured MCP servers / webhooks
    """

    MOCK = "mock"
    DRY_RUN = "dry_run"
    REAL = "real"


class DriveTransport(str, Enum):
    """How the real Google Drive adapter talks to Drive.

    mcp   the official drivemcp.googleapis.com server (Phase 5.2, documented contract only;
          no trash/folder tools, so uploads cannot be rolled back)
    rest  Drive REST API v3 with OAuth 2.0 (Desktop app, PKCE) and ONLY the drive.file
          scope (Phase 6.1): folders, uploads, verification, listing and trash rollback
    """

    MCP = "mcp"
    REST = "rest"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    planner_mode: PlannerMode = PlannerMode.MOCK
    # Read from the shell environment OR .env and passed to the Anthropic client explicitly
    # (pydantic-settings does not export .env values to os.environ). Never logged.
    anthropic_api_key: SecretStr | None = None
    # No default model: the model identifier must come from configuration.
    anthropic_model: str | None = None
    anthropic_max_tokens: int = Field(default=8000, ge=256, le=64000)

    # PLANNER_MODE=gemini: Google Gemini via the official google-genai SDK. Key from the
    # shell or .env, passed to the client explicitly, redacted from logs. No default model:
    # choose one available to your API project (see README).
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    # Includes Gemini "thinking" tokens, which count toward the output limit.
    gemini_max_output_tokens: int = Field(default=16384, ge=256, le=65536)
    planner_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    # PLANNER_MODE=auto: PRIMARY_PLANNER (default Groq) is tried first (with the bounded retry
    # policy below); FALLBACK_PLANNER (default Gemini) is used only when the primary is
    # unavailable (HTTP 429, 500/502/503, 408/504, connection errors, timeouts) - never for bad
    # credentials, refusals or invalid plans. The same order applies to workflow planning,
    # project plans and Phase 7.2 document extraction.
    primary_planner: PlannerProvider = PlannerProvider.GROQ
    fallback_planner: PlannerProvider | None = PlannerProvider.GEMINI

    # Groq (official groq SDK, structured outputs with strict json_schema). Key from the
    # shell or .env, passed to the client explicitly, redacted from logs. No default model:
    # use one with strict structured-output support (see README / .env.example).
    groq_api_key: SecretStr | None = None
    groq_model: str | None = None
    groq_max_tokens: int = Field(default=8192, ge=256, le=65536)

    @field_validator("fallback_planner", mode="before")
    @classmethod
    def _blank_fallback_is_none(cls, value):
        """FALLBACK_PLANNER= (empty) or 'none' disables the fallback."""
        if isinstance(value, str) and value.strip().lower() in {"", "none", "off"}:
            return None
        return value

    @model_validator(mode="after")
    def _fallback_differs_from_primary(self):
        if self.fallback_planner is not None and self.fallback_planner == self.primary_planner:
            raise ValueError("FALLBACK_PLANNER must differ from PRIMARY_PLANNER (or be empty to disable it).")
        return self

    max_workflow_steps: int = Field(default=10, ge=1, le=50)
    max_planning_attempts: int = Field(default=2, ge=1, le=5)
    # Wait between planner attempts after a RETRYABLE planner error (429, 5xx such as Gemini
    # 503 UNAVAILABLE, timeouts, invalid output): base * 2^(n-1), capped at max, with equal
    # jitter (uniform in [d/2, d]). With the defaults: ~0.5-1 s, then ~1-2 s, ... up to 8 s.
    planning_backoff_base_seconds: float = Field(default=1.0, ge=0, le=30)
    planning_backoff_max_seconds: float = Field(default=8.0, ge=0, le=60)
    planning_backoff_jitter: bool = True

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

    # Notion MCP server: the self-hosted @notionhq/notion-mcp-server (verified: 2.6.1) run
    # with --transport http. The bearer token is that server's --auth-token / AUTH_TOKEN;
    # the Notion integration token (NOTION_TOKEN) lives in the MCP server's own environment.
    notion_mcp_url: str | None = None
    notion_mcp_auth_token: SecretStr | None = None
    # Comma-separated subset of the verified tools (app.integrations.contracts) this server
    # may call. Tool names are fixed by the contract; the allowlist can only narrow them.
    notion_mcp_tool_allowlist: str = "API-post-page,API-patch-page,API-retrieve-a-page"
    # Approved destinations: JSON object alias -> Notion page/database id. Plans refer to
    # aliases only (create_page "parent" argument); ids never leave the server.
    notion_destinations: str = "{}"
    notion_default_page_parent: str | None = None  # alias used when "parent" is omitted
    notion_task_database: str | None = None  # alias of the database create_task writes to

    # Google Drive: the official Google-hosted Drive MCP server
    # (https://drivemcp.googleapis.com/mcp/v1). It authenticates with an OAuth 2.0 ACCESS
    # token (short-lived; no refresh flow yet - see README). Contract is documented-only.
    google_drive_mcp_url: str | None = None
    google_drive_mcp_auth_token: SecretStr | None = None
    # Remove create_file to make Drive read-only (create_document is then unsupported).
    google_drive_mcp_tool_allowlist: str = "create_file,search_files,read_file_content,get_file_metadata"
    google_drive_destinations: str = "{}"  # JSON alias -> folder id
    google_drive_default_folder: str | None = None

    # ---------- Phase 6.1: Google Drive REST API (OAuth 2.0, drive.file scope only) ----------
    # Default "mcp" keeps the Phase 5 behaviour; set GOOGLE_DRIVE_TRANSPORT=rest to use the
    # REST adapter. The OAuth scope is FIXED in code (drive.file) and is not configurable.
    google_drive_transport: DriveTransport = DriveTransport.MCP
    # OAuth client of type "Desktop app" from Google Cloud Console. The secret is redacted
    # everywhere; Google documents Desktop-client secrets as not confidential, but we still
    # never log or return it.
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    # Where scripts/google_drive_auth.py stores the refresh token. Must be OUTSIDE the
    # repository. Empty -> %LOCALAPPDATA%\WorkflowWeaver\google-drive-token.json (Windows)
    # or ~/.config/workflowweaver/google-drive-token.json.
    google_drive_token_file: str | None = None
    # true (default): in dry_run/real mode, Drive writes go ONLY into SMOKE_TEST_DRIVE_FOLDER
    # or into folders created earlier in the same run (parent_step).
    google_drive_real_writes_sandbox_only: bool = True

    # Microsoft Teams incoming webhooks: JSON alias -> webhook URL. The URL *is* the
    # credential, so the whole map is a secret. Approved channels are exactly its keys.
    teams_webhook_urls: SecretStr | None = None
    teams_default_channel: str | None = None
    # true (default): in dry_run/real mode, Teams messages go ONLY to SMOKE_TEST_TEAMS_CHANNEL
    # (Phase 7.1 Teams Only workflow). false: any approved TEAMS_WEBHOOK_URLS alias.
    teams_real_writes_sandbox_only: bool = True

    # ---------- Phase 5.3: controlled smoke tests (scripts/smoke_integrations.py) ----------
    # Aliases of NON-PRODUCTION sandbox destinations. Smoke-test writes go ONLY here; each must
    # also be an approved destination alias above. Empty = smoke writes impossible.
    smoke_test_notion_parent: str | None = None
    smoke_test_drive_folder: str | None = None
    smoke_test_teams_channel: str | None = None

    # ---------- Phase 5.3 business workflow: Notion sandbox confinement ----------
    # true (default): in dry_run/real mode, top-level Notion pages may be created ONLY under
    # SMOKE_TEST_NOTION_PARENT, child pages only inside pages created by the same run, and
    # task-database writes are refused. false: any approved NOTION_DESTINATIONS alias.
    notion_real_writes_sandbox_only: bool = True


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
