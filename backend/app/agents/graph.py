"""Builds the LangGraph planning graph.

    START -> planner --(draft ok)----------> validator --(valid)---> reporter -> END
                  \\--(retryable, attempts left)--> planner          \\--(invalid)--> error -> END
                   \\--(outage, attempts exhausted, fallback left)--> fallback -> planner
                    \\--(failed)-------------------------------------------------> error

The fallback edge is taken only with PLANNER_MODE=auto (a FallbackPlanner with 2+ providers).
"""

import random
import time
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.nodes import PlanningNodes, route_after_planner, route_after_validator
from app.agents.planners.base import Planner
from app.agents.state import WorkflowState
from app.schemas.plan import target_integrations
from app.observability import traced_node

if TYPE_CHECKING:
    from app.agents.execution.retry import RetryPolicy


def build_planning_graph(
    planner: Planner,
    *,
    available_documents: Collection[str] | None = None,
    backoff: "RetryPolicy | None" = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> CompiledStateGraph:
    nodes = PlanningNodes(planner, available_documents=available_documents, backoff=backoff, sleeper=sleeper, rng=rng)
    graph = StateGraph(WorkflowState)

    # Each node is wrapped so every transition is a telemetry observation (no-op if disabled).
    graph.add_node("planner", traced_node("planning", "planner", nodes.planner_node))
    graph.add_node("fallback", traced_node("planning", "fallback", nodes.fallback_node))
    graph.add_node("validator", traced_node("planning", "validator", nodes.validator_node))
    graph.add_node("reporter", traced_node("planning", "reporter", nodes.reporter_node))
    graph.add_node("error", traced_node("planning", "error", nodes.error_node))

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"validator": "validator", "planner": "planner", "fallback": "fallback", "error": "error"},
    )
    graph.add_edge("fallback", "planner")
    graph.add_conditional_edges(
        "validator", route_after_validator, {"reporter": "reporter", "error": "error"}
    )
    graph.add_edge("reporter", END)
    graph.add_edge("error", END)
    return graph.compile()


def initial_state(
    instruction: str,
    *,
    planner_name: str,
    max_steps: int,
    planner_model: str | None = None,
    provider_count: int = 1,
    integration_target: str | None = None,
    drive_folders: list[str] | None = None,
    notion_parents: list[str] | None = None,
    teams_channels: list[str] | None = None,
    max_planning_attempts: int,
    workflow_name: str | None = None,
    workflow_id: str | None = None,
) -> WorkflowState:
    return WorkflowState(
        workflow_id=workflow_id or str(uuid4()),
        workflow_name=workflow_name,
        user_input=instruction,
        planner_name=planner_name,
        planner_model=planner_model,
        integration_target=integration_target,
        allowed_integrations=sorted(allowed) if (allowed := target_integrations(integration_target)) else None,
        allowed_drive_folders=sorted(drive_folders) if drive_folders is not None else None,
        allowed_notion_parents=sorted(notion_parents) if notion_parents is not None else None,
        allowed_teams_channels=sorted(teams_channels) if teams_channels is not None else None,
        plan_adjustments=[],
        current_step="start",
        status="pending",
        planning_attempts=0,
        retry_delays=[],
        last_error_retryable=False,
        provider_index=0,
        provider_count=provider_count,
        provider_attempts=0,
        last_error_provider_unavailable=False,
        fallback_used=False,
        fallback_reason=None,
        provider_attempt_log=[],
        draft_plan=None,
        planned_steps=[],
        plan_summary=None,
        validation_issues=[],
        errors=[],
        results={},
        max_steps=max_steps,
        max_planning_attempts=max_planning_attempts,
        metadata={"phase": 2, "executed": False},
    )


def recursion_limit_for(max_planning_attempts: int, provider_count: int = 1) -> int:
    # Each attempt is one planner super-step (per provider), one fallback step per extra
    # provider, plus validator + reporter/error, plus headroom.
    return max_planning_attempts * provider_count + (provider_count - 1) + 5
