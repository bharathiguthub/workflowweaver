import { aiPlannerLabel, type ExecutionMode, type IntegrationsStatus } from "../api/types";
import { orchestrationAvailability, teamsAvailability } from "../components/IntegrationTargetSelector";
import type { Readiness } from "../components/layout/Page";
import type { SectionId } from "../navigation";

export interface CapabilityReadiness {
  value: Readiness;
  detail: string;
}

const modeWord: Record<ExecutionMode, string> = { mock: "mock", dry_run: "dry run", real: "real" };

/** How ready each business capability is, from the backend's status (setting names only). */
export function capabilityReadiness(id: SectionId, status: IntegrationsStatus | null): CapabilityReadiness {
  if (!status) return { value: "limited", detail: "Status unavailable: mock mode only." };
  const best = (ready: Record<ExecutionMode, boolean>, issue: string | undefined): CapabilityReadiness => {
    const modes = (["real", "dry_run", "mock"] as ExecutionMode[]).filter((m) => ready[m] && status.available_modes.includes(m));
    if (modes[0] === "real") return { value: "ready", detail: "Ready for real runs after approval." };
    if (modes.length) return { value: "limited", detail: `Available in ${modes.map((m) => modeWord[m]).reverse().join(" and ")}.${issue ? ` ${issue}` : ""}` };
    return { value: "setup", detail: issue ?? "Not configured on the backend." };
  };
  switch (id) {
    case "documents": {
      const d = status.google_drive_sandbox;
      return best({ mock: true, dry_run: Boolean(d?.dry_run_ready), real: Boolean(d?.real_ready) && Boolean(status.planner?.ai_ready) },
        d?.issues[0]);
    }
    case "projects": {
      const n = status.notion_sandbox;
      return best({ mock: true, dry_run: n.dry_run_ready, real: n.real_ready }, n.issues[0]);
    }
    case "communication":
      return best({
        mock: teamsAvailability(status, "mock").available,
        dry_run: teamsAvailability(status, "dry_run").available,
        real: teamsAvailability(status, "real").available,
      }, teamsAvailability(status, "real").reason ?? undefined);
    case "business":
      return best({
        mock: orchestrationAvailability(status, "mock").available,
        dry_run: orchestrationAvailability(status, "dry_run").available,
        real: orchestrationAvailability(status, "real").available,
      }, orchestrationAvailability(status, "real").reason ?? undefined);
    case "builder": {
      const p = status.planner;
      if (!p || p.mode === "mock") return { value: "limited", detail: "PLANNER_MODE=mock: keyword planner, not AI." };
      return p.ai_ready
        ? { value: "ready", detail: `AI planner: ${plannerSummary(status)}.` }
        : { value: "setup", detail: p.issue ?? "The AI planner is not configured." };
    }
    default:
      return { value: "ready", detail: "" };
  }
}

/** "Groq (Gemini fallback)" in auto mode, else the single provider; "Mock (not AI)" for mock. */
export function plannerSummary(status: IntegrationsStatus | null): string {
  const p = status?.planner;
  if (!p || p.mode === "mock") return "Mock planner (not AI)";
  if (p.mode === "auto") {
    const primary = aiPlannerLabel(p.primary) ?? p.primary ?? "AI";
    const fallback = p.fallback ? (aiPlannerLabel(p.fallback) ?? p.fallback) : null;
    return fallback ? `${primary} (${fallback} fallback)` : primary;
  }
  return aiPlannerLabel(p.mode) ?? p.mode;
}
