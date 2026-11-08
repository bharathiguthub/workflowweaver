import { useEffect, useId, useState } from "react";

import { errorMessage, listOrchestrationSources } from "../api/client";
import type { ExecutionMode, OrchestrationInput, OrchestrationSourcesResponse } from "../api/types";
import { Alert, Spinner } from "./StatusBadge";

export type InputChoice = "instructions" | "document" | "document_instructions";

interface Props {
  mode: ExecutionMode;
  /** Why plans WITH a Google Drive source cannot be made in this mode (null: they can). */
  sourceBlockedReason: string | null;
  /** Why instructions-only plans cannot be made in this mode (null: they can). */
  instructionsBlockedReason: string | null;
  planning: boolean;
  onPlan: (input: OrchestrationInput) => void;
}

export const INSTRUCTIONS_MIN = 10;
export const INSTRUCTIONS_MAX = 5000;

const CHOICES: { value: InputChoice; label: string; help: string }[] = [
  { value: "instructions", label: "Instructions only",
    help: "Groq (Gemini fallback) writes the project plan from your instructions. No Google Drive document is used; every item is labelled AI-generated." },
  { value: "document", label: "Google Drive document only",
    help: "The phases are extracted from the selected document. Nothing is invented: items not found in it are flagged." },
  { value: "document_instructions", label: "Google Drive document + instructions",
    help: "The document is the only factual source; your instructions say how to process it (what to include, order, wording)." },
];

const MODE_NOTE: Record<ExecutionMode, string> = {
  mock: "Mock: synthetic source documents, a deterministic (non-AI) planner and in-memory Notion and Teams. No network call.",
  dry_run:
    "Dry run: synthetic source documents (Google Drive is not contacted), real AI planning (Groq with Gemini fallback), and every Notion and Teams request is built and checked but not sent.",
  real: "Real: reads the selected document from your approved Google Drive sandbox (read-only) if you use one, plans with Groq (Gemini fallback), and after your approval creates real Notion pages, then posts one Teams message.",
};

const shortId = (id: string) => (id.length > 14 ? `${id.slice(0, 6)}…${id.slice(-6)}` : id);

/** Validation of the instructions text, mirroring the server (which re-checks everything). */
export function instructionsError(text: string, required: boolean): string | null {
  const trimmed = text.trim();
  if (!trimmed) return required ? "Enter workflow instructions." : null;
  if (trimmed.length < INSTRUCTIONS_MIN) return `Instructions must be at least ${INSTRUCTIONS_MIN} characters.`;
  if (text.length > INSTRUCTIONS_MAX) return `Instructions are limited to ${INSTRUCTIONS_MAX} characters.`;
  return null;
}

