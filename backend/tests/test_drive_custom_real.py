"""AI-generated, Google Drive-only custom workflows in Real mode.

POST /api/v1/workflows/plan with {"mode": "real", "target": "google_drive"} plans with Gemini
(Groq fallback) and is restricted server-side to the four Drive actions and to the sandbox
folder (or folders created by the same plan). Execution re-checks the target, the recorded
Drive destinations, approval, the operator token and the kill switch.

Gemini, Groq and Google are fakes (no network). No real Drive write happens.
"""

import pytest
from fastapi.testclient import TestClient

from app.agents.plan_registry import get_plan_registry
from app.agents.planners import FallbackPlanner, GeminiPlanner, GroqPlanner, MockPlanner
from app.agents.service import PlanningService
from app.agents.validation import drive_destination_issues
from app.api import planning as planning_api
from app.main import app
from app.observability import set_telemetry
from app.schemas.execution import ExecutionRequest, ExecutionStatus
from app.schemas.plan import DraftPlan, DraftStep, StepArgument
from tests.drive_fakes import OTHER_FOLDER_ID, SANDBOX_ID, DriveWorld, drive_settings, write_token
from tests.telemetry_helpers import RecordingTelemetry
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, api_error, ok_response
from tests.test_planner_fallback import GROQ_MODEL, FakeGroqClient, gemini_503, groq_ok

PLAN_URL = "/api/v1/workflows/plan"
INSTRUCTION = "Create a Q3 planning folder in Drive, save a short kickoff report into it, then list the folder."


def step(n, action, depends_on=(), **args) -> DraftStep:
    return DraftStep(step_number=n, integration="google_drive", action=action, description=f"Step {n}",
                     arguments=[StepArgument(name=k, value=v) for k, v in args.items()], depends_on=list(depends_on))


GOOD = DraftPlan(summary="Q3 planning folder with a kickoff report.", steps=[
    step(1, "create_folder", name="Q3 planning", folder="sandbox"),
    step(2, "create_document", [1], title="Kickoff report.md", content="# Kickoff\n- Synthetic agenda\n",
         parent_step="1"),
    step(3, "list_files", [1, 2], parent_step="1"),
    step(4, "read_document", [1, 2], document_name="Kickoff report.md", parent_step="1"),
])


@pytest.fixture(autouse=True)
def _clean():
    get_plan_registry().clear()
    yield
    get_plan_registry().clear()
    app.dependency_overrides.clear()


@pytest.fixture
def real_api(monkeypatch, tmp_path):
    """Real-mode server (fake Google configuration) with a Gemini -> Groq planner of fakes."""

    def install(gemini_outcomes=(None,), groq_outcomes=(None,), *, authorize=True, planner=None, **overrides):
        if authorize:
            write_token(tmp_path)
        settings = drive_settings("real", tmp_path, **overrides)
        monkeypatch.setattr(planning_api, "get_settings", lambda: settings)
        gemini = FakeGenaiClient(*[o if o is not None else ok_response(GOOD) for o in gemini_outcomes])
        groq = FakeGroqClient(*[o if o is not None else groq_ok(GOOD) for o in groq_outcomes])
        chain = planner or FallbackPlanner([
            GeminiPlanner(client=gemini, model=GEMINI_MODEL, max_output_tokens=4096),
            GroqPlanner(client=groq, model=GROQ_MODEL, max_tokens=4096),
        ])
        service = PlanningService(chain, max_steps=10, max_planning_attempts=2, available_documents=None)
        app.dependency_overrides[planning_api.get_planning_service] = lambda: service
        return gemini, groq

    return install


def post(instruction=INSTRUCTION, **body):
    return TestClient(app).post(PLAN_URL, json={"instruction": instruction, "mode": "real",
                                                "target": "google_drive", **body})


def execution_request(plan: dict, *, approved_target="google_drive", approved=True, steps=None) -> ExecutionRequest:
    return ExecutionRequest.model_validate({
        "workflow_id": plan["workflow_id"],
        "plan": {"summary": plan["summary"], "integration_target": plan["integration_target"],
                 "steps": steps if steps is not None else [
                     {k: s[k] for k in ("step_number", "integration", "action", "description", "arguments",
                                        "depends_on")} for s in plan["planned_steps"]]},
        "approval": {"approved": approved, "mode": "real", "approved_by": "synthetic.reviewer",
                     **({"integration_target": approved_target} if approved_target else {})}})


