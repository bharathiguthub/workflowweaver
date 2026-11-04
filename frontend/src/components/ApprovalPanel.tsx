import { useId, useState, type FormEvent } from "react";

import { validateApprover } from "../state/forms";
import { Alert, Spinner } from "./StatusBadge";

interface Props {
  stepCount: number;
  busy: boolean;
  starting: boolean;
  error: string | null;
  onApprove: (approvedBy: string) => void;
}

export function ApprovalPanel({ stepCount, busy, starting, error, onApprove }: Props) {
  const [approvedBy, setApprovedBy] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const approverId = useId();
  const confirmId = useId();
  const approverError = validateApprover(approvedBy);
  const canExecute = confirmed && !approverError && !busy;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (canExecute) onApprove(approvedBy);
  };

  return (
    <section aria-labelledby="approve-heading" className="rounded-xl border border-amber-200 bg-amber-50/40 p-4 shadow-sm sm:p-6">
      <h2 id="approve-heading" className="text-base font-semibold text-slate-900">
        3. Approve mock execution
      </h2>
      <p className="mt-1 text-sm text-slate-700">
        Execution is <strong>simulated</strong>: the {stepCount} step(s) above run against in-memory mock adapters. No real
        Notion page, Drive document or Teams message is created.
      </p>
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
            className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 sm:max-w-xs"
          />
          <p id={`${approverId}-help`} className="mt-1 text-xs text-slate-600">
            {approvedBy.length > 0 && approverError ? approverError : "A label recorded with the run (not a login)."}
          </p>
        </div>
        <div className="flex items-start gap-2">
          <input
            id={confirmId}
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            className="mt-0.5 h-4 w-4 rounded border-slate-400 text-indigo-600 focus:ring-indigo-500"
          />
          <label htmlFor={confirmId} className="text-sm text-slate-800">
            I reviewed this plan and approve running it in mock mode.
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={!canExecute}
            className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-emerald-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Approve and Execute Mock Workflow
          </button>
          {starting && <Spinner label="Starting execution…" />}
        </div>
        {error && <Alert title="Execution could not be started">{error}</Alert>}
      </form>
    </section>
  );
}
