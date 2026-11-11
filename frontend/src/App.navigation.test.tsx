import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import App from "./App";
import type { IntegrationsStatus } from "./api/types";
import { SECTIONS, sectionFromHash } from "./navigation";
import { findUnsupported } from "./state/capabilities";
import {
  DEFAULT_ROUTES,
  DRIVE_READY,
  PLANNED,
  STATUS_MOCK,
  STATUS_REAL_AUTO,
  STATUS_URL,
  installFakeEventSource,
  installFetch,
  resetEventIds,
  type MockRoute,
} from "./test/doubles";
import { navLink, renderApp } from "./test/renderApp";

const PLAN_URL = "/api/v1/workflows/plan";

const STATUS_AUTO: IntegrationsStatus = {
  ...STATUS_REAL_AUTO,
  google_drive_sandbox: DRIVE_READY,
  planner: { ...STATUS_REAL_AUTO.planner!, model: "openai/gpt-oss-120b", primary: "groq", fallback: "gemini", fallback_model: "gemini-test-flash" },
};

function setup(routes: MockRoute[] = [], where: Parameters<typeof renderApp>[0] = "dashboard") {
  const state = installFetch([...DEFAULT_ROUTES, ...routes]);
  const user = userEvent.setup();
  renderApp(where);
  return { user, ...state };
}

describe("business-capability navigation", () => {
  beforeEach(() => {
    installFakeEventSource();
    resetEventIds();
  });

  it("opens on the Dashboard with every business section in the sidebar", () => {
    setup();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Dashboard", "Document Management", "Project Management", "Team Communication", "Business Workflows",
      "AI Workflow Builder", "Execution History", "Monitoring & Observability", "Integrations & Settings",
    ]);
    expect(navLink("Dashboard")).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { level: 2, name: "Dashboard" })).toBeInTheDocument();
    // Small screens: the same sections in a drawer opened from the header.
    expect(screen.queryByRole("navigation", { name: "Sections" })).not.toBeInTheDocument();
  });

  it("opens the mobile navigation drawer, navigates, closes on Escape and returns focus", async () => {
    const { user } = setup();
    const menu = screen.getByRole("button", { name: "Open navigation menu" });
    expect(menu).toHaveAttribute("aria-expanded", "false");
    await user.click(menu);
    const drawer = screen.getByRole("dialog", { name: "Navigation menu" });
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(within(drawer).getByRole("button", { name: "Close navigation menu" })).toHaveFocus();
    const sections = within(drawer).getByRole("navigation", { name: "Sections" });
    expect(within(sections).getAllByRole("link")).toHaveLength(SECTIONS.length);
    await user.click(within(sections).getByRole("link", { name: "Team Communication" }));
    expect(screen.queryByRole("dialog", { name: "Navigation menu" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Team Communication" })).toBeInTheDocument();
    await user.click(menu);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Navigation menu" })).not.toBeInTheDocument();
    expect(menu).toHaveFocus();
  });

  it("shows the Bitwise logo and the product name in the header", () => {
    setup();
    const banner = screen.getByRole("banner");
    expect(within(banner).getByRole("img", { name: "Bitwise" })).toHaveAttribute("src", expect.stringContaining("bitwise-logo"));
    expect(within(banner).getByRole("heading", { level: 1, name: "WorkflowWeaver" })).toBeInTheDocument();
  });

  it("navigates from the sidebar and updates the address for deep links", async () => {
    const { user } = setup();
    await user.click(navLink("Project Management"));
    expect(screen.getByRole("heading", { level: 2, name: "Project Management" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "AI Project Plan to Notion" })).toBeInTheDocument();
    expect(navLink("Project Management")).toHaveAttribute("aria-current", "page");
    expect(window.location.hash).toBe("#/projects");
  });

  it("opens the section named in the URL hash", () => {
    installFetch(DEFAULT_ROUTES);
    window.history.replaceState(null, "", "/#/history");
    render(<App />);
    expect(screen.getByRole("heading", { level: 2, name: "Execution History" })).toBeInTheDocument();
    expect(sectionFromHash("#/settings")).toBe("settings");
    expect(sectionFromHash("#/unknown")).toBeNull();
  });

  it("shows business capability cards with readiness and opens them", async () => {
    const { user } = setup([{ path: STATUS_URL, body: STATUS_AUTO }]);
    const workspaces = screen.getByRole("region", { name: "Workspaces" });
    const cards = await within(workspaces).findAllByRole("button", { name: /^Open / });
    expect(cards.map((b) => b.getAttribute("aria-label"))).toEqual([
      "Open Document Management", "Open Project Management", "Open Team Communication", "Open Business Workflows", "Open AI Workflow Builder",
    ]);
    expect(await screen.findByText("Groq (Gemini fallback)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open Document Management" }));
    expect(screen.getByRole("region", { name: "Create a document" })).toBeInTheDocument();
  });

  it("keeps a plan with its section while you look elsewhere", async () => {
    const { user } = setup([{ method: "POST", path: PLAN_URL, body: PLANNED }], "Multi-Integration");
    await user.click(await screen.findByRole("button", { name: /Meeting-to-Tasks/ }));
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    await user.click(navLink("Team Communication"));
    expect(screen.queryByRole("region", { name: "2. Review the plan" })).not.toBeInTheDocument();
    await user.click(navLink("AI Workflow Builder"));
    expect(screen.getByRole("region", { name: "2. Review the plan" })).toBeInTheDocument();
  });
});

