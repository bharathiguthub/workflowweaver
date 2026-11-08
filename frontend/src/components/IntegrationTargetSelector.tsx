import { useId } from "react";

import { TARGET_LABELS, type ExecutionMode, type IntegrationTarget, type IntegrationsStatus } from "../api/types";

export interface TargetOption {
  target: IntegrationTarget;
  available: boolean;
  description: string;
  /** Why the target cannot be selected (null when available). */
  reason: string | null;
}

/** Teams needs real Workflows webhooks; the mock adapter alone does not count as configured. */
export function teamsConfigured(status: IntegrationsStatus | null): boolean {
  const teams = status?.integrations?.teams;
  return Boolean(teams && teams.transport === "webhook" && teams.configured);
}

/**
 * Whether the Teams Only target can be used in ``mode``, as reported by the backend (Phase 7.1):
 * mock = simulated delivery, dry_run = request preview with a configured sandbox alias, real =
 * every real-write gate configured. Older backends without ``teams_sandbox`` fall back to
 * "real webhooks configured".
 */
export function teamsAvailability(status: IntegrationsStatus | null, mode: ExecutionMode): { available: boolean; reason: string | null } {
  const teams = status?.teams_sandbox;
  if (!teams) {
    const ok = teamsConfigured(status);
    return { available: ok, reason: ok ? null : "Configure TEAMS_WEBHOOK_URLS on the backend first." };
  }
  const ready = mode === "mock" ? teams.mock_ready : mode === "dry_run" ? teams.dry_run_ready : teams.real_ready;
  if (ready) return { available: true, reason: null };
  const fallback =
    mode === "mock" ? "Execution is disabled on the backend." : "Configure TEAMS_WEBHOOK_URLS and SMOKE_TEST_TEAMS_CHANNEL on the backend first.";
  return { available: false, reason: teams.issues[0] ?? fallback };
}

/** Phase 7.2: the Google Drive + Notion + Teams workflow, as the backend reports it for ``mode``. */
export function orchestrationAvailability(status: IntegrationsStatus | null, mode: ExecutionMode): { available: boolean; reason: string | null } {
  const source = orchestrationInputAvailability(status, mode, "document");
  const instructions = orchestrationInputAvailability(status, mode, "instructions");
  if (source.available || instructions.available) return { available: true, reason: null };
  return { available: false, reason: source.reason ?? instructions.reason };
}

/** Whether plans with a Drive source document ("document") or from instructions alone
 * ("instructions", no Google Drive needed) can be made in ``mode``. */
export function orchestrationInputAvailability(
  status: IntegrationsStatus | null,
  mode: ExecutionMode,
  input: "document" | "instructions",
): { available: boolean; reason: string | null } {
  const o = status?.orchestration;
  if (!o) return { available: false, reason: "This backend does not offer the Google Drive + Notion + Teams workflow." };
  const r = input === "document" ? o : o.instructions_only;
  if (!r) return { available: false, reason: "This backend does not support instructions-only plans." };
  const ready = mode === "mock" ? r.mock_ready : mode === "dry_run" ? r.dry_run_ready : r.real_ready;
  if (ready) return { available: true, reason: null };
  return { available: false, reason: r.issues[0] ?? "Configure the Notion and Teams sandboxes on the backend first." };
}

export function targetOptions(status: IntegrationsStatus | null, mode: ExecutionMode = "mock"): TargetOption[] {
  const { available: teams, reason: teamsReason } = teamsAvailability(status, mode);
  const orchestration = orchestrationAvailability(status, mode);
  return [
    { target: "google_drive", available: true, reason: null,
      description: "Plans may use only Google Drive: folders, report uploads, listing and reads." },
    { target: "notion", available: true, reason: null,
      description: "Plans may use only Notion: the AI Project Plan and the Notion Sandbox Test." },
    { target: "teams", available: teams, reason: teamsReason,
      description: "Plans may use only Microsoft Teams messages, posted to the approved sandbox channel." },
    { target: "drive_notion_teams", available: orchestration.available, reason: orchestration.reason,
      description: "Read a plan from the Drive sandbox, create Notion pages, then notify the Teams channel (one approval)." },
    { target: "multi", available: true, reason: null,
      description: "Future option: plans may mix integrations (templates; mock and dry run)." },
  ];
}

interface Props {
  value: IntegrationTarget;
  options: TargetOption[];
  disabled: boolean;
  onChange: (target: IntegrationTarget) => void;
}

/** The integrations a plan may use. The backend enforces it on planning and execution. */
export function IntegrationTargetSelector({ value, options, disabled, onChange }: Props) {
  const id = useId();
  return (
    <fieldset className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6" disabled={disabled}>
      <legend className="px-1 text-base font-semibold text-slate-900">Integration Target</legend>
      <p className="text-sm text-slate-600">
        Plans that use any tool outside the selected integration are rejected by the server, for every planner (Gemini,
        Groq or mock), and again before execution.
      </p>
      <div className="mt-3 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Integration Target">
        {options.map((o) => (
          <label
            key={o.target}
            className={`flex cursor-pointer items-start gap-2 rounded-lg border p-3 text-sm ${
              value === o.target ? "border-indigo-500 bg-indigo-50" : "border-slate-200"
            } ${o.available ? "" : "cursor-not-allowed opacity-60"}`}
          >
            <input
              type="radio"
              name={`${id}-target`}
              value={o.target}
              checked={value === o.target}
              disabled={!o.available}
              onChange={() => onChange(o.target)}
              aria-label={TARGET_LABELS[o.target]}
              aria-describedby={`${id}-${o.target}`}
              className="mt-0.5"
            />
            <span>
              <span className="font-medium text-slate-900">
                {TARGET_LABELS[o.target]}
                {(o.target === "teams" || o.target === "drive_notion_teams") && !o.available ? " (not available)" : ""}
                {o.target === "multi" ? " (future)" : ""}
              </span>
              <span id={`${id}-${o.target}`} className="block text-xs text-slate-600">
                {o.reason ?? o.description}
              </span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
