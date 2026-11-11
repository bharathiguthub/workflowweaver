/**
 * TypeScript mirrors of the FastAPI/Pydantic contracts. Field names and nullability match
 * the backend exactly; sources are noted on each block.
 */

// ---------- app/schemas/workflow.py ----------

export type Integration = "notion" | "google_drive" | "teams";

export interface WorkflowTemplate {
  id: string;
  name: string;
  description: string;
  steps: string[];
  integrations: Integration[];
  /** Instruction that plans validly and executes against the mock tools (backend-owned). */
  example_instruction: string;
}

// ---------- app/schemas/plan.py ----------

export type Action =
  | "create_page"
  | "create_task"
  | "create_document"
  | "read_document"
  | "create_folder"
  | "list_files"
  | "send_message";

export type PlanStatus = "PLANNED" | "INVALID" | "FAILED";

export interface WorkflowStep {
  step_number: number;
  integration: Integration;
  action: Action;
  description: string;
  arguments: Record<string, string>;
  depends_on: number[];
}

export interface ValidationIssue {
  code: string;
  message: string;
  step_number: number | null;
  field: string | null;
}

export interface ValidationResult {
  is_valid: boolean;
  issues: ValidationIssue[];
}

export interface PlanningError {
  code: string;
  message: string;
  retryable: boolean;
  /** Planner provider that raised it (gemini, groq, ...). Absent on older backends. */
  provider?: string | null;
}

/** One call to one planner provider (codes and HTTP statuses only, never provider messages). */
export interface ProviderAttempt {
  provider: string;
  model: string | null;
  attempt: number;
  outcome: "draft" | "error";
  error_code: string | null;
  http_status: number | null;
  retryable: boolean;
  provider_unavailable: boolean;
}

/** PlanRequest (extra="forbid"): instruction 10..5000 chars, name 3..120 chars or omitted. */
/** Which integrations a plan may use. Enforced server-side (planner validator and executor). */
export type IntegrationTarget = "google_drive" | "notion" | "teams" | "drive_notion_teams" | "multi";

export const TARGET_LABELS: Record<IntegrationTarget, string> = {
  google_drive: "Google Drive Only",
  notion: "Notion Only",
  teams: "Microsoft Teams Only",
  drive_notion_teams: "Google Drive + Notion + Teams",
  multi: "Multi-Integration",
};

/** Integrations a target allows (null: any). */
export const targetIntegrations = (target: IntegrationTarget | null | undefined): Integration[] | null =>
  !target || target === "multi"
    ? null
    : target === "drive_notion_teams"
      ? ["google_drive", "notion", "teams"]
      : [target];

/** Steps of ``plan`` outside its integration target (empty when it has none). */
export const stepsOutsideTarget = (plan: PlanResponse): WorkflowStep[] => {
  const allowed = targetIntegrations(plan.integration_target);
  return allowed ? plan.planned_steps.filter((s) => !allowed.includes(s.integration)) : [];
};

export interface PlanRequest {
  instruction: string;
  name?: string;
  /** Mode the plan is meant for; only mock plans are checked against the synthetic documents. */
  mode?: ExecutionMode;
  /** Integration target; the server rejects any step outside it. */
  target?: IntegrationTarget;
}

export interface PlanResponse {
  workflow_id: string;
  status: PlanStatus;
  planner: string;
  summary: string | null;
  planned_steps: WorkflowStep[];
  validation: ValidationResult;
  errors: PlanningError[];
  planning_attempts: number;
  /** Seconds waited (backoff with jitter) before each planner retry. Absent on older backends. */
  retry_delays_seconds?: number[];
  /** Model id of the provider in `planner` (the one that produced the plan). Absent on older backends. */
  planner_model?: string | null;
  /** First provider tried (PLANNER_MODE=auto: PRIMARY_PLANNER). */
  primary_planner?: string | null;
  /** The plan (or the last error) came from the fallback provider. */
  fallback_used?: boolean;
  /** Error code that made the primary fall back, e.g. PLANNER_API_ERROR (HTTP 503). */
  fallback_reason?: string | null;
  provider_attempts?: ProviderAttempt[];
  /** Target the plan was restricted to (absent/null: unrestricted). */
  integration_target?: IntegrationTarget | null;
  /** Missing Google Drive references the server filled because only one value was possible. */
  plan_adjustments?: string[];
  executed: boolean;
}

