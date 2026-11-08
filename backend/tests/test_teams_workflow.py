"""Phase 7.1: Microsoft Teams Only workflow over a Teams Workflows webhook.

No real Teams message is ever sent: the webhook is either RecordingWebhookSender or the real
WebhookClient over httpx.MockTransport (so the real error classification runs without a socket),
and the conftest blocks every non-loopback connection. Planners are Gemini/Groq fakes.

Covered: status/readiness per mode, planning (target restriction, channel allowlist, normalizer,
real-mode rules, prompt), strict validation (actions, content, destinations), mock delivery,
dry-run preview, real-mode gates (approval, operator identity, token, kill switch, sandbox),
the documented payload contract, bounded retries, ambiguous outcomes, idempotency, no rollback
claims, SSE events, Langfuse spans and secret redaction in responses, events, traces and logs.
"""

import json
import logging
import random
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools.integrations import build_integration_registry
from app.agents.plan_registry import get_plan_registry
from app.agents.planners import FallbackPlanner, GeminiPlanner, GroqPlanner
from app.agents.planners.claude import build_system_prompt, teams_example
from app.agents.planners.mock import MockPlanner
from app.agents.service import PlanningService
from app.agents.validation import normalize_teams_references, teams_destination_issues, validate_draft
from app.api import execution as execution_api
from app.api import integrations as integrations_api
from app.api import planning as planning_api
from app.core.config import Settings
from app.integrations.config import load_integration_config
from app.integrations.errors import IntegrationConfigError
from app.integrations.http.webhook import WebhookClient
from app.main import app
from app.observability import set_telemetry
from app.schemas.execution import CompensationStatus, ExecutionRequest, ExecutionStatus
from app.schemas.plan import DraftPlan, DraftStep, StepArgument
from app.streaming.broker import EventBroker
from app.streaming.runner import ExecutionRunner
from tests.execution_helpers import make_service
from tests.integration_fakes import OPERATOR_TOKEN, SECRETS, WEBHOOK_SIG, WEBHOOK_URL, RecordingWebhookSender, \
    integration_settings
from tests.telemetry_helpers import RecordingTelemetry
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, ok_response
from tests.test_planner_fallback import GROQ_MODEL, FakeGroqClient, gemini_503, groq_ok

OTHER_SIG = "SYNTHETIC-OTHER-SIG-0099"
OTHER_URL = ("https://synthetic-tenant.westus.logic.azure.com:443/workflows/other/triggers/manual/paths/"
             f"invoke?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig={OTHER_SIG}")
ALL_SECRETS = [*SECRETS, OTHER_URL, OTHER_SIG]
MESSAGE = "Release 2.4 is live.\n- New reward balance page\n- Faster redemption"
PLAN_URL = "/api/v1/workflows/plan"
EXEC_URL = "/api/v1/workflows/execute"


