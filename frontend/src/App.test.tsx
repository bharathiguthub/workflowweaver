import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import App from "./App";
import {
  DEFAULT_ROUTES,
  FakeEventSource,
  INVALID,
  PLANNED,
  ROLLED_BACK_REPORT,
  STARTED,
  TEMPLATES,
  ev,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  rollbackEvents,
  type MockRoute,
} from "./test/doubles";

const PLAN_ROUTE: MockRoute = { method: "POST", path: "/api/v1/workflows/plan", body: PLANNED };
const START_ROUTE: MockRoute = { method: "POST", path: "/api/v1/workflows/executions", status: 202, body: STARTED };

function setup(routes: MockRoute[] = []) {
  const fetchState = installFetch([...DEFAULT_ROUTES, PLAN_ROUTE, START_ROUTE, ...routes]);
  const user = userEvent.setup();
  render(<App />);
  return { user, ...fetchState };
}

async function generatePlan(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Meeting-to-Tasks/ }));
  await user.click(screen.getByRole("button", { name: "Generate Plan" }));
  await screen.findByRole("heading", { name: "2. Review the plan" });
}

async function approve(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Approved by"), "synthetic.reviewer");
  await user.click(screen.getByLabelText(/approve running it in mock mode/));
  await user.click(screen.getByRole("button", { name: "Approve and Execute Mock Workflow" }));
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  return FakeEventSource.latest();
}

const stepCard = (n: number) => {
  const list = screen.getByRole("list", { name: "Step progress" });
  return within(list).getAllByRole("listitem")[n - 1]!;
};

