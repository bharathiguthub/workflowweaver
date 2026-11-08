import { integrationLabel, type Integration } from "../api/types";
import { SUPPORTED_ACTIONS, UNSUPPORTED_OPERATIONS, UNSUPPORTED_TOOLS, findUnsupported } from "../state/capabilities";
import { Card } from "./layout/Page";

/** What the AI Workflow Builder can and cannot plan (mirrors the server's allow-list). */
export function CapabilityMatrix() {
  return (
    <Card
      id="capabilities"
      title="What the builder can plan"
      subtitle="Plans may use only these actions, within one integration target. The server rejects anything else before approval and again before execution."
    >
      <div className="grid gap-4 md:grid-cols-3">
        {(Object.keys(SUPPORTED_ACTIONS) as Integration[]).map((integration) => (
          <div key={integration} className="rounded-lg border border-slate-200 p-3">
            <p className="text-sm font-semibold text-slate-900">{integrationLabel(integration)}</p>
            <ul aria-label={`${integrationLabel(integration)} supported actions`} className="mt-2 space-y-1.5">
              {SUPPORTED_ACTIONS[integration].map((a) => (
                <li key={a.action} className="text-sm">
                  <span className="mr-1 text-emerald-600" aria-hidden="true">✓</span>
                  <span className="font-medium text-slate-800">{a.label}</span>
                  <span className="block pl-4 text-xs text-slate-500">{a.note}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3">
        <p className="text-sm font-semibold text-slate-900">Not supported</p>
        <ul aria-label="Unsupported integrations and actions" className="mt-2 flex flex-wrap gap-1.5">
          {[...UNSUPPORTED_TOOLS, ...UNSUPPORTED_OPERATIONS].map((u) => (
            <li key={u.name} className="rounded-full bg-white px-2 py-0.5 text-xs text-slate-600 ring-1 ring-slate-300">
              <span aria-hidden="true">✕ </span>
              {u.name}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-slate-600">
          Free-form plans cannot chain arbitrary tools: choose one integration target. The only cross-tool workflow is{" "}
          <strong>Google Drive → Notion → Teams</strong> in Business Workflows. Mixing integrations (Multi-Integration) is a
          preview for mock and dry-run templates only.
        </p>
      </div>
    </Card>
  );
}

/** Live hint under the builder's instruction box when it names something unsupported. */
export function UnsupportedHints({ instruction }: { instruction: string }) {
  const hits = findUnsupported(instruction);
  if (hits.length === 0) return null;
  return (
    <div role="status" aria-label="Unsupported request" className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
      <p className="font-semibold">WorkflowWeaver cannot do part of this request</p>
      <ul className="mt-1 list-disc pl-5">
        {hits.map((h) => (
          <li key={h.name}>
            {h.kind === "tool" ? `${h.name} is not a supported integration.` : `${h.name} is not a supported action.`}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-xs">
        The planner will leave it out or the plan will be rejected. Rephrase using Google Drive, Notion or Microsoft Teams actions.
      </p>
    </div>
  );
}
