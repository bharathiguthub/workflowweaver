import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type {
  ExecutionMode,
  ExecutionResponse,
  ExecutionStartResponse,
  IntegrationsStatus,
  OrchestrationPlanResponse,
  OrchestrationSourcesResponse,
  WorkflowStep,
} from "./api/types";
import { ExecutionReport } from "./components/ExecutionReport";
import { orchestrationAvailability } from "./components/IntegrationTargetSelector";
import { toExecutionRequest } from "./state/forms";
import {
  CONNECTIVITY_PASS,
  CONNECTIVITY_URL,
  DEFAULT_ROUTES,
  DRIVE_CONNECTIVITY_PASS,
  DRIVE_CONNECTIVITY_URL,
  FakeEventSource,
  PLANNED,
  STARTED,
  STATUS_MOCK,
  STATUS_REAL_DRIVE,
  STATUS_REAL_GEMINI,
  STATUS_URL,
  ev,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  type MockRoute,
} from "./test/doubles";
import { renderApp } from "./test/renderApp";

// Synthetic data only; fetch and EventSource are fakes. No Drive, Notion or Teams call is made.
const WF = "7c6b5a49-3827-4615-8f4e-3d2c1b0a9f8e";
const PLAN_URL = "/api/v1/workflows/orchestration/plan";
const EXECUTIONS = "/api/v1/workflows/executions";
const DIGEST = "ab".repeat(32);
const SOURCE_ID = "mock-src-crp-plan-v2";
const NAME = "Customer Rewards Platform implementation plan.md";
const TOKEN = "synthetic-operator-token";
const PROJECT_PAGE = "5e1f0000-0000-4000-9000-000000000101";
const sourcesUrl = (mode: ExecutionMode) => `/api/v1/workflows/orchestration/sources?mode=${mode}`;
const tev = (...args: Parameters<typeof ev>) => ({ ...ev(...args), workflow_id: WF });

const SOURCES: OrchestrationSourcesResponse = {
  mode: "mock", simulated: true, folder_alias: "sandbox",
  sources: [
    { file_id: SOURCE_ID, name: NAME, mime_type: "text/markdown", size_bytes: 2844, modified_time: "2026-11-06T09:30:00Z", duplicate_name: true },
    { file_id: "mock-src-crp-plan-v1", name: NAME, mime_type: "text/markdown", size_bytes: 512, modified_time: "2026-10-20T14:05:00Z", duplicate_name: true },
  ],
};

const PHASES = ["Customer registration", "Reward balance lookup", "Points earning", "Points redemption", "Transaction history"];

function steps(teamsChannel: string | null, notionParent: string | null): WorkflowStep[] {
  const read: WorkflowStep = { step_number: 1, integration: "google_drive", action: "read_document", description: "Read the approved source",
    arguments: { document_name: NAME, folder: "sandbox", file_id: SOURCE_ID, expected_sha256: "ef".repeat(32) }, depends_on: [] };
  const project: WorkflowStep = { step_number: 2, integration: "notion", action: "create_page", description: "Create the project page",
    arguments: { title: "Customer Rewards Platform", content: "Summary", ...(notionParent ? { parent: notionParent } : {}) }, depends_on: [1] };
  const children: WorkflowStep[] = PHASES.map((p, i) => ({ step_number: i + 3, integration: "notion", action: "create_page",
    description: `Create phase ${i + 1}`, arguments: { title: `Phase ${i + 1}: ${p}`, content: "Objectives:\n- x", parent_step: "2" }, depends_on: [2] }));
  const teams: WorkflowStep = { step_number: 8, integration: "teams", action: "send_message", description: "Notify Teams",
    arguments: { message: `WorkflowWeaver: Customer Rewards orchestration\nProject: Customer Rewards Platform\nPhases created: 5\nExecution reference: ${WF}`,
      link_step: "2", ...(teamsChannel ? { channel: teamsChannel } : {}) }, depends_on: [2, 3, 4, 5, 6, 7] };
  return [read, project, ...children, teams];
}