describe("AI Workflow Builder", () => {
  it("has a prominent instruction box, lists supported actions and never offers free-form cross-tool planning", () => {
    setup([], "builder");
    expect(screen.getByRole("heading", { name: "Describe what you want to automate" })).toBeInTheDocument();
    expect(screen.getByLabelText("Instruction")).toHaveAttribute("rows", "7");
    expect(within(screen.getByRole("list", { name: "Google Drive supported actions" })).getAllByRole("listitem")).toHaveLength(4);
    expect(within(screen.getByRole("list", { name: "Microsoft Teams supported actions" })).getByText("Send message")).toBeInTheDocument();
    const unsupported = screen.getByRole("list", { name: "Unsupported integrations and actions" });
    expect(within(unsupported).getByText("Jira")).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "Google Drive + Notion + Teams" })).not.toBeInTheDocument();
    expect(screen.getByText(/cannot chain arbitrary tools/)).toBeInTheDocument();
  });

  it("flags unsupported integrations and actions while you type", async () => {
    const { user } = setup([], "builder");
    expect(screen.queryByRole("status", { name: "Unsupported request" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Instruction"), "Create a Jira ticket, email the team and delete the old notes.");
    const hint = screen.getByRole("status", { name: "Unsupported request" });
    expect(hint).toHaveTextContent("Jira is not a supported integration.");
    expect(hint).toHaveTextContent("Email (Gmail / Outlook) is not a supported integration.");
    expect(hint).toHaveTextContent("Deleting files, pages or messages is not a supported action.");
    expect(findUnsupported("Save a summary document to Google Drive.")).toEqual([]);
  });

  it("plans with the selected target through the existing planning API", async () => {
    const { user, calls } = setup([{ method: "POST", path: PLAN_URL, body: PLANNED }], "builder");
    await user.click(screen.getByRole("radio", { name: "Notion Only" }));
    await user.type(screen.getByLabelText("Instruction"), "Create a Notion page summarising the rewards pilot.");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    await screen.findByRole("region", { name: "2. Review the plan" });
    expect(calls.find((c) => c.url === PLAN_URL)?.body).toEqual({
      instruction: "Create a Notion page summarising the rewards pilot.", mode: "mock", target: "notion" });
  });
});

describe("Team Communication", () => {
  it("explains why Teams is unavailable instead of offering a form", async () => {
    setup([{ path: STATUS_URL, body: STATUS_MOCK }], "communication");
    expect(await screen.findByText("Microsoft Teams is not available in this mode")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Generate Plan" })).not.toBeInTheDocument();
  });

  it("plans Teams-only notifications for the approved channel", async () => {
    const status: IntegrationsStatus = {
      ...STATUS_MOCK,
      integrations: { ...STATUS_MOCK.integrations, teams: { transport: "webhook", configured: true, destinations: {} } },
      teams_sandbox: { transport: "webhook", channel_alias: "sandbox", sandbox_only: true, mock_ready: true, dry_run_ready: true, real_ready: false, issues: [] },
    };
    const { user, calls } = setup([{ path: STATUS_URL, body: status }, { method: "POST", path: PLAN_URL, body: { ...PLANNED, integration_target: "teams" } }], "communication");
    const input = await screen.findByLabelText("Instruction");
    expect(screen.getByText(/only post messages \(send_message\) to the sandbox channel 'sandbox'/)).toBeInTheDocument();
    await user.type(input, "Tell the team the rewards pilot starts next sprint.");
    await user.click(screen.getByRole("button", { name: "Generate Plan" }));
    await waitFor(() => expect(calls.find((c) => c.url === PLAN_URL)?.body).toMatchObject({ target: "teams", mode: "mock" }));
  });
});

describe("Execution History and Settings", () => {
  it("says history is session-local and lost on reload or restart", () => {
    setup([], "history");
    expect(screen.getByText("History is kept in this browser tab only")).toBeInTheDocument();
    expect(screen.getByText(/restarting the WorkflowWeaver server loses this list/)).toBeInTheDocument();
    expect(screen.getByText("No runs yet.")).toBeInTheDocument();
  });

  it("shows the planner order and the fixed drive.file scope without any secret", async () => {
    setup([{ path: STATUS_URL, body: STATUS_AUTO }], "settings");
    const planner = await screen.findByRole("region", { name: "AI planner" });
    await waitFor(() => expect(within(planner).getByText("Groq (Gemini fallback)")).toBeInTheDocument());
    expect(within(planner).getByText("openai/gpt-oss-120b")).toBeInTheDocument();
    const drive = screen.getByRole("region", { name: "Google Drive sandbox" });
    expect(within(drive).getByText(/drive\.file \(fixed; only files this app creates\)/)).toBeInTheDocument();
    expect(screen.getByText("Configured (never shown)")).toBeInTheDocument();
  });
});
