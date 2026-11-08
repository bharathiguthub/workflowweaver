import { useEffect, useRef } from "react";

import { SECTIONS, sectionHash, type Section, type SectionId } from "../../navigation";
import { Icon } from "./Icon";

const GROUPS: { id: Section["group"]; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "workspaces", label: "Workspaces" },
  { id: "operations", label: "Operations" },
];

interface Props {
  active: SectionId;
  /** Section whose workflow is running (shown with a live indicator). */
  running: SectionId | null;
  onNavigate: (id: SectionId) => void;
  /** Small screens: the navigation drawer is open. */
  mobileOpen: boolean;
  onCloseMobile: () => void;
  /** Shown at the bottom of the sidebar, e.g. the current mode. */
  footer?: React.ReactNode;
}

function NavLinks({ active, running, onNavigate }: Pick<Props, "active" | "running" | "onNavigate">) {
  return (
    <div className="space-y-6">
      {GROUPS.map((g) => (
        <div key={g.id}>
          <p className="px-3 text-[11px] font-semibold uppercase tracking-wider text-charcoal-400">{g.label}</p>
          <ul className="mt-2 space-y-0.5">
            {SECTIONS.filter((s) => s.group === g.id).map((s) => {
              const current = s.id === active;
              return (
                <li key={s.id}>
                  <a
                    href={sectionHash(s.id)}
                    aria-current={current ? "page" : undefined}
                    onClick={(e) => {
                      e.preventDefault();
                      onNavigate(s.id);
                    }}
                    className={`group relative flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500 ${
                      current ? "bg-charcoal-800 text-white" : "text-charcoal-300 hover:bg-charcoal-800/60 hover:text-white"
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`absolute inset-y-1 left-0 w-1 rounded-r ${current ? "bg-brand-600" : "bg-transparent"}`}
                    />
                    <Icon name={s.icon} className={`h-5 w-5 shrink-0 ${current ? "text-brand-400" : "text-charcoal-400 group-hover:text-charcoal-200"}`} />
                    <span>{s.label}</span>
                    {running === s.id && (
                      <span className="ml-auto inline-flex h-2 w-2 rounded-full bg-brand-500 motion-safe:animate-pulse" title="Workflow running">
                        <span className="sr-only">(workflow running)</span>
                      </span>
                    )}
                  </a>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

/** Primary navigation: a charcoal sidebar on large screens, a slide-in drawer on small ones. */
export function Sidebar({ active, running, onNavigate, mobileOpen, onCloseMobile, footer }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!mobileOpen) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseMobile();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobileOpen, onCloseMobile]);

  const navigateAndClose = (id: SectionId) => {
    onNavigate(id);
    onCloseMobile();
  };

  return (
    <>
      <aside className="hidden w-64 shrink-0 bg-charcoal-900 lg:block">
        <div className="sticky top-[4.25rem] flex h-[calc(100vh-4.25rem)] flex-col overflow-y-auto">
          <nav aria-label="Main" className="flex-1 px-3 py-6">
            <NavLinks active={active} running={running} onNavigate={onNavigate} />
          </nav>
          {footer && <div className="border-t border-charcoal-800 px-6 py-4">{footer}</div>}
        </div>
      </aside>

      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" id="mobile-navigation">
          <div aria-hidden="true" className="absolute inset-0 bg-charcoal-950/50" onClick={onCloseMobile} />
          <div role="dialog" aria-modal="true" aria-label="Navigation menu"
            className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-charcoal-900 shadow-xl">
            <div className="flex items-center justify-between border-b border-charcoal-800 px-4 py-3">
              <span className="text-sm font-semibold text-white">WorkflowWeaver</span>
              <button ref={closeRef} type="button" onClick={onCloseMobile} aria-label="Close navigation menu"
                className="rounded-md p-2 text-charcoal-300 hover:bg-charcoal-800 hover:text-white focus-visible:outline-2 focus-visible:outline-brand-500">
                <Icon name="close" className="h-5 w-5" />
              </button>
            </div>
            <nav aria-label="Sections" className="flex-1 overflow-y-auto px-3 py-5">
              <NavLinks active={active} running={running} onNavigate={navigateAndClose} />
            </nav>
            {footer && <div className="border-t border-charcoal-800 px-6 py-4">{footer}</div>}
          </div>
        </div>
      )}
    </>
  );
}
