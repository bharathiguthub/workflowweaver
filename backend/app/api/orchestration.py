"""Phase 7.2 multi-integration workflow: [Google Drive source] -> AI -> Notion -> Teams.

GET  /api/v1/workflows/orchestration/sources?mode=mock|dry_run|real
    Candidate source documents in the approved Drive sandbox (aliases and file ids only).
    Mock and dry run list synthetic fixtures (``simulated: true``); real lists the sandbox
    folder through the Drive REST client (drive.file). Read-only.
POST /api/v1/workflows/orchestration/plan
    body {"source_file_id": str?, "instruction": str?, "mode": ..., "expected_phases": 5}
    At least one of source_file_id / instruction. Input mode:
      document_only               source_file_id only (the original Phase 7.2 workflow)
      instructions_only           instruction only: Groq/Gemini generate the plan, no Drive step
      document_and_instructions   both: the document is the factual source, the instructions say
                                  how to process it
    200 PLANNED   validated plan + extraction + checks; NOTHING is written
    422 INVALID   extraction/grounding/plan rules failed (validation.issues); or (detail) invalid
                  instructions, a source problem or an invalid body
    502 FAILED    every AI provider failed
    404           source not found / not accessible
    409           mode above the server's INTEGRATION_MODE
    503           execution disabled, a sandbox not ready for the mode (detail names the
                  setting), or real mode without an AI planner

Instructions never change the compiled plan's structure, integrations or destinations (the
server compiles them); requests outside the workflow are reported in ``instruction_notes``.
The plan is recorded with its digest, which includes the input digest (mode, source id and
content hash, instructions, phase count); execution goes through the normal approval-gated
/executions endpoint, which requires the approval to carry that digest.
"""

import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.execution.modes import allowed_modes
from app.agents.execution.retry import RetryPolicy
from app.agents.orchestration import (
    AI_EXTRACTORS,
    DEFAULT_INSTRUCTION,
    DEFAULT_PHASES,
    INSTRUCTIONS_MAX,
    MAX_PHASES,
    SECTIONS,
    WORKFLOW_NAME,
    ClaudeExtractor,
    Destinations,
    ExtractionReview,
    ExtractionSource,
    GeminiExtractor,
    GroqExtractor,
    InputMode,
    MockExtractor,
    OrchestrationPlanner,
    SourceRef,
    input_digest,
    input_mode_of,
    scope_notes,
    validate_instructions,
)
from app.agents.orchestration_sources import (
    SourceError,
    SourceRead,
    duplicate_names,
    list_drive_sources,
    list_fixture_sources,
    read_drive_source,
    read_fixture_source,
    sandbox_folder_id,
)
from app.agents.plan_registry import get_plan_registry
from app.agents.planners.base import FallbackPlanner, PlannerConfigError
from app.agents.planners.factory import (
    ai_planner_problem,
    build_auto_chain,
    create_anthropic_client,
    create_gemini_client,
    create_groq_client,
)
from app.agents.service import PlanningService
from app.agents.validation import plan_digest
from app.core.config import IntegrationMode, PlannerMode, PlannerProvider, Settings, get_settings
from app.integrations.config import IntegrationConfig, load_integration_config
from app.integrations.errors import IntegrationConfigError
from app.integrations.readiness import (
    drive_sandbox_issues,
    real_drive_issues,
    real_sandbox_issues,
    real_teams_issues,
    sandbox_parent_issues,
    teams_sandbox_issues,
)
from app.observability import get_telemetry
from app.schemas.execution import ExecutionMode
from app.schemas.plan import IntegrationTarget, PlanResponse, PlanStatus, ValidationResult

router = APIRouter(prefix="/api/v1/workflows/orchestration", tags=["orchestration"])

TARGET = IntegrationTarget.DRIVE_NOTION_TEAMS
_HTTP_STATUS = {PlanStatus.PLANNED: 200, PlanStatus.INVALID: 422, PlanStatus.FAILED: 502}


# ---------- readiness ----------


