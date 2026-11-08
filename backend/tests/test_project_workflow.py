"""Phase 5.3 business workflow: AI project plan -> Notion project page + action item pages.

Claude is replaced by FakeAnthropicClient and Notion by the fake MCP server built from the
LIVE-captured @notionhq/notion-mcp-server 2.6.1 tools/list. No real service is called."""

from uuid import uuid4

import anthropic
import httpx2
import jsonschema
import pytest
from fastapi.testclient import TestClient

from app.agents.execution.tools import MockToolRegistry
from app.agents.execution.tools.base import Effect, ToolCall, ToolError
from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.planners.base import PlannerError
from app.agents.project_workflow import (
    EXAMPLE_INSTRUCTION,
    ActionItemDraft,
    ClaudeProjectPlanner,
    MockProjectPlanner,
    ProjectPlanDraft,
    compile_project,
    validate_project,
)
from app.agents.validation import validate_draft
from app.api import integrations as integrations_api
from app.api import project as project_api
from app.main import app
from app.schemas.execution import ExecutionRequest, ExecutionStatus
from app.schemas.plan import Action, DraftPlan, DraftStep, StepArgument
from app.schemas.workflow import Integration
from tests.conftest import FakeAnthropicClient, fake_response
from tests.integration_fakes import (
    NOTION_FIXTURE,
    NOTION_PAGE_ID,
    SECRETS,
    FakeWorld,
    integration_settings,
    notion_error,
)

PLAN_URL = "/api/v1/workflows/notion-project/plan"
API_KEY = "synthetic-anthropic-key-0009"
REQUEST = httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")


def rewards_project(**overrides) -> ProjectPlanDraft:
    items = [
        ActionItemDraft(title="Customer registration", description="Let customers sign up and verify their email.",
                        priority="High", owner="Backend team"),
        ActionItemDraft(title="Reward balance lookup", description="Show the current points balance in the app.",
                        priority="High", owner="Frontend team"),
        ActionItemDraft(title="Points redemption", description="Redeem points for rewards with balance checks.",
                        priority="Medium", owner="Backend team"),
        ActionItemDraft(title="Transaction history", description="List earn and redeem transactions per customer.",
                        priority="Low", owner="Data team"),
    ]
    values = {"project_title": "Customer Rewards Platform - Project Plan",
              "overview": "Build a loyalty platform where customers earn and redeem reward points.",
              "action_items": items, **overrides}
    return ProjectPlanDraft(**values)


def settings_for(mode="real", **overrides):
    values = dict(smoke_test_notion_parent="engineering", notion_real_writes_sandbox_only=True,
                  planner_mode="claude", anthropic_model="test-model-from-config", anthropic_api_key=API_KEY)
    values.update(overrides)
    return integration_settings(mode, **values)


def assert_no_secrets(text: str) -> None:
    leaked = [s for s in [*SECRETS, API_KEY] if s in text]
    assert not leaked, f"secret values leaked: {leaked}"


@pytest.fixture
def endpoint(monkeypatch):
    """Patch settings and the planner source; returns the fake Anthropic client."""

    def install(outcome=None, mode="real", **overrides):
        settings = settings_for(mode, **overrides)
        monkeypatch.setattr(project_api, "get_settings", lambda: settings)
        monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
        client = FakeAnthropicClient(outcome if outcome is not None else fake_response(rewards_project()))

        def factory(s, m):
            if s.planner_mode.value == "claude":
                return ClaudeProjectPlanner(client=client, model=s.anthropic_model, max_tokens=1024)
            return project_api.default_source(s, m)

        app.dependency_overrides[project_api.get_project_source_factory] = lambda: factory
        return client

    yield install
    app.dependency_overrides.clear()


