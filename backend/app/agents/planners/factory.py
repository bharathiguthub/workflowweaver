"""Builds the configured planner from Settings."""

import logging
import os
from collections.abc import Callable
from typing import Any

import anthropic

from app.agents.planners.base import FallbackPlanner, Planner, PlannerConfigError
from app.agents.planners.claude import ClaudePlanner
from app.agents.planners.gemini import GeminiPlanner
from app.agents.planners.groq import GroqPlanner
from app.agents.planners.mock import MockPlanner
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.core.config import PlannerMode, PlannerProvider, Settings, mock_documents_apply
from app.integrations.secrets import register_secret

logger = logging.getLogger(__name__)


def anthropic_api_key(settings: Settings) -> str | None:
    """ANTHROPIC_API_KEY from Settings (shell or .env), else the process environment."""
    if settings.anthropic_api_key and settings.anthropic_api_key.get_secret_value().strip():
        return settings.anthropic_api_key.get_secret_value().strip()
    return os.environ.get("ANTHROPIC_API_KEY", "").strip() or None


def auto_chain(settings: Settings) -> list[PlannerProvider]:
    """PLANNER_MODE=auto: [PRIMARY_PLANNER, FALLBACK_PLANNER] (fallback omitted when empty)."""
    return [settings.primary_planner, *([settings.fallback_planner] if settings.fallback_planner else [])]


def _selected(settings: Settings, provider: PlannerProvider) -> bool:
    if settings.planner_mode is PlannerMode.AUTO:
        return provider in auto_chain(settings)
    return settings.planner_mode.value == provider.value


def claude_config_problem(settings: Settings) -> str | None:
    """Why the Claude planner cannot be used, naming settings only (never values)."""
    if not _selected(settings, PlannerProvider.CLAUDE):
        return "PLANNER_MODE is 'mock'; set PLANNER_MODE=claude to use the Claude planner."
    if not settings.anthropic_model:
        return "PLANNER_MODE=claude requires ANTHROPIC_MODEL to be set."
    if not anthropic_api_key(settings):
        return "ANTHROPIC_API_KEY is not set (shell environment or .env)."
    return None


def gemini_api_key(settings: Settings) -> str | None:
    """GEMINI_API_KEY from Settings (shell or .env), else the process environment."""
    if settings.gemini_api_key and settings.gemini_api_key.get_secret_value().strip():
        return settings.gemini_api_key.get_secret_value().strip()
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


def gemini_config_problem(settings: Settings) -> str | None:
    if not _selected(settings, PlannerProvider.GEMINI):
        return "PLANNER_MODE is not 'gemini'."
    if not settings.gemini_model:
        return f"PLANNER_MODE={settings.planner_mode.value} requires GEMINI_MODEL to be set."
    if not gemini_api_key(settings):
        return "GEMINI_API_KEY is not set (shell environment or .env)."
    return None


def groq_api_key(settings: Settings) -> str | None:
    """GROQ_API_KEY from Settings (shell or .env), else the process environment."""
    if settings.groq_api_key and settings.groq_api_key.get_secret_value().strip():
        return settings.groq_api_key.get_secret_value().strip()
    return os.environ.get("GROQ_API_KEY", "").strip() or None


def groq_config_problem(settings: Settings) -> str | None:
    if not _selected(settings, PlannerProvider.GROQ):
        return "Groq is not selected (PLANNER_MODE=auto with PRIMARY_PLANNER or FALLBACK_PLANNER=groq)."
    if not settings.groq_model:
        return "GROQ_MODEL must be set to use the Groq planner."
    if not groq_api_key(settings):
        return "GROQ_API_KEY is not set (shell environment or .env)."
    return None


def provider_problem(settings: Settings, provider: PlannerProvider) -> str | None:
    """Why one provider cannot be used, naming settings only (never values)."""
    return {PlannerProvider.CLAUDE: claude_config_problem, PlannerProvider.GEMINI: gemini_config_problem,
            PlannerProvider.GROQ: groq_config_problem}[provider](settings)


def provider_model(settings: Settings, provider: PlannerProvider) -> str | None:
    return {PlannerProvider.CLAUDE: settings.anthropic_model, PlannerProvider.GEMINI: settings.gemini_model,
            PlannerProvider.GROQ: settings.groq_model}[provider]


def fallback_problem(settings: Settings) -> str | None:
    """Auto mode: why the fallback planner is unavailable (None = ready or not configured)."""
    if settings.planner_mode is not PlannerMode.AUTO or settings.fallback_planner is None:
        return None
    return provider_problem(settings, settings.fallback_planner)