# ======================= planning: Gemini, Groq fallback, restrictions =======================


def test_gemini_plans_a_drive_only_real_workflow_with_sandbox_destinations(real_api):
    gemini, groq = real_api()
    response = post()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["planner"] == "gemini" and body["integration_target"] == "google_drive" and body["executed"] is False
    assert [s["action"] for s in body["planned_steps"]] == ["create_folder", "create_document", "list_files",
                                                             "read_document"]
    assert body["planned_steps"][0]["arguments"]["folder"] == "sandbox"
    assert groq.calls == []
    prompt = gemini.models.calls[0]["config"].system_instruction
    assert "may use ONLY google_drive" in prompt and "an approved folder alias (sandbox)" in prompt
    assert "integration=notion" not in prompt and SANDBOX_ID not in prompt  # alias only, never the id
    record = get_plan_registry()
    assert record.get(body["workflow_id"]) == "google_drive" and record.drive_folders(body["workflow_id"]) == ["sandbox"]


@pytest.mark.parametrize("outage", [gemini_503, lambda: api_error(__import__("google.genai").genai.errors.ClientError,
                                                                   429, "RESOURCE_EXHAUSTED")])
def test_gemini_outage_falls_back_to_groq_with_the_same_restrictions(real_api, outage):
    gemini, groq = real_api(gemini_outcomes=(outage(),))
    body = post().json()
    assert body["status"] == "PLANNED" and body["planner"] == "groq" and body["fallback_used"] is True
    assert body["planner_model"] == GROQ_MODEL and len(gemini.models.calls) == 2
    prompt = groq.calls[0]["messages"][0]["content"]
    assert "an approved folder alias (sandbox)" in prompt and "integration=notion" not in prompt


@pytest.mark.parametrize("bad, code", [
    (DraftPlan(summary="s", steps=[DraftStep(step_number=1, integration="notion", action="create_page",
                                             description="d", arguments=[StepArgument(name="title", value="t"),
                                                                         StepArgument(name="content", value="c")])]),
     "INTEGRATION_NOT_ALLOWED"),
    (DraftPlan(summary="s", steps=[DraftStep(step_number=1, integration="teams", action="send_message",
                                             description="d", arguments=[StepArgument(name="message", value="m")])]),
     "INTEGRATION_NOT_ALLOWED"),
    (DraftPlan(summary="s", steps=[step(1, "delete_file", name="x", folder="sandbox")]), "UNSUPPORTED_ACTION"),
    (DraftPlan(summary="s", steps=[step(1, "create_folder", name="x", folder=OTHER_FOLDER_ID)]),
     "DESTINATION_NOT_APPROVED"),
    (DraftPlan(summary="s", steps=[step(1, "create_folder", name="x", folder="reports")]),
     "DESTINATION_NOT_APPROVED"),
    (DraftPlan(summary="s", steps=[step(1, "create_document", title="t", content="c",
                                        folder="https://drive.google.com/drive/folders/abc")]),
     "DESTINATION_NOT_APPROVED"),
    (DraftPlan(summary="s", steps=[step(1, "create_document", title="t", content="c")]), "DESTINATION_REQUIRED"),
    (DraftPlan(summary="s", steps=[step(1, "list_files", folder="reports")]), "DESTINATION_NOT_APPROVED"),
    (DraftPlan(summary="s", steps=[step(1, "create_folder", name="x", folder="sandbox", parent_id="abc")]),
     "UNKNOWN_ARGUMENT"),
])
def test_groq_fallback_plans_outside_drive_or_the_sandbox_are_rejected(real_api, bad, code):
    real_api(gemini_outcomes=(gemini_503(),), groq_outcomes=(groq_ok(bad),))
    response = post()
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["planner"] == "groq" and body["status"] == "INVALID" and body["planned_steps"] == []
    assert code in [i["code"] for i in body["validation"]["issues"]]


def test_gemini_plan_outside_the_sandbox_is_rejected_without_fallback(real_api):
    bad = DraftPlan(summary="s", steps=[step(1, "create_folder", name="x", folder="reports")])
    _, groq = real_api(gemini_outcomes=(ok_response(bad),))
    assert post().status_code == 422 and groq.calls == []


