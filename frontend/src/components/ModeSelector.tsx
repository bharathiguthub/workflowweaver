import { useId } from "react";

import { EXECUTION_MODES, MODE_LABELS, type ExecutionMode } from "../api/types";
import type { ModeOption } from "../state/modes";

const DESCRIPTIONS: Record<ExecutionMode, string> = {
  mock: "In-memory adapters and synthetic data. No network, no real service.",
  dry_run: "Real adapters validate destinations and build the exact requests, but nothing is sent.",
  real: "Calls the real Notion MCP server. Only the Notion Sandbox Test, after explicit confirmation.",
};

interface Props {
  value: ExecutionMode;
  options: Record<ExecutionMode, ModeOption>;
  disabled: boolean;
  onChange: (mode: ExecutionMode) => void;
}

export function ModeSelector({ value, options, disabled, onChange }: Props) {
  const name = useId();
  return (
    <section aria-labelledby={`${name}-heading`} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id={`${name}-heading`} className="text-base font-semibold text-slate-900">
        Execution mode
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        Changing the mode discards the current plan. The backend re-checks the mode, approval, write switch and operator
        token on every run.
      </p>
      <fieldset className="mt-3" disabled={disabled}>
        <legend className="sr-only">Execution mode</legend>
        <div className="grid gap-3 sm:grid-cols-3">
          {EXECUTION_MODES.map((mode) => {
            const option = options[mode];
            const id = `${name}-${mode}`;
            const selected = value === mode;
            const tone =
              mode === "real" ? "border-rose-400 bg-rose-50" : mode === "dry_run" ? "border-sky-400 bg-sky-50" : "border-indigo-500 bg-indigo-50";
            return (
              <div
                key={mode}
                className={`rounded-lg border p-3 ${selected ? tone : "border-slate-200 bg-white"} ${option.available ? "" : "opacity-70"}`}
              >
                <div className="flex items-center gap-2">
                  <input
                    id={id}
                    type="radio"
                    name={name}
                    value={mode}
                    checked={selected}
                    disabled={!option.available}
                    onChange={() => onChange(mode)}
                    aria-describedby={`${id}-desc`}
                    className="h-4 w-4 border-slate-400 text-indigo-600 focus:ring-indigo-500"
                  />
                  <label htmlFor={id} className="font-semibold text-slate-900">
                    {MODE_LABELS[mode]}
                  </label>
                </div>
                <p id={`${id}-desc`} className="mt-1 text-xs text-slate-600">
                  {DESCRIPTIONS[mode]}
                  {option.reason && <span className="mt-1 block font-medium text-amber-800">Unavailable: {option.reason}</span>}
                </p>
              </div>
            );
          })}
        </div>
      </fieldset>
    </section>
  );
}
