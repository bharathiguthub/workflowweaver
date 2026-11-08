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
        # parent: approved destination alias. parent_step: the step_number of an earlier
        # notion.create_page step in the same plan; the page is created inside that page.
        # Content lines starting with "- " become bulleted list items.
        Action.CREATE_PAGE: ActionSpec(
            "Create a Notion page", frozenset({"title", "content"}), frozenset({"parent", "parent_step"})
        ),
        Action.CREATE_TASK: ActionSpec(
            "Create a Notion task", frozenset({"title"}), frozenset({"assignee", "due_date"})
        ),
    },
    Integration.GOOGLE_DRIVE: {
        Action.CREATE_DOCUMENT: ActionSpec(
            "Create a Google Drive document", frozenset({"title", "content"}), frozenset({"folder", "parent_step"})
        ),
        # folder / parent_step: read from an approved folder or a folder created by this run
        # (used to verify an uploaded report).
        # file_id / expected_sha256 (Phase 7.2, server-compiled orchestration plans): read exactly
        # this file (never "the" file with a name) and refuse it if its content changed since
        # planning. The Drive REST and mock adapters support them; others reject them.
        # content_sha256 (server-built Drive Sandbox Test): read-back verification - the document
        # read must hash to exactly the content this plan uploaded, or the step fails (and the
        # run's own folder and report are rolled back). REST and mock adapters only.
        Action.READ_DOCUMENT: ActionSpec(
            "Read a Google Drive document", frozenset({"document_name"}),
            frozenset({"folder", "parent_step", "file_id", "expected_sha256", "content_sha256"})
        ),
        # folder: approved folder alias. parent_step: the step_number of an earlier
        # google_drive.create_folder step in the same plan; the item goes inside that folder.
        Action.CREATE_FOLDER: ActionSpec(
            "Create a Google Drive folder", frozenset({"name"}), frozenset({"folder", "parent_step"})
        ),
        Action.LIST_FILES: ActionSpec(
            "List files in a Google Drive folder (read-only)", frozenset(), frozenset({"folder", "parent_step"})
        ),
    },
    Integration.TEAMS: {
        # link_step (Phase 7.2): the step_number of an earlier notion.create_page step; the
        # adapter appends that page's link (from this run's own results) to the message.
        Action.SEND_MESSAGE: ActionSpec(
            "Send a Microsoft Teams message", frozenset({"message"}), frozenset({"channel", "link_step"})
        ),
    },
}

# Every Google Drive action names its folder with exactly one of these (see validation.py).
DRIVE_DESTINATION_ARGS = frozenset({"folder", "parent_step"})

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


def describe_catalog(allowed_integrations=None) -> str:
    """Human-readable catalog, embedded in the planner system prompts. ``allowed_integrations``
    (integration values) narrows it to the selected integration target."""
    lines = []
    for integration, actions in ACTION_CATALOG.items():
        if allowed_integrations is not None and integration.value not in allowed_integrations:
            continue
        for action, spec in actions.items():
            required = ", ".join(sorted(spec.required)) or "none"
            if integration is Integration.GOOGLE_DRIVE and DRIVE_DESTINATION_ARGS <= spec.optional:
                # Not two independent optional args: every Drive step needs exactly one destination.
                others = ", ".join(sorted(spec.optional - DRIVE_DESTINATION_ARGS)) or "none"
                lines.append(
                    f"- integration={integration.value} action={action.value}: {spec.description}. "
                    f"required args: {required}; destination (exactly one, required): folder or parent_step; "
                    f"optional args: {others}"
                )
                continue
            optional = ", ".join(sorted(spec.optional)) or "none"
            lines.append(
                f"- integration={integration.value} action={action.value}: {spec.description}. "
                f"required args: {required}; optional args: {optional}"
            )
    return "\n".join(lines)
