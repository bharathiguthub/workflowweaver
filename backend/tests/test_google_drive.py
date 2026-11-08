"""Phase 6.1 Google Drive integration: OAuth (drive.file only), the REST adapter, sandbox
confinement, verification, idempotency, retries, Saga rollback, mock / dry-run / real modes,
the API endpoints, SSE events, Langfuse tracing and secret hygiene.

Google is replaced by tests.drive_fakes.FakeGoogle (httpx.MockTransport): no network, no real
OAuth consent, no real Drive writes.
"""

import json
import logging
import threading
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools import MockToolRegistry
from app.agents.execution.tools.base import CompensationCall, Effect, ErrorKind, ToolError
from app.agents.execution.tools.google_drive import GoogleDriveRestAdapter, md5_hex
from app.agents.planners import FallbackPlanner
from app.agents.project_workflow import ProjectPlanDraft
from app.agents.service import PlanningService
from app.agents.validation import validate_draft
from app.api import drive_sandbox as drive_api
from app.api import integrations as integrations_api
from app.core.config import DriveTransport
from app.core.log_redaction import install_secret_redaction, redact
from app.integrations.config import load_integration_config
from app.integrations.errors import IntegrationConfigError, IntegrationError
from app.integrations.google import oauth
from app.integrations.google.drive import KEY_PROPERTY, RUN_PROPERTY, q_literal
from app.integrations.google.oauth import DRIVE_FILE_SCOPE, TokenProvider
from app.main import app
from app.observability import set_telemetry
from app.observability.sanitize import scrub_text
from app.schemas.execution import ExecutionRequest, ExecutionStatus
from app.schemas.plan import Action, DraftPlan, DraftStep, PlanStatus, StepArgument
from app.schemas.workflow import Integration
from app.streaming.broker import EventBroker
from app.streaming.runner import ExecutionRunner
from scripts import google_drive_auth, google_drive_sandbox
from tests.drive_fakes import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    GOOGLE_SECRETS,
    OTHER_FOLDER_ID,
    REFRESH_TOKEN,
    SANDBOX_ID,
    DriveWorld,
    FakeGoogle,
    drive_settings,
    write_token,
)
from tests.integration_fakes import OPERATOR_TOKEN, SECRETS
from tests.telemetry_helpers import RecordingTelemetry
from tests.test_planner_fallback import FakeGroqClient, gemini_503, groq_ok
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient
from app.agents.planners import GeminiPlanner, GroqPlanner
from tests.test_project_workflow import rewards_project

PLAN_URL = "/api/v1/workflows/drive-sandbox/plan"
STATUS_URL = "/api/v1/integrations/status"
CONNECTIVITY_URL = "/api/v1/integrations/google-drive/connectivity"


def assert_no_secrets(text: str) -> None:
    leaked = [s for s in (*GOOGLE_SECRETS, *SECRETS, SANDBOX_ID, OTHER_FOLDER_ID) if s in text]
    assert not leaked, f"secret values leaked: {leaked}"


def step(n, action, depends_on=(), **args) -> dict:
    return {"step_number": n, "integration": "google_drive", "action": action, "description": f"step {n}",
            "arguments": args, "depends_on": list(depends_on)}


def request(*steps, mode="real", approved=True, workflow_id=None) -> ExecutionRequest:
    return ExecutionRequest.model_validate({
        "workflow_id": workflow_id or str(uuid4()), "plan": {"summary": "Drive test", "steps": list(steps)},
        "approval": {"approved": approved, "mode": mode, "approved_by": "synthetic.reviewer"}})


SANDBOX_RUN = (
    step(1, "create_folder", name="WorkflowWeaver run synthetic", folder="sandbox"),
    step(2, "create_document", [1], title="Project report.md", content="# Synthetic report\n- item\n",
         parent_step="1"),
    step(3, "list_files", [1, 2], parent_step="1"),
)


def codes(response) -> list[str]:
    return [e.code for e in response.errors]


# ======================= OAuth: scope, PKCE, token file =======================


def test_authorization_url_requests_only_drive_file_with_pkce_and_offline_access():
    url = httpx.URL(oauth.authorization_url(client_id=CLIENT_ID, redirect_uri="http://127.0.0.1:5555/",
                                            state="synthetic-state", code_challenge="challenge"))
    params = dict(url.params)
    assert params["scope"] == DRIVE_FILE_SCOPE == "https://www.googleapis.com/auth/drive.file"
    assert params["code_challenge_method"] == "S256" and params["access_type"] == "offline"
    assert params["include_granted_scopes"] == "false" and params["state"] == "synthetic-state"
    assert "client_secret" not in params


def test_pkce_challenge_is_s256_of_the_verifier():
    import base64
    import hashlib

    verifier, challenge = oauth.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.mark.parametrize("scope, ok", [
    (DRIVE_FILE_SCOPE, True),
    ("https://www.googleapis.com/auth/drive", False),
    (f"{DRIVE_FILE_SCOPE} https://www.googleapis.com/auth/drive.readonly", False),
    ("https://www.googleapis.com/auth/drive.metadata", False),
    ("", False),
])
def test_only_the_drive_file_scope_is_accepted(scope, ok):
    assert (oauth.scope_problem(scope) is None) is ok


