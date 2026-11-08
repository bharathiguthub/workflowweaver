import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type {
  ExecutionMode,
  ExecutionResponse,
  ExecutionStartResponse,
  IntegrationsStatus,
  PlanResponse,
  TeamsSandboxStatus,
} from "./api/types";
import { TeamsMessagePreview } from "./components/PlanReview";
import { teamsStepNote } from "./components/ExecutionMonitor";
import { resourceKind } from "./components/ExecutionReport";
import { teamsAvailability } from "./components/IntegrationTargetSelector";
import { customPlanBlockedReason, modeOptions } from "./state/modes";
import type { StepView } from "./state/execution";
import {
  CONNECTIVITY_FAIL,
  CONNECTIVITY_URL,
  DEFAULT_ROUTES,
  FakeEventSource,
  PLANNED,
  STARTED,
  STATUS_MOCK,
  STATUS_REAL_GEMINI,
  STATUS_URL,
  ev,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  type MockRoute,
} from "./test/doubles";
import { renderApp } from "./test/renderApp";

// Synthetic data only. No real Teams webhook exists anywhere in the frontend: the UI only ever
// sees the channel ALIAS. Nothing here performs network I/O (fetch and EventSource are fakes).
const PLAN_URL = "/api/v1/workflows/plan";
const EXECUTIONS = "/api/v1/workflows/executions";
const OPERATOR_TOKEN = "synthetic-operator-token";
const TEAMS_WORKFLOW_ID = "1e2d3c4b-5a69-4788-9a0b-1c2d3e4f5a6b";
const MESSAGE = "Release 2.4 is live.\n- New reward balance page\n- Faster redemption";
const INSTRUCTION = "Announce release 2.4 in the sandbox Teams channel.";
const REF = "teams-msg-0123456789abcdef";

const TEAMS_REAL_READY: TeamsSandboxStatus = {
  transport: "webhook", channel_alias: "sandbox", sandbox_only: true, mock_ready: true, dry_run_ready: true,
  real_ready: true, issues: [],
};

// Real-capable server where ONLY Teams is ready (Notion probe fails, Drive not configured).
const STATUS: IntegrationsStatus = {
  ...STATUS_REAL_GEMINI,
  notion_sandbox: { ...STATUS_REAL_GEMINI.notion_sandbox, real_ready: false, issues: ["NOTION_MCP_URL is not set."] },
  teams_sandbox: TEAMS_REAL_READY,
};

function started(mode: ExecutionMode): ExecutionStartResponse {
  return {
    ...STARTED, workflow_id: TEAMS_WORKFLOW_ID, mode,
    stream_url: `/api/v1/workflows/executions/${TEAMS_WORKFLOW_ID}/events?access_token=synthetic-token`,
    result_url: `/api/v1/workflows/executions/${TEAMS_WORKFLOW_ID}`,
  };
}

function teamsPlan(mode: ExecutionMode = "real"): PlanResponse {
  return {
    ...PLANNED, workflow_id: TEAMS_WORKFLOW_ID, planner: "gemini", integration_target: "teams",
    summary: `Post the release announcement (${mode}).`,
    planned_steps: [{ step_number: 1, integration: "teams", action: "send_message", description: "Announce the release",
      arguments: { message: MESSAGE, channel: "sandbox" }, depends_on: [] }],
  };
}

function report(mode: ExecutionMode, output: Record<string, unknown>): ExecutionResponse {
  return {
    workflow_id: TEAMS_WORKFLOW_ID, status: "COMPLETED", mode, validation: { is_valid: true, issues: [] },
    step_results: [{ step_number: 1, integration: "teams", action: "send_message", status: "SUCCEEDED", attempts: 1,
      resource_id: REF, output, replayed: false, error: null }],
    attempts: { "1": 1 }, retry_delays_seconds: [], errors: [], compensation_results: [], replayed: false,
    message: "All steps completed.",
  };
}

const REAL_OUTPUT = { mode: "real", channel: "sandbox", accepted: true, delivery: "accepted_by_webhook", http_status: 202, latency_ms: 41.2 };
const DRY_OUTPUT = { mode: "dry_run", channel: "sandbox", would_call: "teams_webhook", sent: false, delivered: false,
  payload_format: "adaptive_card_1.2", payload_bytes: 412, message_chars: MESSAGE.length };

