import { integrationLabel, type IntegrationsStatus, type NotionConnectivity } from "../api/types";
import { Badge, Spinner } from "./StatusBadge";

interface Props {
  status: IntegrationsStatus | null;
  statusError: string | null;
  connectivity: NotionConnectivity | null;
  connectivityError: string | null;
  checking: boolean;
  onCheck: () => void;
}

const yes = (value: boolean, good = "Yes", bad = "No") => <Badge tone={value ? "success" : "warning"} label={value ? good : bad} />;

export function IntegrationReadiness({ status, statusError, connectivity, connectivityError, checking, onCheck }: Props) {
  return (
    <section aria-labelledby="readiness-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <h2 id="readiness-heading" className="text-sm font-semibold text-slate-900">
        Integration readiness
      </h2>
      {!status && !statusError && (
        <div className="mt-2">
          <Spinner label="Loading…" />
        </div>
      )}
      {statusError && <p className="mt-2 text-sm text-amber-800">Status unavailable: {statusError} Only mock mode is offered.</p>}
      {status && (
        <div className="mt-2 space-y-3 text-sm">
          <dl className="grid grid-cols-[1fr_auto] items-center gap-x-2 gap-y-1">
            <dt className="text-slate-600">Backend mode (ceiling)</dt>
            <dd className="font-mono text-xs">{status.mode}</dd>
            <dt className="text-slate-600">Real writes switch</dt>
            <dd>{yes(status.real_writes_enabled, "Enabled", "Disabled")}</dd>
            <dt className="text-slate-600">Operator token configured</dt>
            <dd>{yes(status.write_authorization_configured)}</dd>
            <dt className="text-slate-600">Configuration valid</dt>
            <dd>{yes(status.configuration_valid)}</dd>
          </dl>
          {status.configuration_error && <p className="text-xs text-rose-700">{status.configuration_error}</p>}

          <ul className="space-y-1" aria-label="Integrations">
            {Object.entries(status.integrations).map(([key, integration]) => (
              <li key={key} className="flex items-center justify-between gap-2">
                <span>{integrationLabel(key)}</span>
                {integration.transport === "mock" ? (
                  <Badge tone="neutral" label="Mock" />
                ) : (
                  yes(integration.configured, "Configured", "Not configured")
                )}
              </li>
            ))}
          </ul>

          <div className="rounded-lg bg-slate-50 p-2">
            <h3 className="text-xs font-semibold uppercase text-slate-500">Notion sandbox</h3>
            <p className="mt-1 text-xs text-slate-700">
              Parent alias:{" "}
              <span className="font-mono">{status.notion_sandbox.parent_alias ?? "not configured"}</span>
            </p>
            {status.notion_sandbox.issues.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-amber-800" aria-label="Notion sandbox issues">
                {status.notion_sandbox.issues.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            )}
            {status.notion_sandbox.real_ready && (
              <div className="mt-2 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={onCheck}
                    disabled={checking}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-semibold text-slate-800 hover:bg-slate-100 disabled:opacity-50"
                  >
                    Check Notion connection
                  </button>
                  {checking && <Spinner label="Checking (read-only)…" />}
                  {!checking && connectivity && (
                    <Badge
                      tone={connectivity.status === "PASS" ? "success" : "danger"}
                      label={connectivity.status === "PASS" ? "Notion reachable" : connectivity.status === "FAIL" ? "Notion unavailable" : "Not available"}
                    />
                  )}
                </div>
                <p className="text-xs text-slate-500">Read-only: lists MCP tools and reads the sandbox parent page. Never writes.</p>
                {connectivityError && <p className="text-xs text-rose-700">{connectivityError}</p>}
                {connectivity && connectivity.checks.length > 0 && (
                  <ul className="space-y-0.5 text-xs" aria-label="Notion connectivity checks">
                    {connectivity.checks.map((c) => (
                      <li key={c.name}>
                        <span className={c.status === "PASS" ? "text-emerald-700" : "text-rose-700"}>[{c.status}]</span> {c.name}
                        {c.detail && <span className="text-slate-500"> · {c.detail}</span>}
                      </li>
                    ))}
                  </ul>
                )}
                {connectivity && connectivity.issues.length > 0 && (
                  <ul className="list-disc pl-4 text-xs text-amber-800">
                    {connectivity.issues.map((issue) => (
                      <li key={issue}>{issue}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