def orchestration_issues_for(settings: Settings, config: IntegrationConfig | None, mode: IntegrationMode,
                             *, needs_drive: bool = True) -> list[str]:
    """What blocks the workflow in ``mode`` (setting names only). Real mode needs every real gate
    of Notion and Teams (and Google Drive when a source document is used); dry run needs their
    sandbox aliases; mock needs nothing."""
    if mode is IntegrationMode.MOCK:
        return []
    if config is None:
        return ["Integration configuration is invalid."]
    if mode is IntegrationMode.DRY_RUN:
        return [*sandbox_parent_issues(settings, config),
                *(drive_sandbox_issues(settings, config) if needs_drive else []),
                *teams_sandbox_issues(settings, config)]
    issues = [*real_sandbox_issues(settings, config), *(real_drive_issues(settings, config) if needs_drive else []),
              *real_teams_issues(settings, config)]
    return list(dict.fromkeys(issues))  # shared gates (kill switch, token) once


def _mode(settings: Settings, requested: str) -> IntegrationMode:
    mode = IntegrationMode(requested)
    if mode not in allowed_modes(settings.integration_mode):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This server allows modes up to '{settings.integration_mode.value}'.")
    if not settings.mock_execution_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Execution is disabled (MOCK_EXECUTION_ENABLED=false).")
    return mode


def _config(settings: Settings, mode: IntegrationMode, *, needs_drive: bool = True) -> IntegrationConfig | None:
    if mode is IntegrationMode.MOCK:
        return None
    try:
        config = load_integration_config(settings)
    except IntegrationConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    issues = orchestration_issues_for(settings, config, mode, needs_drive=needs_drive)
    if issues:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, " ".join(issues))
    return config


def _source_error(exc: SourceError) -> HTTPException:
    return HTTPException(exc.http_status, {"code": exc.code, "message": exc.message})


# ---------- dependencies (tests override them) ----------


def get_orchestration_drive_http() -> Any:
    """None: a real httpx.Client for the Drive REST client. Tests inject httpx.MockTransport."""
    return None


def get_orchestration_sleeper():
    return time.sleep


def extraction_source(settings: Settings, provider: PlannerProvider) -> ExtractionSource:
    if provider is PlannerProvider.GROQ:
        assert settings.groq_model
        return GroqExtractor(client=create_groq_client(settings), model=settings.groq_model,
                             max_tokens=settings.groq_max_tokens)
    if provider is PlannerProvider.GEMINI:
        assert settings.gemini_model
        return GeminiExtractor(client=create_gemini_client(settings), model=settings.gemini_model,
                               max_output_tokens=settings.gemini_max_output_tokens)
    assert settings.anthropic_model
    return ClaudeExtractor(client=create_anthropic_client(settings), model=settings.anthropic_model,
                           max_tokens=settings.anthropic_max_tokens)


def default_sources(settings: Settings, mode: IntegrationMode) -> list[ExtractionSource]:
    """Mock mode: the deterministic parser (no network). Dry run/real: the configured AI chain
    (auto = primary with automatic fallback on provider outages). Real never uses the parser."""
    if mode is IntegrationMode.MOCK:
        return [MockExtractor()]
    try:
        if settings.planner_mode is PlannerMode.AUTO:
            return build_auto_chain(settings, extraction_source)
        if settings.planner_mode is PlannerMode.GEMINI:
            return [extraction_source(settings, PlannerProvider.GEMINI)]
        if settings.planner_mode is PlannerMode.CLAUDE:
            return [extraction_source(settings, PlannerProvider.CLAUDE)]
    except PlannerConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if mode is IntegrationMode.REAL:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Real-mode orchestration plans must come from an AI planner (Groq with Gemini fallback, or "
                            f"Claude). {ai_planner_problem(settings) or 'PLANNER_MODE=mock is not allowed.'}")
    return [MockExtractor()]


def get_extraction_sources_factory() -> Callable[[Settings, IntegrationMode], list[ExtractionSource]]:
    return default_sources


# ---------- sources ----------


class SourceOut(BaseModel):
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None
    duplicate_name: bool  # another file has the same name: pick by id, never by name


