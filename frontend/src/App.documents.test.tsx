import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { DocumentLibraryResponse, DocumentPlanResponse, DocumentPreview, ExecutionMode, IntegrationsStatus } from "./api/types";
import { validateDocumentRequest } from "./components/documents/DocumentStudio";
import { documentBlockedReason } from "./App";
import {
  CONNECTIVITY_FAIL,
  CONNECTIVITY_URL,
  DEFAULT_ROUTES,
  DRIVE_CONNECTIVITY_PASS,
  DRIVE_CONNECTIVITY_URL,
  DRIVE_READY,
  FakeEventSource,
  STARTED,
  STATUS_REAL_AUTO,
  STATUS_URL,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  type MockRoute,
} from "./test/doubles";
import { renderApp } from "./test/renderApp";

// Synthetic data only; fetch and EventSource are fakes. Nothing is read from or written to Google Drive.
const PLAN_URL = "/api/v1/workflows/documents/plan";
const LIBRARY_URL = "/api/v1/workflows/documents/library";
const EXECUTIONS = "/api/v1/workflows/executions";
const WF = "0d0c0000-0000-4000-9000-00000000d0c5";
const DIGEST = "a".repeat(64);
const FILE = "Customer Rewards Requirements - 2026-11-08 100000.md";
const CONTENT = [
  "# Customer Rewards Requirements",
  "",
  "_Document type: Requirements | Project: Customer Rewards Platform_",
  "",
  "Requirements for the synthetic rewards platform.",
  "",
  "## Functional requirements",
  "",
  "- Customer registration",
  "- Points redemption <script>alert(1)</script>",
  "",
  "---",
  "AI-generated draft by WorkflowWeaver.",
].join("\n");

/** Groq primary, Gemini fallback (the default auto order), with a ready Drive sandbox. */
const STATUS: IntegrationsStatus = {
  ...STATUS_REAL_AUTO,
  google_drive_sandbox: DRIVE_READY,
  planner: { ...STATUS_REAL_AUTO.planner!, model: "openai/gpt-oss-120b", primary: "groq", fallback: "gemini", fallback_model: "gemini-test-flash" },
};

function documentPlan(mode: ExecutionMode, overrides: Partial<DocumentPlanResponse> = {}): DocumentPlanResponse {
  const folder: Record<string, string> = mode === "mock" ? {} : { folder: "sandbox" };
  return {
    workflow_id: WF,
    status: "PLANNED",
    planner: "groq",
    planner_model: "openai/gpt-oss-120b",
    primary_planner: "groq",
    fallback_used: false,
    summary: "Requirements: upload the document to the Google Drive sandbox and read it back to verify it.",
    planned_steps: [
      { step_number: 1, integration: "google_drive", action: "create_document", description: "Upload the reviewed requirements as a Markdown file and verify it",
        arguments: { title: FILE, content: CONTENT, ...folder }, depends_on: [] },
      { step_number: 2, integration: "google_drive", action: "read_document", description: "Read the uploaded document back to confirm it (read-only)",
        arguments: { document_name: FILE, ...folder }, depends_on: [1] },
    ],
    validation: { is_valid: true, issues: [] },
    errors: [],
    planning_attempts: 1,
    integration_target: "google_drive",
    executed: false,
    mode,
    model: "openai/gpt-oss-120b",
    ai_generated: true,
    document_type: "requirements",
    document_label: "Requirements",
    project_name: "Customer Rewards Platform",
    document: { title: "Customer Rewards Requirements", summary: "Requirements for the synthetic rewards platform.", sections: [] },
    file_name: FILE,
    content: CONTENT,
    content_chars: CONTENT.length,
    destination_alias: mode === "mock" ? null : "sandbox",
    operation: mode === "real" ? "Drive REST API (drive.file scope): upload the Markdown file into sandbox folder 'sandbox'" : "In-memory mock upload and read-back (no network)",
    rollback: "If the read-back fails, the uploaded file is moved to Drive trash.",
    real_write: mode === "real",
    plan_digest: DIGEST,
    ...overrides,
  };
}

