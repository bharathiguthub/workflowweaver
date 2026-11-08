import type { Integration } from "../api/types";

/** The planner's allow-list (backend/app/agents/catalog.py). Anything else is rejected server-side. */
export const SUPPORTED_ACTIONS: Record<Integration, { action: string; label: string; note: string }[]> = {
  google_drive: [
    { action: "create_folder", label: "Create folder", note: "Inside the approved sandbox folder only" },
    { action: "create_document", label: "Create document", note: "Markdown upload, verified after upload" },
    { action: "list_files", label: "List files", note: "Sandbox folder and folders the run created" },
    { action: "read_document", label: "Read document", note: "Read-only" },
  ],
  notion: [
    { action: "create_page", label: "Create page", note: "Under the approved sandbox parent" },
    { action: "create_task", label: "Create task", note: "Disabled while sandbox confinement is on" },
  ],
  teams: [{ action: "send_message", label: "Send message", note: "Approved sandbox channel only" }],
};

/** Tools people commonly ask for that WorkflowWeaver cannot plan or run. */
export const UNSUPPORTED_TOOLS: { name: string; pattern: RegExp }[] = [
  { name: "Jira", pattern: /\bjira\b/i },
  { name: "Slack", pattern: /\bslack\b/i },
  { name: "Email (Gmail / Outlook)", pattern: /\b(e-?mail|gmail|outlook|inbox)\b/i },
  { name: "Calendar / meetings scheduling", pattern: /\b(calendar|schedule (a|the) meeting|invite)\b/i },
  { name: "GitHub / GitLab", pattern: /\b(github|gitlab|pull request)\b/i },
  { name: "Salesforce / HubSpot (CRM)", pattern: /\b(salesforce|hubspot|crm)\b/i },
  { name: "Trello / Asana / Monday", pattern: /\b(trello|asana|monday\.com)\b/i },
  { name: "SharePoint / OneDrive / Dropbox", pattern: /\b(sharepoint|onedrive|dropbox|box\.com)\b/i },
  { name: "Excel / spreadsheets", pattern: /\b(excel|spreadsheet|google sheets?)\b/i },
  { name: "SMS / phone", pattern: /\b(sms|text message|phone call)\b/i },
];

/** Operations no integration supports (destructive or sharing actions). */
export const UNSUPPORTED_OPERATIONS: { name: string; pattern: RegExp }[] = [
  { name: "Deleting files, pages or messages", pattern: /\b(delete|remove|erase|purge|trash)\b/i },
  { name: "Sharing or changing permissions", pattern: /\b(share with|permissions?|make (it )?public|grant access)\b/i },
  { name: "Editing or moving existing content", pattern: /\b(edit|update|rename|move) (the |an |my )?(existing )?(file|document|page|message)s?\b/i },
];

export interface UnsupportedHit {
  kind: "tool" | "operation";
  name: string;
}

/** Client-side hint only: the server's validator is authoritative and rejects unsupported steps. */
export function findUnsupported(instruction: string): UnsupportedHit[] {
  const text = instruction.trim();
  if (!text) return [];
  return [
    ...UNSUPPORTED_TOOLS.filter((t) => t.pattern.test(text)).map((t) => ({ kind: "tool" as const, name: t.name })),
    ...UNSUPPORTED_OPERATIONS.filter((o) => o.pattern.test(text)).map((o) => ({ kind: "operation" as const, name: o.name })),
  ];
}
