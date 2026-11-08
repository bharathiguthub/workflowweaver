"""Phase 8.2 Document Management API.

POST /api/v1/workflows/documents/plan
    body {"document_type", "title", "project_name"?, "instruction", "mode"}
    200 PLANNED   AI document draft + Markdown preview + a 2-step Drive plan; NOTHING is written
    422 INVALID   the draft broke a document rule or the plan validator (validation.issues);
                  the preview is still returned for review but the plan cannot be approved.
                  Also (detail) an invalid body.
    502 FAILED    every AI provider failed
    409           mode above the server's INTEGRATION_MODE
    503           execution disabled, the Drive sandbox not ready for the mode, or real mode
                  without an AI planner

GET  /api/v1/workflows/documents/library?mode=mock|dry_run|real
    Markdown / plain-text documents that are DIRECT children of the approved Drive sandbox
    folder and accessible to this app (``drive.file``: only files it created or was granted).
    This is NOT a listing of the user's whole Google Drive. Mock and dry run list synthetic
    fixtures and never contact Google Drive. Read-only.
GET  /api/v1/workflows/documents/library/{file_id}?mode=...
    Read-only preview of one of those documents (by file id; it must be inside the sandbox).

Planner: PLANNER_MODE=auto -> Groq primary, Gemini fallback (provider outages only); Claude or
Gemini alone when selected. PLANNER_MODE=mock serves a labelled non-AI stub for mock / dry-run
only. The destination is always the server's SMOKE_TEST_DRIVE_FOLDER alias (dry_run / real);
the request cannot choose another one. Execution uses the normal approval-gated /executions
endpoint; the approval is bound to the exact reviewed plan (plan_digest).
"""

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.documents import (
    AI_DOCUMENT_WRITERS,
    DOCUMENT_TYPES,
    INSTRUCTION_MAX,
    PROJECT_MAX,
    TITLE_MAX,
    ClaudeDocumentWriter,
    DocumentDraft,
    DocumentSource,
    DocumentType,
    DocumentWorkflowPlanner,
    GeminiDocumentWriter,
    GroqDocumentWriter,
    MockDocumentWriter,
    build_document_request,
    document_type_label,
)
from app.agents.execution.modes import allowed_modes
from app.agents.execution.retry import RetryPolicy
from app.agents.orchestration_sources import (
    SourceError,
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
from app.integrations.readiness import drive_sandbox_issues, drive_token_present, real_drive_issues
from app.observability import get_telemetry
from app.schemas.execution import ExecutionMode
from app.schemas.plan import IntegrationTarget, PlanResponse, PlanStatus, ValidationResult

router = APIRouter(prefix="/api/v1/workflows/documents", tags=["documents"])

TARGET = IntegrationTarget.GOOGLE_DRIVE
_HTTP_STATUS = {PlanStatus.PLANNED: 200, PlanStatus.INVALID: 422, PlanStatus.FAILED: 502}


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)


def _mode(settings: Settings, requested: str) -> IntegrationMode:
    mode = IntegrationMode(requested)
    if mode not in allowed_modes(settings.integration_mode):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This server allows modes up to '{settings.integration_mode.value}'.")
    if not settings.mock_execution_enabled:
        raise _unavailable("Execution is disabled (MOCK_EXECUTION_ENABLED=false).")
    return mode


def _config(settings: Settings) -> IntegrationConfig:
    try:
        return load_integration_config(settings)
    except IntegrationConfigError as exc:
        raise _unavailable(str(exc)) from exc


def read_issues(settings: Settings, config: IntegrationConfig) -> list[str]:
    """What blocks READING the real sandbox (no write gates: listing and preview never write)."""
    issues = drive_sandbox_issues(settings, config)
    if settings.integration_mode is not IntegrationMode.REAL:
        issues.append(f"INTEGRATION_MODE is '{settings.integration_mode.value}'; real mode needs INTEGRATION_MODE=real.")
    if config.drive_rest is None:
        issues.append("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set (Desktop app OAuth client).")
    elif not drive_token_present(config):
        issues.append("Google Drive is not authorized: run scripts/google_drive_auth.py (GOOGLE_DRIVE_TOKEN_FILE missing).")
    return issues


