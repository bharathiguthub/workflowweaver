import {
  MODE_LABELS,
  actionLabel,
  aiPlannerLabel,
  isDocumentPlan,
  isDrivePlan,
  integrationLabel,
  isOrchestrationPlan,
  isProjectPlan,
  isSandboxPlan,
  type ExecutionMode,
  type PlanResponse,
  type WorkflowStep,
} from "../api/types";
import { toExecutionRequest } from "../state/forms";
import { DocumentPlanSummary } from "./documents/DocumentPlanSummary";
import { OrchestrationSummary } from "./OrchestrationSummary";
import { PlannerProvenance } from "./PlannerProvenance";
import { ProjectPlanSummary } from "./ProjectPlanSummary";
import { Alert, Badge, PlanStatusBadge } from "./StatusBadge";

const MAX_PREVIEW = 160;

export function PlanReview({ plan, mode = "mock" }: { plan: PlanResponse; mode?: ExecutionMode }) {
  const sandbox = isSandboxPlan(plan) ? plan : null;
  // The exact body POSTed on approval; only approved_by is filled in at that point.
  const request = toExecutionRequest(plan, "<entered at approval>", mode);
  return (
    <section aria-labelledby="review-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="review-heading" className="text-base font-semibold text-slate-900">
          2. Review the plan
        </h2>
        <div className="flex flex-wrap gap-2">
          <Badge tone={mode === "real" ? "danger" : mode === "dry_run" ? "info" : "warning"} label={`Mode: ${MODE_LABELS[mode]}`} />
          <PlanStatusBadge status={plan.status} />
        </div>
      </div>
      <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
        <div className="flex gap-2 sm:block">
          <dt className="text-slate-500">Planner</dt>
          <dd>
            <PlannerProvenance plan={plan} />
          </dd>
        </div>
        <div className="flex gap-2 sm:block">
          <dt className="text-slate-500">Planning attempts</dt>
          <dd className="font-medium text-slate-800">
            {plan.planning_attempts}
            {plan.retry_delays_seconds && plan.retry_delays_seconds.length > 0 && (
              <span className="ml-1 text-xs font-normal text-slate-500">
                (retried after {plan.retry_delays_seconds.map((d) => `${d.toFixed(1)}s`).join(", ")})
              </span>
            )}
          </dd>
        </div>
        <div className="flex gap-2 sm:block">
          <dt className="text-slate-500">Workflow ID</dt>
          <dd className="break-all font-mono text-xs text-slate-800">{plan.workflow_id}</dd>
        </div>
      </dl>
      {plan.summary && <p className="mt-3 text-sm text-slate-700">{plan.summary}</p>}
      {isProjectPlan(plan) && <ProjectPlanSummary plan={plan} />}
      {isOrchestrationPlan(plan) && <OrchestrationSummary plan={plan} />}
      {isDocumentPlan(plan) && <DocumentPlanSummary plan={plan} />}
      {plan.plan_adjustments && plan.plan_adjustments.length > 0 && (
        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">The server adjusted this plan (only unambiguous changes are made):</p>
          <ul aria-label="Server plan adjustments" className="mt-1 list-disc pl-5">
            {plan.plan_adjustments.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
          <p className="mt-1 text-xs">The adjusted plan below was fully re-validated. Review it before approving.</p>
        </div>
      )}
      {plan.status === "PLANNED" && plan.planned_steps.some((s) => s.integration === "google_drive") && (
        <DriveDestinations plan={plan} mode={mode} />
      )}
      {plan.status === "PLANNED" && plan.planned_steps.some((s) => s.integration === "teams") && (
        <TeamsMessagePreview plan={plan} mode={mode} />
      )}
      <p className="mt-2 text-xs text-slate-500">This plan has not been executed. Nothing runs until you approve it below.</p>

      {plan.status === "INVALID" && (
        <div className="mt-4">
          <Alert title="The plan was rejected by the validator and cannot be executed">
            <ul className="list-disc space-y-1 pl-5">
              {plan.validation.issues.map((issue, i) => (
                <li key={`${issue.code}-${i}`}>
                  <span className="font-mono text-xs">{issue.code}</span>
                  {issue.step_number !== null && <> (step {issue.step_number})</>}: {issue.message}
                </li>
              ))}
            </ul>
          </Alert>
        </div>
      )}

      {plan.status === "FAILED" && (
        <div className="mt-4">
          <Alert title="The planner could not produce a plan">
            <ul className="list-disc space-y-1 pl-5">
              {plan.errors.map((error, i) => (
                <li key={`${error.code}-${i}`}>
                  <span className="font-mono text-xs">{error.code}</span>
                  {error.provider && <span className="text-xs"> ({aiPlannerLabel(error.provider) ?? error.provider})</span>}: {error.message}
                  {error.retryable && " (retryable: try again)"}
                </li>
              ))}
            </ul>
          </Alert>
        </div>
      )}

      {sandbox && plan.status === "PLANNED" && (
        <dl aria-label="Execution details" className="mt-3 grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 rounded-lg bg-slate-50 p-3 text-sm">
          <dt className="text-slate-500">Operation</dt>
          <dd className="text-slate-800">{sandbox.operation}</dd>
          <dt className="text-slate-500">Destination</dt>
          <dd className="font-mono text-xs text-slate-800">{sandbox.destination_alias ?? "in-memory mock"}</dd>
          <dt className="text-slate-500">On failure</dt>
          <dd className="text-slate-800">{sandbox.rollback}</dd>
          <dt className="text-slate-500">Real write</dt>
          <dd className={sandbox.real_write ? "font-semibold text-rose-700" : "text-slate-800"}>
            {sandbox.real_write
              ? isDocumentPlan(sandbox)
                ? "Yes: one Markdown document in the Google Drive sandbox folder"
                : isDrivePlan(sandbox)
                ? "Yes: one run folder and one report in Google Drive"
                : "Yes: one real Notion page"
              : "No"}
          </dd>
          {isDrivePlan(sandbox) && (
            <>
              <dt className="text-slate-500">Report</dt>
              <dd className="break-all text-slate-800">
                <span className="font-mono text-xs">{sandbox.report_title}</span> ({sandbox.report_bytes} bytes, verified after
                upload)
              </dd>
            </>
          )}
        </dl>
      )}

      {plan.status === "PLANNED" && (
        <ol className="mt-4 space-y-3" aria-label="Planned steps">
          {plan.planned_steps.map((step) => (
            <li key={step.step_number} className="rounded-lg border border-slate-200 p-3">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-indigo-100 text-xs font-bold text-indigo-800">
                  {step.step_number}
                </span>
                <span className="font-semibold text-slate-900">{integrationLabel(step.integration)}</span>
                <span className="text-sm text-slate-600">{actionLabel(step.action)}</span>
                <span className="ml-auto text-xs text-slate-500">
                  {step.depends_on.length === 0
                    ? "No dependencies"
                    : `Depends on step${step.depends_on.length > 1 ? "s" : ""} ${step.depends_on.join(", ")}`}
                </span>
              </div>
              <p className="mt-1 text-sm text-slate-700">{step.description}</p>
              {Object.keys(step.arguments).length > 0 && (
                <dl className="mt-2 space-y-1 text-xs">
                  {Object.entries(step.arguments).map(([key, value]) => (
                    <div key={key} className="grid grid-cols-[6rem_1fr] gap-2">
                      <dt className="font-mono text-slate-500">{key}</dt>
                      <dd className="break-words text-slate-800">
                        {value.length > MAX_PREVIEW ? (
                          <details>
                            <summary className="cursor-pointer">{value.slice(0, MAX_PREVIEW)}…</summary>
                            {value}
                          </details>
                        ) : (
                          value
                        )}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </li>
          ))}
        </ol>
      )}

      {plan.status === "PLANNED" && (
        <details className="mt-4 text-xs" open={mode === "real"}>
          <summary className="cursor-pointer font-medium text-slate-700">Exact execution request</summary>
          <pre aria-label="Exact execution request" className="mt-2 overflow-x-auto rounded-lg bg-slate-900 p-3 text-slate-100">
            {JSON.stringify(request, null, 2)}
          </pre>
        </details>
      )}
    </section>
  );
}


/** Where every Google Drive step runs: the sandbox folder alias or a folder created by an earlier step. */
export function driveDestination(step: WorkflowStep, mode: ExecutionMode): string {
  const parent = step.arguments.parent_step;
  if (parent) return `folder created by step ${parent}`;
  if (step.arguments.folder) return `sandbox folder '${step.arguments.folder}'`;
  return mode === "mock" ? "in-memory mock folder" : "no destination (rejected)";
}

const TEAMS_MODE_NOTE: Record<ExecutionMode, string> = {
  mock: "Mock mode: delivery is simulated in memory; nothing is posted to Teams.",
  dry_run: "Dry run: the webhook request is built and checked, but nothing is posted to Teams.",
  real: "Real mode: each message is posted to the channel's Teams Workflows webhook after approval and cannot be unsent.",
};

/** Phase 7.1: exactly what each Teams step will post, and where (channel alias only, never the webhook). */
export function TeamsMessagePreview({ plan, mode }: { plan: PlanResponse; mode: ExecutionMode }) {
  const steps = plan.planned_steps.filter((s) => s.integration === "teams");
  return (
    <div className="mt-3 rounded-lg bg-slate-50 p-3 text-sm">
      <h3 className="font-medium text-slate-900">Microsoft Teams messages</h3>
      <ul aria-label="Microsoft Teams messages" className="mt-1 space-y-2">
        {steps.map((s) => {
          const message = s.arguments.message ?? "";
          return (
            <li key={s.step_number} className="rounded-md border border-slate-200 bg-white p-2">
              <p className="text-xs text-slate-600">
                Step {s.step_number} · {actionLabel(s.action)} → channel{" "}
                <span className="font-mono">{s.arguments.channel ?? "(server default)"}</span> · {message.length} / 4000 characters
              </p>
              <p className="mt-1 whitespace-pre-wrap break-words text-slate-900">{message}</p>
              {s.arguments.link_step && (
                <p className="mt-1 text-xs text-slate-600">
                  + a link to the Notion page created in step {s.arguments.link_step}, added when the message is sent.
                </p>
              )}
            </li>
          );
        })}
      </ul>
      <p className="mt-1 text-xs text-slate-600">
        Sent as an Adaptive Card (Teams Workflows webhook format). {TEAMS_MODE_NOTE[mode]}
      </p>
    </div>
  );
}

function DriveDestinations({ plan, mode }: { plan: PlanResponse; mode: ExecutionMode }) {
  const steps = plan.planned_steps.filter((s) => s.integration === "google_drive");
  const writes = steps.filter((s) => s.action === "create_folder" || s.action === "create_document").length;
  return (
    <div className="mt-3 rounded-lg bg-slate-50 p-3 text-sm">
      <h3 className="font-medium text-slate-900">Google Drive destinations</h3>
      <ul aria-label="Google Drive destinations" className="mt-1 space-y-0.5 text-slate-800">
        {steps.map((s) => (
          <li key={s.step_number}>
            Step {s.step_number} · {actionLabel(s.action)} → {driveDestination(s, mode)}
          </li>
        ))}
      </ul>
      {mode === "real" && (
        <p className="mt-1 font-semibold text-rose-700">
          Real write: {writes} Google Drive write action(s); {steps.length - writes} read-only. No delete actions exist.
        </p>
      )}
    </div>
  );
}