// ---------- app/schemas/execution.py ----------

export type ExecutionStatus =
  | "COMPLETED"
  | "FAILED"
  | "ROLLED_BACK"
  | "PARTIALLY_ROLLED_BACK"
  | "COMPENSATION_FAILED"
  | "REJECTED"
  /** Phase 7.2: every Notion page created and verified (kept); only the final Teams notification failed. */
  | "PARTIALLY_COMPLETED";

export type StepStatus = "SUCCEEDED" | "FAILED" | "SKIPPED";

export type CompensationStatus = "COMPENSATED" | "ALREADY_COMPENSATED" | "NOT_COMPENSATABLE" | "FAILED";

export interface ExecutionStepIn {
  step_number: number;
  integration: string;
  action: string;
  description: string;
  arguments: Record<string, string>;
  depends_on: number[];
}

/**
 * Integration mode of one execution, sent as approval.mode. The backend's INTEGRATION_MODE is
 * the ceiling (mock < dry_run < real): a request may select that mode or a lower one, never higher.
 */
export type ExecutionMode = "mock" | "dry_run" | "real";

export const EXECUTION_MODES: readonly ExecutionMode[] = ["mock", "dry_run", "real"];

export const MODE_LABELS: Record<ExecutionMode, string> = { mock: "Mock", dry_run: "Dry Run", real: "Real" };

export interface ExecutionRequest {
  workflow_id: string;
  plan: { summary: string; steps: ExecutionStepIn[]; integration_target?: IntegrationTarget };
  /** integration_target must equal the plan's target, or the server rejects the run. */
  approval: {
    approved: boolean;
    mode: ExecutionMode;
    approved_by: string;
    integration_target?: IntegrationTarget;
    /** Phase 7.2: SHA-256 of the reviewed plan; the server rejects the run unless it matches. */
    plan_digest?: string;
  };
}

export interface ExecutionError {
  code: string;
  message: string;
  step_number: number | null;
  attempt: number | null;
  retryable: boolean;
}

export interface StepResult {
  step_number: number;
  integration: string;
  action: string;
  status: StepStatus;
  attempts: number;
  resource_id: string | null;
  output: Record<string, unknown>;
  replayed: boolean;
  error: ExecutionError | null;
}

export interface CompensationResult {
  step_number: number;
  integration: string;
  action: string;
  resource_id: string | null;
  status: CompensationStatus;
  attempts: number;
  message: string;
}

export interface ExecutionResponse {
  workflow_id: string;
  status: ExecutionStatus;
  mode: ExecutionMode;
  validation: ValidationResult;
  step_results: StepResult[];
  attempts: Record<string, number>;
  retry_delays_seconds: number[];
  errors: ExecutionError[];
  compensation_results: CompensationResult[];
  replayed: boolean;
  message: string;
}

// ---------- app/api/sandbox.py ----------

/** POST /api/v1/workflows/notion-sandbox/plan: a deterministic, server-built, Notion-only plan. */
export interface SandboxPlanResponse extends PlanResponse {
  mode: ExecutionMode;
  destination_alias: string | null;
  operation: string;
  rollback: string;
  real_write: boolean;
}

/** POST /api/v1/workflows/drive-sandbox/plan (Phase 6.1): folder + verified report upload + listing. */
export interface DriveSandboxPlanResponse extends SandboxPlanResponse {
  integration: "google_drive";
  report_title: string;
  report_bytes: number;
}

export const isDrivePlan = (plan: PlanResponse): plan is DriveSandboxPlanResponse =>
  "integration" in plan && (plan as DriveSandboxPlanResponse).integration === "google_drive";

/** A plan from /plan or the sandbox endpoint, with the mode it was generated for. */
export type ReviewedPlan = (PlanResponse | SandboxPlanResponse) & { mode: ExecutionMode };