# ---------- dependencies (tests override them) ----------


def get_documents_drive_http() -> Any:
    """None: a real httpx.Client for the Drive REST client. Tests inject httpx.MockTransport."""
    return None


def get_documents_sleeper():
    return time.sleep


def document_source(settings: Settings, provider: PlannerProvider) -> DocumentSource:
    if provider is PlannerProvider.GROQ:
        assert settings.groq_model
        return GroqDocumentWriter(client=create_groq_client(settings), model=settings.groq_model,
                                  max_tokens=settings.groq_max_tokens)
    if provider is PlannerProvider.GEMINI:
        assert settings.gemini_model
        return GeminiDocumentWriter(client=create_gemini_client(settings), model=settings.gemini_model,
                                    max_output_tokens=settings.gemini_max_output_tokens)
    assert settings.anthropic_model
    return ClaudeDocumentWriter(client=create_anthropic_client(settings), model=settings.anthropic_model,
                                max_tokens=settings.anthropic_max_tokens)


def default_sources(settings: Settings, mode: IntegrationMode) -> list[DocumentSource]:
    """The configured AI chain (auto: Groq, then Gemini). The non-AI stub only for mock/dry run
    with PLANNER_MODE=mock; real mode always needs an AI planner."""
    try:
        if settings.planner_mode is PlannerMode.AUTO:
            return build_auto_chain(settings, document_source)
        if settings.planner_mode is PlannerMode.GEMINI:
            return [document_source(settings, PlannerProvider.GEMINI)]
        if settings.planner_mode is PlannerMode.CLAUDE:
            return [document_source(settings, PlannerProvider.CLAUDE)]
    except PlannerConfigError as exc:
        raise _unavailable(str(exc)) from exc
    if mode is IntegrationMode.REAL:
        raise _unavailable("Real-mode documents must come from an AI planner (Groq with Gemini fallback, or Claude). "
                           f"{ai_planner_problem(settings) or 'PLANNER_MODE=mock is not allowed.'}")
    return [MockDocumentWriter()]


def get_document_sources_factory() -> Callable[[Settings, IntegrationMode], list[DocumentSource]]:
    return default_sources


# ---------- plan ----------


class DocumentPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_type: DocumentType
    title: str = Field(min_length=3, max_length=TITLE_MAX)
    project_name: str | None = Field(default=None, max_length=PROJECT_MAX)
    instruction: str = Field(min_length=10, max_length=INSTRUCTION_MAX,
                             description="What the document should cover (untrusted; never changes the destination).")
    mode: ExecutionMode
    # Google Drive sandbox only: any other target is rejected (422).
    target: Literal["google_drive"] | None = None


class DocumentPlanResponse(PlanResponse):
    mode: ExecutionMode
    model: str | None
    ai_generated: bool
    document_type: DocumentType
    document_label: str
    project_name: str | None
    document: DocumentDraft | None
    file_name: str | None
    content: str | None  # the exact Markdown that would be uploaded (preview)
    content_chars: int
    destination_alias: str | None  # sandbox folder alias (dry_run/real); never the folder id
    operation: str
    rollback: str
    real_write: bool
    plan_digest: str | None  # the approval must carry exactly this value
    # The first draft exceeded the limit and was regenerated once with a compression instruction.
    compression_retry: bool = False
    initial_chars: int | None = None  # rendered length of that first, discarded draft


