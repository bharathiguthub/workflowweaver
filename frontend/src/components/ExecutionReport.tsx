import {
  MODE_LABELS,
  actionLabel,
  integrationLabel,
  type ExecutionEvent,
  type ExecutionResponse,
  type StepResult,
} from "../api/types";
import type { ReportState } from "../state/useExecution";
import { Alert, Badge, Spinner } from "./StatusBadge";

interface Props {
  report: ExecutionResponse | null;
  state: ReportState;
  error: string | null;
  terminal: ExecutionEvent | null;
  onRetry: () => void;
}

const STATUS_TONE = {
  COMPLETED: "success",
  FAILED: "danger",
  ROLLED_BACK: "slate",
  PARTIALLY_ROLLED_BACK: "warning",
  PARTIALLY_COMPLETED: "warning",
  COMPENSATION_FAILED: "danger",
  REJECTED: "danger",
} as const;

export function ExecutionReport({ report, state, error, terminal, onRetry }: Props) {
  return (
    <section aria-labelledby="report-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id="report-heading" className="text-base font-semibold text-slate-900">
        5. Execution report
      </h2>
      {state === "loading" && (
        <div className="mt-3">
          <Spinner label="Loading report…" />
        </div>
      )}
      {state === "error" && (
        <div className="mt-3">
          <Alert title="Could not load the report">
            {error}{" "}
            <button type="button" onClick={onRetry} className="font-semibold underline">
              Try again
            </button>
          </Alert>
        </div>
      )}
      {state === "unavailable" && (
        // The backend does not store REJECTED runs; show what the terminal event reported.
        <div className="mt-3 space-y-2 text-sm text-slate-700">
          <Alert tone="info" title="No stored report for this run">
            The server keeps reports only for runs that executed. This outcome comes from the final stream event.
          </Alert>
          {terminal && <TerminalSummary event={terminal} />}
        </div>
      )}
      {state === "loaded" && report && <ReportBody report={report} />}
    </section>
  );
}

function TerminalSummary({ event }: { event: ExecutionEvent }) {
  const m = event.metadata;
  const list = (value: unknown) => (Array.isArray(value) ? value.map(String) : []);
  return (
    <dl className="grid grid-cols-[10rem_1fr] gap-1">
      <dt className="text-slate-500">Status</dt>
      <dd className="font-semibold">{String(m.status ?? "unknown")}</dd>
      <dt className="text-slate-500">Error codes</dt>
      <dd className="font-mono text-xs">{list(m.error_codes).join(", ") || "none"}</dd>
      <dt className="text-slate-500">Validation issues</dt>
      <dd className="font-mono text-xs">{list(m.validation_issue_codes).join(", ") || "none"}</dd>
    </dl>
  );
}

