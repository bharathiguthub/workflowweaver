import { MODE_LABELS, type DriveConnectivity, type DriveSandboxStatus, type ExecutionMode } from "../api/types";
import { Badge, Spinner } from "./StatusBadge";

interface Props {
  mode: ExecutionMode;
  status: DriveSandboxStatus | null;
  /** Title of the AI project plan currently under review, if any: its report is uploaded. */
  projectTitle: string | null;
  connectivity: DriveConnectivity | null;
  connectivityError: string | null;
  checking: boolean;
  planning: boolean;
  /** Why the test cannot be planned in this mode (null: it can). */
  blockedReason: string | null;
  onCheck: () => void;
  onPlan: () => void;
}

/** Phase 6.1: run folder + verified report upload + verified read-back in the Google Drive sandbox. */
export function GoogleDriveSandboxTest({
  mode, status, projectTitle, connectivity, connectivityError, checking, planning, blockedReason, onCheck, onPlan,
}: Props) {
  const alias = status?.folder_alias ?? null;
  return (
    <section aria-labelledby="drive-sandbox-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="drive-sandbox-heading" className="text-base font-semibold text-slate-900">
          Google Drive Sandbox Test
        </h2>
        {status && status.transport === "rest" && (
          <span className="flex flex-wrap gap-2">
            <Badge tone="info" label="Scope: drive.file only" />
            <Badge tone={status.authorized ? "success" : "warning"} label={status.authorized ? "OAuth authorized" : "OAuth not authorized"} />
          </span>
        )}
      </div>
      <p className="mt-1 text-sm text-slate-600">
        Three steps, Google Drive only: create a run folder
        {mode === "mock" ? " in the in-memory mock" : alias ? <> inside the sandbox folder <span className="font-mono">{alias}</span></> : " inside the sandbox folder"},
        upload {projectTitle ? <>the report for <strong>{projectTitle}</strong></> : "a synthetic project report"} into it and verify the
        upload, then read the report back and verify its content. The server builds this plan itself, without an AI planner,
        so it works even when Groq and Gemini are unavailable; nothing runs until you approve it. A plan can never delete
        anything: only a failed run's own files are moved to trash.
      </p>
      {(mode === "real" || connectivity || checking) && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <button
            type="button"
            onClick={onCheck}
            disabled={checking || !status?.real_ready}
            className="rounded-lg border border-slate-300 px-3 py-1.5 font-medium text-slate-800 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
          >
            Check Drive connectivity (read-only)
          </button>
          {checking && <Spinner label="Checking…" />}
          {!checking && connectivity && (
            <Badge
              tone={connectivity.status === "PASS" ? "success" : "danger"}
              label={connectivity.status === "PASS" ? "Drive sandbox reachable" : connectivity.status === "FAIL" ? "Drive unavailable" : "Not available"}
            />
          )}
        </div>
      )}
      {connectivityError && <p className="mt-1 text-xs text-rose-700">{connectivityError}</p>}
      {connectivity && connectivity.checks.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs" aria-label="Drive connectivity checks">
          {connectivity.checks.map((c) => (
            <li key={c.name} className={c.status === "PASS" ? "text-emerald-700" : "text-rose-700"}>
              {c.status}: {c.name}
              {c.detail ? ` (${c.detail})` : ""}
            </li>
          ))}
        </ul>
      )}
      {blockedReason && <p className="mt-2 text-sm font-medium text-amber-800">Unavailable: {blockedReason}</p>}
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={onPlan}
          disabled={planning || Boolean(blockedReason)}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-60"
        >
          Generate Drive Sandbox Plan ({MODE_LABELS[mode]})
        </button>
        {planning && <Spinner label="Planning…" />}
      </div>
    </section>
  );
}

/** Why the Drive sandbox test cannot be planned in ``mode`` (null: it can). */
export function driveBlockedReason(
  mode: ExecutionMode,
  status: DriveSandboxStatus | null | undefined,
  connectivity: DriveConnectivity | null,
): string | null {
  if (mode === "mock") return null;
  if (!status) return "Google Drive status is unavailable.";
  if (mode === "dry_run") return status.dry_run_ready ? null : status.issues[0] ?? "The Drive sandbox is not configured.";
  if (!status.real_ready) return status.issues[0] ?? "The Drive sandbox is not configured for real mode.";
  if (connectivity?.status !== "PASS") return "Run the read-only Drive connectivity check first.";
  return null;
}
