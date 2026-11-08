import {
  isDocumentPlan,
  isDrivePlan,
  isOrchestrationPlan,
  isSandboxPlan,
  stepsOutsideTarget,
  TARGET_LABELS,
  type ExecutionMode,
  type IntegrationTarget,
  type IntegrationsStatus,
  type PlanResponse,
} from "../api/types";
import type { useExecution } from "../state/useExecution";
import { ApprovalPanel } from "./ApprovalPanel";
import { ExecutionMonitor } from "./ExecutionMonitor";
import { ExecutionReport } from "./ExecutionReport";
import { PlanReview } from "./PlanReview";
import { Alert } from "./StatusBadge";

interface Props {
  plan: PlanResponse | null;
  planMode: ExecutionMode;
  planError: string | null;
  /** The integration target of the section the plan belongs to. */
  target: IntegrationTarget;
  status: IntegrationsStatus | null;
  execution: ReturnType<typeof useExecution>;
  executionActive: boolean;
}

/**
 * Review -> approve -> execute -> report, shared by every workflow section. Approval is offered
 * only for a validated plan whose steps stay inside its integration target (the server enforces
 * this too, together with the mode ceiling, the kill switch, the operator token and idempotency).
 */
export function PlanPipeline({ plan, planMode, planError, target, status, execution, executionActive }: Props) {
  const { view } = execution;
  const alreadyExecuted = view.workflowId !== null && view.workflowId === plan?.workflow_id;
  const outside = plan ? stepsOutsideTarget(plan) : [];
  const targetMismatch = Boolean(plan?.integration_target && plan.integration_target !== target);
  const approvable = plan?.status === "PLANNED" && outside.length === 0 && !targetMismatch;
  const approvalTarget =
    plan && plan.integration_target === "drive_notion_teams"
      ? "drive_notion_teams"
      : plan && plan.integration_target === "teams"
        ? "teams"
        : plan && (isDrivePlan(plan) || plan.integration_target === "google_drive")
          ? "google_drive"
          : "notion";
  const sandboxPlan = plan && isSandboxPlan(plan) ? plan : null;

  return (
    <>
      {planError && <Alert title="Planning request failed">{planError}</Alert>}
      {plan && <PlanReview plan={plan} mode={planMode} />}
      {plan?.status === "PLANNED" && !approvable && (
        <Alert title="This plan cannot be approved">
          {targetMismatch
            ? `It was generated for ${TARGET_LABELS[plan.integration_target ?? "multi"]}, but ${TARGET_LABELS[target]} is selected.`
            : `It contains actions outside ${TARGET_LABELS[plan.integration_target ?? "multi"]}.`}
        </Alert>
      )}
      {plan && approvable && (
        <ApprovalPanel
          key={plan.workflow_id}
          mode={planMode}
          stepCount={plan.planned_steps.length}
          destinationAlias={
            sandboxPlan?.destination_alias ??
            (isOrchestrationPlan(plan)
              ? plan.destinations.notion_parent
              : plan.integration_target === "google_drive"
                ? (status?.google_drive_sandbox?.folder_alias ?? null)
                : plan.integration_target === "teams"
                  ? (status?.teams_sandbox?.channel_alias ?? null)
                  : null)
          }
          teamsChannel={isOrchestrationPlan(plan) ? plan.destinations.teams_channel : null}
          driveCustom={approvalTarget === "google_drive" && (!isDrivePlan(plan) || isDocumentPlan(plan))}
          pageCount={plan.planned_steps.length}
          target={approvalTarget}
          planTarget={plan.integration_target ?? null}
          steps={plan.integration_target ? plan.planned_steps : []}
          busy={executionActive || alreadyExecuted}
          starting={execution.starting}
          error={execution.startError}
          onApprove={(approvedBy, operatorToken) => void execution.execute(plan, approvedBy, planMode, operatorToken)}
        />
      )}
      {view.workflowStatus !== "idle" && (
        <ExecutionMonitor
          view={view}
          connection={execution.connection}
          onReconnect={execution.reconnect}
          onCheckResult={execution.refreshReport}
        />
      )}
      {execution.reportState !== "idle" && (
        <ExecutionReport
          report={execution.report}
          state={execution.reportState}
          error={execution.reportError}
          terminal={view.terminal}
          onRetry={execution.refreshReport}
        />
      )}
    </>
  );
}