describe("App", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("shows the header, mock indicator, API status and three templates", async () => {
    setup();
    expect(screen.getByRole("heading", { name: "WorkflowWeaver" })).toBeInTheDocument();
    expect(screen.getByText(/Mock mode: no real Notion, Drive or Teams calls/)).toBeInTheDocument();
    expect(await screen.findByText("API connected · v0.3.0")).toBeInTheDocument();
    const templates = screen.getByRole("list", { name: "Workflow templates" });
    expect(within(templates).getAllByRole("button").map((b) => b.querySelector("span")?.textContent)).toEqual([
      "Meeting-to-Tasks",
      "Sprint Planning",
      "Incident Response",
    ]);
  });

  it("fills the instruction from the template's backend-provided example", async () => {
    const custom = TEMPLATES.map((t) => (t.id === "incident-response" ? { ...t, example_instruction: "Synthetic instruction from the API" } : t));
    const { user } = setup([{ path: "/api/v1/workflows/templates", body: custom }]);
    const card = await screen.findByRole("button", { name: /Incident Response/ });
    await user.click(card);
    expect(card).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Instruction")).toHaveValue("Synthetic instruction from the API");
    expect(screen.getByLabelText(/Workflow name/)).toHaveValue("Incident Response");
  });

  it("validates the form before calling the API", async () => {
    const { user, calls } = setup();
    await screen.findByText("API connected · v0.3.0");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    expect(screen.getByText("Describe the workflow you want to run.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Instruction"), "too short");
    expect(screen.getByText("Use at least 10 characters.")).toBeInTheDocument();
    expect(screen.getByLabelText("Instruction")).toHaveAttribute("aria-invalid", "true");
    await user.type(screen.getByLabelText(/Workflow name/), "ab");
    expect(screen.getByText(/Use at least 3 characters, or leave it empty/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("sends the exact PlanRequest and renders the validated plan with dependencies", async () => {
    const { user, calls } = setup();
    await generatePlan(user);
    const planCall = calls.find((c) => c.url === "/api/v1/workflows/plan");
    expect(planCall?.body).toEqual({ instruction: TEMPLATES[0]!.example_instruction, name: "Meeting-to-Tasks" });

    expect(screen.getByText("Validated")).toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: "Planned steps" })).getAllByRole("listitem");
    expect(steps).toHaveLength(3);
    expect(steps[0]).toHaveTextContent("Notion");
    expect(steps[0]).toHaveTextContent("No dependencies");
    expect(steps[1]).toHaveTextContent("Google Drive");
    expect(steps[2]).toHaveTextContent("Microsoft Teams");
    expect(steps[2]).toHaveTextContent("Depends on steps 1, 2");
    expect(screen.getByText(/has not been executed/)).toBeInTheDocument();
  });

  it("shows validator issues and offers no execution for an INVALID plan", async () => {
    const { user } = setup([{ method: "POST", path: "/api/v1/workflows/plan", status: 422, body: INVALID }]);
    await user.type(await screen.findByLabelText("Instruction"), "Create a Jira issue and notify the team in Teams");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    expect(await screen.findByText(/rejected by the validator/)).toBeInTheDocument();
    expect(screen.getByText("UNSUPPORTED_INTEGRATION")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and Execute Mock Workflow" })).not.toBeInTheDocument();
  });

  it("shows a missing-document rejection before execution, with the backend's hint", async () => {
    const missing = {
      ...INVALID,
      validation: {
        is_valid: false,
        issues: [
          {
            code: "DOCUMENT_NOT_FOUND",
            message:
              "Step 1 reads 'synthetic Sprint 42 meeting notes', which is not in the synthetic Google Drive catalog and is not created by an earlier step. Available synthetic documents: Synthetic incident report, Synthetic sprint backlog, Synthetic standup notes.",
            step_number: 1,
            field: "arguments.document_name",
          },
        ],
      },
    };
    const { user, calls } = setup([{ method: "POST", path: "/api/v1/workflows/plan", status: 422, body: missing }]);
    await user.type(await screen.findByLabelText("Instruction"), "Read the synthetic Sprint 42 meeting notes from Google Drive");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    expect(await screen.findByText("DOCUMENT_NOT_FOUND")).toBeInTheDocument();
    expect(screen.getByText(/Available synthetic documents: .*Synthetic standup notes/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and Execute Mock Workflow" })).not.toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/v1/workflows/executions")).toBe(false);
  });

  it("requires explicit approval and sends the exact ExecutionRequest", async () => {
    const { user, calls } = setup();
    await generatePlan(user);
    const execute = screen.getByRole("button", { name: "Approve and Execute Mock Workflow" });
    expect(execute).toBeDisabled();
    await user.type(screen.getByLabelText("Approved by"), "synthetic.reviewer");
    expect(execute).toBeDisabled(); // checkbox still unticked
    await user.click(screen.getByLabelText(/approve running it in mock mode/));
    expect(execute).toBeEnabled();
    await user.click(execute);

    const start = calls.find((c) => c.url === "/api/v1/workflows/executions");
    expect(start?.body).toEqual({
      workflow_id: PLANNED.workflow_id,
      plan: {
        summary: PLANNED.summary,
        steps: PLANNED.planned_steps.map((s) => ({ ...s })),
      },
      approval: { approved: true, mode: "mock", approved_by: "synthetic.reviewer" },
    });
    await waitFor(() => expect(FakeEventSource.latest().url).toBe(STARTED.stream_url));
  });

  it("renders live progress: retrying, compensating, rolled back, then the report", async () => {
    const { user, calls } = setup([{ path: STARTED.result_url, body: ROLLED_BACK_REPORT }]);
    await generatePlan(user);
    const source = await approve(user);
    const events = rollbackEvents();
    act(() => source.open());
    expect(screen.getByText("Live")).toBeInTheDocument();

    act(() => events.slice(0, 6).forEach((e) => source.emit(e))); // ... retry_scheduled for step 2
    expect(within(stepCard(1)).getByText("Succeeded")).toBeInTheDocument();
    expect(within(stepCard(2)).getByText("Retrying")).toBeInTheDocument();
    expect(stepCard(2)).toHaveTextContent("last error TOOL_TIMEOUT");
    expect(stepCard(2)).toHaveTextContent("retry in 0.2s");
    expect(screen.getByText("Step 2: retry 2 in 0.2s")).toBeInTheDocument();

    act(() => events.slice(6, 11).forEach((e) => source.emit(e))); // ... compensation_started step 2
    expect(within(stepCard(2)).getByText("Compensating")).toBeInTheDocument();
    expect(within(stepCard(3)).getByText("Failed")).toBeInTheDocument();

    act(() => events.slice(11).forEach((e) => source.emit(e)));
    expect(within(stepCard(1)).getByText("Rolled back")).toBeInTheDocument();
    expect(within(stepCard(2)).getByText("Rolled back")).toBeInTheDocument();
    expect(screen.getByText("Stream complete")).toBeInTheDocument();
    const timeline = screen.getByRole("list", { name: "Execution events" });
    expect(within(timeline).getAllByRole("listitem")).toHaveLength(events.length);

    // Final report from GET result_url
    const report = await screen.findByRole("region", { name: "5. Execution report" });
    expect(calls.some((c) => c.url === STARTED.result_url)).toBe(true);
    expect(within(report).getByText("ROLLED BACK")).toBeInTheDocument();
    expect(within(report).getByText("Tool attempts").nextSibling).toHaveTextContent("4");
    expect(within(report).getByText(/Injected permanent failure/)).toBeInTheDocument();
    expect(within(report).getAllByText("COMPENSATED")).toHaveLength(2);
    expect(within(screen.getByRole("region", { name: "Recent runs" })).getByText("Rolled back")).toBeInTheDocument();
  });

  it("shows reconnecting and disconnected states with manual recovery", async () => {
    const { user } = setup([{ path: STARTED.result_url, status: 404, body: { detail: "No execution for this workflow_id." } }]);
    await generatePlan(user);
    const source = await approve(user);
    act(() => {
      source.open();
      source.emit(ev("workflow_started", null, { step_count: 3 }));
      source.fail(false);
    });
    expect(screen.getByText("Reconnecting…")).toBeInTheDocument();
    expect(screen.getByText(/reconnecting automatically/)).toBeInTheDocument();

    act(() => source.fail(true));
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
    expect(screen.getByText("Live updates stopped")).toBeInTheDocument();

    // The run is still going server-side, so no report is stored yet (404).
    await user.click(screen.getByRole("button", { name: "Check final result" }));
    expect(await screen.findByText("No stored report for this run")).toBeInTheDocument();

    // Manual reconnect resumes after the last event received.
    await user.click(screen.getByRole("button", { name: "Reconnect" }));
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.latest().url).toBe(`${STARTED.stream_url}&last_event_id=1`);
    expect(screen.getByText("Connecting…")).toBeInTheDocument();
    expect(screen.queryByText("Live updates stopped")).not.toBeInTheDocument();
  });

  it("shows a rejected outcome from the terminal event when no report is stored", async () => {
    const { user } = setup([{ path: STARTED.result_url, status: 404, body: { detail: "No execution for this workflow_id." } }]);
    await generatePlan(user);
    const source = await approve(user);
    act(() => source.emit(ev("workflow_failed", null, { status: "REJECTED", error_codes: ["APPROVAL_REQUIRED"], validation_issue_codes: [] })));
    expect(within(screen.getByRole("region", { name: "4. Live execution" })).getByText("Rejected")).toBeInTheDocument();
    expect(await screen.findByText("No stored report for this run")).toBeInTheDocument();
    expect(screen.getByText("APPROVAL_REQUIRED")).toBeInTheDocument();
  });

  it("reports API failures: backend down, planning error, execution conflict", async () => {
    installFetch([
      { path: "/health", networkError: true },
      { path: "/api/v1/workflows/templates", networkError: true },
    ]);
    render(<App />);
    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
    expect(await screen.findByText("Templates unavailable")).toBeInTheDocument();
  });

  it("reports planner and start errors without fabricating results", async () => {
    const { user } = setup([
      { method: "POST", path: "/api/v1/workflows/executions", status: 409, body: { detail: "workflow_id was already executed with a different plan." } },
    ]);
    await generatePlan(user);
    await user.type(screen.getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(screen.getByLabelText(/approve running it in mock mode/));
    await user.click(screen.getByRole("button", { name: "Approve and Execute Mock Workflow" }));
    expect(await screen.findByText("Execution could not be started")).toBeInTheDocument();
    expect(screen.getByText(/already executed with a different plan/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "4. Live execution" })).not.toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("shows a planning network failure", async () => {
    const { user } = setup([{ method: "POST", path: "/api/v1/workflows/plan", networkError: true }]);
    await generatePlan(user).catch(() => undefined);
    expect(await screen.findByText("Planning request failed")).toBeInTheDocument();
    expect(screen.getByText(/Cannot reach the WorkflowWeaver API/)).toBeInTheDocument();
  });
});
