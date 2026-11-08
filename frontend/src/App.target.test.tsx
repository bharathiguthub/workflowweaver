import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { IntegrationsStatus, PlanResponse } from "./api/types";
import { stepsOutsideTarget } from "./api/types";
import { toExecutionRequest, toPlanRequest } from "./state/forms";
import {
  DEFAULT_ROUTES,
  DRIVE_PLAN_URL,
  FakeEventSource,
  PLANNED,
  STATUS_MOCK,
  STATUS_URL,
  drivePlan,
  driveStarted,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  type MockRoute,
} from "./test/doubles";
import type { SectionId } from "./navigation";
import { navLink, renderApp, type TargetName } from "./test/renderApp";

const PLAN_URL = "/api/v1/workflows/plan";
const EXECUTIONS = "/api/v1/workflows/executions";

function setup(routes: MockRoute[] = [], target: TargetName | SectionId = "Google Drive Only") {
  const state = installFetch([...DEFAULT_ROUTES, ...routes]);
  const user = userEvent.setup();
  renderApp(target);
  return { user, ...state };
}

const targetRadio = (name: string) => screen.getByRole("radio", { name });
const DRIVE_ONLY_PLAN: PlanResponse = {
  ...PLANNED,
  integration_target: "google_drive",
  summary: "Planned 1 step(s) using: google_drive.",
  planned_steps: [
    { step_number: 1, integration: "google_drive", action: "create_document", description: "Save a summary document to Google Drive", arguments: { title: "Synthetic summary", content: "Synthetic" }, depends_on: [] },
  ],
};

