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

export type Action = "create_page" | "create_task" | "create_document" | "read_document" | "send_message";

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
}

/** PlanRequest (extra="forbid"): instruction 10..5000 chars, name 3..120 chars or omitted. */
export interface PlanRequest {
  instruction: string;
  name?: string;
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
  executed: boolean;
}

// ---------- app/schemas/execution.py ----------

export type ExecutionStatus =
  | "COMPLETED"
  | "FAILED"
  | "ROLLED_BACK"
  | "PARTIALLY_ROLLED_BACK"
  | "COMPENSATION_FAILED"
  | "REJECTED";

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

export interface ExecutionRequest {
  workflow_id: string;
  plan: { summary: string; steps: ExecutionStepIn[] };
  approval: { approved: boolean; mode: "mock"; approved_by: string };
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
  mode: "mock";
  validation: ValidationResult;
  step_results: StepResult[];
  attempts: Record<string, number>;
  retry_delays_seconds: number[];
  errors: ExecutionError[];
  compensation_results: CompensationResult[];
  replayed: boolean;
  message: string;
}

// ---------- app/schemas/events.py ----------

export interface ExecutionStartResponse {
  workflow_id: string;
  status: "ACCEPTED";
  mode: "mock";
  stream_url: string;
  stream_token: string;
  result_url: string;
  access_model: "mock-capability-token";
}

export const EXECUTION_EVENT_TYPES = [
  "workflow_started",
  "step_started",
  "step_succeeded",
  "step_failed",
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
  send_message: "Send message",
};

export const integrationLabel = (value: string): string => INTEGRATION_LABELS[value] ?? value;
export const actionLabel = (value: string): string => ACTION_LABELS[value] ?? value;