function orchestrationPlan(mode: ExecutionMode): OrchestrationPlanResponse {
  const real = mode !== "mock";
  return {
    ...PLANNED, workflow_id: WF, planner: mode === "mock" ? "mock (not AI)" : "gemini", integration_target: "drive_notion_teams",
    summary: "Customer Rewards Platform: read, create pages, notify.", planned_steps: steps(real ? "sandbox" : null, real ? "engineering" : null),
    mode, workflow_name: "Customer Rewards orchestration (Drive -> Notion -> Teams)", model: mode === "mock" ? null : "gemini-test-flash",
    input_mode: "document_only", input_digest: "cd".repeat(32), instructions: null, instruction_notes: [],
    ai_generated: mode !== "mock", plan_digest: DIGEST,
    source: { file_id: SOURCE_ID, name: NAME, mime_type: "text/markdown", size_bytes: 2844, modified_time: "2026-11-06T09:30:00Z",
      sha256: "ef".repeat(32), simulated: mode !== "real" },
    extraction: {
      project_title: "Customer Rewards Platform", summary: "Customers register, earn and redeem points.", summary_source: "document",
      phases: PHASES.map((title, i) => ({
        title,
        summary: `Deliver ${title.toLowerCase()} for customers.`,
        summary_source: i === 1 ? "source_text" : "document",
        sections: {
          objectives: [{ text: `Objective for ${title}`, in_source: true, origin: "document" }],
          technical_tasks: i === 0
            ? [{ text: "Build the registration API", in_source: true, origin: "document" }, { text: "Add smartwatch support", in_source: false, origin: "unverified" }]
            : [],
          deliverables: [{ text: `Deliverable for ${title}`, in_source: true, origin: "document" }],
          acceptance_criteria: [{ text: `Criterion for ${title}`, in_source: true, origin: "document" }],
        },
        priority: i === 0 ? "High" : "", priority_source: i === 0 ? "document" : "none",
        owner: i === 1 ? "Growth team" : "", owner_source: i === 1 ? "suggested" : "none",
      })),
    },
    warnings: ["Phase 'Customer registration' technical tasks: 'Add smartwatch support' was not found in the source document."],
    stages: [
      { stage: "source_validation", status: mode === "real" ? "passed" : "simulated", detail: "Sandbox folder verified" },
      { stage: "document_read", status: mode === "real" ? "passed" : "simulated", detail: "2844 bytes, SHA-256 efefefefefef" },
      { stage: "extraction", status: "passed", detail: "gemini (gemini-test-flash): 5 phase(s)" },
      { stage: "plan_validation", status: "passed", detail: "Plan valid" },
    ],
    checks: mode === "real"
      ? { performed: ["Sandbox folder verified (live, WorkflowWeaver marker)", "AI extraction (gemini)"], simulated: [] }
      : { performed: ["Plan validation: allow-list"], simulated: ["Google Drive read (synthetic source fixture)"] },
    destinations: { drive_folder: "sandbox", notion_parent: real ? "engineering" : null, teams_channel: real ? "sandbox" : null },
    write_summary: { notion_pages: 6, teams_messages: 1, drive_writes: 0 },
    real_write: mode === "real",
  };
}

const ORCH_READY = { mock_ready: true, dry_run_ready: true, real_ready: true, issues: [] };
const STATUS_REAL: IntegrationsStatus = {
  ...STATUS_REAL_DRIVE, planner: STATUS_REAL_GEMINI.planner,
  notion_sandbox: { ...STATUS_REAL_DRIVE.notion_sandbox, parent_alias: "engineering", real_ready: true, dry_run_ready: true, issues: [] },
  teams_sandbox: { transport: "webhook", channel_alias: "sandbox", sandbox_only: true, mock_ready: true, dry_run_ready: true, real_ready: true, issues: [] },
  orchestration: ORCH_READY,
};
const STATUS_MOCK_ORCH: IntegrationsStatus = { ...STATUS_MOCK, orchestration: { ...ORCH_READY, dry_run_ready: false, real_ready: false } };

