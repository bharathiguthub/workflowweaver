"""Regression: Google Drive-only Real-mode plan rejected with
"DESTINATION_REQUIRED (step 3): Step 3 has no Google Drive destination; use folder=sandbox or parent_step."

Root cause: the planner prompt listed folder/parent_step as OPTIONAL for every Drive action and
had no example, so the Groq fallback emitted read_document with document_name only. Fixed by
(1) catalog text "destination (exactly one, required)", (2) per-action rules plus a complete valid
example in the shared Gemini/Groq prompt, and (3) a deterministic normalizer that fills a missing
reference only when exactly one value is possible; everything is still fully validated.

Gemini, Groq and Google are fakes. No real Drive write happens.
"""

import pytest

from app.agents.planners.claude import build_system_prompt, drive_example
from app.agents.validation import drive_destination_issues, normalize_drive_references, validate_draft
from app.schemas.execution import ExecutionStatus
from app.schemas.plan import DraftPlan, DraftStep, StepArgument
from tests.drive_fakes import SANDBOX_ID, DriveWorld
from tests.test_drive_custom_real import _clean, execution_request, post, real_api, step  # noqa: F401 (fixtures)
from tests.test_gemini_planner import ok_response
from tests.test_planner_fallback import gemini_503, groq_ok

REPORT = "Project Alpha report.md"

# The plan from the failure report: folder -> upload -> read (no destination) -> list.
REPORTED = DraftPlan(summary="Project folder, report, verify, list.", steps=[
    step(1, "create_folder", name="Project Alpha", folder="sandbox"),
    step(2, "create_document", [1], title=REPORT, content="# Project Alpha\n- synthetic\n", parent_step="1"),
    step(3, "read_document", [2], document_name=REPORT),
    step(4, "list_files", [1], parent_step="1"),
])


def args_of(body_step: dict) -> dict:
    return body_step["arguments"]


# ======================= the reported failure =======================


def test_reported_groq_plan_is_now_planned_with_the_existing_reference_contract(real_api):
    real_api(gemini_outcomes=(gemini_503(),), groq_outcomes=(groq_ok(REPORTED),))
    response = post()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["planner"] == "groq" and body["fallback_used"] is True and body["status"] == "PLANNED"
    read = body["planned_steps"][2]
    assert read["action"] == "read_document"
    # Same title + the folder of the create_document step: no new schema field.
    assert args_of(read) == {"document_name": REPORT, "parent_step": "1"}
    assert read["depends_on"] == [1, 2]
    assert body["plan_adjustments"] == [
        f"Step 3 (read_document): added parent_step=1, the folder of step 2 which creates '{REPORT}'."]
    # Everything the planner set itself is untouched.
    assert args_of(body["planned_steps"][0])["folder"] == "sandbox"
    assert args_of(body["planned_steps"][3]) == {"parent_step": "1"}


