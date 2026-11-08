import type { ExecutionMode, IntegrationTarget, IntegrationsStatus, NotionConnectivity } from "../api/types";

export interface ModeOption {
  mode: ExecutionMode;
  available: boolean;
  /** Why the mode cannot be selected (null when available). */
  reason: string | null;
}

/**
 * Which modes the UI offers. The backend is authoritative (it re-checks the ceiling, approval,
 * REAL_WRITES_ENABLED and the operator token on every request); this only avoids offering a
 * mode that would be refused.
 *
 * Real mode additionally requires the Notion sandbox to be ready (configuration) AND a passing
 * read-only connectivity check, because the UI's only real workflow is the Notion Sandbox Test.
 */
export function modeOptions(
  status: IntegrationsStatus | null,
  connectivity: NotionConnectivity | null,
  driveConnectivity: NotionConnectivity | null = null,
): Record<ExecutionMode, ModeOption> {
  const mock: ModeOption = { mode: "mock", available: true, reason: null };
  if (!status) {
    const reason = "Integration status is unavailable, so only mock mode is offered.";
    return { mock, dry_run: { mode: "dry_run", available: false, reason }, real: { mode: "real", available: false, reason } };
  }
  const ceiling = (mode: ExecutionMode): string | null =>
    status.available_modes.includes(mode)
      ? null
      : status.configuration_valid
        ? `The backend's INTEGRATION_MODE is '${status.mode}'; '${mode}' needs INTEGRATION_MODE=${mode} or higher.`
        : `Integration configuration is invalid: ${status.configuration_error ?? "see the backend log"}.`;

  const dryReason = ceiling("dry_run");
  let realReason = ceiling("real");
  if (!realReason && !status.notion_sandbox.real_ready) {
    realReason = status.notion_sandbox.issues[0] ?? "The Notion sandbox is not configured for real mode.";
  }
  if (!realReason) {
    if (!connectivity) realReason = "Run the Notion connectivity check first.";
    else if (connectivity.status !== "PASS") realReason = "Notion is unavailable: the connectivity check did not pass.";
  }
  // Phase 6.1: the Google Drive sandbox (ready + passing read-only check) also enables real mode.
  const driveReady = !ceiling("real") && status.google_drive_sandbox?.real_ready === true && driveConnectivity?.status === "PASS";
  if (driveReady) realReason = null;
  // Phase 7.1: a fully configured Teams sandbox also enables real mode. A Workflows webhook has no
  // read-only probe (any POST triggers the flow), so configuration readiness is the only check.
  if (!ceiling("real") && status.teams_sandbox?.real_ready === true) realReason = null;
  // Phase 7.2: the Drive + Notion + Teams workflow needs both read-only checks (Notion, Drive) to pass.
  if (!ceiling("real") && status.orchestration?.real_ready === true && connectivity?.status === "PASS" && driveConnectivity?.status === "PASS") {
    realReason = null;
  }
  // Instructions-only plans (no Google Drive source) need only the Notion check.
  if (!ceiling("real") && status.orchestration?.instructions_only?.real_ready === true && connectivity?.status === "PASS") {
    realReason = null;
  }
  return {
    mock: { ...mock, available: status.available_modes.includes("mock"), reason: status.available_modes.includes("mock") ? null : "Execution is disabled on the backend." },
    dry_run: { mode: "dry_run", available: !dryReason, reason: dryReason },
    real: { mode: "real", available: !realReason, reason: realReason },
  };
}

/** Real mode runs the Notion workflows and Google Drive Only / Teams Only custom plans; the rest stays mock/dry-run. */
export const REAL_MODE_SANDBOX_ONLY =
  "Real mode runs only the Notion workflows (AI Project Plan and Sandbox Test) and custom instructions with the Google Drive Only or Microsoft Teams Only target. Templates and other custom instructions are available in Mock and Dry Run.";

/**
 * Why the custom-instruction form cannot plan in ``mode`` for ``target`` (null: it can). In real
 * mode only Google Drive Only (ready Drive sandbox, passing read-only Drive check, AI planner) and
 * Microsoft Teams Only (ready Teams sandbox, AI planner) are offered. The backend enforces all of
 * this again.
 */
export function customPlanBlockedReason(
  mode: ExecutionMode,
  target: IntegrationTarget,
  status: IntegrationsStatus | null,
  driveConnectivity: NotionConnectivity | null,
): string | null {
  if (mode !== "real") return null;
  if (target === "teams") {
    // Phase 7.1: real Teams Only plans need every real-write gate configured and an AI planner.
    const teams = status?.teams_sandbox;
    if (!teams?.real_ready) return teams?.issues[0] ?? "The Microsoft Teams sandbox channel is not configured for real mode.";
    if (!(status?.planner?.ai_ready ?? false)) {
      return `Real Teams plans must come from an AI planner (Groq with Gemini fallback): ${status?.planner?.issue ?? "no AI planner is configured."}`;
    }
    return null;
  }
  if (target !== "google_drive") return REAL_MODE_SANDBOX_ONLY;
  const drive = status?.google_drive_sandbox;
  if (!drive?.real_ready) return drive?.issues[0] ?? "The Google Drive sandbox is not configured for real mode.";
  if (!(status?.planner?.ai_ready ?? false)) {
    return `Real Google Drive plans must come from an AI planner (Groq with Gemini fallback): ${status?.planner?.issue ?? "no AI planner is configured."}`;
  }
  if (driveConnectivity?.status !== "PASS") return "Run the read-only Drive connectivity check first.";
  return null;
}

/** Why the AI project workflow cannot be planned in ``mode`` (null: it can). */
export function projectBlockedReason(mode: ExecutionMode, status: IntegrationsStatus | null): string | null {
  if (mode === "mock") return null;
  if (!status) return "Integration status is unavailable.";
  if (!status.notion_sandbox.dry_run_ready) {
    return status.notion_sandbox.issues.find((i) => i.includes("SMOKE_TEST_NOTION_PARENT")) ??
      "Configure SMOKE_TEST_NOTION_PARENT as an approved Notion destination alias.";
  }
  const ready = status.planner?.ai_ready ?? status.planner?.claude_ready ?? false;
  if (mode === "real" && !ready) {
    return `Real project plans must come from an AI planner (Claude or Gemini): ${status.planner?.issue ?? "no AI planner is configured."}`;
  }
  return null;
}
