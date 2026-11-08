import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { ExecutionMode } from "./api/types";
import { modeOptions } from "./state/modes";
import type { SectionId } from "./navigation";
import { navLink, renderApp, type TargetName } from "./test/renderApp";
import {
  CONNECTIVITY_FAIL,
  CONNECTIVITY_PASS,
  CONNECTIVITY_URL,
  DEFAULT_ROUTES,
  DRIVE_CONNECTIVITY_PASS,
  DRIVE_CONNECTIVITY_URL,
  DRIVE_PLAN_URL,
  FakeEventSource,
  PROJECT_PLAN_URL,
  STATUS_REAL,
  STATUS_REAL_CLAUDE,
  STATUS_REAL_DRIVE,
  STATUS_REAL_DRIVE_UNAUTHORIZED,
  STATUS_URL,
  drivePlan,
  driveStarted,
  installFakeEventSource,
  installFetch,
  projectPlan,
  resetEventIds,
  type MockRoute,
} from "./test/doubles";

const OPERATOR_TOKEN = "synthetic-operator-token";
const EXECUTIONS = "/api/v1/workflows/executions";

function setup(routes: MockRoute[] = [], target: TargetName | SectionId = "documents") {
  const state = installFetch([
    ...DEFAULT_ROUTES,
    { path: STATUS_URL, body: STATUS_REAL_DRIVE },
    // Notion is unavailable: real mode must come from the Drive sandbox check alone.
    { method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_FAIL },
    { method: "POST", path: DRIVE_CONNECTIVITY_URL, body: DRIVE_CONNECTIVITY_PASS },
    ...routes,
  ]);
  const user = userEvent.setup();
  renderApp(target);
  return { user, ...state };
}

const radio = (name: string) => screen.getByRole("radio", { name });
const drivePanel = () => screen.getByRole("region", { name: "Google Drive Sandbox Test" });
const planRoute = (mode: ExecutionMode): MockRoute => ({ method: "POST", path: DRIVE_PLAN_URL, body: drivePlan(mode) });

async function realDrivePlan(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(radio("Real")).toBeEnabled());
  await user.click(radio("Real"));
  const generate = () => within(drivePanel()).getByRole("button", { name: "Generate Drive Sandbox Plan (Real)" });
  await waitFor(() => expect(generate()).toBeEnabled());
  await user.click(generate());
  return screen.findByRole("region", { name: "2. Review the plan" });
}

