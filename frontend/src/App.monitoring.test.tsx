import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { MonitoringLogs, MonitoringSummary, MonitoringTraces } from "./api/types";
import { DEFAULT_ROUTES, installFetch, type MockRoute } from "./test/doubles";
import { navLink, renderApp } from "./test/renderApp";

// Synthetic monitoring responses; fetch is a fake. The page must show exactly these values.
const SUMMARY_URL = "/api/v1/monitoring/summary";
const LOGS_URL = /\/api\/v1\/monitoring\/logs/;
const TRACES_URL = "/api/v1/monitoring/traces";
const WF = "0f0e0000-0000-4000-9000-00000000a0a0";
const TRACE = "4bf92f3577b34da6a3ce929d0e0e4736";

function summary(overrides: Partial<MonitoringSummary> = {}): MonitoringSummary {
  return {
    scope: "since this API process started",
    generated_at: "2026-11-10T10:00:00+00:00",
    process_started_at: "2026-11-10T09:00:00+00:00",
    uptime_seconds: 3600,
    config: { service_name: "workflowweaver-api", environment: "development", log_format: "json", log_level: "INFO",
      metrics_enabled: true, metrics_protected: false, tracing_enabled: true, langfuse_enabled: true },
    links: {
      grafana: "http://127.0.0.1:3000", langfuse: "https://cloud.langfuse.com",
      dashboards: [
        { uid: "ww-api", title: "API performance", url: "http://127.0.0.1:3000/d/ww-api" },
        { uid: "ww-workflows", title: "Workflow execution", url: "http://127.0.0.1:3000/d/ww-workflows" },
        { uid: "ww-integrations", title: "Integration health", url: "http://127.0.0.1:3000/d/ww-integrations" },
      ],
    },
    services: [
      { name: "Prometheus", configured: true, status: "up", http_status: 200, latency_ms: 4.2 },
      { name: "Grafana", configured: true, status: "up", http_status: 200, latency_ms: 6.1 },
      { name: "Loki", configured: true, status: "down", error: "ConnectError" },
      { name: "Tempo", configured: false, status: "not_configured" },
    ],
    api: { requests: 1234, errors_4xx: 12, errors_5xx: 3, error_rate_5xx: 0.0024, avg_latency_ms: 41.5, in_progress: 1,
      routes: [{ route: "/api/v1/workflows/plan", requests: 40, errors: 2, avg_ms: 812.3 }] },
    workflows: {
      plans: [{ target: "google_drive", status: "planned", count: 7 }],
      executions_started: 9, executions_in_progress: 0,
      executions_finished: [{ status: "completed", count: 6 }, { status: "rolled_back", count: 2 }],
      rejections: [{ reason: "APPROVAL_REQUIRED", count: 1 }],
      step_retries: 4, compensations: [{ outcome: "compensated", count: 2 }],
    },
    integrations: { step_failures: [{ integration: "teams", action: "send_message", error_code: "TEAMS_WEBHOOK_REJECTED", count: 2 }],
      steps: [], calls: [] },
    ai: {
      providers: [{ provider: "groq", model: "openai/gpt-oss-120b", requests: 20, errors: 3, avg_latency_ms: 1834.2,
        tokens: { input: 18000, output: 6400, total: 24400 } },
      { provider: "gemini", model: "gemini-test-flash", requests: 3, errors: 0, avg_latency_ms: 2210, tokens: {} }],
      fallbacks: [{ from_provider: "groq", to_provider: "gemini", reason: "PLANNER_RATE_LIMITED", count: 3 }],
    },
    ...overrides,
  };
}

const EMPTY: MonitoringSummary = summary({
  config: { ...summary().config, tracing_enabled: false, langfuse_enabled: false },
  links: { grafana: null, langfuse: null, dashboards: [] },
  services: ["Prometheus", "Grafana", "Loki", "Tempo"].map((name) => ({ name, configured: false, status: "not_configured" as const })),
  api: { requests: 0, errors_4xx: 0, errors_5xx: 0, error_rate_5xx: null, avg_latency_ms: null, in_progress: 0, routes: [] },
  workflows: { plans: [], executions_started: 0, executions_in_progress: 0, executions_finished: [], rejections: [], step_retries: 0, compensations: [] },
  integrations: { step_failures: [], steps: [], calls: [] },
  ai: { providers: [], fallbacks: [] },
});

const LOGS: MonitoringLogs = {
  scope: "latest records from this API process (sanitised)",
  loki_explore_url: "http://127.0.0.1:3000/explore?panes=x",
  records: [
    { timestamp: "2026-11-10T10:00:01+00:00", severity: "WARNING", message: "step failed", event: "step.failed",
      integration: "teams", action: "send_message", error_code: "TEAMS_WEBHOOK_REJECTED", workflow_id: WF, request_id: "req-1", duration_ms: 120.5 },
    { timestamp: "2026-11-10T10:00:00+00:00", severity: "INFO", message: "GET /health 200", event: "http.request" },
  ],
};

const TRACES: MonitoringTraces = {
  scope: "latest planning and execution runs in this API process",
  tracing_enabled: true, langfuse_enabled: true, langfuse_url: "https://cloud.langfuse.com",
  runs: [
    { kind: "execution", workflow_id: WF, trace_id: TRACE, trace_url: `http://127.0.0.1:3000/explore?trace=${TRACE}`,
      started_at: "2026-11-10T10:00:00+00:00", duration_ms: 2510.4, status: "COMPLETED", mode: "mock", target: "notion" },
    { kind: "planning", workflow_id: WF, started_at: "2026-11-10T09:59:58+00:00", duration_ms: 1900, status: "PLANNED",
      target: "notion", provider: "gemini", fallback_used: true },
  ],
};

