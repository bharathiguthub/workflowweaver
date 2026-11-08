import type { ReactNode } from "react";

import {
  aiPlannerLabel,
  EXECUTION_MODES,
  MODE_LABELS,
  type ExecutionMode,
  type IntegrationsStatus,
  type NotionConnectivity,
} from "../api/types";
import { teamsAvailability } from "../components/IntegrationTargetSelector";
import type { HistoryEntry } from "../components/SessionHistory";
import { Alert, Spinner, WorkflowStatusBadge } from "../components/StatusBadge";
import { Icon } from "../components/layout/Icon";
import {
  Card,
  EmptyState,
  PageHeader,
  ReadinessPill,
  StatTile,
  StatusDot,
  buttonGhost,
  buttonSecondary,
} from "../components/layout/Page";
import { SECTION_BY_ID, type IconName, type SectionId } from "../navigation";
import type { WorkflowUiStatus } from "../state/execution";
import { capabilityReadiness, plannerSummary } from "./readiness";

const WORKSPACES: SectionId[] = ["documents", "projects", "communication", "business", "builder"];
const NEEDS_ATTENTION: ReadonlySet<WorkflowUiStatus> = new Set([
  "failed", "rejected", "error", "compensation_failed", "rolled_back", "partially_rolled_back", "partially_completed",
]);
const HISTORY_CAP = 50; // App keeps the latest 50 runs of this tab

export interface ConnectivityCheckState {
  result: NotionConnectivity | null;
  error: string | null;
  checking: boolean;
  run: () => void;
}

interface Props {
  status: IntegrationsStatus | null;
  statusError: string | null;
  mode: ExecutionMode;
  history: HistoryEntry[];
  onNavigate: (id: SectionId) => void;
  /** Existing read-only connectivity checks (never write). */
  notionCheck: ConnectivityCheckState;
  driveCheck: ConnectivityCheckState;
}

const QUICK_ACTIONS: { id: SectionId; icon: IconName; label: string; detail: string }[] = [
  { id: "documents", icon: "document", label: "Create a document", detail: "AI draft, preview, then upload to the Drive sandbox" },
  { id: "projects", icon: "project", label: "Plan a Notion project", detail: "Project page and action items in Notion" },
  { id: "communication", icon: "chat", label: "Send a Teams notification", detail: "To the approved sandbox channel" },
  { id: "business", icon: "flow", label: "Run Drive → Notion → Teams", detail: "One approval for the whole workflow" },
  { id: "builder", icon: "sparkles", label: "Describe a new workflow", detail: "Plain language, supported actions only" },
];

// ---------- integration status (from GET /integrations/status and the read-only checks) ----------

type ModeReady = Record<ExecutionMode, boolean>;

interface IntegrationRow {
  name: string;
  connection: ReactNode;
  destination: string | null;
  destinationLabel: string;
  modes: ModeReady | null;
  check: ReactNode;
}

function checkCell(check: ConnectivityCheckState, available: boolean, label: string): ReactNode {
  const r = check.result;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {check.checking ? (
        <Spinner label="Checking…" />
      ) : check.error ? (
        <StatusDot state="error" label="Check failed" />
      ) : r ? (
        <StatusDot state={r.status === "PASS" ? "ok" : r.status === "FAIL" ? "error" : "warn"}
          label={r.status === "PASS" ? "Reachable" : r.status === "FAIL" ? "Unavailable" : "Not available"} />
      ) : (
        <StatusDot state="off" label="Not checked" />
      )}
      {available && (
        <button type="button" onClick={check.run} disabled={check.checking} className={`${buttonGhost} px-2 py-1 text-xs`}
          aria-label={`Run the read-only ${label} connectivity check`}>
          <Icon name="refresh" className="h-3.5 w-3.5" /> Check
        </button>
      )}
    </div>
  );
}