/** True for server-built Notion plans (sandbox test and AI project plan): they carry
 * operation, destination, rollback and real_write details. */
export const isSandboxPlan = (plan: PlanResponse): plan is SandboxPlanResponse => "real_write" in plan;

// ---------- app/api/project.py + app/agents/project_workflow.py ----------

export type Priority = "High" | "Medium" | "Low";

export interface ActionItem {
  title: string;
  description: string;
  priority: Priority;
  /** A suggested TEAM (validated server-side: never a named person). */
  owner: string;
}

export interface ProjectPlan {
  project_title: string;
  overview: string;
  action_items: ActionItem[];
}

/** POST /api/v1/workflows/notion-project/plan (200 PLANNED, 422 INVALID, 502 FAILED). */
export interface ProjectPlanResponse extends SandboxPlanResponse {
  model: string | null;
  ai_generated: boolean;
  project: ProjectPlan | null;
  page_count: number;
}

export const isProjectPlan = (plan: PlanResponse): plan is ProjectPlanResponse => "project" in plan && "ai_generated" in plan;

// ---------- app/api/integrations.py ----------

export interface DestinationStatus {
  aliases: string[];
  default: string | null;
}

export interface IntegrationStatus {
  transport: "mcp" | "webhook" | "mock";
  configured: boolean;
  destinations: Record<string, DestinationStatus>;
}

export interface NotionSandboxStatus {
  parent_alias: string | null;
  dry_run_ready: boolean;
  real_ready: boolean;
  issues: string[];
}

export type PlannerMode = "mock" | "claude" | "gemini" | "auto";

export const PLANNER_LABELS: Record<string, string> = { claude: "Claude", gemini: "Gemini", groq: "Groq" };

/** "Claude" / "Gemini" / "Groq" for AI providers; null for the mock stub or anything unknown. */
export const aiPlannerLabel = (planner: string | null | undefined): string | null =>
  planner ? (PLANNER_LABELS[planner] ?? null) : null;

export interface PlannerStatus {
  mode: PlannerMode;
  model: string | null;
  /** The selected AI planner (Claude or Gemini) has its model and API key. */
  ai_ready: boolean;
  /** Older backends only report this one. */
  claude_ready: boolean;
  issue: string | null;
  /** PLANNER_MODE=auto only: the primary and fallback providers. */
  primary?: string | null;
  fallback?: string | null;
  fallback_model?: string | null;
  fallback_ready?: boolean;
  fallback_issue?: string | null;
}

/** The provider the planner status refers to: the primary in auto mode, else the mode itself. */
export const selectedProvider = (planner: PlannerStatus | null | undefined): string | null =>
  planner ? (planner.mode === "auto" ? (planner.primary ?? null) : planner.mode) : null;

export interface IntegrationsStatus {
  mode: ExecutionMode;
  /** Optional for compatibility with older backends. */
  planner?: PlannerStatus;
  available_modes: ExecutionMode[];
  real_writes_enabled: boolean;
  write_authorization_configured: boolean;
  configuration_valid: boolean;
  configuration_error: string | null;
  integrations: Record<string, IntegrationStatus>;
  notion_sandbox: NotionSandboxStatus;
  /** Phase 6.1; absent on older backends. */
  google_drive_sandbox?: DriveSandboxStatus;
  teams_sandbox?: TeamsSandboxStatus;
  orchestration?: OrchestrationStatus;
}

export interface ConnectivityCheck {
  name: string;
  status: "PASS" | "FAIL" | "SKIPPED";
  detail: string;
}

export interface NotionConnectivity {
  status: "PASS" | "FAIL" | "NOT_AVAILABLE";
  checks: ConnectivityCheck[];
  issues: string[];
  checked_at: string;
}

/** POST /api/v1/integrations/google-drive/connectivity: same shape, READ-ONLY probe. */
export type DriveConnectivity = NotionConnectivity;

/** Phase 6.1 Google Drive sandbox (REST + OAuth drive.file). Aliases only, never ids or tokens. */
export interface DriveSandboxStatus {
  transport: "mcp" | "rest" | "mock";
  folder_alias: string | null;
  oauth_client_configured: boolean;
  authorized: boolean;
  scope: string | null;
  sandbox_only: boolean;
  dry_run_ready: boolean;
  real_ready: boolean;
  issues: string[];
}