function setup(routes: MockRoute[]) {
  const state = installFetch([...DEFAULT_ROUTES, ...routes]);
  const user = userEvent.setup();
  renderApp("monitoring");
  return { user, ...state };
}

const tab = (name: string) => screen.getByRole("tab", { name });

describe("Monitoring & Observability", () => {
  it("is in the sidebar and offers the four tabs", async () => {
    setup([{ path: SUMMARY_URL, body: summary() }]);
    expect(navLink("Monitoring & Observability")).toHaveAttribute("aria-current", "page");
    const tabs = within(screen.getByRole("tablist", { name: "Monitoring views" })).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Overview", "Logs", "Workflow Traces", "AI Monitoring"]);
    expect(tab("Overview")).toHaveAttribute("aria-selected", "true");
  });

  it("overview shows the backend's real values, service reachability and dashboard links", async () => {
    setup([{ path: SUMMARY_URL, body: summary() }]);
    expect(await screen.findByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("0.2%")).toBeInTheDocument();
    expect(screen.getByText("41.5 ms")).toBeInTheDocument();
    expect(screen.getByText("6 completed of 8 finished · 4 retries")).toBeInTheDocument();
    const sources = screen.getByRole("list", { name: "Monitoring sources" });
    expect(within(sources).getByText("Reachable · 4.2 ms")).toBeInTheDocument();
    expect(within(sources).getByText("Unreachable")).toBeInTheDocument();
    expect(within(sources).getByText("Not configured")).toBeInTheDocument();
    const links = within(screen.getByRole("list", { name: "Monitoring links" })).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "http://127.0.0.1:3000/d/ww-api", "http://127.0.0.1:3000/d/ww-workflows", "http://127.0.0.1:3000/d/ww-integrations",
      "https://cloud.langfuse.com"]);
    expect(links.every((a) => a.getAttribute("target") === "_blank" && a.getAttribute("rel") === "noopener noreferrer")).toBe(true);
    expect(screen.getByText("TEAMS_WEBHOOK_REJECTED")).toBeInTheDocument();
  });

  it("never fabricates data: an idle backend shows empty states and dashes", async () => {
    setup([{ path: SUMMARY_URL, body: EMPTY }]);
    expect(await screen.findByText("No monitoring links configured")).toBeInTheDocument();
    expect(screen.getByText("No requests recorded yet")).toBeInTheDocument();
    expect(screen.getByText("No planning or execution recorded yet")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2); // error rate and latency: no data, not 0
    expect(within(screen.getByRole("list", { name: "Monitoring sources" })).getAllByText("Not configured")).toHaveLength(4);
  });

  it("handles an unavailable monitoring API gracefully", async () => {
    setup([{ path: SUMMARY_URL, status: 503, body: { detail: "Monitoring is starting." } }]);
    expect(await screen.findByText("Monitoring data unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Monitoring is starting\. The rest of WorkflowWeaver keeps working/)).toBeInTheDocument();
  });

  it("logs tab lists sanitised records, filters by level and links to Loki", async () => {
    const { user, calls } = setup([{ path: SUMMARY_URL, body: summary() }, { path: LOGS_URL, body: LOGS }]);
    await user.click(tab("Logs"));
    const records = await screen.findByRole("list", { name: "Log records" });
    expect(within(records).getAllByRole("listitem")).toHaveLength(2);
    expect(within(records).getByText(/step\.failed · teams\.send_message · TEAMS_WEBHOOK_REJECTED · 120\.5 ms/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Search all logs in Grafana \(Loki\)/ })).toHaveAttribute("href", LOGS.loki_explore_url);
    await user.selectOptions(screen.getByLabelText("Minimum level"), "WARNING");
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/monitoring/logs?level=WARNING")).toBe(true));
  });

  it("workflow traces tab links runs to their traces and marks fallbacks", async () => {
    const { user } = setup([{ path: SUMMARY_URL, body: summary() }, { path: TRACES_URL, body: TRACES }]);
    await user.click(tab("Workflow Traces"));
    const link = await screen.findByRole("link", { name: /View trace/ });
    expect(link).toHaveAttribute("href", TRACES.runs[0]!.trace_url);
    expect(screen.getByText("Fallback to gemini")).toBeInTheDocument();
    expect(screen.getByText("Not exported")).toBeInTheDocument(); // the planning run has no trace id
    expect(screen.queryByText("Distributed tracing is off")).not.toBeInTheDocument();
  });

  it("AI monitoring shows provider-reported numbers only", async () => {
    const { user } = setup([{ path: SUMMARY_URL, body: summary() }]);
    await user.click(tab("AI Monitoring"));
    expect(await screen.findByText("18,000 / 6,400 / 24,400")).toBeInTheDocument();
    expect(screen.getByText("Not reported")).toBeInTheDocument(); // Gemini returned no token usage
    expect(screen.getByText("3 (15.0%)")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Planner fallbacks" })).getByText("PLANNER_RATE_LIMITED")).toBeInTheDocument();
  });

  it("supports keyboard navigation between tabs", async () => {
    const { user } = setup([{ path: SUMMARY_URL, body: summary() }, { path: LOGS_URL, body: LOGS }]);
    tab("Overview").focus();
    await user.keyboard("{ArrowRight}");
    expect(tab("Logs")).toHaveAttribute("aria-selected", "true");
    expect(tab("Logs")).toHaveFocus();
    await user.keyboard("{End}");
    expect(tab("AI Monitoring")).toHaveAttribute("aria-selected", "true");
  });
});
