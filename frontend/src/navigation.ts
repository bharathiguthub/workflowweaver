import type { IntegrationTarget } from "./api/types";

/** Business sections (Phase 8.2). Each workflow section owns one integration target. */
export type SectionId =
  | "dashboard"
  | "documents"
  | "projects"
  | "communication"
  | "business"
  | "builder"
  | "history"
  | "settings";

export interface Section {
  id: SectionId;
  label: string;
  /** One line shown under the page title and on dashboard cards. */
  description: string;
  /** Integration target the section plans for (builder: chosen by the user; null: not a workflow page). */
  target: IntegrationTarget | "selectable" | null;
  icon: IconName;
  group: "overview" | "workspaces" | "operations";
}

export type IconName =
  | "home" | "document" | "project" | "chat" | "flow" | "sparkles" | "clock" | "settings"
  | "menu" | "close" | "arrow-right" | "refresh";

export const SECTIONS: readonly Section[] = [
  { id: "dashboard", label: "Dashboard", group: "overview", icon: "home", target: null,
    description: "Readiness, recent runs and shortcuts to every business capability." },
  { id: "documents", label: "Document Management", group: "workspaces", icon: "document", target: "google_drive",
    description: "Draft business and technical documents with AI and store them in the approved Google Drive sandbox." },
  { id: "projects", label: "Project Management", group: "workspaces", icon: "project", target: "notion",
    description: "Turn requirements into Notion project plans and action items." },
  { id: "communication", label: "Team Communication", group: "workspaces", icon: "chat", target: "teams",
    description: "Plan and send notifications to the approved Microsoft Teams channel." },
  { id: "business", label: "Business Workflows", group: "workspaces", icon: "flow", target: "drive_notion_teams",
    description: "Run the Google Drive → Notion → Teams workflow end to end, with one approval." },
  { id: "builder", label: "AI Workflow Builder", group: "workspaces", icon: "sparkles", target: "selectable",
    description: "Describe a workflow in plain language; the AI plans it with the supported integrations only." },
  { id: "history", label: "Execution History", group: "operations", icon: "clock", target: null,
    description: "Runs started from this browser tab." },
  { id: "settings", label: "Integrations & Settings", group: "operations", icon: "settings", target: null,
    description: "Connection status, read-only checks and planner configuration." },
];

export const SECTION_BY_ID: Record<SectionId, Section> = Object.fromEntries(SECTIONS.map((s) => [s.id, s])) as Record<
  SectionId,
  Section
>;

export const isSectionId = (value: string): value is SectionId => value in SECTION_BY_ID;

/** Hash routes (#/documents) keep sections linkable without a router dependency. */
export function sectionFromHash(hash: string): SectionId | null {
  const id = hash.replace(/^#\/?/, "").split(/[/?]/)[0] ?? "";
  return isSectionId(id) ? id : null;
}

export const sectionHash = (id: SectionId): string => `#/${id}`;

/** Integration targets the AI Workflow Builder may select (the Drive + Notion + Teams workflow
 * has its own section and is planned from a source, never free-form). */
export const BUILDER_TARGETS: readonly IntegrationTarget[] = ["google_drive", "notion", "teams", "multi"];
