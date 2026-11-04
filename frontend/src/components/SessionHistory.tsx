import type { WorkflowUiStatus } from "../state/execution";
import { WorkflowStatusBadge } from "./StatusBadge";

export interface HistoryEntry {
  workflowId: string;
  name: string;
  status: WorkflowUiStatus;
  finishedAt: string;
}

/** The backend has no "list workflows" endpoint, so history covers this browser tab only. */
export function SessionHistory({ entries }: { entries: HistoryEntry[] }) {
  return (
    <section aria-labelledby="history-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <h2 id="history-heading" className="text-sm font-semibold text-slate-900">
        Recent runs
      </h2>
      <p className="mt-1 text-xs text-slate-500">This browser tab only. The API has no history endpoint yet.</p>
      {entries.length === 0 ? (
        <p className="mt-3 text-sm text-slate-500">No runs yet.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {entries.map((e) => (
            <li key={e.workflowId} className="rounded-lg border border-slate-100 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium text-slate-800">{e.name}</span>
                <WorkflowStatusBadge status={e.status} />
              </div>
              <p className="mt-1 truncate font-mono text-[11px] text-slate-400">
                {e.workflowId} · {new Date(e.finishedAt).toLocaleTimeString()}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
