import type {
  DocumentLibraryResponse,
  DocumentPlanResponse,
  DocumentPreview,
  DocumentRequest,
  DriveConnectivity,
  DriveSandboxPlanResponse,
  ExecutionRequest,
  ExecutionResponse,
  ExecutionStartResponse,
  ExecutionMode,
  FastApiErrorBody,
  IntegrationsStatus,
  NotionConnectivity,
  OrchestrationInput,
  OrchestrationPlanResponse,
  OrchestrationSourcesResponse,
  PlanRequest,
  PlanResponse,
  ProjectPlan,
  ProjectPlanResponse,
  SandboxPlanResponse,
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
  // Phase 7.2 source errors: {"code", "message"} (codes and messages only, never ids or URLs).
  if (detail && typeof detail === "object" && !Array.isArray(detail) && typeof (detail as { message?: unknown }).message === "string") {
    const d = detail as { code?: string; message: string };
    return d.code ? `${d.code}: ${d.message}` : d.message;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((d) => `${d.loc.filter((p) => p !== "body").join(".") || "request"}: ${d.msg}`)
      .join("; ");
  }
  return fallback;
}

async function send(
  path: string,
  init?: RequestInit,
  extraHeaders: Record<string, string> = {},
): Promise<{ status: number; body: unknown }> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...extraHeaders,
      },
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

/** Header carrying the operator token for real-mode writes (verified server-side). */
export const AUTHORIZATION_HEADER = "X-Integration-Authorization";

/**
 * POST /api/v1/workflows/executions -> 202 with stream URL and token. ``operatorToken`` is sent
 * only for real mode, as the X-Integration-Authorization header; it is never stored or logged.
 */
