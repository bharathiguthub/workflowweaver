"""Deterministic keyword-based planner. Needs no API key and makes no network calls.

It splits the instruction into clauses and maps each clause to a step by keyword. It also
emits steps for tools we do NOT support (Slack, Jira, shell commands) so the validator's
rejection paths can be demonstrated and tested without an LLM.
"""

import re

from app.schemas.plan import DraftPlan, DraftStep, StepArgument

_CLAUSE_SPLIT = re.compile(r",|;|\.\s|\bthen\b|\band\b", re.IGNORECASE)
_READ_WORDS = re.compile(r"\b(read|fetch|open|load|get)\b", re.IGNORECASE)
_SHELL_WORDS = re.compile(r"\b(shell|script|command|powershell|terminal)\b", re.IGNORECASE)
_NOTIFY_INTEGRATIONS = {"teams", "slack"}


def _args(**values: str) -> list[StepArgument]:
    return [StepArgument(name=k, value=v) for k, v in values.items()]


class MockPlanner:
    name = "mock"

    def plan(self, instruction: str, *, max_steps: int) -> DraftPlan:
        title = _title_from(instruction)
        steps: list[DraftStep] = []

        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(instruction)):
            draft = self._step_for_clause(clause, title, instruction)
            if draft is None:
                continue
            integration, action, description, arguments = draft
            number = len(steps) + 1
            steps.append(
                DraftStep(
                    step_number=number,
                    integration=integration,
                    action=action,
                    description=description,
                    arguments=arguments,
                    depends_on=self._dependencies(integration, steps),
                )
            )

        integrations = ", ".join(dict.fromkeys(s.integration for s in steps)) or "none"
        return DraftPlan(
            summary=f"Planned {len(steps)} step(s) using: {integrations}.",
            steps=steps,
        )

    @staticmethod
    def _step_for_clause(
        clause: str, title: str, instruction: str
    ) -> tuple[str, str, str, list[StepArgument]] | None:
        lowered = clause.lower()
        if "notion" in lowered:
            if "task" in lowered:
                return "notion", "create_task", "Create a task in Notion", _args(
                    title=f"{title} - Action items"
                )
            return "notion", "create_page", "Create a Notion project page", _args(
                title=f"{title} - Project page",
                content=f"Project page generated from instruction: {instruction}",
            )
        if "drive" in lowered:
            if _READ_WORDS.search(lowered):
                return "google_drive", "read_document", "Read source document from Google Drive", _args(
                    document_name=f"{title} - Source notes"
                )
            return "google_drive", "create_document", "Save a summary document to Google Drive", _args(
                title=f"{title} - Summary",
                content=f"Summary of: {instruction}",
            )
        if "teams" in lowered:
            return "teams", "send_message", "Notify the team in Microsoft Teams", _args(
                message=f"'{title}' workflow is ready for review."
            )
        if "slack" in lowered:
            return "slack", "send_message", "Notify the team in Slack", _args(message=title)
        if "jira" in lowered:
            return "jira", "create_issue", "Create a Jira issue", _args(title=title)
        if _SHELL_WORDS.search(lowered):
            return "system", "run_shell", "Run a shell command", _args(command=clause)
        return None  # e.g. "Read meeting notes" is input context, not a tool step

    @staticmethod
    def _dependencies(integration: str, previous: list[DraftStep]) -> list[int]:
        # Notifications go out after everything before them; other steps depend on any
        # earlier read step (they consume what was read).
        if integration in _NOTIFY_INTEGRATIONS:
            return [s.step_number for s in previous]
        return [s.step_number for s in previous if s.action == "read_document"]


def _title_from(instruction: str) -> str:
    first_clause = _CLAUSE_SPLIT.split(instruction, maxsplit=1)[0]
    words = re.sub(r"\s+", " ", first_clause).strip().split(" ")[:6]
    title = " ".join(words).strip(",.;: ")
    return title[:1].upper() + title[1:] if title else "Synthetic workflow"
