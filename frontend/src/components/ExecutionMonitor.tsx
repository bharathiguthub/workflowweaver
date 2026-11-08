import type { ConnectionState } from "../api/stream";
import { actionLabel, integrationLabel, type ExecutionEvent } from "../api/types";
import { isTerminalStatus, type ExecutionView, type StepView } from "../state/execution";
import { Alert, Badge, StepStatusBadge, WorkflowStatusBadge } from "./StatusBadge";

interface Props {
  view: ExecutionView;
  connection: ConnectionState | "idle";
  onReconnect: () => void;
  onCheckResult: () => void;
}

const CONNECTION: Record<ConnectionState | "idle", { tone: "neutral" | "info" | "success" | "warning" | "danger"; label: string; pulse?: boolean }> = {
  idle: { tone: "neutral", label: "Not connected" },
  connecting: { tone: "info", label: "Connecting…", pulse: true },
  open: { tone: "success", label: "Live", pulse: true },
  reconnecting: { tone: "warning", label: "Reconnecting…", pulse: true },
  closed: { tone: "neutral", label: "Stream complete" },
  failed: { tone: "danger", label: "Disconnected" },
};

export function describeEvent(event: ExecutionEvent): string {
  const m = event.metadata;
  const step = event.step_number !== null ? `Step ${event.step_number}` : "Workflow";
  const where = (k1: string, k2: string) =>
    typeof m[k1] === "string" && typeof m[k2] === "string" ? ` · ${integrationLabel(m[k1] as string)} ${actionLabel(m[k2] as string).toLowerCase()}` : "";
  switch (event.event_type) {
    case "approval_verified":
      return `Approval verified: bound to the reviewed plan (digest ${String(m.plan_digest ?? "?")}…, ${String(m.write_count ?? "?")} write step(s))`;
    case "workflow_started":
      return `Workflow started with ${String(m.step_count ?? "?")} step(s)`;
    case "step_started":
      return `${step} started (attempt ${String(m.attempt ?? 1)})${where("integration", "action")}`;
    case "step_succeeded":
      if (m.integration === "teams") {
        return `${step} succeeded · Microsoft Teams message${m.replayed === true ? " (replayed, not posted again)" : ""}`;
      }
      return `${step} succeeded${m.replayed === true ? " (replayed, no duplicate effect)" : ""}`;
    case "step_failed":
      return `${step} attempt ${String(m.attempt ?? "?")} failed: ${String(m.error_code ?? "error")}${
        m.will_retry === true ? ", will retry" : m.retries_exhausted === true ? ", retries exhausted" : ""
      }`;
    case "resource_verified":
      return `${step}: read-back ${
        m.status === "verified" ? "verified" : m.status === "not_performed" ? "not performed (dry run)" : "unavailable"
      }${where("integration", "action")}`;
    case "retry_scheduled":
      return `${step}: ${m.phase === "compensation" ? "compensation " : ""}retry ${String(m.next_attempt ?? "")} in ${String(
        m.delay_seconds ?? "?",
      )}s`;
    case "compensation_started":
      return `${step}: undoing effect${where("integration", "action")}`;
    case "compensation_completed":
      return `${step}: compensation ${String(m.status ?? "done").toLowerCase().replace(/_/g, " ")}`;
    case "compensation_failed":
      return `${step}: compensation FAILED, manual cleanup required`;
    case "workflow_completed":
      return "Workflow completed";
    case "workflow_failed":
      return `Workflow finished: ${String(m.status ?? "failed").replace(/_/g, " ").toLowerCase()}`;
  }
}

const EVENT_DOT: Record<string, string> = {
  step_succeeded: "bg-emerald-500",
  workflow_completed: "bg-emerald-600",
  step_failed: "bg-rose-500",
  workflow_failed: "bg-rose-600",
  compensation_failed: "bg-rose-600",
  retry_scheduled: "bg-amber-500",
  compensation_started: "bg-violet-500",
  compensation_completed: "bg-violet-500",
};

/** The run's mode, from the workflow_started event (the server's word, not the client's). */
function runMode(view: ExecutionView): string | null {
  const started = view.events.find((e) => e.event_type === "workflow_started");
  return typeof started?.metadata.mode === "string" ? started.metadata.mode : null;
}

/** Phase 7.1: what a Teams step's status means, without claiming more than the webhook reports. */
export function teamsStepNote(step: StepView, mode: string | null): string | null {
  if (step.integration !== "teams") return null;
  if (step.status === "succeeded") {
    if (mode === "real") return "Accepted by the Teams webhook (Microsoft does not confirm the post itself).";
    if (mode === "dry_run") return "Request built and checked; not sent (dry run).";
    return "Delivery simulated (mock); nothing was posted.";
  }
  if (step.status === "retrying") return "Webhook did not receive the request; retrying (no message was posted).";
  if (step.status === "failed" && step.lastErrorCode === "OUTCOME_UNKNOWN") {
    return "Outcome unknown: the webhook may have received the message. Not retried, to avoid a duplicate; check the channel.";
  }
  if (step.status === "failed") return "Not posted.";
  if (step.status === "not_reversible") return "A posted Teams message cannot be unsent; it remains in the channel.";
  return null;
}

