import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { SectionId } from "./navigation";
import { navLink, renderApp, type TargetName } from "./test/renderApp";
import type { ExecutionMode } from "./api/types";
import {
  CONNECTIVITY_FAIL,
  CONNECTIVITY_PASS,
  CONNECTIVITY_URL,
  DEFAULT_ROUTES,
  FakeEventSource,
  PLANNED,
  SANDBOX_PLAN_URL,
  SANDBOX_WORKFLOW_ID,
  STATUS_REAL,
  STATUS_REAL_WRITES_OFF,
  STATUS_URL,
  SYNTHETIC_PAGE_ID,
  SYNTHETIC_PAGE_URL,
  ev,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  sandboxPlan,
  sandboxReport,
  sandboxStarted,
  type MockRoute,
} from "./test/doubles";

const OPERATOR_TOKEN = "synthetic-operator-token";

function setup(routes: MockRoute[] = [], where: TargetName | SectionId = "projects") {
  const state = installFetch([
    ...DEFAULT_ROUTES,
    { path: STATUS_URL, body: STATUS_REAL },
    { method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_PASS },
    ...routes,
  ]);
  const user = userEvent.setup();
  renderApp(where);
  return { user, ...state };
}

/** installFetch matches by method + path only, so each test adds the routes for its mode. */
const planRoute = (mode: ExecutionMode): MockRoute => ({ method: "POST", path: SANDBOX_PLAN_URL, body: sandboxPlan(mode) });
const startRoute = (mode: ExecutionMode): MockRoute => ({ method: "POST", path: "/api/v1/workflows/executions", status: 202, body: sandboxStarted(mode) });
const resultRoute = (mode: ExecutionMode): MockRoute => ({ path: sandboxStarted(mode).result_url, body: sandboxReport(mode) });

const radio = (name: string) => screen.getByRole("radio", { name });

