"""Regression: Notion-only "Customer Rewards Platform" plan rejected by the validator.

Reported (Gemini PLANNER_API_ERROR -> Groq fallback openai/gpt-oss-120b, 4 attempts):
  steps 2-5  UNKNOWN_ARGUMENT  notion.create_page got `parent_page` (accepted: content, parent,
             parent_step, title)
  step 6     UNSUPPORTED_ACTION  notion.read_page (allowed: create_page, create_task)

Root cause: the shared Gemini/Groq/Claude prompt listed the Notion argument names but never said
how a child page names its parent (parent_step = an earlier step NUMBER), had no Notion example,
and its general rule told the model to keep any unsupported action "so the validator can report
it" - so a requested read-back became notion.read_page. Fixed by (1) explicit Notion rules plus a
valid parent/child example in the shared prompt, (2) a deterministic normalizer that repairs
`parent_page` and drops a pure read-back step only when unambiguous (reported in
plan_adjustments), with (3) the strict validator unchanged afterwards and in the executor.

Gemini, Groq and Notion are fakes; no network call and no real write happens.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.agents.planners.claude import build_system_prompt, notion_example
from app.agents.validation import normalize_notion_references, validate_draft
from app.api import planning as planning_api
from app.main import app
from app.schemas.execution import ExecutionStatus, StepStatus
from app.schemas.plan import DraftPlan, PlanStatus
from tests.conftest import make_step
from tests.execution_helpers import make_request, make_service
from tests.test_gemini_planner import ok_response
from tests.test_planner_fallback import chain, gemini_503, groq_ok

PARENT_TITLE = "Customer Rewards Platform"
ITEMS = ["Customer registration", "Reward balance lookup", "Points redemption", "Transaction history"]


def page(n, depends_on=(), **arguments):
    return make_step(n, "notion", "create_page", depends_on, **arguments)


def reported_plan(parent_ref: str = "1", *, read_back: bool = True) -> DraftPlan:
    """The rejected plan's shape: 1 parent page, 4 children with parent_page, then read_page."""
    steps = [page(1, title=PARENT_TITLE, content="Project overview (synthetic).")]
    steps += [page(n, [1], title=t, content=f"- Priority: High\n- Owner: Backend team\n{t} (synthetic).",
                   parent_page=parent_ref) for n, t in enumerate(ITEMS, start=2)]
    if read_back:
        steps.append(make_step(6, "notion", "read_page", [1, 2, 3, 4, 5], page_title=PARENT_TITLE))
    return DraftPlan(summary="Customer Rewards Platform plan with four action items.", steps=steps)


def args(step) -> dict:
    return step["arguments"] if isinstance(step, dict) else step.arguments


def assert_hierarchy(steps) -> None:
    """One parent page and four child pages inside it (parent_step=1)."""
    assert len(steps) == 5
    first = steps[0]
    assert (first["action"] if isinstance(first, dict) else first.action.value) == "create_page"
    assert args(first)["title"] == PARENT_TITLE and "parent_step" not in args(first)
    for n, (child, title) in enumerate(zip(steps[1:], ITEMS), start=2):
        number = child["step_number"] if isinstance(child, dict) else child.step_number
        depends = child["depends_on"] if isinstance(child, dict) else child.depends_on
        assert number == n and args(child)["title"] == title
        assert args(child)["parent_step"] == "1" and "parent_page" not in args(child) and 1 in depends


# ======================= the reported failure: Gemini down, Groq fallback =======================


def test_reported_groq_fallback_plan_is_now_planned_with_parent_child_hierarchy():
    service, gemini, groq_client, _ = chain([gemini_503()], [groq_ok(reported_plan())])
    result = service.plan("Create the Customer Rewards Platform plan in Notion.", target="notion")
    assert result.status is PlanStatus.PLANNED, result.validation.issues
    assert result.planner == "groq" and result.fallback_used is True
    assert_hierarchy(result.planned_steps)
    assert all(s.action.value == "create_page" for s in result.planned_steps)  # read_page is gone
    notes = result.plan_adjustments
    assert len(notes) == 5
    for n in range(2, 6):
        assert notes[n - 2].startswith(f"Step {n} (notion.create_page): replaced unsupported argument "
                                       "'parent_page' with parent_step=1")
    assert "Step 6 (notion.read_page): removed" in notes[4] and "was NOT performed" in notes[4]
    # Both providers were told the Notion rules.
    for system in (gemini.models.calls[0]["config"].system_instruction,
                   groq_client.calls[0]["messages"][0]["content"]):
        assert "Never use parent_page" in system and "no read, get, retrieve" in system


