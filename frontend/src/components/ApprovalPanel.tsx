import { useId, useState, type FormEvent } from "react";

import { TARGET_LABELS, actionLabel, integrationLabel, type ExecutionMode, type IntegrationTarget, type WorkflowStep } from "../api/types";
import {
  DRIVE_CONFIRMATION_PHRASE,
  REAL_CONFIRMATION_PHRASE,
  TEAMS_CONFIRMATION_PHRASE,
  validateApprover,
  validateRealConfirmation,
} from "../state/forms";
import { Alert, Spinner } from "./StatusBadge";

interface Props {
  mode?: ExecutionMode;
  stepCount: number;
  /** Sandbox parent alias the real page is created under (real mode). */
  destinationAlias?: string | null;
  /** Real Notion pages the plan creates (1 for the sandbox test). */
  pageCount?: number;
  /** Which service a real run writes to: Notion (default), the Google Drive sandbox (Phase 6.1) or the Teams sandbox channel (Phase 7.1). */
  target?: "notion" | "google_drive" | "teams" | "drive_notion_teams";
  /** Phase 7.2 (drive_notion_teams): the approved Teams sandbox channel alias. */
  teamsChannel?: string | null;
  /** The plan's integration target; the approval is sent for exactly this target. */
  planTarget?: IntegrationTarget | null;
  /** The steps being approved, listed so the reviewer sees exactly which actions run. */
  steps?: WorkflowStep[];
  /** A custom (AI-generated) Google Drive plan rather than the predefined Drive Sandbox Test. */
  driveCustom?: boolean;
  busy: boolean;
  starting: boolean;
  error: string | null;
  onApprove: (approvedBy: string, operatorToken?: string) => void;
}

const COPY: Record<ExecutionMode, { heading: string; confirm: string; button: string }> = {
  mock: {
    heading: "3. Approve mock execution",
    confirm: "I reviewed this plan and approve running it in mock mode.",
    button: "Approve and Execute Mock Workflow",
  },
  dry_run: {
    heading: "3. Approve dry run",
    confirm: "I reviewed this plan and approve a dry run (requests are built, nothing is sent).",
    button: "Approve and Run Dry Run",
  },
  real: {
    heading: "3. Approve REAL Notion write",
    confirm: "I reviewed this exact plan and approve creating one real Notion page",
    button: "Create Real Notion Page",
  },
};

const inputClass =
  "mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 sm:max-w-xs";

const DRIVE_REAL = {
  heading: "3. Approve REAL Google Drive write",
  confirm: "I reviewed this exact plan and approve creating one run folder and uploading the report",
  button: "Upload Report to Google Drive",
};

/** What a simulated run does NOT create, worded for the plan's target (never mentions Notion for Drive-only). */
const SIMULATED: Record<IntegrationTarget, string> = {
  google_drive: "No real Google Drive folder or file is created.",
  notion: "No real Notion page is created.",
  teams: "No real Teams message is sent.",
  drive_notion_teams: "No real Google Drive read, Notion page or Teams message happens.",
  multi: "No real Notion page, Drive document or Teams message is created.",
};

