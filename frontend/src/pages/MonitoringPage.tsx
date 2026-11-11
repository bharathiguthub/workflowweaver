import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { errorMessage, getMonitoringLogs, getMonitoringSummary, getMonitoringTraces } from "../api/client";
import type { MonitoringLogs, MonitoringSummary, MonitoringTraces } from "../api/types";
import { Alert, Badge, Spinner } from "../components/StatusBadge";
import { Icon } from "../components/layout/Icon";
import { Card, EmptyState, PageHeader, StatTile, StatusDot, buttonSecondary } from "../components/layout/Page";
import { SECTION_BY_ID } from "../navigation";

/*
 * Monitoring & Observability (Phase 8.5). Every value shown comes from the backend's monitoring
 * endpoints, which read the real Prometheus registry, log buffer and run index of the API process,
 * plus server-side reachability probes of configured services. Nothing is estimated or filled in:
 * unconfigured sources say "not configured", empty data says "no data yet".
 */

type TabId = "overview" | "logs" | "traces" | "ai";
const TABS: { id: TabId; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "logs", label: "Logs" },
  { id: "traces", label: "Workflow Traces" },
  { id: "ai", label: "AI Monitoring" },
];

const fmt = (n: number | null | undefined, unit = "") => (n === null || n === undefined ? "—" : `${n.toLocaleString()}${unit}`);
const pct = (r: number | null | undefined) => (r === null || r === undefined ? "—" : `${(r * 100).toFixed(1)}%`);
const time = (iso?: string) => (iso ? new Date(iso).toLocaleTimeString() : "—");
const isHttp = (url: string | null | undefined): url is string => Boolean(url && /^https?:\/\//.test(url));

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-sm font-semibold text-brand-700 hover:text-brand-800 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600">
      {children}
      <Icon name="external" className="h-3.5 w-3.5" />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  );
}

function useLoader<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [updated, setUpdated] = useState<Date | null>(null);
  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await load());
      setUpdated(new Date());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [load]);
  return { data, error, loading, updated, run };
}

function Toolbar({ loading, updated, onRefresh, children }: { loading: boolean; updated: Date | null; onRefresh: () => void; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-3">{children}</div>
      <div className="flex items-center gap-3 text-xs text-charcoal-500">
        {loading ? <Spinner label="Loading…" /> : updated ? <span>Updated {updated.toLocaleTimeString()}</span> : null}
        <button type="button" className={buttonSecondary} onClick={onRefresh} disabled={loading}>
          <Icon name="refresh" className="h-4 w-4" /> Refresh
        </button>
      </div>
    </div>
  );
}

function Unavailable({ what, error }: { what: string; error: string }) {
  return (
    <Alert tone="warning" title={`${what} unavailable`}>
      {error} The rest of WorkflowWeaver keeps working; monitoring data appears once the API's monitoring endpoints respond.
    </Alert>
  );
}

// ---------- Overview ----------