/** Phase 7.2: choose the inputs (instructions and/or a Drive source by id), then plan. Nothing is written here. */
export function OrchestrationWorkflow({ mode, sourceBlockedReason, instructionsBlockedReason, planning, onPlan }: Props) {
  const firstAvailable: InputChoice = sourceBlockedReason && !instructionsBlockedReason ? "instructions" : "document";
  const [choice, setChoice] = useState<InputChoice>(firstAvailable);
  const [sources, setSources] = useState<OrchestrationSourcesResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [instructions, setInstructions] = useState("");
  const [phases, setPhases] = useState(5);
  const name = useId();
  const choiceName = useId();
  const phasesId = useId();
  const instructionsId = useId();

  const usesDocument = choice !== "instructions";
  const usesInstructions = choice !== "document";
  const blockedReason = usesDocument ? sourceBlockedReason : instructionsBlockedReason;

  // If the current choice becomes unavailable (mode change), switch to one that works.
  useEffect(() => {
    if (usesDocument && sourceBlockedReason && !instructionsBlockedReason) setChoice("instructions");
  }, [usesDocument, sourceBlockedReason, instructionsBlockedReason]);

  useEffect(() => {
    let active = true;
    setSources(null);
    setSelected(null);
    setLoadError(null);
    if (!usesDocument || sourceBlockedReason) return;
    listOrchestrationSources(mode)
      .then((s) => active && setSources(s))
      .catch((error: unknown) => active && setLoadError(errorMessage(error)));
    return () => {
      active = false;
    };
  }, [mode, usesDocument, sourceBlockedReason]);

  const phasesValid = Number.isInteger(phases) && phases >= 1 && phases <= 7;
  const textError = usesInstructions ? instructionsError(instructions, true) : null;
  const ready = !blockedReason && phasesValid && !textError && (!usesDocument || Boolean(selected)) && !planning;

  const submit = () => {
    if (!ready) return;
    onPlan({
      sourceFileId: usesDocument ? selected : null,
      instructions: usesInstructions ? instructions.trim() : null,
      expectedPhases: phases,
    });
  };

  return (
    <section aria-labelledby="orchestration-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id="orchestration-heading" className="text-base font-semibold text-slate-900">
        1. Google Drive → Notion → Teams
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        Plan a project from your instructions and/or an implementation plan in the approved Google Drive sandbox, create a Notion
        project page with one child page per phase, then notify the approved Teams channel. You review the full plan and approve
        it once before anything is written.
      </p>
      <p className="mt-2 rounded-lg bg-slate-50 p-2 text-xs text-slate-700">{MODE_NOTE[mode]}</p>

      <fieldset className="mt-3">
        <legend className="text-sm font-medium text-slate-800">Input</legend>
        <div role="radiogroup" aria-label="Workflow input" className="mt-1 grid gap-2 sm:grid-cols-3">
          {CHOICES.map((c) => {
            const unavailable = c.value === "instructions" ? instructionsBlockedReason : sourceBlockedReason;
            return (
              <label
                key={c.value}
                className={`flex cursor-pointer items-start gap-2 rounded-lg border p-2 text-sm ${
                  choice === c.value ? "border-indigo-500 bg-indigo-50" : "border-slate-200"
                } ${unavailable ? "cursor-not-allowed opacity-60" : ""}`}
              >
                <input
                  type="radio"
                  name={choiceName}
                  value={c.value}
                  checked={choice === c.value}
                  disabled={Boolean(unavailable)}
                  onChange={() => setChoice(c.value)}
                  aria-label={c.label}
                  className="mt-0.5"
                />
                <span>
                  <span className="block font-medium text-slate-900">{c.label}</span>
                  <span className="block text-xs text-slate-600">{unavailable ?? c.help}</span>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      {blockedReason && (
        <div className="mt-3">
          <Alert tone="warning" title="Not available in this mode">
            {blockedReason}
          </Alert>
        </div>
      )}

      {usesInstructions && !blockedReason && (
        <div className="mt-3">
          <label htmlFor={instructionsId} className="block text-sm font-medium text-slate-800">
            Workflow Instructions
          </label>
          <textarea
            id={instructionsId}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={6}
            maxLength={INSTRUCTIONS_MAX + 1}
            aria-invalid={instructions.length > 0 && Boolean(textError)}
            aria-describedby={`${instructionsId}-help`}
            placeholder={
              usesDocument
                ? "e.g. Focus on the backend work, keep each phase concise and list acceptance criteria first."
                : "e.g. Create a project plan for our Loyalty App with five phases: discovery, design, build, launch and support."
            }
            className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
          />
          <p id={`${instructionsId}-help`} className="mt-1 text-xs text-slate-600">
            {instructions.length > 0 && textError ? textError : `${instructions.length} / ${INSTRUCTIONS_MAX} characters.`}{" "}
            Plain text only. Instructions cannot change the destinations (sandbox Notion parent and Teams channel), add other tools,
            or skip approval; such requests are listed and not applied.
          </p>
        </div>
      )}

      {usesDocument && !blockedReason && !sources && !loadError && (
        <div className="mt-3">
          <Spinner label="Loading source documents…" />
        </div>
      )}
      {usesDocument && loadError && (
        <div className="mt-3">
          <Alert title="Could not list source documents">{loadError}</Alert>
        </div>
      )}

      {usesDocument && sources && !blockedReason && (
        <fieldset className="mt-3">
          <legend className="text-sm font-medium text-slate-800">
            Source document in sandbox folder <span className="font-mono">{sources.folder_alias ?? "(unknown)"}</span>
            {sources.simulated && <span className="ml-2 text-xs font-normal text-amber-800">(simulated: synthetic fixtures)</span>}
          </legend>
          {sources.sources.length === 0 ? (
            <p className="mt-2 text-sm text-slate-700">
              No Markdown document is visible to this app in the sandbox folder. Upload one with{" "}
              <span className="font-mono">scripts.google_drive_sandbox upload-source --confirm</span> (files added in the Drive
              web UI are not visible under the drive.file scope).
            </p>
          ) : (
            <ul aria-label="Source documents" className="mt-2 space-y-2">
              {sources.sources.map((s) => (
                <li key={s.file_id}>
                  <label
                    className={`flex cursor-pointer items-start gap-2 rounded-lg border p-2 text-sm ${
                      selected === s.file_id ? "border-indigo-500 bg-indigo-50" : "border-slate-200"
                    }`}
                  >
                    <input
                      type="radio"
                      name={name}
                      value={s.file_id}
                      checked={selected === s.file_id}
                      onChange={() => setSelected(s.file_id)}
                      aria-label={`${s.name} (${s.file_id})`}
                      className="mt-0.5"
                    />
                    <span className="min-w-0">
                      <span className="block font-medium text-slate-900">{s.name}</span>
                      <span className="block text-xs text-slate-600">
                        id <span className="font-mono" title={s.file_id}>{shortId(s.file_id)}</span>
                        {s.modified_time && <> · {s.modified_time}</>}
                        {s.size_bytes !== null && <> · {s.size_bytes} bytes</>}
                      </span>
                      {s.duplicate_name && (
                        <span className="mt-0.5 block text-xs font-medium text-amber-800">
                          Another file has the same name: check the id and date before selecting.
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>
      )}

      {!blockedReason && (
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <div>
            <label htmlFor={phasesId} className="block text-sm font-medium text-slate-800">
              Expected phases
            </label>
            <input
              id={phasesId}
              type="number"
              min={1}
              max={7}
              value={phases}
              onChange={(e) => setPhases(Number(e.target.value))}
              className="mt-1 block w-24 rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
          <button
            type="button"
            disabled={!ready}
            onClick={submit}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {usesDocument ? "Read Source and Extract Plan" : "Generate Plan from Instructions"}
          </button>
          {planning && <Spinner label={usesDocument ? "Reading the source and extracting phases…" : "Generating the plan…"} />}
        </div>
      )}
      {!blockedReason && (
        <p className="mt-1 text-xs text-slate-500">
          {usesDocument
            ? "The document is only read. If it defines a different number of phases, the plan is rejected rather than invented."
            : "The plan must have exactly the expected number of phases; otherwise it is rejected."}
        </p>
      )}
    </section>
  );
}