def execution_request(plan: dict, mode="real") -> ExecutionRequest:
    return ExecutionRequest.model_validate({
        "workflow_id": plan["workflow_id"],
        "plan": {"summary": plan["summary"], "steps": [
            {k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
            for s in plan["planned_steps"]]},
        "approval": {"approved": True, "mode": mode, "approved_by": "synthetic.reviewer"},
    })


def compiled_request(mode="real", project=None, parent="engineering") -> ExecutionRequest:
    plan, issues = validate_draft(compile_project(project or rewards_project(), parent), max_steps=10)
    assert plan is not None, issues
    return ExecutionRequest.model_validate({
        "workflow_id": str(uuid4()), "plan": plan.model_dump(mode="json"),
        "approval": {"approved": True, "mode": mode, "approved_by": "synthetic.reviewer"}})


def real_world(**overrides) -> FakeWorld:
    return FakeWorld("real", **{"smoke_test_notion_parent": "engineering",
                                "notion_real_writes_sandbox_only": True, **overrides})


# ======================= Claude planner =======================


def test_claude_project_planner_uses_structured_outputs_and_safe_prompt():
    client = FakeAnthropicClient(fake_response(rewards_project()))
    planner = ClaudeProjectPlanner(client=client, model="test-model-from-config", max_tokens=1024)
    project = planner.project_plan(EXAMPLE_INSTRUCTION, max_items=9)
    assert project == rewards_project()
    call = client.messages.calls[0]
    assert call["output_format"] is ProjectPlanDraft and call["model"] == "test-model-from-config"
    assert "<instruction>" in call["messages"][0]["content"]
    system = call["system"]
    assert "at most 9" in system and "TEAM, never a person" in system and "deadlines" in system
    assert "untrusted" in system


def test_claude_errors_keep_their_codes():
    error = anthropic.AuthenticationError("synthetic", response=httpx2.Response(401, request=REQUEST), body=None)
    planner = ClaudeProjectPlanner(client=FakeAnthropicClient(error), model="m", max_tokens=1024)
    with pytest.raises(PlannerError) as info:
        planner.project_plan(EXAMPLE_INSTRUCTION, max_items=9)
    assert info.value.code == "PLANNER_AUTH_ERROR" and not info.value.retryable


def test_mock_project_planner_is_labelled_not_ai_and_extracts_features():
    planner = MockProjectPlanner()
    project = planner.project_plan(EXAMPLE_INSTRUCTION, max_items=9)
    assert planner.name == "mock (not AI)" and planner.model is None
    assert [i.title for i in project.action_items] == [
        "Customer registration", "Reward balance lookup", "Points redemption", "Transaction history"]
    assert "(mock)" in project.project_title


# ======================= business validation =======================


@pytest.mark.parametrize("item_update, code", [
    ({"owner": "Alice Johnson"}, "OWNER_NOT_A_TEAM"),
    ({"owner": "jane.doe@example.com"}, "OWNER_NOT_A_TEAM"),
    ({"description": "Ship by Friday."}, "DEADLINE_NOT_ALLOWED"),
    ({"description": "Due date 2026-12-01."}, "DEADLINE_NOT_ALLOWED"),
    ({"description": "Launch on March 3rd."}, "DEADLINE_NOT_ALLOWED"),
    ({"description": "Run rm -rf / after deploy."}, "UNSAFE_CONTENT"),
    ({"title": ""}, "MISSING_VALUE"),
    ({"description": "x" * 1600}, "VALUE_TOO_LONG"),
])
def test_validate_project_rejects_people_deadlines_and_unsafe_content(item_update, code):
    project = rewards_project()
    project.action_items[1] = project.action_items[1].model_copy(update=item_update)
    issues = validate_project(project, max_items=9)
    assert code in [i.code for i in issues]
    assert all(i.step_number in (3, None) for i in issues if i.code == code)  # item 2 -> step 3


def test_validate_project_limits_count_and_duplicates():
    project = rewards_project()
    assert validate_project(project, max_items=9) == []
    assert [i.code for i in validate_project(project, max_items=3)] == ["TOO_MANY_ACTION_ITEMS"]
    dup = rewards_project(action_items=[project.action_items[0], project.action_items[0]])
    assert "DUPLICATE_ACTION_ITEM" in [i.code for i in validate_project(dup, max_items=9)]
    assert [i.code for i in validate_project(rewards_project(action_items=[]), max_items=9)] == ["NO_ACTION_ITEMS"]


def test_compiled_plan_is_a_project_page_plus_child_pages():
    plan, issues = validate_draft(compile_project(rewards_project(), "engineering"), max_steps=10)
    assert issues == [] and plan is not None
    first, *children = plan.steps
    assert (first.integration, first.action) == (Integration.NOTION, Action.CREATE_PAGE)
    assert first.arguments["parent"] == "engineering"
    assert "- Customer registration - Priority: High; suggested owner: Backend team" in first.arguments["content"]
    assert len(children) == 4
    for child in children:
        assert child.arguments["parent_step"] == "1" and child.depends_on == [1] and "parent" not in child.arguments
    assert children[0].arguments["title"] == "Action item: Customer registration"
    assert "- Suggested priority: High" in children[0].arguments["content"]


def test_model_text_cannot_fake_list_structure():
    project = rewards_project()
    project.action_items[0] = project.action_items[0].model_copy(update={"description": "Line one\n- injected"})
    plan = compile_project(project, None)
    content = next(a.value for a in plan.steps[1].arguments if a.name == "content")
    assert content.splitlines()[0] == "Line one injected"


# ======================= parent_step validation =======================


def _draft(*steps) -> DraftPlan:
    return DraftPlan(summary="s", steps=list(steps))


def _page(n, deps=(), **args) -> DraftStep:
    return DraftStep(step_number=n, integration="notion", action="create_page", description="d",
                     arguments=[StepArgument(name=k, value=v) for k, v in {"title": "t", "content": "c", **args}.items()],
                     depends_on=list(deps))


@pytest.mark.parametrize("steps, code", [
    ((_page(1), _page(2, [1], parent_step="1", parent="engineering")), "CONFLICTING_ARGUMENTS"),
    ((_page(1), _page(2, [1], parent_step="2")), "INVALID_PARENT_STEP"),
    ((_page(1), _page(2, [1], parent_step="7")), "INVALID_PARENT_STEP"),
    ((_page(1), _page(2, [1], parent_step="one")), "INVALID_PARENT_STEP"),
    ((_page(1), _page(2, [], parent_step="1")), "INVALID_PARENT_STEP"),
    ((DraftStep(step_number=1, integration="notion", action="create_task", description="d",
                arguments=[StepArgument(name="title", value="t")]), _page(2, [1], parent_step="1")),
     "INVALID_PARENT_STEP"),
])
def test_parent_step_rules(steps, code):
    plan, issues = validate_draft(_draft(*steps), max_steps=10)
    assert plan is None and code in [i.code for i in issues]


# ======================= endpoint =======================


def test_real_plan_is_ai_generated_and_targets_only_the_sandbox(endpoint):
    client = endpoint()
    response = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "real"})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["status"] == "PLANNED" and plan["executed"] is False
    assert plan["planner"] == "claude" and plan["ai_generated"] is True and plan["model"] == "test-model-from-config"
    assert plan["real_write"] is True and plan["destination_alias"] == "engineering" and plan["page_count"] == 5
    assert [i["owner"] for i in plan["project"]["action_items"]] == ["Backend team", "Frontend team",
                                                                     "Backend team", "Data team"]
    assert plan["planned_steps"][0]["arguments"]["parent"] == "engineering"
    assert "API-patch-page" in plan["rollback"]
    assert len(client.messages.calls) == 1
    assert_no_secrets(response.text)


