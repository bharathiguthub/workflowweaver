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

    if issues:
        return None, issues
    return WorkflowPlan(summary=draft.summary, steps=validated), []


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