function Overview({ summary }: { summary: MonitoringSummary }) {
  const { config, api, workflows, services, links } = summary;
  const finished = workflows.executions_finished.reduce((n, f) => n + f.count, 0);
  const completed = workflows.executions_finished.find((f) => f.status === "completed")?.count ?? 0;
  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-2">
        <Card id="mon-sources" title="Monitoring sources">
          <ul className="divide-y divide-charcoal-100" aria-label="Monitoring sources">
            <li className="flex items-center justify-between gap-2 py-2 text-sm">
              <span className="font-medium text-charcoal-900">Structured logs</span>
              <StatusDot state="ok" label={`${config.log_format.toUpperCase()} · ${config.log_level}`} />
            </li>
            <li className="flex items-center justify-between gap-2 py-2 text-sm">
              <span className="font-medium text-charcoal-900">Prometheus metrics (/metrics)</span>
              <StatusDot state={config.metrics_enabled ? "ok" : "off"}
                label={config.metrics_enabled ? (config.metrics_protected ? "Enabled · token required" : "Enabled") : "Disabled"} />
            </li>
            <li className="flex items-center justify-between gap-2 py-2 text-sm">
              <span className="font-medium text-charcoal-900">OpenTelemetry tracing</span>
              <StatusDot state={config.tracing_enabled ? "ok" : "off"} label={config.tracing_enabled ? "Exporting" : "Off (OTEL_ENABLED=false)"} />
            </li>
            <li className="flex items-center justify-between gap-2 py-2 text-sm">
              <span className="font-medium text-charcoal-900">Langfuse (LLM tracing)</span>
              <StatusDot state={config.langfuse_enabled ? "ok" : "off"} label={config.langfuse_enabled ? "Enabled" : "Off"} />
            </li>
            {services.map((s) => (
              <li key={s.name} className="flex items-center justify-between gap-2 py-2 text-sm">
                <span className="font-medium text-charcoal-900">{s.name}</span>
                <StatusDot
                  state={s.status === "up" ? "ok" : s.status === "down" ? "error" : "off"}
                  label={s.status === "up" ? `Reachable${s.latency_ms !== undefined ? ` · ${s.latency_ms} ms` : ""}`
                    : s.status === "down" ? "Unreachable" : "Not configured"}
                />
              </li>
            ))}
          </ul>
        </Card>
        <Card id="mon-links" title="Dashboards and tools">
          {links.dashboards.length === 0 && !isHttp(links.langfuse) ? (
            <EmptyState title="No monitoring links configured">
              Set GRAFANA_URL (and LANGFUSE_UI_URL) in the backend .env to link the local Grafana dashboards and Langfuse.
            </EmptyState>
          ) : (
            <ul className="space-y-3" aria-label="Monitoring links">
              {links.dashboards.filter((d) => isHttp(d.url)).map((d) => (
                <li key={d.uid} className="flex items-center justify-between gap-2">
                  <span className="text-sm text-charcoal-700">Grafana · {d.title}</span>
                  <ExternalLink href={d.url}>Open</ExternalLink>
                </li>
              ))}
              {isHttp(links.langfuse) && (
                <li className="flex items-center justify-between gap-2">
                  <span className="text-sm text-charcoal-700">Langfuse · LLM traces</span>
                  <ExternalLink href={links.langfuse}>Open</ExternalLink>
                </li>
              )}
            </ul>
          )}
        </Card>
      </div>

      <section aria-labelledby="mon-api-heading">
        <h3 id="mon-api-heading" className="text-sm font-semibold text-charcoal-900">API and workflows</h3>
        <div className="mt-3 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile label="HTTP requests" value={fmt(api.requests)} hint={`${fmt(api.in_progress)} in flight`} accent />
          <StatTile label="5xx error rate" value={pct(api.error_rate_5xx)} hint={`${fmt(api.errors_5xx)} 5xx · ${fmt(api.errors_4xx)} 4xx`} />
          <StatTile label="Average latency" value={fmt(api.avg_latency_ms, " ms")} hint="All routes" />
          <StatTile label="Executions" value={fmt(workflows.executions_started)}
            hint={finished ? `${completed} completed of ${finished} finished · ${workflows.step_retries} retries` : "None finished yet"} />
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card id="mon-routes" title="Busiest routes">
          {api.routes.length === 0 ? (
            <EmptyState title="No requests recorded yet" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[26rem] text-left text-sm">
                <thead className="border-b border-charcoal-200 text-xs uppercase tracking-wide text-charcoal-500">
                  <tr><th scope="col" className="py-2 pr-3">Route</th><th scope="col" className="py-2 pr-3">Requests</th><th scope="col" className="py-2 pr-3">Errors</th><th scope="col" className="py-2">Avg</th></tr>
                </thead>
                <tbody className="divide-y divide-charcoal-100">
                  {api.routes.map((r) => (
                    <tr key={r.route}>
                      <td className="py-2 pr-3 font-mono text-xs text-charcoal-800">{r.route}</td>
                      <td className="py-2 pr-3 tabular-nums">{fmt(r.requests)}</td>
                      <td className={`py-2 pr-3 tabular-nums ${r.errors ? "text-rose-700" : ""}`}>{fmt(r.errors)}</td>
                      <td className="py-2 tabular-nums">{fmt(r.avg_ms, " ms")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        <Card id="mon-outcomes" title="Workflow outcomes">
          {workflows.executions_finished.length === 0 && workflows.rejections.length === 0 && workflows.plans.length === 0 ? (
            <EmptyState title="No planning or execution recorded yet" />
          ) : (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              {workflows.plans.map((p) => (
                <div key={`p-${p.target}-${p.status}`} className="contents">
                  <dt className="text-charcoal-500">Plans · {p.target} · {p.status}</dt>
                  <dd className="tabular-nums font-medium">{fmt(p.count)}</dd>
                </div>
              ))}
              {workflows.executions_finished.map((f) => (
                <div key={`f-${f.status}`} className="contents">
                  <dt className="text-charcoal-500">Executions · {f.status.replace(/_/g, " ")}</dt>
                  <dd className="tabular-nums font-medium">{fmt(f.count)}</dd>
                </div>
              ))}
              {workflows.rejections.map((r) => (
                <div key={`r-${r.reason}`} className="contents">
                  <dt className="text-charcoal-500">Rejected · {r.reason}</dt>
                  <dd className="tabular-nums font-medium">{fmt(r.count)}</dd>
                </div>
              ))}
              {workflows.compensations.map((c) => (
                <div key={`c-${c.outcome}`} className="contents">
                  <dt className="text-charcoal-500">Compensations · {c.outcome}</dt>
                  <dd className="tabular-nums font-medium">{fmt(c.count)}</dd>
                </div>
              ))}
            </dl>
          )}
        </Card>
      </div>
      <Card id="mon-integrations" title="Integration failures">
        {summary.integrations.step_failures.length === 0 ? (
          <EmptyState title="No failed integration steps recorded" />
        ) : (
          <ul className="divide-y divide-charcoal-100" aria-label="Integration failures">
            {summary.integrations.step_failures.map((f) => (
              <li key={`${f.integration}-${f.action}-${f.error_code}`} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <span className="text-charcoal-800">{f.integration}.{f.action} <span className="font-mono text-xs text-charcoal-500">{f.error_code}</span></span>
                <span className="tabular-nums font-medium text-rose-700">{fmt(f.count)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

// ---------- Logs ----------

const LEVELS = ["", "INFO", "WARNING", "ERROR"] as const;
const SEVERITY_TONE = { DEBUG: "neutral", INFO: "info", WARNING: "warning", ERROR: "danger", CRITICAL: "danger" } as const;

function Logs() {
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("");
  const load = useCallback(() => getMonitoringLogs(level || undefined), [level]);
  const { data, error, loading, updated, run } = useLoader<MonitoringLogs>(load);
  useEffect(() => void run(), [run]);
  const levelId = useId();
  return (
    <div className="space-y-4">
      <Toolbar loading={loading} updated={updated} onRefresh={() => void run()}>
        <label htmlFor={levelId} className="text-sm font-medium text-charcoal-800">Minimum level</label>
        <select id={levelId} value={level} onChange={(e) => setLevel(e.target.value as (typeof LEVELS)[number])}
          className="rounded-lg border border-charcoal-300 bg-white px-2 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/30">
          {LEVELS.map((l) => <option key={l} value={l}>{l || "All"}</option>)}
        </select>
        {data && isHttp(data.loki_explore_url) && <ExternalLink href={data.loki_explore_url}>Search all logs in Grafana (Loki)</ExternalLink>}
      </Toolbar>
      {error && <Unavailable what="Logs" error={error} />}
      {data && (
        <Card id="mon-logs" title="Recent log records" subtitle={`${data.scope}. Secrets, tokens and document content are never logged.`}>
          {data.records.length === 0 ? (
            <EmptyState title="No log records yet" />
          ) : (
            <ol className="divide-y divide-charcoal-100" aria-label="Log records">
              {data.records.map((r, i) => (
                <li key={`${r.timestamp}-${i}`} className="grid gap-1 py-2 text-sm md:grid-cols-[6.5rem_6rem_1fr]">
                  <span className="font-mono text-xs text-charcoal-500">{time(r.timestamp)}</span>
                  <span><Badge tone={SEVERITY_TONE[r.severity] ?? "neutral"} label={r.severity} /></span>
                  <span className="min-w-0">
                    <span className="text-charcoal-900">{r.message}</span>
                    <span className="mt-0.5 block truncate font-mono text-[11px] text-charcoal-500">
                      {[r.event, r.integration && `${r.integration}.${r.action ?? ""}`, r.error_code, r.duration_ms !== undefined && `${r.duration_ms} ms`,
                        r.workflow_id && `workflow ${r.workflow_id}`, r.request_id && `request ${r.request_id}`]
                        .filter(Boolean).join(" · ")}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Card>
      )}
    </div>
  );
}

// ---------- Workflow traces ----------

function Traces() {
  const { data, error, loading, updated, run } = useLoader<MonitoringTraces>(getMonitoringTraces);
  useEffect(() => void run(), [run]);
  return (
    <div className="space-y-4">
      <Toolbar loading={loading} updated={updated} onRefresh={() => void run()}>
        {data && isHttp(data.langfuse_url) && <ExternalLink href={data.langfuse_url}>Open Langfuse</ExternalLink>}
      </Toolbar>
      {error && <Unavailable what="Workflow traces" error={error} />}
      {data && !data.tracing_enabled && (
        <Alert tone="info" title="Distributed tracing is off">
          Runs are listed from this API process, but no trace is exported. Set OTEL_ENABLED=true and start the monitoring
          stack to open each run in Grafana (Tempo).{data.langfuse_enabled ? " Langfuse records the LLM side: search for the workflow id as session." : ""}
        </Alert>
      )}
      {data && (
        <Card id="mon-traces" title="Recent planning and execution runs" subtitle={data.scope}>
          {data.runs.length === 0 ? (
            <EmptyState title="No workflow runs yet">Plan or execute a workflow and it appears here.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[40rem] text-left text-sm">
                <thead className="border-b border-charcoal-200 text-xs uppercase tracking-wide text-charcoal-500">
                  <tr>
                    <th scope="col" className="py-2 pr-3">Run</th><th scope="col" className="py-2 pr-3">Status</th>
                    <th scope="col" className="py-2 pr-3">Duration</th><th scope="col" className="py-2 pr-3">Started</th>
                    <th scope="col" className="py-2">Trace</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-charcoal-100">
                  {data.runs.map((r, i) => (
                    <tr key={`${r.workflow_id}-${r.kind}-${i}`}>
                      <td className="py-2 pr-3">
                        <p className="font-medium capitalize text-charcoal-900">{r.kind}{r.mode ? ` · ${r.mode}` : ""}{r.target ? ` · ${r.target}` : ""}</p>
                        <p className="font-mono text-[11px] text-charcoal-500">{r.workflow_id ?? "—"}</p>
                        {r.fallback_used && <Badge tone="warning" label={`Fallback to ${r.provider ?? "next provider"}`} />}
                      </td>
                      <td className="py-2 pr-3 text-charcoal-800">{r.status ?? "—"}{r.error_code ? <span className="block font-mono text-[11px] text-rose-700">{r.error_code}</span> : null}</td>
                      <td className="py-2 pr-3 tabular-nums">{fmt(r.duration_ms, " ms")}</td>
                      <td className="py-2 pr-3">{time(r.started_at)}</td>
                      <td className="py-2">
                        {isHttp(r.trace_url) ? <ExternalLink href={r.trace_url}>View trace</ExternalLink>
                          : <span className="text-xs text-charcoal-500">{r.trace_id ? `trace ${r.trace_id.slice(0, 12)}…` : "Not exported"}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

// ---------- AI monitoring ----------

function AiMonitoring({ summary }: { summary: MonitoringSummary }) {
  const { providers, fallbacks } = summary.ai;
  return (
    <div className="space-y-6">
      <Card id="mon-ai" title="AI planner calls" subtitle={`${summary.scope}. Token counts are shown only when the provider reports them.`}
        actions={isHttp(summary.links.langfuse) ? <ExternalLink href={summary.links.langfuse}>Open Langfuse</ExternalLink> : undefined}>
        {providers.length === 0 ? (
          <EmptyState title="No AI planner calls recorded yet">Generate a plan or a document with an AI planner to see provider metrics.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-left text-sm">
              <thead className="border-b border-charcoal-200 text-xs uppercase tracking-wide text-charcoal-500">
                <tr>
                  <th scope="col" className="py-2 pr-3">Provider · model</th><th scope="col" className="py-2 pr-3">Requests</th>
                  <th scope="col" className="py-2 pr-3">Errors</th><th scope="col" className="py-2 pr-3">Avg latency</th>
                  <th scope="col" className="py-2">Tokens (in / out / total)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-charcoal-100">
                {providers.map((p) => (
                  <tr key={`${p.provider}-${p.model}`}>
                    <td className="py-2 pr-3"><span className="font-medium capitalize">{p.provider}</span> <span className="font-mono text-xs text-charcoal-500">{p.model}</span></td>
                    <td className="py-2 pr-3 tabular-nums">{fmt(p.requests)}</td>
                    <td className={`py-2 pr-3 tabular-nums ${p.errors ? "text-rose-700" : ""}`}>{fmt(p.errors)}{p.requests ? ` (${pct(p.errors / p.requests)})` : ""}</td>
                    <td className="py-2 pr-3 tabular-nums">{fmt(p.avg_latency_ms, " ms")}</td>
                    <td className="py-2 tabular-nums">
                      {Object.keys(p.tokens).length === 0 ? <span className="text-xs text-charcoal-500">Not reported</span>
                        : `${fmt(p.tokens.input)} / ${fmt(p.tokens.output)} / ${fmt(p.tokens.total)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Card id="mon-fallbacks" title="Planner fallbacks" subtitle="Groq → Gemini switches happen only on rate limits, timeouts or provider failures.">
        {fallbacks.length === 0 ? (
          <EmptyState title="No fallbacks recorded" />
        ) : (
          <ul className="divide-y divide-charcoal-100" aria-label="Planner fallbacks">
            {fallbacks.map((f) => (
              <li key={`${f.from_provider}-${f.to_provider}-${f.reason}`} className="flex items-center justify-between gap-2 py-2 text-sm">
                <span><span className="capitalize">{f.from_provider}</span> → <span className="capitalize">{f.to_provider}</span>{" "}
                  <span className="font-mono text-xs text-charcoal-500">{f.reason}</span></span>
                <span className="tabular-nums font-medium">{fmt(f.count)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

// ---------- page ----------

export function MonitoringPage() {
  const [tab, setTab] = useState<TabId>("overview");
  const summary = useLoader<MonitoringSummary>(getMonitoringSummary);
  const { run } = summary;
  useEffect(() => void run(), [run]);
  const base = useId();
  const tabRefs = useRef<Record<TabId, HTMLButtonElement | null>>({ overview: null, logs: null, traces: null, ai: null });

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const next = e.key === "ArrowRight" ? TABS[(i + 1) % TABS.length] : e.key === "ArrowLeft" ? TABS[(i - 1 + TABS.length) % TABS.length]
      : e.key === "Home" ? TABS[0] : e.key === "End" ? TABS[TABS.length - 1] : null;
    if (next) {
      e.preventDefault();
      setTab(next.id);
      tabRefs.current[next.id]?.focus();
    }
  };

  const s = SECTION_BY_ID.monitoring;
  return (
    <div className="space-y-6">
      <PageHeader icon={s.icon} title={s.label} description={s.description} />
      <div role="tablist" aria-label="Monitoring views" onKeyDown={onKey} className="flex gap-1 overflow-x-auto border-b border-charcoal-200">
        {TABS.map((t) => (
          <button key={t.id} ref={(el) => { tabRefs.current[t.id] = el; }} type="button" role="tab" id={`${base}-${t.id}-tab`}
            aria-selected={tab === t.id} aria-controls={`${base}-${t.id}-panel`} tabIndex={tab === t.id ? 0 : -1}
            onClick={() => setTab(t.id)}
            className={`-mb-px whitespace-nowrap border-b-2 px-4 py-2 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 ${
              tab === t.id ? "border-brand-600 text-brand-700" : "border-transparent text-charcoal-600 hover:border-charcoal-300 hover:text-charcoal-900"}`}>
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`${base}-${tab}-panel`} aria-labelledby={`${base}-${tab}-tab`} tabIndex={0} className="focus-visible:outline-none">
        {(tab === "overview" || tab === "ai") && (
          <div className="space-y-4">
            <Toolbar loading={summary.loading} updated={summary.updated} onRefresh={() => void run()}>
              {summary.data && <span className="text-xs text-charcoal-500">Counters {summary.data.scope} · up {Math.round(summary.data.uptime_seconds / 60)} min</span>}
            </Toolbar>
            {summary.error && <Unavailable what="Monitoring data" error={summary.error} />}
            {summary.data && (tab === "overview" ? <Overview summary={summary.data} /> : <AiMonitoring summary={summary.data} />)}
          </div>
        )}
        {tab === "logs" && <Logs />}
        {tab === "traces" && <Traces />}
      </div>
    </div>
  );
}
