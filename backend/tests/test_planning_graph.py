from app.agents.graph import build_planning_graph, initial_state
from app.agents.nodes import route_after_planner, route_after_validator
from app.agents.planners import MockPlanner, PlannerError
from app.agents.service import PlanningService
from app.schemas.plan import DraftPlan, PlanStatus
from tests.conftest import EXAMPLE_INSTRUCTION, StubPlanner, make_step, timeout_error, valid_draft


def run_nodes(planner, instruction=EXAMPLE_INSTRUCTION, max_attempts=2):
    """Run the graph and return the ordered list of nodes it visited plus the final state."""
    graph = build_planning_graph(planner)
    state = initial_state(
        instruction, planner_name=planner.name, max_steps=10, max_planning_attempts=max_attempts
    )
    visited = []
    final = dict(state)
    for update in graph.stream(state, stream_mode="updates"):
        for node, changes in update.items():
            visited.append(node)
            for key, value in changes.items():
                final[key] = final.get(key, []) + value if key == "errors" else value
    return visited, final


# ---------- mock planner ----------


def test_mock_planner_example_instruction_produces_expected_three_steps():
    draft = MockPlanner().plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert [(s.integration, s.action) for s in draft.steps] == [
        ("notion", "create_page"),
        ("google_drive", "create_document"),
        ("teams", "send_message"),
    ]
    assert draft.steps[2].depends_on == [1, 2]


def test_mock_planner_is_deterministic():
    a = MockPlanner().plan(EXAMPLE_INSTRUCTION, max_steps=10)
    b = MockPlanner().plan(EXAMPLE_INSTRUCTION, max_steps=10)
    assert a == b


def test_mock_planner_maps_notion_tasks_and_drive_reads():
    draft = MockPlanner().plan("Fetch the notes from Google Drive then create tasks in Notion", max_steps=10)
    assert [(s.action, s.depends_on) for s in draft.steps] == [("read_document", []), ("create_task", [1])]


# ---------- conditional routing (pure functions) ----------


def test_route_after_planner():
    base = {"planning_attempts": 1, "max_planning_attempts": 2, "last_error_retryable": False}
    assert route_after_planner({**base, "status": "validating"}) == "validator"
    assert route_after_planner({**base, "status": "planning_failed", "last_error_retryable": True}) == "planner"
    assert route_after_planner({**base, "status": "planning_failed"}) == "error"
    exhausted = {**base, "status": "planning_failed", "last_error_retryable": True, "planning_attempts": 2}
    assert route_after_planner(exhausted) == "error"


def test_route_after_validator():
    assert route_after_validator({"status": "valid"}) == "reporter"
    assert route_after_validator({"status": "invalid"}) == "error"


# ---------- full graph paths ----------


def test_valid_path_visits_planner_validator_reporter():
    visited, final = run_nodes(MockPlanner())
    assert visited == ["planner", "validator", "reporter"]
    assert final["status"] == "planned"
    assert final["results"]["execution"] == "not_executed"
    assert final["results"]["integrations"] == ["google_drive", "notion", "teams"]


def test_invalid_plan_routes_to_error_node():
    visited, final = run_nodes(MockPlanner(), "Create a Notion page and post the update to Slack")
    assert visited == ["planner", "validator", "error"]
    assert final["status"] == "invalid"
    assert [i["code"] for i in final["validation_issues"]] == ["UNSUPPORTED_INTEGRATION"]
    assert final["errors"][-1]["code"] == "PLAN_VALIDATION_FAILED"


def test_shell_request_is_rejected_not_executed():
    visited, final = run_nodes(MockPlanner(), "Run a shell command to clean the server, then notify Teams")
    assert visited[-1] == "error"
    assert final["status"] == "invalid"


def test_retryable_planner_error_is_retried_then_succeeds():
    planner = StubPlanner(timeout_error(), valid_draft())
    visited, final = run_nodes(planner)
    assert visited == ["planner", "planner", "validator", "reporter"]
    assert final["planning_attempts"] == 2
    assert final["status"] == "planned"
    assert [e["code"] for e in final["errors"]] == ["PLANNER_TIMEOUT"]  # recovered error is kept


def test_retries_are_capped():
    planner = StubPlanner(timeout_error())
    visited, final = run_nodes(planner, max_attempts=3)
    assert visited == ["planner", "planner", "planner", "error"]
    assert planner.calls == 3
    assert final["status"] == "failed"


def test_non_retryable_error_goes_straight_to_error():
    planner = StubPlanner(PlannerError("PLANNER_AUTH_ERROR", "bad credentials"))
    visited, final = run_nodes(planner)
    assert visited == ["planner", "error"]
    assert planner.calls == 1
    assert final["status"] == "failed"


def test_unexpected_planner_exception_fails_closed():
    visited, final = run_nodes(StubPlanner(RuntimeError("boom")))
    assert visited == ["planner", "error"]
    assert final["errors"][0]["code"] == "PLANNER_ERROR"


def test_service_response_hides_rejected_draft_steps():
    bad = DraftPlan(summary="s", steps=[make_step(1, "slack", "send_message", message="hi")])
    response = PlanningService(StubPlanner(bad), max_steps=10, max_planning_attempts=2).plan(EXAMPLE_INSTRUCTION)
    assert response.status is PlanStatus.INVALID
    assert response.planned_steps == []
    assert response.validation.is_valid is False
    assert response.executed is False