class SourcesResponse(BaseModel):
    mode: ExecutionMode
    simulated: bool  # true: synthetic fixtures (mock / dry run never contact Google Drive)
    folder_alias: str | None
    sources: list[SourceOut]


def _drive_folder(settings: Settings, config: IntegrationConfig, http: Any):
    from app.agents.execution.tools.integrations import _drive_client

    alias = settings.smoke_test_drive_folder
    assert alias
    client = _drive_client(config, http)
    return client, config.drive_folders.targets[alias]


@router.get("/sources", response_model=SourcesResponse)
def list_sources(mode: Literal["mock", "dry_run", "real"] = Query(...),
                 http: Any = Depends(get_orchestration_drive_http)) -> SourcesResponse:
    settings = get_settings()
    selected = _mode(settings, mode)
    config = _config(settings, selected)
    if selected is not IntegrationMode.REAL:
        sources = list_fixture_sources()
        alias = settings.smoke_test_drive_folder if selected is IntegrationMode.DRY_RUN else "sandbox"
    else:
        assert config is not None
        client, target = _drive_folder(settings, config, http)
        try:
            sources = list_drive_sources(client, sandbox_folder_id(client, target))
        except SourceError as exc:
            raise _source_error(exc) from exc
        finally:
            client.close()
        alias = settings.smoke_test_drive_folder
    dupes = duplicate_names(sources)
    return SourcesResponse(mode=selected.value, simulated=selected is not IntegrationMode.REAL, folder_alias=alias,
                           sources=[SourceOut(**s.__dict__, duplicate_name=s.name in dupes) for s in sources])


# ---------- plan ----------


class OrchestrationPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_file_id: str | None = Field(default=None, min_length=10, max_length=128,
                                       description="Selected by id: names may repeat. Omit for instructions only.")
    mode: ExecutionMode
    expected_phases: int = Field(default=DEFAULT_PHASES, ge=1, le=MAX_PHASES)
    instruction: str | None = Field(
        default=None, max_length=INSTRUCTIONS_MAX,
        description="Workflow instructions (untrusted). With a source: how to process the document. Without: what "
                    "to plan. They can never change destinations, integrations or approval rules.")

    @model_validator(mode="after")
    def _one_input(self) -> "OrchestrationPlanRequest":
        if self.instruction is not None and not self.instruction.strip():
            self.instruction = None
        if self.source_file_id is None and self.instruction is None:
            raise ValueError("Provide a source document (source_file_id), workflow instructions (instruction), or both.")
        return self


class SourceSummary(BaseModel):
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None
    sha256: str
    simulated: bool


ItemOrigin = Literal["document", "instructions", "unverified", "generated"]


class ExtractedItem(BaseModel):
    text: str
    in_source: bool  # true only for items found in the source DOCUMENT
    # document | instructions (from your instructions) | unverified (not found in the document;
    # marked for review) | generated (AI-generated, instructions-only plans)
    origin: ItemOrigin


SummarySourceOut = Literal["document", "source_text", "generated", "none"]


class PhaseOut(BaseModel):
    title: str
    summary: str = ""
    # document: AI summary verified against the source; source_text: copied from the source by the
    # server (the AI's was empty or not found in the source); generated: instructions only.
    summary_source: SummarySourceOut = "none"
    sections: dict[str, list[ExtractedItem]]
    priority: str
    priority_source: Literal["document", "instructions", "suggested", "none"]
    owner: str
    owner_source: Literal["document", "instructions", "suggested", "none"]


class ExtractionOut(BaseModel):
    project_title: str
    summary: str
    summary_source: SummarySourceOut = "document"
    phases: list[PhaseOut]


class Stage(BaseModel):
    stage: Literal["instructions_check", "source_validation", "document_read", "extraction", "plan_validation"]
    status: Literal["passed", "failed", "simulated", "skipped"]
    detail: str


class Checks(BaseModel):
    performed: list[str]
    simulated: list[str]


