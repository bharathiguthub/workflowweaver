"""Document Management: drafts over the 4800-character limit.

Regression for "DOCUMENT_TOO_LONG (step 1): The document is 4883 characters; the limit is 4800."
The prompt now targets 3500-4000 characters; a draft that still renders over the limit is
regenerated ONCE by the same provider with a compression instruction, the regenerated draft
goes through every document and plan check, and a draft that is still too long is rejected with
its actual length. Nothing is truncated and the limit is unchanged.

Groq and Gemini are fake SDK clients; Google Drive is a fake. No real call or write happens.
"""

import pytest
from fastapi.testclient import TestClient

from app.agents.documents import (
    COMPRESSED_TARGET,
    RENDERED_MAX,
    TARGET_MAX,
    TARGET_MIN,
    DocumentDraft,
    DocumentSection,
    build_document_system_prompt,
    render_document,
)
from app.agents.plan_registry import get_plan_registry
from app.agents.validation import MAX_ARGUMENT_LENGTH
from app.main import app
from app.schemas.execution import ExecutionStatus
from tests.execution_helpers import make_service
from tests.test_documents import Env, body, execution_request
from tests.test_gemini_planner import ok_response
from tests.test_planner_fallback import groq_ok, groq_status_error

PLAN_URL = "/api/v1/workflows/documents/plan"
PROJECT = "Customer Rewards Platform"
SENTENCE = "The points ledger records every credit and debit with its source transaction and timestamp. "