def test_parent_step_must_point_to_a_folder_created_in_this_plan(real_api):
    bad = DraftPlan(summary="s", steps=[
        step(1, "list_files", folder="sandbox"),
        step(2, "create_document", [1], title="t", content="c", parent_step="1"),
    ])
    real_api(gemini_outcomes=(ok_response(bad),))
    assert "INVALID_PARENT_STEP" in [i["code"] for i in post().json()["validation"]["issues"]]


def test_real_custom_plans_need_an_ai_planner(real_api):
    real_api(planner=MockPlanner())
    response = post("Save a summary document to Google Drive.")
    assert response.status_code == 503 and "AI planner" in response.json()["detail"]


# Phase 7.1 added real Microsoft Teams Only plans (tests/test_teams_workflow.py).
@pytest.mark.parametrize("target", ["notion", "multi"])
def test_real_custom_plans_are_drive_only(real_api, target):
    real_api()
    assert post(target=target).status_code == 422


def test_real_custom_plans_need_a_ready_drive_sandbox(real_api):
    real_api(authorize=False)
    response = post()
    assert response.status_code == 503 and "google_drive_auth.py" in response.json()["detail"]


def test_real_mode_above_the_server_ceiling_is_refused(real_api, monkeypatch):
    real_api()
    settings = drive_settings("dry_run")
    monkeypatch.setattr(planning_api, "get_settings", lambda: settings)
    assert post().status_code == 409


def test_dry_run_custom_drive_plans_are_also_confined_to_the_sandbox(real_api, monkeypatch):
    bad = DraftPlan(summary="s", steps=[step(1, "create_folder", name="x", folder="reports")])
    real_api(gemini_outcomes=(ok_response(bad),))
    response = post(mode="dry_run")
    assert response.status_code == 422
    assert [i["code"] for i in response.json()["validation"]["issues"]] == ["DESTINATION_NOT_APPROVED"]


def test_mock_mode_drive_plans_are_not_destination_restricted(real_api):
    no_folder = DraftPlan(summary="s", steps=[step(1, "create_document", title="t", content="c")])
    real_api(gemini_outcomes=(ok_response(no_folder),))
    assert post(mode="mock").status_code == 200


def test_planning_trace_records_target_and_restriction(real_api):
    recorder = RecordingTelemetry()
    set_telemetry(recorder)
    real_api(gemini_outcomes=(gemini_503(),))
    post()
    request = recorder.one("planning.request")
    assert request.input["integration_target"] == "google_drive"
    assert request.input["drive_destinations_restricted"] is True
    assert request.output["provider"] == "groq" and request.output["fallback_used"] is True


# ======================= execution: approval, authorization, destinations =======================


