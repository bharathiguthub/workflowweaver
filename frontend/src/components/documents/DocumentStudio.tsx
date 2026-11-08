import { useId, useState, type FormEvent } from "react";

import { DOCUMENT_TYPE_LABELS, type DocumentRequest, type DocumentType, type ExecutionMode } from "../../api/types";
import { Alert, Spinner } from "../StatusBadge";
import { Card, buttonPrimary, inputBase } from "../layout/Page";

export const DOCUMENT_TYPES = Object.keys(DOCUMENT_TYPE_LABELS) as DocumentType[];

export const TITLE_MAX = 120;
export const PROJECT_MAX = 80;
export const INSTRUCTION_MIN = 10;
export const INSTRUCTION_MAX = 4000;

const PLACEHOLDERS: Record<DocumentType, string> = {
  requirements: "Requirements for customer registration, reward balance lookup, points redemption and transaction history.",
  technical_design: "Design a points ledger service with an API layer, an event queue and a reporting store.",
  implementation_plan: "Plan the rollout of the rewards platform in three increments with testing and a pilot.",
  api_specification: "Describe a REST API to look up a customer's reward balance and redeem points.",
  readme: "README for the rewards-service repository: what it does, configuration and how to run it locally.",
  meeting_notes: "Notes for the rewards kickoff: scope agreed, pilot region chosen, open questions on tiers.",
  custom: "A one-page FAQ for store staff explaining how customers earn and redeem points.",
};

export function validateDocumentRequest(r: DocumentRequest): string | null {
  const title = r.title.trim();
  if (title.length < 3) return "Enter a document title (at least 3 characters).";
  if (title.length > TITLE_MAX) return `The title is limited to ${TITLE_MAX} characters.`;
  if ((r.projectName ?? "").trim().length > PROJECT_MAX) return `The project name is limited to ${PROJECT_MAX} characters.`;
  const text = r.instruction.trim();
  if (text.length < INSTRUCTION_MIN) return `Describe the document (at least ${INSTRUCTION_MIN} characters).`;
  if (text.length > INSTRUCTION_MAX) return `Instructions are limited to ${INSTRUCTION_MAX} characters.`;
  return null;
}

interface Props {
  mode: ExecutionMode;
  folderAlias: string | null;
  plannerLabel: string;
  blockedReason: string | null;
  planning: boolean;
  onGenerate: (request: DocumentRequest) => void;
}

/** Natural-language document request. Generating only previews: nothing is uploaded until approval. */
export function DocumentStudio({ mode, folderAlias, plannerLabel, blockedReason, planning, onGenerate }: Props) {
  const id = useId();
  const [documentType, setDocumentType] = useState<DocumentType>("requirements");
  const [title, setTitle] = useState("");
  const [projectName, setProjectName] = useState("");
  const [instruction, setInstruction] = useState("");
  const [touched, setTouched] = useState(false);
  const request: DocumentRequest = { documentType, title, projectName, instruction };
  const error = validateDocumentRequest(request);
  const destination =
    mode === "mock" ? "In-memory mock Drive (no network)" : `Google Drive sandbox folder '${folderAlias ?? "not configured"}'`;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (error || blockedReason || planning) return;
    onGenerate(request);
  };

  return (
    <Card
      id={`${id}-studio`}
      title="Create a document"
      subtitle={`${plannerLabel} drafts the document. You preview it, then approve the upload. Nothing is written before approval.`}
    >
      <form onSubmit={submit} noValidate className="space-y-4">
        <fieldset>
          <legend className="text-sm font-medium text-slate-800">Document type</legend>
          <div role="radiogroup" aria-label="Document type" className="mt-2 flex flex-wrap gap-2">
            {DOCUMENT_TYPES.map((t) => (
              <label
                key={t}
                className={`cursor-pointer rounded-full border px-3 py-1.5 text-sm font-medium has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-indigo-600 ${
                  documentType === t ? "border-indigo-500 bg-indigo-50 text-indigo-700" : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
                }`}
              >
                <input type="radio" name={`${id}-type`} value={t} checked={documentType === t} onChange={() => setDocumentType(t)}
                  className="sr-only" />
                {DOCUMENT_TYPE_LABELS[t]}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor={`${id}-title`} className="block text-sm font-medium text-slate-800">
              Document title
            </label>
            <input id={`${id}-title`} value={title} maxLength={TITLE_MAX + 20} onChange={(e) => setTitle(e.target.value)}
              placeholder="Customer Rewards Requirements" className={inputBase} />
          </div>
          <div>
            <label htmlFor={`${id}-project`} className="block text-sm font-medium text-slate-800">
              Project name <span className="font-normal text-slate-500">(optional)</span>
            </label>
            <input id={`${id}-project`} value={projectName} maxLength={PROJECT_MAX + 20} onChange={(e) => setProjectName(e.target.value)}
              placeholder="Customer Rewards Platform" className={inputBase} />
          </div>
        </div>
        <div>
          <label htmlFor={`${id}-instruction`} className="block text-sm font-medium text-slate-800">
            What should the document cover?
          </label>
          <textarea id={`${id}-instruction`} rows={5} value={instruction} onChange={(e) => setInstruction(e.target.value)}
            placeholder={PLACEHOLDERS[documentType]} aria-describedby={`${id}-help`} className={inputBase} />
          <p id={`${id}-help`} className="mt-1 flex justify-between gap-2 text-xs text-slate-500">
            <span>Do not include confidential data. Code, commands, URLs and email addresses are rejected.</span>
            <span className="tabular-nums">
              {instruction.trim().length}/{INSTRUCTION_MAX}
            </span>
          </p>
        </div>
        <dl className="grid gap-x-6 gap-y-1 rounded-lg bg-slate-50 p-3 text-sm sm:grid-cols-[8rem_1fr]">
          <dt className="text-slate-500">Destination</dt>
          <dd className="font-medium text-slate-800">{destination}</dd>
          <dt className="text-slate-500">Operations</dt>
          <dd className="text-slate-800">Upload one Markdown file (verified), then read it back. Nothing else.</dd>
        </dl>
        {touched && error && <Alert tone="warning" title="Check the form">{error}</Alert>}
        {blockedReason && <Alert tone="warning" title="Document generation is not available in this mode">{blockedReason}</Alert>}
        <div className="flex flex-wrap items-center gap-3">
          <button type="submit" disabled={planning || Boolean(blockedReason)} className={buttonPrimary}>
            Generate Document Preview
          </button>
          {planning && <Spinner label="Drafting the document…" />}
        </div>
      </form>
    </Card>
  );
}