export function ExecutionMonitor({ view, connection, onReconnect, onCheckResult }: Props) {
  const finished = isTerminalStatus(view.workflowStatus);
  const mode = runMode(view);
  const lost = connection === "failed" && !finished;

  return (
    <section aria-labelledby="live-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="live-heading" className="text-base font-semibold text-slate-900">
          4. Live execution
        </h2>
        <div className="flex flex-wrap items-center gap-2" aria-live="polite">
          <span className="sr-only">Workflow status:</span>
          <WorkflowStatusBadge status={view.workflowStatus} />
          <span className="sr-only">Connection:</span>
          <Badge {...CONNECTION[connection]} />
        </div>
      </div>

      {connection === "reconnecting" && (
        <div className="mt-3">
          <Alert tone="warning" title="Connection interrupted, reconnecting automatically">
            The run continues on the server. Missed events are replayed when the connection is restored.
          </Alert>
        </div>
      )}
      {lost && (
        <div className="mt-3">
          <Alert title="Live updates stopped">
            <p>The browser stopped receiving events. The run continues on the server.</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button type="button" onClick={onReconnect} className="rounded-md bg-white px-3 py-1.5 text-sm font-semibold text-rose-900 ring-1 ring-rose-300 hover:bg-rose-100">
                Reconnect
              </button>
              <button type="button" onClick={onCheckResult} className="rounded-md bg-white px-3 py-1.5 text-sm font-semibold text-rose-900 ring-1 ring-rose-300 hover:bg-rose-100">
                Check final result
              </button>
            </div>
          </Alert>
        </div>
      )}

      <ol className="mt-4 grid gap-2 sm:grid-cols-2" aria-label="Step progress">
        {view.steps.map((s) => (
          <li key={s.step_number} className="flex items-start justify-between gap-2 rounded-lg border border-slate-200 p-3">
            <div className="min-w-0">
              <p className="text-sm font-semibold text-slate-900">
                {s.step_number}. {integrationLabel(s.integration)}
              </p>
              <p className="text-xs text-slate-600">{actionLabel(s.action)}</p>
              {s.title && <p className="mt-0.5 break-words text-xs font-medium text-slate-800">{s.title}</p>}
              {s.resourceId && (
                <p className="mt-0.5 break-all font-mono text-[11px] text-slate-600">
                  {s.integration === "notion" ? "Page ID" : s.integration === "teams" ? "Message reference" : "Resource"}: {s.resourceId}
                </p>
              )}
              {teamsStepNote(s, mode) && <p className="mt-0.5 text-xs text-slate-700">{teamsStepNote(s, mode)}</p>}
              {s.verification && (
                <p className="mt-0.5 text-xs text-slate-700">
                  {s.verification === "verified"
                    ? "Read back: verified in Notion."
                    : s.verification === "not_performed"
                      ? "Read back: not performed (dry run)."
                      : "Read back: unavailable; creation succeeded but was not verified."}
                </p>
              )}
              <p className="mt-1 text-xs text-slate-500">
                {s.attempt > 0 && <>Attempt {s.attempt}</>}
                {s.lastErrorCode && <> · last error {s.lastErrorCode}</>}
                {s.retryDelaySeconds !== null && <> · retry in {s.retryDelaySeconds}s</>}
              </p>
            </div>
            <StepStatusBadge status={s.status} />
          </li>
        ))}
      </ol>

      <h3 className="mt-5 text-sm font-semibold text-slate-800">Event timeline</h3>
      {view.events.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">{connection === "failed" ? "No events received." : "Waiting for the first event…"}</p>
      ) : (
        <ol className="mt-2 max-h-80 space-y-1 overflow-y-auto pr-1" aria-label="Execution events" aria-live="polite" aria-relevant="additions">
          {view.events.map((e) => (
            <li key={e.event_id} className="flex items-start gap-2 text-sm">
              <span aria-hidden="true" className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${EVENT_DOT[e.event_type] ?? "bg-sky-500"}`} />
              <span className="w-8 shrink-0 font-mono text-xs text-slate-400">#{e.event_id}</span>
              <span className="flex-1 text-slate-800">{describeEvent(e)}</span>
              <time dateTime={e.timestamp} className="shrink-0 font-mono text-xs text-slate-400">
                {new Date(e.timestamp).toLocaleTimeString()}
              </time>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