def test_same_plan_from_gemini_is_normalized_identically():
    service, _, groq_client, _ = chain([ok_response(reported_plan())], [groq_ok()])
    result = service.plan("Create the Customer Rewards Platform plan in Notion.", target="notion")
    assert result.status is PlanStatus.PLANNED and result.planner == "gemini" and not groq_client.calls
    assert_hierarchy(result.planned_steps)


@pytest.mark.parametrize("ref", ["1", "step 1", "Step_1", "step #1", "{{step_1}}", "$step1", "step_1.id",
                                 PARENT_TITLE, PARENT_TITLE.lower()])
def test_parent_page_references_to_the_earlier_page_become_parent_step(ref):
    draft, notes = normalize_notion_references(reported_plan(ref, read_back=False), None)
    plan, issues = validate_draft(draft, max_steps=10)
    assert issues == [] and plan is not None
    assert_hierarchy(plan.steps)
    assert len(notes) == 4


def test_parent_page_as_approved_alias_becomes_parent():
    draft = DraftPlan(summary="s", steps=[page(1, title=PARENT_TITLE, content="c", parent_page="Engineering")])
    normalized, notes = normalize_notion_references(draft, ["engineering"])
    plan, issues = validate_draft(normalized, max_steps=10)
    assert issues == [] and args(plan.steps[0])["parent"] == "engineering"
    assert notes == ["Step 1 (notion.create_page): replaced unsupported argument 'parent_page' with "
                     "parent=engineering, an approved Notion parent page alias."]


# ======================= never guess: these stay rejected =======================


def rejected_codes(draft: DraftPlan, approved=None) -> list[str]:
    normalized, _ = normalize_notion_references(draft, approved)
    plan, issues = validate_draft(normalized, max_steps=10)
    assert plan is None
    return [i.code for i in issues]


@pytest.mark.parametrize("value", [
    "engineering",                                  # alias, but no approved list (mock / unknown)
    "production-workspace",                         # not an approved alias
    "0c9f3a5e2b7d4e61a8f0c3d2b1a09e87",             # raw page id
    "https://www.notion.so/Some-Page-0c9f3a5e2b7d", # URL
])
def test_unapproved_or_raw_parent_page_values_are_not_guessed(value):
    draft = DraftPlan(summary="s", steps=[page(1, title=PARENT_TITLE, content="c", parent_page=value)])
    assert rejected_codes(draft, approved=None if value == "engineering" else ["engineering"]) == ["UNKNOWN_ARGUMENT"]


def test_ambiguous_title_is_not_guessed():
    draft = DraftPlan(summary="s", steps=[
        page(1, title="Hub", content="c"), page(2, title="Hub", content="c"),
        page(3, [1, 2], title="Child", content="c", parent_page="Hub")])
    assert rejected_codes(draft) == ["UNKNOWN_ARGUMENT"]


def test_title_that_is_also_an_approved_alias_is_not_guessed():
    draft = DraftPlan(summary="s", steps=[
        page(1, title="Engineering", content="c"), page(2, [1], title="Child", content="c", parent_page="engineering")])
    assert rejected_codes(draft, ["engineering"]) == ["UNKNOWN_ARGUMENT"]


@pytest.mark.parametrize("draft", [
    # a reference to a LATER step
    DraftPlan(summary="s", steps=[page(1, title="A", content="c", parent_page="2"), page(2, title="B", content="c")]),
    # a reference to a step that does not create a page
    DraftPlan(summary="s", steps=[make_step(1, "notion", "create_task", title="T"),
                                  page(2, [1], title="B", content="c", parent_page="1")]),
    # parent_page together with parent: conflicting, left for the validator
    DraftPlan(summary="s", steps=[page(1, title="A", content="c"),
                                  page(2, [1], title="B", content="c", parent_page="1", parent="engineering")]),
])
def test_invalid_parent_page_references_stay_rejected(draft):
    assert "UNKNOWN_ARGUMENT" in rejected_codes(draft, ["engineering"])


def test_other_unknown_arguments_are_still_rejected_after_normalization():
    draft = reported_plan(read_back=False)
    draft.steps[1].arguments.append(make_step(0, "notion", "x", icon="star").arguments[0])
    assert rejected_codes(draft) == ["UNKNOWN_ARGUMENT"]


