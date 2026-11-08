import type { ReactNode } from "react";

import { aiPlannerLabel, MODE_LABELS, type DriveConnectivity, type IntegrationsStatus } from "../api/types";
import { Badge, Spinner } from "../components/StatusBadge";
import { Card, PageHeader, buttonSecondary } from "../components/layout/Page";
import { SECTION_BY_ID } from "../navigation";
import { plannerSummary } from "./readiness";

const LABELS: Record<string, string> = { notion: "Notion", google_drive: "Google Drive", teams: "Microsoft Teams" };

interface Props {
  status: IntegrationsStatus | null;
  statusError: string | null;
  /** The existing Notion readiness panel (with its read-only connectivity check). */
  notionReadiness: ReactNode;
  driveConnectivity: DriveConnectivity | null;
  driveConnectivityError: string | null;
  driveChecking: boolean;
  onDriveCheck: () => void;
}

/** Read-only view of the server configuration. Everything is configured in the backend .env. */
export function SettingsPage({ status, statusError, notionReadiness, driveConnectivity, driveConnectivityError, driveChecking, onDriveCheck }: Props) {
  const p = status?.planner;
  const drive = status?.google_drive_sandbox;
  return (
    <div className="space-y-6">
      <PageHeader icon="settings" title="Integrations & Settings" description={SECTION_BY_ID.settings.description} />
      <p className="text-sm text-slate-600">
        Settings are read-only here: they come from the backend <span className="font-mono">.env</span>. Credentials, tokens and
        webhook URLs are never sent to the browser; only aliases and readiness are shown.
      </p>
      {statusError && <p role="alert" className="text-sm text-rose-700">Integration status is unavailable: {statusError}</p>}
      <div className="grid gap-6 xl:grid-cols-2">
        <Card id="settings-planner" title="AI planner">
          <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-2 text-sm">
            <dt className="text-slate-500">Planner</dt>
            <dd className="font-medium text-slate-800">{plannerSummary(status)}</dd>
            <dt className="text-slate-500">PLANNER_MODE</dt>
            <dd className="font-mono text-xs text-slate-800">{p?.mode ?? "unknown"}</dd>
            {p?.mode === "auto" && (
              <>
                <dt className="text-slate-500">Primary</dt>
                <dd className="text-slate-800">
                  {aiPlannerLabel(p.primary) ?? p.primary} <span className="font-mono text-xs">{p.model}</span>{" "}
                  <Badge tone={p.ai_ready ? "success" : "danger"} label={p.ai_ready ? "Ready" : "Not configured"} />
                </dd>
                <dt className="text-slate-500">Fallback</dt>
                <dd className="text-slate-800">
                  {p.fallback ? (
                    <>
                      {aiPlannerLabel(p.fallback) ?? p.fallback} <span className="font-mono text-xs">{p.fallback_model}</span>{" "}
                      <Badge tone={p.fallback_ready ? "success" : "warning"} label={p.fallback_ready ? "Ready" : "Not configured"} />
                    </>
                  ) : (
                    "None"
                  )}
                </dd>
              </>
            )}
            {p?.issue && (
              <>
                <dt className="text-slate-500">Issue</dt>
                <dd className="text-amber-800">{p.issue}</dd>
              </>
            )}
          </dl>
          <p className="mt-3 text-xs text-slate-500">
            Fallback happens only on provider outages (rate limits, timeouts, provider errors), never for invalid keys or rejected plans.
          </p>
        </Card>
        <Card id="settings-safety" title="Execution safeguards">
          <dl className="grid grid-cols-[11rem_1fr] gap-x-3 gap-y-2 text-sm">
            <dt className="text-slate-500">Server mode ceiling</dt>
            <dd className="text-slate-800">{status ? MODE_LABELS[status.mode] : "unknown"}</dd>
            <dt className="text-slate-500">Real writes switch</dt>
            <dd>
              <Badge tone={status?.real_writes_enabled ? "danger" : "neutral"} label={status?.real_writes_enabled ? "Enabled" : "Disabled"} />
            </dd>
            <dt className="text-slate-500">Operator token</dt>
            <dd className="text-slate-800">{status?.write_authorization_configured ? "Configured (never shown)" : "Not configured"}</dd>
            <dt className="text-slate-500">Approval</dt>
            <dd className="text-slate-800">Required for every run; bound to the reviewed plan</dd>
          </dl>
        </Card>
        <Card id="settings-integrations" title="Integrations">
          <ul className="divide-y divide-slate-100">
            {status
              ? Object.entries(status.integrations).map(([name, i]) => (
                  <li key={name} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                    <span className="font-medium text-slate-800">{LABELS[name] ?? name}</span>
                    <span className="flex items-center gap-2 text-xs text-slate-600">
                      transport <span className="font-mono">{i.transport}</span>
                      <Badge tone={i.configured ? "success" : "neutral"} label={i.configured ? "Configured" : "Not configured"} />
                    </span>
                  </li>
                ))
              : <li className="py-2 text-sm text-slate-500">Status unavailable.</li>}
          </ul>
        </Card>
        <Card
          id="settings-drive"
          title="Google Drive sandbox"
          actions={
            <button type="button" className={buttonSecondary} onClick={onDriveCheck} disabled={driveChecking || !drive?.oauth_client_configured}>
              Check Drive connectivity (read-only)
            </button>
          }
        >
          <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-2 text-sm">
            <dt className="text-slate-500">Sandbox folder</dt>
            <dd className="font-mono text-xs text-slate-800">{drive?.folder_alias ?? "not configured"}</dd>
            <dt className="text-slate-500">OAuth scope</dt>
            <dd className="font-mono text-xs text-slate-800">{drive?.scope ?? "drive.file"} (fixed; only files this app creates)</dd>
            <dt className="text-slate-500">Authorized</dt>
            <dd className="text-slate-800">{drive?.authorized ? "Yes" : "No"}</dd>
            <dt className="text-slate-500">Sandbox only</dt>
            <dd className="text-slate-800">{drive?.sandbox_only ? "Yes" : "No"}</dd>
            <dt className="text-slate-500">Last check</dt>
            <dd className="text-slate-800">
              {driveChecking ? <Spinner label="Checking…" /> : driveConnectivityError ?? driveConnectivity?.status ?? "Not run"}
            </dd>
          </dl>
          {drive && drive.issues.length > 0 && (
            <ul className="mt-3 list-disc space-y-0.5 pl-5 text-xs text-amber-800">
              {drive.issues.map((i) => (
                <li key={i}>{i}</li>
              ))}
            </ul>
          )}
        </Card>
        <div className="xl:col-span-2">{notionReadiness}</div>
      </div>
    </div>
  );
}
