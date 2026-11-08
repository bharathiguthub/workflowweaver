"""Phase 7.2 regression: a source whose phases are one-sentence prose descriptions.

Customer_Rewards_Plan.md (repo root) has no "## Summary" section and no Objectives / Technical
tasks / Deliverables / Acceptance criteria headings; each phase is one sentence. It was exported
with Markdown escapes ("\\## Phase 1") and CRLF line endings. Before the fix:
  - the AI returned an empty project summary (the prompt asked to restate the document's own
    summary, which does not exist) -> MISSING_VALUE (step 2): summary is empty;
  - the schema had no field for a phase's own description, so every phase showed "Not stated";
  - the INVALID plan therefore had 0 Notion writes and 0 Teams messages.

Groq and Gemini are fake SDK clients and Google Drive / Notion / Teams are in-memory fakes; the
conftest blocks every non-loopback connection. Nothing real is read or written.
"""

import pytest
from fastapi.testclient import TestClient

from app.agents.orchestration import (
    ExtractedPhase,
    GeminiExtractor,
    GroqExtractor,
    MockExtractor,
    ProjectExtraction,
    _resolve_summary,
    _tokens,
    normalize_source,
    review_extraction,
    source_outline,
)
from app.agents.plan_registry import get_plan_registry
from app.agents.source_documents import CUSTOMER_REWARDS_PLAN, sha256_text
from app.api import orchestration as orchestration_api
from app.main import app
from app.schemas.execution import ExecutionStatus
from tests.test_gemini_planner import MODEL as GEMINI_MODEL
from tests.test_gemini_planner import FakeGenaiClient, ok_response
from tests.test_orchestration import REAL_SOURCE, World, execution_request
from tests.test_planner_fallback import GROQ_MODEL, FakeGroqClient, groq_ok, groq_status_error

PLAN_URL = "/api/v1/workflows/orchestration/plan"

# Byte-for-byte the repository's Customer_Rewards_Plan.md (CRLF, escaped headings).
PROSE_PLAN = "\r\n".join([
    "\\# Customer Rewards Platform", "", "", "",
    "Architecture: Java 21, Spring Boot, AWS Lambda, DynamoDB", "", "", "",
    "\\## Phase 1: Customer Registration", "", "Create registration APIs and initialize customer reward accounts.",
    "", "", "",
    "\\## Phase 2: Reward Balance Management", "", "Implement balance queries, credits, and idempotent updates.",
    "", "", "",
    "\\## Phase 3: Points Redemption", "", "Implement redemption APIs, balance validation, and rollback handling.",
    "", "", "",
    "\\## Phase 4: Transaction History", "", "Create a transaction ledger and paginated history APIs.", "", "", "",
    "\\## Phase 5: AWS Deployment", "", "Deploy using AWS Lambda, API Gateway, DynamoDB, and CloudWatch.", "",
])
TITLES = ["Customer Registration", "Reward Balance Management", "Points Redemption", "Transaction History",
          "AWS Deployment"]
DESCRIPTIONS = [
    "Create registration APIs and initialize customer reward accounts.",
    "Implement balance queries, credits, and idempotent updates.",
    "Implement redemption APIs, balance validation, and rollback handling.",
    "Create a transaction ledger and paginated history APIs.",
    "Deploy using AWS Lambda, API Gateway, DynamoDB, and CloudWatch.",
]


def phase(title: str, *, summary: str = "", tasks: list[str] | None = None) -> ExtractedPhase:
    return ExtractedPhase(title=title, summary=summary, objectives=[], technical_tasks=tasks or [], deliverables=[],
                          acceptance_criteria=[], priority="", owner="")


# What Groq returned in the reported failure: correct titles, nothing else.
OBSERVED = ProjectExtraction(project_title="Customer Rewards Platform", summary="",
                             phases=[phase(t) for t in TITLES])