@pytest.mark.parametrize("step", [
    make_step(2, "notion", "read_page", page_title="A page this plan does not create"),  # not a read-back
    make_step(2, "notion", "read_page"),                                              # references nothing
    make_step(2, "notion", "delete_page", [1], page_title=PARENT_TITLE),              # not a read action
    make_step(2, "notion", "update_page", [1], title=PARENT_TITLE),
])
def test_other_unsupported_notion_actions_stay_rejected(step):
    draft = DraftPlan(summary="s", steps=[page(1, title=PARENT_TITLE, content="c"), step])
    assert rejected_codes(draft) == ["UNSUPPORTED_ACTION"]


def test_read_back_step_that_a_later_step_depends_on_is_kept_and_rejected():
    draft = DraftPlan(summary="s", steps=[
        page(1, title=PARENT_TITLE, content="c"),
        make_step(2, "notion", "read_page", [1], page_title=PARENT_TITLE),
        make_step(3, "notion", "create_task", [2], title="Follow up")])
    assert rejected_codes(draft) == ["UNSUPPORTED_ACTION"]


def test_read_back_in_the_middle_is_removed_and_later_steps_renumbered():
    draft = DraftPlan(summary="s", steps=[
        page(1, title=PARENT_TITLE, content="c"),
        make_step(2, "notion", "read_page", [1], page="step 1"),
        page(3, [1], title=ITEMS[0], content="c", parent_page="1"),
        page(4, [1, 3], title=ITEMS[1], content="c", parent_step="1")])
    normalized, notes = normalize_notion_references(draft, None)
    plan, issues = validate_draft(normalized, max_steps=10)
    assert issues == []
    assert [(s.step_number, args(s).get("parent_step"), s.depends_on) for s in plan.steps] == [
        (1, None, []), (2, "1", [1]), (3, "1", [1, 2])]
    assert notes[-1] == "Later steps were renumbered after the removal: 3->2, 4->3."


# ======================= strictness and safety are preserved =======================


def test_executor_still_rejects_an_unnormalized_plan():
    """The executor validates strictly and never normalizes: a client cannot bypass the planner."""
    steps = [s.model_dump() for s in reported_plan().steps]
    for s in steps:
        s["arguments"] = {a["name"]: a["value"] for a in s["arguments"]}
    service, _ = make_service()
    response = service.execute(make_request(steps))
    assert response.status is ExecutionStatus.REJECTED
    codes = [i.code for i in response.validation.issues]
    assert codes.count("UNKNOWN_ARGUMENT") == 4 and "UNSUPPORTED_ACTION" in codes
    assert service.registry.store.calls == []


def test_normalized_plan_executes_in_mock_mode_with_children_inside_the_parent():
    service, _, _, _ = chain([gemini_503()], [groq_ok(reported_plan())])
    result = service.plan("Customer Rewards Platform in Notion.", target="notion")
    steps = [{**s.model_dump(mode="json")} for s in result.planned_steps]
    executor, _ = make_service()
    response = executor.execute(make_request(steps))
    assert response.status is ExecutionStatus.COMPLETED
    assert [r.status for r in response.step_results] == [StepStatus.SUCCEEDED] * 5
    assert len(executor.registry.store.live_resources()) == 5  # in-memory mock pages only


def test_notion_example_in_the_prompt_is_a_valid_parent_child_plan():
    for alias in ("engineering", None):
        draft = DraftPlan.model_validate(json.loads(notion_example(alias)))
        plan, issues = validate_draft(draft, max_steps=10)
        assert issues == [] and plan is not None
        assert [args(s).get("parent_step") for s in plan.steps] == [None, "1", "1"]
        assert args(plan.steps[0]).get("parent") == alias


def test_prompt_names_the_approved_alias_and_omits_notion_rules_for_other_targets():
    prompt = build_system_prompt(10, allowed_integrations={"notion"}, notion_parents={"engineering"})
    assert "approved Notion parent alias (engineering)" in prompt and '"value": "engineering"' in prompt
    assert "Notion rules (a step" not in build_system_prompt(10, allowed_integrations={"google_drive"})


