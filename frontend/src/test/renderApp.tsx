import { fireEvent, render, screen, within } from "@testing-library/react";

import App from "../App";
import { isSectionId, type SectionId } from "../navigation";

export type TargetName = "Google Drive Only" | "Notion Only" | "Microsoft Teams Only" | "Multi-Integration";

/**
 * Render the App on a section (Phase 8.2 business navigation). A TargetName opens the AI Workflow
 * Builder with that Integration Target selected (the builder defaults to Google Drive Only).
 */
export function renderApp(where: TargetName | SectionId = "Google Drive Only") {
  window.history.replaceState(null, "", "/"); // no section hash left over from a previous test
  if (isSectionId(where)) return render(<App initialSection={where} />);
  const result = render(<App initialSection="builder" />);
  if (where !== "Google Drive Only") fireEvent.click(screen.getByRole("radio", { name: where }));
  return result;
}

/** The sidebar link to a section (the mobile tab strip duplicates it, so scope to the main nav). */
export function navLink(name: string) {
  return within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: new RegExp(`^${name}`) });
}