# What the updated prompt asks for: source-grounded summaries and the prose's own actions as tasks.
GOOD = ProjectExtraction(
    project_title="Customer Rewards Platform",
    summary="The Customer Rewards Platform uses Java 21, Spring Boot, AWS Lambda and DynamoDB, delivered in five "
            "phases: customer registration, reward balance management, points redemption, transaction history and "
            "AWS deployment.",
    phases=[
        phase(TITLES[0], summary=DESCRIPTIONS[0], tasks=["Create registration APIs", "Initialize customer reward accounts"]),
        phase(TITLES[1], summary=DESCRIPTIONS[1], tasks=["Implement balance queries", "Implement credits",
                                                         "Implement idempotent updates"]),
        phase(TITLES[2], summary=DESCRIPTIONS[2], tasks=["Implement redemption APIs", "Implement balance validation",
                                                         "Implement rollback handling"]),
        phase(TITLES[3], summary=DESCRIPTIONS[3], tasks=["Create a transaction ledger", "Create paginated history APIs"]),
        phase(TITLES[4], summary=DESCRIPTIONS[4], tasks=["Deploy using AWS Lambda, API Gateway, DynamoDB and CloudWatch"]),
    ],
)


@pytest.fixture(autouse=True)
def _clean():
    get_plan_registry().clear()
    yield
    get_plan_registry().clear()
    app.dependency_overrides.clear()


def prose_world(monkeypatch, tmp_path, mode="real", *, groq=None, gemini=None) -> World:
    """Real-mode world (fake Drive / Notion / Teams) with the prose document as the source and the
    default provider order: Groq primary, Gemini fallback."""
    world = World(monkeypatch, tmp_path, mode)
    world.google.files[REAL_SOURCE]["content"] = PROSE_PLAN.encode("utf-8")
    world.groq = FakeGroqClient(*(groq or (groq_ok(OBSERVED),)))
    world.gemini = FakeGenaiClient(*(gemini or (ok_response(GOOD),)))
    sources = [GroqExtractor(client=world.groq, model=GROQ_MODEL, max_tokens=8192),
               GeminiExtractor(client=world.gemini, model=GEMINI_MODEL, max_output_tokens=8192)]
    app.dependency_overrides[orchestration_api.get_extraction_sources_factory] = lambda: (lambda _s, _m: sources)
    return world


def plan(mode="real"):
    return TestClient(app).post(PLAN_URL, json={"source_file_id": REAL_SOURCE, "mode": mode, "expected_phases": 5})


def steps_of(body):
    return [(s["integration"], s["action"]) for s in body["planned_steps"]]


EXPECTED_STEPS = [("google_drive", "read_document"), ("notion", "create_page"), *[("notion", "create_page")] * 5,
                  ("teams", "send_message")]


# ======================= the reported failure, end to end =======================


def test_observed_groq_output_now_plans_with_source_grounded_summaries(monkeypatch, tmp_path):
    world = prose_world(monkeypatch, tmp_path)
    response = plan()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PLANNED" and body["planner"] == "groq" and body["fallback_used"] is False
    assert world.gemini.models.calls == []
    assert steps_of(body) == EXPECTED_STEPS  # read, project page, 5 phase pages, Teams last
    assert body["write_summary"]["notion_pages"] == 6 and body["write_summary"]["teams_messages"] == 1

    extraction = body["extraction"]
    # Project summary: the document's own statements only (architecture line + its phase names).
    assert extraction["summary_source"] == "source_text"
    assert extraction["summary"] == (
        "Architecture: Java 21, Spring Boot, AWS Lambda, DynamoDB. The document defines 5 implementation phases: "
        "Customer Registration, Reward Balance Management, Points Redemption, Transaction History and AWS Deployment.")
    assert [p["title"] for p in extraction["phases"]] == TITLES
    assert [p["summary"] for p in extraction["phases"]] == DESCRIPTIONS
    assert {p["summary_source"] for p in extraction["phases"]} == {"source_text"}
    # Not invented: sections the source does not state stay empty; no priority or owner.
    assert all(not any(p["sections"].values()) for p in extraction["phases"])
    assert {(p["priority_source"], p["owner_source"]) for p in extraction["phases"]} == {("none", "none")}
    assert any("taken from the source document" in w for w in body["warnings"])

    pages = [s["arguments"] for s in body["planned_steps"] if s["integration"] == "notion"]
    assert pages[0]["content"].startswith(extraction["summary"])
    for page, description in zip(pages[1:], DESCRIPTIONS):
        assert page["content"].startswith(f"Summary: {description} [copied from the source document]")
        assert "- Not stated in the source document." in page["content"]
        assert "- Priority: not stated" in page["content"] and "- Owner: not stated" in page["content"]
    message = body["planned_steps"][-1]["arguments"]["message"]
    summary_line = next(line for line in message.split("\n") if line.startswith("Summary: "))
    assert "Architecture: Java 21" in summary_line and "5. AWS Deployment" in summary_line


