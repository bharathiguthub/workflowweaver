import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  checkDriveConnectivity,
  checkNotionConnectivity,
  errorMessage,
  getHealth,
  getIntegrationStatus,
  getTemplates,
  planDocument,
  planDriveSandbox,
  planNotionProject,
  planNotionSandbox,
  planOrchestration,
  planWorkflow,
} from "./api/client";
import {
  MODE_LABELS,
  isProjectPlan,
  type DocumentRequest,
  type DriveConnectivity,
  type ExecutionMode,
  type IntegrationTarget,
  type IntegrationsStatus,
  type NotionConnectivity,
  type OrchestrationInput,
  type PlanResponse,
  type WorkflowTemplate,
} from "./api/types";
import { CapabilityMatrix, UnsupportedHints } from "./components/CapabilityMatrix";
import { DocumentLibrary } from "./components/documents/DocumentLibrary";
import { DocumentStudio } from "./components/documents/DocumentStudio";
import { GoogleDriveSandboxTest, driveBlockedReason } from "./components/GoogleDriveSandboxTest";
import { Header, type ApiHealth } from "./components/Header";
import { IntegrationReadiness } from "./components/IntegrationReadiness";
import { IntegrationTargetSelector, orchestrationInputAvailability, targetOptions } from "./components/IntegrationTargetSelector";
import { Card, PageHeader } from "./components/layout/Page";
import { Sidebar } from "./components/layout/Sidebar";
import { ModeSelector } from "./components/ModeSelector";
import { NotionProjectWorkflow } from "./components/NotionProjectWorkflow";
import { NotionSandboxTest } from "./components/NotionSandboxTest";
import { OrchestrationWorkflow } from "./components/OrchestrationWorkflow";
import { PlanPipeline } from "./components/PlanPipeline";
import { SessionHistory, type HistoryEntry } from "./components/SessionHistory";
import { StageTracker } from "./components/StageTracker";
import { Alert } from "./components/StatusBadge";
import { WorkflowInput } from "./components/WorkflowInput";
import { BUILDER_TARGETS, SECTION_BY_ID, sectionFromHash, sectionHash, type SectionId } from "./navigation";
import { DashboardPage } from "./pages/DashboardPage";
import { HistoryPage } from "./pages/HistoryPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { plannerSummary } from "./pages/readiness";
import { SettingsPage } from "./pages/SettingsPage";
import { isTerminalStatus } from "./state/execution";
import { toPlanRequest } from "./state/forms";
import { customPlanBlockedReason, modeOptions, projectBlockedReason } from "./state/modes";
import { useExecution } from "./state/useExecution";

const HISTORY_LIMIT = 50;

/** Why a document cannot be generated in ``mode`` (null: it can). The backend re-checks everything. */
export function documentBlockedReason(mode: ExecutionMode, status: IntegrationsStatus | null, drive: DriveConnectivity | null): string | null {
  const reason = driveBlockedReason(mode, status?.google_drive_sandbox, drive);
  if (reason) return reason;
  if (mode === "real" && !(status?.planner?.ai_ready ?? false)) {
    return `Real-mode documents must come from an AI planner (Groq with Gemini fallback): ${status?.planner?.issue ?? "no AI planner is configured."}`;
  }
  return null;
}

function initialSectionFromLocation(fallback: SectionId): SectionId {
  if (typeof window === "undefined") return fallback;
  return sectionFromHash(window.location.hash) ?? fallback;
}