def test_token_file_must_be_outside_the_repository(tmp_path):
    with pytest.raises(IntegrationConfigError, match="outside the repository"):
        oauth.resolve_token_path(str(oauth.REPO_ROOT / "google-drive-token.json"))
    with pytest.raises(IntegrationConfigError, match="absolute"):
        oauth.resolve_token_path("relative/token.json")
    assert oauth.resolve_token_path(str(tmp_path / "t.json")) == (tmp_path / "t.json").resolve()
    assert oauth.REPO_ROOT not in oauth.resolve_token_path(None).parents


def test_token_file_round_trip_has_no_client_secret_or_access_token(tmp_path):
    path = write_token(tmp_path)
    stored = oauth.load_token(path)
    assert stored.refresh_token == REFRESH_TOKEN and stored.scope == DRIVE_FILE_SCOPE and stored.client_id == CLIENT_ID
    text = path.read_text()
    assert CLIENT_SECRET not in text and ACCESS_TOKEN not in text
    assert REFRESH_TOKEN not in repr(stored)


def test_broader_scope_is_never_saved_or_loaded(tmp_path):
    with pytest.raises(IntegrationConfigError):
        oauth.save_token(tmp_path / "x.json", refresh_token=REFRESH_TOKEN,
                         scope="https://www.googleapis.com/auth/drive", client_id=CLIENT_ID)
    path = write_token(tmp_path, scope="https://www.googleapis.com/auth/drive")
    with pytest.raises(IntegrationError) as info:
        oauth.load_token(path)
    assert info.value.code == "GOOGLE_OAUTH_SCOPE_REJECTED"


def provider(tmp_path, google: FakeGoogle, **kwargs) -> TokenProvider:
    return TokenProvider(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                         token_path=tmp_path / "google-drive-token.json", http=google.client(), **kwargs)


def test_access_token_is_cached_until_shortly_before_expiry(tmp_path):
    write_token(tmp_path)
    google = FakeGoogle()
    now = [0.0]
    tokens = provider(tmp_path, google, clock=lambda: now[0])
    assert tokens.access_token() == ACCESS_TOKEN and tokens.access_token() == ACCESS_TOKEN
    assert google.token_requests == 1
    now[0] = 3599 - 30  # inside the 60 s safety margin
    tokens.access_token()
    assert google.token_requests == 2


@pytest.mark.parametrize("setup, code", [
    (lambda g, p: None, "GOOGLE_OAUTH_NOT_AUTHORIZED"),  # no token file
    (lambda g, p: (write_token(p), setattr(g, "token_status", 400)), "GOOGLE_OAUTH_REAUTH_REQUIRED"),
    (lambda g, p: (write_token(p), setattr(g, "token_status", 401), setattr(g, "token_error", "invalid_client")),
     "GOOGLE_OAUTH_CLIENT_REJECTED"),
    (lambda g, p: write_token(p, client_id="other.apps.googleusercontent.com"), "GOOGLE_OAUTH_CLIENT_MISMATCH"),
    (lambda g, p: (write_token(p), setattr(g, "token_scope", "https://www.googleapis.com/auth/drive")),
     "GOOGLE_OAUTH_SCOPE_REJECTED"),
])
def test_token_problems_are_permission_errors_without_provider_text(tmp_path, setup, code):
    google = FakeGoogle()
    setup(google, tmp_path)
    with pytest.raises(IntegrationError) as info:
        provider(tmp_path, google).access_token()
    assert info.value.code == code and info.value.kind is ErrorKind.PERMISSION
    assert "SYNTHETIC-PROVIDER-DETAIL" not in info.value.message


def test_code_exchange_requires_refresh_token_and_drive_file_scope():
    def server(scope, refresh=True):
        def handle(request):
            body = {"access_token": ACCESS_TOKEN, "scope": scope}
            if refresh:
                body["refresh_token"] = REFRESH_TOKEN
            return httpx.Response(200, json=body)
        return httpx.Client(transport=httpx.MockTransport(handle))

    kwargs = dict(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="4/0SYNTH", code_verifier="v",
                  redirect_uri="http://127.0.0.1:1/")
    assert oauth.exchange_code(server(DRIVE_FILE_SCOPE), **kwargs)["refresh_token"] == REFRESH_TOKEN
    with pytest.raises(IntegrationError, match="no refresh token"):
        oauth.exchange_code(server(DRIVE_FILE_SCOPE, refresh=False), **kwargs)
    with pytest.raises(IntegrationError) as info:
        oauth.exchange_code(server("https://www.googleapis.com/auth/drive"), **kwargs)
    assert info.value.code == "GOOGLE_OAUTH_SCOPE_REJECTED"


# ======================= scripts/google_drive_auth.py (loopback + PKCE, faked) =======================


