import { Badge } from "./StatusBadge";

export type ApiHealth = { state: "checking" } | { state: "ok"; version: string } | { state: "down" };

export function Header({ health }: { health: ApiHealth }) {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
        <div className="flex items-center gap-3">
          <div aria-hidden="true" className="grid h-9 w-9 place-items-center rounded-lg bg-indigo-600 font-bold text-white">
            W
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight text-slate-900">WorkflowWeaver</h1>
            <p className="text-xs text-slate-500">Plan, approve and watch multi-tool workflows</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span title="Notion, Google Drive and Teams are simulated in memory. No real service is called.">
            <Badge tone="warning" label="Mock mode: no real Notion, Drive or Teams calls" />
          </span>
          {health.state === "checking" && <Badge tone="neutral" label="API: checking…" />}
          {health.state === "ok" && <Badge tone="success" label={`API connected · v${health.version}`} />}
          {health.state === "down" && <Badge tone="danger" label="API unreachable" />}
        </div>
      </div>
    </header>
  );
}