def test_approved_plan_executes_read_six_verified_pages_then_one_teams_message(monkeypatch, tmp_path):
    world = prose_world(monkeypatch, tmp_path)
    body = plan().json()
    # The plan is bound to the ORIGINAL bytes (CRLF + escapes), not to the normalised text.
    read = body["planned_steps"][0]["arguments"]
    assert read["expected_sha256"] == sha256_text(PROSE_PLAN) and read["file_id"] == REAL_SOURCE
    assert world.drive_writes() == [] and world.notion.tool_calls == [] and world.teams.posts == []

    result = world.service.execute(execution_request(body), write_authorized=True)
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    assert result.step_results[0].output["source_verified"] is True
    assert len(world.notion_calls("API-post-page")) == 6
    assert [r.output["verification"] for r in result.step_results[1:7]] == ["verified"] * 6
    assert len(world.teams.posts) == 1 and world.notion.tool_calls[-1][0] == "API-retrieve-a-page"
    assert world.drive_writes() == []  # the source is only ever read


def test_unapproved_or_tampered_plans_still_cannot_run(monkeypatch, tmp_path):
    world = prose_world(monkeypatch, tmp_path)
    body = plan().json()
    rejected = world.service.execute(execution_request(body, approved=False), write_authorized=True)
    assert [e.code for e in rejected.errors] == ["APPROVAL_REQUIRED"]
    steps = [{k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
             for s in body["planned_steps"]]
    steps[2]["arguments"]["content"] = steps[2]["arguments"]["content"].replace("Create registration", "Delete all")
    tampered = world.service.execute(execution_request(body, steps=steps), write_authorized=True)
    assert [e.code for e in tampered.errors] == ["APPROVAL_PLAN_MISMATCH"]
    assert world.notion.tool_calls == [] and world.teams.posts == []


# ======================= Groq primary / Gemini fallback =======================


def test_groq_grounded_extraction_maps_prose_actions_to_technical_tasks(monkeypatch, tmp_path):
    world = prose_world(monkeypatch, tmp_path, groq=(groq_ok(GOOD),))
    body = plan().json()
    assert body["status"] == "PLANNED" and body["planner"] == "groq" and world.gemini.models.calls == []
    extraction = body["extraction"]
    assert extraction["summary_source"] == "document" and extraction["summary"] == GOOD.summary
    assert [p["summary_source"] for p in extraction["phases"]] == ["document"] * 5
    tasks = extraction["phases"][2]["sections"]["technical_tasks"]
    assert [t["text"] for t in tasks] == ["Implement redemption APIs", "Implement balance validation", "Implement rollback handling"]
    assert all(t["origin"] == "document" for t in tasks)
    assert "[copied from the source document]" not in body["planned_steps"][2]["arguments"]["content"]
    assert "Groq" not in str(body["warnings"])
    # The prompt now asks for phase summaries and allows prose actions as technical tasks.
    prompt = world.groq.calls[0]["messages"][0]["content"]
    assert "summary: the phase's own description" in prompt and "Never leave it empty" in prompt
    # The model reads the normalised document (no Markdown escapes, LF line endings).
    user = world.groq.calls[0]["messages"][1]["content"]
    assert "## Phase 1: Customer Registration" in user and "\\##" not in user and "\r" not in user


@pytest.mark.parametrize("groq_error", [groq_status_error(429), groq_status_error(503)])
def test_gemini_fallback_produces_the_same_validated_workflow(monkeypatch, tmp_path, groq_error):
    world = prose_world(monkeypatch, tmp_path, groq=(groq_error,), gemini=(ok_response(GOOD),))
    body = plan().json()
    assert body["status"] == "PLANNED" and body["planner"] == "gemini" and body["fallback_used"] is True
    assert body["primary_planner"] == "groq" and len(world.gemini.models.calls) == 1
    assert steps_of(body) == EXPECTED_STEPS
    assert [p["summary"] for p in body["extraction"]["phases"]] == DESCRIPTIONS


def test_gemini_fallback_with_empty_summaries_uses_the_source_text(monkeypatch, tmp_path):
    prose_world(monkeypatch, tmp_path, groq=(groq_status_error(429),), gemini=(ok_response(OBSERVED),))
    body = plan().json()
    assert body["status"] == "PLANNED" and body["planner"] == "gemini"
    assert [p["summary"] for p in body["extraction"]["phases"]] == DESCRIPTIONS


def test_invented_phase_summary_is_replaced_by_the_source_text(monkeypatch, tmp_path):
    invented = GOOD.model_copy(deep=True)
    invented.phases[2].summary = "Launch a blockchain loyalty token with partner airlines."
    prose_world(monkeypatch, tmp_path, groq=(groq_ok(invented),))
    body = plan().json()
    assert body["status"] == "PLANNED"
    third = body["extraction"]["phases"][2]
    assert third["summary"] == DESCRIPTIONS[2] and third["summary_source"] == "source_text"
    assert any("Phase 3 'Points Redemption': the AI summary was not found in the source document" in w
               for w in body["warnings"])


# ======================= missing source details: clear, blocking errors =======================


def test_a_phase_without_any_source_description_is_a_clear_blocking_error(monkeypatch, tmp_path):
    world = prose_world(monkeypatch, tmp_path)
    world.google.files[REAL_SOURCE]["content"] = PROSE_PLAN.replace(
        DESCRIPTIONS[2], "").encode("utf-8")  # Phase 3 heading with no text under it
    response = plan()
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "INVALID" and body["planned_steps"] == []
    [issue] = body["validation"]["issues"]
    assert issue["code"] == "MISSING_VALUE" and issue["field"] == "phases[2].summary" and issue["step_number"] == 5
    assert issue["message"].startswith("Phase 3 'Points Redemption': summary is empty - source phase "
                                       "'Points Redemption' has no description or objectives in the document.")
    assert world.notion.tool_calls == [] and world.teams.posts == []


def test_project_summary_missing_everywhere_is_reported_not_invented():
    doc = "# Rewards\n\n## Step 1: Something\n\nNothing parseable as a phase.\n"
    extraction = ProjectExtraction(project_title="Rewards", summary="", phases=[])
    review = review_extraction(extraction, doc, expected_phases=1)
    codes = {(i.code, i.field) for i in review.issues}
    assert ("MISSING_VALUE", "summary") in codes and review.summary == ""
    message = next(i.message for i in review.issues if i.field == "summary")
    assert "does not invent one" in message


def test_resolve_summary_rules():
    tokens = set(_tokens(normalize_source(PROSE_PLAN)))
    assert _resolve_summary(DESCRIPTIONS[0], "src", has_doc=True, doc_tokens=tokens) == (DESCRIPTIONS[0], "document", None)
    assert _resolve_summary("", "src", has_doc=True, doc_tokens=tokens) == ("src", "source_text", "was empty")
    assert _resolve_summary("Quantum ledger on Mars", "", has_doc=True, doc_tokens=tokens) == \
        ("", "none", "was not found in the source document")
    assert _resolve_summary("", "objectives", has_doc=False, doc_tokens=set()) == ("objectives", "generated", None)


# ======================= deterministic parsing (mock) =======================


def test_outline_and_mock_parser_read_escaped_crlf_markdown():
    outline = source_outline(PROSE_PLAN)
    assert outline.title == "Customer Rewards Platform"
    assert outline.overview == "Architecture: Java 21, Spring Boot, AWS Lambda, DynamoDB."
    assert [p.title for p in outline.phases] == TITLES
    assert [p.description for p in outline.phases] == DESCRIPTIONS
    extraction = MockExtractor().extract("Customer_Rewards_Plan.md", PROSE_PLAN, expected_phases=5)
    assert [p.title for p in extraction.phases] == TITLES
    assert [p.summary for p in extraction.phases] == DESCRIPTIONS
    review = review_extraction(extraction, normalize_source(PROSE_PLAN), expected_phases=5)
    assert review.issues == [] and review.summary.startswith("Architecture: Java 21")


def test_structured_fixture_keeps_its_own_summary_and_sections():
    """The original structured document (## Summary + ### sections) is unaffected."""
    extraction = MockExtractor().extract("plan.md", CUSTOMER_REWARDS_PLAN, expected_phases=5)
    assert extraction.summary.startswith("The Customer Rewards Platform lets customers register")
    review = review_extraction(extraction, CUSTOMER_REWARDS_PLAN, expected_phases=5)
    assert review.issues == [] and review.summary_source == "document"
    first = review.phases[0]
    # No prose under the heading: the phase summary is its own listed objectives.
    assert first.summary == ("Let customers create a rewards account with email verification; Link a rewards account "
                             "to an existing customer profile.")
    assert first.priority == "High" and first.priority_source == "document" and first.owner == "Identity team"
    assert [t for t, _ in first.items["technical_tasks"]][0] == "Build the registration API with input validation"
