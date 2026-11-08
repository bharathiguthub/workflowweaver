import { MODE_LABELS } from "../api/types";
import type { HistoryEntry } from "../components/SessionHistory";
import { Alert, WorkflowStatusBadge } from "../components/StatusBadge";
import { Card, PageHeader } from "../components/layout/Page";
import { SECTION_BY_ID } from "../navigation";

/** Session-local run history. There is deliberately no persistent history backend. */
export function HistoryPage({ entries }: { entries: HistoryEntry[] }) {
  return (
    <div className="space-y-6">
      <PageHeader icon="clock" title="Execution History" description={SECTION_BY_ID.history.description} />
      <Alert tone="warning" title="History is kept in this browser tab only">
        Runs are listed only while this tab stays open. Reloading the page, closing the tab or restarting the WorkflowWeaver server
        loses this list; WorkflowWeaver does not store execution history. Check Notion, Google Drive or Teams directly for what a
        real run created.
      </Alert>
      <Card id="history-table" title={`Runs this session (${entries.length})`}>
        {entries.length === 0 ? (
          <p className="text-sm text-slate-500">No runs yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[36rem] text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th scope="col" className="py-2 pr-4 font-medium">Workflow</th>
                  <th scope="col" className="py-2 pr-4 font-medium">Workspace</th>
                  <th scope="col" className="py-2 pr-4 font-medium">Mode</th>
                  <th scope="col" className="py-2 pr-4 font-medium">Finished</th>
                  <th scope="col" className="py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {entries.map((e) => (
                  <tr key={e.workflowId}>
                    <td className="py-2 pr-4">
                      <p className="font-medium text-slate-800">{e.name}</p>
                      <p className="font-mono text-[11px] text-slate-400">{e.workflowId}</p>
                    </td>
                    <td className="py-2 pr-4 text-slate-700">{e.section ? SECTION_BY_ID[e.section].label : "-"}</td>
                    <td className="py-2 pr-4 text-slate-700">{e.mode ? MODE_LABELS[e.mode] : "-"}</td>
                    <td className="py-2 pr-4 text-slate-700">{new Date(e.finishedAt).toLocaleString()}</td>
                    <td className="py-2">
                      <WorkflowStatusBadge status={e.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