/** ev() for this workflow (the reducer ignores events of other workflows). */
const tev = (...args: Parameters<typeof ev>) => ({ ...ev(...args), workflow_id: TEAMS_WORKFLOW_ID });

function setup(routes: MockRoute[] = [], status: IntegrationsStatus = STATUS) {
  const state = installFetch([
    ...DEFAULT_ROUTES,
    { path: STATUS_URL, body: status },
    { method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_FAIL },
    ...routes,
  ]);
  const user = userEvent.setup();
  renderApp("Google Drive Only");
  return { user, ...state };
}

const radio = (name: string) => screen.getByRole("radio", { name });

async function planTeams(user: ReturnType<typeof userEvent.setup>, mode: "Mock" | "Dry Run" | "Real") {
  if (mode !== "Mock") {
    await waitFor(() => expect(radio(mode)).toBeEnabled());
    await user.click(radio(mode));
  }
  await waitFor(() => expect(radio("Microsoft Teams Only")).toBeEnabled());
  await user.click(radio("Microsoft Teams Only"));
  await user.type(screen.getByLabelText("Instruction"), INSTRUCTION);
  await user.click(screen.getByRole("button", { name: "Generate Plan" }));
  return screen.findByRole("region", { name: "2. Review the plan" });
}

function assertNoWebhookUrl() {
  const html = document.body.innerHTML;
  expect(html).not.toMatch(/logic\.azure\.com|webhook\.office\.com|sig=/);
}

describe("Microsoft Teams Only target availability", () => {
  it("is enabled in Mock when the backend reports simulated delivery", async () => {
    setup([], { ...STATUS_MOCK, teams_sandbox: { ...TEAMS_REAL_READY, transport: "mock", channel_alias: null, dry_run_ready: false, real_ready: false } });
    await waitFor(() => expect(radio("Microsoft Teams Only")).toBeEnabled());
  });

  it("is disabled for a mode the backend says Teams is not ready for, with the setting name", async () => {
    const notReal = { ...TEAMS_REAL_READY, real_ready: false, issues: ["REAL_WRITES_ENABLED is false (server write kill switch)."] };
    expect(teamsAvailability({ ...STATUS, teams_sandbox: notReal }, "real")).toEqual({
      available: false, reason: "REAL_WRITES_ENABLED is false (server write kill switch)." });
    expect(teamsAvailability({ ...STATUS, teams_sandbox: notReal }, "dry_run").available).toBe(true);
    const notConfigured = { ...TEAMS_REAL_READY, dry_run_ready: false, real_ready: false, channel_alias: null,
      issues: ["TEAMS_WEBHOOK_URLS is not set (JSON map of channel alias -> Teams Workflows webhook URL)."] };
    expect(teamsAvailability({ ...STATUS, teams_sandbox: notConfigured }, "dry_run").reason).toMatch(/TEAMS_WEBHOOK_URLS/);
    expect(teamsAvailability({ ...STATUS, teams_sandbox: notConfigured }, "mock").available).toBe(true);
  });

  it("a ready Teams sandbox alone enables Real mode (no webhook probe exists)", () => {
    expect(modeOptions(STATUS, CONNECTIVITY_FAIL).real.available).toBe(true);
    expect(modeOptions({ ...STATUS, teams_sandbox: { ...TEAMS_REAL_READY, real_ready: false } }, CONNECTIVITY_FAIL).real.available).toBe(false);
  });

  it("custom Teams plans in Real mode need the Teams sandbox and an AI planner", () => {
    expect(customPlanBlockedReason("real", "teams", STATUS, null)).toBeNull();
    expect(customPlanBlockedReason("real", "teams", { ...STATUS, teams_sandbox: { ...TEAMS_REAL_READY, real_ready: false, issues: ["REAL_WRITE_AUTHORIZATION_TOKEN is not set."] } }, null))
      .toBe("REAL_WRITE_AUTHORIZATION_TOKEN is not set.");
    expect(customPlanBlockedReason("real", "teams", { ...STATUS, planner: { ...STATUS_REAL_GEMINI.planner!, ai_ready: false, issue: "GEMINI_API_KEY is not set." } }, null))
      .toMatch(/AI planner.*GEMINI_API_KEY/);
    expect(customPlanBlockedReason("dry_run", "teams", STATUS, null)).toBeNull();
  });

  it("switches away from Teams when the selected mode makes it unavailable", async () => {
    const { user } = setup([], { ...STATUS, teams_sandbox: { ...TEAMS_REAL_READY, real_ready: false, issues: ["REAL_WRITES_ENABLED is false (server write kill switch)."] },
      google_drive_sandbox: undefined });
    await waitFor(() => expect(radio("Microsoft Teams Only")).toBeEnabled());
    await user.click(radio("Microsoft Teams Only"));
    expect(radio("Microsoft Teams Only")).toBeChecked();
    expect(radio("Real")).toBeDisabled(); // nothing is real-ready here
  });
});

