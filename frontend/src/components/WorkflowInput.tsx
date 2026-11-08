import { useId, useState, type FormEvent, type ReactNode } from "react";

import { integrationLabel, type WorkflowTemplate } from "../api/types";
import { INSTRUCTION_MAX, validateInstruction, validateName } from "../state/forms";
import { Alert, Spinner } from "./StatusBadge";

interface Props {
  templates: WorkflowTemplate[];
  templatesState: "loading" | "loaded" | "error";
  templatesError: string | null;
  planning: boolean;
  /** Set when the selected mode cannot plan templates or custom instructions (real mode). */
  blockedReason?: string | null;
  /** Shown above the form, e.g. what a Real-mode Google Drive plan may do. */
  notice?: string | null;
  /** "real" (default): a red Real mode notice; "info": a neutral notice for mock/dry-run. */
  noticeTone?: "real" | "info";
  onSubmit: (instruction: string, name: string) => void;
  /** Phase 8.2: section-specific heading and intro (defaults keep the original wording). */
  heading?: string;
  intro?: string;
  /** A larger instruction box (AI Workflow Builder). */
  prominent?: boolean;
  placeholder?: string;
  /** Rendered under the instruction, e.g. warnings about unsupported tools. */
  renderHints?: (instruction: string) => ReactNode;
}

export function WorkflowInput({
  templates,
  templatesState,
  templatesError,
  planning,
  blockedReason = null,
  notice = null,
  noticeTone = "real",
  onSubmit,
  heading = "1. Describe the workflow",
  intro = "Start from a template or write your own instruction. Use synthetic data only.",
  prominent = false,
  placeholder = "e.g. Read meeting notes, create Notion tasks, and notify the team in Microsoft Teams.",
  renderHints,
}: Props) {
  const [instruction, setInstruction] = useState("");
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const instructionId = useId();
  const nameId = useId();

  const instructionError = validateInstruction(instruction);
  const nameError = validateName(name);
  const showInstructionError = (submitted || instruction.length > 0) && instructionError;
  const showNameError = (submitted || name.length > 0) && nameError;

  const chooseTemplate = (template: WorkflowTemplate) => {
    setSelected(template.id);
    // The backend owns the example instruction, and its tests verify that it plans and
    // executes successfully against the mock tools.
    setInstruction(template.example_instruction);
    setName(template.name);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (instructionError || nameError || planning || blockedReason) return;
    onSubmit(instruction, name);
  };

  return (
    <section aria-labelledby="describe-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id="describe-heading" className="text-base font-semibold text-slate-900">
        {heading}
      </h2>
      <p className="mt-1 text-sm text-slate-600">{intro}</p>
      {notice && !blockedReason && (
        <p
          className={`mt-2 rounded-lg p-2 text-sm ${noticeTone === "real" ? "bg-rose-50 text-rose-900" : "bg-sky-50 text-sky-900"}`}
          aria-label={noticeTone === "real" ? "Real mode notice" : "Workflow notice"}
        >
          {notice}
        </p>
      )}
      {blockedReason && (
        <div className="mt-3">
          <Alert tone="info" title="Not available in this mode">
            {blockedReason}
          </Alert>
        </div>
      )}

      <div className="mt-4">
        {templatesState === "loading" && <Spinner label="Loading templates…" />}
        {templatesState === "error" && (
          <Alert tone="warning" title="Templates unavailable">
            {templatesError} You can still type an instruction.
          </Alert>
        )}
        {templatesState === "loaded" && (
          <ul className="grid gap-3 sm:grid-cols-3" aria-label="Workflow templates">
            {templates.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  disabled={Boolean(blockedReason)}
                  onClick={() => chooseTemplate(t)}
                  aria-pressed={selected === t.id}
                  className={`h-full w-full rounded-lg border p-3 text-left transition hover:border-indigo-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 ${
                    selected === t.id ? "border-indigo-500 bg-indigo-50" : "border-slate-200 bg-white"
                  }`}
                >
                  <span className="block font-semibold text-slate-900">{t.name}</span>
                  <span className="mt-1 block text-sm text-slate-600">{t.description}</span>
                  <span className="mt-2 flex flex-wrap gap-1">
                    {t.integrations.map((i) => (
                      <span key={i} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700">
                        {integrationLabel(i)}
                      </span>
                    ))}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <form className="mt-4 space-y-4" onSubmit={submit} noValidate>
        <div>
          <label htmlFor={instructionId} className="block text-sm font-medium text-slate-800">
            Instruction
          </label>
          <textarea
            id={instructionId}
            value={instruction}
            onChange={(e) => {
              setInstruction(e.target.value);
              setSelected(null);
            }}
            rows={prominent ? 7 : 4}
            maxLength={INSTRUCTION_MAX + 500}
            aria-invalid={Boolean(showInstructionError)}
            aria-describedby={`${instructionId}-help ${instructionId}-error`}
            placeholder={placeholder}
            className={`mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 ${prominent ? "text-base" : "text-sm"} shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 aria-[invalid=true]:border-rose-400`}
          />
          <div className="mt-1 flex justify-between gap-2 text-xs">
            <p id={`${instructionId}-error`} className="text-rose-700">
              {showInstructionError || ""}
            </p>
            <p id={`${instructionId}-help`} className="shrink-0 text-slate-500">
              {instruction.trim().length}/{INSTRUCTION_MAX}
            </p>
          </div>
          {renderHints?.(instruction)}
        </div>

        <div>
          <label htmlFor={nameId} className="block text-sm font-medium text-slate-800">
            Workflow name <span className="font-normal text-slate-500">(optional)</span>
          </label>
          <input
            id={nameId}
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-invalid={Boolean(showNameError)}
            aria-describedby={`${nameId}-error`}
            className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 aria-[invalid=true]:border-rose-400 sm:max-w-md"
          />
          <p id={`${nameId}-error`} className="mt-1 text-xs text-rose-700">
            {showNameError || ""}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={planning || Boolean(blockedReason)}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-60"
          >
            Generate Plan
          </button>
          {planning && <Spinner label="Planning…" />}
        </div>
      </form>
    </section>
  );
}
