import { aiPlannerLabel, type PlanResponse, type ProviderAttempt } from "../api/types";
import { Badge } from "./StatusBadge";

const providerName = (provider: string | null | undefined): string =>
  aiPlannerLabel(provider) ?? provider ?? "unknown";

function attemptLabel(a: ProviderAttempt): string {
  const who = `${providerName(a.provider)} attempt ${a.attempt}`;
  if (a.outcome === "draft") return `${who}: plan produced`;
  const status = a.http_status ? ` (HTTP ${a.http_status})` : "";
  return `${who}: ${a.error_code ?? "error"}${status}`;
}

/** Which provider and model actually produced (or failed to produce) the plan, with a
 * visible fallback indicator and the per-provider attempt log. */
export function PlannerProvenance({ plan }: { plan: PlanResponse }) {
  const attempts = plan.provider_attempts ?? [];
  const model = plan.planner_model ?? null;
  const fallback = plan.fallback_used === true;
  const primary = plan.primary_planner ?? null;
  return (
    <div aria-label="Planner provenance" className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-slate-800">{providerName(plan.planner)}</span>
        {model && <span className="break-all font-mono text-xs text-slate-600">{model}</span>}
        {fallback && (
          <Badge
            tone="warning"
            label={`Fallback: ${providerName(primary)} unavailable${plan.fallback_reason ? ` (${plan.fallback_reason})` : ""}`}
          />
        )}
      </div>
      {attempts.length > 1 && (
        <details className="text-xs text-slate-600">
          <summary className="cursor-pointer">Provider attempts ({attempts.length})</summary>
          <ol aria-label="Provider attempts" className="mt-1 list-decimal space-y-0.5 pl-5">
            {attempts.map((a, i) => (
              <li key={`${a.provider}-${a.attempt}-${i}`} className={a.outcome === "draft" ? "text-emerald-700" : ""}>
                {attemptLabel(a)}
                {a.model ? <span className="font-mono"> · {a.model}</span> : null}
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}