def test_plan_api_reports_the_normalization_in_the_preview():
    service, _, _, _ = chain([gemini_503()], [groq_ok(reported_plan())])
    app.dependency_overrides[planning_api.get_planning_service] = lambda: service
    try:
        response = TestClient(app).post("/api/v1/workflows/plan", json={
            "instruction": "Create the Customer Rewards Platform plan in Notion.", "mode": "mock", "target": "notion"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PLANNED" and body["executed"] is False
    assert_hierarchy(body["planned_steps"])
    assert any("read-back verification" in n and "NOT performed" in n for n in body["plan_adjustments"])


# =============== follow-up report: parent_page_title + an "assistant" summary step ===============
# Groq openai/gpt-oss-120b after a Gemini PLANNER_API_ERROR produced a six-step plan:
#   steps 2-5  UNKNOWN_ARGUMENT        `parent_page_title` is not accepted by notion.create_page
#   step 6     UNSUPPORTED_INTEGRATION `assistant` is not supported


def assistant_summary(n=6, depends_on=(1, 2, 3, 4, 5), action="summarize", **arguments):
    arguments = arguments or {"message": "Created the Customer Rewards Platform page and four action items."}
    return make_step(n, "assistant", action, depends_on, **arguments)


def six_step_plan(parent_title: str = PARENT_TITLE, summary_step=None) -> DraftPlan:
    steps = [page(1, title=PARENT_TITLE, content="Project overview (synthetic).")]
    steps += [page(n, [1], title=t, content=f"- Priority: Medium\n- Owner: Platform team\n{t} (synthetic).",
                   parent_page_title=parent_title) for n, t in enumerate(ITEMS, start=2)]
    steps.append(summary_step or assistant_summary())
    return DraftPlan(summary="Customer Rewards Platform plan with four action items.", steps=steps)


def test_six_step_plan_without_normalization_reproduces_the_exact_reported_errors():
    _, issues = validate_draft(six_step_plan(), max_steps=10)
    assert [(i.code, i.step_number, i.field) for i in issues] == [
        *[("UNKNOWN_ARGUMENT", n, "arguments.parent_page_title") for n in range(2, 6)],
        ("UNSUPPORTED_INTEGRATION", 6, "integration")]
    assert "'parent_page_title' is not accepted by notion.create_page" in issues[0].message
    assert "Allowed: content, parent, parent_step, title." in issues[0].message
    assert "Integration 'assistant' is not supported" in issues[4].message


def test_reported_six_step_groq_plan_is_now_planned():
    service, gemini, groq_client, _ = chain([gemini_503()], [groq_ok(six_step_plan())])
    result = service.plan("Create the Customer Rewards Platform plan in Notion.", target="notion")
    assert result.status is PlanStatus.PLANNED, result.validation.issues
    assert result.planner == "groq" and result.fallback_used is True
    assert_hierarchy(result.planned_steps)
    notes = result.plan_adjustments
    assert len(notes) == 5
    for n in range(2, 6):
        assert notes[n - 2] == (f"Step {n} (notion.create_page): replaced unsupported argument 'parent_page_title' "
                                f"with parent_step=1, the page created by step 1 ('{PARENT_TITLE}').")
    assert notes[4].startswith("Step 6 (assistant.summarize): removed. It only produced a text summary")
    for system in (gemini.models.calls[0]["config"].system_instruction,
                   groq_client.calls[0]["messages"][0]["content"]):
        assert "parent_page_title" in system and "use parent_step directly" in system
        assert 'Never add a step for "assistant"' in system


def test_same_six_step_plan_from_gemini_is_normalized_identically():
    service, _, groq_client, _ = chain([ok_response(six_step_plan())], [groq_ok()])
    result = service.plan("Create the Customer Rewards Platform plan in Notion.", target="notion")
    assert result.status is PlanStatus.PLANNED and result.planner == "gemini" and not groq_client.calls
    assert_hierarchy(result.planned_steps)


def test_six_step_plan_api_preview_reports_both_adjustments_and_mock_execution_succeeds():
    service, _, _, _ = chain([gemini_503()], [groq_ok(six_step_plan())])
    app.dependency_overrides[planning_api.get_planning_service] = lambda: service
    try:
        response = TestClient(app).post("/api/v1/workflows/plan", json={
            "instruction": "Create the Customer Rewards Platform plan in Notion.", "mode": "mock", "target": "notion"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert_hierarchy(body["planned_steps"])
    assert any("parent_page_title" in n for n in body["plan_adjustments"])
    assert any(n.startswith("Step 6 (assistant.summarize): removed") for n in body["plan_adjustments"])
    executor, _ = make_service()
    steps = [{k: s[k] for k in ("step_number", "integration", "action", "arguments", "depends_on")}
             for s in body["planned_steps"]]
    run = executor.execute(make_request(steps))
    assert run.status is ExecutionStatus.COMPLETED and len(executor.registry.store.live_resources()) == 5


def test_executor_rejects_the_unnormalized_six_step_plan():
    steps = [s.model_dump() for s in six_step_plan().steps]
    for s in steps:
        s["arguments"] = {a["name"]: a["value"] for a in s["arguments"]}
    executor, _ = make_service()
    response = executor.execute(make_request(steps))
    assert response.status is ExecutionStatus.REJECTED
    codes = [i.code for i in response.validation.issues]
    assert codes.count("UNKNOWN_ARGUMENT") == 4 and "UNSUPPORTED_INTEGRATION" in codes
    assert executor.registry.store.calls == []


@pytest.mark.parametrize("value", [
    "",                                   # missing
    "Some Other Project",                 # unknown title
    PARENT_TITLE.lower(),                 # not an EXACT match
    "1",                                  # a step number is not a title
    "engineering",                        # an approved alias is not a page of this plan
])
def test_parent_page_title_that_is_not_one_exact_earlier_title_is_rejected(value):
    draft = six_step_plan(parent_title=value, summary_step=page(6, title="Extra", content="c"))
    normalized, notes = normalize_notion_references(draft, ["engineering"])
    _, issues = validate_draft(normalized, max_steps=10)
    assert [(i.code, i.field) for i in issues] == [("UNKNOWN_ARGUMENT", "arguments.parent_page_title")] * 4
    assert notes == []


def test_parent_page_title_of_a_future_page_is_rejected():
    draft = DraftPlan(summary="s", steps=[
        page(1, title="Child", content="c", parent_page_title="Hub"), page(2, title="Hub", content="c")])
    assert rejected_codes(draft) == ["UNKNOWN_ARGUMENT"]


def test_ambiguous_parent_page_title_is_rejected():
    draft = DraftPlan(summary="s", steps=[
        page(1, title="Hub", content="c"), page(2, title="Hub", content="c"),
        page(3, [1, 2], title="Child", content="c", parent_page_title="Hub")])
    assert rejected_codes(draft) == ["UNKNOWN_ARGUMENT"]


@pytest.mark.parametrize("extra", [{"parent_step": "1"}, {"parent": "engineering"}, {"parent_page": "1"}])
def test_parent_page_title_with_another_parent_argument_is_rejected(extra):
    draft = DraftPlan(summary="s", steps=[
        page(1, title="Hub", content="c"), page(2, [1], title="Child", content="c", parent_page_title="Hub", **extra)])
    assert "UNKNOWN_ARGUMENT" in rejected_codes(draft, ["engineering"])


@pytest.mark.parametrize("summary_step", [
    assistant_summary(action="send_message"),                                     # performs an action
    assistant_summary(action="notify", message="Done"),
    assistant_summary(action="do_something"),                                     # unknown / ambiguous
    assistant_summary(message="Done", recipient="team@example.invalid"),          # has a destination
    assistant_summary(message="Done", channel="general"),
    assistant_summary(message="Done", webhook_url="https://example.invalid/hook"),
    assistant_summary(message="run $(rm -rf /)"),                                 # unsafe content
])
def test_assistant_steps_that_are_not_pure_summaries_are_rejected(summary_step):
    normalized, notes = normalize_notion_references(six_step_plan(summary_step=summary_step), None)
    plan, issues = validate_draft(normalized, max_steps=10)
    assert plan is None
    assert [(i.code, i.step_number) for i in issues] == [("UNSUPPORTED_INTEGRATION", 6)]
    assert not any("assistant" in n for n in notes)


def test_assistant_summary_that_a_later_step_depends_on_is_rejected():
    draft = DraftPlan(summary="s", steps=[
        page(1, title=PARENT_TITLE, content="c"), assistant_summary(2, [1]),
        page(3, [1, 2], title=ITEMS[0], content="c", parent_step="1")])
    assert rejected_codes(draft) == ["UNSUPPORTED_INTEGRATION"]


def test_assistant_summary_in_the_middle_is_removed_and_later_steps_renumbered():
    draft = DraftPlan(summary="s", steps=[
        page(1, title=PARENT_TITLE, content="c"), assistant_summary(2, [1]),
        page(3, [1], title=ITEMS[0], content="c", parent_page_title=PARENT_TITLE)])
    normalized, notes = normalize_notion_references(draft, None)
    plan, issues = validate_draft(normalized, max_steps=10)
    assert issues == []
    assert [(s.step_number, args(s).get("parent_step"), s.depends_on) for s in plan.steps] == [
        (1, None, []), (2, "1", [1])]
    assert notes[-1] == "Later steps were renumbered after the removal: 3->2."


def test_removing_a_summary_never_bypasses_the_integration_target():
    draft = six_step_plan()
    draft.steps[1] = make_step(2, "google_drive", "create_document", [1], title="x", content="y", folder="sandbox")
    service, _, _, _ = chain([gemini_503()], [groq_ok(draft)])
    result = service.plan("Customer Rewards Platform in Notion.", target="notion")
    assert result.status is PlanStatus.INVALID
    assert [i.code for i in result.validation.issues] == ["INTEGRATION_NOT_ALLOWED"]