def test_callback_rejects_state_mismatch_and_reports_errors():
    cb = google_drive_auth.Callback(expected_state="good")
    cb.handle("state=evil&code=4/0SYNTH")
    assert cb.code is None and "state mismatch" in cb.error
    cb = google_drive_auth.Callback(expected_state="good")
    cb.handle("state=good&error=access_denied")
    assert cb.error == "access_denied" and cb.code is None
    cb = google_drive_auth.Callback(expected_state="good")
    cb.handle("state=good&code=4/0SYNTH")
    assert cb.code == "4/0SYNTH" and cb.error is None


def test_authorize_flow_saves_only_a_drive_file_refresh_token(tmp_path, capsys):
    settings = drive_settings("dry_run", tmp_path)
    exchanged: list[dict] = []

    def token_endpoint(request):
        form = {k: v[0] for k, v in __import__("urllib.parse").parse.parse_qs(request.content.decode()).items()}
        exchanged.append(form)
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN,
                                         "scope": DRIVE_FILE_SCOPE, "expires_in": 3599})

    def browser(url):  # the "user" consents: Google redirects to the loopback server
        params = dict(httpx.URL(url).params)
        assert params["scope"] == DRIVE_FILE_SCOPE
        threading.Thread(target=lambda: httpx.get(
            params["redirect_uri"], params={"state": params["state"], "code": "4/0SYNTHETIC-code"})).start()

    rc = google_drive_auth.authorize(settings, open_browser=browser,
                                     http=httpx.Client(transport=httpx.MockTransport(token_endpoint)), timeout=10)
    assert rc == 0
    assert exchanged[0]["grant_type"] == "authorization_code" and exchanged[0]["code_verifier"]
    assert exchanged[0]["redirect_uri"].startswith("http://127.0.0.1:")
    assert oauth.load_token(tmp_path / "google-drive-token.json").scope == DRIVE_FILE_SCOPE
    out = capsys.readouterr().out
    assert REFRESH_TOKEN not in out and ACCESS_TOKEN not in out and CLIENT_SECRET not in out
    assert "4/0SYNTHETIC-code" not in out


def test_authorize_with_state_mismatch_saves_nothing(tmp_path):
    settings = drive_settings("dry_run", tmp_path)

    def browser(url):
        params = dict(httpx.URL(url).params)
        threading.Thread(target=lambda: httpx.get(params["redirect_uri"],
                                                  params={"state": "forged", "code": "4/0X"})).start()

    assert google_drive_auth.authorize(settings, open_browser=browser, timeout=10) == 1
    assert not (tmp_path / "google-drive-token.json").exists()


# ======================= configuration =======================


def test_default_transport_is_unchanged_mcp():
    from tests.integration_fakes import integration_settings

    config = load_integration_config(integration_settings("real"))
    assert config.drive_transport is DriveTransport.MCP and config.drive_rest is None


@pytest.mark.parametrize("overrides, setting", [
    ({"google_oauth_client_secret": None}, "GOOGLE_OAUTH_CLIENT_SECRET"),
    ({"google_oauth_client_id": "not-an-oauth-client"}, "GOOGLE_OAUTH_CLIENT_ID"),
])
def test_invalid_oauth_client_settings_name_the_setting(tmp_path, overrides, setting):
    with pytest.raises(IntegrationConfigError, match=setting):
        load_integration_config(drive_settings("real", tmp_path, **overrides))


def test_rest_config_never_reveals_the_secret(tmp_path):
    config = load_integration_config(drive_settings("real", tmp_path))
    assert config.drive_rest is not None and CLIENT_SECRET not in repr(config.drive_rest)


# ======================= real mode: the sandbox workflow =======================


def test_real_sandbox_run_creates_folder_uploads_verified_report_and_lists_it(tmp_path):
    world = DriveWorld(tmp_path)
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED, response.errors
    folder, report, listing = response.step_results
    run_folder = world.google.files[folder.resource_id]
    assert run_folder["parents"] == [SANDBOX_ID]  # inside the sandbox
    uploaded = world.google.files[report.resource_id]
    assert uploaded["parents"] == [folder.resource_id] and uploaded["mimeType"] == "text/markdown"
    assert uploaded["content"] == b"# Synthetic report\n- item\n"
    assert report.output["verified"] is True and report.output["md5"] == md5_hex(uploaded["content"])
    assert report.output["size_bytes"] == len(uploaded["content"])
    assert listing.output["file_count"] == 1 and listing.output["files"][0]["name"] == "Project report.md"
    # Every created resource carries the private idempotency marker of its step.
    key = f"{response.workflow_id}:step-2"
    assert uploaded["appProperties"] == {KEY_PROPERTY: key, RUN_PROPERTY: str(response.workflow_id)}
    assert_no_secrets(response.model_dump_json())


