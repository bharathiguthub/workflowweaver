"""Deterministic keyword-based planner. Needs no API key and makes no network calls.

It splits the instruction into clauses and maps each clause to a step by keyword. It also
emits steps for tools we do NOT support (Slack, Jira, shell commands) so the validator's
rejection paths can be demonstrated and tested without an LLM.
"""

import re

from app.agents.synthetic_documents import canonical_document_name, find_document_mentioned_in
from app.schemas.plan import DraftPlan, DraftStep, StepArgument

_CLAUSE_SPLIT = re.compile(r",|;|\.\s|\bthen\b|\band\b", re.IGNORECASE)
_READ_WORDS = re.compile(r"\b(read|fetch|open|load|get)\b", re.IGNORECASE)
_SHELL_WORDS = re.compile(r"\b(shell|script|command|powershell|terminal)\b", re.IGNORECASE)
_NOTIFY_INTEGRATIONS = {"teams", "slack"}
# A quoted document name: opening quote at start/after whitespace, closing quote before a
# non-word character (so apostrophes such as "team's" are not mistaken for quotes).
_QUOTED = re.compile(r"(?:^|(?<=\s))[\"'\u2018\u201c]([^\"'\u2019\u201d\n]{2,200})[\"'\u2019\u201d](?=\W|$)")
_LEADING_READ = re.compile(r"^\s*(?:read|fetch|open|load|get)\s+(?:the\s+|my\s+|our\s+|a\s+)?", re.IGNORECASE)
_DRIVE_SUFFIX = re.compile(
    r"\s+(?:from|in|on|out\s+of)\s+(?:the\s+|my\s+|our\s+)?(?:google\s+)?drive\b.*$", re.IGNORECASE
)


def _args(**values: str) -> list[StepArgument]:
    return [StepArgument(name=k, value=v) for k, v in values.items()]


class MockPlanner:
    name = "mock"

    def plan(self, instruction: str, *, max_steps: int) -> DraftPlan:
        clauses = [c.strip() for c in _CLAUSE_SPLIT.split(instruction)]
        title = workflow_subject(clauses)
        steps: list[DraftStep] = []

        for clause in clauses:
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
                    title=f"Action items: {title}"
                )
            return "notion", "create_page", "Create a Notion project page", _args(
                title=f"{title}: project page",
                content=f"Project page generated from instruction: {instruction}",
            )
        if "drive" in lowered:
            if _READ_WORDS.search(lowered):
                return "google_drive", "read_document", "Read source document from Google Drive", _args(
                    document_name=requested_document_name(clause)
                )
            return "google_drive", "create_document", "Save a summary document to Google Drive", _args(
                title=f"{title}: summary",
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


def requested_document_name(clause: str) -> str:
    """The Drive document a read clause asks for.

    1. A quoted name ("Read 'Synthetic standup notes' from Google Drive").
    2. Otherwise a synthetic catalog name mentioned verbatim in the clause.
    3. Otherwise the noun phrase the user used ("the synthetic Sprint 42 meeting notes").
    A name that matches the catalog is returned in its canonical spelling. Nothing is
    invented or fuzzily matched: an unknown name is kept as requested, so execution
    reports DOCUMENT_NOT_FOUND for it instead of reading a different document.
    """
    quoted = _QUOTED.search(clause)
    if quoted:
        name = quoted.group(1).strip()
    else:
        mentioned = find_document_mentioned_in(clause)
        if mentioned:
            return mentioned
        name = _DRIVE_SUFFIX.sub("", _LEADING_READ.sub("", clause)).strip(" .,;:")
    name = " ".join(name.split())[:200]
    return canonical_document_name(name) or name or "Untitled document"


_LEADING_VERB = re.compile(
    r"^\s*(?:please\s+)?(?:read|fetch|open|load|get|review|summari[sz]e|analy[sz]e|use|take|collect|gather|"
    r"process|extract|draft|prepare|create|save|share|notify|alert|send|post|turn|plan|coordinate|triage)\s+"
    r"(?:the\s+|my\s+|our\s+|a\s+|an\s+|all\s+|this\s+|today'?s\s+)?",
    re.IGNORECASE,
)
_TRAILING_LOCATION = re.compile(
    r"\s+(?:from|in|on|to|into|out\s+of|via)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?:google\s+drive|drive|notion|microsoft\s+teams|teams|slack|jira)\b.*$",
    re.IGNORECASE,
)
MAX_SUBJECT = 60


def workflow_subject(clauses: list[str]) -> str:
    """Short human-readable subject used in generated titles and messages.

    1. The document a Drive read clause refers to (it is what the workflow is about).
    2. Otherwise the first clause without its leading verb/article and trailing tool
       ("Load the sprint backlog" -> "Sprint backlog").
    Truncated at a word boundary; never mid-word.
    """
    read_clause = next((c for c in clauses if "drive" in c.lower() and _READ_WORDS.search(c)), None)
    if read_clause:
        subject = requested_document_name(read_clause)
    else:
        first = next((c for c in clauses if c), "")
        subject = _TRAILING_LOCATION.sub("", _LEADING_VERB.sub("", first))
    subject = " ".join(subject.split()).strip(" ,.;:'\"\u2018\u2019\u201c\u201d")
    if len(subject) > MAX_SUBJECT:
        cut = subject[:MAX_SUBJECT].rsplit(" ", 1)[0]
        subject = f"{cut}..."
    return subject[:1].upper() + subject[1:] if subject else "Synthetic workflow"
