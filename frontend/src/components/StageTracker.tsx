import type { PlanResponse } from "../api/types";
import { isTerminalStatus, type WorkflowUiStatus } from "../state/execution";

type Stage = "plan" | "approve" | "execute" | "report";
type StageState = "done" | "current" | "upcoming" | "failed";

const STAGES: { id: Stage; label: string; detail: string }[] = [
  { id: "plan", label: "Plan", detail: "AI drafts and the server validates" },
  { id: "approve", label: "Approve", detail: "You review and approve the exact plan" },
  { id: "execute", label: "Execute", detail: "Steps run with retries and rollback" },
  { id: "report", label: "Report", detail: "Verified results and links" },
];

export function stageStates(plan: PlanResponse | null, status: WorkflowUiStatus): Record<Stage, StageState> {
  const terminal = isTerminalStatus(status);
  const failedRun = ["failed", "rejected", "error", "compensation_failed", "rolled_back", "partially_rolled_back"].includes(status);
  if (status !== "idle") {
    return {
      plan: "done",
      approve: "done",
      execute: terminal ? (failedRun ? "failed" : "done") : "current",
      report: terminal ? (failedRun ? "failed" : "done") : "upcoming",
    };
  }
  if (plan?.status === "PLANNED") return { plan: "done", approve: "current", execute: "upcoming", report: "upcoming" };
  if (plan) return { plan: "failed", approve: "upcoming", execute: "upcoming", report: "upcoming" };
  return { plan: "current", approve: "upcoming", execute: "upcoming", report: "upcoming" };
}

const STYLE: Record<StageState, { dot: string; text: string; label: string }> = {
  done: { dot: "bg-emerald-500 text-white", text: "text-slate-900", label: "done" },
  current: { dot: "bg-indigo-600 text-white ring-4 ring-indigo-100", text: "text-indigo-700", label: "current" },
  upcoming: { dot: "bg-slate-200 text-slate-600", text: "text-slate-500", label: "upcoming" },
  failed: { dot: "bg-rose-500 text-white", text: "text-rose-700", label: "needs attention" },
};

/** Planning -> approval -> execution -> reporting, for the Business Workflows page. */
export function StageTracker({ plan, status }: { plan: PlanResponse | null; status: WorkflowUiStatus }) {
  const states = stageStates(plan, status);
  return (
    <ol aria-label="Workflow stages" className="grid gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:grid-cols-4">
      {STAGES.map((s, i) => {
        const st = STYLE[states[s.id]];
        return (
          <li key={s.id} className="flex items-start gap-3" aria-current={states[s.id] === "current" ? "step" : undefined}>
            <span className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold ${st.dot}`}>{i + 1}</span>
            <span>
              <span className={`block text-sm font-semibold ${st.text}`}>
                {s.label} <span className="sr-only">({st.label})</span>
              </span>
              <span className="block text-xs text-slate-500">{s.detail}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}