/** Phase 7.2 Google Drive + Notion + Teams workflow readiness per mode (setting names only). The
 * top-level fields are for plans WITH a source document; ``instructions_only`` for plans made from
 * workflow instructions alone (Google Drive not needed). */
export interface OrchestrationStatus {
  mock_ready: boolean;
  dry_run_ready: boolean;
  real_ready: boolean;
  issues: string[];
  instructions_only?: { mock_ready: boolean; dry_run_ready: boolean; real_ready: boolean; issues: string[] };
}

/** How a Phase 7.2 plan was made. */
export type OrchestrationInputMode = "document_only" | "instructions_only" | "document_and_instructions";

/** The inputs of a Phase 7.2 planning request (at least one of the two). */
export interface OrchestrationInput {
  sourceFileId: string | null;
  instructions: string | null;
  expectedPhases: number;
}

// ---------- app/api/orchestration.py (Phase 7.2) ----------

export interface OrchestrationSource {
  file_id: string;
  name: string;
  mime_type: string;
  size_bytes: number | null;
  modified_time: string | null;
  /** Another file has the same name: select by id, never by name. */
  duplicate_name: boolean;
}

export interface OrchestrationSourcesResponse {
  mode: ExecutionMode;
  /** true: synthetic fixtures (mock and dry run never contact Google Drive). */
  simulated: boolean;
  folder_alias: string | null;
  sources: OrchestrationSource[];
}

export interface ExtractedItem {
  text: string;
  /** true only for items found in the source DOCUMENT. */
  in_source: boolean;
  /** document | instructions (from your instructions) | unverified (not in the document; marked for
   * review) | generated (AI-generated: instructions-only plans). */
  origin: "document" | "instructions" | "unverified" | "generated";
}

export type Provenance = "document" | "instructions" | "suggested" | "none";

/** Where a summary came from: verified AI text ("document"), the source's own text copied by the
 * server ("source_text"), AI-generated from instructions ("generated"), or none. */
export type SummarySource = "document" | "source_text" | "generated" | "none";

export interface ExtractedPhase {
  title: string;
  /** The phase's own description (source-grounded with a document). Absent on older backends. */
  summary?: string;
  summary_source?: SummarySource;
  sections: Record<"objectives" | "technical_tasks" | "deliverables" | "acceptance_criteria", ExtractedItem[]>;
  priority: string;
  priority_source: Provenance;
  owner: string;
  owner_source: Provenance;
}

export interface OrchestrationPlanResponse extends PlanResponse {
  mode: ExecutionMode;
  workflow_name: string;
  input_mode: OrchestrationInputMode;
  model: string | null;
  ai_generated: boolean;
  plan_digest: string | null;
  /** SHA-256 of the inputs (mode, source id + content hash, instructions, phase count); part of plan_digest. */
  input_digest: string;
  /** The instructions as received (the user's own text), or null. */
  instructions: string | null;
  /** Parts of the instructions that were NOT applied (outside the workflow). */
  instruction_notes: string[];
  /** null: no source document (instructions only). */
  source: {
    file_id: string;
    name: string;
    mime_type: string;
    size_bytes: number | null;
    modified_time: string | null;
    sha256: string;
    simulated: boolean;
  } | null;
  extraction: { project_title: string; summary: string; summary_source?: SummarySource; phases: ExtractedPhase[] } | null;
  warnings: string[];
  stages: {
    stage: "instructions_check" | "source_validation" | "document_read" | "extraction" | "plan_validation";
    status: "passed" | "failed" | "simulated" | "skipped";
    detail: string;
  }[];
  checks: { performed: string[]; simulated: string[] };
  destinations: { drive_folder: string | null; notion_parent: string | null; teams_channel: string | null };
  write_summary: { notion_pages: number; teams_messages: number; drive_writes: number };
  real_write: boolean;
}

