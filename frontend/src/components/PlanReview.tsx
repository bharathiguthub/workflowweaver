import { actionLabel, integrationLabel, type PlanResponse } from "../api/types";
import { Alert, PlanStatusBadge } from "./StatusBadge";

const MAX_PREVIEW = 160;

export function PlanReview({ plan }: { plan: PlanResponse }) {
  return (
    <section aria-labelledby="review-heading" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="review-heading" className="text-base font-semibold text-slate-900">
          2. Review the plan
        </h2>
        <PlanStatusBadge status={plan.status} />
      </div>
      <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
        <div className="flex gap-2 sm:block">
          <dt className="text-slate-500">Planner</dt>
          <dd className="font-medium text-slate-800">{plan.planner}</dd>
        </div>
        <div className="flex gap-2 sm:block">
          <dt className="text-slate-500">Planning attempts</dt>
          <dd className="font-medium text-slate-800">{plan.planning_attempts}</dd>
        </div>
        <div className="flex gap-2 sm:block">
          <dt className="text-slate-500">Workflow ID</dt>
          <dd className="break-all font-mono text-xs text-slate-800">{plan.workflow_id}</dd>
        </div>
      </dl>
      {plan.summary && <p className="mt-3 text-sm text-slate-700">{plan.summary}</p>}
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
                  <span className="font-mono text-xs">{error.code}</span>: {error.message}
                  {error.retryable && " (retryable: try again)"}
                </li>
              ))}
            </ul>
          </Alert>
        </div>
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
    </section>
  );
}