def draft_with_body_chars(total_body: int, *, title="Customer Rewards Requirements") -> DocumentDraft:
    """A valid draft whose section bodies add up to about ``total_body`` characters."""
    sections, remaining, n = [], total_body, 1
    while remaining > 0:
        size = min(1400, remaining)
        text = (SENTENCE * (size // len(SENTENCE) + 1))[:size].rsplit(" ", 1)[0] + "."
        sections.append(DocumentSection(heading=f"Requirement area {n}", body=text))
        remaining -= size
        n += 1
    return DocumentDraft(title=title, summary="Requirements for the synthetic rewards platform.", sections=sections)


def rendered_len(draft: DocumentDraft) -> int:
    # The stamp has a fixed width, so any stamp gives the server's exact length.
    return len(render_document(draft, document_type="requirements", project_name=PROJECT, stamp="2026-01-01 00:00 UTC"))


OVERSIZED = draft_with_body_chars(4550)   # renders just over the limit, like the reported 4883
COMPRESSED = draft_with_body_chars(3000)  # what a successful compression returns
FITS = draft_with_body_chars(3400)


@pytest.fixture(autouse=True)
def _clean():
    get_plan_registry().clear()
    yield
    get_plan_registry().clear()
    app.dependency_overrides.clear()


def post(mode="dry_run"):
    return TestClient(app).post(PLAN_URL, json=body(mode=mode, project_name=PROJECT))


def system_prompts(env) -> list[str]:
    return [c["messages"][0]["content"] for c in env.groq.calls]


def test_fixture_sizes_reproduce_the_reported_situation():
    assert RENDERED_MAX == 4800 == MAX_ARGUMENT_LENGTH - 200  # the limit is unchanged
    assert 4800 < rendered_len(OVERSIZED) < MAX_ARGUMENT_LENGTH
    assert rendered_len(COMPRESSED) < rendered_len(FITS) < 4800


def test_prompt_targets_3500_to_4000_characters_and_compression_is_a_system_instruction():
    prompt = build_document_system_prompt("requirements")
    assert (TARGET_MIN, TARGET_MAX) == (3500, 4000)
    assert "aim for 3500-4000 characters in total" in prompt and "the upload limit is 4800 characters" in prompt
    assert "COMPRESSION REQUIRED" not in prompt
    compressed = build_document_system_prompt("requirements", compress_from=4883)
    assert "COMPRESSION REQUIRED: your previous draft for this request was 4883 characters" in compressed
    assert f"at most {COMPRESSED_TARGET} characters" in compressed
    assert "Keep every essential technical fact" in compressed
    assert compressed.startswith(prompt)  # every original rule still applies


def test_a_draft_that_fits_is_used_without_a_retry(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, groq=(groq_ok(FITS),))
    plan = post().json()
    assert plan["status"] == "PLANNED" and plan["compression_retry"] is False and plan["initial_chars"] is None
    assert len(env.groq.calls) == 1


def test_oversized_draft_is_regenerated_once_compressed_and_then_planned(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, groq=(groq_ok(OVERSIZED), groq_ok(COMPRESSED)))
    response = post()
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["status"] == "PLANNED" and plan["planner"] == "groq" and plan["fallback_used"] is False
    assert plan["compression_retry"] is True and plan["initial_chars"] == rendered_len(OVERSIZED)
    assert plan["content_chars"] == len(plan["content"]) == rendered_len(COMPRESSED) <= RENDERED_MAX
    # Exactly two calls to the same provider; the user message is unchanged, the system prompt
    # carries the compression instruction with the first draft's real length.
    assert len(env.groq.calls) == 2 and env.gemini.models.calls == []
    first, second = system_prompts(env)
    assert "COMPRESSION REQUIRED" not in first
    assert f"your previous draft for this request was {rendered_len(OVERSIZED)} characters" in second
    assert env.groq.calls[0]["messages"][1] == env.groq.calls[1]["messages"][1]
    # The reviewed preview is exactly what would be uploaded: the compressed draft, untruncated.
    upload = plan["planned_steps"][0]["arguments"]
    assert upload["content"] == plan["content"] and plan["document"]["sections"] == \
        [s.model_dump() for s in COMPRESSED.sections]


def test_compressed_draft_is_approved_and_executed_exactly_as_reviewed(monkeypatch, tmp_path):
    Env(monkeypatch, tmp_path, groq=(groq_ok(OVERSIZED), groq_ok(COMPRESSED)))
    plan = TestClient(app).post(PLAN_URL, json=body(mode="mock", project_name=PROJECT)).json()
    service, _ = make_service()
    result = service.execute(execution_request(plan))
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    stored = [r for r in service.registry.store.resources.values() if r["type"] == "drive_document"]
    assert [r["data"]["content"] for r in stored] == [plan["content"]]
    # Editing the reviewed (compressed) content after review is still refused.
    steps = [{k: s[k] for k in ("step_number", "integration", "action", "description", "arguments", "depends_on")}
             for s in plan["planned_steps"]]
    steps[0]["arguments"]["content"] += "\nAppended after review."
    service2, _ = make_service()
    assert [e.code for e in service2.execute(execution_request(plan, steps=steps)).errors] == ["APPROVAL_PLAN_MISMATCH"]


def test_still_too_long_after_the_retry_is_a_clear_error_with_the_actual_length(monkeypatch, tmp_path):
    still_long = draft_with_body_chars(4500, title="Customer Rewards Requirements v2")
    env = Env(monkeypatch, tmp_path, groq=(groq_ok(OVERSIZED), groq_ok(still_long)))
    response = post()
    assert response.status_code == 422
    plan = response.json()
    assert plan["status"] == "INVALID" and plan["planned_steps"] == [] and plan["plan_digest"] is None
    [issue] = plan["validation"]["issues"]
    assert issue["code"] == "DOCUMENT_TOO_LONG" and issue["step_number"] == 1
    assert issue["message"] == (
        f"The document is {rendered_len(still_long)} characters after one automatic compression retry (first draft: "
        f"{rendered_len(OVERSIZED)} characters); the limit is 4800. Ask for a shorter document or fewer sections.")
    # Only one retry, no fallback for an invalid document, and nothing truncated in the preview.
    assert len(env.groq.calls) == 2 and env.gemini.models.calls == []
    assert plan["content_chars"] == len(plan["content"]) == rendered_len(still_long)


def test_far_too_long_documents_are_reported_without_truncation(monkeypatch, tmp_path):
    huge = draft_with_body_chars(5600)
    Env(monkeypatch, tmp_path, groq=(groq_ok(huge), groq_ok(huge)))
    plan = post().json()
    assert plan["status"] == "INVALID"
    codes = [i["code"] for i in plan["validation"]["issues"]]
    assert codes[0] == "DOCUMENT_TOO_LONG"  # the document's own explanation comes first
    assert len(plan["content"]) == plan["content_chars"] > MAX_ARGUMENT_LENGTH  # shown in full, never cut


@pytest.mark.parametrize("bad_body, code", [
    ("See https://example.invalid/spec for details.", "URL_NOT_ALLOWED"),
    ("Then run rm -rf / on the host.", "UNSAFE_CONTENT"),
    ("Contact owner@example.invalid for access.", "PERSONAL_DATA_NOT_ALLOWED"),
])
def test_the_regenerated_draft_goes_through_every_safety_check(monkeypatch, tmp_path, bad_body, code):
    unsafe = COMPRESSED.model_copy(deep=True)
    unsafe.sections[0].body = bad_body
    Env(monkeypatch, tmp_path, groq=(groq_ok(OVERSIZED), groq_ok(unsafe)))
    plan = post().json()
    assert plan["status"] == "INVALID" and plan["compression_retry"] is True
    codes = [i["code"] for i in plan["validation"]["issues"]]
    assert code in codes and "DOCUMENT_TOO_LONG" not in codes


def test_gemini_fallback_gets_the_same_single_compression_retry(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, groq=(groq_status_error(429),),
              gemini=(ok_response(OVERSIZED), ok_response(COMPRESSED)))
    plan = post().json()
    assert plan["status"] == "PLANNED" and plan["planner"] == "gemini" and plan["fallback_used"] is True
    assert plan["primary_planner"] == "groq" and plan["compression_retry"] is True
    assert len(env.gemini.models.calls) == 2
    assert plan["content_chars"] == rendered_len(COMPRESSED)


def test_dry_run_of_a_compressed_document_sends_nothing(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, groq=(groq_ok(OVERSIZED), groq_ok(COMPRESSED)))
    plan = post().json()
    result = env.drive.service.execute(execution_request(plan), write_authorized=False)
    assert result.status is ExecutionStatus.COMPLETED, result.errors
    assert env.google.calls == []