class OrchestrationDestinations(BaseModel):
    drive_folder: str | None  # None: no source document (instructions only)
    notion_parent: str | None
    teams_channel: str | None


class WriteSummary(BaseModel):
    notion_pages: int
    teams_messages: int
    drive_writes: int = 0  # the source is only read, never modified


class OrchestrationPlanResponse(PlanResponse):
    mode: ExecutionMode
    workflow_name: str
    input_mode: InputMode
    model: str | None
    ai_generated: bool
    plan_digest: str | None  # approval must carry exactly this value
    input_digest: str  # SHA-256 of mode + source id/content hash + instructions + phase count
    source: SourceSummary | None  # None: instructions only
    instructions: str | None  # the instructions as received (the user's own text)
    instruction_notes: list[str]  # parts of the instructions that were not applied
    extraction: ExtractionOut | None
    warnings: list[str]
    stages: list[Stage]
    checks: Checks
    destinations: OrchestrationDestinations
    write_summary: WriteSummary
    real_write: bool


def _extraction_out(review: ExtractionReview | None) -> ExtractionOut | None:
    if review is None:
        return None
    return ExtractionOut(project_title=review.project_title, summary=review.summary,
                         summary_source=review.summary_source, phases=[
        PhaseOut(title=p.title, summary=p.summary, summary_source=p.summary_source,
                 sections={key: [ExtractedItem(text=t, in_source=origin == "document", origin=origin)
                                 for t, origin in p.items.get(key, [])]
                           for key, _ in SECTIONS},
                 priority=p.priority, priority_source=p.priority_source, owner=p.owner, owner_source=p.owner_source)
        for p in review.phases])


def _checks(mode: IntegrationMode, read: SourceRead | None, provider: str, input_mode: InputMode) -> Checks:
    validation = "Plan validation: allow-list, integration target, Drive + Notion + Teams rules, destinations"
    instructions = (["Instructions checked: no code, shell, HTML or control characters; requests outside the "
                     "workflow are listed and not applied"] if input_mode != "document_only" else [])
    generated = "Plan generated" if input_mode == "instructions_only" else "Extraction"
    if mode is IntegrationMode.MOCK:
        return Checks(performed=[*instructions, validation, "Plan digest (incl. inputs) recorded for approval binding"],
                      simulated=[*(["Google Drive read (synthetic source fixture)"] if read else []),
                                 f"{generated} (deterministic mock, not AI)",
                                 "Notion page creation and read-back (in memory)", "Teams notification (in memory)"])
    if mode is IntegrationMode.DRY_RUN:
        aliases = "Notion sandbox parent" + (", Drive sandbox folder" if read else "") + " and Teams channel aliases"
        return Checks(performed=[f"Configuration: {aliases}", *instructions, f"AI {generated.lower()} ({provider})",
                                 validation, "Plan digest (incl. inputs) recorded for approval binding",
                                 "At execution: every Notion and Teams request is built and checked, not sent"],
                      simulated=[*(["Google Drive read (synthetic source fixture: dry run never contacts Google Drive)"]
                                   if read else []),
                                 "Notion page creation and read-back (requests not sent)",
                                 "Teams notification (request not sent)"])
    return Checks(performed=[*(read.checks if read else []), *instructions, f"AI {generated.lower()} ({provider})",
                             validation, "Plan digest (incl. inputs) recorded for approval binding"], simulated=[])


@router.post("/plan", response_model=OrchestrationPlanResponse,
             responses={404: {"description": "Source not found"}, 409: {"description": "Mode above the ceiling"},
                        422: {"model": OrchestrationPlanResponse}, 502: {"model": OrchestrationPlanResponse},
                        503: {"description": "Not configured for this mode"}})