@router.post(
    "/plan",
    response_model=DocumentPlanResponse,
    responses={409: {"description": "Mode above the server ceiling"},
               422: {"model": DocumentPlanResponse, "description": "Document or plan rejected, or invalid body"},
               502: {"model": DocumentPlanResponse, "description": "Every AI provider failed"},
               503: {"description": "Not configured for this mode"}},
)
def plan_document(
    payload: DocumentPlanRequest,
    sources_factory: Callable[[Settings, IntegrationMode], list[DocumentSource]] = Depends(get_document_sources_factory),
    sleeper=Depends(get_documents_sleeper),
) -> JSONResponse:
    settings = get_settings()
    mode = _mode(settings, payload.mode)
    folder: str | None = None
    if mode is not IntegrationMode.MOCK:
        config = _config(settings)
        issues = real_drive_issues(settings, config) if mode is IntegrationMode.REAL \
            else drive_sandbox_issues(settings, config)
        if issues:
            raise _unavailable(" ".join(issues))
        folder = settings.smoke_test_drive_folder

    sources = sources_factory(settings, mode)
    now = datetime.now(timezone.utc)
    project = payload.project_name or None
    adapters = [DocumentWorkflowPlanner(s, document_type=payload.document_type, project_name=project,
                                        folder_alias=folder, now=now) for s in sources]
    chain = FallbackPlanner(adapters) if len(adapters) > 1 else adapters[0]
    service = PlanningService(chain, max_steps=max(settings.max_workflow_steps, 2),
                              max_planning_attempts=settings.max_planning_attempts, available_documents=None,
                              backoff=RetryPolicy.for_planning(settings), sleeper=sleeper)
    request = build_document_request(document_type=payload.document_type, title=payload.title,
                                     project_name=project, instruction=payload.instruction)
    folders = [folder] if folder else None
    result = service.plan(request, target=TARGET.value, drive_folders=folders)
    used = next((a for a in adapters if a.name == result.planner), adapters[0])

    if result.status is not PlanStatus.FAILED and used.issues:
        # Document rules first (they explain the problem, e.g. DOCUMENT_TOO_LONG with the actual
        # length), then anything the plan validator found that they do not already cover.
        seen = {(i.code, i.field) for i in used.issues}
        extra = [i for i in result.validation.issues if (i.code, i.field) not in seen]
        result = result.model_copy(update={"status": PlanStatus.INVALID, "planned_steps": [],
                                           "validation": ValidationResult(is_valid=False,
                                                                          issues=[*used.issues, *extra])})
    planned = result.status is PlanStatus.PLANNED
    digest = plan_digest(result.planned_steps, TARGET.value) if planned else None
    get_plan_registry().record(result.workflow_id, TARGET.value, folders, None, digest)
    get_telemetry().event("documents.plan", workflow_id=str(result.workflow_id),
                          level="DEFAULT" if planned else "WARNING",
                          metadata={"mode": mode.value, "document_type": payload.document_type,
                                    "status": result.status.value, "provider": result.planner,
                                    "fallback_used": result.fallback_used,
                                    "content_chars": len(used.content or ""),
                                    "compression_retry": used.initial_chars is not None,
                                    "initial_chars": used.initial_chars,
                                    "issue_codes": [i.code for i in result.validation.issues][:20]})

    operation, rollback = {
        IntegrationMode.MOCK: ("In-memory mock upload and read-back (no network)", "Mock compensation (in memory)"),
        IntegrationMode.DRY_RUN: ("Build the Drive upload and read-back requests without sending them",
                                  "Offline only; nothing is sent"),
        IntegrationMode.REAL: (f"Drive REST API (drive.file scope): upload the Markdown file into sandbox folder "
                               f"'{folder}', verify it (parent, size, md5) and read it back",
                               "If the read-back fails, the uploaded file is moved to Drive trash. A successful run "
                               "keeps it."),
    }[mode]
    response = DocumentPlanResponse(
        **result.model_dump(),
        mode=mode.value, model=used.model,
        ai_generated=isinstance(sources[adapters.index(used)], AI_DOCUMENT_WRITERS),
        document_type=payload.document_type, document_label=document_type_label(payload.document_type),
        project_name=project, document=used.draft, file_name=used.file_name, content=used.content,
        content_chars=len(used.content or ""), destination_alias=folder, operation=operation, rollback=rollback,
        real_write=mode is IntegrationMode.REAL and planned, plan_digest=digest,
        compression_retry=used.initial_chars is not None, initial_chars=used.initial_chars,
    )
    return JSONResponse(status_code=_HTTP_STATUS[response.status], content=response.model_dump(mode="json"))


