"""Builds the configured planner from Settings."""

import anthropic

from app.agents.planners.base import Planner, PlannerConfigError
from app.agents.planners.claude import ClaudePlanner
from app.agents.planners.mock import MockPlanner
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.core.config import PlannerMode, Settings


def create_planner(settings: Settings) -> Planner:
    if settings.planner_mode is PlannerMode.MOCK:
        return MockPlanner()

    if not settings.anthropic_model:
        raise PlannerConfigError("PLANNER_MODE=claude requires ANTHROPIC_MODEL to be set.")
    try:
        # Credentials are resolved by the SDK from the environment (ANTHROPIC_API_KEY, ...).
        # max_retries=0: retries are handled by the graph's planner retry edge.
        client = anthropic.Anthropic(timeout=settings.planner_timeout_seconds, max_retries=0)
    except Exception as exc:  # never include exc text: it could echo configuration
        raise PlannerConfigError(
            f"Could not initialise the Anthropic client ({type(exc).__name__})."
        ) from exc
    return ClaudePlanner(
        client=client,
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        # Execution is mock-only: tell Claude which synthetic documents exist so it does not
        # invent names that would be rejected. Names only, never document contents.
        available_documents=frozenset(SYNTHETIC_DRIVE_DOCUMENTS) if settings.mock_execution_enabled else None,
    )