function integrationRows(status: IntegrationsStatus, notionCheck: ConnectivityCheckState, driveCheck: ConnectivityCheckState): IntegrationRow[] {
  const allowed = (m: ExecutionMode) => status.available_modes.includes(m);
  const gate = (r: ModeReady): ModeReady => ({ mock: r.mock && allowed("mock"), dry_run: r.dry_run && allowed("dry_run"), real: r.real && allowed("real") });
  const connection = (key: string) => {
    const i = status.integrations[key];
    if (!i) return <StatusDot state="off" label="Not reported" />;
    const live = i.transport !== "mock" && i.configured;
    return <StatusDot state={live ? "ok" : "off"} label={live ? `Connected (${i.transport.toUpperCase()})` : i.transport === "mock" ? "Mock adapter" : "Not configured"} />;
  };
  const notion = status.notion_sandbox;
  const drive = status.google_drive_sandbox;
  const teams = status.teams_sandbox;
  return [
    {
      name: "Notion", connection: connection("notion"), destination: notion.parent_alias, destinationLabel: "Sandbox parent",
      modes: gate({ mock: true, dry_run: notion.dry_run_ready, real: notion.real_ready }),
      check: checkCell(notionCheck, notion.real_ready, "Notion"),
    },
    {
      name: "Google Drive",
      connection: drive?.transport === "rest"
        ? <StatusDot state={drive.authorized ? "ok" : "warn"} label={drive.authorized ? "Connected (REST, drive.file)" : "OAuth not authorized"} />
        : connection("google_drive"),
      destination: drive?.folder_alias ?? null, destinationLabel: "Sandbox folder",
      modes: gate({ mock: true, dry_run: Boolean(drive?.dry_run_ready), real: Boolean(drive?.real_ready) }),
      check: checkCell(driveCheck, Boolean(drive?.real_ready), "Google Drive"),
    },
    {
      name: "Microsoft Teams", connection: connection("teams"), destination: teams?.channel_alias ?? null, destinationLabel: "Sandbox channel",
      modes: gate({ mock: teamsAvailability(status, "mock").available, dry_run: teamsAvailability(status, "dry_run").available,
        real: teamsAvailability(status, "real").available }),
      check: <span className="text-xs text-charcoal-500">No read-only check (a webhook cannot be probed without posting)</span>,
    },
  ];
}

function ModeDots({ modes }: { modes: ModeReady }) {
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1" aria-label="Ready modes">
      {EXECUTION_MODES.map((m) => (
        <li key={m}>
          <StatusDot state={modes[m] ? "ok" : "off"} label={`${MODE_LABELS[m]}${modes[m] ? "" : " (not ready)"}`} />
        </li>
      ))}
    </ul>
  );
}