def plan_orchestration(
    payload: OrchestrationPlanRequest,
    http: Any = Depends(get_orchestration_drive_http),
    sources_factory: Callable[[Settings, IntegrationMode], list[ExtractionSource]] = Depends(
        get_extraction_sources_factory),
    sleeper=Depends(get_orchestration_sleeper),
) -> JSONResponse:
    settings = get_settings()
    has_document = payload.source_file_id is not None
    instructions = payload.instruction
    input_mode = input_mode_of(has_document, instructions)
    workflow_id = str(uuid4())
    telemetry = get_telemetry()

    # 0. instructions check (before any configuration, network or AI call)
    if instructions is not None:
        problems = validate_instructions(instructions)
        telemetry.event("orchestration.instructions_check", workflow_id=workflow_id,
                        level="WARNING" if problems else "DEFAULT",
                        metadata={"input_mode": input_mode, "chars": len(instructions), "status":
                                  "failed" if problems else "passed", "codes": [p.code for p in problems]})
        if problems:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                {"code": "INVALID_INSTRUCTIONS", "message": " ".join(p.message for p in problems)})
    notes = scope_notes(instructions or "")

    mode = _mode(settings, payload.mode)
    config = _config(settings, mode, needs_drive=has_document)
    sources = sources_factory(settings, mode)
    if mode is IntegrationMode.REAL and any(isinstance(s, MockExtractor) for s in sources):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Real-mode orchestration plans need an AI planner.")

    # 1-2. source validation + document read (read-only; document modes only)
    read: SourceRead | None = None
    if has_document:
        assert payload.source_file_id is not None
        try:
            if mode is IntegrationMode.REAL:
                assert config is not None
                client, target = _drive_folder(settings, config, http)
                try:
                    read = read_drive_source(client, sandbox_folder_id(client, target), payload.source_file_id)
                finally:
                    client.close()
            else:
                read = read_fixture_source(payload.source_file_id, mode)
        except SourceError as exc:
            telemetry.event("orchestration.source_validation", workflow_id=workflow_id, level="WARNING",
                            metadata={"mode": mode.value, "status": "failed", "code": exc.code})
            raise _source_error(exc) from exc
        telemetry.event("orchestration.source_validation", workflow_id=workflow_id,
                        metadata={"mode": mode.value, "status": "simulated" if read.simulated else "passed"})
        telemetry.event("orchestration.document_read", workflow_id=workflow_id,
                        metadata={"mode": mode.value, "size_bytes": len(read.text.encode("utf-8")),
                                  "simulated": read.simulated, "sha256_prefix": read.sha256[:12]})

    drive_alias = (settings.smoke_test_drive_folder if mode is not IntegrationMode.MOCK else "sandbox") \
        if has_document else None
    destinations = Destinations(
        notion_parent=settings.smoke_test_notion_parent if mode is not IntegrationMode.MOCK else None,
        teams_channel=settings.smoke_test_teams_channel if mode is not IntegrationMode.MOCK else None)
    ref = SourceRef(read.info.file_id, read.info.name, read.sha256, drive_alias or "sandbox") if read else None
    inputs = input_digest(input_mode, ref, instructions, payload.expected_phases)

    # 3-4. AI extraction / generation (auto: Groq -> Gemini) + validation, through the normal planning graph
    adapters = [OrchestrationPlanner(s, document_text=read.text if read else None, source_ref=ref,
                                     destinations=destinations, workflow_id=workflow_id,
                                     expected_phases=payload.expected_phases, instructions=instructions)
                for s in sources]
    chain = FallbackPlanner(adapters) if len(adapters) > 1 else adapters[0]
    service = PlanningService(chain, max_steps=settings.max_workflow_steps,
                              max_planning_attempts=settings.max_planning_attempts, available_documents=None,
                              backoff=RetryPolicy.for_planning(settings), sleeper=sleeper)
    restricted = mode is not IntegrationMode.MOCK
    drive_folders = [drive_alias] if restricted and drive_alias else None
    teams_channels = [destinations.teams_channel] if restricted and destinations.teams_channel else None
    result = service.plan(instructions or DEFAULT_INSTRUCTION, target=TARGET.value, workflow_id=workflow_id,
                          drive_folders=drive_folders, teams_channels=teams_channels)
    used = next((a for a in adapters if a.name == result.planner), adapters[0])
    review = used.review
    if result.status is PlanStatus.PLANNED and review is not None and review.issues:
        result = result.model_copy(update={"status": PlanStatus.INVALID, "planned_steps": [],
                                           "validation": ValidationResult(is_valid=False, issues=review.issues)})
    if review is not None:
        notes = list(dict.fromkeys([*notes, *(f"Not applied (reported by the AI): {n}"
                                             for n in review.ignored_instructions)]))
    telemetry.event("orchestration.extraction", workflow_id=workflow_id,
                    level="DEFAULT" if review is not None else "WARNING",
                    metadata={"provider": result.planner, "model": used.model, "fallback_used": result.fallback_used,
                              "input_mode": input_mode, "phase_count": len(review.phases) if review else 0,
                              "warning_count": len(review.warnings) if review else 0,
                              "instruction_note_count": len(notes), "attempts": result.planning_attempts})
    telemetry.event("orchestration.plan_validation", workflow_id=workflow_id,
                    level="DEFAULT" if result.status is PlanStatus.PLANNED else "WARNING",
                    metadata={"status": result.status.value, "input_mode": input_mode,
                              "issue_codes": [i.code for i in result.validation.issues][:20]})

    planned = result.status is PlanStatus.PLANNED
    digest = plan_digest(result.planned_steps, TARGET.value, inputs) if planned else None
    get_plan_registry().record(workflow_id, TARGET.value, drive_folders, teams_channels, digest, input_digest=inputs)
    stages: list[Stage] = []
    if instructions is not None:
        stages.append(Stage(stage="instructions_check", status="passed",
                            detail=f"{len(instructions)} characters; {len(notes)} request(s) not applied"))
    if read is not None:
        source_status = "simulated" if read.simulated else "passed"
        stages += [Stage(stage="source_validation", status=source_status, detail=read.checks[0]),
                   Stage(stage="document_read", status=source_status,
                         detail=f"{len(read.text.encode('utf-8'))} bytes, SHA-256 {read.sha256[:12]}")]
    else:
        stages += [Stage(stage="source_validation", status="skipped", detail="No source document (instructions only)"),
                   Stage(stage="document_read", status="skipped", detail="Google Drive is not used")]
    stages += [
        Stage(stage="extraction", status="passed" if review is not None else "failed",
              detail=(f"{result.planner}" + (f" ({used.model})" if used.model else "")
                      + (" after fallback" if result.fallback_used else "")
                      + (f": {len(review.phases)} phase(s)" if review else "")
                      + (" generated from your instructions" if review and not read else ""))),
        Stage(stage="plan_validation", status="passed" if planned else "failed",
              detail="Plan valid" if planned else f"{len(result.validation.issues)} issue(s)"),
    ]
    pages = sum(1 for s in result.planned_steps if s.integration.value == "notion")
    response = OrchestrationPlanResponse(
        **result.model_dump(),
        mode=mode.value, workflow_name=WORKFLOW_NAME, input_mode=input_mode, model=used.model,
        ai_generated=isinstance(sources[adapters.index(used)], AI_EXTRACTORS),
        plan_digest=digest, input_digest=inputs,
        source=SourceSummary(file_id=read.info.file_id, name=read.info.name, mime_type=read.info.mime_type,
                             size_bytes=read.info.size_bytes, modified_time=read.info.modified_time,
                             sha256=read.sha256, simulated=read.simulated) if read else None,
        instructions=instructions, instruction_notes=notes,
        extraction=_extraction_out(review),
        warnings=review.warnings if review else [],
        stages=stages,
        checks=_checks(mode, read, result.planner, input_mode),
        destinations=OrchestrationDestinations(drive_folder=drive_alias, notion_parent=destinations.notion_parent,
                                               teams_channel=destinations.teams_channel),
        write_summary=WriteSummary(notion_pages=pages,
                                   teams_messages=sum(1 for s in result.planned_steps if s.integration.value == "teams")),
        real_write=mode is IntegrationMode.REAL and planned,
    )
    return JSONResponse(status_code=_HTTP_STATUS[response.status], content=response.model_dump(mode="json"))
