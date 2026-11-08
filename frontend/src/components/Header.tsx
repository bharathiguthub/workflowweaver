import bitwiseLogo from "../../branding/bitwise-logo.svg";
import type { ExecutionMode } from "../api/types";
import { Icon } from "./layout/Icon";
import { Badge } from "./StatusBadge";

export type ApiHealth = { state: "checking" } | { state: "ok"; version: string } | { state: "down" };

const MODE_BADGE = {
  mock: {
    tone: "warning",
    label: "Mock mode: no real Notion, Drive or Teams calls",
    title: "Notion, Google Drive and Teams are simulated in memory. No real service is called.",
  },
  dry_run: {
    tone: "info",
    label: "Dry run: requests are built, nothing is sent",
    title: "Real adapters validate and build requests without opening any connection.",
  },
  real: {
    tone: "danger",
    label: "Real mode: sandbox writes after approval",
    title: "Approved runs write to the approved Notion, Google Drive and Teams sandboxes.",
  },
} as const;

interface Props {
  health: ApiHealth;
  mode?: ExecutionMode;
  /** Small screens: opens the navigation drawer. */
  onOpenMenu?: () => void;
  menuOpen?: boolean;
}

/** Application header: Bitwise logo, product name, execution mode and API status. */
export function Header({ health, mode = "mock", onOpenMenu, menuOpen = false }: Props) {
  const badge = MODE_BADGE[mode];
  return (
    <header className="sticky top-0 z-30 border-b border-charcoal-200 bg-white">
      <div aria-hidden="true" className="h-1 bg-brand-600" />
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 sm:px-6 lg:px-8">
        <div className="flex min-w-0 items-center gap-3">
          {onOpenMenu && (
            <button
              type="button"
              onClick={onOpenMenu}
              aria-label="Open navigation menu"
              aria-expanded={menuOpen}
              aria-controls="mobile-navigation"
              className="-ml-1 rounded-lg p-2 text-charcoal-700 hover:bg-charcoal-100 focus-visible:outline-2 focus-visible:outline-brand-600 lg:hidden"
            >
              <Icon name="menu" className="h-6 w-6" />
            </button>
          )}
          <img src={bitwiseLogo} alt="Bitwise" width={181} height={70} className="h-9 w-auto shrink-0 sm:h-10" />
          <span aria-hidden="true" className="hidden h-8 w-px bg-charcoal-200 sm:block" />
          <div className="min-w-0">
            <h1 className="truncate text-lg font-semibold tracking-tight text-charcoal-900">WorkflowWeaver</h1>
            <p className="hidden text-xs text-charcoal-500 sm:block">AI-planned business workflows, approved by you</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span title={badge.title}>
            <Badge tone={badge.tone} label={badge.label} />
          </span>
          {health.state === "checking" && <Badge tone="neutral" label="API: checking…" />}
          {health.state === "ok" && <Badge tone="success" label={`API connected · v${health.version}`} />}
          {health.state === "down" && <Badge tone="danger" label="API unreachable" />}
        </div>
      </div>
    </header>
  );
}