function IntegrationStatus({ status, notionCheck, driveCheck }: Pick<Props, "status" | "notionCheck" | "driveCheck">) {
  if (!status) return <EmptyState title="Integration status unavailable">Only mock mode is offered until the API responds.</EmptyState>;
  const p = status.planner;
  return (
    <ul className="divide-y divide-charcoal-100" aria-label="Integrations">
      {integrationRows(status, notionCheck, driveCheck).map((row) => (
        <li key={row.name} className="grid gap-x-6 gap-y-2 py-4 first:pt-0 last:pb-0 md:grid-cols-[10rem_1fr_1fr] xl:grid-cols-[10rem_1fr_1fr_1.2fr]">
          <div>
            <p className="font-semibold text-charcoal-900">{row.name}</p>
            <div className="mt-1">{row.connection}</div>
          </div>
          <div className="text-sm">
            <p className="text-xs text-charcoal-500">{row.destinationLabel}</p>
            <p className="font-mono text-xs text-charcoal-800">{row.destination ?? "not configured"}</p>
          </div>
          <div>{row.modes && <ModeDots modes={row.modes} />}</div>
          <div className="md:col-span-3 xl:col-span-1">{row.check}</div>
        </li>
      ))}
      <li className="grid gap-x-6 gap-y-2 py-4 last:pb-0 md:grid-cols-[10rem_1fr_1fr] xl:grid-cols-[10rem_1fr_1fr_1.2fr]">
        <div>
          <p className="font-semibold text-charcoal-900">AI planner</p>
          <div className="mt-1">
            <StatusDot state={p?.ai_ready ? "ok" : p && p.mode !== "mock" ? "error" : "off"}
              label={p?.ai_ready ? "Configured" : p?.mode === "mock" ? "Not AI (mock planner)" : "Not configured"} />
          </div>
        </div>
        <div className="text-sm">
          <p className="text-xs text-charcoal-500">Provider order</p>
          <p className="font-medium text-charcoal-800">{plannerSummary(status)}</p>
        </div>
        <div className="text-xs text-charcoal-600">
          {p?.mode === "auto" && p.fallback ? (
            <StatusDot state={p.fallback_ready ? "ok" : "warn"}
              label={`Fallback ${aiPlannerLabel(p.fallback) ?? p.fallback}: ${p.fallback_ready ? "ready" : "not configured"}`} />
          ) : (
            p?.model && <span className="font-mono">{p.model}</span>
          )}
        </div>
        <div className="text-xs text-charcoal-500 md:col-span-3 xl:col-span-1">{p?.issue ?? "Falls back only on provider outages."}</div>
      </li>
    </ul>
  );
}

// ---------- page ----------

