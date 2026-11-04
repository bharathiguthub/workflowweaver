"""Builds the LangGraph planning graph.

    START -> planner --(draft ok)----------> validator --(valid)---> reporter -> END
                  \\--(retryable, attempts left)--> planner          \\--(invalid)--> error -> END
                   \\--(failed)--------------------------------------------------> error
"""

from collections.abc import Collection
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.nodes import PlanningNodes, route_after_planner, route_after_validator
from app.agents.planners.base import Planner
from app.agents.state import WorkflowState
from app.observability import traced_node


def build_planning_graph(planner: Planner, *, available_documents: Collection[str] | None = None) -> CompiledStateGraph:
    nodes = PlanningNodes(planner, available_documents=available_documents)
    graph = StateGraph(WorkflowState)

    # Each node is wrapped so every transition is a telemetry observation (no-op if disabled).
    graph.add_node("planner", traced_node("planning", "planner", nodes.planner_node))
    graph.add_node("validator", traced_node("planning", "validator", nodes.validator_node))
    graph.add_node("reporter", traced_node("planning", "reporter", nodes.reporter_node))
    graph.add_node("error", traced_node("planning", "error", nodes.error_node))

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"validator": "validator", "planner": "planner", "error": "error"},
    )
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
    max_planning_attempts: int,
    workflow_name: str | None = None,
    workflow_id: str | None = None,
) -> WorkflowState:
    return WorkflowState(
        workflow_id=workflow_id or str(uuid4()),
        workflow_name=workflow_name,
        user_input=instruction,
        planner_name=planner_name,
        current_step="start",
        status="pending",
        planning_attempts=0,
        last_error_retryable=False,
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


def recursion_limit_for(max_planning_attempts: int) -> int:
    # Each attempt is one planner super-step, plus validator + reporter/error, plus headroom.
    return max_planning_attempts + 5
