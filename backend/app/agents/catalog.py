"""Allow-list of integrations, actions and their arguments.

This is the single source of truth for what a plan may contain. The validator enforces it
and the Claude planner's system prompt is generated from it, so the two never drift apart.
"""

from dataclasses import dataclass, field

from app.schemas.plan import Action
from app.schemas.workflow import Integration


@dataclass(frozen=True)
class ActionSpec:
    description: str
    required: frozenset[str]
    optional: frozenset[str] = field(default_factory=frozenset)

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


ACTION_CATALOG: dict[Integration, dict[Action, ActionSpec]] = {
    Integration.NOTION: {
        Action.CREATE_PAGE: ActionSpec(
            "Create a Notion page", frozenset({"title", "content"}), frozenset({"parent"})
        ),
        Action.CREATE_TASK: ActionSpec(
            "Create a Notion task", frozenset({"title"}), frozenset({"assignee", "due_date"})
        ),
    },
    Integration.GOOGLE_DRIVE: {
        Action.CREATE_DOCUMENT: ActionSpec(
            "Create a Google Drive document", frozenset({"title", "content"}), frozenset({"folder"})
        ),
        Action.READ_DOCUMENT: ActionSpec(
            "Read a Google Drive document", frozenset({"document_name"})
        ),
    },
    Integration.TEAMS: {
        Action.SEND_MESSAGE: ActionSpec(
            "Send a Microsoft Teams message", frozenset({"message"}), frozenset({"channel"})
        ),
    },
}

# Common spellings an LLM might use for a supported integration.
INTEGRATION_ALIASES: dict[str, Integration] = {
    "notion": Integration.NOTION,
    "google_drive": Integration.GOOGLE_DRIVE,
    "googledrive": Integration.GOOGLE_DRIVE,
    "gdrive": Integration.GOOGLE_DRIVE,
    "drive": Integration.GOOGLE_DRIVE,
    "teams": Integration.TEAMS,
    "microsoft_teams": Integration.TEAMS,
    "ms_teams": Integration.TEAMS,
}


def normalize_integration(raw: str) -> Integration | None:
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return INTEGRATION_ALIASES.get(key)


def describe_catalog() -> str:
    """Human-readable catalog, embedded in the Claude system prompt."""
    lines = []
    for integration, actions in ACTION_CATALOG.items():
        for action, spec in actions.items():
            optional = ", ".join(sorted(spec.optional)) or "none"
            lines.append(
                f"- integration={integration.value} action={action.value}: {spec.description}. "
                f"required args: {', '.join(sorted(spec.required))}; optional args: {optional}"
            )
    return "\n".join(lines)