function ReportBody({ report }: { report: ExecutionResponse }) {
  const totalAttempts = Object.values(report.attempts).reduce((a, b) => a + b, 0);
  return (
    <div className="mt-3 space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={STATUS_TONE[report.status]} label={report.status.replace(/_/g, " ")} />
        {report.replayed && <Badge tone="neutral" label="Replayed from stored result" />}
        <Badge
          tone={report.mode === "real" ? "danger" : report.mode === "dry_run" ? "info" : "warning"}
          label={`Mode: ${MODE_LABELS[report.mode] ?? report.mode}`}
        />
      </div>
      <p className="text-sm text-slate-700">{report.message}</p>
      <ResourceReferences report={report} />
      <dl className="grid grid-cols-3 gap-3 text-center">
        <Stat label="Tool attempts" value={totalAttempts} />
        <Stat label="Retries" value={report.retry_delays_seconds.length} />
        <Stat label="Errors" value={report.errors.length} />
      </dl>

      <div>
        <h3 className="text-sm font-semibold text-slate-800">Steps</h3>
        <div className="mt-2 overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <caption className="sr-only">Step results</caption>
            <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
              <tr>
                <th scope="col" className="py-2 pr-3">#</th>
                <th scope="col" className="py-2 pr-3">Step</th>
                <th scope="col" className="py-2 pr-3">Status</th>
                <th scope="col" className="py-2 pr-3">Attempts</th>
                <th scope="col" className="py-2">Resource / error</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {report.step_results.map((r) => (
                <tr key={r.step_number}>
                  <td className="py-2 pr-3 font-mono text-xs">{r.step_number}</td>
                  <td className="py-2 pr-3">
                    {integrationLabel(r.integration)} · {actionLabel(r.action)}
                  </td>
                  <td className="py-2 pr-3">{r.status}</td>
                  <td className="py-2 pr-3">{r.attempts}</td>
                  <td className="break-all py-2 font-mono text-xs">
                    {r.error ? `${r.error.code}` : r.resource_id ?? "—"}
                    {r.replayed && " (replayed)"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {report.errors.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-slate-800">Errors</h3>
          <ul className="mt-2 space-y-1 text-sm">
            {report.errors.map((e, i) => (
              <li key={`${e.code}-${i}`} className="rounded bg-rose-50 px-2 py-1">
                <span className="font-mono text-xs">{e.code}</span>
                {e.step_number !== null && <> · step {e.step_number}</>}
                {e.attempt !== null && <> · attempt {e.attempt}</>}
                {e.retryable && " · retryable"}: {e.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.compensation_results.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-slate-800">Compensation (rollback)</h3>
          <ul className="mt-2 space-y-1 text-sm">
            {report.compensation_results.map((c) => (
              <li key={c.step_number} className="rounded bg-violet-50 px-2 py-1">
                Step {c.step_number} · {integrationLabel(c.integration)} {actionLabel(c.action).toLowerCase()} ·{" "}
                <span className="font-semibold">{c.status.replace(/_/g, " ")}</span> · {c.attempts} attempt(s) · {c.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.retry_delays_seconds.length > 0 && (
        <p className="text-xs text-slate-500">Retry delays: {report.retry_delays_seconds.map((d) => `${d}s`).join(", ")}</p>
      )}
    </div>
  );
}

const text = (value: unknown): string | null => (typeof value === "string" && value ? value : null);
/** Only https links returned by the backend are rendered as links. */
const safeUrl = (value: unknown): string | null => {
  const url = text(value);
  return url && url.startsWith("https://") ? url : null;
};

/** How a step's resource is described. Derived from integration + action only (the report carries
 * no resource type); unknown combinations get neutral wording and no claims. */
export interface ResourceKind {
  /** Label of the identifier row. */
  idLabel: string;
  /** The step created a new resource. */
  creates: boolean;
  /** The step only read an existing item (known Google Drive read actions only). */
  readOnly: boolean;
  /** Note for a resource created in real mode that the report does not show as rolled back. */
  keptNote: string | null;
}

const NOTION_KEPT_NOTE =
  "This page exists in your Notion sandbox and was not rolled back. Verify it in Notion, then delete it when done.";
const DRIVE_CREATED_NOTE = "Created in your approved Google Drive sandbox.";
// Phase 7.1: a 2xx means the Workflows webhook ACCEPTED the request; Microsoft does not confirm
// the post itself, returns no message id, and there is no API to unsend or delete it.
const TEAMS_SENT_NOTE =
  "Accepted by the Teams Workflows webhook of your sandbox channel. Check the channel to confirm it was posted; WorkflowWeaver cannot unsend or delete Teams messages.";

export function resourceKind(integration: string, action: string, mode: ExecutionResponse["mode"]): ResourceKind {
  if (integration === "google_drive") {
    switch (action) {
      case "create_folder":
        return { idLabel: "Folder ID", creates: true, readOnly: false, keptNote: DRIVE_CREATED_NOTE };
      case "create_document":
        return { idLabel: "File ID", creates: true, readOnly: false, keptNote: DRIVE_CREATED_NOTE };
      case "read_document":
        return { idLabel: "File ID", creates: false, readOnly: true, keptNote: null };
      case "list_files":
        return { idLabel: "Folder ID", creates: false, readOnly: true, keptNote: null };
      default:
        return { idLabel: "Resource ID", creates: false, readOnly: false, keptNote: null };
    }
  }
  if (integration === "teams") {
    // A local reference derived from the run (webhooks return no Teams message id).
    return action === "send_message"
      ? { idLabel: "Message reference", creates: true, readOnly: false, keptNote: TEAMS_SENT_NOTE }
      : { idLabel: "Resource ID", creates: false, readOnly: false, keptNote: null };
  }
  if (integration === "notion") {
    // Unchanged Notion wording: tasks are database pages, so both are "Page ID" in real mode.
    return { idLabel: mode === "real" ? "Page ID" : "Resource ID", creates: true, readOnly: false, keptNote: NOTION_KEPT_NOTE };
  }
  // Other integrations: neutral wording, no read-only or sandbox claims.
  return { idLabel: "Resource ID", creates: true, readOnly: false, keptNote: null };
}

function ResourceReferences({ report }: { report: ExecutionResponse }) {
  const created = report.step_results.filter((r: StepResult) => r.status === "SUCCEEDED" && r.resource_id);
  if (created.length === 0) return null;
  const compensated = new Set(
    report.compensation_results
      .filter((c) => c.status === "COMPENSATED" || c.status === "ALREADY_COMPENSATED")
      .map((c) => c.step_number),
  );
  const anyCreated = created.some((r) => resourceKind(r.integration, r.action, report.mode).creates);
  const onlyTeams = created.every((r) => r.integration === "teams");
  return (
    <div>
      <h3 className="text-sm font-semibold text-slate-800">
        {report.mode === "real" && anyCreated ? (onlyTeams ? "Sent to Microsoft Teams" : "Created in the real service") : "Resource references"}
      </h3>
      <ul className="mt-2 space-y-2" aria-label="Resource references">
        {created.map((r) => {
          const url = safeUrl(r.output.url);
          const title = text(r.output.title);
          const destination = text(r.output.destination);
          const channel = r.integration === "teams" ? text(r.output.channel) : null;
          const wouldCall = text(r.output.would_call);
          const kind = resourceKind(r.integration, r.action, report.mode);
          // Only for resources this run created in real mode that the report does not list as rolled back.
          const keptNote =
            report.mode === "real" && kind.creates && !compensated.has(r.step_number) ? kind.keptNote : null;
          return (
            <li key={r.step_number} className="rounded-lg border border-slate-200 p-3 text-sm">
              <div className="font-semibold text-slate-900">
                Step {r.step_number} · {integrationLabel(r.integration)} {actionLabel(r.action).toLowerCase()}
                {compensated.has(r.step_number) && <span className="ml-2 font-normal text-violet-700">(rolled back)</span>}
              </div>
              <dl className="mt-1 grid grid-cols-[7rem_1fr] gap-x-2 gap-y-0.5 text-xs">
                {title && (
                  <>
                    <dt className="text-slate-500">Title</dt>
                    <dd className="text-slate-800">{title}</dd>
                  </>
                )}
                {typeof r.output.verification === "string" && (
                  <>
                    <dt className="text-slate-500">Read-back</dt>
                    <dd className="text-slate-800">
                      {r.output.verification === "verified"
                        ? "Verified: read back from the service after creation"
                        : r.output.verification === "not_performed"
                          ? "Not performed (dry run)"
                          : "Unavailable: created, but not verified"}
                    </dd>
                  </>
                )}
                {r.output.source_verified === true && (
                  <>
                    <dt className="text-slate-500">Source check</dt>
                    <dd className="text-slate-800">Content matched the approved source (SHA-256); the file was only read</dd>
                  </>
                )}
                {channel && (
                  <>
                    <dt className="text-slate-500">Channel</dt>
                    <dd className="font-mono text-slate-800">{channel}</dd>
                  </>
                )}
                {destination && (
                  <>
                    <dt className="text-slate-500">Destination</dt>
                    <dd className="font-mono text-slate-800">{destination}</dd>
                  </>
                )}
                <dt className="text-slate-500">{kind.idLabel}</dt>
                <dd className="break-all font-mono text-slate-800">{r.resource_id}</dd>
                {wouldCall && (
                  <>
                    <dt className="text-slate-500">Would call</dt>
                    <dd className="font-mono text-slate-800">
                      {wouldCall}
                      {r.output.sent === false ? " (not sent)" : ""}
                    </dd>
                  </>
                )}
                {url && (
                  <>
                    <dt className="text-slate-500">Link</dt>
                    <dd className="break-all">
                      <a href={url} target="_blank" rel="noopener noreferrer" className="font-medium text-indigo-700 underline">
                        {url}
                      </a>
                    </dd>
                  </>
                )}
              </dl>
              {keptNote && <p className="mt-2 text-xs text-rose-800">{keptNote}</p>}
              {kind.readOnly && (
                <p className="mt-2 text-xs text-slate-600">Read-only step: an existing item was read; nothing was created.</p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg bg-slate-50 p-3">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="text-xl font-bold text-slate-900">{value}</dd>
    </div>
  );
}
