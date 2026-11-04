import { beforeEach, describe, expect, it, vi } from "vitest";

import { FakeEventSource, INVALID, PLANNED, STARTED, ev, installFakeEventSource, installFetch, resetEventIds } from "../test/doubles";
import { ApiError, NetworkError, getExecutionResult, planWorkflow, startExecution } from "./client";
import { openExecutionStream, streamUrlFrom, type ConnectionState } from "./stream";
import type { ExecutionEvent } from "./types";

describe("api client", () => {
  it("returns PLANNED, INVALID (422) and FAILED (502) plan responses", async () => {
    installFetch([{ method: "POST", path: "/api/v1/workflows/plan", status: 422, body: INVALID }]);
    await expect(planWorkflow({ instruction: "Create a Jira issue please" })).resolves.toMatchObject({ status: "INVALID" });
    installFetch([{ method: "POST", path: "/api/v1/workflows/plan", status: 502, body: { ...PLANNED, status: "FAILED", planned_steps: [] } }]);
    await expect(planWorkflow({ instruction: "Create a Notion page please" })).resolves.toMatchObject({ status: "FAILED" });
  });

  it("throws FastAPI validation details as readable errors", async () => {
    installFetch([
      {
        method: "POST",
        path: "/api/v1/workflows/plan",
        status: 422,
        body: { detail: [{ loc: ["body", "instruction"], msg: "String should have at least 10 characters", type: "string_too_short" }] },
      },
    ]);
    await expect(planWorkflow({ instruction: "short" })).rejects.toThrow("instruction: String should have at least 10 characters");
  });

  it("reports network failures clearly", async () => {
    installFetch([{ method: "POST", path: "/api/v1/workflows/plan", networkError: true }]);
    const error = await planWorkflow({ instruction: "Create a Notion page please" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(NetworkError);
    expect((error as Error).message).toMatch(/Cannot reach the WorkflowWeaver API/);
  });

  it("surfaces 409 conflicts from execution start", async () => {
    installFetch([{ method: "POST", path: "/api/v1/workflows/executions", status: 409, body: { detail: "An execution stream for this workflow_id is already active." } }]);
    const error = await startExecution({
      workflow_id: PLANNED.workflow_id,
      plan: { summary: "", steps: [] },
      approval: { approved: true, mode: "mock", approved_by: "synthetic.reviewer" },
    }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).message).toContain("already active");
  });

  it("treats a missing stored result as null", async () => {
    installFetch([{ path: STARTED.result_url, status: 404, body: { detail: "No execution for this workflow_id." } }]);
    await expect(getExecutionResult(STARTED.result_url)).resolves.toBeNull();
  });
});

describe("execution stream", () => {
  let events: ExecutionEvent[];
  let states: ConnectionState[];
  const handlers = () => ({ onEvent: (e: ExecutionEvent) => events.push(e), onState: (s: ConnectionState) => states.push(s) });

  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
    events = [];
    states = [];
  });

  it("delivers typed events in order, drops duplicates, and closes on the terminal event", () => {
    openExecutionStream(STARTED.stream_url, handlers());
    const source = FakeEventSource.latest();
    expect(source.url).toBe(STARTED.stream_url);
    source.open();
    const a = ev("workflow_started");
    const b = ev("step_started", 1, { attempt: 1 });
    source.emit(a);
    source.emit(b);
    source.emit(a); // replayed after a reconnect
    source.emitRaw("step_succeeded", "{not json");
    source.emit(ev("workflow_completed", null, { status: "COMPLETED" }));
    expect(events.map((e) => e.event_id)).toEqual([1, 2, 3]);
    expect(states).toEqual(["connecting", "open", "closed"]);
    expect(source.readyState).toBe(FakeEventSource.CLOSED);
  });

  it("reports reconnecting while the browser retries and failed when it gives up", () => {
    openExecutionStream(STARTED.stream_url, handlers());
    const source = FakeEventSource.latest();
    source.open();
    source.fail(false);
    source.open();
    source.fail(true);
    expect(states).toEqual(["connecting", "open", "reconnecting", "open", "failed"]);
  });

  it("resumes after the last event id on manual reconnect", () => {
    expect(streamUrlFrom(STARTED.stream_url, 7)).toBe(`${STARTED.stream_url}&last_event_id=7`);
    expect(streamUrlFrom("/x", 0)).toBe("/x");
    openExecutionStream(STARTED.stream_url, handlers(), 2);
    const source = FakeEventSource.latest();
    source.emit(ev("workflow_started"));
    source.emit(ev("step_started", 1));
    source.emit(ev("step_succeeded", 1));
    expect(events.map((e) => e.event_id)).toEqual([3]);
  });

  it("ignores errors after it was closed by the caller", () => {
    const close = openExecutionStream(STARTED.stream_url, handlers());
    close();
    FakeEventSource.latest().onerror?.(new Event("error"));
    expect(states).toEqual(["connecting"]);
    vi.restoreAllMocks();
  });
});
