"""Phase 4 Stage 4: Claude planner receives the synthetic document catalog (names only) in
mock execution mode, and the mock planner generates readable titles."""

import pytest

from app.agents.planners.claude import ClaudePlanner, build_system_prompt
from app.agents.planners.factory import create_planner
from app.agents.planners.mock import MAX_SUBJECT, MockPlanner, workflow_subject
from app.agents.synthetic_documents import SYNTHETIC_DRIVE_DOCUMENTS
from app.api.workflows import TEMPLATES_BY_ID
from app.core.config import Settings
from tests.conftest import EXAMPLE_INSTRUCTION, FakeAnthropicClient, fake_response, valid_draft

CATALOG = frozenset(SYNTHETIC_DRIVE_DOCUMENTS)


# ---------- item 6: Claude planner + synthetic catalog ----------


def test_prompt_lists_document_names_but_never_contents():
    prompt = build_system_prompt(10, CATALOG)
    for name, content in SYNTHETIC_DRIVE_DOCUMENTS.items():
        assert f"- {name}" in prompt
        assert content not in prompt
    assert "Never substitute a different document" in prompt
    assert "created by an earlier create_document step" in prompt


def test_prompt_is_unchanged_without_a_catalog_and_deterministic_with_one():
    assert "Google Drive documents available" not in build_system_prompt(10)
    assert build_system_prompt(10, CATALOG) == build_system_prompt(10, set(reversed(sorted(CATALOG))))


def test_claude_request_carries_the_catalog():
    client = FakeAnthropicClient(fake_response(valid_draft()))
    ClaudePlanner(client=client, model="claude-synthetic", max_tokens=1000, available_documents=CATALOG).plan(
        EXAMPLE_INSTRUCTION, max_steps=10
    )
    system = client.messages.calls[0]["system"]
    assert "Synthetic standup notes" in system
    assert not any(c in system for c in SYNTHETIC_DRIVE_DOCUMENTS.values())
    # the instruction is still only in the user turn, wrapped as untrusted data
    assert EXAMPLE_INSTRUCTION not in system


@pytest.mark.parametrize(("mock_execution", "expected"), [(True, True), (False, False)])
def test_factory_passes_catalog_only_in_mock_execution_mode(monkeypatch, mock_execution, expected):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-real-key")
    planner = create_planner(Settings(_env_file=None, planner_mode="claude", anthropic_model="test-model",
                                      mock_execution_enabled=mock_execution))
    assert ("Synthetic standup notes" in build_system_prompt(10, planner._available_documents)) is expected


# ---------- item 7: readable titles ----------


@pytest.mark.parametrize(
    ("template_id", "expected"),
    [
        ("meeting-to-tasks", {"create_task": "Action items: Synthetic standup notes",
                              "create_document": "Synthetic standup notes: summary"}),
        ("sprint-planning", {"create_page": "Sprint backlog: project page"}),
        ("incident-response", {"create_document": "Synthetic incident report: summary"}),
    ],
)
def test_template_titles(template_id, expected):
    draft = MockPlanner().plan(TEMPLATES_BY_ID[template_id].example_instruction, max_steps=10)
    titles = {s.action: next(a.value for a in s.arguments if a.name == "title") for s in draft.steps
              if any(a.name == "title" for a in s.arguments)}
    assert titles == expected


@pytest.mark.parametrize(
    ("clauses", "expected"),
    [
        (["Read meeting notes", "create a Notion project page"], "Meeting notes"),
        (["Load the sprint backlog", "draft the plan in Notion"], "Sprint backlog"),
        (["Summarise the incident report"], "Incident report"),
        (["Read the synthetic Sprint 42 meeting notes from Google Drive"], "Synthetic Sprint 42 meeting notes"),
        (["Fetch 'Synthetic sprint backlog' from Google Drive"], "Synthetic sprint backlog"),
        ([""], "Synthetic workflow"),
    ],
)
def test_workflow_subject(clauses, expected):
    assert workflow_subject(clauses) == expected


def test_long_subjects_are_truncated_at_a_word_boundary():
    subject = workflow_subject(["Prepare " + "synthetic quarterly roadmap review " * 5])
    assert len(subject) <= MAX_SUBJECT + 3 and subject.endswith("...")
    assert subject[:-3].split(" ")[-1] in {"synthetic", "quarterly", "roadmap", "review"}  # whole word


def test_titles_no_longer_contain_instruction_fragments():
    for template in TEMPLATES_BY_ID.values():
        for step in MockPlanner().plan(template.example_instruction, max_steps=10).steps:
            for arg in step.arguments:
                if arg.name in {"title", "message"}:
                    assert not arg.value.startswith(("Read ", "Load ", "Fetch "))
                    assert "from Google" not in arg.value and "- Source notes" not in arg.value
