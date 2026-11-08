import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { IntegrationsStatus, PlanResponse } from "./api/types";
import { REAL_MODE_SANDBOX_ONLY, customPlanBlockedReason } from "./state/modes";
import {
  CONNECTIVITY_FAIL,
  CONNECTIVITY_URL,
  DEFAULT_ROUTES,
  DRIVE_CONNECTIVITY_PASS,
  DRIVE_CONNECTIVITY_URL,
  FakeEventSource,
  PLANNED,
  STATUS_REAL_DRIVE,
  STATUS_REAL_GEMINI,
  STATUS_URL,
  driveStarted,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  type MockRoute,
} from "./test/doubles";
import { renderApp, type TargetName } from "./test/renderApp";

const PLAN_URL = "/api/v1/workflows/plan";
const EXECUTIONS = "/api/v1/workflows/executions";
const OPERATOR_TOKEN = "synthetic-operator-token";
const INSTRUCTION = "Create a Q3 planning folder in Drive, save a kickoff report into it, then list the folder.";

// Real-mode Drive sandbox ready, AI planner (Gemini primary) ready, Notion unavailable.
const STATUS: IntegrationsStatus = { ...STATUS_REAL_DRIVE, planner: STATUS_REAL_GEMINI.planner };

const CUSTOM_PLAN: PlanResponse = {
  ...PLANNED,
  workflow_id: driveStarted("real").workflow_id,
  planner: "groq",
  planner_model: "openai/gpt-oss-120b",
  primary_planner: "gemini",
  fallback_used: true,
  fallback_reason: "PLANNER_API_ERROR",
  integration_target: "google_drive",
  summary: "Q3 planning folder with a kickoff report.",
  planned_steps: [
    { step_number: 1, integration: "google_drive", action: "create_folder", description: "Create the Q3 planning folder", arguments: { name: "Q3 planning", folder: "sandbox" }, depends_on: [] },
    { step_number: 2, integration: "google_drive", action: "create_document", description: "Save the kickoff report", arguments: { title: "Kickoff report.md", content: "# Kickoff", parent_step: "1" }, depends_on: [1] },
    { step_number: 3, integration: "google_drive", action: "list_files", description: "List the folder", arguments: { parent_step: "1" }, depends_on: [1, 2] },
    { step_number: 4, integration: "google_drive", action: "read_document", description: "Read the report back", arguments: { document_name: "Kickoff report.md", parent_step: "1" }, depends_on: [1, 2] },
  ],
};

function setup(routes: MockRoute[] = [], status: IntegrationsStatus = STATUS, target: TargetName = "Google Drive Only") {
  const state = installFetch([
    ...DEFAULT_ROUTES,
    { path: STATUS_URL, body: status },
    { method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_FAIL },
    { method: "POST", path: DRIVE_CONNECTIVITY_URL, body: DRIVE_CONNECTIVITY_PASS },
    ...routes,
  ]);
  const user = userEvent.setup();
  renderApp(target);
  return { user, ...state };
}

const radio = (name: string) => screen.getByRole("radio", { name });

async function realCustomPlan(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(radio("Real")).toBeEnabled());
  await user.click(radio("Real"));
  await user.type(screen.getByLabelText("Instruction"), INSTRUCTION);
  await user.click(screen.getByRole("button", { name: "Generate Plan" }));
  return screen.findByRole("region", { name: "2. Review the plan" });
}

