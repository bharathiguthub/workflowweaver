import type { PlanStatus } from "../api/types";
import type { StepUiStatus, WorkflowUiStatus } from "../state/execution";

type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "violet" | "slate";

const TONES: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-700 ring-slate-300",
  info: "bg-sky-50 text-sky-800 ring-sky-300",
  success: "bg-emerald-50 text-emerald-800 ring-emerald-300",
  warning: "bg-amber-50 text-amber-900 ring-amber-300",
  danger: "bg-rose-50 text-rose-800 ring-rose-300",
  violet: "bg-violet-50 text-violet-800 ring-violet-300",
  slate: "bg-slate-200 text-slate-800 ring-slate-400",
};

export function Badge({ tone, label, pulse = false }: { tone: Tone; label: string; pulse?: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${TONES[tone]}`}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full bg-current ${pulse ? "motion-safe:animate-pulse" : ""}`} />
      {label}
    </span>
  );
}

const STEP: Record<StepUiStatus, { tone: Tone; label: string; pulse?: boolean }> = {
  pending: { tone: "neutral", label: "Pending" },
  running: { tone: "info", label: "Running", pulse: true },
  succeeded: { tone: "success", label: "Succeeded" },
  failed: { tone: "danger", label: "Failed" },
  retrying: { tone: "warning", label: "Retrying", pulse: true },
  compensating: { tone: "violet", label: "Compensating", pulse: true },
  rolled_back: { tone: "slate", label: "Rolled back" },
  not_reversible: { tone: "warning", label: "Not reversible" },
  compensation_failed: { tone: "danger", label: "Compensation failed" },
  skipped: { tone: "neutral", label: "Skipped" },
};

export const StepStatusBadge = ({ status }: { status: StepUiStatus }) => <Badge {...STEP[status]} />;

const WORKFLOW: Record<WorkflowUiStatus, { tone: Tone; label: string; pulse?: boolean }> = {
  idle: { tone: "neutral", label: "Not started" },
  pending: { tone: "neutral", label: "Pending", pulse: true },
  running: { tone: "info", label: "Running", pulse: true },
  retrying: { tone: "warning", label: "Retrying", pulse: true },
  compensating: { tone: "violet", label: "Compensating", pulse: true },
  completed: { tone: "success", label: "Completed" },
  failed: { tone: "danger", label: "Failed" },
  rolled_back: { tone: "slate", label: "Rolled back" },
  partially_rolled_back: { tone: "warning", label: "Partially rolled back" },
  compensation_failed: { tone: "danger", label: "Compensation failed" },
  rejected: { tone: "danger", label: "Rejected" },
  error: { tone: "danger", label: "Error" },
};

export const WorkflowStatusBadge = ({ status }: { status: WorkflowUiStatus }) => <Badge {...WORKFLOW[status]} />;

const PLAN: Record<PlanStatus, { tone: Tone; label: string }> = {
  PLANNED: { tone: "success", label: "Validated" },
  INVALID: { tone: "danger", label: "Rejected by validator" },
  FAILED: { tone: "danger", label: "Planning failed" },
};

export const PlanStatusBadge = ({ status }: { status: PlanStatus }) => <Badge {...PLAN[status]} />;

export function Spinner({ label }: { label: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-slate-600">
      <span aria-hidden="true" className="h-4 w-4 rounded-full border-2 border-slate-300 border-t-indigo-600 motion-safe:animate-spin" />
      {label}
    </span>
  );
}

export function Alert({ tone = "danger", title, children }: { tone?: "danger" | "warning" | "info"; title: string; children?: React.ReactNode }) {
  const styles = {
    danger: "border-rose-200 bg-rose-50 text-rose-900",
    warning: "border-amber-200 bg-amber-50 text-amber-900",
    info: "border-sky-200 bg-sky-50 text-sky-900",
  }[tone];
  return (
    <div role={tone === "info" ? "status" : "alert"} className={`rounded-lg border p-3 text-sm ${styles}`}>
      <p className="font-semibold">{title}</p>
      {children ? <div className="mt-1">{children}</div> : null}
    </div>
  );
}