describe("Integration Target selector", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("the AI Workflow Builder defaults to Google Drive Only and keeps workspace workflows in their own sections", async () => {
    setup();
    expect(targetRadio("Google Drive Only")).toBeChecked();
    // The combined Drive + Notion + Teams workflow is never planned free-form from the builder.
    expect(screen.queryByRole("radio", { name: "Google Drive + Notion + Teams" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Google Drive Sandbox Test" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "AI Project Plan to Notion" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Notion Sandbox Test" })).not.toBeInTheDocument();
    // The multi-integration templates (they create Notion pages) are not offered for Drive only.
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Workflow templates" })).queryAllByRole("button")).toHaveLength(0));
  });

  it("disables Teams until real webhooks are configured, and offers the future multi option", async () => {
    setup();
    expect(targetRadio("Microsoft Teams Only")).toBeDisabled();
    expect(screen.getByText(/Configure TEAMS_WEBHOOK_URLS/)).toBeInTheDocument();
    expect(targetRadio("Multi-Integration")).toBeEnabled();
  });

  it("enables Teams when the backend reports configured Teams webhooks", async () => {
    const status: IntegrationsStatus = {
      ...STATUS_MOCK,
      integrations: { ...STATUS_MOCK.integrations, teams: { transport: "webhook", configured: true, destinations: {} } },
    };
    setup([{ path: STATUS_URL, body: status }]);
    await waitFor(() => expect(targetRadio("Microsoft Teams Only")).toBeEnabled());
  });

  it("Project Management shows the Notion workflows and Document Management the Drive test", async () => {
    const { user } = setup();
    await user.click(navLink("Project Management"));
    expect(screen.getByRole("region", { name: "Notion Sandbox Test" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Google Drive Sandbox Test" })).not.toBeInTheDocument();
    await user.click(navLink("Document Management"));
    expect(screen.getByRole("region", { name: "Google Drive Sandbox Test" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Notion Sandbox Test" })).not.toBeInTheDocument();
  });

  it("sends the selected target with a custom instruction", async () => {
    const { user, calls } = setup([{ method: "POST", path: PLAN_URL, body: DRIVE_ONLY_PLAN }]);
    await user.type(screen.getByLabelText("Instruction"), "Save a summary document to Google Drive.");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === PLAN_URL)?.body).toEqual({ instruction: "Save a summary document to Google Drive.", mode: "mock", target: "google_drive" });
  });

  it("changing the target discards the current plan", async () => {
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: DRIVE_ONLY_PLAN }]);
    await user.type(screen.getByLabelText("Instruction"), "Save a summary document to Google Drive.");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    await user.click(targetRadio("Notion Only"));
    expect(screen.queryByRole("region", { name: "2. Review the plan" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and Execute Mock Workflow" })).not.toBeInTheDocument();
  });
});

describe("Drive-only approval", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("lists only Google Drive actions, never mentions Notion, and approves exactly the Drive target", async () => {
    const { user, calls } = setup([
      { method: "POST", path: DRIVE_PLAN_URL, body: drivePlan("mock") },
      { method: "POST", path: EXECUTIONS, status: 202, body: driveStarted("mock") },
    ], "documents");
    await user.click(screen.getByRole("button", { name: "Generate Drive Sandbox Plan (Mock)" }));
    const approval = await screen.findByRole("region", { name: "3. Approve mock execution" });
    const actions = within(approval).getByRole("list", { name: "Actions to approve" });
    expect(within(actions).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Google Drive: Create folder",
      "Google Drive: Create document",
      "Google Drive: Read document",
    ]);
    expect(within(approval).getByText("Google Drive Only")).toBeInTheDocument();
    expect(within(approval).getByText(/No real Google Drive folder or file is created/)).toBeInTheDocument();
    expect(approval.textContent).not.toMatch(/Notion/);

    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(within(approval).getByRole("checkbox"));
    await user.click(within(approval).getByRole("button", { name: "Approve and Execute Mock Workflow" }));
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const body = calls.find((c) => c.url === EXECUTIONS)?.body as {
      plan: { integration_target: string; steps: { integration: string }[] };
      approval: { integration_target: string };
    };
    expect(body.plan.integration_target).toBe("google_drive");
    expect(body.approval.integration_target).toBe("google_drive");
    expect(new Set(body.plan.steps.map((s) => s.integration))).toEqual(new Set(["google_drive"]));
  });

  it("refuses to offer approval for a Drive-only plan that contains a Notion step", async () => {
    const tampered: PlanResponse = {
      ...DRIVE_ONLY_PLAN,
      planned_steps: [
        ...DRIVE_ONLY_PLAN.planned_steps,
        { step_number: 2, integration: "notion", action: "create_page", description: "Create a Notion page", arguments: { title: "t", content: "c" }, depends_on: [] },
      ],
    };
    const { user, calls } = setup([{ method: "POST", path: PLAN_URL, body: tampered }]);
    await user.type(screen.getByLabelText("Instruction"), "Save a summary document to Google Drive.");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    expect(await screen.findByText("This plan cannot be approved")).toBeInTheDocument();
    expect(screen.getByText(/contains actions outside Google Drive Only/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(calls.some((c) => c.url === EXECUTIONS)).toBe(false);
  });

  it("never shows Notion approval copy for a Drive-only custom plan", async () => {
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: DRIVE_ONLY_PLAN }]);
    await user.type(screen.getByLabelText("Instruction"), "Save a summary document to Google Drive.");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    const approval = await screen.findByRole("region", { name: "3. Approve mock execution" });
    expect(approval.textContent).not.toMatch(/Notion/);
  });
});

describe("target helpers", () => {
  it("toPlanRequest adds the target only when given", () => {
    expect(toPlanRequest("Save a summary to Drive.", "", "mock", "google_drive")).toEqual({ instruction: "Save a summary to Drive.", mode: "mock", target: "google_drive" });
    expect(toPlanRequest("Save a summary to Drive.", "", "mock")).toEqual({ instruction: "Save a summary to Drive.", mode: "mock" });
  });

  it("toExecutionRequest approves exactly the plan's target", () => {
    const request = toExecutionRequest(DRIVE_ONLY_PLAN, "synthetic.reviewer", "mock");
    expect(request.plan.integration_target).toBe("google_drive");
    expect(request.approval.integration_target).toBe("google_drive");
    expect(toExecutionRequest(PLANNED, "synthetic.reviewer").approval).not.toHaveProperty("integration_target");
  });

  it("stepsOutsideTarget finds steps outside the target", () => {
    expect(stepsOutsideTarget(DRIVE_ONLY_PLAN)).toEqual([]);
    expect(stepsOutsideTarget({ ...PLANNED, integration_target: "google_drive" }).map((s) => s.integration)).toEqual(["notion", "teams"]);
    expect(stepsOutsideTarget(PLANNED)).toEqual([]);
  });
});