describe("Microsoft Teams Only: real workflow", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("plans with the Teams target and previews exactly what will be posted, and where", async () => {
    const { user, calls } = setup([{ method: "POST", path: PLAN_URL, body: teamsPlan() }]);
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    await user.click(radio("Microsoft Teams Only"));
    expect(screen.getByLabelText("Real mode notice")).toHaveTextContent("sandbox channel 'sandbox'");
    expect(screen.getByLabelText("Real mode notice")).toHaveTextContent("cannot be unsent");
    await user.type(screen.getByLabelText("Instruction"), INSTRUCTION);
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    const review = await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === PLAN_URL)?.body).toEqual({ instruction: INSTRUCTION, mode: "real", target: "teams" });
    const messages = within(review).getByRole("list", { name: "Microsoft Teams messages" });
    const item = within(messages).getByRole("listitem");
    expect(item).toHaveTextContent("Step 1 · Send message → channel sandbox");
    expect(item).toHaveTextContent(`${MESSAGE.length} / 4000 characters`);
    expect(item.querySelector("p.whitespace-pre-wrap")?.textContent).toBe(MESSAGE);
    expect(within(review).getByText(/cannot be unsent/)).toBeInTheDocument();
    assertNoWebhookUrl();
  });

  it("requires the Teams phrase, approver, checkbox and operator token, then approves exactly the Teams target", async () => {
    const { user, calls } = setup([
      { method: "POST", path: PLAN_URL, body: teamsPlan() },
      { method: "POST", path: EXECUTIONS, status: 202, body: started("real") },
    ]);
    await planTeams(user, "Real");
    const approval = screen.getByRole("region", { name: "3. Approve REAL Microsoft Teams message" });
    expect(approval.textContent).not.toMatch(/Notion|Google Drive/);
    expect(within(approval).getByText("This posts a real message to Microsoft Teams")).toBeInTheDocument();
    expect(approval.textContent).toMatch(/cannot be unsent, edited or deleted/);
    expect(approval.textContent).toMatch(/retried only when the webhook did not receive it/);
    const button = within(approval).getByRole("button", { name: "Send 1 Teams Message" });

    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.type(within(approval).getByLabelText(/to confirm/), "UPLOAD TO DRIVE");
    await user.click(within(approval).getByLabelText(/approve posting 1 real Teams message to sandbox channel/));
    expect(button).toBeDisabled(); // wrong phrase, no token
    await user.clear(within(approval).getByLabelText(/to confirm/));
    await user.type(within(approval).getByLabelText(/to confirm/), "SEND TEAMS MESSAGE");
    expect(button).toBeDisabled(); // still no operator token
    await user.type(within(approval).getByLabelText("Operator token"), OPERATOR_TOKEN);
    expect(button).toBeEnabled();
    expect(calls.some((c) => c.url === EXECUTIONS)).toBe(false);

    await user.click(button);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const start = calls.find((c) => c.url === EXECUTIONS);
    expect(start?.headers["X-Integration-Authorization"]).toBe(OPERATOR_TOKEN);
    const body = start?.body as { plan: { integration_target: string }; approval: { integration_target: string; mode: string; approved_by: string } };
    expect(body.plan.integration_target).toBe("teams");
    expect(body.approval).toMatchObject({ integration_target: "teams", mode: "real", approved_by: "synthetic.reviewer" });
  }, 15_000); // long UI flow: generous timeout under parallel load

  it("shows Teams-specific live status (retry, then accepted) and a final report with no rollback claim", async () => {
    const { user } = setup([
      { method: "POST", path: PLAN_URL, body: teamsPlan() },
      { method: "POST", path: EXECUTIONS, status: 202, body: started("real") },
      { path: started("real").result_url, body: report("real", REAL_OUTPUT) },
    ]);
    await planTeams(user, "Real");
    const approval = screen.getByRole("region", { name: "3. Approve REAL Microsoft Teams message" });
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.type(within(approval).getByLabelText(/to confirm/), "SEND TEAMS MESSAGE");
    await user.type(within(approval).getByLabelText("Operator token"), OPERATOR_TOKEN);
    await user.click(within(approval).getByLabelText(/approve posting/));
    await user.click(within(approval).getByRole("button", { name: "Send 1 Teams Message" }));
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const source = FakeEventSource.latest();
    act(() => {
      source.open();
      source.emit(tev("workflow_started", null, { step_count: 1, mode: "real" }));
      source.emit(tev("step_started", 1, { attempt: 1, integration: "teams", action: "send_message" }));
      source.emit(tev("step_failed", 1, { attempt: 1, error_code: "WEBHOOK_UNREACHABLE", will_retry: true, integration: "teams", action: "send_message" }));
      source.emit(tev("retry_scheduled", 1, { phase: "execution", next_attempt: 2, delay_seconds: 0.5 }));
    });
    const monitor = screen.getByRole("region", { name: "4. Live execution" });
    expect(within(monitor).getByText(/retrying \(no message was posted\)/)).toBeInTheDocument();
    act(() => {
      source.emit(tev("step_started", 1, { attempt: 2, integration: "teams", action: "send_message" }));
      source.emit(tev("step_succeeded", 1, { attempt: 2, resource_id: REF, replayed: false, integration: "teams", action: "send_message", effect: "irreversible" }));
      source.emit(tev("workflow_completed", null, { status: "COMPLETED" }));
    });
    expect(within(monitor).getByText(/Accepted by the Teams webhook/)).toBeInTheDocument();
    expect(within(monitor).getByText(`Message reference: ${REF}`)).toBeInTheDocument();
    expect(within(monitor).getByText("Step 1 succeeded · Microsoft Teams message")).toBeInTheDocument();

    const final = await screen.findByRole("region", { name: "5. Execution report" });
    await within(final).findByText("Sent to Microsoft Teams");
    const [item] = within(within(final).getByRole("list", { name: "Resource references" })).getAllByRole("listitem");
    expect(within(item!).getByText("Message reference")).toBeInTheDocument();
    expect(within(item!).getByText("sandbox")).toBeInTheDocument();
    expect(within(item!).getByText(/Accepted by the Teams Workflows webhook/)).toBeInTheDocument();
    expect(within(item!).getByText(/cannot unsend or delete Teams messages/)).toBeInTheDocument();
    expect(final.textContent).not.toMatch(/rolled back|Page ID|Notion|delivered/i);
    assertNoWebhookUrl();
  }, 15_000); // long UI flow: generous timeout under parallel load
});