def assert_no_secrets(text: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text, secret
    assert "logic.azure.com" not in text and "sig=" not in text


def teams_settings(mode: str = "real", **overrides) -> Settings:
    values = dict(
        teams_webhook_urls=json.dumps({"sandbox": WEBHOOK_URL, "other": OTHER_URL}),
        teams_default_channel="sandbox",
        smoke_test_teams_channel="sandbox",
        teams_real_writes_sandbox_only=True,
    )
    values.update(overrides)
    return integration_settings(mode, **values)


class TeamsWorld:
    """Dry-run/real registry with the Teams adapter wired to ``sender`` (a fake)."""

    def __init__(self, mode: str = "real", sender=None, **overrides) -> None:
        self.settings = teams_settings(mode, **overrides)
        self.sender = sender if sender is not None else RecordingWebhookSender()
        self.sleeps: list[float] = []
        self.registry = build_integration_registry(self.settings, webhook_sender=self.sender)
        self.service = ExecutionService(
            self.registry,
            RetryPolicy(max_attempts=3, base_delay_seconds=0.2, max_delay_seconds=2.0, jitter=False),
            max_steps=10, sleeper=self.sleeps.append, rng=random.Random(7))


class MockWebhook:
    """httpx.MockTransport handler: returns queued status codes / raises queued exceptions."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=request)


def webhook_client(handler: MockWebhook) -> WebhookClient:
    return WebhookClient(connect_timeout=1, timeout=1, http_transport=httpx.MockTransport(handler),
                         sleeper=lambda _s: None)


def teams_step(n=1, message=MESSAGE, depends_on=(), **extra) -> dict:
    return {"step_number": n, "integration": "teams", "action": "send_message", "description": f"Post {n}",
            "arguments": {"message": message, "channel": "sandbox", **extra}, "depends_on": list(depends_on)}


def execution_request(steps, *, mode="real", approved=True, approved_by="synthetic.reviewer", target="teams",
                      workflow_id=None) -> ExecutionRequest:
    return ExecutionRequest.model_validate({
        "workflow_id": str(workflow_id or uuid4()),
        "plan": {"summary": "Teams plan", "integration_target": target, "steps": steps},
        "approval": {"approved": approved, "mode": mode, "approved_by": approved_by, "integration_target": target}})


def draft(*steps: DraftStep) -> DraftPlan:
    return DraftPlan(summary="Teams plan", steps=list(steps))


def dstep(n=1, integration="teams", action="send_message", depends_on=(), **arguments) -> DraftStep:
    return DraftStep(step_number=n, integration=integration, action=action, description=f"Step {n}",
                     arguments=[StepArgument(name=k, value=v) for k, v in arguments.items()],
                     depends_on=list(depends_on))


GOOD = draft(dstep(message=MESSAGE, channel="sandbox"))


@pytest.fixture(autouse=True)
def _clean():
    get_plan_registry().clear()
    yield
    get_plan_registry().clear()
    app.dependency_overrides.clear()


# ======================= status / readiness =======================


def status_with(monkeypatch, settings: Settings) -> dict:
    monkeypatch.setattr(integrations_api, "get_settings", lambda: settings)
    response = TestClient(app).get("/api/v1/integrations/status")
    assert response.status_code == 200
    assert_no_secrets(response.text)
    return response.json()


def test_mock_mode_offers_teams_with_simulated_delivery(monkeypatch):
    teams = status_with(monkeypatch, Settings(_env_file=None))["teams_sandbox"]
    assert teams["transport"] == "mock" and teams["mock_ready"] is True
    assert teams["dry_run_ready"] is False and teams["real_ready"] is False


def test_dry_run_status_reports_the_alias_never_the_url(monkeypatch):
    teams = status_with(monkeypatch, teams_settings("dry_run"))["teams_sandbox"]
    assert teams == {"transport": "webhook", "channel_alias": "sandbox", "sandbox_only": True, "mock_ready": True,
                     "dry_run_ready": True, "real_ready": False, "issues": []}


def test_real_status_is_ready_only_with_every_gate_configured(monkeypatch):
    assert status_with(monkeypatch, teams_settings("real"))["teams_sandbox"]["real_ready"] is True


@pytest.mark.parametrize(("overrides", "setting"), [
    ({"teams_webhook_urls": None, "teams_default_channel": None}, "TEAMS_WEBHOOK_URLS"),
    ({"smoke_test_teams_channel": None}, "SMOKE_TEST_TEAMS_CHANNEL"),
    ({"smoke_test_teams_channel": "unlisted"}, "SMOKE_TEST_TEAMS_CHANNEL"),
    ({"real_writes_enabled": False}, "REAL_WRITES_ENABLED"),
    ({"real_write_authorization_token": None}, "REAL_WRITE_AUTHORIZATION_TOKEN"),
    ({"teams_real_writes_sandbox_only": False}, "TEAMS_REAL_WRITES_SANDBOX_ONLY"),
])
def test_missing_configuration_blocks_real_mode_and_names_the_setting(monkeypatch, overrides, setting):
    teams = status_with(monkeypatch, teams_settings("real", **overrides))["teams_sandbox"]
    assert teams["real_ready"] is False
    assert any(setting in issue for issue in teams["issues"])


def test_retired_office365_connector_urls_are_refused():
    with pytest.raises(IntegrationConfigError) as info:
        load_integration_config(teams_settings(
            "real", teams_webhook_urls=json.dumps({"sandbox": "https://contoso.webhook.office.com/webhookb2/x"})))
    assert "Teams Workflows webhook" in str(info.value) and "office.com" not in str(info.value)


# ======================= strict validation =======================


def issue_codes(plan: DraftPlan) -> list[str]:
    _, issues = validate_draft(plan, max_steps=10)
    return [i.code for i in issues]


def test_teams_example_in_the_prompt_is_valid():
    assert issue_codes(DraftPlan.model_validate(json.loads(teams_example("sandbox")))) == []


@pytest.mark.parametrize(("message", "code"), [
    ("x" * 4001, "MESSAGE_TOO_LONG"),
    ("bell \x07 character", "INVALID_MESSAGE_CONTENT"),
    ("Hello <at>Alex</at>", "INVALID_MESSAGE_CONTENT"),
    ("<b>bold</b> news", "INVALID_MESSAGE_CONTENT"),
    ("", "MISSING_ARGUMENT"),
])
def test_invalid_message_content_is_rejected(message, code):
    assert issue_codes(draft(dstep(message=message, channel="sandbox"))) == [code]


def test_plain_text_and_markdown_up_to_the_limit_are_accepted():
    assert issue_codes(draft(dstep(message="**Release** is live\n- item " + "x" * 3900, channel="sandbox"))) == []


@pytest.mark.parametrize("channel", [WEBHOOK_URL, "https://example.invalid/hook", "General Channel", "a" * 41])
def test_channel_must_be_an_alias_and_is_never_echoed(channel):
    _, issues = validate_draft(draft(dstep(message="hi", channel=channel)), max_steps=10)
    assert [i.code for i in issues] == ["INVALID_DESTINATION"]
    assert_no_secrets(json.dumps([i.model_dump() for i in issues]))


@pytest.mark.parametrize(("action", "code"), [("delete_message", "UNSUPPORTED_ACTION"),
                                              ("read_messages", "UNSUPPORTED_ACTION"),
                                              ("edit_message", "UNSUPPORTED_ACTION")])
def test_unsupported_teams_actions_are_rejected(action, code):
    assert issue_codes(draft(dstep(action=action, message="hi"))) == [code]


def test_unknown_teams_arguments_are_rejected():
    assert issue_codes(draft(dstep(message="hi", channel="sandbox", webhook_url="x"))) == ["UNKNOWN_ARGUMENT"]


def test_destination_allowlist_and_normalizer():
    steps = validate_draft(draft(dstep(message="a", channel="other"), dstep(2, message="b")), max_steps=10)[0].steps
    assert [(i.code, i.step_number) for i in teams_destination_issues(steps, ["sandbox"])] == [
        ("DESTINATION_NOT_APPROVED", 1), ("DESTINATION_REQUIRED", 2)]
    normalized, notes = normalize_teams_references(draft(dstep(message="a")), ["sandbox"])
    assert normalized.steps[0].arguments[-1] == StepArgument(name="channel", value="sandbox")
    assert notes == ["Step 1 (teams.send_message): added channel=sandbox, the only approved Teams channel."]
    # never fills when more than one alias is approved, never changes a given channel
    assert normalize_teams_references(draft(dstep(message="a")), ["sandbox", "other"])[1] == []
    assert normalize_teams_references(draft(dstep(message="a", channel="other")), ["sandbox"])[1] == []


# ======================= planning API =======================


@pytest.fixture
def plan_api(monkeypatch):
    def install(mode="real", gemini=(None,), groq=(None,), planner=None, **overrides):
        settings = teams_settings(mode, **overrides)
        monkeypatch.setattr(planning_api, "get_settings", lambda: settings)
        g = FakeGenaiClient(*[o if o is not None else ok_response(GOOD) for o in gemini])
        q = FakeGroqClient(*[o if o is not None else groq_ok(GOOD) for o in groq])
        chain = planner or FallbackPlanner([GeminiPlanner(client=g, model=GEMINI_MODEL, max_output_tokens=4096),
                                            GroqPlanner(client=q, model=GROQ_MODEL, max_tokens=4096)])
        service = PlanningService(chain, max_steps=10, max_planning_attempts=2, available_documents=None)
        app.dependency_overrides[planning_api.get_planning_service] = lambda: service
        return g, q
    return install


def plan(mode="real", target="teams"):
    return TestClient(app).post(PLAN_URL, json={"instruction": "Announce the release in Teams.", "mode": mode,
                                                "target": target})


def test_real_teams_plan_from_gemini_records_the_approved_channel(plan_api):
    gemini, groq = plan_api()
    response = plan()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["planner"] == "gemini" and body["integration_target"] == "teams" and body["executed"] is False
    assert body["planned_steps"][0]["arguments"] == {"message": MESSAGE, "channel": "sandbox"}
    assert get_plan_registry().teams_channels(body["workflow_id"]) == ["sandbox"]
    prompt = gemini.models.calls[0]["config"].system_instruction
    assert "may use ONLY teams" in prompt and "approved channel alias (sandbox)" in prompt
    assert "Microsoft Teams rules" in prompt and "Notion rules (a step" not in prompt
    assert_no_secrets(prompt + response.text)


def test_groq_fallback_plan_gets_the_same_rules(plan_api):
    _, groq = plan_api(gemini=(gemini_503(),), groq=(groq_ok(draft(dstep(message=MESSAGE))),))
    body = plan().json()
    assert body["status"] == "PLANNED" and body["planner"] == "groq" and body["fallback_used"] is True
    assert body["planned_steps"][0]["arguments"]["channel"] == "sandbox"
    assert body["plan_adjustments"] == [
        "Step 1 (teams.send_message): added channel=sandbox, the only approved Teams channel."]
    assert "approved channel alias (sandbox)" in groq.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(("bad", "code"), [
    (draft(dstep(message="hi", channel="other")), "DESTINATION_NOT_APPROVED"),
    (draft(dstep(message="hi", channel=WEBHOOK_URL)), "INVALID_DESTINATION"),
    (draft(dstep(action="delete_message", message="hi")), "UNSUPPORTED_ACTION"),
    (draft(dstep(message="hi", channel="sandbox"),
           dstep(2, "notion", "create_page", title="t", content="c")), "INTEGRATION_NOT_ALLOWED"),
])
def test_invalid_teams_plans_are_rejected(plan_api, bad, code):
    plan_api(gemini=(ok_response(bad),))
    response = plan()
    assert response.status_code == 422, response.text
    assert code in [i["code"] for i in response.json()["validation"]["issues"]]
    assert_no_secrets(response.text)


def test_real_mode_refuses_the_mock_planner(plan_api):
    plan_api(planner=MockPlanner())
    response = plan()
    assert response.status_code == 503 and "AI planner" in response.json()["detail"]


def test_real_mode_without_configuration_is_503_naming_the_setting(plan_api):
    plan_api(teams_webhook_urls=None, teams_default_channel=None)
    response = plan()
    assert response.status_code == 503 and "TEAMS_WEBHOOK_URLS" in response.json()["detail"]


def test_dry_run_plan_is_restricted_to_the_sandbox_channel(plan_api):
    plan_api(mode="dry_run", gemini=(ok_response(draft(dstep(message="hi", channel="other"))),))
    response = plan(mode="dry_run")
    assert response.status_code == 422
    assert [i["code"] for i in response.json()["validation"]["issues"]] == ["DESTINATION_NOT_APPROVED"]


def test_mock_mode_plans_without_any_webhook(plan_api):
    plan_api(mode="mock", teams_webhook_urls=None, teams_default_channel=None, smoke_test_teams_channel=None,
             gemini=(ok_response(draft(dstep(message="hi"))),))
    response = plan(mode="mock")
    assert response.status_code == 200 and response.json()["integration_target"] == "teams"


# ======================= mock and dry run =======================


def test_mock_mode_simulates_delivery_without_network():
    service, _ = make_service()
    response = service.execute(execution_request([teams_step()], mode="mock"))
    assert response.status is ExecutionStatus.COMPLETED
    assert service.registry.store.messages[0]["text"] == MESSAGE
    assert response.step_results[0].resource_id.startswith("mock-teams-msg")


def test_dry_run_builds_and_previews_the_request_without_sending():
    world = TeamsWorld("dry_run")
    response = world.service.execute(execution_request([teams_step()], mode="dry_run"))
    assert response.status is ExecutionStatus.COMPLETED
    out = response.step_results[0].output
    assert out["sent"] is False and out["delivered"] is False and out["would_call"] == "teams_webhook"
    assert out["channel"] == "sandbox" and out["payload_format"] == "adaptive_card_1.2"
    assert out["payload_bytes"] > 0 and out["message_chars"] == len(MESSAGE)
    assert world.sender.posts == []
    assert_no_secrets(response.model_dump_json())


def test_dry_run_rejects_an_unapproved_channel_before_anything_runs():
    world = TeamsWorld("dry_run")
    response = world.service.execute(execution_request([teams_step(channel="other")], mode="dry_run"))
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]


# ======================= real mode: gates =======================


def test_real_send_posts_the_documented_workflows_payload_once():
    world = TeamsWorld()
    response = world.service.execute(execution_request([teams_step()]), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED
    [(url, payload)] = world.sender.posts
    assert url == WEBHOOK_URL  # resolved server-side from the alias
    assert payload["type"] == "message" and len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive" and attachment["contentUrl"] is None
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.2"
    assert card["body"] == [{"type": "TextBlock", "text": MESSAGE, "wrap": True}]
    assert "@type" not in payload and "themeColor" not in payload  # no legacy MessageCard fields
    out = response.step_results[0].output
    assert out["accepted"] is True and out["delivery"] == "accepted_by_webhook" and "delivered" not in out
    assert out["channel"] == "sandbox" and out["http_status"] == 202
    assert_no_secrets(response.model_dump_json())


def test_real_send_requires_the_operator_token():
    world = TeamsWorld()
    response = world.service.execute(execution_request([teams_step()]), write_authorized=False)
    assert [e.code for e in response.errors] == ["REAL_WRITE_AUTHORIZATION_REQUIRED"] and world.sender.posts == []


def test_real_send_is_blocked_by_the_kill_switch():
    world = TeamsWorld(real_writes_enabled=False)
    response = world.service.execute(execution_request([teams_step()]), write_authorized=True)
    assert [e.code for e in response.errors] == ["REAL_WRITES_DISABLED"] and world.sender.posts == []


def test_real_send_requires_explicit_approval():
    world = TeamsWorld()
    response = world.service.execute(execution_request([teams_step()], approved=False), write_authorized=True)
    assert [e.code for e in response.errors] == ["APPROVAL_REQUIRED"] and world.sender.posts == []


@pytest.mark.parametrize("approved_by", ["", "ab"])
def test_real_send_requires_an_operator_identity(approved_by):
    with pytest.raises(ValueError):
        execution_request([teams_step()], approved_by=approved_by)


def test_sandbox_only_refuses_other_approved_channels():
    world = TeamsWorld()
    response = world.service.execute(execution_request([teams_step(channel="other")]), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]
    assert world.sender.posts == []


def test_executor_rechecks_the_channel_recorded_at_planning_time():
    world = TeamsWorld(teams_real_writes_sandbox_only=False)  # adapter would allow "other"
    workflow_id = uuid4()
    get_plan_registry().record(workflow_id, "teams", None, ["sandbox"])
    response = world.service.execute(execution_request([teams_step(channel="other")], workflow_id=workflow_id),
                                     write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED and world.sender.posts == []
    assert [i.code for i in response.validation.issues] == ["DESTINATION_NOT_APPROVED"]


def test_executor_rejects_a_teams_plan_with_other_integrations():
    world = TeamsWorld()
    notion = {"step_number": 2, "integration": "notion", "action": "create_page",
              "arguments": {"title": "t", "content": "c"}, "depends_on": [1]}
    response = world.service.execute(execution_request([teams_step(), notion]), write_authorized=True)
    assert response.status is ExecutionStatus.REJECTED and world.sender.posts == []


def test_api_real_send_without_header_is_403_and_with_token_is_200(monkeypatch):
    world = TeamsWorld()
    monkeypatch.setattr(execution_api, "get_settings", lambda: world.settings)
    app.dependency_overrides[execution_api.get_execution_service] = lambda: world.service
    client = TestClient(app)
    body = json.loads(execution_request([teams_step()]).model_dump_json())
    denied = client.post(EXEC_URL, json=body)
    assert denied.status_code == 403 and world.sender.posts == []
    body["workflow_id"] = str(uuid4())
    allowed = client.post(EXEC_URL, json=body, headers={"X-Integration-Authorization": OPERATOR_TOKEN})
    assert allowed.status_code == 200 and len(world.sender.posts) == 1
    assert_no_secrets(denied.text + allowed.text)


# ======================= retries, ambiguous outcomes, idempotency, no rollback =======================


def test_connect_error_is_retried_because_nothing_was_sent():
    handler = MockWebhook(httpx.ConnectError("synthetic"), 202)
    world = TeamsWorld(sender=webhook_client(handler))
    response = world.service.execute(execution_request([teams_step()]), write_authorized=True)
    assert response.status is ExecutionStatus.COMPLETED and response.attempts == {"1": 2}
    assert len(handler.requests) == 2 and world.sleeps == [0.2]


def test_http_429_is_retried_with_backoff_and_bounded():
    handler = MockWebhook(429)
    world = TeamsWorld(sender=webhook_client(handler))
    response = world.service.execute(execution_request([teams_step()]), write_authorized=True)
    assert response.status is ExecutionStatus.FAILED and response.attempts == {"1": 3}
    assert len(handler.requests) == 3 and world.sleeps == [0.2, 0.4]
    assert [e.code for e in response.errors][-1] == "RETRIES_EXHAUSTED"


@pytest.mark.parametrize("outcome", [503, httpx.ReadTimeout("synthetic")])
def test_ambiguous_failure_is_not_retried_to_avoid_a_duplicate_post(outcome):
    handler = MockWebhook(outcome, 202)
    world = TeamsWorld(sender=webhook_client(handler))
    response = world.service.execute(execution_request([teams_step()]), write_authorized=True)
    assert response.status is ExecutionStatus.FAILED and len(handler.requests) == 1
    assert response.errors[0].code == "OUTCOME_UNKNOWN"


@pytest.mark.parametrize("status_code", [401, 404, 400])
def test_permanent_webhook_errors_are_not_retried(status_code):
    handler = MockWebhook(status_code)
    world = TeamsWorld(sender=webhook_client(handler))
    response = world.service.execute(execution_request([teams_step()]), write_authorized=True)
    assert response.status is ExecutionStatus.FAILED and len(handler.requests) == 1


def test_duplicate_request_replays_without_a_second_post():
    world = TeamsWorld()
    request = execution_request([teams_step()])
    first = world.service.execute(request, write_authorized=True)
    second = world.service.execute(request, write_authorized=True)
    assert first.status is second.status is ExecutionStatus.COMPLETED
    assert second.replayed is True and len(world.sender.posts) == 1


def test_a_sent_message_is_never_claimed_to_be_rolled_back():
    world = TeamsWorld()
    world.sender.failures = [None]  # placeholder replaced below
    world.sender.failures = []
    handler_steps = [teams_step(1, "first"), teams_step(2, "second", depends_on=[1])]
    sender = world.sender
    original = sender.post_json

    def second_fails(url, payload):
        if len(sender.posts) == 1:
            sender.posts.append((url, payload))
            from app.agents.execution.tools.base import ErrorKind
            from app.integrations.errors import IntegrationError
            raise IntegrationError(ErrorKind.PERMISSION, "WEBHOOK_UNAUTHORIZED", "synthetic", request_sent=True)
        return original(url, payload)

    sender.post_json = second_fails
    response = world.service.execute(execution_request(handler_steps), write_authorized=True)
    assert response.status is not ExecutionStatus.COMPLETED
    [comp] = response.compensation_results
    assert comp.step_number == 1 and comp.status is CompensationStatus.NOT_COMPENSATABLE
    assert "cannot be undone" in comp.message
    assert len(sender.posts) == 2  # no extra "delete" call exists or was attempted


# ======================= SSE, Langfuse, logs =======================


def test_sse_events_cover_start_retry_and_success_without_secrets():
    handler = MockWebhook(httpx.ConnectError("synthetic"), 202)
    world = TeamsWorld(sender=webhook_client(handler))
    runner = ExecutionRunner(world.service, EventBroker(), max_workers=1)
    try:
        channel, future = runner.start(execution_request([teams_step()]), write_authorized=True)
        future.result(timeout=10)
    finally:
        runner.shutdown()
    events = channel.events()
    assert [e.event_type.value for e in events] == [
        "workflow_started", "step_started", "step_failed", "retry_scheduled", "step_started", "step_succeeded",
        "workflow_completed"]
    failed, succeeded = events[2], events[5]
    assert failed.metadata["integration"] == "teams" and failed.metadata["will_retry"] is True
    assert failed.metadata["error_code"] == "WEBHOOK_UNREACHABLE"
    assert succeeded.metadata["integration"] == "teams" and succeeded.metadata["action"] == "send_message"
    assert succeeded.metadata["effect"] == "irreversible"
    dump = json.dumps([e.model_dump(mode="json") for e in events])
    assert_no_secrets(dump) and MESSAGE not in dump


def test_sse_failure_event_for_a_permanent_error():
    world = TeamsWorld(sender=webhook_client(MockWebhook(401)))
    runner = ExecutionRunner(world.service, EventBroker(), max_workers=1)
    try:
        channel, future = runner.start(execution_request([teams_step()]), write_authorized=True)
        future.result(timeout=10)
    finally:
        runner.shutdown()
    events = channel.events()
    failed = next(e for e in events if e.event_type.value == "step_failed")
    assert failed.metadata["error_code"] == "WEBHOOK_UNAUTHORIZED" and failed.metadata["will_retry"] is False
    assert events[-1].event_type.value == "workflow_failed"
    assert_no_secrets(json.dumps([e.model_dump(mode="json") for e in events]))


def test_langfuse_span_has_safe_metadata_and_latency():
    telemetry = RecordingTelemetry()
    set_telemetry(telemetry)
    world = TeamsWorld()
    world.service.execute(execution_request([teams_step(message="SYNTHETIC-MESSAGE-MARKER")]), write_authorized=True)
    span = telemetry.one("teams.webhook.post")
    assert span.input == {"destination_alias": "sandbox", "payload_bytes": span.input["payload_bytes"],
                          "message_chars": len("SYNTHETIC-MESSAGE-MARKER"), "attempt": 1}
    assert span.output["http_status"] == 202 and span.output["accepted"] is True
    assert isinstance(span.output["latency_ms"], float)
    dump = telemetry.dump()
    assert_no_secrets(dump) and "SYNTHETIC-MESSAGE-MARKER" not in dump


def test_langfuse_span_records_failures_without_secrets():
    telemetry = RecordingTelemetry()
    set_telemetry(telemetry)
    world = TeamsWorld(sender=webhook_client(MockWebhook(401)))
    world.service.execute(execution_request([teams_step()]), write_authorized=True)
    span = telemetry.one("teams.webhook.post")
    assert span.output["error_code"] == "WEBHOOK_UNAUTHORIZED" and span.output["request_sent"] is True
    assert_no_secrets(telemetry.dump())


def test_logs_never_contain_the_webhook_url(caplog):
    caplog.set_level(logging.DEBUG)
    world = TeamsWorld(sender=webhook_client(MockWebhook(httpx.ConnectError("synthetic"), 202)))
    world.service.execute(execution_request([teams_step()]), write_authorized=True)
    assert_no_secrets(caplog.text)


def test_prompt_never_contains_webhook_urls():
    prompt = build_system_prompt(10, allowed_integrations={"teams"}, teams_channels={"sandbox"})
    assert_no_secrets(prompt)
    assert "unsent" in prompt and "delete" not in build_system_prompt(10).lower()