def test_reported_plan_executes_and_verifies_the_report_in_the_fake_drive(real_api, tmp_path):
    real_api(gemini_outcomes=(gemini_503(),), groq_outcomes=(groq_ok(REPORTED),))
    plan = post().json()
    world = DriveWorld(tmp_path)
    response = world.service.execute(execution_request(plan), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED, response.errors
    folder, upload, read, listing = response.step_results
    assert world.google.files[folder.resource_id]["parents"] == [SANDBOX_ID]
    assert read.resource_id == upload.resource_id and read.output["md5"] == upload.output["md5"]
    assert listing.output["file_count"] == 1


def test_reported_plan_still_needs_the_operator_token_and_rolls_back(real_api, tmp_path):
    real_api(gemini_outcomes=(gemini_503(),), groq_outcomes=(groq_ok(REPORTED),))
    plan = post().json()
    world = DriveWorld(tmp_path)
    assert [e.code for e in world.service.execute(execution_request(plan), write_authorized=False).errors] == [
        "REAL_WRITE_AUTHORIZATION_REQUIRED"]
    world.google.failures[("GET", r"^/drive/v3/files$")] = [None, None, 404]  # dedup x2, then the read fails
    failed = world.service.execute(execution_request(plan), write_authorized=True)
    assert failed.status is ExecutionStatus.ROLLED_BACK
    assert [c.step_number for c in failed.compensation_results] == [2, 1]


# ======================= valid plans are unchanged =======================


def test_valid_drive_plan_needs_no_adjustment(real_api):
    real_api(gemini_outcomes=(ok_response(DraftPlan.model_validate_json(drive_example("sandbox"))),))
    body = post().json()
    assert body["status"] == "PLANNED" and body["planner"] == "gemini" and body["plan_adjustments"] == []


def test_prompt_example_is_a_valid_drive_only_plan():
    plan, issues = validate_draft(DraftPlan.model_validate_json(drive_example("sandbox")), max_steps=10)
    assert plan is not None and issues == []
    assert [s.action.value for s in plan.steps] == ["create_folder", "create_document", "read_document", "list_files"]
    assert drive_destination_issues(plan.steps, ["sandbox"]) == []


def test_gemini_and_groq_prompts_carry_the_rules_and_the_example(real_api):
    gemini, groq = real_api(gemini_outcomes=(gemini_503(),))
    post()
    for prompt in (gemini.models.calls[0]["config"].system_instruction, groq.calls[0]["messages"][0]["content"]):
        assert "destination (exactly one, required): folder or parent_step" in prompt
        assert "document_name alone is NOT enough" in prompt
        assert '"action": "read_document"' in prompt and '{"name": "parent_step", "value": "1"}' in prompt
        assert "optional args: folder, parent_step" not in prompt  # the misleading wording is gone


# ======================= normalization: only unambiguous, never a guess =======================


def normalize(*steps):
    return normalize_drive_references(DraftPlan(summary="s", steps=list(steps)), ["sandbox"])


def test_missing_references_with_exactly_one_possible_value_are_filled():
    draft, notes = normalize(
        step(1, "create_folder", name="Alpha"),
        step(2, "create_document", title="r.md", content="c"),
        step(3, "list_files"),
        step(4, "read_document", document_name="r.md"),
    )
    got = [{a.name: a.value for a in s.arguments} for s in draft.steps]
    assert got[0]["folder"] == "sandbox" and got[1]["parent_step"] == "1" and got[2]["parent_step"] == "1"
    assert got[3]["parent_step"] == "1" and draft.steps[3].depends_on == [1, 2]
    assert len(notes) == 4
    plan, issues = validate_draft(draft, max_steps=10)
    assert plan is not None and drive_destination_issues(plan.steps, ["sandbox"]) == []


@pytest.mark.parametrize("steps", [
    # two folders: which one holds the document is ambiguous
    [step(1, "create_folder", name="A", folder="sandbox"), step(2, "create_folder", name="B", folder="sandbox"),
     step(3, "create_document", [1, 2], title="r", content="c")],
    # second create_folder: top-level or sub-folder is ambiguous
    [step(1, "create_folder", name="A", folder="sandbox"), step(2, "create_folder", name="B")],
    # read of a document this plan does not create
    [step(1, "create_folder", name="A", folder="sandbox"), step(2, "read_document", [1], document_name="other.md")],
    # two created documents with the same title
    [step(1, "create_folder", name="A", folder="sandbox"), step(2, "create_folder", [1], name="B", parent_step="1"),
     step(3, "create_document", [1], title="r", content="c", parent_step="1"),
     step(4, "create_document", [2], title="r", content="c", parent_step="2"),
     step(5, "read_document", [3, 4], document_name="r")],
    # list with no folder in the plan at all
    [step(1, "list_files")],
])
def test_ambiguous_references_are_not_guessed_and_stay_rejected(steps):
    draft, notes = normalize_drive_references(DraftPlan(summary="s", steps=steps), ["sandbox"])
    plan, issues = validate_draft(draft, max_steps=10)
    remaining = drive_destination_issues(plan.steps, ["sandbox"]) if plan is not None else issues
    assert any(i.code == "DESTINATION_REQUIRED" for i in remaining)


def test_create_folder_alias_is_not_filled_when_several_aliases_are_approved():
    draft, notes = normalize_drive_references(DraftPlan(summary="s", steps=[step(1, "create_folder", name="A")]),
                                              ["sandbox", "other"])
    assert notes == [] and draft.steps[0].arguments == [StepArgument(name="name", value="A")]


def test_destinations_given_by_the_planner_are_never_changed():
    bad = DraftPlan(summary="s", steps=[
        step(1, "create_folder", name="A", folder="reports"),
        step(2, "read_document", [1], document_name="x", folder="1AbCdEfRawFolderId"),
    ])
    draft, notes = normalize_drive_references(bad, ["sandbox"])
    assert notes == [] and draft == bad
    plan, _ = validate_draft(draft, max_steps=10)
    assert [i.code for i in drive_destination_issues(plan.steps, ["sandbox"])] == ["DESTINATION_NOT_APPROVED"] * 2


def test_normalization_is_off_without_drive_restrictions():
    draft, notes = normalize_drive_references(REPORTED, None)
    assert notes == [] and draft == REPORTED


def test_cross_integration_is_still_rejected_after_normalization(real_api):
    mixed = DraftPlan(summary="s", steps=[
        *REPORTED.steps,
        DraftStep(step_number=5, integration="notion", action="create_page", description="d",
                  arguments=[StepArgument(name="title", value="t"), StepArgument(name="content", value="c")]),
    ])
    real_api(gemini_outcomes=(gemini_503(),), groq_outcomes=(groq_ok(mixed),))
    body = post().json()
    assert body["status"] == "INVALID" and "INTEGRATION_NOT_ALLOWED" in [i["code"] for i in body["validation"]["issues"]]


def test_unresolvable_reported_shape_is_still_rejected(real_api):
    """read_document of a title no step creates: no guess, the original error remains."""
    unresolvable = DraftPlan(summary="s", steps=[
        step(1, "create_folder", name="A", folder="sandbox"),
        step(2, "create_document", [1], title=REPORT, content="c", parent_step="1"),
        step(3, "read_document", [2], document_name="Some other file.md"),
    ])
    real_api(gemini_outcomes=(gemini_503(),), groq_outcomes=(groq_ok(unresolvable),))
    body = post().json()
    assert body["status"] == "INVALID" and body["plan_adjustments"] == []
    assert [(i["code"], i["step_number"]) for i in body["validation"]["issues"]] == [("DESTINATION_REQUIRED", 3)]


def test_wrong_parent_step_from_the_planner_is_rejected_not_rewritten(real_api):
    """parent_step must point at a create_folder; pointing at the document is not 'fixed'."""
    wrong = DraftPlan(summary="s", steps=[
        step(1, "create_folder", name="A", folder="sandbox"),
        step(2, "create_document", [1], title=REPORT, content="c", parent_step="1"),
        step(3, "read_document", [2], document_name=REPORT, parent_step="2"),
    ])
    real_api(gemini_outcomes=(gemini_503(),), groq_outcomes=(groq_ok(wrong),))
    body = post().json()
    assert body["status"] == "INVALID" and "INVALID_PARENT_STEP" in [i["code"] for i in body["validation"]["issues"]]


def test_prompt_without_drive_restrictions_has_no_example():
    assert "Valid Google Drive-only example" not in build_system_prompt(10)