function started(mode: ExecutionMode): ExecutionStartResponse {
  return { ...STARTED, workflow_id: WF, mode, stream_url: `/api/v1/workflows/executions/${WF}/events?access_token=synthetic-token`,
    result_url: `/api/v1/workflows/executions/${WF}` };
}

function setup(routes: MockRoute[], status: IntegrationsStatus) {
  const state = installFetch([
    ...DEFAULT_ROUTES,
    { path: STATUS_URL, body: status },
    { method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_PASS },
    { method: "POST", path: DRIVE_CONNECTIVITY_URL, body: DRIVE_CONNECTIVITY_PASS },
    ...routes,
  ]);
  const user = userEvent.setup();
  renderApp("business");
  return { user, ...state };
}

const radio = (name: string) => screen.getByRole("radio", { name });

/** Business Workflows hosts the Drive + Notion + Teams workflow; wait until the backend status is loaded. */
async function waitForOrchestration() {
  await waitFor(() => expect(screen.queryByText(/does not offer the Google Drive \+ Notion \+ Teams workflow/)).not.toBeInTheDocument());
}

async function planIn(user: ReturnType<typeof userEvent.setup>, mode: "Mock" | "Dry Run" | "Real") {
  if (mode !== "Mock") {
    await waitFor(() => expect(radio(mode)).toBeEnabled());
    await user.click(radio(mode));
  }
  await waitForOrchestration();
  const list = await screen.findByRole("list", { name: "Source documents" });
  await user.click(within(list).getByRole("radio", { name: `${NAME} (${SOURCE_ID})` }));
  await user.click(screen.getByRole("button", { name: "Read Source and Extract Plan" }));
  return screen.findByRole("region", { name: "2. Review the plan" });
}

