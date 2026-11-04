import { vi } from "vitest";

import type {
  ExecutionEvent,
  ExecutionEventType,
  ExecutionResponse,
  ExecutionStartResponse,
  PlanResponse,
  WorkflowTemplate,
} from "../api/types";

// ---------- EventSource ----------

type Listener = (event: MessageEvent) => void;

/** Controllable stand-in for the browser EventSource. */
export class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState = FakeEventSource.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private listeners = new Map<string, Set<Listener>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  static latest(): FakeEventSource {
    const source = FakeEventSource.instances.at(-1);
    if (!source) throw new Error("No EventSource was opened");
    return source;
  }

  addEventListener(type: string, listener: Listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }

  close() {
    this.readyState = FakeEventSource.CLOSED;
  }

  open() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.(new Event("open"));
  }

  emit(event: ExecutionEvent) {
    this.emitRaw(event.event_type, JSON.stringify(event), String(event.event_id));
  }

  emitRaw(type: string, data: string, lastEventId = "") {
    if (this.readyState === FakeEventSource.CLOSED) return;
    const message = new MessageEvent(type, { data, lastEventId });
    this.listeners.get(type)?.forEach((listener) => listener(message));
  }

  /** Simulate a network drop (browser keeps retrying) or a fatal error (browser gives up). */
  fail(fatal: boolean) {
    this.readyState = fatal ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
    this.onerror?.(new Event("error"));
  }
}

export function installFakeEventSource() {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
}

// ---------- fetch ----------

export interface MockRoute {
  method?: string;
  path: string | RegExp;
  status?: number;
  body?: unknown;
  networkError?: boolean;
}

export interface FetchCall {
  method: string;
  url: string;
  body: unknown;
}

/** Route-based fetch mock. Later routes win, so tests can override defaults. */
export function installFetch(routes: MockRoute[]) {
  const calls: FetchCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
    const route = [...routes]
      .reverse()
      .find((r) => (r.method ?? "GET") === method && (typeof r.path === "string" ? url === r.path : r.path.test(url)));
    if (!route) return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
    if (route.networkError) throw new TypeError("Failed to fetch");
    const status = route.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(route.body ?? null), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, routes };
}

// ---------- fixtures (shapes match the backend Pydantic models) ----------

export const WORKFLOW_ID = "3f6c2a9e-8b1d-4c7e-9a2f-5d4e6b7c8a90";

export const TEMPLATES: WorkflowTemplate[] = [
  {
    id: "meeting-to-tasks",
    name: "Meeting-to-Tasks",
    description: "Extract action items from meeting notes and create tasks.",
    steps: ["Fetch meeting notes", "Extract action items", "Create tasks in Notion", "Notify attendees in Teams"],
    integrations: ["google_drive", "notion", "teams"],
    example_instruction:
      "Read 'Synthetic standup notes' from Google Drive, create Notion tasks for each action item, save the meeting minutes to Google Drive, and notify attendees in Microsoft Teams.",
  },
  {
    id: "sprint-planning",
    name: "Sprint Planning",
    description: "Turn a backlog into a prioritised sprint plan.",
    steps: ["Load backlog items", "Draft sprint plan page in Notion", "Share sprint summary in Teams"],
    integrations: ["notion", "teams"],
    example_instruction: "Load the sprint backlog, draft the sprint plan page in Notion, and share the sprint summary in Microsoft Teams.",
  },
  {
    id: "incident-response",
    name: "Incident Response",
    description: "Coordinate triage, communication and post-mortem for an incident.",
    steps: ["Alert on-call channel in Teams", "Create post-mortem document in Google Drive"],
    integrations: ["teams", "google_drive"],
    example_instruction:
      "Read 'Synthetic incident report' from Google Drive, alert the on-call channel in Microsoft Teams, and create a post-mortem document in Google Drive.",
  },
];

export const PLANNED: PlanResponse = {
  workflow_id: WORKFLOW_ID,
  status: "PLANNED",
  planner: "mock",
  summary: "Planned 3 step(s) using: notion, google_drive, teams.",
  planned_steps: [
    { step_number: 1, integration: "notion", action: "create_task", description: "Create a task in Notion", arguments: { title: "Action items: Synthetic standup notes" }, depends_on: [] },
    { step_number: 2, integration: "google_drive", action: "create_document", description: "Save a summary document to Google Drive", arguments: { title: "Synthetic standup notes: summary", content: "Summary of: synthetic" }, depends_on: [] },
    { step_number: 3, integration: "teams", action: "send_message", description: "Notify the team in Microsoft Teams", arguments: { message: "'Synthetic standup notes' workflow is ready for review." }, depends_on: [1, 2] },
  ],
  validation: { is_valid: true, issues: [] },
  errors: [],
  planning_attempts: 1,
  executed: false,
};

export const INVALID: PlanResponse = {
  ...PLANNED,
  status: "INVALID",
  summary: "Planned 2 step(s) using: jira, teams.",
  planned_steps: [],
  validation: {
    is_valid: false,
    issues: [
      { code: "UNSUPPORTED_INTEGRATION", message: "Integration 'jira' is not supported. Allowed: notion, google_drive, teams.", step_number: 1, field: "integration" },
    ],
  },
  errors: [{ code: "PLAN_VALIDATION_FAILED", message: "The generated plan failed validation with 1 issue(s).", retryable: false }],
};