# ---------- library (read-only) ----------


class LibraryDocument(BaseModel):
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None
    duplicate_name: bool


class LibraryResponse(BaseModel):
    mode: ExecutionMode
    simulated: bool  # true: synthetic fixtures (mock / dry run never contact Google Drive)
    folder_alias: str | None
    scope: str  # what the listing covers, stated plainly for the UI
    documents: list[LibraryDocument]


class DocumentPreview(BaseModel):
    mode: ExecutionMode
    simulated: bool
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None
    sha256: str
    content: str
    checks: list[str]


SCOPE = ("Markdown and plain-text files directly inside the approved Google Drive sandbox folder that this app can "
         "access (drive.file scope). Other Drive files are not visible to WorkflowWeaver.")


def _source_error(exc: SourceError) -> HTTPException:
    return HTTPException(exc.http_status, {"code": exc.code, "message": exc.message})


def _real_client(settings: Settings, http: Any):
    from app.agents.execution.tools.integrations import _drive_client

    config = _config(settings)
    issues = read_issues(settings, config)
    if issues:
        raise _unavailable(" ".join(issues))
    alias = settings.smoke_test_drive_folder
    assert alias
    return _drive_client(config, http), config.drive_folders.targets[alias], alias


@router.get("/library", response_model=LibraryResponse)
def list_library(mode: Literal["mock", "dry_run", "real"] = Query(...),
                 http: Any = Depends(get_documents_drive_http)) -> LibraryResponse:
    settings = get_settings()
    selected = _mode(settings, mode)
    if selected is not IntegrationMode.REAL:
        docs = list_fixture_sources()
        alias = settings.smoke_test_drive_folder if selected is IntegrationMode.DRY_RUN else "sandbox"
    else:
        client, target, alias = _real_client(settings, http)
        try:
            docs = list_drive_sources(client, sandbox_folder_id(client, target))
        except SourceError as exc:
            raise _source_error(exc) from exc
        finally:
            client.close()
    dupes = duplicate_names(docs)
    return LibraryResponse(mode=selected.value, simulated=selected is not IntegrationMode.REAL, folder_alias=alias,
                           scope=SCOPE, documents=[LibraryDocument(**d.__dict__, duplicate_name=d.name in dupes)
                                                   for d in docs])


@router.get("/library/{file_id}", response_model=DocumentPreview)
def preview_document(file_id: str = Path(min_length=10, max_length=128),
                     mode: Literal["mock", "dry_run", "real"] = Query(...),
                     http: Any = Depends(get_documents_drive_http)) -> DocumentPreview:
    settings = get_settings()
    selected = _mode(settings, mode)
    try:
        if selected is not IntegrationMode.REAL:
            read = read_fixture_source(file_id, selected)
        else:
            client, target, _ = _real_client(settings, http)
            try:
                read = read_drive_source(client, sandbox_folder_id(client, target), file_id)
            finally:
                client.close()
    except SourceError as exc:
        raise _source_error(exc) from exc
    info = read.info
    return DocumentPreview(mode=selected.value, simulated=read.simulated, file_id=info.file_id, name=info.name,
                           mime_type=info.mime_type, size_bytes=info.size_bytes, modified_time=info.modified_time,
                           sha256=read.sha256, content=read.text, checks=list(read.checks))


@router.get("/types")
def document_types() -> list[dict[str, Any]]:
    """The supported document types and their default outlines (static, no secrets)."""
    return [{"id": key, "label": label, "sections": list(sections)} for key, (label, sections) in DOCUMENT_TYPES.items()]