describe("Google Drive + Notion + Teams: target and source selection", () => {
  it("is offered per mode only when the backend reports it ready", () => {
    expect(orchestrationAvailability(STATUS_MOCK_ORCH, "mock").available).toBe(true);
    expect(orchestrationAvailability(STATUS_MOCK_ORCH, "real").available).toBe(false);
    expect(orchestrationAvailability(STATUS_MOCK, "mock")).toEqual({
      available: false, reason: "This backend does not offer the Google Drive + Notion + Teams workflow." });
    const blocked = { ...STATUS_REAL, orchestration: { ...ORCH_READY, real_ready: false, issues: ["REAL_WRITES_ENABLED is false (server write kill switch)."] } };
    expect(orchestrationAvailability(blocked, "real").reason).toMatch(/REAL_WRITES_ENABLED/);
  });

  it("lists sources by id, flags duplicate names, hides free-form planning and sends the selected id", async () => {
    const { user, calls } = setup([
      { path: sourcesUrl("mock"), body: SOURCES },
      { method: "POST", path: PLAN_URL, body: orchestrationPlan("mock") },
    ], STATUS_MOCK_ORCH);
    await waitForOrchestration();
    const list = await screen.findByRole("list", { name: "Source documents" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getAllByText(/Another file has the same name/)).toHaveLength(2);
    expect(screen.getByText(/simulated: synthetic fixtures/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Instruction")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Read Source and Extract Plan" })).toBeDisabled();
    await user.click(within(list).getByRole("radio", { name: `${NAME} (${SOURCE_ID})` }));
    await user.click(screen.getByRole("button", { name: "Read Source and Extract Plan" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === PLAN_URL)?.body).toEqual({ source_file_id: SOURCE_ID, mode: "mock", expected_phases: 5 });
  });
});

describe("Google Drive + Notion + Teams: plan review and approval", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("shows each phase's source-grounded summary and marks summaries copied from the source", async () => {
    const { user } = setup([
      { path: sourcesUrl("mock"), body: SOURCES },
      { method: "POST", path: PLAN_URL, body: orchestrationPlan("mock") },
    ], STATUS_MOCK_ORCH);
    const review = await planIn(user, "Mock");
    const phases = within(review).getByRole("list", { name: "Extracted phases" });
    expect(within(phases).getByLabelText("Phase 1 summary")).toHaveTextContent("Deliver customer registration for customers.");
    expect(within(phases).getByLabelText("Phase 1 summary")).not.toHaveTextContent("copied from the source document");
    expect(within(phases).getByLabelText("Phase 2 summary")).toHaveTextContent(
      "Deliver reward balance lookup for customers. (copied from the source document)");
  });

  it("shows the source, stages, checks, extracted phases, Notion tree, Teams preview and dependencies", async () => {
    const { user } = setup([
      { path: sourcesUrl("mock"), body: SOURCES },
      { method: "POST", path: PLAN_URL, body: orchestrationPlan("mock") },
    ], STATUS_MOCK_ORCH);
    const review = await planIn(user, "Mock");
    expect(within(review).getAllByText(SOURCE_ID).length).toBeGreaterThan(0); // source card (+ request preview)
    expect(within(review).getByText(/Simulated: a synthetic fixture/)).toBeInTheDocument();
    expect(within(within(review).getByRole("list", { name: "Planning stages" })).getAllByRole("listitem")).toHaveLength(4);
    expect(within(review).getByRole("list", { name: "Simulated steps" })).toHaveTextContent("synthetic source fixture");
    const phases = within(review).getByRole("list", { name: "Extracted phases" });
    expect(within(phases).getAllByRole("listitem").filter((li) => li.parentElement === phases)).toHaveLength(5);
    expect(within(phases).getByText("(not found in source)")).toBeInTheDocument();
    expect(within(phases).getByText(/Owner: Growth team \(AI suggestion, not in source\)/)).toBeInTheDocument();
    expect(within(phases).getByText(/Priority: High \(from source\)/)).toBeInTheDocument();
    expect(within(review).getByRole("list", { name: "Extraction warnings" })).toHaveTextContent("smartwatch");
    const pages = within(review).getByRole("list", { name: "Notion pages" });
    expect(pages).toHaveTextContent("Step 2 · Customer Rewards Platform");
    expect(pages).toHaveTextContent("Step 7 · Phase 5: Transaction history (child of step 2)");
    const teams = within(review).getByRole("list", { name: "Microsoft Teams messages" });
    expect(teams).toHaveTextContent("a link to the Notion page created in step 2");
    expect(within(review).getByRole("list", { name: "Step dependencies" })).toHaveTextContent(
      "Step 8 · Microsoft Teams send message ← after step(s) 2, 3, 4, 5, 6, 7");
    expect(within(review).getByText(/0 Google Drive write\(s\)/)).toBeInTheDocument();
  });

  it("mock approval binds the plan digest and the combined target", async () => {
    const { user, calls } = setup([
      { path: sourcesUrl("mock"), body: SOURCES },
      { method: "POST", path: PLAN_URL, body: orchestrationPlan("mock") },
      { method: "POST", path: EXECUTIONS, status: 202, body: started("mock") },
    ], STATUS_MOCK_ORCH);
    await planIn(user, "Mock");
    const approval = screen.getByRole("region", { name: "3. Approve mock execution" });
    expect(approval.textContent).toMatch(/No real Google Drive read, Notion page or Teams message happens/);
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(within(approval).getByRole("checkbox"));
    await user.click(within(approval).getByRole("button", { name: "Approve and Execute Mock Workflow" }));
    await waitFor(() => expect(calls.some((c) => c.url === EXECUTIONS)).toBe(true));
    const body = calls.find((c) => c.url === EXECUTIONS)?.body as { approval: Record<string, unknown>; plan: { integration_target: string } };
    expect(body.approval).toMatchObject({ integration_target: "drive_notion_teams", plan_digest: DIGEST, mode: "mock" });
    expect(body.plan.integration_target).toBe("drive_notion_teams");
  });

  it("dry run explains that requests are built but nothing is read or sent", async () => {
    const { user } = setup([
      { path: sourcesUrl("dry_run"), body: { ...SOURCES, mode: "dry_run" } },
      { method: "POST", path: PLAN_URL, body: orchestrationPlan("dry_run") },
    ], STATUS_REAL);
    await planIn(user, "Dry Run");
    const approval = screen.getByRole("region", { name: "3. Approve dry run" });
    expect(approval.textContent).toMatch(/dry run never contacts Google Drive/);
    expect(approval.textContent).toMatch(/parent engineering and channel sandbox, but nothing is sent/);
  });

  it("real approval needs BOTH phrases, the approver, the checkbox and the operator token", async () => {
    const { user, calls } = setup([
      { path: sourcesUrl("real"), body: { ...SOURCES, mode: "real", simulated: false } },
      { method: "POST", path: PLAN_URL, body: orchestrationPlan("real") },
      { method: "POST", path: EXECUTIONS, status: 202, body: started("real") },
    ], STATUS_REAL);
    await planIn(user, "Real");
    const approval = screen.getByRole("region", { name: "3. Approve REAL Notion pages and Teams notification" });
    expect(within(approval).getByText("This writes to Notion and posts to Microsoft Teams")).toBeInTheDocument();
    expect(approval.textContent).toMatch(/only after every page succeeded is 1 Teams message posted/);
    expect(approval.textContent).toMatch(/cannot be unsent/);
    const button = within(approval).getByRole("button", { name: "Create Notion Pages and Notify Teams" });
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.type(within(approval).getByLabelText(/to confirm the Notion pages/), "CREATE NOTION PAGE");
    await user.click(within(approval).getByLabelText(/approve creating 6 real Notion pages under sandbox parent engineering, then posting 1 real Teams message to sandbox channel sandbox/));
    await user.type(within(approval).getByLabelText("Operator token"), TOKEN);
    expect(button).toBeDisabled(); // the Teams phrase is still missing
    await user.type(within(approval).getByLabelText(/to confirm the Teams notification/), "SEND TEAMS MESSAGE");
    expect(button).toBeEnabled();
    await user.click(button);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const start = calls.find((c) => c.url === EXECUTIONS);
    expect(start?.headers["X-Integration-Authorization"]).toBe(TOKEN);
    expect((start?.body as { approval: Record<string, unknown> }).approval).toMatchObject({ plan_digest: DIGEST, mode: "real" });
  }, 15_000);

  it("streams approval, verification and the notification, then reports each integration correctly", async () => {
    const report: ExecutionResponse = {
      workflow_id: WF, status: "COMPLETED", mode: "real", validation: { is_valid: true, issues: [] },
      step_results: [
        { step_number: 1, integration: "google_drive", action: "read_document", status: "SUCCEEDED", attempts: 1, resource_id: "1SyntheticSourceDocument00001",
          output: { document_name: NAME, file_id: "1SyntheticSourceDocument00001", source_verified: true, sha256: "ef".repeat(32) }, replayed: false, error: null },
        ...[2, 3, 4, 5, 6, 7].map((n) => ({ step_number: n, integration: "notion", action: "create_page", status: "SUCCEEDED" as const, attempts: 1,
          resource_id: n === 2 ? PROJECT_PAGE : `5e1f0000-0000-4000-9000-00000000010${n}`,
          output: { title: n === 2 ? "Customer Rewards Platform" : `Phase ${n - 2}`, verification: "verified",
            url: `https://www.notion.so/page${n}` }, replayed: false, error: null })),
        { step_number: 8, integration: "teams", action: "send_message", status: "SUCCEEDED", attempts: 1, resource_id: "teams-msg-0001",
          output: { channel: "sandbox", accepted: true, delivery: "accepted_by_webhook", http_status: 202, notion_link_included: true }, replayed: false, error: null },
      ],
      attempts: Object.fromEntries([1, 2, 3, 4, 5, 6, 7, 8].map((n) => [String(n), 1])), retry_delays_seconds: [], errors: [],
      compensation_results: [], replayed: false, message: "All steps completed against the configured services.",
    };
    const { user } = setup([
      { path: sourcesUrl("real"), body: { ...SOURCES, mode: "real", simulated: false } },
      { method: "POST", path: PLAN_URL, body: orchestrationPlan("real") },
      { method: "POST", path: EXECUTIONS, status: 202, body: started("real") },
      { path: started("real").result_url, body: report },
    ], STATUS_REAL);
    await planIn(user, "Real");
    const approval = screen.getByRole("region", { name: "3. Approve REAL Notion pages and Teams notification" });
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.type(within(approval).getByLabelText(/to confirm the Notion pages/), "CREATE NOTION PAGE");
    await user.type(within(approval).getByLabelText(/to confirm the Teams notification/), "SEND TEAMS MESSAGE");
    await user.type(within(approval).getByLabelText("Operator token"), TOKEN);
    await user.click(within(approval).getByRole("checkbox"));
    await user.click(within(approval).getByRole("button", { name: "Create Notion Pages and Notify Teams" }));
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    act(() => {
      const s = FakeEventSource.latest();
      s.open();
      s.emit(tev("approval_verified", null, { plan_digest: DIGEST.slice(0, 16), mode: "real", write_count: 7 }));
      s.emit(tev("workflow_started", null, { step_count: 8, mode: "real" }));
      s.emit(tev("step_started", 1, { attempt: 1, integration: "google_drive", action: "read_document" }));
      s.emit(tev("step_succeeded", 1, { attempt: 1, resource_id: "1SyntheticSourceDocument00001", integration: "google_drive", action: "read_document" }));
      for (const n of [2, 3, 4, 5, 6, 7]) {
        s.emit(tev("step_started", n, { attempt: 1, integration: "notion", action: "create_page" }));
        s.emit(tev("resource_verified", n, { status: "verified", integration: "notion", action: "create_page" }));
        s.emit(tev("step_succeeded", n, { attempt: 1, resource_id: `page-${n}`, integration: "notion", action: "create_page", verification: "verified" }));
      }
      s.emit(tev("step_started", 8, { attempt: 1, integration: "teams", action: "send_message" }));
      s.emit(tev("step_succeeded", 8, { attempt: 1, resource_id: "teams-msg-0001", integration: "teams", action: "send_message" }));
      s.emit(tev("workflow_completed", null, { status: "COMPLETED" }));
    });
    const monitor = screen.getByRole("region", { name: "4. Live execution" });
    expect(within(monitor).getByText(/Approval verified: bound to the reviewed plan/)).toBeInTheDocument();
    expect(within(monitor).getAllByText("Read back: verified in Notion.")).toHaveLength(6);
    expect(within(monitor).getAllByText(/read-back verified/)).toHaveLength(6);
    expect(within(monitor).getByText(/Accepted by the Teams webhook/)).toBeInTheDocument();

    const final = await screen.findByRole("region", { name: "5. Execution report" });
    const items = await within(final).findAllByRole("listitem");
    const refs = within(within(final).getByRole("list", { name: "Resource references" })).getAllByRole("listitem");
    expect(items.length).toBeGreaterThan(0);
    const [drive, project] = refs;
    const teams = refs[refs.length - 1]!;
    expect(within(drive!).getByText("File ID")).toBeInTheDocument();
    expect(within(drive!).getByText(/Content matched the approved source/)).toBeInTheDocument();
    expect(within(drive!).queryByText(/Notion sandbox/)).not.toBeInTheDocument();
    expect(within(project!).getByText("Page ID")).toBeInTheDocument();
    expect(within(project!).getByText(/Verified: read back/)).toBeInTheDocument();
    expect(within(project!).getByText(/exists in your Notion sandbox/)).toBeInTheDocument();
    expect(within(teams).getByText("Message reference")).toBeInTheDocument();
    expect(within(teams).getByText(/Accepted by the Teams Workflows webhook/)).toBeInTheDocument();
    expect(within(teams).queryByText(/Notion sandbox/)).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toMatch(/logic\.azure\.com|sig=/);
  }, 20_000);

  it("shows an INVALID extraction (wrong phase count) and offers no approval", async () => {
    const invalid: OrchestrationPlanResponse = {
      ...orchestrationPlan("mock"), status: "INVALID", planned_steps: [], plan_digest: null,
      validation: { is_valid: false, issues: [{ code: "PHASE_COUNT_MISMATCH", message: "The document defines 4 phase(s) but 5 were requested; phases are never invented or merged.", step_number: null, field: "phases" }] },
    };
    const { user } = setup([
      { path: sourcesUrl("mock"), body: SOURCES },
      { method: "POST", path: PLAN_URL, status: 422, body: invalid },
    ], STATUS_MOCK_ORCH);
    const review = await planIn(user, "Mock");
    expect(within(review).getByText("PHASE_COUNT_MISMATCH")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Approve/ })).not.toBeInTheDocument();
  });
});

