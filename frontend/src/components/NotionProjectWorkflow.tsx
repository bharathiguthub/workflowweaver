import { useId, useState, type FormEvent } from "react";

import { MODE_LABELS, aiPlannerLabel, selectedProvider, type ExecutionMode, type PlannerStatus } from "../api/types";
import { INSTRUCTION_MAX, validateInstruction } from "../state/forms";
import { Badge, Spinner } from "./StatusBadge";

export const PROJECT_EXAMPLE =
  "Create a project plan in Notion for our Customer Rewards Platform. We need customer registration, reward balance lookup, points redemption, and transaction history. Create an action item for each feature with a description, suggested priority, and owner. Show me the plan before creating anything.";

interface Props {
  mode: ExecutionMode;
  planner: PlannerStatus | null;
  parentAlias: string | null;
  /** Why this workflow cannot be planned in the selected mode (null: it can). */
  blockedReason: string | null;
  planning: boolean;
  onPlan: (instruction: string) => void;
}

export function NotionProjectWorkflow({ mode, planner, parentAlias, blockedReason, planning, onPlan }: Props) {
  const [instruction, setInstruction] = useState(PROJECT_EXAMPLE);
  const [submitted, setSubmitted] = useState(false);
  const id = useId();
  const error = validateInstruction(instruction);
  const provider = aiPlannerLabel(selectedProvider(planner)); // "Claude" | "Gemini" | "Groq" | null (mock stub)
  const ready = planner?.ai_ready ?? planner?.claude_ready ?? false;
  const fallback = planner?.mode === "auto" ? aiPlannerLabel(planner.fallback) : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (!error && !blockedReason && !planning) onPlan(instruction);
  };

  return (
    <section aria-labelledby={`${id}-heading`} className="rounded-xl border border-indigo-200 bg-white p-4 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={`${id}-heading`} className="text-base font-semibold text-slate-900">
          AI Project Plan to Notion
        </h2>
        {planner &&
          (provider ? (
            <span className="flex flex-wrap gap-2">
              <Badge tone={ready ? "success" : "danger"} label={ready ? `AI planner: ${provider} · ${planner.model}` : `${provider} not configured`} />
              {fallback && (
                <Badge
                  tone={planner.fallback_ready ? "info" : "warning"}
                  label={planner.fallback_ready ? `Fallback: ${fallback} · ${planner.fallback_model}` : `Fallback ${fallback} not configured`}
                />
              )}
            </span>
          ) : (
            <Badge tone="warning" label="Mock planner (not AI)" />
          ))}
      </div>
      <p className="mt-1 text-sm text-slate-600">
        Describe the project in plain English. {provider ?? "The planner"} proposes a project page and one action item per feature
        (description, suggested priority, suggested owning team). Nothing is created until you review and approve the plan
        {mode !== "mock" && parentAlias ? (
          <>
            ; pages go only under the sandbox parent <span className="font-mono">{parentAlias}</span>
          </>
        ) : null}
        .
      </p>
      {!provider && mode !== "real" && (
        <p className="mt-1 text-xs text-amber-800">
          PLANNER_MODE=mock: plans come from a keyword stub, not AI. Real mode requires an AI planner (Claude, Gemini or auto).
        </p>
      )}
      <form className="mt-3 space-y-3" onSubmit={submit} noValidate>
        <div>
          <label htmlFor={`${id}-instruction`} className="block text-sm font-medium text-slate-800">
            Project requirements
          </label>
          <textarea
            id={`${id}-instruction`}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            rows={4}
            maxLength={INSTRUCTION_MAX + 500}
            aria-invalid={submitted && Boolean(error)}
            className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
          />
          {submitted && error && <p className="mt-1 text-xs text-rose-700">{error}</p>}
        </div>
        {blockedReason && <p className="text-sm font-medium text-amber-800">Unavailable: {blockedReason}</p>}
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={planning || Boolean(blockedReason)}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-60"
          >
            Generate AI Project Plan ({MODE_LABELS[mode]})
          </button>
          <button type="button" onClick={() => setInstruction(PROJECT_EXAMPLE)} className="text-sm font-medium text-indigo-700 underline">
            Use the Customer Rewards example
          </button>
          {planning && <Spinner label={provider ? `${provider} is planning…` : "Planning…"} />}
        </div>
      </form>
    </section>
  );
}