def ai_planner_problem(settings: Settings) -> str | None:
    """Why no AI planner is usable, naming settings only (never values). In auto mode only the
    PRIMARY must be ready; a missing fallback is reported separately by ``fallback_problem``."""
    if settings.planner_mode is PlannerMode.AUTO:
        return provider_problem(settings, settings.primary_planner)
    if settings.planner_mode is PlannerMode.GEMINI:
        return gemini_config_problem(settings)
    if settings.planner_mode is PlannerMode.CLAUDE:
        return claude_config_problem(settings)
    return "PLANNER_MODE is 'mock'; set PLANNER_MODE=claude, gemini or auto to use an AI planner."


def ai_planner_model(settings: Settings) -> str | None:
    if settings.planner_mode is PlannerMode.AUTO:
        return provider_model(settings, settings.primary_planner)
    return {PlannerMode.CLAUDE: settings.anthropic_model,
            PlannerMode.GEMINI: settings.gemini_model}.get(settings.planner_mode)


def create_gemini_client(settings: Settings):
    problem = gemini_config_problem(settings)
    if problem:
        raise PlannerConfigError(problem)
    from google import genai
    from google.genai import types

    key = gemini_api_key(settings)
    register_secret(key)  # scrubbed from every log record and trace
    try:
        # attempts=1: no SDK retries; the planning graph's retry edge owns retry policy.
        http = types.HttpOptions(timeout=int(settings.planner_timeout_seconds * 1000),
                                 retry_options=types.HttpRetryOptions(attempts=1))
        return genai.Client(api_key=key, http_options=http)
    except Exception as exc:  # never include exc text: it could echo configuration
        raise PlannerConfigError(f"Could not initialise the Gemini client ({type(exc).__name__}).") from exc


def create_anthropic_client(settings: Settings) -> anthropic.Anthropic:
    problem = claude_config_problem(settings)
    if problem:
        raise PlannerConfigError(problem)
    key = anthropic_api_key(settings)
    register_secret(key)  # scrubbed from every log record and trace
    try:
        # max_retries=0: retries are handled by the graph's planner retry edge.
        return anthropic.Anthropic(api_key=key, timeout=settings.planner_timeout_seconds, max_retries=0)
    except Exception as exc:  # never include exc text: it could echo configuration
        raise PlannerConfigError(f"Could not initialise the Anthropic client ({type(exc).__name__}).") from exc


def create_groq_client(settings: Settings):
    problem = groq_config_problem(settings)
    if problem:
        raise PlannerConfigError(problem)
    import groq

    key = groq_api_key(settings)
    register_secret(key)  # scrubbed from every log record and trace
    try:
        # max_retries=0: the planning graph owns retry and fallback policy.
        return groq.Groq(api_key=key, timeout=settings.planner_timeout_seconds, max_retries=0)
    except Exception as exc:  # never include exc text: it could echo configuration
        raise PlannerConfigError(f"Could not initialise the Groq client ({type(exc).__name__}).") from exc


def _documents(settings: Settings):
    # Execution is mock-only: tell the model which synthetic documents exist so it does not
    # invent names that would be rejected. Names only, never document contents.
    return frozenset(SYNTHETIC_DRIVE_DOCUMENTS) if mock_documents_apply(settings) else None


def create_provider_planner(settings: Settings, provider: PlannerProvider) -> Planner:
    """One AI provider's workflow planner. Raises PlannerConfigError naming the missing setting."""
    if provider is PlannerProvider.GEMINI:
        return GeminiPlanner(client=create_gemini_client(settings), model=settings.gemini_model,
                             max_output_tokens=settings.gemini_max_output_tokens,
                             available_documents=_documents(settings))
    if provider is PlannerProvider.GROQ:
        return GroqPlanner(client=create_groq_client(settings), model=settings.groq_model,
                           max_tokens=settings.groq_max_tokens, available_documents=_documents(settings))
    return ClaudePlanner(client=create_anthropic_client(settings), model=settings.anthropic_model,
                         max_tokens=settings.anthropic_max_tokens, available_documents=_documents(settings))


def build_auto_chain(settings: Settings, build: Callable[[Settings, PlannerProvider], Any]) -> list:
    """[primary, fallback] built with ``build(settings, provider)``. The primary must be
    configured (PlannerConfigError otherwise); an unconfigured fallback is skipped with a
    warning so the primary still works - /integrations/status reports it."""
    providers = [build(settings, settings.primary_planner)]
    if settings.fallback_planner is not None:
        try:
            providers.append(build(settings, settings.fallback_planner))
        except PlannerConfigError as exc:
            logger.warning("fallback planner %s unavailable: %s", settings.fallback_planner.value, exc)
    return providers


def create_planner(settings: Settings) -> Planner:
    if settings.planner_mode is PlannerMode.MOCK:
        return MockPlanner()
    if settings.planner_mode is PlannerMode.AUTO:
        return FallbackPlanner(build_auto_chain(settings, create_provider_planner))
    if settings.planner_mode is PlannerMode.GEMINI:
        return create_provider_planner(settings, PlannerProvider.GEMINI)
    return create_provider_planner(settings, PlannerProvider.CLAUDE)