const LIBRARY: DocumentLibraryResponse = {
  mode: "mock", simulated: true, folder_alias: "sandbox",
  scope: "Markdown and plain-text files directly inside the approved Google Drive sandbox folder.",
  documents: [
    { file_id: "mock-src-crp-plan-v2", name: "Customer Rewards Plan.md", mime_type: "text/markdown", size_bytes: 2048, modified_time: "2026-11-05T10:00:00Z", duplicate_name: false },
    { file_id: "mock-src-notes-0001", name: "Kickoff notes.md", mime_type: "text/markdown", size_bytes: 512, modified_time: "2026-11-04T10:00:00Z", duplicate_name: false },
  ],
};

const PREVIEW: DocumentPreview = {
  mode: "mock", simulated: true, file_id: "mock-src-notes-0001", name: "Kickoff notes.md", mime_type: "text/markdown",
  size_bytes: 512, modified_time: null, sha256: "b".repeat(64),
  content: "# Kickoff notes\n\n- Pilot region agreed\n", checks: ["Synthetic source fixture (mock mode never contacts Google Drive)"],
};

function setup(routes: MockRoute[] = [], status: IntegrationsStatus = STATUS) {
  const state = installFetch([
    ...DEFAULT_ROUTES,
    { path: STATUS_URL, body: status },
    { method: "POST", path: CONNECTIVITY_URL, body: CONNECTIVITY_FAIL },
    { method: "POST", path: DRIVE_CONNECTIVITY_URL, body: DRIVE_CONNECTIVITY_PASS },
    { path: /\/api\/v1\/workflows\/documents\/library\?mode=/, body: LIBRARY },
    ...routes,
  ]);
  const user = userEvent.setup();
  renderApp("documents");
  return { user, ...state };
}

const radio = (name: string) => screen.getByRole("radio", { name });
const studio = () => screen.getByRole("region", { name: "Create a document" });

async function fillAndGenerate(user: ReturnType<typeof userEvent.setup>, { title = "Customer Rewards Requirements", project = "Customer Rewards Platform" } = {}) {
  const form = studio();
  await user.click(within(form).getByRole("radio", { name: "API Specification" }));
  await user.click(within(form).getByRole("radio", { name: "Requirements" }));
  if (title) await user.type(within(form).getByLabelText("Document title"), title);
  if (project) await user.type(within(form).getByLabelText(/Project name/), project);
  await user.type(within(form).getByLabelText("What should the document cover?"), "Registration, balance lookup and redemption.");
  await user.click(within(form).getByRole("button", { name: "Generate Document Preview" }));
}