describe("Google Drive sandbox test", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("runs the read-only Drive check once, shows the drive.file scope and enables Real", async () => {
    const { calls } = setup();
    expect(await screen.findByText("Drive sandbox reachable")).toBeInTheDocument();
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    expect(within(drivePanel()).getByText("Scope: drive.file only")).toBeInTheDocument();
    expect(within(drivePanel()).getByText("OAuth authorized")).toBeInTheDocument();
    expect(calls.filter((c) => c.url === DRIVE_CONNECTIVITY_URL)).toHaveLength(1);
    // Only the two read-only probes were POSTed: nothing was planned or written.
    expect(calls.filter((c) => c.method === "POST").map((c) => c.url).sort()).toEqual([CONNECTIVITY_URL, DRIVE_CONNECTIVITY_URL].sort());
  });

  it("explains a missing OAuth authorization and blocks the real Drive test", async () => {
    installFetch([
      ...DEFAULT_ROUTES,
      { path: STATUS_URL, body: STATUS_REAL_DRIVE_UNAUTHORIZED },
      { method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_PASS },
    ]);
    const user = userEvent.setup();
    renderApp("documents");
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    expect(within(drivePanel()).getByText("OAuth not authorized")).toBeInTheDocument();
    expect(within(drivePanel()).getByText(/run scripts\/google_drive_auth\.py/)).toBeInTheDocument();
    expect(within(drivePanel()).getByRole("button", { name: "Generate Drive Sandbox Plan (Real)" })).toBeDisabled();
  });

  it("reviews the real Drive plan and requires the Drive phrase, approver and operator token", async () => {
    const { user, calls } = setup([planRoute("real"), { method: "POST", path: EXECUTIONS, status: 202, body: driveStarted("real") }]);
    const review = await realDrivePlan(user);
    expect(calls.find((c) => c.url === DRIVE_PLAN_URL)?.body).toEqual({ mode: "real", target: "google_drive" });
    expect(within(review).getByText("Yes: one run folder and one report in Google Drive")).toBeInTheDocument();
    const details = within(review).getByLabelText("Execution details");
    expect(within(details).getByText("Sandbox report - report 2026-11-05 100000.md")).toBeInTheDocument();
    const steps = within(review).getByRole("list", { name: "Planned steps" });
    expect(within(steps).getByText("Create folder")).toBeInTheDocument();
    expect(within(steps).getByText("Read document")).toBeInTheDocument();
    expect(within(steps).queryByText("List files")).not.toBeInTheDocument();

    const approval = screen.getByRole("region", { name: "3. Approve REAL Google Drive write" });
    expect(within(approval).getByText("This writes to your real Google Drive")).toBeInTheDocument();
    const button = within(approval).getByRole("button", { name: "Upload Report to Google Drive" });
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.type(within(approval).getByLabelText(/to confirm/), "CREATE NOTION PAGE"); // the Notion phrase is not enough
    await user.type(within(approval).getByLabelText("Operator token"), OPERATOR_TOKEN);
    await user.click(within(approval).getByLabelText(/approve creating one run folder and uploading the report/));
    expect(button).toBeDisabled();
    expect(calls.some((c) => c.url === EXECUTIONS)).toBe(false);

    await user.clear(within(approval).getByLabelText(/to confirm/));
    await user.type(within(approval).getByLabelText(/to confirm/), "UPLOAD TO DRIVE");
    expect(button).toBeEnabled();
    await user.click(button);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const start = calls.find((c) => c.url === EXECUTIONS);
    expect(start?.headers["X-Integration-Authorization"]).toBe(OPERATOR_TOKEN);
    expect(start?.body).toMatchObject({
      workflow_id: drivePlan("real").workflow_id,
      approval: { approved: true, mode: "real", approved_by: "synthetic.reviewer" },
    });
  });

  it("uploads the reviewed AI project plan as the report", async () => {
    const { user, calls } = setup([
      { path: STATUS_URL, body: { ...STATUS_REAL_DRIVE, planner: STATUS_REAL_CLAUDE.planner } },
      { method: "POST", path: PROJECT_PLAN_URL, body: projectPlan("real") },
      planRoute("real"),
    ], "projects");
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    await user.click(screen.getByRole("button", { name: "Generate AI Project Plan (Real)" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    // The reviewed project plan is carried over to Document Management's Drive sandbox test.
    await user.click(navLink("Document Management"));
    expect(within(drivePanel()).getByText("Customer Rewards Platform - Project Plan")).toBeInTheDocument();
    await user.click(within(drivePanel()).getByRole("button", { name: "Generate Drive Sandbox Plan (Real)" }));
    await waitFor(() => expect(calls.some((c) => c.url === DRIVE_PLAN_URL)).toBe(true));
    const body = calls.find((c) => c.url === DRIVE_PLAN_URL)?.body as { mode: string; project: { project_title: string } };
    expect(body.mode).toBe("real");
    expect(body.project.project_title).toBe("Customer Rewards Platform - Project Plan");
  });

  it("plans the Drive test in mock mode with mock approval copy", async () => {
    const { user, calls } = setup([planRoute("mock")]);
    await user.click(within(drivePanel()).getByRole("button", { name: "Generate Drive Sandbox Plan (Mock)" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === DRIVE_PLAN_URL)?.body).toEqual({ mode: "mock", target: "google_drive" });
    expect(screen.getByRole("button", { name: "Approve and Execute Mock Workflow" })).toBeInTheDocument();
    expect(screen.queryByText("This writes to your real Google Drive")).not.toBeInTheDocument();
  });

  it("plans the Drive test as a dry run", async () => {
    const { user, calls } = setup([planRoute("dry_run")]);
    await waitFor(() => expect(radio("Dry Run")).toBeEnabled());
    await user.click(radio("Dry Run"));
    await user.click(within(drivePanel()).getByRole("button", { name: "Generate Drive Sandbox Plan (Dry Run)" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === DRIVE_PLAN_URL)?.body).toEqual({ mode: "dry_run", target: "google_drive" });
    expect(screen.getByRole("button", { name: "Approve and Run Dry Run" })).toBeInTheDocument();
  });
});

describe("modeOptions with the Google Drive sandbox", () => {
  it("enables Real from a passing Drive check even when Notion fails", () => {
    expect(modeOptions(STATUS_REAL_DRIVE, CONNECTIVITY_FAIL, DRIVE_CONNECTIVITY_PASS).real.available).toBe(true);
  });

  it("keeps the Notion behaviour when Drive is not ready or its check fails", () => {
    const options = modeOptions(STATUS_REAL, CONNECTIVITY_FAIL, DRIVE_CONNECTIVITY_PASS);
    expect(options.real.available).toBe(false);
    expect(options.real.reason).toBe("Notion is unavailable: the connectivity check did not pass.");
    expect(modeOptions(STATUS_REAL_DRIVE, CONNECTIVITY_FAIL, { ...DRIVE_CONNECTIVITY_PASS, status: "FAIL" }).real.available).toBe(false);
  });
});

describe("Google Drive Sandbox Test without an AI planner", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("plans the real smoke test from the server even when Groq and Gemini are unavailable", async () => {
    const noAi = {
      ...STATUS_REAL_DRIVE,
      planner: { mode: "auto" as const, model: "openai/gpt-oss-120b", ai_ready: false, claude_ready: false,
        issue: "GROQ_API_KEY is not set (shell environment or .env).", primary: "groq", fallback: "gemini",
        fallback_model: "gemini-test-flash", fallback_ready: false, fallback_issue: "GEMINI_API_KEY is not set." },
    };
    const { user, calls } = setup([{ path: STATUS_URL, body: noAi }, planRoute("real")]);
    const review = await realDrivePlan(user);
    expect(calls.find((c) => c.url === DRIVE_PLAN_URL)?.body).toEqual({ mode: "real", target: "google_drive" });
    expect(calls.some((c) => c.url === "/api/v1/workflows/plan")).toBe(false); // never the LLM planner
    const steps = within(review).getByRole("list", { name: "Planned steps" });
    expect(within(steps).getByText("Create folder")).toBeInTheDocument();
    expect(within(steps).getByText("Create document")).toBeInTheDocument();
    expect(within(steps).getByText("Read document")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "3. Approve REAL Google Drive write" })).toBeInTheDocument();
    expect(screen.getByText(/without an AI planner, so it works even when Groq and Gemini are unavailable/)).toBeInTheDocument();
  });

  it("points AI Workflow Builder users to the server-built sandbox test", async () => {
    const { user } = setup([], "builder");
    expect(screen.getByText("Testing the Google Drive sandbox connection?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open Document Management" }));
    expect(drivePanel()).toBeInTheDocument();
    await user.click(navLink("AI Workflow Builder"));
    await user.click(radio("Notion Only"));
    expect(screen.queryByText("Testing the Google Drive sandbox connection?")).not.toBeInTheDocument();
  });
});
