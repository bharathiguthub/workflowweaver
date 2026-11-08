import { MODE_LABELS, type ExecutionMode } from "../api/types";
import { Spinner } from "./StatusBadge";

interface Props {
  mode: ExecutionMode;
  parentAlias: string | null;
  /** Why the test cannot be planned in this mode (null: it can). */
  blockedReason: string | null;
  planning: boolean;
  onPlan: () => void;
}

export function NotionSandboxTest({ mode, parentAlias, blockedReason, planning, onPlan }: Props) {
  return (
    <section aria-labelledby="sandbox-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id="sandbox-heading" className="text-base font-semibold text-slate-900">
        Notion Sandbox Test
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        One step, Notion only (no Google Drive or Teams): create a single page with synthetic content
        {mode === "mock" ? " in the in-memory mock" : parentAlias ? <> under the sandbox parent <span className="font-mono">{parentAlias}</span></> : " under the sandbox parent"}.
        The server builds the plan; nothing runs until you approve it.
      </p>
      {blockedReason && <p className="mt-2 text-sm font-medium text-amber-800">Unavailable: {blockedReason}</p>}
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={onPlan}
          disabled={planning || Boolean(blockedReason)}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-60"
        >
          Generate Sandbox Test Plan ({MODE_LABELS[mode]})
        </button>
        {planning && <Spinner label="Planning…" />}
      </div>
    </section>
  );
}
