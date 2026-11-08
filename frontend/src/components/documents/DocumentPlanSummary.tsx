import type { DocumentPlanResponse } from "../../api/types";
import { Badge } from "../StatusBadge";
import { MarkdownPreview } from "./MarkdownPreview";

/** Server-side maximum for a generated document (backend RENDERED_MAX). */
export const DOCUMENT_CHAR_LIMIT = 4800;

/** Rendered preview of the generated document, shown in the plan review before any upload. */
export function DocumentPlanSummary({ plan }: { plan: DocumentPlanResponse }) {
  if (!plan.content) return null;
  return (
    <div className="mt-4 space-y-3" aria-label="Generated document">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="violet" label={plan.document_label} />
        <Badge tone={plan.ai_generated ? "info" : "warning"} label={plan.ai_generated ? "AI-generated draft" : "Mock planner (not AI)"} />
        {plan.project_name && <span className="text-xs text-slate-600">Project: {plan.project_name}</span>}
      </div>
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-slate-500">File name</dt>
        <dd className="break-all font-mono text-xs text-slate-800">{plan.file_name}</dd>
        <dt className="text-slate-500">Size</dt>
        <dd className="text-slate-800">
          {plan.content_chars} of {DOCUMENT_CHAR_LIMIT} characters (Markdown)
        </dd>
      </dl>
      {plan.compression_retry && (
        <p role="note" className="rounded-lg bg-sky-50 p-2 text-xs text-sky-900">
          The first draft was {plan.initial_chars ?? "over"} characters, above the {DOCUMENT_CHAR_LIMIT}-character limit, so the AI
          rewrote it once more concisely. This shorter version is the one you are reviewing; nothing was cut automatically.
        </p>
      )}
      <MarkdownPreview content={plan.content} label="Generated document preview" />
      <p className="text-xs text-slate-500">
        This is exactly what would be uploaded. Your approval is bound to this content: any change after review is rejected by the server.
      </p>
    </div>
  );
}
