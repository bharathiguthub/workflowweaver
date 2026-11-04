import type { ExecutionRequest, PlanRequest, PlanResponse } from "../api/types";

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

export function toPlanRequest(instruction: string, name: string): PlanRequest {
  const trimmedName = name.trim();
  return trimmedName ? { instruction: instruction.trim(), name: trimmedName } : { instruction: instruction.trim() };
}

/** The validated plan exactly as returned by /plan, plus explicit mock approval. */
export function toExecutionRequest(plan: PlanResponse, approvedBy: string): ExecutionRequest {
  return {
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
    approval: { approved: true, mode: "mock", approved_by: approvedBy.trim() },
  };
}
