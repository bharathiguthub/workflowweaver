import { actionLabel, integrationLabel, type ExecutionEvent, type ExecutionResponse } from "../api/types";
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
        <Badge tone="warning" label={`Mode: ${report.mode}`} />
      </div>
      <p className="text-sm text-slate-700">{report.message}</p>
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

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg bg-slate-50 p-3">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="text-xl font-bold text-slate-900">{value}</dd>
    </div>
  );
}
