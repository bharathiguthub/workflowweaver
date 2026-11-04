"""Regression: template -> plan -> mock execution -> report, plus genuinely missing documents.

Original failure: the mock planner invented document_name="<first six words> - Source notes"
for every Drive read (e.g. 'Read the synthetic Sprint 42 meeting - Source notes'), which
never exists in the synthetic Drive catalog, so execution failed with DOCUMENT_NOT_FOUND.

Since Stage 4, a missing document is reported BEFORE execution (plan validation, and again
in the execution prepare node). The adapter's runtime DOCUMENT_NOT_FOUND check remains and
is exercised with document_precheck=False.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools import DRIVE_FIXTURES, MockToolRegistry
from app.agents.planners.mock import MockPlanner, requested_document_name
from app.agents.service import PlanningService
from app.agents.synthetic_documents import (
    SYNTHETIC_DRIVE_DOCUMENTS,
    canonical_document_name,
    find_document_mentioned_in,
)
from app.api import execution as execution_api
from app.api import planning as planning_api
from app.api import streaming as streaming_api
from app.main import app
from tests.sse_helpers import make_runner, parse_sse

APPROVAL = {"approved": True, "mode": "mock", "approved_by": "synthetic.reviewer"}
ORIGINAL_FAILING_INSTRUCTION = (
    "Read the synthetic Sprint 42 meeting notes from Google Drive, create Notion tasks for each "
    "action item, and notify attendees in Microsoft Teams."
)


@pytest.fixture
def client():
    runner = make_runner()
    planning = PlanningService(
        MockPlanner(), max_steps=10, max_planning_attempts=2, available_documents=frozenset(SYNTHETIC_DRIVE_DOCUMENTS)
    )
    app.dependency_overrides[planning_api.get_planning_service] = lambda: planning
    app.dependency_overrides[execution_api.get_execution_service] = lambda: runner.service
    app.dependency_overrides[streaming_api.get_execution_runner] = lambda: runner
    app.dependency_overrides[streaming_api.get_heartbeat_seconds] = lambda: 0.2
    try:
        yield TestClient(app), runner
    finally:
        app.dependency_overrides.clear()
        runner.shutdown()


def execution_body(plan: dict) -> dict:
    return {"workflow_id": plan["workflow_id"], "plan": {"summary": plan["summary"], "steps": plan["planned_steps"]},
            "approval": APPROVAL}


def templates(client) -> dict[str, dict]:
    return {t["id"]: t for t in client.get("/api/v1/workflows/templates").json()}


# ---------- shared catalog ----------


def test_planner_and_adapter_share_one_document_catalog():
    assert DRIVE_FIXTURES is SYNTHETIC_DRIVE_DOCUMENTS


def test_catalog_lookup_is_exact_not_fuzzy():
    assert canonical_document_name("  synthetic   STANDUP notes ") == "Synthetic standup notes"
    assert canonical_document_name("synthetic standup") is None
    assert canonical_document_name("Synthetic Sprint 42 meeting notes") is None
    assert find_document_mentioned_in("fetch the synthetic sprint backlog please") == "Synthetic sprint backlog"
    assert find_document_mentioned_in("fetch the sprint 42 notes") is None


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("Read 'Synthetic standup notes' from Google Drive", "Synthetic standup notes"),
        ('read "SYNTHETIC  standup NOTES" from google drive', "Synthetic standup notes"),
        ("Fetch the synthetic incident report from Drive", "Synthetic incident report"),
        ("Open “Quarterly plan” in Google Drive", "Quarterly plan"),  # named, unknown: kept
        ("Read the synthetic Sprint 42 meeting notes from Google Drive", "synthetic Sprint 42 meeting notes"),
        ("Read the team's notes from Google Drive", "team's notes"),
    ],
)
def test_requested_document_name(clause, expected):
    assert requested_document_name(clause) == expected


def test_planner_no_longer_invents_source_notes_names():
    for instruction in (ORIGINAL_FAILING_INSTRUCTION, "Fetch the notes from Google Drive then create tasks in Notion"):
        draft = MockPlanner().plan(instruction, max_steps=10)
        names = [a.value for s in draft.steps for a in s.arguments if a.name == "document_name"]
        assert names and not any(n.endswith("- Source notes") for n in names)


# ---------- every template: template -> plan -> execute -> completed report ----------


@pytest.mark.parametrize("template_id", ["meeting-to-tasks", "sprint-planning", "incident-response"])
def test_template_plans_and_executes_to_completion(client, template_id):
    http, runner = client
    template = templates(http)[template_id]

    plan_response = http.post("/api/v1/workflows/plan", json={"instruction": template["example_instruction"], "name": template["name"]})
    assert plan_response.status_code == 200, plan_response.json()
    plan = plan_response.json()
    assert plan["status"] == "PLANNED" and plan["validation"]["is_valid"]
    assert {s["integration"] for s in plan["planned_steps"]} <= set(template["integrations"])
    for step in plan["planned_steps"]:
        if step["action"] == "read_document":
            assert step["arguments"]["document_name"] in SYNTHETIC_DRIVE_DOCUMENTS

    executed = http.post("/api/v1/workflows/execute", json=execution_body(plan))
    assert executed.status_code == 200, executed.json()
    report = http.get(f"/api/v1/workflows/executions/{plan['workflow_id']}").json()
    assert report["status"] == "COMPLETED"
    assert all(r["status"] == "SUCCEEDED" for r in report["step_results"])
    assert report["errors"] == [] and report["compensation_results"] == []


def test_meeting_to_tasks_reads_an_existing_synthetic_document(client):
    http, runner = client
    template = templates(http)["meeting-to-tasks"]
    assert "google_drive" in template["integrations"] and template["steps"][0] == "Fetch meeting notes"

    plan = http.post("/api/v1/workflows/plan", json={"instruction": template["example_instruction"]}).json()
    read = plan["planned_steps"][0]
    assert (read["integration"], read["action"], read["arguments"]) == (
        "google_drive", "read_document", {"document_name": "Synthetic standup notes"}
    )
    assert all(1 in s["depends_on"] for s in plan["planned_steps"][1:])  # later steps use what was read

    report = http.post("/api/v1/workflows/execute", json=execution_body(plan)).json()
    assert report["status"] == "COMPLETED"
    assert report["step_results"][0]["output"] == {
        "document_name": "Synthetic standup notes", "content": SYNTHETIC_DRIVE_DOCUMENTS["Synthetic standup notes"],
    }


def test_meeting_to_tasks_streams_to_completion(client):
    http, _ = client
    template = templates(http)["meeting-to-tasks"]
    plan = http.post("/api/v1/workflows/plan", json={"instruction": template["example_instruction"]}).json()
    start = http.post("/api/v1/workflows/executions", json=execution_body(plan))
    assert start.status_code == 202
    events = parse_sse(http.get(start.json()["stream_url"]).text)
    assert [e["event"] for e in events] == ["workflow_started"] + ["step_started", "step_succeeded"] * 4 + ["workflow_completed"]
    assert http.get(start.json()["result_url"]).json()["status"] == "COMPLETED"


# ---------- negative: genuinely missing documents still fail (now before execution) ----------


def _draft_steps(instruction: str) -> list[dict]:
    """Steps exactly as the mock planner drafts them (a rejected PlanResponse omits them)."""
    draft = MockPlanner().plan(instruction, max_steps=10)
    return [
        {"step_number": d.step_number, "integration": d.integration, "action": d.action, "description": d.description,
         "arguments": {a.name: a.value for a in d.arguments}, "depends_on": d.depends_on}
        for d in draft.steps
    ]


def _execute(service, steps):
    from app.schemas.execution import ExecutionRequest

    return service.execute(ExecutionRequest.model_validate(
        {"workflow_id": str(uuid4()), "plan": {"summary": "s", "steps": steps}, "approval": APPROVAL}
    ))


def test_original_missing_document_is_rejected_at_planning(client):
    http, runner = client
    response = http.post("/api/v1/workflows/plan", json={"instruction": ORIGINAL_FAILING_INSTRUCTION})
    assert response.status_code == 422
    plan = response.json()
    assert plan["status"] == "INVALID" and plan["planned_steps"] == []
    issue = plan["validation"]["issues"][0]
    assert (issue["code"], issue["step_number"], issue["field"]) == ("DOCUMENT_NOT_FOUND", 1, "arguments.document_name")
    assert "synthetic Sprint 42 meeting notes" in issue["message"]
    assert "Synthetic standup notes" in issue["message"]  # lists what IS available
    assert runner.service.registry.store.calls == []


def test_forcing_a_missing_document_plan_is_rejected_before_any_tool_call(client):
    """/execute accepts client-supplied plans, so prepare re-checks: nothing is executed."""
    http, runner = client
    steps = _draft_steps(ORIGINAL_FAILING_INSTRUCTION)
    body = {"workflow_id": str(uuid4()), "plan": {"summary": "s", "steps": steps}, "approval": APPROVAL}

    response = http.post("/api/v1/workflows/execute", json=body)
    assert response.status_code == 422
    report = response.json()
    assert report["status"] == "REJECTED"
    assert [i["code"] for i in report["validation"]["issues"]] == ["DOCUMENT_NOT_FOUND"]
    assert report["step_results"] == [] and report["compensation_results"] == []
    assert runner.service.registry.store.calls == [] and runner.service.registry.store.side_effects == 0

    body["workflow_id"] = str(uuid4())
    start = http.post("/api/v1/workflows/executions", json=body).json()
    events = parse_sse(http.get(start["stream_url"]).text)
    assert [e["event"] for e in events] == ["workflow_failed"]
    assert events[0]["data"]["metadata"]["status"] == "REJECTED"
    assert events[0]["data"]["metadata"]["validation_issue_codes"] == ["DOCUMENT_NOT_FOUND"]


def test_wrong_case_document_name_gets_a_did_you_mean_hint():
    steps = [{"step_number": 1, "integration": "google_drive", "action": "read_document",
              "arguments": {"document_name": "synthetic STANDUP notes"}}]
    report = _execute(ExecutionService(MockToolRegistry(), RetryPolicy(jitter=False), max_steps=10), steps)
    assert report.status.value == "REJECTED"
    assert "Did you mean 'Synthetic standup notes'?" in report.validation.issues[0].message


def test_document_created_by_an_earlier_step_is_accepted_but_not_a_later_one():
    create = {"integration": "google_drive", "action": "create_document",
              "arguments": {"title": "Synthetic minutes", "content": "Synthetic minutes body"}}
    read = {"integration": "google_drive", "action": "read_document", "arguments": {"document_name": "Synthetic minutes"}}
    service = ExecutionService(MockToolRegistry(), RetryPolicy(jitter=False), max_steps=10, sleeper=lambda _: None)

    ok = _execute(service, [{**create, "step_number": 1}, {**read, "step_number": 2, "depends_on": [1]}])
    assert ok.status.value == "COMPLETED"
    assert ok.step_results[1].output["content"] == "Synthetic minutes body"

    wrong_order = _execute(service, [{**read, "step_number": 1}, {**create, "step_number": 2}])
    assert wrong_order.status.value == "REJECTED"
    assert wrong_order.validation.issues[0].step_number == 1


def test_planning_accepts_a_read_of_a_document_created_earlier_in_the_plan():
    from app.schemas.plan import DraftPlan
    from tests.conftest import StubPlanner, make_step

    create = make_step(1, "google_drive", "create_document", title="Synthetic minutes", content="Synthetic body")
    read_after = make_step(2, "google_drive", "read_document", depends_on=[1], document_name="Synthetic minutes")
    catalog = frozenset(SYNTHETIC_DRIVE_DOCUMENTS)

    def plan(*steps):
        service = PlanningService(StubPlanner(DraftPlan(summary="s", steps=list(steps))), max_steps=10,
                                  max_planning_attempts=1, available_documents=catalog)
        return service.plan("synthetic instruction")

    assert plan(create, read_after).status.value == "PLANNED"
    read_first = make_step(1, "google_drive", "read_document", document_name="Synthetic minutes")
    create_later = make_step(2, "google_drive", "create_document", title="Synthetic minutes", content="Synthetic body")
    rejected = plan(read_first, create_later)
    assert rejected.status.value == "INVALID"
    assert [(i.code, i.step_number) for i in rejected.validation.issues] == [("DOCUMENT_NOT_FOUND", 1)]
    # Without a catalog (non-mock mode) the check is not applied.
    no_check = PlanningService(StubPlanner(DraftPlan(summary="s", steps=[read_first])), max_steps=10, max_planning_attempts=1)
    assert no_check.plan("synthetic instruction").status.value == "PLANNED"


def test_runtime_document_not_found_still_enforced_by_the_adapter_and_rolled_back():
    """With the precheck disabled, the adapter is the last line of defence: the missing read
    fails at runtime with DOCUMENT_NOT_FOUND (permanent: not retried) and the earlier
    Notion page is compensated."""
    steps = [
        {"step_number": 1, "integration": "notion", "action": "create_page",
         "arguments": {"title": "Synthetic page", "content": "Synthetic"}},
        {"step_number": 2, "integration": "google_drive", "action": "read_document",
         "arguments": {"document_name": "Synthetic document that does not exist"}},
    ]
    service = ExecutionService(MockToolRegistry(), RetryPolicy(jitter=False), max_steps=10,
                               sleeper=lambda _: None, document_precheck=False)
    report = _execute(service, steps)
    assert report.status.value == "ROLLED_BACK"
    assert (report.step_results[1].error.code, report.step_results[1].attempts) == ("DOCUMENT_NOT_FOUND", 1)
    assert [c.status.value for c in report.compensation_results] == ["COMPENSATED"]
    assert service.registry.store.live_resources() == {}

    checked = ExecutionService(MockToolRegistry(), RetryPolicy(jitter=False), max_steps=10)
    rejected = _execute(checked, steps)
    assert rejected.status.value == "REJECTED" and checked.registry.store.side_effects == 0
