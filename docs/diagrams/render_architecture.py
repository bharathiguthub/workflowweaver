"""Renders docs/architecture.png (WorkflowWeaver, Phase 4 as implemented).

Diagram source of truth. Needs only matplotlib (tested with 3.11.2); it does not import
the application. Regenerate from the repo root:

    py -3.11 -m venv .docvenv
    .\\.docvenv\\Scripts\\python.exe -m pip install matplotlib==3.11.2
    .\\.docvenv\\Scripts\\python.exe docs\\diagrams\\render_architecture.py

(.docvenv is a throwaway environment; do not add matplotlib to backend/requirements.txt.)
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "architecture.png"

# palette
INK = "#1e293b"
MUTED = "#64748b"
LINE = "#94a3b8"
C = {
    "browser": ("#eef2ff", "#6366f1"),
    "api": ("#f8fafc", "#475569"),
    "router": ("#ffffff", "#64748b"),
    "plan": ("#ecfdf5", "#059669"),
    "exec": ("#eff6ff", "#2563eb"),
    "stream": ("#fff7ed", "#ea580c"),
    "tools": ("#fefce8", "#ca8a04"),
    "obs": ("#faf5ff", "#9333ea"),
    "ext": ("#ffffff", "#94a3b8"),
}

fig, ax = plt.subplots(figsize=(18, 11.5), dpi=150)
ax.set_xlim(0, 18)
ax.set_ylim(0, 11.5)
ax.axis("off")
fig.patch.set_facecolor("white")


def box(x, y, w, h, kind, title=None, lines=(), *, title_size=10.5, size=8.6, dashed=False, radius=0.12, z=2):
    face, edge = C[kind]
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}", linewidth=1.4,
            edgecolor=edge, facecolor=face, linestyle=(0, (4, 3)) if dashed else "solid", zorder=z,
        )
    )
    ty = y + h - 0.22
    if title:
        ax.text(x + 0.14, ty, title, fontsize=title_size, fontweight="bold", color=edge if kind != "api" else INK,
                va="top", ha="left", zorder=z + 1)
        ty -= 0.32
    for line in lines:
        ax.text(x + 0.14, ty, line, fontsize=size, color=INK, va="top", ha="left", zorder=z + 1)
        ty -= 0.255
    return (x, y, w, h)


def chip(x, y, w, h, text, kind, size=8.4):
    face, edge = C[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.08", linewidth=1,
                                edgecolor=edge, facecolor="white", zorder=4))
    ax.text(x + w / 2, y + h / 2, text, fontsize=size, color=INK, ha="center", va="center", zorder=5)


def arrow(p, q, label=None, *, color=LINE, dashed=False, rad=0.0, lx=0.0, ly=0.0, both=False, size=8):
    ax.add_patch(
        FancyArrowPatch(p, q, arrowstyle="<|-|>" if both else "-|>", mutation_scale=12, linewidth=1.3, color=color,
                        linestyle=(0, (4, 3)) if dashed else "solid", connectionstyle=f"arc3,rad={rad}", zorder=6)
    )
    if label:
        mx, my = (p[0] + q[0]) / 2 + lx, (p[1] + q[1]) / 2 + ly
        ax.text(mx, my, label, fontsize=size, color=color if color != LINE else MUTED, ha="center", va="center",
                zorder=7, bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.9))


# ---------------------------------------------------------------- title
ax.text(0.3, 11.25, "WorkflowWeaver architecture (Phase 4, as implemented)", fontsize=16, fontweight="bold",
        color=INK, va="top")
ax.text(0.3, 10.85, "Natural language -> validated plan -> explicit approval -> mock execution with retries, "
        "Saga rollback, live SSE progress and Langfuse traces.  Mock tools only: no real Notion, Drive or Teams calls.",
        fontsize=9.5, color=MUTED, va="top")

# ---------------------------------------------------------------- browser
box(0.3, 9.25, 13.2, 1.3, "browser", "Browser: React 19 + TypeScript + Tailwind CSS 4  (Vite dev server 127.0.0.1:5173)")
chips = ["Templates + instruction", "Plan review", "Approve (mock)", "Live timeline (EventSource)", "Execution report",
         "Recent runs (tab only)"]
cx = 0.45
for label in chips:
    w = 0.25 + 0.083 * len(label)
    chip(cx, 9.38, w, 0.42, label, "browser")
    cx += w + 0.14

box(4.6, 8.35, 4.6, 0.6, "browser", None,
    ["Vite dev proxy: /api, /health -> :8000 (same origin, no CORS)"], size=8.6)
arrow((6.9, 9.25), (6.9, 8.95), color=C["browser"][1])

# ---------------------------------------------------------------- FastAPI container
box(0.3, 0.35, 13.2, 7.75, "api", "FastAPI backend  (127.0.0.1:8000)", radius=0.18, z=1)
arrow((6.9, 8.35), (6.9, 8.12), color=C["browser"][1])

routers = [
    (0.5, "Phase 1", "GET /templates\nPOST /workflows"),
    (3.6, "Planning", "POST /plan"),
    (6.7, "Sync execution", "POST /execute\nGET /executions/{id}"),
    (9.8, "Streaming (mock-only token)", "POST /executions  (202)\nGET /executions/{id}/events"),
]
for x, t, body in routers:
    box(x, 6.45, 3.5 if x == 9.8 else 3.0, 1.05, "router", t, body.split("\n"), title_size=9, size=8.3)

# planning column
box(0.5, 3.15, 3.9, 3.15, "plan", "Planning  (PlanningService)", [
    "LangGraph: planner -> validator ->",
    "  reporter | error   (bounded planner retry)",
    "Planner: MockPlanner | ClaudePlanner",
    "  structured output, catalog in prompt",
    "Validator: allow-list, args, deps,",
    "  unsafe content, step limit,",
    "  DOCUMENT_NOT_FOUND (mock catalog",
    "  or created by an earlier step)",
    "Output: PlanResponse (executed=false)",
])
arrow((4.6, 6.45), (3.6, 6.3), color=C["plan"][1])

# execution column
box(4.65, 3.15, 4.75, 3.15, "exec", "Execution  (ExecutionService)", [
    "Idempotency: workflow_id + plan fingerprint",
    "  replay / 409 conflict / per-key lock",
    "LangGraph: prepare -> execute_step",
    "  <-> retry_wait -> compensate -> finalize",
    "prepare: re-validate + approval + doc check",
    "Bounded retries: transient only, backoff",
    "Saga journal -> reverse compensation",
    "Reconcile orphan effects by idempotency key",
    "Status: COMPLETED | FAILED | ROLLED_BACK | ...",
])
arrow((7.9, 6.45), (7.6, 6.3), color=C["exec"][1])

# runner
box(9.65, 4.95, 3.35, 1.35, "stream", "ExecutionRunner", [
    "Runs ExecutionService.execute() on a",
    "thread pool: graph + retry sleeps never",
    "block the event loop. Per-run publisher.",
])
arrow((11.15, 6.45), (11.15, 6.3), color=C["stream"][1])
arrow((9.65, 5.6), (9.4, 5.6), color=C["stream"][1])

# broker
box(9.65, 3.15, 3.65, 1.65, "stream", "EventBroker  (in-memory)", [
    "<- emit() at each graph-node transition",
    "Per-workflow ordered history (event_id)",
    "256-bit capability token; Last-Event-ID",
    "Keep-alive, retention, 204 when done",
])
arrow((9.4, 4.3), (9.65, 4.3), color=C["stream"][1])
arrow((13.16, 4.8), (13.16, 6.45), color=C["stream"][1])
ax.text(13.2, 5.65, "SSE", fontsize=8, color=C["stream"][1], ha="left", rotation=90, va="center")

# tools
box(4.65, 1.45, 8.65, 1.45, "tools", "MockToolRegistry  (idempotency key = workflow_id:step-n)", [
    "Notion: create_page, create_task (reversible)      Google Drive: create_document (reversible), read_document",
    "Teams: send_message (irreversible)      In-memory MockToolStore      Synthetic document catalog",
    "FailureInjector: transient, timeout-after-write, permanent, permission, malformed (tests only)",
])
arrow((7.0, 3.15), (7.0, 2.9), "tool calls / compensations", color=C["tools"][1], lx=1.35)

# observability strip
box(0.5, 0.5, 12.8, 0.8, "obs", None, [
    "Cross-cutting:  traced_node on every graph node  -  tool / compensation / LLM spans  -  "
    "sanitize(): no prompts, argument values, contents or keys leave the process",
    "SSE token redaction in access/request logs  -  CORS: exact origins only  -  Settings from env / .env (secrets as SecretStr)",
], size=8.4)
box(0.5, 1.45, 3.9, 1.45, "obs", "Telemetry facade", [
    "NoopTelemetry (default) |",
    "LangfuseTelemetry (failure-isolated)",
    "session_id = workflow_id",
])

# ---------------------------------------------------------------- external services
box(14.0, 6.35, 3.7, 1.5, "ext", "Anthropic API (Claude)", [
    "Called by ClaudePlanner", "Optional: PLANNER_MODE=claude", "Structured outputs, max_retries=0"], dashed=True)
arrow((13.5, 7.1), (14.0, 7.1), color=C["plan"][1], dashed=True)

box(14.0, 4.45, 3.7, 1.5, "ext", "Langfuse", [
    "Exported by LangfuseTelemetry", "Optional: LANGFUSE_ENABLED", "Background OTel export"], dashed=True)
arrow((13.5, 5.2), (14.0, 5.2), color=C["obs"][1], dashed=True)

box(14.0, 2.05, 3.7, 2.05, "ext", "Real Notion / Google Drive /", [
    "Microsoft Teams", "NOT called in Phase 4.", "Real adapters must implement the", "same ToolAdapter contract (Phase 5)."],
    dashed=True)
ax.plot([13.3, 14.0], [2.2, 2.9], color=LINE, linestyle=":", linewidth=1.3, zorder=6)
ax.text(13.62, 2.42, "x", fontsize=12, color="#dc2626", ha="center", va="center", fontweight="bold", zorder=7)

# legend
ax.text(14.0, 1.75, "Legend", fontsize=9.5, fontweight="bold", color=INK)
for i, (k, label) in enumerate([("plan", "planning"), ("exec", "execution"), ("stream", "streaming / SSE"),
                                ("tools", "mock tools"), ("obs", "observability")]):
    y = 1.45 - i * 0.24
    ax.add_patch(FancyBboxPatch((14.0, y - 0.07), 0.3, 0.16, boxstyle="round,pad=0,rounding_size=0.03",
                                fc=C[k][0], ec=C[k][1], lw=1.1))
    ax.text(14.42, y, label, fontsize=8.4, color=INK, va="center")
ax.text(15.9, 1.45, "- - -  optional /", fontsize=8.4, color=MUTED, va="center")
ax.text(15.9, 1.21, "        external", fontsize=8.4, color=MUTED, va="center")

fig.savefig(OUT, bbox_inches="tight", facecolor="white")
print(f"wrote {OUT}")