def test_named_person_from_claude_is_rejected_before_approval(endpoint):
    project = rewards_project()
    project.action_items[2] = project.action_items[2].model_copy(update={"owner": "Priya Sharma"})
    endpoint(fake_response(project))
    response = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "real"})
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "INVALID" and body["planned_steps"] == [] and body["real_write"] is False
    assert [(i["code"], i["step_number"]) for i in body["validation"]["issues"]] == [("OWNER_NOT_A_TEAM", 4)]
    assert body["project"]["action_items"][2]["owner"] == "Priya Sharma"  # shown so the user sees why


def test_planner_retry_then_success(endpoint, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(project_api, "get_planning_sleeper", lambda: slept.append)
    outcomes = iter([anthropic.APITimeoutError(request=REQUEST), fake_response(rewards_project())])

    def next_outcome():
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    endpoint(next_outcome)
    body = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "real"}).json()
    assert body["status"] == "PLANNED" and body["planning_attempts"] == 2
    assert len(slept) == 1 and 0.5 <= slept[0] <= 1.0  # default backoff: base 1 s, equal jitter
    assert body["retry_delays_seconds"] == slept


def test_planner_failure_is_reported_not_fabricated(endpoint):
    error = anthropic.AuthenticationError("synthetic", response=httpx2.Response(401, request=REQUEST), body=None)
    endpoint(error)
    response = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "real"})
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "FAILED" and body["planned_steps"] == [] and body["project"] is None
    assert [e["code"] for e in body["errors"]] == ["PLANNER_AUTH_ERROR"]


