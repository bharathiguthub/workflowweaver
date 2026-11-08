import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CompensationResult, ExecutionMode, ExecutionResponse, StepResult } from "../api/types";
import { ExecutionReport, resourceKind } from "./ExecutionReport";

// Synthetic ids and URLs only; nothing here talks to Notion or Google Drive.
const NOTION_PAGE_ID = "5e1f0000-0000-4000-9000-0000000000a1";
const NOTION_URL = "https://www.notion.so/5e1f00000000400090000000000000a1";
const FOLDER_ID = "1FolderSyntheticAAAAAAAAAAAAAAAAA";
const FOLDER_URL = `https://drive.google.com/drive/folders/${FOLDER_ID}`;
const FILE_ID = "1FileSyntheticBBBBBBBBBBBBBBBBBBBB";
const FILE_URL = `https://docs.google.com/document/d/${FILE_ID}/edit`;

const NOTION_NOTE = /This page exists in your Notion sandbox and was not rolled back/;
const DRIVE_NOTE = "Created in your approved Google Drive sandbox.";
const READ_ONLY_NOTE = /Read-only step: an existing item was read; nothing was created/;

function step(n: number, integration: string, action: string, resourceId: string | null, output: Record<string, unknown> = {},
  status: StepResult["status"] = "SUCCEEDED"): StepResult {
  return { step_number: n, integration, action, status, attempts: 1, resource_id: resourceId, output, replayed: false, error: null };
}

function report(mode: ExecutionMode, steps: StepResult[], compensation: CompensationResult[] = []): ExecutionResponse {
  return {
    workflow_id: "0d6e1c2b-3a4f-4b5c-8d7e-9f0a1b2c3d4e",
    status: compensation.length ? "ROLLED_BACK" : "COMPLETED",
    mode,
    validation: { is_valid: true, issues: [] },
    step_results: steps,
    attempts: Object.fromEntries(steps.map((s) => [String(s.step_number), 1])),
    retry_delays_seconds: [],
    errors: [],
    compensation_results: compensation,
    replayed: false,
    message: "Synthetic report.",
  };
}

type Items = [HTMLElement, HTMLElement, HTMLElement];

/** Renders the report and returns its resource-reference items (asserted to exist). */
function renderReport(r: ExecutionResponse): Items {
  render(<ExecutionReport report={r} state="loaded" error={null} terminal={null} onRetry={() => {}} />);
  const items = screen
    .getAllByRole("listitem")
    .filter((li) => li.closest("ul")?.getAttribute("aria-label") === "Resource references");
  expect(items).toHaveLength(r.step_results.filter((s) => s.status === "SUCCEEDED" && s.resource_id).length);
  return items as unknown as Items;
}

const driveRun = (mode: ExecutionMode) =>
  report(mode, [
    step(1, "google_drive", "create_folder", FOLDER_ID, { name: "Project Alpha", destination: "sandbox", url: FOLDER_URL }),
    step(2, "google_drive", "create_document", FILE_ID, { title: "Project Alpha report.md", destination: "folder from step 1", url: FILE_URL }),
    step(3, "google_drive", "read_document", FILE_ID, { title: "Project Alpha report.md", url: FILE_URL }),
  ]);

describe("execution report resource labels: Google Drive", () => {
  it("labels a created folder 'Folder ID' and a created document 'File ID', never 'Page ID' or Notion text", () => {
    const [folder, doc] = renderReport(driveRun("real"));
    expect(within(folder).getByText("Folder ID")).toBeInTheDocument();
    expect(within(folder).getByText(FOLDER_ID)).toBeInTheDocument();
    expect(within(doc).getByText("File ID")).toBeInTheDocument();
    expect(within(doc).getByText(FILE_ID)).toBeInTheDocument();
    expect(screen.queryByText("Page ID")).not.toBeInTheDocument();
    expect(screen.queryByText(/Notion/)).not.toBeInTheDocument();
    expect(screen.getByText("Created in the real service")).toBeInTheDocument();
  });

  it("says 'Created in your approved Google Drive sandbox' only for the created resources", () => {
    const [folder, doc, read] = renderReport(driveRun("real"));
    expect(within(folder).getByText(DRIVE_NOTE)).toBeInTheDocument();
    expect(within(doc).getByText(DRIVE_NOTE)).toBeInTheDocument();
    expect(within(read).queryByText(DRIVE_NOTE)).not.toBeInTheDocument();
    expect(screen.getAllByText(DRIVE_NOTE)).toHaveLength(2);
  });

  it("shows a read action's File ID without suggesting anything was created", () => {
    const read = renderReport(driveRun("real"))[2];
    expect(within(read).getByText("File ID")).toBeInTheDocument();
    expect(within(read).getByText(READ_ONLY_NOTE)).toBeInTheDocument();
    expect(within(read).queryByText(/rolled back/)).not.toBeInTheDocument();
  });

  it("a real run that only reads does not claim anything was created", () => {
    renderReport(report("real", [step(1, "google_drive", "read_document", FILE_ID, { url: FILE_URL })]));
    expect(screen.queryByText("Created in the real service")).not.toBeInTheDocument();
    expect(screen.getByText("Resource references")).toBeInTheDocument();
    expect(screen.queryByText(DRIVE_NOTE)).not.toBeInTheDocument();
    expect(screen.getByText(READ_ONLY_NOTE)).toBeInTheDocument();
  });

  it("keeps the Google Drive URLs as links", () => {
    const [folder, doc, read] = renderReport(driveRun("real"));
    expect(within(folder).getByRole("link", { name: FOLDER_URL })).toHaveAttribute("href", FOLDER_URL);
    expect(within(doc).getByRole("link", { name: FILE_URL })).toHaveAttribute("href", FILE_URL);
    expect(within(read).getByRole("link", { name: FILE_URL })).toHaveAttribute("href", FILE_URL);
  });

  it("makes no sandbox or rollback claims outside real mode", () => {
    for (const mode of ["mock", "dry_run"] as const) {
      const { unmount } = render(
        <ExecutionReport report={driveRun(mode)} state="loaded" error={null} terminal={null} onRetry={() => {}} />,
      );
      expect(screen.queryByText(DRIVE_NOTE)).not.toBeInTheDocument();
      expect(screen.queryByText(/rolled back/)).not.toBeInTheDocument();
      expect(screen.getByText("Folder ID")).toBeInTheDocument();
      unmount();
    }
  });

  it("a created Drive resource that the report shows as compensated is marked rolled back, without the sandbox note", () => {
    const run = report("real", [step(1, "google_drive", "create_folder", FOLDER_ID, { url: FOLDER_URL })], [
      { step_number: 1, integration: "google_drive", action: "create_folder", resource_id: FOLDER_ID, status: "COMPENSATED", attempts: 1, message: "Effect undone." },
    ]);
    const [folder] = renderReport(run);
    expect(within(folder).getByText("(rolled back)")).toBeInTheDocument();
    expect(within(folder).queryByText(DRIVE_NOTE)).not.toBeInTheDocument();
  });
});

