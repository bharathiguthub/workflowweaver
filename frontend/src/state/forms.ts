import type { ExecutionMode, ExecutionRequest, IntegrationTarget, PlanRequest, PlanResponse } from "../api/types";

// Limits mirror the backend Pydantic models (PlanRequest, ExecutionApproval).
export const INSTRUCTION_MIN = 10;
export const INSTRUCTION_MAX = 5000;
export const NAME_MIN = 3;
export const NAME_MAX = 120;
export const APPROVER_MIN = 3;
export const APPROVER_MAX = 80;

export function validateInstruction(value: string): string | null {
  const length = value.trim().length;
  if (length === 0) return "Describe the workflow you want to run.";
  if (length < INSTRUCTION_MIN) return `Use at least ${INSTRUCTION_MIN} characters.`;
  if (length > INSTRUCTION_MAX) return `Use at most ${INSTRUCTION_MAX} characters (currently ${length}).`;
  return null;
}

export function validateName(value: string): string | null {
  const length = value.trim().length;
  if (length === 0) return null; // optional
  if (length < NAME_MIN) return `Use at least ${NAME_MIN} characters, or leave it empty.`;
  if (length > NAME_MAX) return `Use at most ${NAME_MAX} characters.`;
  return null;
}

export function validateApprover(value: string): string | null {
  const length = value.trim().length;
  if (length < APPROVER_MIN) return `Enter at least ${APPROVER_MIN} characters.`;
  if (length > APPROVER_MAX) return `Use at most ${APPROVER_MAX} characters.`;
  return null;
}

/** Typed exactly by the approver before a real Notion page can be created. */
export const REAL_CONFIRMATION_PHRASE = "CREATE NOTION PAGE";
/** Typed before a real Google Drive upload (Phase 6.1). */
/** Typed to approve a REAL Teams post (Phase 7.1). Messages cannot be unsent. */
export const TEAMS_CONFIRMATION_PHRASE = "SEND TEAMS MESSAGE";

export const DRIVE_CONFIRMATION_PHRASE = "UPLOAD TO DRIVE";

export function validateRealConfirmation(value: string, phrase: string = REAL_CONFIRMATION_PHRASE): string | null {
  return value.trim() === phrase ? null : `Type ${phrase} exactly.`;
}

export function toPlanRequest(
  instruction: string,
  name: string,
  mode: ExecutionMode = "mock",
  target?: IntegrationTarget,
): PlanRequest {
  const trimmedName = name.trim();
  const base: PlanRequest = trimmedName
    ? { instruction: instruction.trim(), name: trimmedName, mode }
    : { instruction: instruction.trim(), mode };
  return target ? { ...base, target } : base;
}

/** The validated plan exactly as returned by the server, plus explicit approval for ``mode``. */
export function toExecutionRequest(plan: PlanResponse, approvedBy: string, mode: ExecutionMode = "mock"): ExecutionRequest {
  const request: ExecutionRequest = {
    workflow_id: plan.workflow_id,
    plan: {
      summary: plan.summary ?? "",
      steps: plan.planned_steps.map((s) => ({
        step_number: s.step_number,
        integration: s.integration,
        action: s.action,
        description: s.description,
        arguments: { ...s.arguments },
        depends_on: [...s.depends_on],
      })),
    },
    approval: { approved: true, mode, approved_by: approvedBy.trim() },
  };
  // The server rejects the run unless the approved target equals the plan's target.
  const target = plan.integration_target;
  const bound = target
    ? { ...request, plan: { ...request.plan, integration_target: target }, approval: { ...request.approval, integration_target: target } }
    : request;
  // Phase 7.2: the approval names the exact reviewed plan (server-recorded digest).
  const digest = (plan as { plan_digest?: unknown }).plan_digest;
  return typeof digest === "string" && digest ? { ...bound, approval: { ...bound.approval, plan_digest: digest } } : bound;
}
