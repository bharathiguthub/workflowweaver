"""Pure validation of a planner's DraftPlan against the allow-list and safety rules.

No I/O and no LangGraph here, so every rule is unit-testable on its own.
"""

import re

from app.agents.catalog import ACTION_CATALOG, normalize_integration
from collections.abc import Collection

from app.agents.synthetic_documents import canonical_document_name
from app.schemas.plan import Action, DraftPlan, ValidationIssue, WorkflowPlan, WorkflowStep

MAX_ARGUMENT_LENGTH = 5_000

# Plans are data, never code. Reject values that look like code or shell to be safe even
# though Phase 2 has no executor at all.
UNSAFE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+-rf\b",
        r"\bsudo\b",
        r"\b(powershell|pwsh|cmd\.exe|/bin/(ba)?sh)\b",
        r"\bInvoke-Expression\b",
        r"\$\(",
        r"<script\b",
        r"\b(os\.system|subprocess\.\w+|eval|exec)\s*\(",
        r"\b__import__\b",
        r"\bdrop\s+table\b",
        r"\b(curl|wget)\b[^\n|]*\|\s*(ba)?sh\b",
    )
]


def find_unsafe_content(text: str) -> str | None:
    for pattern in UNSAFE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def validate_draft(draft: DraftPlan, *, max_steps: int) -> tuple[WorkflowPlan | None, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []

    if not draft.steps:
        return None, [ValidationIssue(code="EMPTY_PLAN", message="The plan contains no steps.")]
    if len(draft.steps) > max_steps:
        issues.append(
            ValidationIssue(
                code="TOO_MANY_STEPS",
                message=f"The plan has {len(draft.steps)} steps; the limit is {max_steps}.",
            )
        )

    validated: list[WorkflowStep] = []
    for index, step in enumerate(draft.steps, start=1):
        n = step.step_number
        if n != index:
            issues.append(
                ValidationIssue(
                    code="INVALID_STEP_NUMBER",
                    message=f"Step at position {index} is numbered {n}; steps must be numbered 1..N in order.",
                    step_number=n,
                    field="step_number",
                )
            )

        unsafe = find_unsafe_content(step.description)
        if unsafe:
            issues.append(_unsafe(n, "description", unsafe))

        integration = normalize_integration(step.integration)
        if integration is None:
            issues.append(
                ValidationIssue(
                    code="UNSUPPORTED_INTEGRATION",
                    message=f"Integration '{step.integration}' is not supported. "
                    "Allowed: notion, google_drive, teams.",
                    step_number=n,
                    field="integration",
                )
            )
            continue

        allowed_actions = ACTION_CATALOG[integration]
        action = next((a for a in allowed_actions if a.value == step.action.strip().lower()), None)
        if action is None:
            issues.append(
                ValidationIssue(
                    code="UNSUPPORTED_ACTION",
                    message=f"Action '{step.action}' is not allowed for {integration.value}. "
                    f"Allowed: {', '.join(a.value for a in allowed_actions)}.",
                    step_number=n,
                    field="action",
                )
            )
            continue

        arguments, arg_issues = _validate_arguments(step.arguments, integration, action, n)
        issues.extend(arg_issues)

        for dep in step.depends_on:
            if not 1 <= dep < n:
                issues.append(
                    ValidationIssue(
                        code="INVALID_DEPENDENCY",
                        message=f"Step {n} depends on step {dep}; dependencies must reference an earlier step.",
                        step_number=n,
                        field="depends_on",
                    )
                )

        validated.append(
            WorkflowStep(
                step_number=n,
                integration=integration,
                action=action,
                description=step.description,
                arguments=arguments,
                depends_on=sorted(set(step.depends_on)),
            )
        )

    issues.extend(_parent_step_issues(validated))
    issues.extend(teams_message_issues(validated))
    issues.extend(reference_argument_issues(validated))
    if issues:
        return None, issues
    return WorkflowPlan(summary=draft.summary, steps=validated), []


# ---------------- Phase 7.2: source binding, link references, orchestration rules ----------------

# Google Drive file ids (and the mock fixture ids): URL-safe, bounded. Anything else (URLs,
# paths, query syntax) is an untrusted identifier and is rejected.
DRIVE_FILE_ID = re.compile(r"[A-Za-z0-9_-]{10,128}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def reference_argument_issues(steps: list[WorkflowStep]) -> list[ValidationIssue]:
    """read_document file_id/expected_sha256 and teams link_step must be well-formed references."""
    issues: list[ValidationIssue] = []
    by_number = {s.step_number: s for s in steps}
    for step in steps:
        n, args = step.step_number, step.arguments
        if step.integration.value == "google_drive" and step.action is Action.READ_DOCUMENT:
            file_id, digest = args.get("file_id"), args.get("expected_sha256")
            if file_id is not None and not DRIVE_FILE_ID.fullmatch(file_id):
                issues.append(_arg_issue("INVALID_IDENTIFIER", f"Step {n} file_id is not a Google Drive file id.", n,
                                         "file_id"))
            if file_id is not None and "parent_step" in args:
                issues.append(_arg_issue("CONFLICTING_ARGUMENTS", f"Step {n} file_id needs folder (the approved "
                                                                  "folder alias), not parent_step.", n, "file_id"))
            if digest is not None and (file_id is None or not SHA256_HEX.fullmatch(digest)):
                issues.append(_arg_issue("INVALID_IDENTIFIER", f"Step {n} expected_sha256 must be 64 lowercase hex "
                                                               "characters and needs file_id.", n, "expected_sha256"))
            content_digest = args.get("content_sha256")
            if content_digest is not None and not SHA256_HEX.fullmatch(content_digest):
                issues.append(_arg_issue("INVALID_IDENTIFIER", f"Step {n} content_sha256 must be 64 lowercase hex "
                                                               "characters.", n, "content_sha256"))
        if step.integration.value == "teams" and "link_step" in args:
            raw = args["link_step"].strip()
            target = by_number.get(int(raw)) if raw.isdigit() else None
            if target is None or target.step_number >= n or (target.integration.value, target.action) != (
                    "notion", Action.CREATE_PAGE):
                issues.append(_arg_issue("INVALID_LINK_STEP", f"Step {n} link_step must be the number of an earlier "
                                                              "notion create_page step.", n, "link_step"))
            elif target.step_number not in step.depends_on:
                issues.append(_arg_issue("INVALID_LINK_STEP", f"Step {n} must depend on its link_step "
                                                              f"{target.step_number}.", n, "link_step"))
    return issues


# What the Drive + Notion + Teams orchestration target may do: read the bound source document
# (never write to Drive), create Notion pages, post Teams messages.
ORCHESTRATION_ACTIONS = frozenset({("google_drive", Action.READ_DOCUMENT), ("notion", Action.CREATE_PAGE),
                                   ("teams", Action.SEND_MESSAGE)})


def orchestration_issues(steps) -> list[ValidationIssue]:
    """Server-side rules for the drive_notion_teams target (planning AND execution):
      - only read_document / notion.create_page / teams.send_message;
      - every read_document is bound to a file id and its content hash;
      - exactly one top-level Notion page; every other page is its child (parent_step);
      - Teams steps come last and depend on EVERY Notion step, so no notification can be sent
        before all pages were created (the executor skips dependants of a failed step).
    ``steps`` are WorkflowSteps or validated dicts."""
    def get(step, key):
        value = step[key] if isinstance(step, dict) else getattr(step, key)
        return getattr(value, "value", value)

    issues: list[ValidationIssue] = []
    rows = [(get(s, "step_number"), get(s, "integration"), Action(get(s, "action")), get(s, "arguments"),
             set(get(s, "depends_on"))) for s in steps]
    for n, integration, action, args, _deps in rows:
        if (integration, action) not in ORCHESTRATION_ACTIONS:
            issues.append(ValidationIssue(code="ACTION_NOT_ALLOWED_FOR_TARGET", step_number=n, field="action",
                                          message=f"Step {n} {integration}.{action.value} is not allowed in the Google "
                                                  "Drive + Notion + Teams workflow."))
        elif action is Action.READ_DOCUMENT and not (args.get("file_id") and args.get("expected_sha256")):
            issues.append(ValidationIssue(code="SOURCE_NOT_BOUND", step_number=n, field="arguments.file_id",
                                          message=f"Step {n} must read the approved source by file_id and "
                                                  "expected_sha256."))
    pages = [(n, args) for n, integration, action, args, _ in rows if (integration, action) == ("notion", Action.CREATE_PAGE)]
    roots = [n for n, args in pages if not args.get("parent_step")]
    if pages and len(roots) != 1:
        issues.append(ValidationIssue(code="NOTION_HIERARCHY_INVALID", field="arguments.parent_step",
                                      message=f"The workflow must create exactly one top-level Notion page "
                                              f"(found {len(roots)})."))
    elif pages:
        for n, args in pages:
            if n != roots[0] and args.get("parent_step", "").strip() != str(roots[0]):
                issues.append(ValidationIssue(code="NOTION_HIERARCHY_INVALID", step_number=n,
                                              field="arguments.parent_step",
                                              message=f"Step {n} must be a child page of the project page "
                                                      f"(parent_step={roots[0]})."))
    page_numbers = {n for n, _ in pages}
    seen_teams = False
    for n, integration, _action, _args, deps in rows:
        if integration == "teams":
            seen_teams = True
            missing = sorted(page_numbers - deps)
            if missing or any(p > n for p in page_numbers):
                issues.append(ValidationIssue(code="TEAMS_BEFORE_NOTION", step_number=n, field="depends_on",
                                              message=f"Step {n} (Teams) must come after and depend on every Notion step "
                                                      f"({', '.join(map(str, sorted(page_numbers)))})."))
        elif seen_teams:
            issues.append(ValidationIssue(code="TEAMS_NOT_LAST", step_number=n, field="step_number",
                                          message=f"Step {n} runs after a Teams notification; Teams messages cannot be "
                                                  "unsent, so they must be the last steps."))
    return issues


def plan_digest(steps, target: str | None, inputs: str | None = None) -> str:
    """SHA-256 over exactly what executes: target and each step's number, integration, action,
    arguments and dependencies (descriptions are display text). Recorded when a plan is
    produced and re-computed from the submitted plan at execution, so an approval is bound to
    the exact plan that was reviewed. ``inputs`` (Phase 7.2): the input digest of what the plan was
    made from (source + instructions); when given it is part of the digest. ``steps`` are
    WorkflowSteps or dicts."""
    import hashlib
    import json

    def get(step, key):
        value = step[key] if isinstance(step, dict) else getattr(step, key)
        return getattr(value, "value", value)

    canonical = {
        "target": target,
        **({"inputs": inputs} if inputs else {}),
        "steps": [{"step_number": get(s, "step_number"), "integration": str(get(s, "integration")).strip().lower(),
                   "action": str(get(s, "action")).strip().lower(), "arguments": dict(sorted(get(s, "arguments").items())),
                   "depends_on": sorted(set(get(s, "depends_on")))}
                  for s in sorted(steps, key=lambda s: get(s, "step_number"))],
    }
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------- Microsoft Teams: message content and destination aliases ----------------

TEAMS_MAX_MESSAGE_CHARS = 4000
# Destination aliases are short identifiers; a URL (the webhook itself) is never a valid value.
TEAMS_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Adaptive Card TextBlocks do not render HTML; tags (including <at> mentions) are rejected.
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def teams_message_issues(steps: list[WorkflowStep]) -> list[ValidationIssue]:
    """Content rules for teams.send_message: plain text up to 4000 characters, no control
    characters, no HTML/mention tags, and ``channel`` (if given) must look like an alias.
    Messages never echo the offending value (it may be a webhook URL)."""
    issues: list[ValidationIssue] = []
    for step in steps:
        if step.integration.value != "teams" or step.action is not Action.SEND_MESSAGE:
            continue
        n = step.step_number
        message = step.arguments.get("message", "")
        if len(message) > TEAMS_MAX_MESSAGE_CHARS:
            issues.append(_arg_issue("MESSAGE_TOO_LONG",
                                     f"Teams messages are limited to {TEAMS_MAX_MESSAGE_CHARS} characters "
                                     f"(step {n} has {len(message)}).", n, "message"))
        if _CONTROL_CHARS.search(message):
            issues.append(_arg_issue("INVALID_MESSAGE_CONTENT",
                                     f"Step {n} message contains control characters; use plain text.", n, "message"))
        if _HTML_TAG.search(message) and not find_unsafe_content(message):  # unsafe is reported already
            issues.append(_arg_issue("INVALID_MESSAGE_CONTENT",
                                     f"Step {n} message contains HTML or mention tags; Teams cards show plain text "
                                     "and Markdown only.", n, "message"))
        channel = step.arguments.get("channel")
        if channel is not None and not TEAMS_ALIAS.fullmatch(channel.strip().lower()):
            issues.append(_arg_issue("INVALID_DESTINATION",
                                     f"Step {n} channel must be an approved channel alias (letters, digits, '-', "
                                     "'_'), never a URL or webhook address.", n, "channel"))
    return issues


def teams_destination_issues(steps, allowed_channels) -> list[ValidationIssue]:
    """Teams destination allowlist for dry-run/real Teams Only plans: every teams step must name
    exactly an approved channel alias. ``allowed_channels`` None = no restriction (mock mode)."""
    if allowed_channels is None:
        return []
    allowed = {a.lower() for a in allowed_channels}
    names = ", ".join(sorted(allowed)) or "(none configured)"
    issues: list[ValidationIssue] = []
    for step in steps:
        integration = step["integration"] if isinstance(step, dict) else step.integration.value
        if integration != "teams":
            continue
        number = step["step_number"] if isinstance(step, dict) else step.step_number
        arguments = step["arguments"] if isinstance(step, dict) else step.arguments
        channel = arguments.get("channel", "").strip().lower()
        if not channel:
            issues.append(ValidationIssue(
                code="DESTINATION_REQUIRED", step_number=number, field="arguments.channel",
                message=f"Step {number} has no Teams channel; use channel={names}."))
        elif channel not in allowed:
            shown = channel[:40] if TEAMS_ALIAS.fullmatch(channel) else "(not an alias)"
            issues.append(ValidationIssue(
                code="DESTINATION_NOT_APPROVED", step_number=number, field="arguments.channel",
                message=f"Step {number} channel '{shown}' is not an approved Teams destination (allowed: {names})."))
    return issues


def normalize_teams_references(draft: DraftPlan, allowed_channels) -> tuple[DraftPlan, list[str]]:
    """Fill a MISSING Teams channel only when exactly one approved alias exists (dry-run/real
    Teams plans). A channel the planner gave is never changed; the result is fully validated."""
    if allowed_channels is None:
        return draft, []
    aliases = sorted({a.lower() for a in allowed_channels})
    if len(aliases) != 1:
        return draft, []
    from app.schemas.plan import StepArgument

    steps = [s.model_copy(deep=True) for s in draft.steps]
    notes: list[str] = []
    for step in steps:
        integration = normalize_integration(step.integration)
        if integration is None or integration.value != "teams" or step.action.strip().lower() != "send_message":
            continue
        if any(a.name.strip() == "channel" for a in step.arguments):
            continue
        step.arguments.append(StepArgument(name="channel", value=aliases[0]))
        notes.append(f"Step {step.step_number} (teams.send_message): added channel={aliases[0]}, the only approved "
                     "Teams channel.")
    return draft.model_copy(update={"steps": steps}), notes


# parent_step: which earlier step may contain this one, and which argument it replaces.
#   notion        child page inside a page created by an earlier notion.create_page step
#   google_drive  file/folder/listing inside a folder created by an earlier create_folder step
_PARENT_RULES: dict[str, tuple[Action, str]] = {
    "notion": (Action.CREATE_PAGE, "parent"),
    "google_drive": (Action.CREATE_FOLDER, "folder"),
}


def _parent_step_issues(steps: list[WorkflowStep]) -> list[ValidationIssue]:
    """parent_step must name an EARLIER step of the same integration that creates a container
    (notion.create_page / google_drive.create_folder), this step must depend on it, and it
    excludes the alias argument ('parent' / 'folder'). The item is then created inside a
    container this same run created, so everything stays under an approved destination."""
    issues: list[ValidationIssue] = []
    by_number = {s.step_number: s for s in steps}
    for step in steps:
        raw = step.arguments.get("parent_step")
        if raw is None:
            continue
        field = "arguments.parent_step"
        n = step.step_number
        container_action, alias_arg = _PARENT_RULES[step.integration.value]
        if alias_arg in step.arguments:
            issues.append(ValidationIssue(code="CONFLICTING_ARGUMENTS", step_number=n, field=field,
                                          message=f"Step {n} gives both '{alias_arg}' and 'parent_step'; use one."))
            continue
        target = by_number.get(int(raw)) if raw.strip().isdigit() else None
        if target is None or target.step_number >= n or (target.integration, target.action) != (
                step.integration, container_action):
            issues.append(ValidationIssue(code="INVALID_PARENT_STEP", step_number=n, field=field,
                                          message=f"Step {n} parent_step must be the number of an earlier "
                                                  f"{step.integration.value} {container_action.value} step."))
        elif target.step_number not in step.depends_on:
            issues.append(ValidationIssue(code="INVALID_PARENT_STEP", step_number=n, field=field,
                                          message=f"Step {n} must depend on its parent_step {target.step_number}."))
    return issues


def _validate_arguments(raw_arguments, integration, action: Action, n: int):
    spec = ACTION_CATALOG[integration][action]
    issues: list[ValidationIssue] = []
    arguments: dict[str, str] = {}

    for arg in raw_arguments:
        name = arg.name.strip()
        if name in arguments:
            issues.append(_arg_issue("DUPLICATE_ARGUMENT", f"Argument '{name}' is given more than once.", n, name))
            continue
        if name not in spec.allowed:
            issues.append(
                _arg_issue(
                    "UNKNOWN_ARGUMENT",
                    f"Argument '{name}' is not accepted by {integration.value}.{action.value}. "
                    f"Allowed: {', '.join(sorted(spec.allowed))}.",
                    n,
                    name,
                )
            )
            continue
        if len(arg.value) > MAX_ARGUMENT_LENGTH:
            issues.append(
                _arg_issue("ARGUMENT_TOO_LONG", f"Argument '{name}' exceeds {MAX_ARGUMENT_LENGTH} characters.", n, name)
            )
        unsafe = find_unsafe_content(arg.value)
        if unsafe:
            issues.append(_unsafe(n, f"arguments.{name}", unsafe))
        arguments[name] = arg.value

    for name in sorted(spec.required):
        if not arguments.get(name, "").strip():
            issues.append(
                _arg_issue(
                    "MISSING_ARGUMENT",
                    f"Required argument '{name}' is missing or empty for {integration.value}.{action.value}.",
                    n,
                    name,
                )
            )
    return arguments, issues


def _arg_issue(code: str, message: str, n: int, name: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, step_number=n, field=f"arguments.{name}")


def _unsafe(n: int, field: str, fragment: str) -> ValidationIssue:
    return ValidationIssue(
        code="UNSAFE_CONTENT",
        message=f"Step {n} {field} contains disallowed code/shell content ('{fragment[:40]}').",
        step_number=n,
        field=field,
    )


def document_availability_issues(plan: WorkflowPlan, available_documents: Collection[str]) -> list[ValidationIssue]:
    """Mock-mode check: every google_drive.read_document must name a document that will
    exist when the step runs, i.e. one in ``available_documents`` (the synthetic catalog) or
    the title of a create_document step EARLIER in this plan. Names are matched exactly,
    exactly as the mock Drive adapter matches them.

    The adapter's runtime DOCUMENT_NOT_FOUND check stays authoritative; this only moves the
    failure for a deterministic mock catalog to before any side effect happens.
    """
    created: set[str] = set()
    issues: list[ValidationIssue] = []
    for step in plan.steps:  # steps run in step_number order
        if step.integration.value != "google_drive":
            continue
        if step.action is Action.CREATE_DOCUMENT:
            created.add(step.arguments.get("title", ""))
        elif step.action is Action.READ_DOCUMENT:
            if step.arguments.get("file_id"):
                continue  # Phase 7.2: bound to a file id; the adapter checks id, name and content hash
            name = step.arguments.get("document_name", "")
            if name in available_documents or name in created:
                continue
            canonical = canonical_document_name(name)
            hint = (
                f" Did you mean '{canonical}'? Names must match exactly."
                if canonical
                else f" Available synthetic documents: {', '.join(sorted(available_documents))}."
            )
            issues.append(
                ValidationIssue(
                    code="DOCUMENT_NOT_FOUND",
                    message=f"Step {step.step_number} reads '{name[:120]}', which is not in the synthetic Google Drive "
                    f"catalog and is not created by an earlier step.{hint}",
                    step_number=step.step_number,
                    field="arguments.document_name",
                )
            )
    return issues


def integration_target_issues(steps, allowed_integrations) -> list[ValidationIssue]:
    """Every step must use an integration of the selected target (server-side allowlist).
    ``steps`` are WorkflowSteps or dicts; ``allowed_integrations`` None = unrestricted."""
    if allowed_integrations is None:
        return []
    issues: list[ValidationIssue] = []
    allowed = ", ".join(sorted(allowed_integrations))
    for step in steps:
        integration = step["integration"] if isinstance(step, dict) else step.integration.value
        number = step["step_number"] if isinstance(step, dict) else step.step_number
        if integration not in allowed_integrations:
            issues.append(ValidationIssue(
                code="INTEGRATION_NOT_ALLOWED",
                message=f"Step {number} uses {integration}, which is outside the selected integration target "
                        f"(allowed: {allowed}).",
                step_number=number, field="integration"))
    return issues


def drive_destination_issues(steps, allowed_folders) -> list[ValidationIssue]:
    """Google Drive destination allowlist for dry-run/real custom plans. Every google_drive step
    must either name an approved folder ALIAS (``folder``) or work inside a folder created by an
    earlier create_folder step of the same plan (``parent_step``; validate_draft checks that
    reference). Raw folder ids, URLs, other aliases and missing destinations are rejected.
    ``allowed_folders`` None = no restriction (mock mode and legacy requests)."""
    if allowed_folders is None:
        return []
    allowed = {a.lower() for a in allowed_folders}
    names = ", ".join(sorted(allowed)) or "(none configured)"
    issues: list[ValidationIssue] = []
    for step in steps:
        integration = step["integration"] if isinstance(step, dict) else step.integration.value
        if integration != "google_drive":
            continue
        number = step["step_number"] if isinstance(step, dict) else step.step_number
        arguments = step["arguments"] if isinstance(step, dict) else step.arguments
        if arguments.get("parent_step", "").strip():
            continue
        folder = arguments.get("folder", "").strip().lower()
        if not folder:
            issues.append(ValidationIssue(
                code="DESTINATION_REQUIRED", step_number=number, field="arguments.folder",
                message=f"Step {number} has no Google Drive destination; use folder={names} or parent_step."))
        elif folder not in allowed:
            issues.append(ValidationIssue(
                code="DESTINATION_NOT_APPROVED", step_number=number, field="arguments.folder",
                message=f"Step {number} folder '{folder[:40]}' is not an approved destination (allowed: {names}, "
                        "or parent_step of a folder created by this plan)."))
    return issues


def normalize_drive_references(draft: DraftPlan, allowed_folders) -> tuple[DraftPlan, list[str]]:
    """Fill a MISSING Google Drive destination only when exactly one value is possible.

    Runs before validate_draft on dry-run/real Drive plans. It never changes a destination the
    planner gave, never invents a folder id, and the result still goes through the full
    validator and the destination allowlist. Each change is returned as a sentence for the
    reviewer (and Langfuse). The rules, per step without ``folder`` and without ``parent_step``:

      create_folder    -> folder=<alias> if exactly one approved alias exists and the plan has no
                          earlier create_folder (otherwise top-level vs sub-folder is ambiguous)
      create_document  -> parent_step=<n> if exactly one earlier create_folder exists
      list_files       -> parent_step=<n> if exactly one earlier create_folder exists
      read_document    -> the destination of the ONE earlier create_document whose title equals
                          document_name exactly (the existing "same title, same folder" contract)
    parent_step references are also added to depends_on, as the validator requires.
    """
    if allowed_folders is None:
        return draft, []
    aliases = sorted({a.lower() for a in allowed_folders})
    steps = [s.model_copy(deep=True) for s in draft.steps]
    notes: list[str] = []

    def is_drive(step) -> bool:
        integration = normalize_integration(step.integration)
        return integration is not None and integration.value == "google_drive"

    def args(step) -> dict[str, str]:
        return {a.name.strip(): a.value for a in step.arguments}

    def has_destination(step) -> bool:
        a = args(step)
        return bool(a.get("folder", "").strip() or a.get("parent_step", "").strip())

    def set_arg(step, name: str, value: str) -> None:
        from app.schemas.plan import StepArgument

        step.arguments.append(StepArgument(name=name, value=value))

    def depend(step, *numbers: int) -> None:
        step.depends_on = sorted(set(step.depends_on) | set(numbers))

    for index, step in enumerate(steps):
        if not is_drive(step) or has_destination(step):
            continue
        action = step.action.strip().lower()
        earlier = [s for s in steps[:index] if is_drive(s)]
        folders = [s for s in earlier if s.action.strip().lower() == Action.CREATE_FOLDER.value]
        n = step.step_number
        if action == Action.CREATE_FOLDER.value:
            if len(aliases) == 1 and not folders:
                set_arg(step, "folder", aliases[0])
                notes.append(f"Step {n} (create_folder): added folder={aliases[0]}, the only approved Drive folder.")
        elif action in (Action.CREATE_DOCUMENT.value, Action.LIST_FILES.value):
            if len(folders) == 1:
                parent = folders[0].step_number
                set_arg(step, "parent_step", str(parent))
                depend(step, parent)
                notes.append(f"Step {n} ({action}): added parent_step={parent}, the only folder created earlier "
                             "in this plan.")
        elif action == Action.READ_DOCUMENT.value:
            name = args(step).get("document_name", "")
            sources = [s for s in earlier if s.action.strip().lower() == Action.CREATE_DOCUMENT.value
                       and args(s).get("title", "") == name and name]
            if len(sources) != 1 or not has_destination(sources[0]):
                continue
            source_args = args(sources[0])
            parent = source_args.get("parent_step", "").strip()
            if parent:
                set_arg(step, "parent_step", parent)
                depend(step, *([int(parent)] if parent.isdigit() else []), sources[0].step_number)
                where = f"parent_step={parent}"
            else:
                set_arg(step, "folder", source_args["folder"].strip())
                depend(step, sources[0].step_number)
                where = f"folder={source_args['folder'].strip()}"
            notes.append(f"Step {n} (read_document): added {where}, the folder of step {sources[0].step_number} "
                         f"which creates '{name[:60]}'.")
    return draft.model_copy(update={"steps": steps}), notes


# ---------------- Notion: safe normalization of known LLM mistakes ----------------

# Argument names LLMs use for the parent of a Notion page that the catalog does not accept.
#   parent_page        step reference, exact title of one earlier page, or approved alias
#   parent_page_title  EXACT title of one earlier page only (never a step number or an alias)
NOTION_PARENT_ALIASES = ("parent_page", "parent_page_title")
_TITLE_ONLY_PARENT_ARGS = frozenset({"parent_page_title"})

# A step for a pseudo-integration that only "says" something (no connector exists for it).
# Removed only when it is a pure text summary / completion message: a summary-type action, text-only
# arguments, no unsafe content, and no later step depends on it. Anything else (send/post/notify,
# a recipient/channel/url argument, or a step something depends on) stays and is rejected as
# UNSUPPORTED_INTEGRATION.
NARRATION_INTEGRATIONS = frozenset({"assistant"})
NARRATION_ACTIONS = frozenset({
    "summarize", "summary", "generate_summary", "create_summary", "write_summary", "summarize_plan",
    "summarize_results", "final_summary", "provide_summary", "respond", "reply", "complete", "completion",
    "completion_message", "final_message", "finish", "done", "report_completion",
})
NARRATION_ARGUMENTS = frozenset({"message", "text", "summary", "content", "title", "note", "notes", "response"})
# Read-back actions LLMs invent for Notion. There is NO supported Notion read action (the catalog
# has create_page / create_task only, and the adapters have no read tool), so such a step is
# removed - only when it does nothing but read back pages created earlier in the same plan.
NOTION_READ_BACK_ACTIONS = frozenset({
    "read_page", "get_page", "retrieve_page", "fetch_page", "verify_page", "check_page", "read_pages",
    "verify_pages", "read_back_page",
})
# An explicit step reference: "1", "step 1", "step_1", "Step #1", "{{step_1}}", "$step1", "step_1.id".
_STEP_REF = re.compile(
    r"^\{?\{?\s*\$?(?:steps?)?[\s._#-]*(\d{1,3})(?:[._](?:id|page_id|output|result|url))?\s*\}?\}?$",
    re.IGNORECASE,
)


def _step_ref(value: str) -> int | None:
    match = _STEP_REF.fullmatch(value.strip())
    return int(match.group(1)) if match else None


def normalize_notion_references(draft: DraftPlan, approved_parents: Collection[str] | None
                                ) -> tuple[DraftPlan, list[str]]:
    """Repair known planner mistakes, only when the intent is unambiguous.

    1. ``notion.create_page`` with ``parent_page_title`` (not in the catalog), neither ``parent``
       nor ``parent_step``, whose value is EXACTLY the title of exactly one EARLIER
       notion.create_page step -> parent_step=<n>. Never converted to an alias.
       ``notion.create_page`` with ``parent_page`` (not in the catalog) and neither ``parent`` nor
       ``parent_step``:
         - an explicit step reference ("1", "step 1", "{{step_1}}") to an EARLIER notion.create_page
           step, or the exact title of exactly ONE earlier create_page step  -> parent_step=<n>
           (and <n> is added to depends_on);
         - exactly an approved Notion parent alias (``approved_parents``)    -> parent=<alias>.
       Anything else (ids, URLs, unknown names, a value that matches both a title and an alias, or
       a step that also gives parent/parent_step) is left unchanged, so the strict validator still
       rejects it as UNKNOWN_ARGUMENT. No parent is ever guessed and no alias is invented.
    2. A Notion read-back step (``read_page`` etc.) that only references pages created earlier in
       this plan (via depends_on, a step reference or an exact title) and that no other step
       depends on is REMOVED, later steps are renumbered and the reviewer is told that read-back
       verification was NOT performed. Any other unsupported action is left for the validator.
    3. An ``assistant`` step that is only a text summary / completion message (summary-type
       action, text-only arguments, no unsafe content, no later step depends on it) is REMOVED and
       reported. It may itself depend on earlier steps (that is only ordering). Any other
       assistant step stays and is rejected as UNSUPPORTED_INTEGRATION.

    Runs before validate_draft; the result is still fully validated (allow-list, parent_step
    rules, integration target) and every change is returned as a sentence for the plan preview.
    Plans whose steps are not numbered 1..N are returned unchanged (the validator rejects them).
    """
    from app.schemas.plan import StepArgument

    steps = [s.model_copy(deep=True) for s in draft.steps]
    if [s.step_number for s in steps] != list(range(1, len(steps) + 1)):
        return draft, []
    approved = {a.strip().lower() for a in approved_parents} if approved_parents is not None else set()
    notes: list[str] = []

    def is_notion(step) -> bool:
        integration = normalize_integration(step.integration)
        return integration is not None and integration.value == "notion"

    def is_page_creation(step) -> bool:
        return is_notion(step) and step.action.strip().lower() == Action.CREATE_PAGE.value

    def title_of(step) -> str:
        return next((a.value.strip() for a in step.arguments if a.name.strip() == "title"), "")

    # ---- 1. parent_page -> parent_step / parent ----
    for index, step in enumerate(steps):
        if not is_page_creation(step):
            continue
        names = [a.name.strip() for a in step.arguments]
        found = [name for name in names if name in NOTION_PARENT_ALIASES]
        if len(found) != 1 or names.count(found[0]) != 1 or "parent" in names or "parent_step" in names:
            continue
        position = names.index(found[0])
        raw = step.arguments[position].value.strip()
        earlier_pages = [s for s in steps[:index] if is_page_creation(s)]
        n = step.step_number
        target = None
        if found[0] in _TITLE_ONLY_PARENT_ARGS:
            # Exact (whitespace-trimmed) title of exactly ONE earlier page; nothing else.
            exact = [s for s in earlier_pages if raw and title_of(s) == raw]
            if len(exact) != 1:
                continue  # missing, unknown, future-only or ambiguous title: left for the validator
            target = exact[0]
            ref = by_title = None
        else:
            ref = _step_ref(raw)
            by_title = [s for s in earlier_pages if raw and title_of(s).lower() == raw.lower()]
        if target is not None:
            pass
        elif ref is not None:
            target = next((s for s in earlier_pages if s.step_number == ref), None)
        elif by_title and raw.lower() in approved:
            continue  # both a page title and an approved alias: ambiguous, never guessed
        elif len(by_title) == 1:
            target = by_title[0]
        if target is not None:
            step.arguments[position] = StepArgument(name="parent_step", value=str(target.step_number))
            step.depends_on = sorted(set(step.depends_on) | {target.step_number})
            notes.append(f"Step {n} (notion.create_page): replaced unsupported argument '{found[0]}' with "
                         f"parent_step={target.step_number}, the page created by step {target.step_number} "
                         f"('{title_of(target)[:60]}').")
        elif ref is None and not by_title and raw.lower() in approved:
            step.arguments[position] = StepArgument(name="parent", value=raw.lower())
            notes.append(f"Step {n} (notion.create_page): replaced unsupported argument '{found[0]}' with "
                         f"parent={raw.lower()}, an approved Notion parent page alias.")

    # ---- 2. remove read-back steps Notion cannot perform and pure assistant summaries ----
    removed: list[int] = []

    def used_later(index: int, n: int) -> bool:
        """Does any later step depend on / point at step n? Then removing it would change the plan."""
        return any(n in s.depends_on or any(a.name.strip() in ("parent_step", "parent_page")
                                            and _step_ref(a.value) == n for a in s.arguments)
                   for s in steps[index + 1:])

    for index, step in enumerate(steps):
        n = step.step_number
        if step.integration.strip().lower() in NARRATION_INTEGRATIONS:
            names = [a.name.strip().lower() for a in step.arguments]
            text = [step.description, *(a.value for a in step.arguments)]
            if (step.action.strip().lower() in NARRATION_ACTIONS and set(names) <= NARRATION_ARGUMENTS
                    and not any(find_unsafe_content(t) for t in text) and not used_later(index, n)):
                removed.append(n)
                notes.append(f"Step {n} ({step.integration.strip()}.{step.action.strip()}): removed. It only "
                             "produced a text summary/completion message, has no external effect and no step "
                             "depends on it; 'assistant' is not an integration. The plan summary above "
                             "describes the workflow.")
            continue  # anything else stays and is rejected as UNSUPPORTED_INTEGRATION
        if not is_notion(step) or step.action.strip().lower() not in NOTION_READ_BACK_ACTIONS:
            continue
        created = {s.step_number: s for s in steps[:index] if is_page_creation(s)}
        titles = {title_of(s).lower(): s.step_number for s in created.values() if title_of(s)}
        referenced: set[int] = set(step.depends_on)
        unresolved = False
        for arg in step.arguments:
            value = arg.value.strip()
            if (ref := _step_ref(value)) is not None:
                referenced.add(ref)
            elif value.lower() in titles:
                referenced.add(titles[value.lower()])
            elif arg.name.strip() in ("page", "page_title", "title", "page_name", "page_id", "parent_step"):
                unresolved = True  # names a page this plan does not create: not a read-back
        if not referenced or unresolved or used_later(index, n) or not referenced <= created.keys():
            continue
        removed.append(n)
        pages = ", ".join(str(r) for r in sorted(referenced))
        notes.append(f"Step {n} (notion.{step.action.strip()}): removed. Notion has no supported read action, "
                     f"so read-back verification of the page(s) created by step(s) {pages} was NOT performed; "
                     "the execution report lists each page created.")

    if removed:
        kept = [s for s in steps if s.step_number not in removed]
        mapping = {s.step_number: new for new, s in enumerate(kept, start=1)}
        for s in kept:
            s.step_number = mapping[s.step_number]
            s.depends_on = sorted({mapping[d] for d in s.depends_on if d in mapping}
                                  | {d for d in s.depends_on if d not in mapping and d not in removed})
            for i, arg in enumerate(s.arguments):
                if arg.name.strip() == "parent_step" and arg.value.strip().isdigit() \
                        and int(arg.value.strip()) in mapping:
                    s.arguments[i] = StepArgument(name=arg.name, value=str(mapping[int(arg.value.strip())]))
        moved = [(old, new) for old, new in mapping.items() if old != new]
        if moved:
            notes.append("Later steps were renumbered after the removal: "
                         + ", ".join(f"{old}->{new}" for old, new in moved) + ".")
        steps = kept
    return draft.model_copy(update={"steps": steps}), notes