export const isOrchestrationPlan = (plan: PlanResponse): plan is OrchestrationPlanResponse =>
  "stages" in plan && "write_summary" in plan;

/** Phase 7.1 Microsoft Teams Only workflow (Workflows webhook). Aliases only, never webhook URLs. */
export interface TeamsSandboxStatus {
  transport: "webhook" | "mock";
  channel_alias: string | null;
  sandbox_only: boolean;
  /** Simulated delivery, no network call. */
  mock_ready: boolean;
  /** The webhook request can be built and previewed; nothing is sent. */
  dry_run_ready: boolean;
  /** Configuration only: a webhook cannot be probed without posting. */
  real_ready: boolean;
  issues: string[];
}

// ---------- app/schemas/events.py ----------

export interface ExecutionStartResponse {
  workflow_id: string;
  status: "ACCEPTED";
  mode: ExecutionMode;
  stream_url: string;
  stream_token: string;
  result_url: string;
  access_model: "mock-capability-token";
}

export const EXECUTION_EVENT_TYPES = [
  "approval_verified",
  "workflow_started",
  "step_started",
  "step_succeeded",
  "step_failed",
  "resource_verified",
  "retry_scheduled",
  "compensation_started",
  "compensation_completed",
  "compensation_failed",
  "workflow_completed",
  "workflow_failed",
] as const;

export type ExecutionEventType = (typeof EXECUTION_EVENT_TYPES)[number];

export const TERMINAL_EVENT_TYPES: ReadonlySet<ExecutionEventType> = new Set(["workflow_completed", "workflow_failed"]);

export interface ExecutionEvent {
  event_id: number;
  workflow_id: string;
  event_type: ExecutionEventType;
  step_number: number | null;
  timestamp: string;
  metadata: Record<string, unknown>;
}

/** Terminal event status: an ExecutionStatus, or "ERROR" for an unexpected server error. */
export type TerminalStatus = ExecutionStatus | "ERROR";

// ---------- FastAPI error body ----------