describe("Microsoft Teams Only: mock and dry run", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("mock: approval says nothing is posted and sends no operator token", async () => {
    const mockStatus = { ...STATUS_MOCK, teams_sandbox: { ...TEAMS_REAL_READY, transport: "mock" as const, channel_alias: null, dry_run_ready: false, real_ready: false } };
    const { user, calls } = setup([
      { method: "POST", path: PLAN_URL, body: teamsPlan("mock") },
      { method: "POST", path: EXECUTIONS, status: 202, body: started("mock") },
    ], mockStatus);
    const review = await planTeams(user, "Mock");
    expect(within(review).getByText(/delivery is simulated in memory/)).toBeInTheDocument();
    expect(screen.getByLabelText("Workflow notice")).toHaveTextContent("Mock mode simulates delivery");
    const approval = screen.getByRole("region", { name: "3. Approve mock execution" });
    expect(approval.textContent).toMatch(/No real Teams message is sent\./);
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(within(approval).getByRole("checkbox"));
    await user.click(within(approval).getByRole("button", { name: "Approve and Execute Mock Workflow" }));
    await waitFor(() => expect(calls.some((c) => c.url === EXECUTIONS)).toBe(true));
    expect(calls.find((c) => c.url === EXECUTIONS)?.headers["X-Integration-Authorization"]).toBeUndefined();
  });

  it("dry run: previews the webhook request for the sandbox channel and reports it as not sent", async () => {
    const { user } = setup([
      { method: "POST", path: PLAN_URL, body: teamsPlan("dry_run") },
      { method: "POST", path: EXECUTIONS, status: 202, body: started("dry_run") },
      { path: started("dry_run").result_url, body: report("dry_run", DRY_OUTPUT) },
    ]);
    await planTeams(user, "Dry Run");
    const approval = screen.getByRole("region", { name: "3. Approve dry run" });
    expect(approval.textContent).toMatch(/payload is built for channel sandbox; no webhook is called/);
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(within(approval).getByRole("checkbox"));
    await user.click(within(approval).getByRole("button", { name: "Approve and Run Dry Run" }));
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    act(() => {
      const source = FakeEventSource.latest();
      source.open();
      source.emit(tev("workflow_started", null, { step_count: 1, mode: "dry_run" }));
      source.emit(tev("step_started", 1, { attempt: 1, integration: "teams", action: "send_message" }));
      source.emit(tev("step_succeeded", 1, { attempt: 1, resource_id: REF, replayed: false, integration: "teams", action: "send_message" }));
      source.emit(tev("workflow_completed", null, { status: "COMPLETED" }));
    });
    expect(screen.getByText("Request built and checked; not sent (dry run).")).toBeInTheDocument();
    const final = await screen.findByRole("region", { name: "5. Execution report" });
    await within(final).findByText("teams_webhook (not sent)");
    expect(within(final).getByText("Resource references")).toBeInTheDocument();
    expect(final.textContent).not.toMatch(/Accepted by the Teams Workflows webhook|Sent to Microsoft Teams/);
    assertNoWebhookUrl();
  });
});