@pytest.mark.parametrize("overrides, setting", [
    ({"planner_mode": "mock"}, "PLANNER_MODE"),
    ({"anthropic_api_key": None}, "ANTHROPIC_API_KEY"),
    ({"anthropic_model": None}, "ANTHROPIC_MODEL"),
])
def test_real_mode_requires_claude_with_explicit_configuration_error(monkeypatch, overrides, setting):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = settings_for("real", **overrides)
    monkeypatch.setattr(project_api, "get_settings", lambda: settings)
    response = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "real"})
    assert response.status_code == 503 and setting in response.json()["detail"]


def test_mock_mode_keeps_working_without_claude_and_says_so(monkeypatch):
    settings = settings_for("mock", planner_mode="mock", anthropic_api_key=None)
    monkeypatch.setattr(project_api, "get_settings", lambda: settings)
    response = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "mock"})
    assert response.status_code == 200
    body = response.json()
    assert body["planner"] == "mock (not AI)" and body["ai_generated"] is False and body["model"] is None
    assert body["page_count"] == 5 and body["destination_alias"] is None and body["real_write"] is False


@pytest.mark.parametrize("overrides, status_code, text", [
    ({"integration_mode": "mock"}, 409, "allows modes up to"),
    ({"smoke_test_notion_parent": None}, 503, "SMOKE_TEST_NOTION_PARENT"),
    ({"real_writes_enabled": False}, 503, "REAL_WRITES_ENABLED"),
])
def test_real_plan_needs_a_ready_sandbox(monkeypatch, overrides, status_code, text):
    settings = settings_for("real", **overrides)
    monkeypatch.setattr(project_api, "get_settings", lambda: settings)
    response = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "real"})
    assert response.status_code == status_code and text in response.json()["detail"]


def test_status_reports_planner_readiness_without_the_key(monkeypatch):
    settings = settings_for("real")
    monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
    response = TestClient(app).get("/api/v1/integrations/status")
    assert response.json()["planner"] == {"mode": "claude", "model": "test-model-from-config", "ai_ready": True,
                                          "claude_ready": True, "issue": None}
    assert_no_secrets(response.text)


# ======================= execution against the fake Notion MCP server =======================


def _block_validator():
    schema = next(t for t in NOTION_FIXTURE["tools"] if t["name"] == "API-post-page")["inputSchema"]
    return jsonschema.Draft202012Validator({"$ref": "#/$defs/blockObjectRequest", "$defs": schema["$defs"]})


def test_real_execution_creates_project_page_then_child_action_items(endpoint):
    endpoint()
    plan = TestClient(app).post(PLAN_URL, json={"instruction": EXAMPLE_INSTRUCTION, "mode": "real"}).json()
    world = real_world()
    response = world.service.execute(execution_request(plan), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED, response.errors
    posts = world.notion.calls_named("API-post-page")
    assert len(posts) == 5
    project_id = response.step_results[0].resource_id
    assert posts[0]["parent"] == {"page_id": NOTION_PAGE_ID}  # the sandbox parent, resolved server-side
    assert all(p["parent"] == {"page_id": project_id} for p in posts[1:])  # children inside the project page
    validator = _block_validator()
    kinds = set()
    for post in posts:
        for block in post["children"]:
            validator.validate(block)  # only verified block types (strict, additionalProperties: false)
            kinds.add(block["type"])
    assert kinds == {"paragraph", "bulleted_list_item"}
    for result in response.step_results:
        assert result.resource_id and result.output["url"].startswith("https://www.notion.so/")
    assert response.step_results[1].output["destination"] == "page from step 1"
    assert_no_secrets(response.model_dump_json())


def test_partial_failure_rolls_back_every_page_newest_first():
    world = real_world()
    original = world.notion._api_post_page
    calls = {"n": 0}

    def post(args):
        calls["n"] += 1
        return notion_error(400, "validation_error") if calls["n"] == 4 else original(args)

    world.notion._api_post_page = post
    response = world.service.execute(compiled_request(), write_authorized=True)
    assert response.status is ExecutionStatus.ROLLED_BACK
    assert [r.status.value for r in response.step_results] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "FAILED",
                                                              "SKIPPED"]
    assert response.step_results[3].error.code == "NOTION_API_ERROR"
    assert [c.step_number for c in response.compensation_results] == [3, 2, 1]
    assert all(c.status.value == "COMPENSATED" for c in response.compensation_results)
    trashed = [c["page_id"] for c in world.notion.calls_named("API-patch-page")]
    assert trashed == [response.step_results[i].resource_id for i in (2, 1, 0)]
    assert all(world.notion.resources[pid]["in_trash"] for pid in trashed)


