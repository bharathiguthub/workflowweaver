import type {
  ExecutionRequest,
  ExecutionResponse,
  ExecutionStartResponse,
  FastApiErrorBody,
  PlanRequest,
  PlanResponse,
  WorkflowTemplate,
} from "./types";

/** "" = same origin (Vite dev proxy). Otherwise an absolute backend URL without trailing slash. */
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");

export const apiUrl = (path: string): string => `${API_BASE_URL}${path}`;

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** Network failure (backend down, proxy error, CORS): status 0. */
export class NetworkError extends ApiError {
  constructor() {
    super(0, "Cannot reach the WorkflowWeaver API. Is the backend running on port 8000?");
    this.name = "NetworkError";
  }
}

function detailMessage(body: unknown, fallback: string): string {
  const detail = (body as Partial<FastApiErrorBody> | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((d) => `${d.loc.filter((p) => p !== "body").join(".") || "request"}: ${d.msg}`)
      .join("; ");
  }
  return fallback;
}

async function send(path: string, init?: RequestInit): Promise<{ status: number; body: unknown }> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}) },
    });
  } catch {
    throw new NetworkError();
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  return { status: response.status, body };
}

const isPlanResponse = (body: unknown): body is PlanResponse =>
  typeof body === "object" && body !== null && "status" in body && "planned_steps" in body;

export async function getHealth(): Promise<{ status: string; service: string; version: string }> {
  const { status, body } = await send("/health");
  if (status !== 200) throw new ApiError(status, `Health check failed (HTTP ${status}).`, body);
  return body as { status: string; service: string; version: string };
}

export async function getTemplates(): Promise<WorkflowTemplate[]> {
  const { status, body } = await send("/api/v1/workflows/templates");
  if (status !== 200) throw new ApiError(status, detailMessage(body, `Could not load templates (HTTP ${status}).`), body);
  return body as WorkflowTemplate[];
}

/**
 * POST /api/v1/workflows/plan. PLANNED (200), INVALID (422) and FAILED (502) all return a
 * PlanResponse and are resolved, not thrown. A 422 FastAPI body error and 503 are thrown.
 */
export async function planWorkflow(request: PlanRequest): Promise<PlanResponse> {
  const { status, body } = await send("/api/v1/workflows/plan", { method: "POST", body: JSON.stringify(request) });
  if ([200, 422, 502].includes(status) && isPlanResponse(body)) return body;
  throw new ApiError(status, detailMessage(body, `Planning request failed (HTTP ${status}).`), body);
}

/** POST /api/v1/workflows/executions -> 202 with stream URL and token. */
export async function startExecution(request: ExecutionRequest): Promise<ExecutionStartResponse> {
  const { status, body } = await send("/api/v1/workflows/executions", { method: "POST", body: JSON.stringify(request) });
  if (status === 202) return body as ExecutionStartResponse;
  throw new ApiError(status, detailMessage(body, `Could not start execution (HTTP ${status}).`), body);
}

/** GET result_url. null when the backend has no stored result (404: rejected or unknown). */
export async function getExecutionResult(resultUrl: string): Promise<ExecutionResponse | null> {
  const { status, body } = await send(resultUrl);
  if (status === 200) return body as ExecutionResponse;
  if (status === 404) return null;
  throw new ApiError(status, detailMessage(body, `Could not load the execution report (HTTP ${status}).`), body);
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Unexpected error. See the browser console for details.";
}
