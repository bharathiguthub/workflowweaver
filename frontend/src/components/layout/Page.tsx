import type { ReactNode } from "react";

import type { IconName } from "../../navigation";
import { Icon } from "./Icon";

/*
 * Shared design-system primitives (red / white / charcoal / light gray; tokens in index.css).
 * Every page builds on these so headings, surfaces, buttons, inputs and status pills look the same.
 */

export function PageHeader({ icon, title, description, actions }: { icon: IconName; title: string; description: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4 border-b border-charcoal-200 pb-5">
      <div className="flex items-start gap-3">
        <div aria-hidden="true" className="grid h-11 w-11 shrink-0 place-items-center rounded-lg bg-brand-50 text-brand-600 ring-1 ring-brand-100">
          <Icon name={icon} className="h-6 w-6" />
        </div>
        <div>
          <h2 className="text-xl font-semibold tracking-tight text-charcoal-900 sm:text-2xl">{title}</h2>
          <p className="mt-1 max-w-3xl text-sm text-charcoal-600">{description}</p>
        </div>
      </div>
      {actions}
    </div>
  );
}

/** A white surface card with an optional heading (headings name the region for assistive tech). */
export function Card({ title, subtitle, children, className = "", actions, id }: {
  title?: string;
  subtitle?: ReactNode;
  children?: ReactNode;
  className?: string;
  actions?: ReactNode;
  id?: string;
}) {
  const headingId = id ? `${id}-heading` : undefined;
  return (
    <section aria-labelledby={headingId} className={`rounded-xl border border-charcoal-200 bg-white p-4 shadow-sm sm:p-6 ${className}`}>
      {(title || actions) && (
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            {title && (
              <h3 id={headingId} className="text-base font-semibold text-charcoal-900">
                {title}
              </h3>
            )}
            {subtitle && <div className="mt-1 text-sm text-charcoal-600">{subtitle}</div>}
          </div>
          {actions}
        </div>
      )}
      <div className={title || actions ? "mt-4" : ""}>{children}</div>
    </section>
  );
}

/** One metric. ``value`` must be real data; pass "—" (with a hint) when there is nothing to count yet. */
export function StatTile({ label, value, hint, accent = false }: { label: string; value: string; hint?: ReactNode; accent?: boolean }) {
  return (
    <div className={`rounded-xl border bg-white p-4 shadow-sm ${accent ? "border-brand-200" : "border-charcoal-200"}`}>
      <p className="text-xs font-semibold uppercase tracking-wide text-charcoal-500">{label}</p>
      <p className={`mt-2 text-2xl font-semibold tabular-nums ${accent ? "text-brand-700" : "text-charcoal-900"}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-charcoal-500">{hint}</p>}
    </div>
  );
}

export type Readiness = "ready" | "limited" | "setup";

const READINESS: Record<Readiness, { label: string; className: string }> = {
  ready: { label: "Ready", className: "bg-emerald-50 text-emerald-700 ring-emerald-200" },
  limited: { label: "Limited", className: "bg-amber-50 text-amber-800 ring-amber-200" },
  setup: { label: "Needs setup", className: "bg-charcoal-100 text-charcoal-600 ring-charcoal-300" },
};

export function ReadinessPill({ value, label }: { value: Readiness; label?: string }) {
  const r = READINESS[value];
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${r.className}`}>
      {label ?? r.label}
    </span>
  );
}

/** A small yes/no/unknown indicator with a visible text label (never colour alone). */
export function StatusDot({ state, label }: { state: "ok" | "warn" | "off" | "error"; label: string }) {
  const dot = { ok: "bg-emerald-500", warn: "bg-amber-500", off: "bg-charcoal-300", error: "bg-rose-500" }[state];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-charcoal-700">
      <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-charcoal-300 bg-charcoal-50 px-4 py-6 text-center">
      <p className="text-sm font-medium text-charcoal-800">{title}</p>
      {children && <div className="mt-1 text-sm text-charcoal-600">{children}</div>}
    </div>
  );
}

const focus = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600";

/** Shared button and input styles so every page uses the same controls. */
export const buttonPrimary =
  `inline-flex items-center justify-center gap-2 rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-brand-700 ${focus} disabled:cursor-not-allowed disabled:opacity-50`;
export const buttonSecondary =
  `inline-flex items-center justify-center gap-2 rounded-lg border border-charcoal-300 bg-white px-3 py-2 text-sm font-medium text-charcoal-800 shadow-sm hover:bg-charcoal-50 ${focus} disabled:cursor-not-allowed disabled:opacity-50`;
export const buttonGhost =
  `inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-charcoal-700 hover:bg-charcoal-100 ${focus} disabled:cursor-not-allowed disabled:opacity-50`;
export const inputBase =
  "mt-1 block w-full rounded-lg border border-charcoal-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/30";
