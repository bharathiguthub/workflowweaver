"""Builds the LangGraph execution graph (mock, dry-run or real tools; see ToolRegistry).

    START -> prepare --(rejected)--------------------------------------------> finalize -> END
                \\--(running)--> execute_step --(succeeded, more steps)--> execute_step
                                     |  \\--(succeeded, last step)------------> finalize
                                     |  \\--(transient, attempts left)--> retry_wait -> execute_step
                                     \\--(failed, journal not empty)--> compensate -> finalize
                                      \\--(failed, nothing to undo)-------------------> finalize
"""

import random
import time

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.execution.nodes import ExecutionNodes, Sleeper, route_after_execute, route_after_prepare
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.state import ExecutionState
from app.agents.execution.tools.registry import ToolRegistry
from app.observability import traced_node


def build_execution_graph(
    registry: ToolRegistry,
    policy: RetryPolicy,
    *,
    sleeper: Sleeper = time.sleep,
    rng: random.Random | None = None,
    document_precheck: bool = True,
) -> CompiledStateGraph:
    nodes = ExecutionNodes(registry, policy, sleeper, rng, document_precheck=document_precheck)
    graph = StateGraph(ExecutionState)

    # Every node transition is a telemetry observation (no-op when Langfuse is disabled).
    for name, fn in (
        ("prepare", nodes.prepare_node),
        ("execute_step", nodes.execute_step_node),
        ("retry_wait", nodes.retry_wait_node),
        ("compensate", nodes.compensate_node),
        ("finalize", nodes.finalize_node),
    ):
        graph.add_node(name, traced_node("execution", name, fn))

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare", route_after_prepare, {"execute_step": "execute_step", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "execute_step",
        route_after_execute,
        {
            "execute_step": "execute_step",
            "retry_wait": "retry_wait",
            "compensate": "compensate",
            "finalize": "finalize",
        },
    )
    graph.add_edge("retry_wait", "execute_step")
    graph.add_edge("compensate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


def initial_execution_state(
    *,
    workflow_id: str,
    raw_plan: dict,
    approved: bool,
    approved_by: str,
    max_steps: int,
    max_attempts: int,
    mode: str = "mock",
    requested_mode: str = "mock",
    write_authorized: bool = False,
) -> ExecutionState:
    return ExecutionState(
        workflow_id=workflow_id,
        approved=approved,
        approved_by=approved_by,
        raw_plan=raw_plan,
        mode=mode,
        requested_mode=requested_mode,
        write_authorized=write_authorized,
        steps=[],
        validation_issues=[],
        current_step="start",
        status="pending",
        current_index=0,
        last_outcome="none",
        attempts={},
        failed_step=None,
        step_results=[],
        journal=[],
        errors=[],
        retry_delays=[],
        compensation_results=[],
        final_status=None,
        message="",
        max_steps=max_steps,
        max_attempts=max_attempts,
    )


def execution_recursion_limit(step_count: int, max_attempts: int) -> int:
    # Per step: max_attempts execute + (max_attempts - 1) retry_wait super-steps.
    # Plus prepare, compensate, finalize and headroom.
    return max(step_count, 1) * 2 * max_attempts + 10
