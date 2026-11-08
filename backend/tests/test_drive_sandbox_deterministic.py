"""The Google Drive Sandbox Test is a deterministic, server-built plan: no LLM is ever involved.

Regression for: "INVALID_LLM_OUTPUT (Groq): Groq returned output that does not match the plan
schema" when generating the Drive smoke test. The fixed smoke test must never depend on Groq or
Gemini: it is exactly three server-built steps (run folder -> verified report upload -> read the
report back and verify its content), it works when every AI provider is down, and planning never
writes to Google Drive.

Google Drive is a fake (httpx.MockTransport); the conftest blocks every non-loopback connection.
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.agents import service as planning_service_module
from app.agents.planners import factory as planner_factory
from app.agents.validation import validate_draft
from app.api import drive_sandbox as drive_api
from app.api import integrations as integrations_api
from app.main import app
from app.schemas.execution import ExecutionRequest, ExecutionStatus
from app.schemas.plan import DraftPlan, DraftStep, StepArgument
from tests.drive_fakes import SANDBOX_ID, DriveWorld, drive_settings, write_token
from tests.execution_helpers import make_service
from tests.integration_fakes import integration_settings

PLAN_URL = "/api/v1/workflows/drive-sandbox/plan"
STEPS = ["create_folder", "create_document", "read_document"]


@pytest.fixture
def no_ai(monkeypatch):
    """Every way to reach an AI provider explodes: the sandbox test must not touch any of them."""
    def boom(*_a, **_k):
        raise AssertionError("The Drive Sandbox Test must not use an LLM planner")
    for name in ("create_planner", "create_groq_client", "create_gemini_client", "create_anthropic_client",
                 "create_provider_planner", "build_auto_chain"):
        monkeypatch.setattr(planner_factory, name, boom)
    monkeypatch.setattr(planning_service_module.PlanningService, "plan", boom)
    monkeypatch.setattr(planning_service_module.PlanningService, "run", boom)


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    """PLANNER_MODE=auto with Groq primary and Gemini fallback configured (synthetic keys) - the
    configuration under which the reported failure happened - plus a ready Drive sandbox."""
    def install(mode: str):
        write_token(tmp_path)
        settings = drive_settings(mode, tmp_path, planner_mode="auto", groq_model="openai/gpt-oss-120b",
                                  groq_api_key="gsk_SYNTHETICnotARealGroqKey0000000099",
                                  gemini_model="gemini-test-flash", gemini_api_key="synthetic-gemini-key-0000000099")
        monkeypatch.setattr(drive_api, "get_settings", lambda: settings)
        monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
        return settings
    return install


def plan(mode: str):
    return TestClient(app).post(PLAN_URL, json={"mode": mode, "target": "google_drive"})


def request_from(body: dict, *, mode: str, steps=None) -> ExecutionRequest:
    return ExecutionRequest.model_validate({
        "workflow_id": body["workflow_id"],
        "plan": {"summary": body["summary"], "integration_target": "google_drive", "steps": steps or [
            {k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
            for s in body["planned_steps"]]},
        "approval": {"approved": True, "mode": mode, "approved_by": "synthetic.reviewer",
                     "integration_target": "google_drive"}})


# ======================= planning: deterministic, no AI, no Drive I/O =======================


@pytest.mark.parametrize("mode", ["mock", "dry_run", "real"])
def test_plans_three_server_built_steps_with_every_ai_provider_unavailable(no_ai, settings_for, mode):
    settings_for(mode)
    response = plan(mode)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PLANNED" and body["planner"] == "drive-sandbox" and body["executed"] is False
    assert body["errors"] == [] and body["planning_attempts"] == 1 and body.get("provider_attempts") in (None, [])
    assert [s["action"] for s in body["planned_steps"]] == STEPS
    assert {s["integration"] for s in body["planned_steps"]} == {"google_drive"}
    folder, upload, read = (s["arguments"] for s in body["planned_steps"])
    assert folder["name"].startswith("WorkflowWeaver run ")
    assert folder.get("folder") == (None if mode == "mock" else "sandbox")  # the server's sandbox alias only
    assert upload["parent_step"] == "1" and read["parent_step"] == "1"
    assert upload["title"] == read["document_name"] == body["report_title"]
    # Read-back verification is bound to exactly the report this plan uploads.
    assert read["content_sha256"] == hashlib.sha256(upload["content"].encode("utf-8")).hexdigest()
    assert body["real_write"] is (mode == "real")


def test_planning_never_contacts_google_drive(no_ai, settings_for, tmp_path):
    settings_for("real")
    world = DriveWorld(tmp_path)  # a fake Drive that records every call
    plan("real")
    assert world.google.calls == []


def test_each_plan_gets_a_unique_run_folder_and_report(no_ai, settings_for):
    settings_for("dry_run")
    first, second = plan("dry_run").json(), plan("dry_run").json()
    assert first["workflow_id"] != second["workflow_id"]
    folders = [b["planned_steps"][0]["arguments"]["name"] for b in (first, second)]
    assert folders[0] != folders[1] and all(f.startswith("WorkflowWeaver run ") for f in folders)
    assert first["report_title"] != second["report_title"]
    assert folders[0].endswith(first["workflow_id"][:8])


def test_planner_configuration_is_untouched(settings_for):
    settings = settings_for("real")
    assert (settings.planner_mode.value, settings.primary_planner.value, settings.fallback_planner.value) == \
        ("auto", "groq", "gemini")


# ======================= execution: same gates, verified read-back =======================


def test_mock_run_uploads_and_verifies_the_read_back(no_ai, settings_for):
    settings_for("real")
    body = plan("mock").json()
    service, _ = make_service()
    result = service.execute(request_from(body, mode="mock"))
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    assert result.step_results[2].output["content_verified"] is True


def test_dry_run_builds_requests_without_any_network_call(no_ai, settings_for, tmp_path):
    settings_for("real")
    body = plan("dry_run").json()
    world = DriveWorld(tmp_path, "dry_run")
    result = world.service.execute(request_from(body, mode="dry_run"), write_authorized=False)
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    assert all(r.output["sent"] is False for r in result.step_results)
    assert result.step_results[2].output["content_check"] == "not performed (dry run)"
    assert world.google.calls == []


def test_real_run_needs_the_operator_gate_then_creates_uploads_and_verifies(no_ai, settings_for, tmp_path):
    settings_for("real")
    body = plan("real").json()
    world = DriveWorld(tmp_path)
    refused = world.service.execute(request_from(body, mode="real"), write_authorized=False)
    assert refused.status is ExecutionStatus.REJECTED and world.google.writes == []

    result = world.service.execute(request_from(body, mode="real"), write_authorized=True)
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    folder, upload, read = result.step_results
    run_folder = world.google.files[folder.resource_id]
    assert run_folder["parents"] == [SANDBOX_ID]  # inside the configured sandbox
    report = world.google.files[upload.resource_id]
    assert report["parents"] == [folder.resource_id] and upload.output["verified"] is True
    assert read.output["content_verified"] is True and read.output["sha256"] == body["planned_steps"][2]["arguments"]["content_sha256"]

    # Idempotency: replaying the same approved run creates nothing new.
    writes = len(world.google.writes)
    world.service.execute(request_from(body, mode="real"), write_authorized=True)
    assert len(world.google.writes) == writes


def test_read_back_mismatch_fails_and_rolls_back_this_runs_folder_and_report(no_ai, settings_for, tmp_path):
    settings_for("real")
    body = plan("real").json()
    steps = [{k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
             for s in body["planned_steps"]]
    steps[2]["arguments"]["content_sha256"] = "0" * 64  # what was uploaded will not match
    world = DriveWorld(tmp_path)
    result = world.service.execute(request_from(body, mode="real", steps=steps), write_authorized=True)
    assert result.status is not ExecutionStatus.COMPLETED
    assert any(e.code == "READ_BACK_MISMATCH" for e in result.errors)
    created = [f for f in world.google.files.values() if f["id"] != SANDBOX_ID]
    assert created and all(f["trashed"] for f in created)  # compensation: only this run's files, trashed


def test_writes_outside_the_sandbox_are_still_refused(no_ai, settings_for, tmp_path):
    settings_for("real")
    body = plan("real").json()
    steps = [{k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
             for s in body["planned_steps"]]
    steps[0]["arguments"]["folder"] = "reports"  # approved alias, but not the sandbox
    world = DriveWorld(tmp_path)
    result = world.service.execute(request_from(body, mode="real", steps=steps), write_authorized=True)
    assert result.status is ExecutionStatus.REJECTED and world.google.writes == []


# ======================= validation of the new read-back argument =======================


def test_content_sha256_must_be_a_sha256():
    draft = DraftPlan(summary="s", steps=[DraftStep(
        step_number=1, integration="google_drive", action="read_document", description="read",
        arguments=[StepArgument(name="document_name", value="r.md"), StepArgument(name="folder", value="sandbox"),
                   StepArgument(name="content_sha256", value="not-a-hash")], depends_on=[])])
    plan_, issues = validate_draft(draft, max_steps=10)
    assert plan_ is None and [i.code for i in issues] == ["INVALID_IDENTIFIER"]


def test_drive_mcp_adapter_refuses_read_back_verification_it_cannot_perform():
    from app.agents.execution.tools.integrations import build_integration_registry
    from app.schemas.plan import Action
    from app.schemas.workflow import Integration

    registry = build_integration_registry(integration_settings("dry_run"))
    adapter = registry.adapters[Integration.GOOGLE_DRIVE]
    problems = adapter.preflight(Action.READ_DOCUMENT, {"document_name": "r.md", "folder": "reports",
                                                        "content_sha256": "a" * 64})
    assert problems and problems[0][0] == "UNSUPPORTED_ARGUMENT"
