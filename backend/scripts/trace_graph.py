"""Print the WorkflowState before the graph runs and after every node.

Uses the mock planner by default, so it needs no API key and makes no network calls.

    .\\.venv\\Scripts\\python.exe -m scripts.trace_graph
    .\\.venv\\Scripts\\python.exe -m scripts.trace_graph "Create a Jira issue and notify Teams"
    .\\.venv\\Scripts\\python.exe -m scripts.trace_graph --mermaid
"""

import json
import sys

from app.agents.graph import build_planning_graph, initial_state
from app.agents.planners import MockPlanner

DEFAULT_INSTRUCTION = (
    "Read meeting notes, create a Notion project page, save a summary to Google Drive, "
    "and notify the team in Microsoft Teams."
)


def main(argv: list[str]) -> None:
    graph = build_planning_graph(MockPlanner())
    if "--mermaid" in argv:
        print(graph.get_graph().draw_mermaid())
        return

    instruction = next((a for a in argv if not a.startswith("--")), DEFAULT_INSTRUCTION)
    state = initial_state(
        instruction, planner_name="mock", max_steps=10, max_planning_attempts=2,
        workflow_id="00000000-0000-4000-8000-000000000001",
    )
    print("=== initial state ===")
    print(json.dumps(state, indent=2))

    # stream_mode="updates" yields {node_name: partial_update} after each node runs.
    for update in graph.stream(state, stream_mode="updates"):
        for node, changes in update.items():
            print(f"\n=== after {node} (keys changed: {', '.join(changes)}) ===")
            print(json.dumps(changes, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