export function DashboardPage({ status, statusError, mode, history, onNavigate, notionCheck, driveCheck }: Props) {
  const total = history.length;
  const completed = history.filter((h) => h.status === "completed").length;
  const attention = history.filter((h) => NEEDS_ATTENTION.has(h.status)).length;
  const byMode = EXECUTION_MODES.map((m) => `${MODE_LABELS[m]} ${history.filter((h) => h.mode === m).length}`).join(" · ");
  const last = history[0];
  return (
    <div className="space-y-6">
      <PageHeader icon="home" title="Dashboard"
        description="Plan business work in plain language, review every action, and approve before anything is written." />
      {statusError && (
        <Alert tone="warning" title="Integration status is unavailable">
          {statusError} Only mock mode is offered.
        </Alert>
      )}

      <section aria-labelledby="metrics-heading">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 id="metrics-heading" className="text-sm font-semibold text-charcoal-900">Workflow executions</h3>
          <p className="text-xs text-charcoal-500">
            Runs started from this browser tab{total >= HISTORY_CAP ? ` (latest ${HISTORY_CAP})` : ""}. They reset when the page reloads.
          </p>
        </div>
        <div className="mt-3 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile label="Executions" value={String(total)} hint={total ? byMode : "No runs yet in this session"} accent />
          <StatTile label="Completed" value={String(completed)} hint={total ? `of ${total} run${total === 1 ? "" : "s"}` : "—"} />
          <StatTile label="Needs attention" value={String(attention)} hint="Failed, rejected, rolled back or partial" />
          <StatTile label="Success rate" value={total ? `${Math.round((completed / total) * 100)}%` : "—"}
            hint={last ? `Last run ${new Date(last.finishedAt).toLocaleTimeString()}` : "Shown after the first run"} />
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(18rem,1fr)]">
        <Card id="dashboard-integrations" title="Integration status"
          subtitle={<>Configuration reported by the server; checks are read-only and never write. Current mode: <strong>{MODE_LABELS[mode]}</strong>.</>}>
          <IntegrationStatus status={status} notionCheck={notionCheck} driveCheck={driveCheck} />
        </Card>
        <Card id="dashboard-quick-actions" title="Quick actions">
          <ul className="space-y-2">
            {QUICK_ACTIONS.map((a) => (
              <li key={a.id}>
                <button type="button" onClick={() => onNavigate(a.id)}
                  className="group flex w-full items-center gap-3 rounded-lg border border-charcoal-200 bg-white p-3 text-left hover:border-brand-300 hover:bg-brand-50/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600">
                  <span aria-hidden="true" className="grid h-9 w-9 shrink-0 place-items-center rounded-md bg-charcoal-100 text-charcoal-700 group-hover:bg-brand-600 group-hover:text-white">
                    <Icon name={a.icon} className="h-5 w-5" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold text-charcoal-900">{a.label}</span>
                    <span className="block truncate text-xs text-charcoal-500">{a.detail}</span>
                  </span>
                  <Icon name="arrow-right" className="h-4 w-4 shrink-0 text-charcoal-400 group-hover:text-brand-600" />
                </button>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card id="dashboard-recent" title="Recent executions" subtitle="This browser tab only."
        actions={<button type="button" className={buttonSecondary} onClick={() => onNavigate("history")}>View history</button>}>
        {history.length === 0 ? (
          <EmptyState title="No executions yet">Plan a workflow in a workspace and approve it to see it here.</EmptyState>
        ) : (
          <div className="-mx-4 overflow-x-auto sm:mx-0">
            <table className="w-full min-w-[34rem] text-left text-sm">
              <thead className="border-b border-charcoal-200 text-xs uppercase tracking-wide text-charcoal-500">
                <tr>
                  <th scope="col" className="px-4 py-2 font-medium sm:pl-0">Workflow</th>
                  <th scope="col" className="px-4 py-2 font-medium">Workspace</th>
                  <th scope="col" className="px-4 py-2 font-medium">Mode</th>
                  <th scope="col" className="px-4 py-2 font-medium">Finished</th>
                  <th scope="col" className="px-4 py-2 font-medium sm:pr-0">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-charcoal-100">
                {history.slice(0, 5).map((e) => (
                  <tr key={e.workflowId}>
                    <td className="px-4 py-2.5 sm:pl-0">
                      <p className="font-medium text-charcoal-900">{e.name}</p>
                      <p className="font-mono text-[11px] text-charcoal-500">{e.workflowId}</p>
                    </td>
                    <td className="px-4 py-2.5 text-charcoal-700">{e.section ? SECTION_BY_ID[e.section].label : "—"}</td>
                    <td className="px-4 py-2.5 text-charcoal-700">{e.mode ? MODE_LABELS[e.mode] : "—"}</td>
                    <td className="px-4 py-2.5 text-charcoal-700">{new Date(e.finishedAt).toLocaleTimeString()}</td>
                    <td className="px-4 py-2.5 sm:pr-0"><WorkflowStatusBadge status={e.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <section aria-labelledby="workspaces-heading">
        <h3 id="workspaces-heading" className="text-sm font-semibold text-charcoal-900">Workspaces</h3>
        <ul className="mt-3 grid gap-4 md:grid-cols-2 xl:grid-cols-5">
          {WORKSPACES.map((id) => {
            const s = SECTION_BY_ID[id];
            const r = capabilityReadiness(id, status);
            return (
              <li key={id} className="flex flex-col rounded-xl border border-charcoal-200 bg-white p-4 shadow-sm">
                <div className="flex items-start justify-between gap-2">
                  <span aria-hidden="true" className="grid h-9 w-9 place-items-center rounded-md bg-brand-50 text-brand-600">
                    <Icon name={s.icon} className="h-5 w-5" />
                  </span>
                  <ReadinessPill value={r.value} />
                </div>
                <h4 className="mt-3 text-sm font-semibold text-charcoal-900">{s.label}</h4>
                {r.detail && <p className="mt-1 flex-1 text-xs text-charcoal-600">{r.detail}</p>}
                <button type="button" onClick={() => onNavigate(id)} aria-label={`Open ${s.label}`}
                  className="mt-3 self-start text-sm font-semibold text-brand-700 hover:text-brand-800 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600">
                  Open
                </button>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