export async function startExecution(request: ExecutionRequest, operatorToken?: string): Promise<ExecutionStartResponse> {
  const headers: Record<string, string> =
    request.approval.mode === "real" && operatorToken ? { [AUTHORIZATION_HEADER]: operatorToken } : {};
  const { status, body } = await send(
    "/api/v1/workflows/executions",
    { method: "POST", body: JSON.stringify(request) },
    headers,
  );
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

/** POST /api/v1/workflows/notion-sandbox/plan: server-built Notion-only plan. Never executes. */
export async function planNotionSandbox(mode: ExecutionMode): Promise<SandboxPlanResponse> {
  const { status, body } = await send("/api/v1/workflows/notion-sandbox/plan", {
    method: "POST",
    body: JSON.stringify({ mode, target: "notion" }),
  });
  if (status === 200 && isPlanResponse(body)) return body as SandboxPlanResponse;
  throw new ApiError(status, detailMessage(body, `Could not plan the Notion sandbox test (HTTP ${status}).`), body);
}

/**
 * POST /api/v1/workflows/drive-sandbox/plan: server-built Google Drive plan (run folder, verified
 * report upload, verified read-back; no AI planner). ``project`` (an AI project plan) only changes the report text. Never
 * executes. PLANNED (200) and INVALID (422) resolve; 409/503 throw.
 */
export async function planDriveSandbox(mode: ExecutionMode, project?: ProjectPlan | null): Promise<DriveSandboxPlanResponse> {
  const { status, body } = await send("/api/v1/workflows/drive-sandbox/plan", {
    method: "POST",
    body: JSON.stringify(project ? { mode, target: "google_drive", project } : { mode, target: "google_drive" }),
  });
  if ([200, 422].includes(status) && isPlanResponse(body)) return body as DriveSandboxPlanResponse;
  throw new ApiError(status, detailMessage(body, `Could not plan the Google Drive sandbox test (HTTP ${status}).`), body);
}

/** POST /api/v1/integrations/google-drive/connectivity: READ-ONLY Drive probe (token, sandbox, listing). */
export async function checkDriveConnectivity(): Promise<DriveConnectivity> {
  const { status, body } = await send("/api/v1/integrations/google-drive/connectivity", { method: "POST" });
  if (status !== 200) throw new ApiError(status, detailMessage(body, `Drive connectivity check failed (HTTP ${status}).`), body);
  return body as DriveConnectivity;
}

/**
 * POST /api/v1/workflows/notion-project/plan: AI (Claude) project plan for Notion. Never
 * executes. PLANNED (200), INVALID (422) and FAILED (502) resolve; 409/503 and body errors throw.
 */
export async function planNotionProject(instruction: string, mode: ExecutionMode): Promise<ProjectPlanResponse> {
  const { status, body } = await send("/api/v1/workflows/notion-project/plan", {
    method: "POST",
    body: JSON.stringify({ instruction: instruction.trim(), mode, target: "notion" }),
  });
  if ([200, 422, 502].includes(status) && isPlanResponse(body)) return body as ProjectPlanResponse;
  throw new ApiError(status, detailMessage(body, `Could not plan the Notion project (HTTP ${status}).`), body);
}

/** GET /api/v1/workflows/orchestration/sources: candidate source documents (Phase 7.2). Read-only. */
export async function listOrchestrationSources(mode: ExecutionMode): Promise<OrchestrationSourcesResponse> {
  const { status, body } = await send(`/api/v1/workflows/orchestration/sources?mode=${encodeURIComponent(mode)}`);
  if (status !== 200) throw new ApiError(status, detailMessage(body, `Could not list source documents (HTTP ${status}).`), body);
  return body as OrchestrationSourcesResponse;
}

/**
 * POST /api/v1/workflows/orchestration/plan: read the selected source (read-only), extract the
 * phases (Groq with Gemini fallback) and compile the Drive -> Notion -> Teams plan. Never writes.
 * PLANNED (200), INVALID (422 with a plan body) and FAILED (502) resolve; other errors throw.
 */
export async function planOrchestration(input: OrchestrationInput, mode: ExecutionMode): Promise<OrchestrationPlanResponse> {
  const instructions = input.instructions?.trim() || null;
  const { status, body } = await send("/api/v1/workflows/orchestration/plan", {
    method: "POST",
    // Only the inputs actually provided are sent (the server derives the input mode from them).
    body: JSON.stringify({
      ...(input.sourceFileId ? { source_file_id: input.sourceFileId } : {}),
      ...(instructions ? { instruction: instructions } : {}),
      mode,
      expected_phases: input.expectedPhases,
    }),
  });
  if ([200, 422, 502].includes(status) && isPlanResponse(body)) return body as OrchestrationPlanResponse;
  throw new ApiError(status, detailMessage(body, `Could not plan the workflow (HTTP ${status}).`), body);
}

/**
 * POST /api/v1/workflows/documents/plan: AI document draft (Groq, Gemini fallback) + Markdown preview +
 * a two-step Drive sandbox plan. Never writes. PLANNED (200), INVALID (422) and FAILED (502) resolve.
 */
export async function planDocument(request: DocumentRequest, mode: ExecutionMode): Promise<DocumentPlanResponse> {
  const project = request.projectName?.trim();
  const { status, body } = await send("/api/v1/workflows/documents/plan", {
    method: "POST",
    body: JSON.stringify({
      document_type: request.documentType,
      title: request.title.trim(),
      ...(project ? { project_name: project } : {}),
      instruction: request.instruction.trim(),
      mode,
      target: "google_drive",
    }),
  });
  if ([200, 422, 502].includes(status) && isPlanResponse(body)) return body as DocumentPlanResponse;
  throw new ApiError(status, detailMessage(body, `Could not generate the document (HTTP ${status}).`), body);
}

/** GET /api/v1/workflows/documents/library: documents in the approved Drive sandbox (read-only). */
export async function listDocumentLibrary(mode: ExecutionMode): Promise<DocumentLibraryResponse> {
  const { status, body } = await send(`/api/v1/workflows/documents/library?mode=${encodeURIComponent(mode)}`);
  if (status !== 200) throw new ApiError(status, detailMessage(body, `Could not list sandbox documents (HTTP ${status}).`), body);
  return body as DocumentLibraryResponse;
}

/** GET /api/v1/workflows/documents/library/{file_id}: read-only preview of one sandbox document. */
export async function previewDocument(fileId: string, mode: ExecutionMode): Promise<DocumentPreview> {
  const { status, body } = await send(
    `/api/v1/workflows/documents/library/${encodeURIComponent(fileId)}?mode=${encodeURIComponent(mode)}`,
  );
  if (status !== 200) throw new ApiError(status, detailMessage(body, `Could not preview the document (HTTP ${status}).`), body);
  return body as DocumentPreview;
}

/** GET /api/v1/integrations/status: modes, write switches and destination aliases (no secrets). */
export async function getIntegrationStatus(): Promise<IntegrationsStatus> {
  const { status, body } = await send("/api/v1/integrations/status");
  if (status !== 200) throw new ApiError(status, detailMessage(body, `Could not load integration status (HTTP ${status}).`), body);
  return body as IntegrationsStatus;
}

/** POST /api/v1/integrations/notion/connectivity: READ-ONLY Notion MCP probe. */
export async function checkNotionConnectivity(): Promise<NotionConnectivity> {
  const { status, body } = await send("/api/v1/integrations/notion/connectivity", { method: "POST" });
  if (status !== 200) throw new ApiError(status, detailMessage(body, `Notion connectivity check failed (HTTP ${status}).`), body);
  return body as NotionConnectivity;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Unexpected error. See the browser console for details.";
}