export const STARTED: ExecutionStartResponse = {
  workflow_id: WORKFLOW_ID,
  status: "ACCEPTED",
  mode: "mock",
  stream_url: `/api/v1/workflows/executions/${WORKFLOW_ID}/events?access_token=synthetic-token`,
  stream_token: "synthetic-token",
  result_url: `/api/v1/workflows/executions/${WORKFLOW_ID}`,
  access_model: "mock-capability-token",
};

let nextId = 1;
export function resetEventIds() {
  nextId = 1;
}

export function ev(event_type: ExecutionEventType, step_number: number | null = null, metadata: Record<string, unknown> = {}): ExecutionEvent {
  return { event_id: nextId++, workflow_id: WORKFLOW_ID, event_type, step_number, timestamp: "2026-11-04T03:14:49.865218Z", metadata };
}

export const ROLLED_BACK_REPORT: ExecutionResponse = {
  workflow_id: WORKFLOW_ID,
  status: "ROLLED_BACK",
  mode: "mock",
  validation: { is_valid: true, issues: [] },
  step_results: [
    { step_number: 1, integration: "notion", action: "create_task", status: "SUCCEEDED", attempts: 1, resource_id: "mock-notion-task-aaa", output: {}, replayed: false, error: null },
    { step_number: 2, integration: "google_drive", action: "create_document", status: "SUCCEEDED", attempts: 2, resource_id: "mock-gdrive-doc-bbb", output: {}, replayed: false, error: null },
    {
      step_number: 3, integration: "teams", action: "send_message", status: "FAILED", attempts: 1, resource_id: null, output: {}, replayed: false,
      error: { code: "TOOL_REJECTED", message: "Injected permanent failure", step_number: 3, attempt: 1, retryable: false },
    },
  ],
  attempts: { "1": 1, "2": 2, "3": 1 },
  retry_delays_seconds: [0.2],
  errors: [
    { code: "TOOL_TIMEOUT", message: "Injected transient failure", step_number: 2, attempt: 1, retryable: true },
    { code: "TOOL_REJECTED", message: "Injected permanent failure", step_number: 3, attempt: 1, retryable: false },
  ],
  compensation_results: [
    { step_number: 2, integration: "google_drive", action: "create_document", resource_id: "mock-gdrive-doc-bbb", status: "COMPENSATED", attempts: 1, message: "Effect undone." },
    { step_number: 1, integration: "notion", action: "create_task", resource_id: "mock-notion-task-aaa", status: "COMPENSATED", attempts: 1, message: "Effect undone." },
  ],
  replayed: false,
  message: "A step failed; all completed side effects were compensated.",
};

/** The real event sequence the backend emits for ROLLED_BACK_REPORT (one retry, then rollback). */
export function rollbackEvents(): ExecutionEvent[] {
  resetEventIds();
  return [
    ev("workflow_started", null, { step_count: 3 }),
    ev("step_started", 1, { attempt: 1, integration: "notion", action: "create_task" }),
    ev("step_succeeded", 1, { attempt: 1, resource_id: "mock-notion-task-aaa", replayed: false }),
    ev("step_started", 2, { attempt: 1, integration: "google_drive", action: "create_document" }),
    ev("step_failed", 2, { attempt: 1, error_code: "TOOL_TIMEOUT", retryable: true, will_retry: true, retries_exhausted: false }),
    ev("retry_scheduled", 2, { phase: "execution", failed_attempt: 1, next_attempt: 2, delay_seconds: 0.2, error_code: "TOOL_TIMEOUT" }),
    ev("step_started", 2, { attempt: 2, integration: "google_drive", action: "create_document" }),
    ev("step_succeeded", 2, { attempt: 2, resource_id: "mock-gdrive-doc-bbb", replayed: false }),
    ev("step_started", 3, { attempt: 1, integration: "teams", action: "send_message" }),
    ev("step_failed", 3, { attempt: 1, error_code: "TOOL_REJECTED", retryable: false, will_retry: false, retries_exhausted: false }),
    ev("compensation_started", 2, { integration: "google_drive", action: "create_document" }),
    ev("compensation_completed", 2, { status: "COMPENSATED", attempts: 1 }),
    ev("compensation_started", 1, { integration: "notion", action: "create_task" }),
    ev("compensation_completed", 1, { status: "COMPENSATED", attempts: 1 }),
    ev("workflow_failed", null, {
      status: "ROLLED_BACK", failed_step: 3, step_statuses: { "1": "SUCCEEDED", "2": "SUCCEEDED", "3": "FAILED" },
      compensation_statuses: { "2": "COMPENSATED", "1": "COMPENSATED" }, error_codes: ["TOOL_TIMEOUT", "TOOL_REJECTED"], validation_issue_codes: [],
    }),
  ];
}

export const DEFAULT_ROUTES: MockRoute[] = [
  { path: "/health", body: { status: "ok", service: "workflowweaver", version: "0.3.0" } },
  { path: "/api/v1/workflows/templates", body: TEMPLATES },
];
