import { beforeEach, describe, expect, it } from "vitest";

import { PLANNED, WORKFLOW_ID, ev, resetEventIds, rollbackEvents } from "../test/doubles";
import { executionReducer, initialExecutionView, type ExecutionView } from "./execution";

const init = (): ExecutionView =>
  executionReducer(initialExecutionView, { type: "init", workflowId: WORKFLOW_ID, steps: PLANNED.planned_steps });

const run = (events: ReturnType<typeof rollbackEvents>, state = init()) =>
  events.reduce((s, event) => executionReducer(s, { type: "event", event }), state);

const statuses = (s: ExecutionView) => s.steps.map((x) => x.status);

describe("executionReducer", () => {
  beforeEach(() => resetEventIds());

  it("starts every planned step as pending", () => {
    const s = init();
    expect(s.workflowStatus).toBe("pending");
    expect(statuses(s)).toEqual(["pending", "pending", "pending"]);
  });

  it("tracks a successful run", () => {
    const s = run([
      ev("workflow_started"),
      ev("step_started", 1, { attempt: 1 }),
      ev("step_succeeded", 1, { resource_id: "r1" }),
      ev("step_started", 2, { attempt: 1 }),
    ]);
    expect(s.workflowStatus).toBe("running");
    expect(statuses(s)).toEqual(["succeeded", "running", "pending"]);
    expect(s.steps[0]?.resourceId).toBe("r1");
  });

  it("shows retrying after a retryable failure, then running on the next attempt", () => {
    let s = run([
      ev("workflow_started"),
      ev("step_started", 1, { attempt: 1 }),
      ev("step_failed", 1, { attempt: 1, error_code: "TOOL_TIMEOUT", will_retry: true }),
      ev("retry_scheduled", 1, { phase: "execution", delay_seconds: 0.4 }),
    ]);
    expect(s.workflowStatus).toBe("retrying");
    expect(s.steps[0]).toMatchObject({ status: "retrying", lastErrorCode: "TOOL_TIMEOUT", retryDelaySeconds: 0.4 });
    s = executionReducer(s, { type: "event", event: ev("step_started", 1, { attempt: 2 }) });
    expect(s.steps[0]).toMatchObject({ status: "running", attempt: 2, retryDelaySeconds: null });
  });

  it("tracks compensation through to rolled back", () => {
    const events = rollbackEvents();
    const midway = run(events.slice(0, 11)); // ... compensation_started for step 2
    expect(midway.workflowStatus).toBe("compensating");
    expect(statuses(midway)).toEqual(["succeeded", "compensating", "failed"]);

    const done = run(events);
    expect(done.workflowStatus).toBe("rolled_back");
    expect(statuses(done)).toEqual(["rolled_back", "rolled_back", "failed"]);
    expect(done.terminal?.event_type).toBe("workflow_failed");
    expect(done.events).toHaveLength(events.length);
  });

  it("marks non-reversible effects, compensation failures and skipped steps", () => {
    const s = run([
      ev("workflow_started"),
      ev("step_started", 1),
      ev("step_succeeded", 1),
      ev("step_started", 2),
      ev("step_failed", 2, { will_retry: false }),
      ev("compensation_started", 1),
      ev("compensation_completed", 1, { status: "NOT_COMPENSATABLE" }),
      ev("workflow_failed", null, { status: "PARTIALLY_ROLLED_BACK", step_statuses: { "1": "SUCCEEDED", "2": "FAILED", "3": "SKIPPED" } }),
    ]);
    expect(statuses(s)).toEqual(["not_reversible", "failed", "skipped"]);
    expect(s.workflowStatus).toBe("partially_rolled_back");

    resetEventIds();
    const f = run([ev("compensation_started", 1), ev("compensation_failed", 1, { status: "FAILED" }), ev("workflow_failed", null, { status: "COMPENSATION_FAILED" })]);
    expect(f.steps[0]?.status).toBe("compensation_failed");
    expect(f.workflowStatus).toBe("compensation_failed");
  });

  it.each([
    ["REJECTED", "rejected"],
    ["FAILED", "failed"],
    ["ERROR", "error"],
  ])("maps terminal status %s", (status, expected) => {
    const s = run([ev("workflow_failed", null, { status })]);
    expect(s.workflowStatus).toBe(expected);
  });

  it("ignores duplicate, out-of-order, foreign and post-terminal events", () => {
    const first = ev("step_started", 1);
    let s = run([ev("workflow_started"), first]);
    const before = s;
    s = executionReducer(s, { type: "event", event: first }); // duplicate
    s = executionReducer(s, { type: "event", event: { ...ev("step_succeeded", 1), workflow_id: "another-workflow" } });
    expect(s).toBe(before);
    s = run([ev("workflow_completed", null, { status: "COMPLETED" })], s);
    const after = executionReducer(s, { type: "event", event: ev("step_failed", 2) });
    expect(after).toBe(s);
  });
});