describe("Notion sandbox test and execution modes", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("keeps mock as the default and disables modes above the backend ceiling", async () => {
    installFetch(DEFAULT_ROUTES); // mock-ceiling status
    renderApp("Multi-Integration");
    await waitFor(() => expect(screen.getByText(/INTEGRATION_MODE is 'mock'; 'dry_run'/)).toBeInTheDocument());
    expect(radio("Mock")).toBeChecked();
    expect(radio("Dry Run")).toBeDisabled();
    expect(radio("Real")).toBeDisabled();
    const templates = screen.getByRole("list", { name: "Workflow templates" });
    expect(within(templates).getAllByRole("button")).toHaveLength(3);
  });

  it("runs the read-only connectivity check once and then enables Real", async () => {
    const { calls } = setup();
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    expect(calls.filter((c) => c.url === CONNECTIVITY_URL)).toHaveLength(1);
    expect(screen.getByText("Notion reachable")).toBeInTheDocument();
    // Nothing else was written: only GETs plus the read-only probe.
    expect(calls.filter((c) => c.method === "POST").map((c) => c.url)).toEqual([CONNECTIVITY_URL]);
  });

  it("disables Real when Notion is unavailable", async () => {
    setup([{ method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_FAIL }]);
    expect(await screen.findByText("Notion unavailable")).toBeInTheDocument();
    expect(radio("Real")).toBeDisabled();
    expect(screen.getByText(/Notion is unavailable: the connectivity check did not pass/)).toBeInTheDocument();
    expect(radio("Dry Run")).toBeEnabled();
  });

  it("does not probe Notion and disables Real when the write kill switch is off", async () => {
    const { calls } = setup([{ path: STATUS_URL, body: STATUS_REAL_WRITES_OFF }]);
    await waitFor(() => expect(screen.getAllByText(/REAL_WRITES_ENABLED is false/).length).toBeGreaterThan(0));
    expect(radio("Real")).toBeDisabled();
    expect(calls.some((c) => c.url === CONNECTIVITY_URL)).toBe(false);
  });

  it("real mode: shows the exact plan, requires explicit confirmation, then shows the created page", async () => {
    const { user, calls } = setup([planRoute("real"), startRoute("real"), resultRoute("real")]);
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    expect(screen.getByText(/Real mode: sandbox writes after approval/)).toBeInTheDocument();

    // In the AI Workflow Builder, templates and multi-integration instructions are blocked in real mode.
    await user.click(navLink("AI Workflow Builder"));
    await user.click(radio("Multi-Integration"));
    expect(screen.getByText(/Real mode runs only the Notion workflows/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate Plan" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Meeting-to-Tasks/ })).toBeDisabled();
    await user.click(navLink("Project Management"));

    await user.click(screen.getByRole("button", { name: "Generate Sandbox Test Plan (Real)" }));
    expect(calls.find((c) => c.url === SANDBOX_PLAN_URL)?.body).toEqual({ mode: "real", target: "notion" });

    const review = await screen.findByRole("region", { name: "2. Review the plan" });
    expect(within(review).getByText("Mode: Real")).toBeInTheDocument();
    expect(within(review).getByText("Notion MCP API-post-page under sandbox parent 'sandbox'")).toBeInTheDocument();
    expect(within(review).getByText("Yes: one real Notion page")).toBeInTheDocument();
    const exact = within(review).getByLabelText("Exact execution request");
    expect(exact.textContent).toContain('"mode": "real"');
    expect(exact.textContent).toContain('"parent": "sandbox"');
    expect(exact.textContent).toContain(SANDBOX_WORKFLOW_ID);

    // Every confirmation input is required before the button enables.
    const create = screen.getByRole("button", { name: "Create Real Notion Page" });
    expect(create).toBeDisabled();
    await user.type(screen.getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(screen.getByLabelText(/approve creating one real Notion page under sandbox parent/));
    expect(create).toBeDisabled();
    await user.type(screen.getByLabelText(/to confirm/), "create notion page");
    await user.type(screen.getByLabelText("Operator token"), OPERATOR_TOKEN);
    expect(create).toBeDisabled(); // wrong phrase (case-sensitive)
    await user.clear(screen.getByLabelText(/to confirm/));
    await user.type(screen.getByLabelText(/to confirm/), "CREATE NOTION PAGE");
    expect(calls.some((c) => c.url === "/api/v1/workflows/executions")).toBe(false); // nothing ran yet
    expect(create).toBeEnabled();
    await user.click(create);

    const start = calls.find((c) => c.url === "/api/v1/workflows/executions");
    expect(start?.headers["X-Integration-Authorization"]).toBe(OPERATOR_TOKEN);
    expect(start?.body).toMatchObject({
      workflow_id: SANDBOX_WORKFLOW_ID,
      approval: { approved: true, mode: "real", approved_by: "synthetic.reviewer" },
    });
    expect((start?.body as { plan: { steps: unknown[] } }).plan.steps).toEqual(sandboxPlan("real").planned_steps);
    expect(screen.getByLabelText("Operator token")).toHaveValue(""); // never kept after use

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const source = FakeEventSource.latest();
    act(() => {
      source.open();
      source.emit(ev("workflow_started", null, { step_count: 1 }));
      source.emit(ev("step_started", 1, { attempt: 1, integration: "notion", action: "create_page" }));
      source.emit(ev("step_succeeded", 1, { attempt: 1, resource_id: SYNTHETIC_PAGE_ID, replayed: false }));
      source.emit(ev("workflow_completed", null, { status: "COMPLETED" }));
    });

    const report = await screen.findByRole("region", { name: "5. Execution report" });
    await within(report).findByText("Created in the real service");
    expect(within(report).getByText("COMPLETED")).toBeInTheDocument();
    expect(within(report).getByText("Mode: Real")).toBeInTheDocument();
    expect(within(report).getAllByText(SYNTHETIC_PAGE_ID).length).toBeGreaterThan(0);
    expect(within(report).getByRole("link", { name: SYNTHETIC_PAGE_URL })).toHaveAttribute("href", SYNTHETIC_PAGE_URL);
    expect(within(report).getByText(/was not rolled back/)).toBeInTheDocument();
  }, 20_000);

  it("dry run: sends no operator token and reports the unsent request", async () => {
    const { user, calls } = setup([planRoute("dry_run"), startRoute("dry_run"), resultRoute("dry_run")]);
    await waitFor(() => expect(radio("Dry Run")).toBeEnabled());
    await user.click(radio("Dry Run"));
    await user.click(screen.getByRole("button", { name: "Generate Sandbox Test Plan (Dry Run)" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    expect(screen.queryByLabelText("Operator token")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(screen.getByLabelText(/approve a dry run/));
    await user.click(screen.getByRole("button", { name: "Approve and Run Dry Run" }));

    const start = calls.find((c) => c.url === "/api/v1/workflows/executions");
    expect(start?.headers["X-Integration-Authorization"]).toBeUndefined();
    expect((start?.body as { approval: { mode: string } }).approval.mode).toBe("dry_run");
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    act(() => FakeEventSource.latest().emit(ev("workflow_completed", null, { status: "COMPLETED" })));
    const report = await screen.findByRole("region", { name: "5. Execution report" });
    expect(await within(report).findByText(/API-post-page/)).toHaveTextContent("(not sent)");
  });

  it("mock mode still plans templates, and changing mode discards the plan", async () => {
    const { user, calls } = setup([{ method: "POST", path: "/api/v1/workflows/plan", body: PLANNED }], "Multi-Integration");
    await waitFor(() => expect(radio("Dry Run")).toBeEnabled());
    await user.click(screen.getByRole("button", { name: /Meeting-to-Tasks/ }));
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === "/api/v1/workflows/plan")?.body).toMatchObject({ mode: "mock" });
    expect(screen.getByRole("button", { name: "Approve and Execute Mock Workflow" })).toBeInTheDocument();

    await user.click(radio("Dry Run"));
    expect(screen.queryByRole("region", { name: "2. Review the plan" })).not.toBeInTheDocument();
  });

  it("shows a backend refusal of the sandbox plan without fabricating a plan", async () => {
    const { user } = setup([
      { method: "POST", path: SANDBOX_PLAN_URL, status: 503, body: { detail: "SMOKE_TEST_NOTION_PARENT is not set (alias of a non-production sandbox page)." } },
    ]);
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    await user.click(screen.getByRole("button", { name: "Generate Sandbox Test Plan (Real)" }));
    expect(await screen.findByText("Planning request failed")).toBeInTheDocument();
    expect(screen.getByText(/SMOKE_TEST_NOTION_PARENT is not set/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create Real Notion Page" })).not.toBeInTheDocument();
  });
});
