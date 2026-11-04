import type { ExecutionEvent, TerminalStatus, WorkflowStep } from "../api/types";

/** Per-step UI status, derived only from streamed events (never guessed). */
export type StepUiStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "retrying"
  | "compensating"
  | "rolled_back"
  | "not_reversible"
  | "compensation_failed"
  | "skipped";

export type WorkflowUiStatus =
  | "idle"
  | "pending" // request accepted, waiting for the first event
  | "running"
  | "retrying"
  | "compensating"
  | "completed"
  | "failed"
  | "rolled_back"
  | "partially_rolled_back"
  | "compensation_failed"
  | "rejected"
  | "error";

export interface StepView {
  step_number: number;
  integration: string;
  action: string;
  status: StepUiStatus;
  attempt: number;
  lastErrorCode: string | null;
  retryDelaySeconds: number | null;
  resourceId: string | null;
  compensationStatus: string | null;
}

export interface ExecutionView {
  workflowId: string | null;
  workflowStatus: WorkflowUiStatus;
  steps: StepView[];
  events: ExecutionEvent[];
  lastEventId: number;
  terminal: ExecutionEvent | null;
}

export type ExecutionAction =
  | { type: "reset" }
  | { type: "init"; workflowId: string; steps: WorkflowStep[] }
  | { type: "event"; event: ExecutionEvent }
  | { type: "error" };

export const initialExecutionView: ExecutionView = {
  workflowId: null,
  workflowStatus: "idle",
  steps: [],
  events: [],
  lastEventId: 0,
  terminal: null,
};

const TERMINAL_STATUS_TO_UI: Record<TerminalStatus, WorkflowUiStatus> = {
  COMPLETED: "completed",
  FAILED: "failed",
  ROLLED_BACK: "rolled_back",
  PARTIALLY_ROLLED_BACK: "partially_rolled_back",
  COMPENSATION_FAILED: "compensation_failed",
  REJECTED: "rejected",
  ERROR: "error",
};

const asString = (value: unknown): string | null => (typeof value === "string" ? value : null);
const asNumber = (value: unknown): number | null => (typeof value === "number" ? value : null);

function updateStep(steps: StepView[], stepNumber: number | null, patch: Partial<StepView>): StepView[] {
  if (stepNumber === null) return steps;
  return steps.map((s) => (s.step_number === stepNumber ? { ...s, ...patch } : s));
}

function applyEvent(state: ExecutionView, event: ExecutionEvent): ExecutionView {
  const m = event.metadata;
  const n = event.step_number;
  let { steps, workflowStatus } = state;

  switch (event.event_type) {
    case "workflow_started":
      workflowStatus = "running";
      break;
    case "step_started":
      steps = updateStep(steps, n, {
        status: "running",
        attempt: asNumber(m.attempt) ?? 1,
        retryDelaySeconds: null,
      });
      workflowStatus = "running";
      break;
    case "step_succeeded":
      steps = updateStep(steps, n, { status: "succeeded", resourceId: asString(m.resource_id), lastErrorCode: null });
      break;
    case "step_failed": {
      const willRetry = m.will_retry === true;
      steps = updateStep(steps, n, { status: willRetry ? "retrying" : "failed", lastErrorCode: asString(m.error_code) });
      if (willRetry) workflowStatus = "retrying";
      break;
    }
    case "retry_scheduled":
      if (m.phase === "compensation") {
        steps = updateStep(steps, n, { status: "compensating", retryDelaySeconds: asNumber(m.delay_seconds) });
      } else {
        steps = updateStep(steps, n, { status: "retrying", retryDelaySeconds: asNumber(m.delay_seconds) });
        workflowStatus = "retrying";
      }
      break;
    case "compensation_started":
      steps = updateStep(steps, n, { status: "compensating", retryDelaySeconds: null });
      workflowStatus = "compensating";
      break;
    case "compensation_completed": {
      const status = asString(m.status);
      steps = updateStep(steps, n, {
        status: status === "NOT_COMPENSATABLE" ? "not_reversible" : "rolled_back",
        compensationStatus: status,
      });
      break;
    }
    case "compensation_failed":
      steps = updateStep(steps, n, { status: "compensation_failed", compensationStatus: asString(m.status) });
      break;
    case "workflow_completed":
    case "workflow_failed": {
      const status = (asString(m.status) ?? (event.event_type === "workflow_completed" ? "COMPLETED" : "ERROR")) as TerminalStatus;
      workflowStatus = TERMINAL_STATUS_TO_UI[status] ?? "error";
      const stepStatuses = (m.step_statuses ?? {}) as Record<string, unknown>;
      steps = steps.map((s) => (s.status === "pending" && stepStatuses[String(s.step_number)] === "SKIPPED" ? { ...s, status: "skipped" } : s));
      return { ...state, steps, workflowStatus, terminal: event };
    }
  }
  return { ...state, steps, workflowStatus };
}

export function executionReducer(state: ExecutionView, action: ExecutionAction): ExecutionView {
  switch (action.type) {
    case "reset":
      return initialExecutionView;
    case "init":
      return {
        ...initialExecutionView,
        workflowId: action.workflowId,
        workflowStatus: "pending",
        steps: action.steps.map((s) => ({
          step_number: s.step_number,
          integration: s.integration,
          action: s.action,
          status: "pending",
          attempt: 0,
          lastErrorCode: null,
          retryDelaySeconds: null,
          resourceId: null,
          compensationStatus: null,
        })),
      };
    case "event": {
      const { event } = action;
      // Ignore other workflows, duplicates/out-of-order frames, and anything after the end.
      if (event.workflow_id !== state.workflowId || event.event_id <= state.lastEventId || state.terminal) return state;
      const next = applyEvent(state, event);
      return { ...next, events: [...state.events, event], lastEventId: event.event_id };
    }
    case "error":
      return state.terminal ? state : { ...state, workflowStatus: "error" };
  }
}

export const isTerminalStatus = (status: WorkflowUiStatus): boolean =>
  ["completed", "failed", "rolled_back", "partially_rolled_back", "compensation_failed", "rejected", "error"].includes(status);
