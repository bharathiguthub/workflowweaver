import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage, getHealth, getTemplates, planWorkflow } from "./api/client";
import type { PlanResponse, WorkflowTemplate } from "./api/types";
import { ApprovalPanel } from "./components/ApprovalPanel";
import { ExecutionMonitor } from "./components/ExecutionMonitor";
import { ExecutionReport } from "./components/ExecutionReport";
import { Header, type ApiHealth } from "./components/Header";
import { PlanReview } from "./components/PlanReview";
import { SessionHistory, type HistoryEntry } from "./components/SessionHistory";
import { Alert } from "./components/StatusBadge";
import { WorkflowInput } from "./components/WorkflowInput";
import { isTerminalStatus } from "./state/execution";
import { toPlanRequest } from "./state/forms";
import { useExecution } from "./state/useExecution";

export default function App() {
  const [health, setHealth] = useState<ApiHealth>({ state: "checking" });
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [templatesState, setTemplatesState] = useState<"loading" | "loaded" | "error">("loading");
  const [templatesError, setTemplatesError] = useState<string | null>(null);

  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [planName, setPlanName] = useState("");
  const [history, setHistory] = useState<HistoryEntry[]>([]);

  const execution = useExecution();
  const { view, reset } = execution;
  const recorded = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    getHealth()
      .then((h) => active && setHealth({ state: "ok", version: h.version }))
      .catch(() => active && setHealth({ state: "down" }));
    getTemplates()
      .then((t) => {
        if (!active) return;
        setTemplates(t);
        setTemplatesState("loaded");
      })
      .catch((error: unknown) => {
        if (!active) return;
        setTemplatesError(errorMessage(error));
        setTemplatesState("error");
      });
    return () => {
      active = false;
    };
  }, []);

  // Record each finished run once in the session history.
  useEffect(() => {
    if (view.workflowId && isTerminalStatus(view.workflowStatus) && recorded.current !== view.workflowId) {
      recorded.current = view.workflowId;
      const entry: HistoryEntry = {
        workflowId: view.workflowId,
        name: planName || "Untitled workflow",
        status: view.workflowStatus,
        finishedAt: new Date().toISOString(),
      };
      setHistory((h) => [entry, ...h].slice(0, 10));
    }
  }, [view.workflowId, view.workflowStatus, planName]);

  const generatePlan = useCallback(
    async (instruction: string, name: string) => {
      reset();
      setPlanning(true);
      setPlanError(null);
      setPlan(null);
      try {
        const response = await planWorkflow(toPlanRequest(instruction, name));
        setPlan(response);
        setPlanName(name.trim());
      } catch (error) {
        setPlanError(errorMessage(error));
      } finally {
        setPlanning(false);
      }
    },
    [reset],
  );

  const executionActive = execution.starting || (view.workflowStatus !== "idle" && !isTerminalStatus(view.workflowStatus));
  const alreadyExecuted = view.workflowId !== null && view.workflowId === plan?.workflow_id;

  return (
    <div className="min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <Header health={health} />
      <main id="main" className="mx-auto grid max-w-6xl gap-6 px-4 py-6 sm:px-6 lg:grid-cols-[1fr_18rem]">
        <div className="min-w-0 space-y-6">
          {health.state === "down" && (
            <Alert title="The WorkflowWeaver API is not reachable">
              Start the backend on http://127.0.0.1:8000 (see README), then reload this page.
            </Alert>
          )}
          <WorkflowInput
            templates={templates}
            templatesState={templatesState}
            templatesError={templatesError}
            planning={planning}
            onSubmit={generatePlan}
          />
          {planError && <Alert title="Planning request failed">{planError}</Alert>}
          {plan && <PlanReview plan={plan} />}
          {plan?.status === "PLANNED" && (
            <ApprovalPanel
              key={plan.workflow_id}
              stepCount={plan.planned_steps.length}
              busy={executionActive || alreadyExecuted}
              starting={execution.starting}
              error={execution.startError}
              onApprove={(approvedBy) => void execution.execute(plan, approvedBy)}
            />
          )}
          {view.workflowStatus !== "idle" && (
            <ExecutionMonitor
              view={view}
              connection={execution.connection}
              onReconnect={execution.reconnect}
              onCheckResult={execution.refreshReport}
            />
          )}
          {execution.reportState !== "idle" && (
            <ExecutionReport
              report={execution.report}
              state={execution.reportState}
              error={execution.reportError}
              terminal={view.terminal}
              onRetry={execution.refreshReport}
            />
          )}
        </div>
        <aside className="space-y-4">
          <SessionHistory entries={history} />
        </aside>
      </main>
    </div>
  );
}