describe("Real-mode AI-generated Google Drive workflows", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("enables custom instructions in Real mode for Google Drive Only and sends the target", async () => {
    const { user, calls } = setup([{ method: "POST", path: PLAN_URL, body: CUSTOM_PLAN }]);
    await realCustomPlan(user);
    expect(calls.find((c) => c.url === PLAN_URL)?.body).toEqual({ instruction: INSTRUCTION, mode: "real", target: "google_drive" });
  });

  it("explains what a real Drive plan may do before planning", async () => {
    const { user } = setup();
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    const notice = screen.getByLabelText("Real mode notice");
    expect(notice).toHaveTextContent("Google Drive actions only");
    expect(notice).toHaveTextContent("sandbox folder 'sandbox'");
    expect(screen.getByRole("button", { name: "Generate Plan" })).toBeEnabled();
  });

  it("shows the exact plan, provider fallback and every Drive destination for review", async () => {
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: CUSTOM_PLAN }]);
    const review = await realCustomPlan(user);
    const destinations = within(review).getByRole("list", { name: "Google Drive destinations" });
    expect(within(destinations).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Step 1 · Create folder → sandbox folder 'sandbox'",
      "Step 2 · Create document → folder created by step 1",
      "Step 3 · List files → folder created by step 1",
      "Step 4 · Read document → folder created by step 1",
    ]);
    expect(within(review).getByText(/Real write: 2 Google Drive write action\(s\); 2 read-only/)).toBeInTheDocument();
    expect(within(review).getByText("Fallback: Gemini unavailable (PLANNER_API_ERROR)")).toBeInTheDocument();
    expect(within(review).getByLabelText("Exact execution request")).toHaveTextContent('"integration_target": "google_drive"');
  });

  it("requires the Drive phrase, approver, checkbox and operator token, then approves exactly the Drive target", async () => {
    const { user, calls } = setup([
      { method: "POST", path: PLAN_URL, body: CUSTOM_PLAN },
      { method: "POST", path: EXECUTIONS, status: 202, body: driveStarted("real") },
    ]);
    await realCustomPlan(user);
    const approval = screen.getByRole("region", { name: "3. Approve REAL Google Drive write" });
    expect(approval.textContent).not.toMatch(/Notion/);
    expect(within(approval).getByText("This writes to your real Google Drive")).toBeInTheDocument();
    expect(approval.textContent).toMatch(/only inside the sandbox folder sandbox/);
    const actions = within(approval).getByRole("list", { name: "Actions to approve" });
    expect(within(actions).getAllByRole("listitem")).toHaveLength(4);
    const button = within(approval).getByRole("button", { name: "Run Plan on Google Drive" });

    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.type(within(approval).getByLabelText(/to confirm/), "CREATE NOTION PAGE");
    await user.click(within(approval).getByLabelText(/approve its 2 Google Drive write action\(s\)/));
    expect(button).toBeDisabled(); // wrong phrase, no operator token
    await user.clear(within(approval).getByLabelText(/to confirm/));
    await user.type(within(approval).getByLabelText(/to confirm/), "UPLOAD TO DRIVE");
    expect(button).toBeDisabled(); // still no operator token
    await user.type(within(approval).getByLabelText("Operator token"), OPERATOR_TOKEN);
    expect(button).toBeEnabled();
    expect(calls.some((c) => c.url === EXECUTIONS)).toBe(false);

    await user.click(button);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const start = calls.find((c) => c.url === EXECUTIONS);
    expect(start?.headers["X-Integration-Authorization"]).toBe(OPERATOR_TOKEN);
    const body = start?.body as { plan: { integration_target: string }; approval: { integration_target: string; mode: string } };
    expect(body.plan.integration_target).toBe("google_drive");
    expect(body.approval).toMatchObject({ integration_target: "google_drive", mode: "real" });
  });

  it("shows a server rejection of an out-of-sandbox plan and offers no approval", async () => {
    const rejected: PlanResponse = {
      ...CUSTOM_PLAN,
      status: "INVALID",
      planned_steps: [],
      validation: { is_valid: false, issues: [{ code: "DESTINATION_NOT_APPROVED", message: "Step 1 folder 'reports' is not an approved destination (allowed: sandbox, or parent_step of a folder created by this plan).", step_number: 1, field: "arguments.folder" }] },
    };
    const { user, calls } = setup([{ method: "POST", path: PLAN_URL, status: 422, body: rejected }]);
    const review = await realCustomPlan(user);
    expect(within(review).getByText("DESTINATION_NOT_APPROVED")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run Plan on Google Drive|Approve/ })).not.toBeInTheDocument();
    expect(calls.some((c) => c.url === EXECUTIONS)).toBe(false);
  });

  it("refuses approval when a returned plan leaves the Drive target", async () => {
    const tampered: PlanResponse = {
      ...CUSTOM_PLAN,
      planned_steps: [...CUSTOM_PLAN.planned_steps, { step_number: 5, integration: "notion", action: "create_page", description: "x", arguments: { title: "t", content: "c" }, depends_on: [] }],
    };
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: tampered }]);
    await realCustomPlan(user);
    expect(screen.getByText("This plan cannot be approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run Plan on Google Drive" })).not.toBeInTheDocument();
  });

  it("keeps custom instructions blocked in Real mode for other targets", async () => {
    const { user } = setup([], { ...STATUS, notion_sandbox: { ...STATUS.notion_sandbox } }, "Multi-Integration");
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    expect(screen.getByText(REAL_MODE_SANDBOX_ONLY)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate Plan" })).toBeDisabled();
  });
});

describe("customPlanBlockedReason", () => {
  it("allows non-real modes and blocks other real targets", () => {
    expect(customPlanBlockedReason("mock", "notion", STATUS, null)).toBeNull();
    expect(customPlanBlockedReason("real", "notion", STATUS, DRIVE_CONNECTIVITY_PASS)).toBe(REAL_MODE_SANDBOX_ONLY);
  });

  it("requires a ready Drive sandbox, an AI planner and a passing Drive check", () => {
    expect(customPlanBlockedReason("real", "google_drive", STATUS, DRIVE_CONNECTIVITY_PASS)).toBeNull();
    expect(customPlanBlockedReason("real", "google_drive", STATUS, null)).toBe("Run the read-only Drive connectivity check first.");
    const mockPlanner: IntegrationsStatus = { ...STATUS, planner: { mode: "mock", model: null, ai_ready: false, claude_ready: false, issue: "PLANNER_MODE is 'mock'." } };
    expect(customPlanBlockedReason("real", "google_drive", mockPlanner, DRIVE_CONNECTIVITY_PASS)).toMatch(/AI planner/);
    const notReady: IntegrationsStatus = { ...STATUS, google_drive_sandbox: { ...STATUS.google_drive_sandbox!, real_ready: false, issues: ["REAL_WRITES_ENABLED is false (server write kill switch)."] } };
    expect(customPlanBlockedReason("real", "google_drive", notReady, DRIVE_CONNECTIVITY_PASS)).toBe("REAL_WRITES_ENABLED is false (server write kill switch).");
  });
});

describe("server plan adjustments", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("shows references the server filled before approval", async () => {
    const adjusted: PlanResponse = {
      ...CUSTOM_PLAN,
      plan_adjustments: ["Step 4 (read_document): added parent_step=1, the folder of step 2 which creates 'Kickoff report.md'."],
    };
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: adjusted }]);
    const review = await realCustomPlan(user);
    const notes = within(review).getByRole("list", { name: "Server plan adjustments" });
    expect(notes).toHaveTextContent("Step 4 (read_document): added parent_step=1");
    expect(within(review).getByText("Step 4 · Read document → folder created by step 1")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "3. Approve REAL Google Drive write" })).toBeInTheDocument();
  });

  it("shows no adjustment box for a plan that needed none", async () => {
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: CUSTOM_PLAN }]);
    const review = await realCustomPlan(user);
    expect(within(review).queryByRole("list", { name: "Server plan adjustments" })).not.toBeInTheDocument();
  });
});
