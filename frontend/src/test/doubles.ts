import { vi } from "vitest";

import type {
  DriveConnectivity,
  DriveSandboxPlanResponse,
  DriveSandboxStatus,
  ExecutionEvent,
  ExecutionEventType,
  ExecutionMode,
  ExecutionResponse,
  ExecutionStartResponse,
  IntegrationsStatus,
  NotionConnectivity,
  PlanResponse,
  ProjectPlan,
  ProjectPlanResponse,
  ProviderAttempt,
  SandboxPlanResponse,
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
  headers: Record<string, string>;
}

/** Route-based fetch mock. Later routes win, so tests can override defaults. */
export function installFetch(routes: MockRoute[]) {
  const calls: FetchCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({
      method,
      url,
      body: init?.body ? JSON.parse(String(init.body)) : null,
      headers: { ...((init?.headers ?? {}) as Record<string, string>) },
    });
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


// ---------- Phase 5.4: modes, Notion readiness, sandbox test (synthetic values only) ----------

export const STATUS_URL = "/api/v1/integrations/status";
export const CONNECTIVITY_URL = "/api/v1/integrations/notion/connectivity";
export const SANDBOX_PLAN_URL = "/api/v1/workflows/notion-sandbox/plan";

export const STATUS_MOCK: IntegrationsStatus = {
  mode: "mock",
  available_modes: ["mock"],
  real_writes_enabled: false,
  write_authorization_configured: false,
  configuration_valid: true,
  configuration_error: null,
  integrations: {
    notion: { transport: "mock", configured: true, destinations: {} },
    google_drive: { transport: "mock", configured: true, destinations: {} },
    teams: { transport: "mock", configured: true, destinations: {} },
  },
  notion_sandbox: {
    parent_alias: null,
    dry_run_ready: false,
    real_ready: false,
    issues: ["INTEGRATION_MODE is 'mock'; set INTEGRATION_MODE=dry_run or real to test Notion."],
  },
};

export const STATUS_REAL: IntegrationsStatus = {
  mode: "real",
  available_modes: ["mock", "dry_run", "real"],
  real_writes_enabled: true,
  write_authorization_configured: true,
  configuration_valid: true,
  configuration_error: null,
  integrations: {
    notion: { transport: "mcp", configured: true, destinations: { pages: { aliases: ["sandbox"], default: "sandbox" } } },
    google_drive: { transport: "mcp", configured: false, destinations: { folders: { aliases: [], default: null } } },
    teams: { transport: "webhook", configured: false, destinations: { channels: { aliases: [], default: null } } },
  },
  notion_sandbox: { parent_alias: "sandbox", dry_run_ready: true, real_ready: true, issues: [] },
};

export const STATUS_REAL_WRITES_OFF: IntegrationsStatus = {
  ...STATUS_REAL,
  real_writes_enabled: false,
  notion_sandbox: {
    parent_alias: "sandbox",
    dry_run_ready: true,
    real_ready: false,
    issues: ["REAL_WRITES_ENABLED is false (server write kill switch)."],
  },
};

export const CONNECTIVITY_PASS: NotionConnectivity = {
  status: "PASS",
  checks: [
    { name: "MCP initialize + tools/list", status: "PASS", detail: "3 allowlisted tool(s) offered" },
    { name: "retrieve sandbox parent page", status: "PASS", detail: "'sandbox' is reachable and shared with the integration" },
  ],
  issues: [],
  checked_at: "2026-11-04T10:00:00Z",
};

export const CONNECTIVITY_FAIL: NotionConnectivity = {
  status: "FAIL",
  checks: [{ name: "MCP initialize + tools/list", status: "FAIL", detail: "MCP_CONNECTION_FAILED" }],
  issues: [],
  checked_at: "2026-11-04T10:00:00Z",
};

export const SANDBOX_WORKFLOW_ID = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";

export function sandboxPlan(mode: ExecutionMode): SandboxPlanResponse {
  const parent: Record<string, string> = mode === "mock" ? {} : { parent: "sandbox" };
  return {
    workflow_id: SANDBOX_WORKFLOW_ID,
    status: "PLANNED",
    planner: "notion-sandbox",
    summary: "Notion Sandbox Test: create one synthetic page in the Notion sandbox.",
    planned_steps: [
      {
        step_number: 1,
        integration: "notion",
        action: "create_page",
        description: "Create one synthetic page under the Notion sandbox parent",
        arguments: { title: "WorkflowWeaver sandbox test 2026-11-04 10:00:00 UTC", content: "Synthetic test page.", ...parent },
        depends_on: [],
      },
    ],
    validation: { is_valid: true, issues: [] },
    errors: [],
    planning_attempts: 1,
    executed: false,
    mode,
    destination_alias: mode === "mock" ? null : "sandbox",
    operation: mode === "real" ? "Notion MCP API-post-page under sandbox parent 'sandbox'" : mode === "dry_run" ? "Build the API-post-page request without sending it" : "In-memory mock create_page (no network)",
    rollback: mode === "real" ? "Only if the run fails: API-patch-page in_trash=true." : "Offline only; nothing is sent",
    real_write: mode === "real",
  };
}

export function sandboxStarted(mode: ExecutionMode): ExecutionStartResponse {
  return {
    ...STARTED,
    workflow_id: SANDBOX_WORKFLOW_ID,
    mode,
    stream_url: `/api/v1/workflows/executions/${SANDBOX_WORKFLOW_ID}/events?access_token=synthetic-token`,
    result_url: `/api/v1/workflows/executions/${SANDBOX_WORKFLOW_ID}`,
  };
}

export const SYNTHETIC_PAGE_ID = "5e1f0000-0000-4000-9000-000000000001";
export const SYNTHETIC_PAGE_URL = "https://www.notion.so/5e1f0000000040009000000000000001";

export function sandboxReport(mode: ExecutionMode): ExecutionResponse {
  const output: Record<string, unknown> =
    mode === "real"
      ? { mode, destination: "sandbox", title: "WorkflowWeaver sandbox test 2026-11-04 10:00:00 UTC", url: SYNTHETIC_PAGE_URL }
      : { mode, would_call: "API-post-page", sent: false, destination: "sandbox", title: "WorkflowWeaver sandbox test 2026-11-04 10:00:00 UTC" };
  return {
    workflow_id: SANDBOX_WORKFLOW_ID,
    status: "COMPLETED",
    mode,
    validation: { is_valid: true, issues: [] },
    step_results: [
      {
        step_number: 1, integration: "notion", action: "create_page", status: "SUCCEEDED", attempts: 1,
        resource_id: mode === "real" ? SYNTHETIC_PAGE_ID : "dryrun-notion-abc", output, replayed: false, error: null,
      },
    ],
    attempts: { "1": 1 },
    retry_delays_seconds: [],
    errors: [],
    compensation_results: [],
    replayed: false,
    message: "All steps completed.",
  };
}

export const DEFAULT_ROUTES: MockRoute[] = [
  { path: "/health", body: { status: "ok", service: "workflowweaver", version: "0.3.0" } },
  { path: "/api/v1/workflows/templates", body: TEMPLATES },
  { path: "/api/v1/integrations/status", body: STATUS_MOCK },
];

// ---------- Phase 5.3 business workflow: AI project plan (synthetic values only) ----------

export const PROJECT_PLAN_URL = "/api/v1/workflows/notion-project/plan";
export const PROJECT_WORKFLOW_ID = "9c8b7a6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";

export const STATUS_REAL_CLAUDE: IntegrationsStatus = {
  ...STATUS_REAL,
  planner: { mode: "claude", model: "claude-test-model", ai_ready: true, claude_ready: true, issue: null },
};

export const STATUS_REAL_NO_KEY: IntegrationsStatus = {
  ...STATUS_REAL,
  planner: { mode: "claude", model: "claude-test-model", ai_ready: false, claude_ready: false, issue: "ANTHROPIC_API_KEY is not set (shell environment or .env)." },
};

export const STATUS_REAL_GEMINI: IntegrationsStatus = {
  ...STATUS_REAL,
  planner: { mode: "gemini", model: "gemini-test-flash", ai_ready: true, claude_ready: false, issue: null },
};

export const STATUS_REAL_GEMINI_NO_KEY: IntegrationsStatus = {
  ...STATUS_REAL,
  planner: { mode: "gemini", model: "gemini-test-flash", ai_ready: false, claude_ready: false, issue: "GEMINI_API_KEY is not set (shell environment or .env)." },
};

export const REWARDS_PROJECT: ProjectPlan = {
  project_title: "Customer Rewards Platform - Project Plan",
  overview: "Build a loyalty platform where customers earn and redeem reward points.",
  action_items: [
    { title: "Customer registration", description: "Let customers sign up and verify their email.", priority: "High", owner: "Backend team" },
    { title: "Reward balance lookup", description: "Show the current points balance.", priority: "High", owner: "Frontend team" },
    { title: "Points redemption", description: "Redeem points for rewards with balance checks.", priority: "Medium", owner: "Backend team" },
    { title: "Transaction history", description: "List earn and redeem transactions.", priority: "Low", owner: "Data team" },
  ],
};

export function projectPlan(mode: ExecutionMode, overrides: Partial<ProjectPlanResponse> = {}): ProjectPlanResponse {
  const project = REWARDS_PROJECT;
  const parent: Record<string, string> = mode === "mock" ? {} : { parent: "sandbox" };
  return {
    workflow_id: PROJECT_WORKFLOW_ID,
    status: "PLANNED",
    planner: "claude",
    summary: `${project.project_title}: project page plus 4 action item page(s) in Notion.`,
    planned_steps: [
      {
        step_number: 1, integration: "notion", action: "create_page", description: "Create the project page",
        arguments: { title: project.project_title, content: `${project.overview}\n- Customer registration - Priority: High`, ...parent },
        depends_on: [],
      },
      ...project.action_items.map((item, i) => ({
        step_number: i + 2, integration: "notion" as const, action: "create_page" as const,
        description: `Create action item '${item.title}' inside the project page`,
        arguments: { title: `Action item: ${item.title}`, content: `${item.description}\n- Suggested priority: ${item.priority}`, parent_step: "1" },
        depends_on: [1],
      })),
    ],
    validation: { is_valid: true, issues: [] },
    errors: [],
    planning_attempts: 1,
    executed: false,
    mode,
    model: "claude-test-model",
    ai_generated: true,
    project,
    destination_alias: mode === "mock" ? null : "sandbox",
    page_count: 5,
    operation: mode === "real" ? "Notion MCP API-post-page x5: the project page under sandbox parent 'sandbox', then each action item inside it" : "In-memory mock pages (no network)",
    rollback: mode === "real" ? "If a step fails, every page created by this run is moved to trash (API-patch-page in_trash=true), newest first." : "Mock compensation (in memory)",
    real_write: mode === "real",
    ...overrides,
  };
}

export const projectStarted = (mode: ExecutionMode): ExecutionStartResponse => ({
  ...STARTED,
  workflow_id: PROJECT_WORKFLOW_ID,
  mode,
  stream_url: `/api/v1/workflows/executions/${PROJECT_WORKFLOW_ID}/events?access_token=synthetic-token`,
  result_url: `/api/v1/workflows/executions/${PROJECT_WORKFLOW_ID}`,
});

export const pageId = (n: number) => `5e1f0000-0000-4000-9000-00000000000${n}`;
export const pageUrl = (n: number) => `https://www.notion.so/5e1f000000004000900000000000000${n}`;

export function projectReport(mode: ExecutionMode = "real"): ExecutionResponse {
  const titles = [REWARDS_PROJECT.project_title, ...REWARDS_PROJECT.action_items.map((i) => `Action item: ${i.title}`)];
  return {
    workflow_id: PROJECT_WORKFLOW_ID,
    status: "COMPLETED",
    mode,
    validation: { is_valid: true, issues: [] },
    step_results: titles.map((title, i) => ({
      step_number: i + 1, integration: "notion", action: "create_page", status: "SUCCEEDED" as const, attempts: i === 2 ? 2 : 1,
      resource_id: pageId(i + 1), replayed: false, error: null,
      output: { mode, title, destination: i === 0 ? "sandbox" : "page from step 1", url: pageUrl(i + 1) },
    })),
    attempts: { "1": 1, "2": 1, "3": 2, "4": 1, "5": 1 },
    retry_delays_seconds: [0.2],
    errors: [{ code: "NOTION_API_ERROR", message: "Notion rejected the request (HTTP 429, rate_limited).", step_number: 3, attempt: 1, retryable: true }],
    compensation_results: [],
    replayed: false,
    message: "All steps completed.",
  };
}

export function projectRolledBackReport(): ExecutionResponse {
  const ok = projectReport().step_results.slice(0, 3);
  return {
    ...projectReport(),
    status: "ROLLED_BACK",
    step_results: [
      ...ok,
      {
        step_number: 4, integration: "notion", action: "create_page", status: "FAILED", attempts: 1, resource_id: null, output: {}, replayed: false,
        error: { code: "NOTION_API_ERROR", message: "Notion rejected the request (HTTP 400, validation_error).", step_number: 4, attempt: 1, retryable: false },
      },
      { step_number: 5, integration: "notion", action: "create_page", status: "SKIPPED", attempts: 0, resource_id: null, output: {}, replayed: false, error: null },
    ],
    attempts: { "1": 1, "2": 1, "3": 1, "4": 1 },
    retry_delays_seconds: [],
    errors: [{ code: "NOTION_API_ERROR", message: "Notion rejected the request (HTTP 400, validation_error).", step_number: 4, attempt: 1, retryable: false }],
    compensation_results: [3, 2, 1].map((n) => ({
      step_number: n, integration: "notion", action: "create_page", resource_id: pageId(n), status: "COMPENSATED" as const, attempts: 1, message: "Effect undone.",
    })),
    message: "A step failed; all completed side effects were compensated.",
  };
}

// ---------- PLANNER_MODE=auto: Gemini primary, Groq fallback (synthetic values only) ----------

export const STATUS_REAL_AUTO: IntegrationsStatus = {
  ...STATUS_REAL,
  planner: {
    mode: "auto", model: "gemini-test-flash", ai_ready: true, claude_ready: false, issue: null,
    primary: "gemini", fallback: "groq", fallback_model: "openai/gpt-oss-120b", fallback_ready: true, fallback_issue: null,
  },
};

export const STATUS_REAL_AUTO_NO_GROQ: IntegrationsStatus = {
  ...STATUS_REAL,
  planner: {
    mode: "auto", model: "gemini-test-flash", ai_ready: true, claude_ready: false, issue: null,
    primary: "gemini", fallback: "groq", fallback_model: "openai/gpt-oss-120b", fallback_ready: false,
    fallback_issue: "GROQ_API_KEY is not set (shell environment or .env).",
  },
};

const GEMINI_503 = (attempt: number): ProviderAttempt => ({
  provider: "gemini", model: "gemini-test-flash", attempt, outcome: "error", error_code: "PLANNER_API_ERROR",
  http_status: 503, retryable: true, provider_unavailable: true,
});

/** A real-mode project plan that Groq produced after Gemini returned HTTP 503 twice. */
export function groqFallbackPlan(): ProjectPlanResponse {
  return projectPlan("real", {
    planner: "groq",
    model: "openai/gpt-oss-120b",
    planner_model: "openai/gpt-oss-120b",
    primary_planner: "gemini",
    fallback_used: true,
    fallback_reason: "PLANNER_API_ERROR",
    planning_attempts: 3,
    errors: [
      { code: "PLANNER_API_ERROR", message: "Attempt 1: Gemini API error (HTTP 503 UNAVAILABLE).", retryable: true, provider: "gemini" },
      { code: "PLANNER_API_ERROR", message: "Attempt 2: Gemini API error (HTTP 503 UNAVAILABLE).", retryable: true, provider: "gemini" },
    ],
    provider_attempts: [
      GEMINI_503(1),
      GEMINI_503(2),
      { provider: "groq", model: "openai/gpt-oss-120b", attempt: 1, outcome: "draft", error_code: null, http_status: null, retryable: false, provider_unavailable: false },
    ],
  });
}

/** Both providers failed: no plan, nothing to approve. */
export function bothProvidersFailedPlan(): ProjectPlanResponse {
  return projectPlan("real", {
    status: "FAILED",
    planner: "groq",
    model: "openai/gpt-oss-120b",
    planner_model: "openai/gpt-oss-120b",
    primary_planner: "gemini",
    fallback_used: true,
    fallback_reason: "PLANNER_RATE_LIMITED",
    planned_steps: [],
    page_count: 0,
    real_write: false,
    validation: { is_valid: false, issues: [] },
    errors: [
      { code: "PLANNER_RATE_LIMITED", message: "Attempt 1: Gemini API rate limit or quota hit (HTTP 429 RESOURCE_EXHAUSTED).", retryable: true, provider: "gemini" },
      { code: "PLANNER_API_ERROR", message: "Attempt 2: Groq API error (HTTP 503).", retryable: true, provider: "groq" },
      { code: "ALL_PLANNERS_FAILED", message: "No plan was produced: every planner provider failed (gemini, groq). Nothing was executed. Try again later.", retryable: false },
    ],
    provider_attempts: [
      { ...GEMINI_503(1), error_code: "PLANNER_RATE_LIMITED", http_status: 429 },
      { provider: "groq", model: "openai/gpt-oss-120b", attempt: 1, outcome: "error", error_code: "PLANNER_API_ERROR", http_status: 503, retryable: true, provider_unavailable: true },
    ],
  });
}

// ---------- Phase 6.1: Google Drive sandbox (synthetic values only) ----------

export const DRIVE_PLAN_URL = "/api/v1/workflows/drive-sandbox/plan";
export const DRIVE_CONNECTIVITY_URL = "/api/v1/integrations/google-drive/connectivity";
export const DRIVE_WORKFLOW_ID = "d71e0000-0000-4000-9000-00000000d71e";

export const DRIVE_READY: DriveSandboxStatus = {
  transport: "rest", folder_alias: "sandbox", oauth_client_configured: true, authorized: true,
  scope: "https://www.googleapis.com/auth/drive.file", sandbox_only: true, dry_run_ready: true, real_ready: true, issues: [],
};

export const STATUS_REAL_DRIVE: IntegrationsStatus = { ...STATUS_REAL, google_drive_sandbox: DRIVE_READY };

export const STATUS_REAL_DRIVE_UNAUTHORIZED: IntegrationsStatus = {
  ...STATUS_REAL,
  google_drive_sandbox: {
    ...DRIVE_READY, authorized: false, real_ready: false,
    issues: ["Google Drive is not authorized: run scripts/google_drive_auth.py (GOOGLE_DRIVE_TOKEN_FILE missing)."],
  },
};

export const DRIVE_CONNECTIVITY_PASS: DriveConnectivity = {
  status: "PASS",
  checks: [
    { name: "OAuth token refresh (drive.file)", status: "PASS", detail: "access token issued; scope is drive.file only" },
    { name: "read sandbox folder", status: "PASS", detail: "'sandbox' is the WorkflowWeaver sandbox" },
    { name: "list sandbox files", status: "PASS", detail: "0 item(s) visible to this app" },
  ],
  issues: [],
  checked_at: "2026-11-05T10:00:00Z",
};

export function drivePlan(mode: ExecutionMode): DriveSandboxPlanResponse {
  const folder: Record<string, string> = mode === "mock" ? {} : { folder: "sandbox" };
  return {
    workflow_id: DRIVE_WORKFLOW_ID,
    status: "PLANNED",
    planner: "drive-sandbox",
    summary: "Google Drive Sandbox Test: create a run folder in the sandbox, upload the project report into it (verified after upload) and read the report back to verify its content.",
    planned_steps: [
      { step_number: 1, integration: "google_drive", action: "create_folder", description: "Create a run folder inside the Google Drive sandbox folder", arguments: { name: "WorkflowWeaver run 2026-11-05 100000", ...folder }, depends_on: [] },
      { step_number: 2, integration: "google_drive", action: "create_document", description: "Upload the project report into the run folder and verify it", arguments: { title: "Sandbox report - report 2026-11-05 100000.md", content: "# WorkflowWeaver sandbox report\n", parent_step: "1" }, depends_on: [1] },
      { step_number: 3, integration: "google_drive", action: "read_document", description: "Read the uploaded report back and verify its content (read-only)", arguments: { document_name: "Sandbox report - report 2026-11-05 100000.md", parent_step: "1", content_sha256: "ab".repeat(32) }, depends_on: [1, 2] },
    ],
    validation: { is_valid: true, issues: [] },
    errors: [],
    planning_attempts: 1,
    executed: false,
    mode,
    integration: "google_drive",
    integration_target: "google_drive",
    destination_alias: mode === "mock" ? null : "sandbox",
    operation: mode === "real" ? "Drive REST API (drive.file scope): create a run folder in sandbox folder 'sandbox', upload the report (multipart) and verify it, then read the report back and verify its content" : "In-memory mock folder + report + verified read-back (no network)",
    rollback: "If a step fails, the report and run folder created by this run are moved to Drive trash (newest first). A successful run keeps them.",
    real_write: mode === "real",
    report_title: "Sandbox report - report 2026-11-05 100000.md",
    report_bytes: 31,
  };
}

export function driveStarted(mode: ExecutionMode): ExecutionStartResponse {
  return {
    ...STARTED,
    workflow_id: DRIVE_WORKFLOW_ID,
    mode,
    stream_url: `/api/v1/workflows/executions/${DRIVE_WORKFLOW_ID}/events?access_token=synthetic-token`,
    result_url: `/api/v1/workflows/executions/${DRIVE_WORKFLOW_ID}`,
  };
}