def test_sandbox_folder_without_marker_is_refused_before_any_write(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.files[SANDBOX_ID]["appProperties"] = {}  # a real folder id pasted by mistake
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.status is ExecutionStatus.FAILED
    assert response.step_results[0].error.code == "SANDBOX_FOLDER_INVALID"
    assert world.google.writes == []


def test_sandbox_folder_not_visible_to_the_app_is_refused(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.files[SANDBOX_ID]["app_created"] = False  # created in the Drive UI: invisible under drive.file
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.step_results[0].error.code == "SANDBOX_FOLDER_INVALID" and world.google.writes == []


@pytest.mark.parametrize("bad_step, code", [
    (step(1, "create_folder", name="x", folder="reports"), "DESTINATION_NOT_APPROVED"),  # approved, but not sandbox
    (step(1, "create_folder", name="x", folder=OTHER_FOLDER_ID), "DESTINATION_NOT_APPROVED"),  # raw id
    (step(1, "create_document", title="r", content="c", folder="unknown"), "DESTINATION_NOT_APPROVED"),
    (step(1, "list_files", folder="reports"), "DESTINATION_NOT_APPROVED"),
    (step(1, "create_document", title="r", content="c"), "DESTINATION_NOT_APPROVED"),  # no folder, no default
])
def test_writes_and_reads_outside_the_sandbox_are_rejected_before_any_call(tmp_path, bad_step, code):
    world = DriveWorld(tmp_path)
    response = world.service.execute(request(bad_step), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == [code]
    assert world.google.calls == []  # not even a token refresh


def test_failure_after_upload_rolls_back_report_then_folder(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.failures[("GET", r"^/drive/v3/files$")] = [None, None, 404]  # dedup x2 ok, then listing fails
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.status is ExecutionStatus.ROLLED_BACK
    assert [c.step_number for c in response.compensation_results] == [2, 1]  # newest first
    assert all(c.status.value == "COMPENSATED" for c in response.compensation_results)
    created = [f for f in world.google.files.values() if f["id"] != SANDBOX_ID]
    assert len(created) == 2 and all(f["trashed"] for f in created)
    assert not world.google.files[SANDBOX_ID]["trashed"]  # the sandbox itself is never touched
    assert not [c for c in world.google.calls if c[0] == "DELETE"]  # trash only, never permanent delete


def test_verification_mismatch_trashes_the_upload_and_rolls_back(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.corrupt_upload = True
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.status is ExecutionStatus.ROLLED_BACK
    assert response.step_results[1].error.code == "UPLOAD_VERIFICATION_FAILED"
    assert all(f["trashed"] for f in world.google.files.values() if f["id"] != SANDBOX_ID)


def test_lost_response_after_upload_is_retried_without_a_duplicate(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.failures[("POST", r"^/upload/")] = [("after", 503)]  # applied, but client sees 503
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED, response.errors
    assert response.attempts["2"] == 2 and response.step_results[1].output["reused"] is True
    reports = [f for f in world.google.files.values() if f["mimeType"] == "text/markdown"]
    assert len(reports) == 1  # remote idempotency marker prevented a duplicate


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
def test_transient_failures_are_retried_with_backoff(tmp_path, failure):
    world = DriveWorld(tmp_path)
    world.google.failures[("POST", r"^/drive/v3/files$")] = [failure]
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED and response.attempts["1"] == 2
    assert world.sleeps == [0.2]
    assert len([f for f in world.google.files.values() if f["mimeType"].endswith("folder")]) == 2  # sandbox + 1


@pytest.mark.parametrize("failure", [[403], [401, 401]])  # 403 forbidden; 401 even after a token refresh
def test_permission_denied_is_not_retried(tmp_path, failure):
    world = DriveWorld(tmp_path)
    world.google.failures[("POST", r"^/drive/v3/files$")] = failure
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.status is ExecutionStatus.FAILED
    assert response.step_results[0].error.code == "GOOGLE_DRIVE_PERMISSION_DENIED"
    assert response.attempts["1"] == 1


def test_compensation_refuses_files_this_run_did_not_create(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.add_folder("1ForeignAppFolder", "not ours", parent=SANDBOX_ID, props={})
    adapter: GoogleDriveRestAdapter = world.adapter
    call = CompensationCall("wf-1", 1, Integration.GOOGLE_DRIVE, Action.CREATE_FOLDER, "1ForeignAppFolder")
    with pytest.raises(ToolError) as info:
        adapter._undo(call)
    assert info.value.code == "COMPENSATION_REFUSED" and not world.google.files["1ForeignAppFolder"]["trashed"]


def test_compensation_of_an_already_deleted_file_succeeds(tmp_path):
    world = DriveWorld(tmp_path)
    call = CompensationCall("wf-1", 1, Integration.GOOGLE_DRIVE, Action.CREATE_FOLDER, "1DoesNotExist")
    world.adapter._undo(call)  # no exception


def test_duplicate_request_replays_without_new_drive_calls(tmp_path):
    world = DriveWorld(tmp_path)
    req = request(*SANDBOX_RUN)
    first = world.service.execute(req, write_authorized=True)
    calls = len(world.google.calls)
    second = world.service.execute(req, write_authorized=True)
    assert second.replayed is True and first.status is ExecutionStatus.COMPLETED
    assert len(world.google.calls) == calls


# ======================= approval, operator authorization, kill switch =======================


def test_unapproved_plan_makes_no_drive_call(tmp_path):
    world = DriveWorld(tmp_path)
    response = world.service.execute(request(*SANDBOX_RUN, approved=False), write_authorized=True)
    assert codes(response) == ["APPROVAL_REQUIRED"] and world.google.calls == []


def test_operator_token_and_kill_switch_still_gate_drive_writes(tmp_path):
    no_token = DriveWorld(tmp_path)
    assert codes(no_token.service.execute(request(*SANDBOX_RUN), write_authorized=False)) == [
        "REAL_WRITE_AUTHORIZATION_REQUIRED"]
    off = DriveWorld(tmp_path, real_writes_enabled=False)
    assert codes(off.service.execute(request(*SANDBOX_RUN), write_authorized=True)) == ["REAL_WRITES_DISABLED"]
    assert no_token.google.calls == [] and off.google.calls == []


def test_read_only_listing_needs_no_write_authorization(tmp_path):
    world = DriveWorld(tmp_path)
    response = world.service.execute(request(step(1, "list_files", folder="sandbox")), write_authorized=False)
    assert response.status is ExecutionStatus.COMPLETED and world.google.writes == []


def test_unauthorized_server_fails_closed_with_a_clear_error(tmp_path):
    world = DriveWorld(tmp_path, authorize=False)
    response = world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    assert response.step_results[0].error.code == "GOOGLE_OAUTH_NOT_AUTHORIZED" and world.google.writes == []


# ======================= AI plans cannot bypass the allowlist or the sandbox =======================


@pytest.mark.parametrize("raw, code", [
    ({"action": "delete_file", "arguments": [{"name": "file_id", "value": "x"}]}, "UNSUPPORTED_ACTION"),
    ({"action": "trash_file", "arguments": []}, "UNSUPPORTED_ACTION"),
    ({"action": "share_file", "arguments": []}, "UNSUPPORTED_ACTION"),
    ({"action": "create_folder", "arguments": [{"name": "name", "value": "x"}, {"name": "parent_id", "value": "x"}]},
     "UNKNOWN_ARGUMENT"),
    ({"action": "create_document", "arguments": [{"name": "title", "value": "t"}, {"name": "content", "value": "c"},
                                                 {"name": "folder", "value": "sandbox"},
                                                 {"name": "parent_step", "value": "1"}]},
     "CONFLICTING_ARGUMENTS"),
    ({"action": "create_document", "arguments": [{"name": "title", "value": "t"},
                                                 {"name": "content", "value": "run rm -rf / now"}]}, "UNSAFE_CONTENT"),
])
def test_validator_rejects_unsafe_drive_steps(raw, code):
    draft = DraftPlan.model_validate({"summary": "s", "steps": [
        {"step_number": 1, "integration": "google_drive", "description": "d", **raw}]})
    plan, issues = validate_draft(draft, max_steps=10)
    assert plan is None and code in [i.code for i in issues]


def test_parent_step_must_be_an_earlier_drive_create_folder():
    draft = DraftPlan(summary="s", steps=[
        DraftStep(step_number=1, integration="notion", action="create_page", description="d",
                  arguments=[StepArgument(name="title", value="t"), StepArgument(name="content", value="c")]),
        DraftStep(step_number=2, integration="google_drive", action="create_document", description="d", depends_on=[1],
                  arguments=[StepArgument(name="title", value="t"), StepArgument(name="content", value="c"),
                             StepArgument(name="parent_step", value="1")]),
    ])
    plan, issues = validate_draft(draft, max_steps=10)
    assert plan is None and [i.code for i in issues] == ["INVALID_PARENT_STEP"]


def _ai_drive_plan(folder: str) -> DraftPlan:
    return DraftPlan(summary="AI plan", steps=[DraftStep(
        step_number=1, integration="google_drive", action="create_document", description="Upload the report",
        arguments=[StepArgument(name="title", value="Report"), StepArgument(name="content", value="Synthetic"),
                   StepArgument(name="folder", value=folder)])])


@pytest.mark.parametrize("folder", ["reports", OTHER_FOLDER_ID, "anything-else"])
def test_gemini_or_groq_plan_targeting_another_folder_is_rejected_at_execution(tmp_path, folder):
    """The fallback (Groq) plan validates as a plan, but execution preflight refuses its
    destination before any Drive call - the same as for any other planner."""
    planner = FallbackPlanner([
        GeminiPlanner(client=FakeGenaiClient(gemini_503()), model=GEMINI_MODEL, max_output_tokens=1024),
        GroqPlanner(client=FakeGroqClient(groq_ok(_ai_drive_plan(folder))), model="openai/gpt-oss-120b",
                    max_tokens=1024),
    ])
    planned = PlanningService(planner, max_steps=10, max_planning_attempts=1).plan("Upload the report to Drive")
    assert planned.status is PlanStatus.PLANNED and planned.planner == "groq"
    world = DriveWorld(tmp_path)
    steps = [s.model_dump(mode="json") for s in planned.planned_steps]
    response = world.service.execute(request(*steps), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]
    assert world.google.calls == []


def test_ai_planner_prompt_lists_drive_actions_but_no_delete():
    from app.agents.planners.claude import build_system_prompt

    prompt = build_system_prompt(10)
    assert "action=create_folder" in prompt and "action=list_files" in prompt
    assert "delete" not in prompt.lower() and "trash" not in prompt.lower()


def test_mcp_transport_does_not_support_the_new_actions():
    from app.agents.execution.tools.integrations import build_integration_registry
    from tests.integration_fakes import integration_settings

    registry = build_integration_registry(integration_settings("dry_run"))
    assert (Integration.GOOGLE_DRIVE, Action.CREATE_FOLDER) not in registry.supported_actions()
    assert (Integration.GOOGLE_DRIVE, Action.CREATE_DOCUMENT) in registry.supported_actions()


# ======================= dry run and mock =======================


def test_dry_run_builds_requests_and_sends_nothing(tmp_path):
    world = DriveWorld(tmp_path, mode="dry_run", authorize=False)
    response = world.service.execute(request(*SANDBOX_RUN, mode="dry_run"))
    assert response.status is ExecutionStatus.COMPLETED
    folder, report, listing = response.step_results
    assert all(r.output["sent"] is False for r in response.step_results)
    assert folder.output["destination"] == "sandbox" and folder.resource_id.startswith("dryrun-google_drive-")
    assert report.output["destination"] == "folder from step 1"
    assert report.output["md5"] == md5_hex(b"# Synthetic report\n- item\n") and report.output["verified"] is False
    assert "files.list" in listing.output["would_call"]
    assert world.google.calls == []


def test_dry_run_still_enforces_the_sandbox(tmp_path):
    world = DriveWorld(tmp_path, mode="dry_run", authorize=False)
    response = world.service.execute(request(step(1, "create_folder", name="x", folder="reports"), mode="dry_run"))
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]


def test_mock_mode_runs_the_sandbox_workflow_in_memory():
    service = ExecutionService(MockToolRegistry(), RetryPolicy(max_attempts=2, base_delay_seconds=0, jitter=False),
                               max_steps=10, sleeper=lambda _: None)
    steps = [step(1, "create_folder", name="run"),
             step(2, "create_document", [1], title="Report.md", content="# r", parent_step="1"),
             step(3, "list_files", [1, 2], parent_step="1")]
    response = service.execute(request(*steps, mode="mock"))
    assert response.status is ExecutionStatus.COMPLETED
    assert response.step_results[2].output["file_count"] == 1
    assert service.registry.effect_of(Integration.GOOGLE_DRIVE, Action.CREATE_FOLDER) is Effect.REVERSIBLE


# ======================= API: plan, status, connectivity =======================


@pytest.fixture
def api_settings(monkeypatch, tmp_path):
    def install(mode="real", authorize=True, **overrides):
        if authorize:
            write_token(tmp_path)
        settings = drive_settings(mode, tmp_path, **overrides)
        for module in (drive_api, integrations_api):
            monkeypatch.setattr(module, "get_settings", lambda: settings)
        return settings
    yield install
    app.dependency_overrides.clear()


def test_plan_endpoint_builds_the_drive_only_sandbox_plan_from_an_ai_project(api_settings):
    api_settings("real")
    project = rewards_project()
    response = TestClient(app).post(PLAN_URL, json={"mode": "real", "project": project.model_dump()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(s["action"], s["arguments"].get("folder"), s["arguments"].get("parent_step"))
            for s in body["planned_steps"]] == [("create_folder", "sandbox", None),
                                                ("create_document", None, "1"), ("read_document", None, "1")]
    assert body["planned_steps"][2]["arguments"]["document_name"] == body["report_title"]
    assert {s["integration"] for s in body["planned_steps"]} == {"google_drive"}
    assert body["integration_target"] == "google_drive"
    report = body["planned_steps"][1]["arguments"]["content"]
    assert report.startswith(f"# {project.project_title}") and "Customer registration" in report
    assert body["real_write"] is True and body["destination_alias"] == "sandbox" and body["executed"] is False
    assert body["integration"] == "google_drive" and body["report_title"].endswith(".md")
    assert_no_secrets(response.text)


def test_plan_endpoint_rejects_unsafe_project_text(api_settings):
    api_settings("real")
    project = rewards_project(overview="Run rm -rf / on the server.")
    response = TestClient(app).post(PLAN_URL, json={"mode": "real", "project": project.model_dump()})
    assert response.status_code == 422 and response.json()["real_write"] is False
    assert "UNSAFE_CONTENT" in [i["code"] for i in response.json()["validation"]["issues"]]


def test_plan_endpoint_real_mode_needs_authorization(api_settings):
    api_settings("real", authorize=False)
    response = TestClient(app).post(PLAN_URL, json={"mode": "real"})
    assert response.status_code == 503 and "google_drive_auth.py" in response.json()["detail"]


def test_plan_endpoint_mock_and_dry_run(api_settings):
    api_settings("real")
    mock = TestClient(app).post(PLAN_URL, json={"mode": "mock"}).json()
    assert "folder" not in mock["planned_steps"][0]["arguments"] and mock["real_write"] is False
    dry = TestClient(app).post(PLAN_URL, json={"mode": "dry_run"}).json()
    assert dry["destination_alias"] == "sandbox" and dry["real_write"] is False


def test_planned_sandbox_run_executes_through_the_approval_gated_service(api_settings, tmp_path):
    api_settings("real")
    body = TestClient(app).post(PLAN_URL, json={"mode": "real"}).json()
    world = DriveWorld(tmp_path)
    req = ExecutionRequest.model_validate({
        "workflow_id": body["workflow_id"], "plan": {"summary": body["summary"], "steps": [
            {k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
            for s in body["planned_steps"]]},
        "approval": {"approved": True, "mode": "real", "approved_by": "synthetic.reviewer"}})
    response = world.service.execute(req, write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED and response.step_results[1].output["verified"] is True


def test_status_reports_drive_sandbox_without_ids_or_secrets(api_settings):
    api_settings("real")
    body = TestClient(app).get(STATUS_URL)
    status = body.json()["google_drive_sandbox"]
    assert status == {"transport": "rest", "folder_alias": "sandbox", "oauth_client_configured": True,
                      "authorized": True, "scope": DRIVE_FILE_SCOPE, "sandbox_only": True, "dry_run_ready": True,
                      "real_ready": True, "issues": []}
    assert body.json()["integrations"]["google_drive"]["transport"] == "rest"
    assert_no_secrets(body.text)


def test_status_names_missing_authorization(api_settings):
    api_settings("real", authorize=False)
    status = TestClient(app).get(STATUS_URL).json()["google_drive_sandbox"]
    assert status["authorized"] is False and status["real_ready"] is False
    assert any("google_drive_auth.py" in i for i in status["issues"])


def _connectivity(google: FakeGoogle):
    app.dependency_overrides[integrations_api.get_drive_http] = lambda: google.client()
    return TestClient(app).post(CONNECTIVITY_URL).json()


def test_connectivity_probe_passes_read_only(api_settings):
    api_settings("real")
    google = FakeGoogle()
    body = _connectivity(google)
    assert body["status"] == "PASS", body
    assert [c["name"] for c in body["checks"]] == ["OAuth token refresh (drive.file)", "read sandbox folder",
                                                   "list sandbox files"]
    assert google.writes == []


def test_connectivity_probe_fails_on_a_folder_without_the_marker(api_settings):
    api_settings("real")
    google = FakeGoogle()
    google.files[SANDBOX_ID]["appProperties"] = {}
    body = _connectivity(google)
    assert body["status"] == "FAIL" and body["checks"][-1]["status"] == "FAIL" and google.writes == []


def test_connectivity_is_not_available_without_configuration(api_settings):
    api_settings("dry_run")
    google = FakeGoogle()
    body = _connectivity(google)
    assert body["status"] == "NOT_AVAILABLE" and google.calls == []


# ======================= SSE, Langfuse, logs =======================


def test_sse_events_for_a_drive_run_with_rollback(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.failures[("GET", r"^/drive/v3/files$")] = [None, None, 404]
    runner = ExecutionRunner(world.service, EventBroker(), max_workers=1)
    try:
        channel, future = runner.start(request(*SANDBOX_RUN), write_authorized=True)
        future.result(timeout=10)
    finally:
        runner.shutdown()
    events = channel.events()
    assert [e.event_type.value for e in events] == [
        "workflow_started",
        "step_started", "step_succeeded",
        "step_started", "step_succeeded",
        "step_started", "step_failed",
        "compensation_started", "compensation_completed",
        "compensation_started", "compensation_completed",
        "workflow_failed",
    ]
    assert events[0].metadata["mode"] == "real"
    dump = json.dumps([e.model_dump(mode="json") for e in events])
    assert_no_secrets(dump)
    assert "Synthetic report" not in dump  # report content is never streamed


def test_langfuse_traces_drive_calls_without_tokens_or_content(tmp_path):
    telemetry = RecordingTelemetry()
    set_telemetry(telemetry)
    world = DriveWorld(tmp_path)
    world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    upload = telemetry.one("google_drive.files.upload")
    assert upload.as_type == "tool" and upload.merged_metadata()["http_status"] == 200
    names = {o.name for o in telemetry.named("google_drive.")}
    assert {"google_drive.files.create_folder", "google_drive.files.get", "google_drive.files.list"} <= names
    assert telemetry.named("tool.") and all(o.metadata["mode"] == "real" for o in telemetry.named("tool."))
    dump = telemetry.dump()
    assert_no_secrets(dump)
    assert "Synthetic report" not in scrub_text(dump)


def test_logs_never_contain_tokens_or_client_secret(tmp_path, caplog):
    install_secret_redaction()
    caplog.set_level(logging.DEBUG)
    world = DriveWorld(tmp_path)
    world.google.failures[("POST", r"^/drive/v3/files$")] = [503]
    world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    logging.getLogger("synthetic").warning("leak attempt %s %s %s", ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET)
    assert_no_secrets(caplog.text)


@pytest.mark.parametrize("text", [
    "Authorization: Bearer ya29.a0AfB_SYNTHETIC_unregistered_token",
    "refresh 1/" + "/0gSYNTHETIC_unregistered_refresh",
    "secret GOCSPX" + "-SYNTHETIC_unregistered_secret",
    "GET http://127.0.0.1:5555/?state=s&code=4/0SYNTHETIC",
])
def test_google_token_shapes_are_redacted_even_when_unregistered(text):
    assert "SYNTHETIC" not in str(redact(text))


def test_drive_query_literals_are_escaped():
    assert q_literal("it's a \\ test") == "'it\\'s a \\\\ test'"
    with pytest.raises(IntegrationError):
        q_literal("bad\nname")


# ======================= scripts/google_drive_sandbox.py =======================


def test_create_folder_requires_confirm(tmp_path):
    write_token(tmp_path)
    google = FakeGoogle()
    assert google_drive_sandbox.create_folder(drive_settings("real", tmp_path), confirm=False,
                                              http=google.client()) == 2
    assert google.calls == []


def test_create_folder_marks_the_sandbox(tmp_path, capsys):
    write_token(tmp_path)
    google = FakeGoogle()
    assert google_drive_sandbox.create_folder(drive_settings("real", tmp_path), confirm=True,
                                              http=google.client()) == 0
    created = [f for f in google.files.values() if f["parents"] == ["root"] and f["id"] != SANDBOX_ID]
    assert len(created) == 1 and created[0]["appProperties"] == {"workflowweaver_sandbox": "true"}
    assert ACCESS_TOKEN not in capsys.readouterr().out


def test_cleanup_trashes_only_workflowweaver_items_after_confirm(tmp_path):
    world = DriveWorld(tmp_path)
    world.service.execute(request(*SANDBOX_RUN), write_authorized=True)
    world.google.add_folder("1ManualFolder", "manual", parent=SANDBOX_ID, props={})
    settings = world.settings
    assert google_drive_sandbox.cleanup(settings, confirm=False, http=world.google.client()) == 2
    assert not any(f["trashed"] for f in world.google.files.values())
    assert google_drive_sandbox.cleanup(settings, confirm=True, http=world.google.client()) == 0
    run_folder = next(f for f in world.google.files.values() if f["appProperties"].get(RUN_PROPERTY))
    assert run_folder["trashed"] and not world.google.files["1ManualFolder"]["trashed"]
    assert not world.google.files[SANDBOX_ID]["trashed"]


def test_cleanup_refuses_a_folder_that_is_not_the_sandbox(tmp_path):
    world = DriveWorld(tmp_path)
    world.google.files[SANDBOX_ID]["appProperties"] = {}
    assert google_drive_sandbox.cleanup(world.settings, confirm=True, http=world.google.client()) == 1
    assert world.google.writes == []


def test_operator_token_constant_is_not_a_google_secret():
    assert OPERATOR_TOKEN not in GOOGLE_SECRETS


@pytest.mark.parametrize("status, reason, kind, code, sent", [
    (403, "rateLimitExceeded", ErrorKind.TRANSIENT, "GOOGLE_DRIVE_RATE_LIMITED", False),
    (403, "userRateLimitExceeded", ErrorKind.TRANSIENT, "GOOGLE_DRIVE_RATE_LIMITED", False),
    (403, "insufficientPermissions", ErrorKind.PERMISSION, "GOOGLE_DRIVE_PERMISSION_DENIED", False),
    (403, "storageQuotaExceeded", ErrorKind.PERMISSION, "GOOGLE_DRIVE_PERMISSION_DENIED", False),
    (429, "rateLimitExceeded", ErrorKind.TRANSIENT, "GOOGLE_DRIVE_RATE_LIMITED", False),
    (503, "backendError", ErrorKind.TRANSIENT, "GOOGLE_DRIVE_API_ERROR", True),
    (404, "notFound", ErrorKind.PERMANENT, "GOOGLE_DRIVE_NOT_FOUND", False),
    (400, "badRequest", ErrorKind.PERMANENT, "GOOGLE_DRIVE_API_ERROR", False),
])
def test_drive_error_mapping_uses_status_and_reason_only(status, reason, kind, code, sent):
    from app.integrations.google.drive import drive_error

    response = httpx.Response(status, json={"error": {"message": "SYNTHETIC-PROVIDER-DETAIL",
                                                      "errors": [{"reason": reason}]}})
    error = drive_error(response, write=True)
    assert (error.kind, error.code, error.request_sent) == (kind, code, sent)
    assert reason in error.message and "SYNTHETIC-PROVIDER-DETAIL" not in error.message