describe("Teams-specific wording helpers", () => {
  const view = (status: StepView["status"], lastErrorCode: string | null = null): StepView => ({
    step_number: 1, integration: "teams", action: "send_message", status, attempt: 1, lastErrorCode,
    retryDelaySeconds: null, resourceId: null, compensationStatus: null, title: null, verification: null,
  });

  it("never claims more than the webhook reports", () => {
    expect(teamsStepNote(view("succeeded"), "real")).toMatch(/Accepted by the Teams webhook \(Microsoft does not confirm/);
    expect(teamsStepNote(view("succeeded"), "mock")).toMatch(/simulated/);
    expect(teamsStepNote(view("failed", "OUTCOME_UNKNOWN"), "real")).toMatch(/Outcome unknown.*Not retried, to avoid a duplicate/);
    expect(teamsStepNote(view("failed", "WEBHOOK_UNAUTHORIZED"), "real")).toBe("Not posted.");
    expect(teamsStepNote(view("not_reversible"), "real")).toMatch(/cannot be unsent/);
    expect(teamsStepNote({ ...view("succeeded"), integration: "notion" }, "real")).toBeNull();
  });

  it("labels a Teams result as a message reference and only notes acceptance in real mode", () => {
    expect(resourceKind("teams", "send_message", "real")).toMatchObject({ idLabel: "Message reference", creates: true });
    expect(resourceKind("teams", "send_message", "real").keptNote).toMatch(/cannot unsend or delete/);
  });

  it("the plan preview shows the server default when no channel is given", () => {
    const plan = teamsPlan("mock");
    plan.planned_steps = [{ ...plan.planned_steps[0]!, arguments: { message: "hi" } }];
    render(<TeamsMessagePreview plan={plan} mode="mock" />);
    expect(screen.getByText(/channel/)).toHaveTextContent("(server default)");
  });
});