describe("Google Drive + Notion + Teams: reporting partial success", () => {
  it("PARTIALLY_COMPLETED keeps verified pages and never claims a rollback", () => {
    const report: ExecutionResponse = {
      workflow_id: WF, status: "PARTIALLY_COMPLETED", mode: "real", validation: { is_valid: true, issues: [] },
      step_results: [
        { step_number: 2, integration: "notion", action: "create_page", status: "SUCCEEDED", attempts: 1, resource_id: PROJECT_PAGE,
          output: { title: "Customer Rewards Platform", verification: "verified" }, replayed: false, error: null },
        { step_number: 8, integration: "teams", action: "send_message", status: "FAILED", attempts: 1, resource_id: null, output: {}, replayed: false,
          error: { code: "OUTCOME_UNKNOWN", message: "The webhook may have received the request.", step_number: 8, attempt: 1, retryable: false } },
      ],
      attempts: { "2": 1, "8": 1 }, retry_delays_seconds: [],
      errors: [{ code: "OUTCOME_UNKNOWN", message: "The webhook may have received the request.", step_number: 8, attempt: 1, retryable: false }],
      compensation_results: [],
      replayed: false,
      message: "Every Notion page was created and verified and is kept; the final Microsoft Teams notification failed or its outcome is unknown (see errors). Teams messages cannot be unsent, so nothing was rolled back; check the channel before notifying again.",
    };
    render(<ExecutionReport report={report} state="loaded" error={null} terminal={null} onRetry={() => {}} />);
    expect(screen.getByText("PARTIALLY COMPLETED")).toBeInTheDocument();
    expect(screen.getByText(/check the channel before notifying again/)).toBeInTheDocument();
    expect(screen.queryByText("(rolled back)")).not.toBeInTheDocument();
    expect(screen.getByText(/exists in your Notion sandbox and was not rolled back/)).toBeInTheDocument();
    expect(screen.getAllByText(/OUTCOME_UNKNOWN/).length).toBeGreaterThan(0);
  });

  it("the execution request carries the digest only when the plan has one", () => {
    const withDigest = toExecutionRequest(orchestrationPlan("real"), "synthetic.reviewer", "real");
    expect(withDigest.approval.plan_digest).toBe(DIGEST);
    expect(toExecutionRequest(PLANNED, "synthetic.reviewer").approval.plan_digest).toBeUndefined();
  });
});