export function ApprovalPanel({ mode = "mock", stepCount, destinationAlias = null, pageCount = 1, target = "notion", teamsChannel = null, planTarget = null, steps = [], driveCustom = false, busy, starting, error, onApprove }: Props) {
  const [approvedBy, setApprovedBy] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [teamsPhrase, setTeamsPhrase] = useState("");
  const teamsPhraseId = useId();
  const [operatorToken, setOperatorToken] = useState("");
  const approverId = useId();
  const confirmId = useId();
  const phraseId = useId();
  const tokenId = useId();
  const real = mode === "real";
  const pages = pageCount === 1 ? "one real Notion page" : `${pageCount} real Notion pages`;
  const drive = target === "google_drive";
  const teams = target === "teams";
  // Phase 7.2: one approval for every Notion write and the Teams notification (both phrases required).
  const orchestration = target === "drive_notion_teams";
  const notionPages = steps.filter((s) => s.integration === "notion" && s.action === "create_page").length;
  const readsSource = steps.some((s) => s.integration === "google_drive" && s.action === "read_document");
  const messages = steps.filter((s) => s.integration === "teams" && s.action === "send_message").length || stepCount;
  const messageWord = messages === 1 ? "message" : "messages";
  const writes = steps.filter((s) => s.action === "create_folder" || s.action === "create_document").length;
  const copy =
    real && orchestration
      ? {
          heading: "3. Approve REAL Notion pages and Teams notification",
          confirm: `I reviewed this exact plan and approve creating ${notionPages} real Notion pages under sandbox parent ${
            destinationAlias ?? "(unknown)"
          }, then posting 1 real Teams message to sandbox channel ${teamsChannel ?? "(unknown)"}.`,
          button: "Create Notion Pages and Notify Teams",
        }
      : real && teams
      ? {
          heading: "3. Approve REAL Microsoft Teams message",
          confirm: `I reviewed this exact plan and approve posting ${messages} real Teams ${messageWord}`,
          button: `Send ${messages} Teams ${messages === 1 ? "Message" : "Messages"}`,
        }
      : real && drive && driveCustom
      ? {
          heading: DRIVE_REAL.heading,
          confirm: `I reviewed this exact plan and approve its ${writes} Google Drive write action(s)`,
          button: "Run Plan on Google Drive",
        }
      : real && drive
      ? DRIVE_REAL
      : real && pageCount !== 1
        ? { ...COPY.real, confirm: `I reviewed this exact plan and approve creating ${pages}`, button: `Create ${pageCount} Real Notion Pages` }
        : COPY[mode];
  const confirmPhrase = teams ? TEAMS_CONFIRMATION_PHRASE : drive ? DRIVE_CONFIRMATION_PHRASE : REAL_CONFIRMATION_PHRASE;

  const approverError = validateApprover(approvedBy);
  const phraseError = real
    ? validateRealConfirmation(phrase, confirmPhrase) ??
      (orchestration ? validateRealConfirmation(teamsPhrase, TEAMS_CONFIRMATION_PHRASE) : null)
    : null;
  const tokenError = real && !operatorToken.trim() ? "Enter the operator token." : null;
  const canExecute = confirmed && !approverError && !phraseError && !tokenError && !busy;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canExecute) return;
    if (real) {
      const token = operatorToken.trim();
      setOperatorToken(""); // never kept after use
      setPhrase("");
      setTeamsPhrase("");
      setConfirmed(false);
      onApprove(approvedBy, token);
    } else {
      onApprove(approvedBy);
    }
  };

  return (
    <section
      aria-labelledby="approve-heading"
      className={`rounded-xl border p-4 shadow-sm sm:p-6 ${real ? "border-rose-300 bg-rose-50/50" : "border-amber-200 bg-amber-50/40"}`}
    >
      <h2 id="approve-heading" className="text-base font-semibold text-slate-900">
        {copy.heading}
      </h2>
      {mode === "mock" && (
        <p className="mt-1 text-sm text-slate-700">
          Execution is <strong>simulated</strong>: the {stepCount} step(s) above run against in-memory mock adapters.{" "}
          {SIMULATED[planTarget ?? "multi"]}
        </p>
      )}
      {mode === "dry_run" && (
        <p className="mt-1 text-sm text-slate-700">
          <strong>Dry run</strong>: the real adapters check destinations and build the exact request for each of the {stepCount}{" "}
          step(s), but <strong>nothing is sent</strong> and no connection is opened.
        </p>
      )}
      {mode === "dry_run" && teams && (
        <p className="mt-1 text-sm text-slate-700">
          The Teams Adaptive Card payload is built for channel <span className="font-mono">{destinationAlias ?? "(unknown)"}</span>;
          no webhook is called and no message is posted.
        </p>
      )}
      {mode === "dry_run" && orchestration && (
        <p className="mt-1 text-sm text-slate-700">
          {readsSource ? "The source is not read (dry run never contacts Google Drive); the" : "No Google Drive document is used; the"}{" "}
          {notionPages} Notion page request(s) and the Teams card
          are built for parent <span className="font-mono">{destinationAlias ?? "(unknown)"}</span> and channel{" "}
          <span className="font-mono">{teamsChannel ?? "(unknown)"}</span>, but nothing is sent.
        </p>
      )}
      {real && orchestration && (
        <div className="mt-2">
          <Alert title="This writes to Notion and posts to Microsoft Teams">
            In order:{" "}
            {readsSource
              ? "the source document is re-read from Google Drive (read-only; the run stops if it changed), then "
              : "(no Google Drive document is used; the plan was generated by AI from your instructions) "}
            <strong>{notionPages} Notion pages</strong> are created under the sandbox parent{" "}
            <span className="font-mono">{destinationAlias ?? "(unknown)"}</span> and each is read back, and only after every page
            succeeded is <strong>1 Teams message</strong> posted to the sandbox channel{" "}
            <span className="font-mono">{teamsChannel ?? "(unknown)"}</span>. If a Notion step fails, only the pages this run
            created are moved to trash and Teams is not notified. A posted Teams message cannot be unsent; if the notification
            fails, the verified Notion pages are kept and the run is reported as partially completed. Your approval is bound to
            this exact plan; the backend re-checks it, the sandboxes, REAL_WRITES_ENABLED and your operator token.
          </Alert>
        </div>
      )}
      {real && teams && (
        <div className="mt-2">
          <Alert title="This posts a real message to Microsoft Teams">
            Approving calls the Teams Workflows webhook of the sandbox channel{" "}
            <span className="font-mono">{destinationAlias ?? "(unknown)"}</span> and posts{" "}
            <strong>
              {messages} {messageWord}
            </strong>
            . A posted message <strong>cannot be unsent, edited or deleted</strong> by WorkflowWeaver. A request is retried
            only when the webhook did not receive it (connection failure or HTTP 429); if the outcome is unknown it is not
            retried, so check the channel. The backend re-checks the target, the channel, REAL_WRITES_ENABLED and your
            operator token.
          </Alert>
        </div>
      )}
      {real && drive && driveCustom && (
        <div className="mt-2">
          <Alert title="This writes to your real Google Drive">
            Approving calls the Google Drive API (scope <span className="font-mono">drive.file</span>: only files this app
            creates) and runs the {stepCount} action(s) listed below, AI-generated and reviewed by you, only inside the
            sandbox folder <span className="font-mono">{destinationAlias ?? "(unknown)"}</span> and folders this run
            creates there. Nothing can be deleted; if a step fails, everything this run created is moved to Drive trash.
            The backend re-checks the target, the destinations, REAL_WRITES_ENABLED and your operator token.
          </Alert>
        </div>
      )}
      {real && drive && !driveCustom && (
        <div className="mt-2">
          <Alert title="This writes to your real Google Drive">
            Approving calls the Google Drive API (scope <span className="font-mono">drive.file</span>: only files this app
            creates) and creates <strong>one run folder</strong> inside the sandbox folder{" "}
            <span className="font-mono">{destinationAlias ?? "(unknown)"}</span>, uploads the report into it and verifies the
            upload. If a step fails, everything this run created is moved to Drive trash. A successful run keeps the files;
            remove them with the cleanup script when done. The backend still requires REAL_WRITES_ENABLED and verifies your
            operator token.
          </Alert>
        </div>
      )}
      {real && !drive && !teams && !orchestration && (
        <div className="mt-2">
          <Alert title="This creates a real page in Notion">
            Approving calls the Notion MCP server and creates <strong>{pages}</strong> under the sandbox parent{" "}
            <span className="font-mono">{destinationAlias ?? "(unknown)"}</span>. If a step fails, pages created by this run are moved
            to trash. A successful run keeps the pages; delete them in Notion when done. The backend still requires
            REAL_WRITES_ENABLED and verifies your operator token.
          </Alert>
        </div>
      )}
      {planTarget && (
        <p className="mt-2 text-sm text-slate-700">
          Integration target: <strong>{TARGET_LABELS[planTarget]}</strong>. The server rejects this run if the plan uses any
          other integration.
        </p>
      )}
      {steps.length > 0 && (
        <ul aria-label="Actions to approve" className="mt-2 list-disc space-y-0.5 pl-5 text-sm text-slate-800">
          {steps.map((s) => (
            <li key={s.step_number}>
              {integrationLabel(s.integration)}: {actionLabel(s.action)}
            </li>
          ))}
        </ul>
      )}
      <form className="mt-4 space-y-3" onSubmit={submit} noValidate>
        <div>
          <label htmlFor={approverId} className="block text-sm font-medium text-slate-800">
            Approved by
          </label>
          <input
            id={approverId}
            value={approvedBy}
            onChange={(e) => setApprovedBy(e.target.value)}
            placeholder="synthetic.reviewer"
            aria-invalid={approvedBy.length > 0 && Boolean(approverError)}
            aria-describedby={`${approverId}-help`}
            className={inputClass}
          />
          <p id={`${approverId}-help`} className="mt-1 text-xs text-slate-600">
            {approvedBy.length > 0 && approverError ? approverError : "A label recorded with the run (not a login)."}
          </p>
        </div>
        {real && (
          <>
            <div>
              <label htmlFor={phraseId} className="block text-sm font-medium text-slate-800">
                Type <span className="font-mono">{confirmPhrase}</span> to confirm{orchestration ? " the Notion pages" : ""}
              </label>
              <input
                id={phraseId}
                value={phrase}
                onChange={(e) => setPhrase(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={phrase.length > 0 && Boolean(validateRealConfirmation(phrase, confirmPhrase))}
                className={inputClass}
              />
            </div>
            {orchestration && (
              <div>
                <label htmlFor={teamsPhraseId} className="block text-sm font-medium text-slate-800">
                  Type <span className="font-mono">{TEAMS_CONFIRMATION_PHRASE}</span> to confirm the Teams notification
                </label>
                <input
                  id={teamsPhraseId}
                  value={teamsPhrase}
                  onChange={(e) => setTeamsPhrase(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={teamsPhrase.length > 0 && Boolean(validateRealConfirmation(teamsPhrase, TEAMS_CONFIRMATION_PHRASE))}
                  className={inputClass}
                />
              </div>
            )}
            <div>
              <label htmlFor={tokenId} className="block text-sm font-medium text-slate-800">
                Operator token
              </label>
              <input
                id={tokenId}
                type="password"
                value={operatorToken}
                onChange={(e) => setOperatorToken(e.target.value)}
                autoComplete="off"
                aria-describedby={`${tokenId}-help`}
                className={inputClass}
              />
              <p id={`${tokenId}-help`} className="mt-1 text-xs text-slate-600">
                REAL_WRITE_AUTHORIZATION_TOKEN from the backend .env. Sent once as a request header; never stored.
              </p>
            </div>
          </>
        )}
        <div className="flex items-start gap-2">
          <input
            id={confirmId}
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            className="mt-0.5 h-4 w-4 rounded border-slate-400 text-indigo-600 focus:ring-indigo-500"
          />
          <label htmlFor={confirmId} className="text-sm text-slate-800">
            {real && orchestration ? (
              copy.confirm
            ) : real ? (
              <>
                {copy.confirm} {teams ? "to sandbox channel" : drive ? "in sandbox folder" : "under sandbox parent"}{" "}
                <span className="font-mono">{destinationAlias ?? "(unknown)"}</span>.
              </>
            ) : (
              copy.confirm
            )}
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={!canExecute}
            className={`rounded-lg px-4 py-2 text-sm font-semibold text-white shadow-sm focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
              real
                ? "bg-rose-600 hover:bg-rose-700 focus-visible:outline-rose-600"
                : "bg-emerald-600 hover:bg-emerald-700 focus-visible:outline-emerald-600"
            }`}
          >
            {copy.button}
          </button>
          {starting && <Spinner label="Starting execution…" />}
        </div>
        {error && <Alert title="Execution could not be started">{error}</Alert>}
      </form>
    </section>
  );
}