describe("execution report resource labels: Notion (unchanged)", () => {
  const notionPage = () =>
    step(1, "notion", "create_page", NOTION_PAGE_ID, { title: "Customer Rewards Platform", destination: "engineering", url: NOTION_URL });
  const notionRun = (mode: ExecutionMode) => report(mode, [notionPage()]);

  it("keeps 'Page ID' and the Notion sandbox note for a real page that was not rolled back", () => {
    const [pageItem] = renderReport(notionRun("real"));
    expect(within(pageItem).getByText("Page ID")).toBeInTheDocument();
    expect(within(pageItem).getByText(NOTION_PAGE_ID)).toBeInTheDocument();
    expect(within(pageItem).getByText(NOTION_NOTE)).toBeInTheDocument();
    expect(within(pageItem).getByRole("link", { name: NOTION_URL })).toHaveAttribute("href", NOTION_URL);
    expect(screen.queryByText(DRIVE_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByText(READ_ONLY_NOTE)).not.toBeInTheDocument();
  });

  it("keeps 'Resource ID' and no sandbox note outside real mode", () => {
    renderReport(notionRun("mock"));
    expect(screen.getByText("Resource ID")).toBeInTheDocument();
    expect(screen.queryByText(NOTION_NOTE)).not.toBeInTheDocument();
  });

  it("a rolled-back Notion page shows '(rolled back)' and not the 'was not rolled back' note", () => {
    const run = report("real", notionRun("real").step_results, [
      { step_number: 1, integration: "notion", action: "create_page", resource_id: NOTION_PAGE_ID, status: "COMPENSATED", attempts: 1, message: "Moved to trash." },
    ]);
    const [pageItem] = renderReport(run);
    expect(within(pageItem).getByText("(rolled back)")).toBeInTheDocument();
    expect(within(pageItem).queryByText(NOTION_NOTE)).not.toBeInTheDocument();
  });

  it("a mixed real run labels each integration's resource with its own wording", () => {
    const [pageItem, docItem] = renderReport(
      report("real", [
        notionPage(),
        step(2, "google_drive", "create_document", FILE_ID, { url: FILE_URL }),
      ]),
    );
    expect(within(pageItem).getByText("Page ID")).toBeInTheDocument();
    expect(within(pageItem).getByText(NOTION_NOTE)).toBeInTheDocument();
    expect(within(docItem).getByText("File ID")).toBeInTheDocument();
    expect(within(docItem).getByText(DRIVE_NOTE)).toBeInTheDocument();
    expect(within(docItem).queryByText(NOTION_NOTE)).not.toBeInTheDocument();
  });
});

describe("resourceKind", () => {
  it("maps each integration/action to its label", () => {
    expect(resourceKind("google_drive", "create_folder", "real")).toMatchObject({ idLabel: "Folder ID", creates: true, readOnly: false });
    expect(resourceKind("google_drive", "create_document", "real")).toMatchObject({ idLabel: "File ID", creates: true });
    expect(resourceKind("google_drive", "read_document", "real")).toMatchObject({ idLabel: "File ID", creates: false, readOnly: true, keptNote: null });
    expect(resourceKind("google_drive", "list_files", "real")).toMatchObject({ creates: false, readOnly: true, keptNote: null });
    expect(resourceKind("notion", "create_page", "real").idLabel).toBe("Page ID");
    expect(resourceKind("notion", "create_page", "mock").idLabel).toBe("Resource ID");
    // Phase 7.1: Teams messages get their own wording (see App.teams.test.tsx).
    expect(resourceKind("teams", "send_message", "real")).toMatchObject({ idLabel: "Message reference", creates: true, readOnly: false });
    // Other integrations: neutral wording, never read-only or sandbox claims.
    expect(resourceKind("jira", "create_issue", "real")).toMatchObject({ idLabel: "Resource ID", readOnly: false, keptNote: null });
  });
});