describe("Document Management: generation, preview and approval", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("offers every document type and states the sandbox-only scope", async () => {
    setup();
    const types = within(studio()).getByRole("radiogroup", { name: "Document type" });
    expect(within(types).getAllByRole("radio").map((r) => r.parentElement?.textContent)).toEqual([
      "Requirements", "Technical Design", "Implementation Plan", "API Specification", "README", "Meeting Notes", "Custom",
    ]);
    expect(screen.getByText(/never your whole Google Drive/)).toBeInTheDocument();
    expect(await within(studio()).findByText(/Groq \(Gemini fallback\) drafts the document/)).toBeInTheDocument();
  });

  it("generates a preview without writing anything, then approves exactly the reviewed document", async () => {
    const { user, calls } = setup([
      { method: "POST", path: PLAN_URL, body: documentPlan("mock") },
      { method: "POST", path: EXECUTIONS, status: 202, body: { ...STARTED, workflow_id: WF, mode: "mock",
        stream_url: `/api/v1/workflows/executions/${WF}/events?access_token=t`, result_url: `/api/v1/workflows/executions/${WF}` } },
    ]);
    await fillAndGenerate(user);
    const review = await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === PLAN_URL)?.body).toEqual({
      document_type: "requirements", title: "Customer Rewards Requirements", project_name: "Customer Rewards Platform",
      instruction: "Registration, balance lookup and redemption.", mode: "mock", target: "google_drive",
    });
    // Preview: rendered headings and bullets; markup in the content is shown as text, never executed.
    const preview = within(review).getByRole("article", { name: "Generated document preview" });
    expect(within(preview).getByRole("heading", { name: "Functional requirements" })).toBeInTheDocument();
    expect(within(preview).getByText("Points redemption <script>alert(1)</script>")).toBeInTheDocument();
    expect(preview.querySelector("script")).toBeNull();
    expect(within(review).getAllByText(FILE).length).toBeGreaterThan(0);
    expect(within(review).getByText("AI-generated draft")).toBeInTheDocument();
    expect(calls.some((c) => c.url === EXECUTIONS)).toBe(false); // generating never executes

    const approval = screen.getByRole("region", { name: "3. Approve mock execution" });
    expect(within(within(approval).getByRole("list", { name: "Actions to approve" })).getAllByRole("listitem").map((li) => li.textContent))
      .toEqual(["Google Drive: Create document", "Google Drive: Read document"]);
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(within(approval).getByRole("checkbox"));
    await user.click(within(approval).getByRole("button", { name: "Approve and Execute Mock Workflow" }));
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const body = calls.find((c) => c.url === EXECUTIONS)?.body as {
      approval: Record<string, unknown>; plan: { integration_target: string; steps: { arguments: Record<string, string> }[] };
    };
    expect(body.approval).toMatchObject({ approved: true, plan_digest: DIGEST, integration_target: "google_drive", mode: "mock" });
    expect(body.plan.integration_target).toBe("google_drive");
    expect(body.plan.steps[0]?.arguments.content).toBe(CONTENT);
  });

  it("real mode requires the Drive phrase, approver and operator token for the sandbox destination", async () => {
    const { user, calls } = setup([{ method: "POST", path: PLAN_URL, body: documentPlan("real") }]);
    await waitFor(() => expect(radio("Real")).toBeEnabled());
    await user.click(radio("Real"));
    await fillAndGenerate(user);
    const approval = await screen.findByRole("region", { name: "3. Approve REAL Google Drive write" });
    expect(within(approval).getByText("This writes to your real Google Drive")).toBeInTheDocument();
    expect(approval.textContent).toMatch(/only inside the\s+sandbox folder sandbox/);
    const button = within(approval).getByRole("button", { name: "Run Plan on Google Drive" });
    await user.type(within(approval).getByLabelText("Approved by"), "synthetic.reviewer");
    await user.click(within(approval).getByRole("checkbox"));
    expect(button).toBeDisabled(); // phrase and operator token still missing
    await user.type(within(approval).getByLabelText(/to confirm/), "UPLOAD TO DRIVE");
    expect(button).toBeDisabled(); // the operator token is still required
    await user.type(within(approval).getByLabelText("Operator token"), "synthetic-operator-token");
    expect(button).toBeEnabled();
    expect(calls.some((c) => c.url === EXECUTIONS)).toBe(false);
  });

  it("says when the AI rewrote an over-long first draft, and shows the size against the limit", async () => {
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: documentPlan("mock", { compression_retry: true, initial_chars: 4883 }) }]);
    await fillAndGenerate(user);
    const review = await screen.findByRole("region", { name: "2. Review the plan" });
    expect(within(review).getByRole("note")).toHaveTextContent(
      "The first draft was 4883 characters, above the 4800-character limit, so the AI rewrote it once more concisely.");
    expect(within(review).getByText(`${CONTENT.length} of 4800 characters (Markdown)`)).toBeInTheDocument();
  });

  it("shows the actual length when the document is still too long after the retry", async () => {
    const message = "The document is 4950 characters after one automatic compression retry (first draft: 4883 characters); the limit is 4800. Ask for a shorter document or fewer sections.";
    const { user } = setup([{ method: "POST", path: PLAN_URL, status: 422, body: documentPlan("mock", {
      status: "INVALID", planned_steps: [], plan_digest: null, compression_retry: true, initial_chars: 4883,
      validation: { is_valid: false, issues: [{ code: "DOCUMENT_TOO_LONG", message, step_number: 1, field: "content" }] },
    }) }]);
    await fillAndGenerate(user);
    expect(await screen.findByText("DOCUMENT_TOO_LONG")).toBeInTheDocument();
    expect(screen.getByText(new RegExp("The document is 4950 characters after one automatic compression retry"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });

  it("shows a rejected document with its issues and offers no approval", async () => {
    const { user } = setup([{ method: "POST", path: PLAN_URL, status: 422, body: documentPlan("mock", {
      status: "INVALID", planned_steps: [], plan_digest: null,
      validation: { is_valid: false, issues: [{ code: "URL_NOT_ALLOWED", message: "sections[0].body contains a URL; describe links in words instead.", step_number: 1, field: "sections[0].body" }] },
    }) }]);
    await fillAndGenerate(user);
    expect(await screen.findByText(/The plan was rejected by the validator/)).toBeInTheDocument();
    expect(screen.getByText("URL_NOT_ALLOWED")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Generated document preview" })).toBeInTheDocument(); // still reviewable
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });

  it("validates the form before sending anything", async () => {
    const { user, calls } = setup();
    await fillAndGenerate(user, { title: "", project: "" });
    expect(await within(studio()).findByText(/Enter a document title/)).toBeInTheDocument();
    expect(calls.some((c) => c.url === PLAN_URL)).toBe(false);
    expect(validateDocumentRequest({ documentType: "custom", title: "Doc", instruction: "too short" })).toMatch(/at least 10/);
    expect(validateDocumentRequest({ documentType: "custom", title: "Doc", instruction: "x".repeat(4001) })).toMatch(/4000/);
  });

  it("blocks generation when the sandbox or the AI planner is not ready, and says why", () => {
    expect(documentBlockedReason("mock", null, null)).toBeNull();
    expect(documentBlockedReason("dry_run", { ...STATUS, google_drive_sandbox: { ...DRIVE_READY, dry_run_ready: false, issues: ["SMOKE_TEST_DRIVE_FOLDER is not set."] } }, null))
      .toBe("SMOKE_TEST_DRIVE_FOLDER is not set.");
    expect(documentBlockedReason("real", STATUS, null)).toMatch(/Drive connectivity check/);
    expect(documentBlockedReason("real", { ...STATUS, planner: { ...STATUS.planner!, ai_ready: false, issue: "GROQ_API_KEY is not set." } }, DRIVE_CONNECTIVITY_PASS))
      .toMatch(/AI planner.*GROQ_API_KEY/);
    expect(documentBlockedReason("real", STATUS, DRIVE_CONNECTIVITY_PASS)).toBeNull();
  });
});

describe("Document Management: sandbox library", () => {
  it("lists only sandbox documents and previews one read-only", async () => {
    const { user, calls } = setup([{ path: /\/api\/v1\/workflows\/documents\/library\/mock-src-notes-0001\?mode=mock/, body: PREVIEW }]);
    const list = await screen.findByRole("list", { name: "Sandbox documents" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText(/It cannot browse the rest of your Google Drive/)).toBeInTheDocument();
    expect(screen.getByText(/never contacts Google Drive/)).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "Preview Kickoff notes.md" }));
    const preview = await screen.findByRole("article", { name: "Preview of Kickoff notes.md" });
    expect(within(preview).getByText("Pilot region agreed")).toBeInTheDocument();
    expect(calls.filter((c) => c.method !== "GET" && c.url.startsWith("/api/v1/workflows"))).toEqual([]); // read-only
    expect(calls.some((c) => c.url === `${LIBRARY_URL}?mode=mock`)).toBe(true);
  });

  it("shows a server refusal (document outside the sandbox) without inventing content", async () => {
    const { user } = setup([{ path: /\/api\/v1\/workflows\/documents\/library\/mock-src-notes-0001/, status: 422,
      body: { detail: { code: "SOURCE_NOT_IN_SANDBOX", message: "The document is not inside the approved Drive sandbox folder." } } }]);
    const list = await screen.findByRole("list", { name: "Sandbox documents" });
    await user.click(within(list).getByRole("button", { name: "Preview Kickoff notes.md" }));
    expect(await screen.findByText(/SOURCE_NOT_IN_SANDBOX: The document is not inside the approved Drive sandbox folder/)).toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "Preview of Kickoff notes.md" })).not.toBeInTheDocument();
  });

  it("reloads the list for the selected mode", async () => {
    const { user, calls } = setup([], { ...STATUS });
    await screen.findByRole("list", { name: "Sandbox documents" });
    await waitFor(() => expect(radio("Dry Run")).toBeEnabled());
    await user.click(radio("Dry Run"));
    await waitFor(() => expect(calls.some((c) => c.url === `${LIBRARY_URL}?mode=dry_run`)).toBe(true));
  });
});