export interface FastApiValidationItem {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface FastApiErrorBody {
  detail: string | FastApiValidationItem[];
}

// ---------- display helpers ----------

export const INTEGRATION_LABELS: Record<string, string> = {
  notion: "Notion",
  google_drive: "Google Drive",
  teams: "Microsoft Teams",
};

export const ACTION_LABELS: Record<string, string> = {
  create_page: "Create page",
  create_task: "Create task",
  create_document: "Create document",
  read_document: "Read document",
  create_folder: "Create folder",
  list_files: "List files",
  send_message: "Send message",
};

export const integrationLabel = (value: string): string => INTEGRATION_LABELS[value] ?? value;
export const actionLabel = (value: string): string => ACTION_LABELS[value] ?? value;

// ---------- app/api/documents.py (Phase 8.2 Document Management) ----------

export type DocumentType =
  | "requirements"
  | "technical_design"
  | "implementation_plan"
  | "api_specification"
  | "readme"
  | "meeting_notes"
  | "custom";

export const DOCUMENT_TYPE_LABELS: Record<DocumentType, string> = {
  requirements: "Requirements",
  technical_design: "Technical Design",
  implementation_plan: "Implementation Plan",
  api_specification: "API Specification",
  readme: "README",
  meeting_notes: "Meeting Notes",
  custom: "Custom",
};

export interface DocumentRequest {
  documentType: DocumentType;
  title: string;
  projectName?: string;
  instruction: string;
}

export interface DocumentSection {
  heading: string;
  body: string;
}

export interface DocumentDraft {
  title: string;
  summary: string;
  sections: DocumentSection[];
}

/** POST /api/v1/workflows/documents/plan (200 PLANNED, 422 INVALID, 502 FAILED). Nothing is written. */
export interface DocumentPlanResponse extends SandboxPlanResponse {
  model: string | null;
  ai_generated: boolean;
  document_type: DocumentType;
  document_label: string;
  project_name: string | null;
  document: DocumentDraft | null;
  /** Markdown file name the upload would use (unique per plan). */
  file_name: string | null;
  /** The exact Markdown that would be uploaded. */
  content: string | null;
  content_chars: number;
  plan_digest: string | null;
  /** The first draft exceeded the 4800-character limit and was regenerated once, compressed. */
  compression_retry?: boolean;
  /** Rendered length of that first, discarded draft. */
  initial_chars?: number | null;
}

export const isDocumentPlan = (plan: PlanResponse): plan is DocumentPlanResponse =>
  "document_type" in plan && "content" in plan;

export interface LibraryDocument {
  file_id: string;
  name: string;
  mime_type: string;
  size_bytes: number | null;
  modified_time: string | null;
  duplicate_name: boolean;
}

/** GET /api/v1/workflows/documents/library: sandbox documents only (never the whole Drive). */
export interface DocumentLibraryResponse {
  mode: ExecutionMode;
  simulated: boolean;
  folder_alias: string | null;
  scope: string;
  documents: LibraryDocument[];
}

/** GET /api/v1/workflows/documents/library/{file_id}: read-only preview. */
export interface DocumentPreview {
  mode: ExecutionMode;
  simulated: boolean;
  file_id: string;
  name: string;
  mime_type: string;
  size_bytes: number | null;
  modified_time: string | null;
  sha256: string;
  content: string;
  checks: string[];
}

// ---------- app/api/monitoring.py (Phase 8 monitoring; values are from THIS API process) ----------

export type ServiceProbeStatus = "up" | "down" | "not_configured";

export interface MonitoringService {
  name: string;
  configured: boolean;
  status: ServiceProbeStatus;
  http_status?: number;
  latency_ms?: number;
  error?: string;
}

export interface MonitoringSummary {
  scope: string;
  generated_at: string;
  process_started_at: string;
  uptime_seconds: number;
  config: {
    service_name: string;
    environment: string;
    log_format: "json" | "console";
    log_level: string;
    metrics_enabled: boolean;
    metrics_protected: boolean;
    tracing_enabled: boolean;
    langfuse_enabled: boolean;
  };
  links: { grafana: string | null; langfuse: string | null; dashboards: { uid: string; title: string; url: string }[] };
  services: MonitoringService[];
  api: {
    requests: number;
    errors_4xx: number;
    errors_5xx: number;
    error_rate_5xx: number | null;
    avg_latency_ms: number | null;
    in_progress: number;
    routes: { route: string; requests: number; errors: number; avg_ms: number | null }[];
  };
  workflows: {
    plans: { target: string; status: string; count: number }[];
    executions_started: number;
    executions_in_progress: number;
    executions_finished: { status: string; count: number }[];
    rejections: { reason: string; count: number }[];
    step_retries: number;
    compensations: { outcome: string; count: number }[];
  };
  integrations: {
    step_failures: { integration: string; action: string; error_code: string; count: number }[];
    steps: { integration: string; status: string; count: number }[];
    calls: { integration: string; outcome: string; count: number }[];
  };
  ai: {
    providers: {
      provider: string;
      model: string;
      requests: number;
      errors: number;
      avg_latency_ms: number | null;
      tokens: Record<string, number>;
    }[];
    fallbacks: { from_provider: string; to_provider: string; reason: string; count: number }[];
  };
}

export interface MonitoringLogRecord {
  timestamp: string;
  severity: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  message: string;
  logger?: string;
  event?: string;
  request_id?: string;
  workflow_id?: string;
  trace_id?: string;
  integration?: string;
  action?: string;
  duration_ms?: number;
  error_code?: string;
  status?: string;
}

export interface MonitoringLogs {
  scope: string;
  records: MonitoringLogRecord[];
  loki_explore_url: string | null;
}

export interface MonitoringRun {
  kind: "planning" | "execution";
  workflow_id?: string;
  trace_id?: string;
  trace_url?: string | null;
  started_at?: string;
  duration_ms?: number;
  status?: string;
  error_code?: string;
  target?: string;
  mode?: string;
  provider?: string;
  fallback_used?: boolean;
  replayed?: boolean;
}

export interface MonitoringTraces {
  scope: string;
  tracing_enabled: boolean;
  langfuse_enabled: boolean;
  langfuse_url: string | null;
  runs: MonitoringRun[];
}
