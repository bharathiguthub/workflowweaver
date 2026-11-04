import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { errorMessage, getExecutionResult, startExecution } from "../api/client";
import { openExecutionStream, type ConnectionState } from "../api/stream";
import { TERMINAL_EVENT_TYPES, type ExecutionResponse, type ExecutionStartResponse, type PlanResponse } from "../api/types";
import { executionReducer, initialExecutionView } from "./execution";
import { toExecutionRequest } from "./forms";

export type ReportState = "idle" | "loading" | "loaded" | "unavailable" | "error";

/** Starts a mock execution, follows its SSE stream, and loads the final report. */
export function useExecution() {
  const [view, dispatch] = useReducer(executionReducer, initialExecutionView);
  const [connection, setConnection] = useState<ConnectionState | "idle">("idle");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [started, setStarted] = useState<ExecutionStartResponse | null>(null);
  const [report, setReport] = useState<ExecutionResponse | null>(null);
  const [reportState, setReportState] = useState<ReportState>("idle");
  const [reportError, setReportError] = useState<string | null>(null);

  const closeStream = useRef<(() => void) | null>(null);
  const lastEventId = useRef(0);
  const generation = useRef(0); // ignore late callbacks from a previous run

  const loadReport = useCallback(async (resultUrl: string) => {
    const run = generation.current;
    setReportState("loading");
    setReportError(null);
    try {
      const result = await getExecutionResult(resultUrl);
      if (run !== generation.current) return;
      setReport(result);
      setReportState(result ? "loaded" : "unavailable");
    } catch (error) {
      if (run !== generation.current) return;
      setReportState("error");
      setReportError(errorMessage(error));
    }
  }, []);

  const connect = useCallback(
    (start: ExecutionStartResponse, after: number) => {
      closeStream.current?.();
      const run = generation.current;
      closeStream.current = openExecutionStream(
        start.stream_url,
        {
          onEvent: (event) => {
            if (run !== generation.current) return;
            lastEventId.current = Math.max(lastEventId.current, event.event_id);
            dispatch({ type: "event", event });
            if (TERMINAL_EVENT_TYPES.has(event.event_type)) void loadReport(start.result_url);
          },
          onState: (state) => {
            if (run === generation.current) setConnection(state);
          },
        },
        after,
      );
    },
    [loadReport],
  );

  const reset = useCallback(() => {
    generation.current += 1;
    closeStream.current?.();
    closeStream.current = null;
    lastEventId.current = 0;
    dispatch({ type: "reset" });
    setConnection("idle");
    setStarted(null);
    setStartError(null);
    setReport(null);
    setReportState("idle");
    setReportError(null);
  }, []);

  const execute = useCallback(
    async (plan: PlanResponse, approvedBy: string) => {
      reset();
      const run = generation.current;
      setStarting(true);
      dispatch({ type: "init", workflowId: plan.workflow_id, steps: plan.planned_steps });
      try {
        const start = await startExecution(toExecutionRequest(plan, approvedBy));
        if (run !== generation.current) return;
        setStarted(start);
        connect(start, 0);
      } catch (error) {
        if (run !== generation.current) return;
        dispatch({ type: "reset" });
        setStartError(errorMessage(error));
      } finally {
        if (run === generation.current) setStarting(false);
      }
    },
    [connect, reset],
  );

  /** Manual reconnect after the browser gave up; resumes after the last event received. */
  const reconnect = useCallback(() => {
    if (started) connect(started, lastEventId.current);
  }, [connect, started]);

  const refreshReport = useCallback(() => {
    if (started) void loadReport(started.result_url);
  }, [loadReport, started]);

  useEffect(() => () => closeStream.current?.(), []);

  return {
    view,
    connection,
    starting,
    startError,
    started,
    report,
    reportState,
    reportError,
    execute,
    reconnect,
    refreshReport,
    reset,
  };
}
