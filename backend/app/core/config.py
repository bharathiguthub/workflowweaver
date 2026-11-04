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


DEV_FRONTEND_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def cors_origins(settings: "Settings") -> list[str]:
    explicit = [o.strip().rstrip("/") for o in settings.cors_allow_origins.split(",") if o.strip()]
    if "*" in explicit:
        raise ValueError("CORS_ALLOW_ORIGINS must list exact origins; '*' is not allowed.")
    if explicit:
        return explicit
    return list(DEV_FRONTEND_ORIGINS) if settings.app_env.lower() == "development" else []


@lru_cache
def get_settings() -> Settings:
    return Settings()
