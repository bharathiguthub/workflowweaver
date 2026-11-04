"""Application settings loaded from environment variables (and an optional .env file).

Secrets such as ANTHROPIC_API_KEY are deliberately NOT modelled here: the Anthropic SDK
reads them from the environment itself, so they never pass through our code or logs.
"""

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