def test_planned_custom_workflow_executes_inside_the_sandbox(real_api, tmp_path):
    real_api(gemini_outcomes=(gemini_503(),))  # planned by the Groq fallback
    plan = post().json()
    world = DriveWorld(tmp_path)
    response = world.service.execute(execution_request(plan), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED, response.errors
    folder_id = response.step_results[0].resource_id
    assert world.google.files[folder_id]["parents"] == [SANDBOX_ID]
    report = world.google.files[response.step_results[1].resource_id]
    assert report["parents"] == [folder_id] and response.step_results[1].output["verified"] is True
    assert response.step_results[3].output["md5"] == response.step_results[1].output["md5"]
    created = [f for fid, f in world.google.files.items() if fid != SANDBOX_ID]
    assert created and all(f["parents"][0] in {SANDBOX_ID, folder_id} for f in created)  # sandbox tree only


def test_execution_without_operator_token_is_refused(real_api, tmp_path):
    real_api()
    plan = post().json()
    world = DriveWorld(tmp_path)
    response = world.service.execute(execution_request(plan), write_authorized=False)
    assert [e.code for e in response.errors] == ["REAL_WRITE_AUTHORIZATION_REQUIRED"] and world.google.calls == []


def test_execution_without_approval_is_refused(real_api, tmp_path):
    real_api()
    plan = post().json()
    world = DriveWorld(tmp_path)
    response = world.service.execute(execution_request(plan, approved=False), write_authorized=True)
    assert [e.code for e in response.errors] == ["APPROVAL_REQUIRED"] and world.google.calls == []


def test_kill_switch_blocks_the_custom_workflow(real_api, tmp_path):
    real_api()
    plan = post().json()
    world = DriveWorld(tmp_path, real_writes_enabled=False)
    response = world.service.execute(execution_request(plan), write_authorized=True)
    assert [e.code for e in response.errors] == ["REAL_WRITES_DISABLED"] and world.google.calls == []


def test_approval_for_another_target_is_refused(real_api, tmp_path):
    real_api()
    plan = post().json()
    world = DriveWorld(tmp_path)
    response = world.service.execute(execution_request(plan, approved_target="notion"), write_authorized=True)
    assert [e.code for e in response.errors] == ["INTEGRATION_TARGET_MISMATCH"] and world.google.calls == []


def _steps(plan: dict) -> list[dict]:
    return [{k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
            for s in plan["planned_steps"]]


@pytest.mark.parametrize("folder", ["reports", OTHER_FOLDER_ID])
def test_tampered_destination_is_refused_before_any_drive_call(real_api, tmp_path, folder):
    """The client edits the approved plan to write elsewhere: rejected by the recorded Drive
    destinations, even if the adapter's sandbox setting were relaxed."""
    real_api()
    plan = post().json()
    steps = _steps(plan)
    steps[0]["arguments"]["folder"] = folder
    world = DriveWorld(tmp_path, google_drive_real_writes_sandbox_only=False)
    response = world.service.execute(execution_request(plan, steps=steps), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]
    assert world.google.calls == []


def test_injected_notion_step_is_refused(real_api, tmp_path):
    real_api()
    plan = post().json()
    steps = _steps(plan) + [{"step_number": 5, "integration": "notion", "action": "create_page", "description": "x",
                             "arguments": {"title": "t", "content": "c"}, "depends_on": []}]
    world = DriveWorld(tmp_path)
    response = world.service.execute(execution_request(plan, steps=steps), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert "INTEGRATION_NOT_ALLOWED" in [i.code for i in response.validation.issues] and world.google.calls == []


def test_relabelled_custom_plan_is_refused(real_api, tmp_path):
    real_api()
    plan = post().json()
    relabelled = execution_request({**plan, "integration_target": "multi"}, approved_target="multi")
    world = DriveWorld(tmp_path)
    response = world.service.execute(relabelled, write_authorized=True)
    assert [e.code for e in response.errors] == ["INTEGRATION_TARGET_MISMATCH"] and world.google.calls == []


def test_custom_workflow_failure_rolls_back_and_replay_is_idempotent(real_api, tmp_path):
    real_api()
    plan = post().json()
    world = DriveWorld(tmp_path)
    world.google.failures[("GET", r"^/drive/v3/files$")] = [None, None, 404]  # dedup x2, then listing fails
    request = execution_request(plan)
    first = world.service.execute(request, write_authorized=True)
    assert first.status is ExecutionStatus.ROLLED_BACK
    assert [c.step_number for c in first.compensation_results] == [2, 1]
    calls = len(world.google.calls)
    second = world.service.execute(request, write_authorized=True)
    assert second.replayed is True and len(world.google.calls) == calls


def test_custom_workflow_streams_sse_events(real_api, tmp_path):
    from app.streaming.broker import EventBroker
    from app.streaming.runner import ExecutionRunner

    real_api()
    plan = post().json()
    world = DriveWorld(tmp_path)
    runner = ExecutionRunner(world.service, EventBroker(), max_workers=1)
    try:
        channel, future = runner.start(execution_request(plan), write_authorized=True)
        future.result(timeout=10)
    finally:
        runner.shutdown()
    events = [e.event_type.value for e in channel.events()]
    assert events[0] == "workflow_started" and events[-1] == "workflow_completed"
    assert events.count("step_succeeded") == 4


# ======================= unit =======================


def test_destination_rule_unit():
    steps = [{"step_number": 1, "integration": "google_drive", "arguments": {"folder": "Sandbox"}},
             {"step_number": 2, "integration": "google_drive", "arguments": {"parent_step": "1"}},
             {"step_number": 3, "integration": "google_drive", "arguments": {"folder": "x"}},
             {"step_number": 4, "integration": "notion", "arguments": {}}]
    assert [(i.code, i.step_number) for i in drive_destination_issues(steps, ["sandbox"])] == [
        ("DESTINATION_NOT_APPROVED", 3)]
    assert drive_destination_issues(steps, None) == []


def test_legacy_requests_without_target_are_unchanged(real_api):
    real_api(planner=MockPlanner())
    response = TestClient(app).post(PLAN_URL, json={"instruction": "Save a summary document to Google Drive.",
                                                    "mode": "real"})
    assert response.status_code == 200 and response.json()["integration_target"] is None
    assert get_plan_registry().get(response.json()["workflow_id"]) is None