export default function App({ initialSection = "dashboard" }: { initialSection?: SectionId }) {
  const [section, setSection] = useState<SectionId>(() => initialSectionFromLocation(initialSection));
  const [health, setHealth] = useState<ApiHealth>({ state: "checking" });
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [templatesState, setTemplatesState] = useState<"loading" | "loaded" | "error">("loading");
  const [templatesError, setTemplatesError] = useState<string | null>(null);

  const [status, setStatus] = useState<IntegrationsStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [connectivity, setConnectivity] = useState<NotionConnectivity | null>(null);
  const [connectivityError, setConnectivityError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [driveConnectivity, setDriveConnectivity] = useState<DriveConnectivity | null>(null);
  const [driveConnectivityError, setDriveConnectivityError] = useState<string | null>(null);
  const [driveChecking, setDriveChecking] = useState(false);
  const [mode, setMode] = useState<ExecutionMode>("mock");
  // The AI Workflow Builder's own target; the other workspaces have a fixed target.
  const [builderTarget, setBuilderTarget] = useState<IntegrationTarget>("google_drive");

  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [planMode, setPlanMode] = useState<ExecutionMode>("mock"); // the mode the plan was generated for
  const [planName, setPlanName] = useState("");
  const [planSection, setPlanSection] = useState<SectionId | null>(null); // the section the plan belongs to
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [libraryRefresh, setLibraryRefresh] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false); // small screens: navigation drawer
  const closeMenu = useCallback(() => {
    setMenuOpen(false);
    // Return focus to the control that opened the drawer.
    document.querySelector<HTMLElement>('[aria-controls="mobile-navigation"]')?.focus();
  }, []);

  const execution = useExecution();
  const { view, reset } = execution;
  const recorded = useRef<string | null>(null);

  // ---------- navigation (hash routes, no router dependency) ----------
  const navigate = useCallback((id: SectionId) => {
    setSection(id);
    if (typeof window !== "undefined" && window.location.hash !== sectionHash(id)) {
      window.history.pushState(null, "", sectionHash(id));
    }
    if (typeof window !== "undefined") window.scrollTo?.({ top: 0 });
  }, []);
  useEffect(() => {
    const onHash = () => {
      const next = sectionFromHash(window.location.hash);
      if (next) setSection(next);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("popstate", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("popstate", onHash);
    };
  }, []);

  /** READ-ONLY probe: MCP tools/list + retrieve the sandbox parent page. Never writes. */
  const runConnectivityCheck = useCallback(async () => {
    setChecking(true);
    setConnectivityError(null);
    try {
      setConnectivity(await checkNotionConnectivity());
    } catch (error) {
      setConnectivity(null);
      setConnectivityError(errorMessage(error));
    } finally {
      setChecking(false);
    }
  }, []);

  /** READ-ONLY probe: OAuth token refresh, sandbox folder marker, list its files. Never writes. */
  const runDriveCheck = useCallback(async () => {
    setDriveChecking(true);
    setDriveConnectivityError(null);
    try {
      setDriveConnectivity(await checkDriveConnectivity());
    } catch (error) {
      setDriveConnectivity(null);
      setDriveConnectivityError(errorMessage(error));
    } finally {
      setDriveChecking(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    getHealth()
      .then((h) => active && setHealth({ state: "ok", version: h.version }))
      .catch(() => active && setHealth({ state: "down" }));
    getTemplates()
      .then((t) => {
        if (!active) return;
        setTemplates(t);
        setTemplatesState("loaded");
      })
      .catch((error: unknown) => {
        if (!active) return;
        setTemplatesError(errorMessage(error));
        setTemplatesState("error");
      });
    getIntegrationStatus()
      .then((s) => {
        if (!active) return;
        setStatus(s);
        // Real mode is offered only after a passing read-only check, so run it once up front.
        if (s.notion_sandbox.real_ready) void runConnectivityCheck();
        if (s.google_drive_sandbox?.real_ready) void runDriveCheck();
      })
      .catch((error: unknown) => active && setStatusError(errorMessage(error)));
    return () => {
      active = false;
    };
  }, [runConnectivityCheck, runDriveCheck]);

  const options = useMemo(() => modeOptions(status, connectivity, driveConnectivity), [status, connectivity, driveConnectivity]);

  const clearPlan = useCallback(() => {
    reset();
    setPlan(null);
    setPlanError(null);
    setPlanSection(null);
  }, [reset]);

  const executionActive = execution.starting || (view.workflowStatus !== "idle" && !isTerminalStatus(view.workflowStatus));

  // If the selected mode stops being available (e.g. a failed re-check), fall back to mock.
  useEffect(() => {
    if (mode !== "mock" && !options[mode].available && !executionActive) {
      setMode("mock");
      clearPlan();
    }
  }, [options, mode, executionActive, clearPlan]);

  const allTargets = useMemo(() => targetOptions(status, mode), [status, mode]);
  const builderTargets = useMemo(() => allTargets.filter((o) => BUILDER_TARGETS.includes(o.target)), [allTargets]);
  // Teams availability depends on the mode (Phase 7.1): leave the Teams target if it becomes unavailable.
  useEffect(() => {
    if (builderTarget === "teams" && !builderTargets.find((o) => o.target === "teams")?.available && !executionActive) {
      setBuilderTarget("google_drive");
      if (planSection === "builder") clearPlan();
    }
  }, [builderTargets, builderTarget, executionActive, clearPlan, planSection]);

  const changeBuilderTarget = (next: IntegrationTarget) => {
    if (next === builderTarget || !builderTargets.find((o) => o.target === next)?.available) return;
    setBuilderTarget(next);
    if (planSection === "builder") clearPlan(); // a plan for another target can never be approved here
  };

  const changeMode = (next: ExecutionMode) => {
    if (next === mode || !options[next].available) return;
    setMode(next);
    clearPlan();
  };

  // Record each finished run once in the session history (this tab only).
  useEffect(() => {
    if (view.workflowId && isTerminalStatus(view.workflowStatus) && recorded.current !== view.workflowId) {
      recorded.current = view.workflowId;
      const entry: HistoryEntry = {
        workflowId: view.workflowId,
        name: planName || "Untitled workflow",
        status: view.workflowStatus,
        finishedAt: new Date().toISOString(),
        section: planSection ?? undefined,
        mode: planMode,
      };
      setHistory((h) => [entry, ...h].slice(0, HISTORY_LIMIT));
      if (planSection === "documents") setLibraryRefresh((n) => n + 1);
    }
  }, [view.workflowId, view.workflowStatus, planName, planSection, planMode]);

  const runPlanning = useCallback(
    async (request: () => Promise<PlanResponse>, name: string, forMode: ExecutionMode, forSection: SectionId) => {
      reset();
      setPlanning(true);
      setPlanError(null);
      setPlan(null);
      setPlanSection(forSection);
      try {
        const response = await request();
        setPlan(response);
        setPlanMode(forMode);
        setPlanName(name.trim());
      } catch (error) {
        setPlanError(errorMessage(error));
      } finally {
        setPlanning(false);
      }
    },
    [reset],
  );

  const sectionTarget = (id: SectionId): IntegrationTarget =>
    id === "builder" ? builderTarget : ((SECTION_BY_ID[id].target as IntegrationTarget | null) ?? "multi");

  const generatePlan = (forSection: SectionId) => (instruction: string, name: string) =>
    void runPlanning(() => planWorkflow(toPlanRequest(instruction, name, mode, sectionTarget(forSection))), name, mode, forSection);

  const generateSandboxPlan = () => void runPlanning(() => planNotionSandbox(mode), "Notion Sandbox Test", mode, "projects");

  // The AI project plan under review (if any) becomes the uploaded report; otherwise a synthetic one.
  const reviewedProject = plan && isProjectPlan(plan) && plan.status === "PLANNED" ? plan.project : null;
  const generateDrivePlan = () =>
    void runPlanning(() => planDriveSandbox(mode, reviewedProject), "Google Drive Sandbox Test", mode, "documents");

  const generateProjectPlan = (instruction: string) =>
    void runPlanning(() => planNotionProject(instruction, mode), "AI Project Plan", mode, "projects");

  const generateDocument = (request: DocumentRequest) =>
    void runPlanning(() => planDocument(request, mode), `Document: ${request.title.trim()}`, mode, "documents");

  const generateOrchestrationPlan = (input: OrchestrationInput) =>
    void runPlanning(
      () => planOrchestration(input, mode),
      input.sourceFileId ? "Google Drive → Notion → Teams" : "Instructions → Notion → Teams",
      mode,
      "business",
    );

  // Phase 7.2: availability per input from the backend. Real mode also needs the read-only Notion
  // check, and plans with a Drive source the read-only Drive check too.
  const orchestrationBlocked = (input: "document" | "instructions"): string | null => {
    const availability = orchestrationInputAvailability(status, mode, input);
    if (!availability.available) return availability.reason;
    if (mode !== "real") return null;
    if (connectivity?.status !== "PASS") return "Run the read-only Notion connectivity check first.";
    if (input === "document" && driveConnectivity?.status !== "PASS") return "Run the read-only Google Drive connectivity check first.";
    return null;
  };

  const sandboxBlocked =
    mode === "dry_run" && status && !status.notion_sandbox.dry_run_ready
      ? (status.notion_sandbox.issues.find((i) => i.includes("SMOKE_TEST_NOTION_PARENT")) ??
        "Configure SMOKE_TEST_NOTION_PARENT as an approved Notion destination alias.")
      : null;

  const teamsOption = allTargets.find((o) => o.target === "teams");
  const teamsNotice = `Microsoft Teams Only: plans may only post messages (send_message) to the sandbox channel '${
    status?.teams_sandbox?.channel_alias ?? "sandbox"
  }'. ${
    mode === "real"
      ? "Real mode posts through the channel's Workflows webhook after your approval; a posted message cannot be unsent."
      : mode === "dry_run"
        ? "Dry run builds the webhook request without sending it."
        : "Mock mode simulates delivery; nothing is posted."
  }`;

  // ---------- shared page pieces ----------

  const modeSelector = <ModeSelector value={mode} options={options} disabled={executionActive || planning} onChange={changeMode} />;

  const pipelineFor = (id: SectionId) =>
    planSection === id ? (
      <PlanPipeline
        plan={plan}
        planMode={planMode}
        planError={planError}
        target={sectionTarget(id)}
        status={status}
        execution={execution}
        executionActive={executionActive}
      />
    ) : null;

  const notionReadiness = (
    <IntegrationReadiness
      status={status}
      statusError={statusError}
      connectivity={connectivity}
      connectivityError={connectivityError}
      checking={checking}
      onCheck={() => void runConnectivityCheck()}
    />
  );

  /** A workflow page: header, mode, the section's own panels, the plan pipeline, and a status rail. */
  const workspace = (id: SectionId, content: ReactNode, opts: { rail?: boolean } = {}) => {
    const s = SECTION_BY_ID[id];
    const runningElsewhere = executionActive && planSection !== null && planSection !== id;
    return (
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="min-w-0 space-y-6">
          <PageHeader icon={s.icon} title={s.label} description={s.description} />
          {runningElsewhere && planSection && (
            <Alert tone="info" title={`A workflow is running in ${SECTION_BY_ID[planSection].label}`}>
              <button type="button" className="font-semibold underline" onClick={() => navigate(planSection)}>
                Go to the running workflow
              </button>
            </Alert>
          )}
          {modeSelector}
          {content}
          {pipelineFor(id)}
        </div>
        {opts.rail !== false && (
          <aside aria-label="Status" className="space-y-4">
            {notionReadiness}
            <SessionHistory entries={history} />
          </aside>
        )}
      </div>
    );
  };

  const page = (() => {
    switch (section) {
      case "dashboard":
        return (
          <DashboardPage
            status={status}
            statusError={statusError}
            mode={mode}
            history={history}
            onNavigate={navigate}
            notionCheck={{ result: connectivity, error: connectivityError, checking, run: () => void runConnectivityCheck() }}
            driveCheck={{ result: driveConnectivity, error: driveConnectivityError, checking: driveChecking, run: () => void runDriveCheck() }}
          />
        );
      case "documents":
        return workspace(
          "documents",
          <>
            <Card id="documents-scope" className="bg-indigo-50/40">
              <p className="text-sm text-slate-700">
                Documents are stored only in the approved Google Drive sandbox folder
                {status?.google_drive_sandbox?.folder_alias ? (
                  <>
                    {" "}
                    <span className="font-mono text-xs">{status.google_drive_sandbox.folder_alias}</span>
                  </>
                ) : null}
                . WorkflowWeaver uses the <span className="font-mono text-xs">drive.file</span> scope: it sees only files it created
                or was given, never your whole Google Drive.
              </p>
            </Card>
            <div className="grid gap-6 2xl:grid-cols-2">
              <DocumentStudio
                mode={mode}
                folderAlias={status?.google_drive_sandbox?.folder_alias ?? null}
                plannerLabel={plannerSummary(status)}
                blockedReason={documentBlockedReason(mode, status, driveConnectivity)}
                planning={planning}
                onGenerate={generateDocument}
              />
              <DocumentLibrary mode={mode} refreshKey={libraryRefresh} />
            </div>
            <GoogleDriveSandboxTest
              mode={mode}
              status={status?.google_drive_sandbox ?? null}
              projectTitle={reviewedProject?.project_title ?? null}
              connectivity={driveConnectivity}
              connectivityError={driveConnectivityError}
              checking={driveChecking}
              planning={planning}
              blockedReason={driveBlockedReason(mode, status?.google_drive_sandbox, driveConnectivity)}
              onCheck={() => void runDriveCheck()}
              onPlan={generateDrivePlan}
            />
          </>,
        );
      case "projects":
        return workspace(
          "projects",
          <>
            <NotionProjectWorkflow
              mode={mode}
              planner={status?.planner ?? null}
              parentAlias={status?.notion_sandbox.parent_alias ?? null}
              blockedReason={projectBlockedReason(mode, status)}
              planning={planning}
              onPlan={generateProjectPlan}
            />
            <NotionSandboxTest
              mode={mode}
              parentAlias={status?.notion_sandbox.parent_alias ?? null}
              blockedReason={sandboxBlocked}
              planning={planning}
              onPlan={generateSandboxPlan}
            />
          </>,
        );
      case "communication":
        return workspace(
          "communication",
          <>
            <Card id="teams-channel" title="Approved channel">
              <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-sm">
                <dt className="text-slate-500">Sandbox channel</dt>
                <dd className="font-mono text-xs text-slate-800">{status?.teams_sandbox?.channel_alias ?? "not configured"}</dd>
                <dt className="text-slate-500">Delivery</dt>
                <dd className="text-slate-800">
                  {status?.teams_sandbox?.transport === "webhook" ? "Teams Workflows webhook" : "Simulated (mock adapter)"}
                </dd>
                <dt className="text-slate-500">Allowed action</dt>
                <dd className="text-slate-800">Send message (only to the approved channel)</dd>
              </dl>
            </Card>
            {teamsOption && !teamsOption.available ? (
              <Alert tone="warning" title="Microsoft Teams is not available in this mode">
                {teamsOption.reason ?? "Configure the Teams sandbox channel on the backend."}
              </Alert>
            ) : (
              <WorkflowInput
                templates={templates.filter((t) => t.integrations.every((i) => i === "teams"))}
                templatesState={templatesState}
                templatesError={templatesError}
                planning={planning}
                blockedReason={customPlanBlockedReason(mode, "teams", status, driveConnectivity)}
                notice={teamsNotice}
                noticeTone={mode === "real" ? "real" : "info"}
                heading="1. Describe the notification"
                intro="Say what to announce and to whom; the plan posts to the approved sandbox channel only."
                placeholder="e.g. Notify the team that the rewards pilot starts next sprint and ask for volunteers."
                onSubmit={generatePlan("communication")}
              />
            )}
          </>,
        );
      case "business":
        return workspace(
          "business",
          <>
            <StageTracker plan={planSection === "business" ? plan : null} status={planSection === "business" ? view.workflowStatus : "idle"} />
            <OrchestrationWorkflow
              mode={mode}
              sourceBlockedReason={orchestrationBlocked("document")}
              instructionsBlockedReason={orchestrationBlocked("instructions")}
              planning={planning}
              onPlan={generateOrchestrationPlan}
            />
          </>,
        );
      case "builder": {
        const target = builderTarget;
        const visibleTemplates = target === "multi" ? templates : templates.filter((t) => t.integrations.every((i) => i === target));
        return workspace(
          "builder",
          <>
            {target === "google_drive" && (
              <Alert tone="info" title="Testing the Google Drive sandbox connection?">
                Use the <strong>Google Drive Sandbox Test</strong> in Document Management instead. Its plan (run folder, report
                upload, verified read-back) is built by the server without an AI planner, so it works even when Groq and Gemini
                are unavailable.{" "}
                <button type="button" className="font-semibold underline" onClick={() => navigate("documents")}>
                  Open Document Management
                </button>
              </Alert>
            )}
            <WorkflowInput
              templates={visibleTemplates}
              templatesState={templatesState}
              templatesError={templatesError}
              planning={planning}
              blockedReason={customPlanBlockedReason(mode, target, status, driveConnectivity)}
              notice={
                mode === "real" && target === "google_drive"
                  ? `Real mode: ${plannerSummary(status)} plans Google Drive actions only (create folder, create document, list files, read document), inside the sandbox folder '${status?.google_drive_sandbox?.folder_alias ?? "sandbox"}' and folders the plan creates there. Nothing runs until you approve it.`
                  : target === "teams"
                    ? teamsNotice
                    : null
              }
              noticeTone={mode === "real" ? "real" : "info"}
              heading="Describe what you want to automate"
              intro="Write it in plain language. The AI plans it with the supported actions of the selected integration; you review every step before anything runs."
              prominent
              renderHints={(text) => <UnsupportedHints instruction={text} />}
              onSubmit={generatePlan("builder")}
            />
            <IntegrationTargetSelector value={target} options={builderTargets} disabled={executionActive || planning} onChange={changeBuilderTarget} />
            <CapabilityMatrix />
          </>,
        );
      }
      case "history":
        return <HistoryPage entries={history} />;
      case "monitoring":
        return <MonitoringPage />;
      case "settings":
        return (
          <SettingsPage
            status={status}
            statusError={statusError}
            notionReadiness={notionReadiness}
            driveConnectivity={driveConnectivity}
            driveConnectivityError={driveConnectivityError}
            driveChecking={driveChecking}
            onDriveCheck={() => void runDriveCheck()}
          />
        );
    }
  })();

  return (
    <div className="min-h-screen bg-slate-50">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <Header health={health} mode={mode} onOpenMenu={() => setMenuOpen(true)} menuOpen={menuOpen} />
      <div className="lg:flex">
        <Sidebar
          active={section}
          running={executionActive ? planSection : null}
          onNavigate={navigate}
          mobileOpen={menuOpen}
          onCloseMobile={closeMenu}
          footer={
            <div className="space-y-1 text-xs">
              <p className="font-semibold uppercase tracking-wider text-charcoal-400">Session</p>
              <p className="text-charcoal-300">
                Mode: <span className="font-medium text-white">{MODE_LABELS[mode]}</span>
              </p>
              <p className="text-charcoal-300">
                Runs this tab: <span className="font-medium text-white">{history.length}</span>
              </p>
            </div>
          }
        />
        <main id="main" className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto max-w-7xl space-y-6">
            {health.state === "down" && (
              <Alert title="The WorkflowWeaver API is not reachable">
                Start the backend on http://127.0.0.1:8000 (see README), then reload this page.
              </Alert>
            )}
            {page}
          </div>
        </main>
      </div>
    </div>
  );
}
