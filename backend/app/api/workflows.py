"""Workflow API routes (Phase 1: planning only, no external tool execution)."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status

from app.schemas.workflow import (
    Integration,
    PlannedStep,
    WorkflowCreate,
    WorkflowResponse,
    WorkflowStatus,
    WorkflowTemplate,
)

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

TEMPLATES: list[WorkflowTemplate] = [
    WorkflowTemplate(
        id="meeting-to-tasks",
        name="Meeting-to-Tasks",
        description="Extract action items from meeting notes and create tasks.",
        steps=[
            "Fetch meeting notes",
            "Extract action items with owners and due dates",
            "Create tasks in Notion",
            "Notify attendees in Teams",
        ],
        integrations=[Integration.GOOGLE_DRIVE, Integration.NOTION, Integration.TEAMS],
        example_instruction=(
            "Read 'Synthetic standup notes' from Google Drive, create Notion tasks for each action item, "
            "save the meeting minutes to Google Drive, and notify attendees in Microsoft Teams."
        ),
    ),
    WorkflowTemplate(
        id="sprint-planning",
        name="Sprint Planning",
        description="Turn a backlog into a prioritised sprint plan.",
        steps=[
            "Load backlog items",
            "Estimate and prioritise items",
            "Draft sprint plan page in Notion",
            "Share sprint summary in Teams",
        ],
        integrations=[Integration.NOTION, Integration.TEAMS],
        example_instruction=(
            "Load the sprint backlog, draft the sprint plan page in Notion, "
            "and share the sprint summary in Microsoft Teams."
        ),
    ),
    WorkflowTemplate(
        id="incident-response",
        name="Incident Response",
        description="Coordinate triage, communication and post-mortem for an incident.",
        steps=[
            "Summarise incident report",
            "Classify severity and assign responders",
            "Alert on-call channel in Teams",
            "Create post-mortem document in Google Drive",
        ],
        integrations=[Integration.TEAMS, Integration.GOOGLE_DRIVE],
        example_instruction=(
            "Read 'Synthetic incident report' from Google Drive, alert the on-call channel in Microsoft Teams, "
            "and create a post-mortem document in Google Drive."
        ),
    ),
]
TEMPLATES_BY_ID = {t.id: t for t in TEMPLATES}

STEP_INTEGRATION_HINTS = {
    "notion": Integration.NOTION,
    "google drive": Integration.GOOGLE_DRIVE,
    "teams": Integration.TEAMS,
}


def _integration_for_step(action: str) -> Integration | None:
    lowered = action.lower()
    for hint, integration in STEP_INTEGRATION_HINTS.items():
        if hint in lowered:
            return integration
    return None


@router.get("/templates", response_model=list[WorkflowTemplate])
def list_templates() -> list[WorkflowTemplate]:
    return TEMPLATES


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
def create_workflow(payload: WorkflowCreate) -> WorkflowResponse:
    if payload.template_id is not None:
        template = TEMPLATES_BY_ID.get(payload.template_id)
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown template_id '{payload.template_id}'",
            )
        actions = template.steps
        integrations = payload.integrations or template.integrations
    else:
        actions = ["Analyse input", "Draft execution plan"]
        integrations = payload.integrations

    steps = [
        PlannedStep(order=i, action=action, integration=_integration_for_step(action))
        for i, action in enumerate(actions, start=1)
    ]

    return WorkflowResponse(
        id=uuid4(),
        name=payload.name,
        template_id=payload.template_id,
        status=WorkflowStatus.PLANNED,
        steps=steps,
        integrations=integrations,
        created_at=datetime.now(timezone.utc),
        message="Workflow planned. No external tools were executed (Phase 1).",
    )