def test_rate_limited_action_item_is_retried():
    world = real_world()
    original = world.notion._api_post_page
    calls = {"n": 0}

    def post(args):
        calls["n"] += 1
        return notion_error(429, "rate_limited") if calls["n"] == 2 else original(args)

    world.notion._api_post_page = post
    response = world.service.execute(compiled_request(), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED
    assert response.attempts["2"] == 2 and len(response.retry_delays_seconds) == 1
    assert len([p for p in world.notion.resources if p != NOTION_PAGE_ID]) == 5  # no duplicate page


def test_kill_switch_and_operator_token_still_gate_project_plans():
    off = real_world(real_writes_enabled=False)
    assert [e.code for e in off.service.execute(compiled_request(), write_authorized=True).errors] == [
        "REAL_WRITES_DISABLED"]
    no_token = real_world()
    assert [e.code for e in no_token.service.execute(compiled_request(), write_authorized=False).errors] == [
        "REAL_WRITE_AUTHORIZATION_REQUIRED"]
    assert off.remote_calls() == 0 and no_token.remote_calls() == 0


def test_sandbox_only_refuses_other_destinations_before_any_call():
    world = real_world()
    response = world.service.execute(compiled_request(parent="tasks"), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]
    assert "SMOKE_TEST_NOTION_PARENT" in response.validation.issues[0].message
    task = ExecutionRequest.model_validate({
        "workflow_id": str(uuid4()),
        "plan": {"summary": "s", "steps": [{"step_number": 1, "integration": "notion", "action": "create_task",
                                            "description": "d", "arguments": {"title": "t"}, "depends_on": []}]},
        "approval": {"approved": True, "mode": "real", "approved_by": "synthetic.reviewer"}})
    assert world.service.execute(task, write_authorized=True).status is ExecutionStatus.REJECTED
    assert world.remote_calls() == 0


def test_child_page_without_a_created_parent_is_refused():
    world = real_world()
    adapter = world.registry.adapters[Integration.NOTION]
    call = ToolCall("wf", 2, Integration.NOTION, Action.CREATE_PAGE,
                    {"title": "t", "content": "c", "parent_step": "1"}, write_authorized=True)
    with pytest.raises(ToolError) as info:
        adapter.execute(call)
    assert info.value.code == "PARENT_NOT_AVAILABLE" and world.remote_calls() == 0


def test_dry_run_builds_child_requests_and_sends_nothing():
    world = FakeWorld("dry_run", smoke_test_notion_parent="engineering", notion_real_writes_sandbox_only=True)
    response = world.service.execute(compiled_request("dry_run"))
    assert response.status is ExecutionStatus.COMPLETED
    assert all(r.output["sent"] is False and r.output["would_call"] == "API-post-page" for r in response.step_results)
    assert world.remote_calls() == 0 and world.factories_called == []


def test_mock_execution_of_the_compiled_plan():
    service = ExecutionService(MockToolRegistry(), RetryPolicy(max_attempts=2, base_delay_seconds=0, jitter=False),
                               max_steps=10, sleeper=lambda _: None)
    response = service.execute(compiled_request("mock", parent=None))
    assert response.status is ExecutionStatus.COMPLETED and len(response.step_results) == 5
    assert service.registry.effect_of(Integration.NOTION, Action.CREATE_PAGE) is Effect.REVERSIBLE

