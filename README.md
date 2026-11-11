# WorkflowWeaver

FDE Capstone: an AI workflow orchestrator that turns unstructured input (meeting notes, backlogs, incident reports) into executed multi-tool workflows.

## Architecture

![WorkflowWeaver architecture](docs/architecture.png)

| Layer | Technology |
|---|---|
| API | FastAPI: REST + Server-Sent Events for live progress |
| Orchestration | LangGraph: planning graph + execution graph (retries, Saga compensation); Claude or mock planner |
| Integrations | Server-selected mode: **mock** (default), **dry_run**, or **real** (Notion MCP, Google Drive MCP, Teams webhook; foundation only, tested against fakes) |
| Observability | Langfuse Python SDK v5 (optional, failure-isolated) |
| Frontend | React 19 + TypeScript + Vite 8 + Tailwind CSS 4 (`frontend/`) |

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | FastAPI skeleton, templates, `PLANNED` workflow records | Done |
| 2 | LangGraph planning agent: natural language → validated plan | Done |
| 3 | LangGraph execution graph over **mock** tools: approval gate, bounded retries, idempotency, Saga compensation | **Done (mock only)** |
| 4 | Langfuse observability, SSE live progress, React + Tailwind UI, mock document validation, integration and reliability testing | **Done (mock only)** |
| 5.1 | Real-integration foundation: integration modes, MCP + webhook clients, approved destinations, write authorization, secret redaction | **Done (fakes only; no real service connected)** |
| 5.2 | Contract verification: Notion MCP verified **live** (tools/list, `@notionhq/notion-mcp-server` 2.6.1); Google Drive MCP and Teams Workflows verified against **official documentation only**; contract tests, drift detection, opt-in live verification script | **Done (no real workspace, Drive or Teams call made)** |
| 5.3 | Controlled smoke-test harness: offline preflight checklist, read-only connectivity checks, approval-gated synthetic writes to sandbox destinations with remote read-back verification | **Harness done; not yet run against real services (no credentials configured)** |
| 5.4 | UI mode selection (Mock / Dry Run / Real under the server ceiling), Notion readiness + read-only connectivity probe, server-built **Notion Sandbox Test**, explicit real-write confirmation, created-page reference in the report | **Done (tested against fakes; first manual UI run pending)** |
| 5.3 business | **AI-to-Notion business workflow**: plain-English requirements → Claude project plan (project page + action items with description, suggested priority, suggested owning team) → review → explicit approval → real Notion pages under the sandbox parent, with live progress, retries, rollback and page links | **Done (tested against fakes; first real run pending your approval)** |
| 5.5+ | Google Drive and Teams in the UI, Google OAuth token refresh, durable persistence, user authentication | Planned |

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) describes the implementation in detail: both LangGraph graphs, state, the SSE transport, observability, security boundaries, a real worked example, test inventory, limitations and Phase 5 recommendations.

## Quick start (two terminals, Windows PowerShell)

```powershell
# Terminal 1 - backend (http://127.0.0.1:8000, docs at /docs)
cd C:\Projects\workflowweaver\backend
$env:PLANNER_MODE = "mock"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 2 - frontend (http://127.0.0.1:5173)
cd C:\Projects\workflowweaver\frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173, pick a template, click **Generate Plan**, review it, approve, and watch the live timeline and report. First-time backend setup is described in [Setup](#setup-windows-powershell).

## Endpoints

| Method | Path | Phase | Description |
|---|---|---|---|
| GET | `/health` | 1 | Liveness check |
| GET | `/metrics` | 8 | Prometheus metrics (optional bearer token: `METRICS_AUTH_TOKEN`) |
| GET | `/api/v1/monitoring/summary` | 8 | Monitoring configuration, service reachability and real counters of this API process |
| GET | `/api/v1/monitoring/logs` | 8 | Latest sanitised structured log records |
| GET | `/api/v1/monitoring/traces` | 8 | Latest planning/execution runs with trace ids |
| GET | `/api/v1/workflows/templates` | 1 | Meeting-to-Tasks, Sprint Planning, Incident Response |
| POST | `/api/v1/workflows` | 1 | Validates input, returns a UUID and status `PLANNED` |
| POST | `/api/v1/workflows/plan` | 2 | Runs the LangGraph planner; returns a validated plan |
| POST | `/api/v1/workflows/execute` | 3 | Executes an approved plan against **mock** tools |
| GET | `/api/v1/workflows/executions/{workflow_id}` | 3 | Stored result of a finished execution (404 if unknown) |
| POST | `/api/v1/workflows/executions` | 4 | Start a **mock** execution in the background; returns 202 + stream URL/token |
| POST | `/api/v1/workflows/documents/plan` | 8.2 | AI document draft + Markdown preview + 2-step Drive sandbox plan (never writes) |
| GET | `/api/v1/workflows/documents/library?mode=` | 8.2 | Read-only list of app-accessible documents in the Drive sandbox folder |
| GET | `/api/v1/workflows/documents/library/{file_id}?mode=` | 8.2 | Read-only preview of one sandbox document |
| GET | `/api/v1/workflows/executions/{workflow_id}/events` | 4 | Live progress as Server-Sent Events (token required) |
| GET | `/api/v1/integrations/status` | 5.1 | Integration mode (ceiling), selectable modes, write switches, destination **aliases**, Notion sandbox readiness (never URLs, ids or secrets) |
| POST | `/api/v1/integrations/notion/connectivity` | 5.4 | **Read-only** Notion MCP probe (tools/list + retrieve the sandbox parent page); only when real mode is configured |
| POST | `/api/v1/workflows/notion-sandbox/plan` | 5.4 | Server-built, Notion-only sandbox test plan for `{"mode"}`. Never executes |
| POST | `/api/v1/workflows/drive-sandbox/plan` | 6.1 | Server-built Google Drive plan for `{"mode", "project"?}`: run folder, verified report upload, listing. Never executes |
| POST | `/api/v1/integrations/google-drive/connectivity` | 6.1 | **Read-only** Drive probe (token refresh, sandbox folder marker, listing); only when real mode is configured |
| POST | `/api/v1/workflows/notion-project/plan` | 5.3 business | `{"instruction", "mode"}` → Claude project plan compiled to Notion pages. 200 PLANNED / 422 INVALID / 502 FAILED / 503 not configured. Never executes |

`POST /api/v1/workflows/plan` status codes:

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `PLANNED` | Plan generated and validated. **Not executed.** |
| 422 | `INVALID` | Plan generated but rejected by the validator; see `validation.issues` (incl. `DOCUMENT_NOT_FOUND`, below) |
| 422 | — | Request body invalid (FastAPI `detail` body) |
| 502 | `FAILED` | Planner failed (timeout, API error, invalid LLM output) after retries |
| 503 | — | Planner not configured (e.g. `PLANNER_MODE=claude` without `ANTHROPIC_MODEL`) |

`POST /api/v1/workflows/execute` status codes:

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `COMPLETED` | Every step succeeded |
| 403 | `REJECTED` | `approval.approved` is false; nothing executed |
| 422 | `REJECTED` | Plan failed re-validation (`validation.issues`); nothing executed |
| 422 | — | Request body invalid (e.g. `approval.mode` other than `mock`) |
| 409 | — | `workflow_id` already executed with a **different** plan |
| 502 | `FAILED` | A step failed before any side effect; nothing to undo |
| 502 | `ROLLED_BACK` | A step failed; all completed effects were compensated |
| 502 | `PARTIALLY_ROLLED_BACK` | Reversible effects undone; a non-reversible one (Teams message) remains |
| 502 | `COMPENSATION_FAILED` | A compensation also failed; manual review required |
| 503 | — | `MOCK_EXECUTION_ENABLED=false` |

A duplicate request (same `workflow_id` and plan) returns the stored response with `replayed: true` and the same HTTP status, without calling any tool.

Interactive docs: http://127.0.0.1:8000/docs

## Setup (Windows PowerShell)

```powershell
cd C:\Projects\workflowweaver\backend
py -3.11 -m venv .venv          # skip if .venv already exists
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Configuration

Settings come from environment variables, or from `C:\Projects\workflowweaver\.env` (copy `.env.example`). Shell variables win over `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `PLANNER_MODE` | `mock` | `mock` (no key, no network), `claude`, `gemini`, or `auto` (primary + fallback, see [Planner fallback](#planner-fallback-gemini--groq)) |
| `PRIMARY_PLANNER` | `gemini` | `auto` only: provider tried first (`gemini`, `groq`, `claude`) |
| `FALLBACK_PLANNER` | `groq` | `auto` only: provider used when the primary is unavailable; empty disables fallback |
| `GROQ_API_KEY` | — | Required for the Groq planner. Shell or `.env`; redacted from logs and traces; never logged or returned |
| `GROQ_MODEL` | — | Required for Groq; no default in code. Selected: `openai/gpt-oss-120b` (strict structured outputs) |
| `GROQ_MAX_TOKENS` | `8192` | Output cap per Groq planning call |
| `ANTHROPIC_API_KEY` | — | Required for `claude`. Read from the shell or `.env`, passed to the Anthropic client, redacted from logs; never logged or returned |
| `ANTHROPIC_MODEL` | — | Required for `claude` (e.g. `claude-opus-5`). No default in code |
| `ANTHROPIC_MAX_TOKENS` | `8000` | Output cap per planning call |
| `GEMINI_API_KEY` | — | Required for `PLANNER_MODE=gemini` (Google AI Studio key). Shell or `.env`; redacted from logs; never logged or returned |
| `GEMINI_MODEL` | — | Required for `gemini`; no default in code. Selected: `gemini-3.8-flash` (see [Gemini planner](#gemini-planner)) |
| `GEMINI_MAX_OUTPUT_TOKENS` | `16384` | Output cap per Gemini planning call, including thinking tokens |
| `PLANNER_TIMEOUT_SECONDS` | `60` | Per-request timeout for Claude |
| `MAX_WORKFLOW_STEPS` | `10` | Plans with more steps are rejected |
| `MAX_PLANNING_ATTEMPTS` | `2` (`.env.example`: `3`) | Planner (LLM) attempts per plan for retryable failures; each attempt is one API call |
| `PLANNING_BACKOFF_BASE_SECONDS` / `PLANNING_BACKOFF_MAX_SECONDS` | `1.0` / `8.0` | Wait before planner retry n: base × 2^(n-1), capped at max |
| `PLANNING_BACKOFF_JITTER` | `true` | Equal jitter: each wait is uniform in [d/2, d] |
| `MOCK_EXECUTION_ENABLED` | `true` | `false`: `/execute` and `/executions` return 503, and planning skips the mock document check |
| `EXECUTION_MAX_ATTEMPTS` | `3` | Attempts per step (and per compensation) for transient errors (1–5) |
| `EXECUTION_BACKOFF_BASE_SECONDS` | `0.2` | First retry delay; doubles each attempt |
| `EXECUTION_BACKOFF_MAX_SECONDS` | `2.0` | Cap on any single retry delay |
| `EXECUTION_BACKOFF_JITTER` | `true` | Delay drawn uniformly from [d/2, d] |
| `EXECUTION_WORKER_THREADS` | `4` | Worker threads for background (streamed) executions |
| `SSE_HEARTBEAT_SECONDS` | `15` | Keep-alive comment interval on idle streams |
| `SSE_RETENTION_SECONDS` | `300` | How long a finished stream remains available for reconnects |
| `SSE_MAX_CHANNELS` | `1000` | Max streams held in memory |
| `CORS_ALLOW_ORIGINS` | dev: Vite origins | Exact origins allowed to call the API from a browser (not needed with the Vite proxy) |
| `LANGFUSE_ENABLED` | `false` | Send traces to Langfuse |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | — | Langfuse project secret key (secret; never logged) |
| `LANGFUSE_BASE_URL` | SDK default | e.g. `https://cloud.langfuse.com`, `https://us.cloud.langfuse.com`, or self-hosted |
| `INTEGRATION_MODE` | `mock` | `mock`, `dry_run` or `real`; see [Real integrations](#real-integrations-phase-51-foundation) |
| `REAL_WRITES_ENABLED` | `false` | Kill switch for side effects in real mode |
| `REAL_WRITE_AUTHORIZATION_TOKEN` | — | Operator token required (header `X-Integration-Authorization`) for real writes (secret) |
| `INTEGRATION_CONNECT_TIMEOUT_SECONDS` / `INTEGRATION_TIMEOUT_SECONDS` | `5` / `20` | Timeouts for every MCP / webhook request |
| `NOTION_MCP_URL`, `NOTION_MCP_AUTH_TOKEN` | — | Notion MCP server (Streamable HTTP; https, or http on loopback) and its bearer token (secret) |
| `NOTION_MCP_TOOL_ALLOWLIST` | `API-post-page,API-patch-page` | Subset of the verified Notion tools that may be called (names are fixed by the contract) |
| `NOTION_DESTINATIONS`, `NOTION_DEFAULT_PAGE_PARENT`, `NOTION_TASK_DATABASE` | `{}` | Approved destinations: JSON alias → page/database id (UUID) |
| `GOOGLE_DRIVE_MCP_URL`, `GOOGLE_DRIVE_MCP_AUTH_TOKEN`, `GOOGLE_DRIVE_MCP_TOOL_ALLOWLIST` | —, —, `create_file,search_files,read_file_content` | Official Drive MCP server, OAuth 2.0 access token (secret), verified-tool subset |
| `GOOGLE_DRIVE_DESTINATIONS`, `GOOGLE_DRIVE_DEFAULT_FOLDER` | `{}` | Approved folders: JSON alias → folder id (writes **and** reads are confined to them) |
| `TEAMS_WEBHOOK_URLS`, `TEAMS_DEFAULT_CHANNEL` | — | Secret JSON alias → Teams **Workflows** webhook URL (https only; retired `*.webhook.office.com` connectors refused) |
| `SMOKE_TEST_NOTION_PARENT` | — | Alias (in `NOTION_DESTINATIONS`) of the non-production sandbox page used by the smoke test, the UI Sandbox Test and the AI project workflow |
| `NOTION_REAL_WRITES_SANDBOX_ONLY` | `true` | Dry-run/real Notion pages only under `SMOKE_TEST_NOTION_PARENT` (or inside pages created by the same run); task-database writes refused |

Claude Code's login does **not** give your application an API key. Create one in the Claude Console and set it only in your shell or in `.env`.

## Run tests

```powershell
# Backend: 599 tests (includes live-server SSE and end-to-end scenarios on 127.0.0.1,
# Phase 5.1-5.4 integration, contract, smoke-harness, UI-mode and AI-to-Notion business
# workflow tests against a fake Claude client, fake MCP servers and fake webhooks)
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\python.exe -m pytest -q

# End-to-end report: real uvicorn server, 3 templates + fault scenarios, PASS/FAIL per scenario
.\.venv\Scripts\python.exe -m scripts.e2e_check

# Frontend: 56 tests, type check, production build
cd C:\Projects\workflowweaver\frontend
npm test
npm run typecheck
npm run build
```

Tests never call the Claude API or any real Notion / Google Drive / Teams service. The Claude planner is tested with a fake client, `ANTHROPIC_API_KEY` is removed from the environment for every test, and telemetry is pinned to a no-op. Integration tests use an in-memory fake MCP server, `httpx.MockTransport` and a recording webhook sender. An autouse fixture (`tests/conftest.py`) removes integration env vars and **blocks every non-loopback connection** — sync and async httpx, `socket.connect`, DNS lookups (so also urllib and any SDK) — so a test that tries to reach a real service fails. The only network traffic is to servers the tests start on `127.0.0.1`.

## Start the API

Mock planner (no API key needed):

```powershell
cd C:\Projects\workflowweaver\backend
$env:PLANNER_MODE = "mock"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Claude planner (current PowerShell session only; the key is not saved anywhere):

```powershell
cd C:\Projects\workflowweaver\backend
$env:PLANNER_MODE = "claude"
$env:ANTHROPIC_MODEL = "claude-opus-5"
$env:ANTHROPIC_API_KEY = Read-Host "Anthropic API key" -AsSecureString | ForEach-Object { [System.Net.NetworkCredential]::new("", $_).Password }
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## Try the planner

```powershell
$body = @{
  name        = "Sprint sync"
  instruction = "Read meeting notes, create a Notion project page, save a summary to Google Drive, and notify the team in Microsoft Teams."
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/workflows/plan -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 6
```

A rejected plan (unsupported tool) returns HTTP 422, which `Invoke-RestMethod` raises as an error. To see the body:

```powershell
$body = @{ instruction = "Create a Jira issue and notify the team in Teams" } | ConvertTo-Json
try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/workflows/plan -ContentType "application/json" -Body $body }
catch { $_.ErrorDetails.Message }
```

Trace the graph state after every node (mock planner, no server needed):

```powershell
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\python.exe -m scripts.trace_graph
.\.venv\Scripts\python.exe -m scripts.trace_graph "Create a Jira issue and notify Teams"
.\.venv\Scripts\python.exe -m scripts.trace_graph --mermaid
```

## Try mock execution (Phase 3)

```powershell
$body = @{
  workflow_id = [guid]::NewGuid().ToString()
  approval    = @{ approved = $true; mode = "mock"; approved_by = "synthetic.reviewer" }
  plan        = @{
    summary = "Meeting notes to Notion page and Teams notice"
    steps   = @(
      @{ step_number = 1; integration = "google_drive"; action = "read_document"; arguments = @{ document_name = "Synthetic standup notes" } },
      @{ step_number = 2; integration = "notion"; action = "create_page"; arguments = @{ title = "Sprint 42 kickoff"; content = "Synthetic notes" }; depends_on = @(1) },
      @{ step_number = 3; integration = "teams"; action = "send_message"; arguments = @{ message = "Sprint 42 page is ready"; channel = "synthetic-team" }; depends_on = @(2) }
    )
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/workflows/execute -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 6
```

Response (abridged): `status: COMPLETED`, one `step_results` entry per step with a synthetic `resource_id` such as `mock-notion-page-505a1d07ddc4`, `attempts: {"1":1,"2":1,"3":1}`, empty `errors` and `compensation_results`. Posting the same body again returns `replayed: true`.

Synthetic Drive documents available to `read_document`: `Synthetic standup notes`, `Synthetic sprint backlog`, `Synthetic incident report` (single source: `app/agents/synthetic_documents.py`), or a document created by an earlier `create_document` step in the same plan (exact title).

**Missing documents are caught before execution.** In mock execution mode, every `read_document` name must match the catalog exactly (as the mock Drive adapter does), or match the title of an earlier `create_document` step. Otherwise:
- `/plan` returns 422 `INVALID` with a `DOCUMENT_NOT_FOUND` issue. The message lists the available documents, or says "Did you mean …?" when only capitalisation differs.
- `/execute` and `/executions` reject such a client-supplied plan in `prepare`, as `REJECTED` with no tool calls.

The adapter's runtime `DOCUMENT_NOT_FOUND` check is still the authoritative last line of defence. It is permanent, so it is not retried, and earlier effects are rolled back. Tests exercise it with `ExecutionService(document_precheck=False)`.

**Mock planner.**
- A Drive read uses the document the instruction names: a quoted name, otherwise a catalog name mentioned verbatim, otherwise the phrase used. There is no fuzzy matching.
- Titles come from the workflow's subject, e.g. `Action items: Synthetic standup notes`, `Sprint backlog: project page`.

**Claude planner.** In mock execution mode, the system prompt lists the synthetic document *names*. It never includes contents, ids or keys. Claude is told to use exact names and never to substitute a different document.

### Execution graph

```
START -> prepare --(rejected)----------------------------------------> finalize -> END
            \--(ok)--> execute_step --(ok, more steps)--> execute_step
                            |   \--(ok, last step)---------------> finalize
                            |   \--(transient, attempts left)--> retry_wait -> execute_step
                            \--(failed, effects journaled)--> compensate -> finalize
                             \--(failed, nothing to undo)---------------> finalize
```

- **prepare** re-validates the plan with the Phase 2 validator (the client's plan is not trusted), checks every action has an adapter, then checks approval.
- **execute_step** makes exactly one tool attempt. Steps run sequentially in `step_number` order, which satisfies `depends_on` (dependencies must point to earlier steps).
- **Retries**: only `transient` errors are retried, up to `EXECUTION_MAX_ATTEMPTS`, with capped exponential backoff. `permanent` and `permission` errors fail immediately.
- **Saga journal**: every successful step with a side effect is journaled. On failure, **compensate** undoes the journal in reverse order (compensations are retried for transient errors too). Read-only steps are not journaled; Teams messages are reported as `NOT_COMPENSATABLE`.
- **Reconcile**: if a step fails after its effect happened (e.g. timeout after the write), the engine looks up the effect by idempotency key and journals it, so it is still rolled back.
- **Idempotency**: tool calls use the key `{workflow_id}:step-{n}`; repeating a key returns the original result (`replayed: true`) without a second effect. Requests are deduplicated by `workflow_id` (per-key lock, so concurrent duplicates execute once).

Failure injection (`FailureInjector`: `transient`, `transient_after_effect`, `permanent`, `permission_denied`, `malformed_result`) exists for tests only. The API never accepts it, and `create_tool_registry` refuses it when `APP_ENV=production`.

## Frontend (React + TypeScript + Tailwind)

`frontend/` is a Vite app that uses the real API: `GET /health`, `GET /templates`, `POST /plan`, `POST /executions`, the SSE stream, and `GET /executions/{id}` for the report. Its TypeScript types (`src/api/types.ts`) mirror the Pydantic models field for field.

Flow: pick a template or type an instruction → **Generate Plan** → review the validated steps, integrations and dependencies (or validator issues) → enter *Approved by*, tick the confirmation, and click **Approve and Execute Mock Workflow** → watch the live step badges and event timeline → read the final report (attempts, errors, compensation results).

Requires Node.js ≥ 22.13 (tested with 22.14).

```powershell
# Terminal 1 - backend
cd C:\Projects\workflowweaver\backend
$env:PLANNER_MODE = "mock"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 2 - frontend (first time: npm install)
cd C:\Projects\workflowweaver\frontend
npm install
npm run dev          # http://127.0.0.1:5173
```

Other scripts: `npm test` (Vitest + Testing Library), `npm run typecheck`, `npm run build` (type check + production build to `dist/`), `npm run preview`.

**How the browser reaches the API.** By default the browser only talks to `http://127.0.0.1:5173`, and the Vite dev server proxies `/api` and `/health` to the backend (`WW_BACKEND_URL`, default `http://127.0.0.1:8000`). That is same-origin, so no CORS is involved, and the SSE stream passes through the proxy unbuffered. To call the API directly instead, set `VITE_API_BASE_URL=http://127.0.0.1:8000` in `frontend/.env.local`. The backend then allows only the origins in `CORS_ALLOW_ORIGINS` (in development, the two Vite origins by default): GET/POST, `Content-Type`, `Last-Event-ID` and `X-Stream-Token` headers, no credentials.

**Secrets.** The frontend has no secrets and needs none. `VITE_*` variables are compiled into the public bundle, so never put Anthropic or Langfuse keys in any frontend `.env` file. The SSE `stream_token` is the per-run capability described below. It is kept in memory only.

**UI states.**
- API reachable or unreachable; templates loading or failed (you can still type an instruction).
- Inline form validation (same limits as the backend); planning spinner.
- Validator rejection (issues listed, execution not offered); planner failure.
- Execution start errors (e.g. 409).
- Live / reconnecting (the browser retries automatically and replays missed events) / disconnected (manual **Reconnect** resumes after the last event; **Check final result** fetches the report).
- Report loading or unavailable. A REJECTED run has no stored report, so its outcome is shown from the final stream event.

Every step and workflow badge comes from real stream events: pending, running, succeeded, failed, retrying, compensating, rolled back, not reversible, compensation failed, skipped.

**Recent runs** covers the current browser tab only, because the API has no workflow-history endpoint.

## Live progress (SSE)

`POST /api/v1/workflows/execute` (synchronous) is unchanged. For live progress, send the **same request body** to `POST /api/v1/workflows/executions`:

| HTTP | Meaning |
|---|---|
| 202 | Started. Body: `workflow_id`, `stream_url`, `stream_token`, `result_url`, `access_model: "mock-capability-token"` |
| 409 | `workflow_id` already executed with a different plan, or its stream is still active |
| 422 | Invalid body (same rules as `/execute`) |
| 503 | `MOCK_EXECUTION_ENABLED=false`, `APP_ENV=production`, or `SSE_MAX_CHANNELS` reached |

Then open `stream_url` (`GET /api/v1/workflows/executions/{workflow_id}/events?access_token=...`):

| HTTP | Meaning |
|---|---|
| 200 | `text/event-stream`; ends after `workflow_completed` or `workflow_failed` |
| 204 | Stream finished and the client already has every event (tells `EventSource` to stop reconnecting) |
| 404 | Unknown workflow, expired stream, or wrong/missing token (deliberately indistinguishable) |
| 503 | Streaming disabled (see above) |

The final `ExecutionResponse` is available at `result_url` once the run finishes, exactly as for `/execute`.

### Event contract

```
id: 4
event: step_failed
data: {"event_id":4,"workflow_id":"...","event_type":"step_failed","step_number":2,"timestamp":"2026-11-04T03:14:49.865218Z","metadata":{"attempt":1,"error_code":"TOOL_TIMEOUT","error_kind":"transient","retryable":true,"will_retry":true,"retries_exhausted":false}}
```

`event_id` is a per-workflow sequence (1, 2, 3, ...) and the SSE `id`. Events are always delivered in `event_id` order. Every event is emitted **inside the LangGraph node that performs the transition**; none are synthesised from the final result.

| `event_type` | Emitted by node | `step_number` | Key `metadata` |
|---|---|---|---|
| `workflow_started` | `prepare` (plan valid and approved) | — | `step_count`, `steps[{step_number, integration, action, depends_on}]`, `max_attempts` |
| `step_started` | `execute_step`, before each tool attempt | yes | `attempt`, `integration`, `action` |
| `step_succeeded` | `execute_step` | yes | `attempt`, `resource_id`, `replayed` |
| `step_failed` | `execute_step`, after **each** failed attempt | yes | `attempt`, `error_code`, `error_kind`, `retryable`, `will_retry`, `retries_exhausted` |
| `retry_scheduled` | `retry_wait` (before sleeping) or `compensate` | yes | `phase` (`execution`/`compensation`), `failed_attempt`, `next_attempt`, `delay_seconds`, `error_code` |
| `compensation_started` | `compensate`, per journaled step (reverse order) | yes | `integration`, `action`, `effect`, `resource_id` |
| `compensation_completed` | `compensate` | yes | `status` (`COMPENSATED`/`ALREADY_COMPENSATED`/`NOT_COMPENSATABLE`), `attempts` |
| `compensation_failed` | `compensate` | yes | `status`, `attempts`, `error_codes` |
| `workflow_completed` | `finalize` | — | `status: COMPLETED`, `attempts`, `step_statuses`, ... |
| `workflow_failed` | `finalize` | — | `status` (`FAILED`/`ROLLED_BACK`/`PARTIALLY_ROLLED_BACK`/`COMPENSATION_FAILED`/`REJECTED`), `failed_step`, `step_statuses`, `compensation_statuses`, `error_codes`, `validation_issue_codes` |

Special cases:
- A **rejected** request (not approved or invalid plan) emits only `workflow_failed` with `status: REJECTED`.
- A **duplicate** request that was already finished doesn't re-run the graph. It emits one terminal event with `replayed: true`.
- An **unexpected server error** emits `workflow_failed` with `{"status": "ERROR", "error_code": "INTERNAL_ERROR"}`.

**Never in events**: instruction or prompt text, tool argument values, document contents, error message text, credentials. Metadata is also passed through the observability sanitiser.

### How it works

```
POST /executions --> ExecutionRunner (ThreadPoolExecutor) --> worker thread:
                       set_publisher(channel)  [ContextVar, this run only]
                       ExecutionService.execute -> LangGraph nodes -> emit(...)
                                                                 |
                       EventChannel.publish: lock, append history, loop.call_soon_threadsafe
                                                                 v
GET /executions/{id}/events --> subscribe(after Last-Event-ID): backlog + asyncio.Queue --> SSE
```

- **Event loop is never blocked.** The graph and its retry sleeps run on worker threads. The SSE handler only awaits its queue.
- **Events are scoped to one run.** The publisher is a `ContextVar` set only in the worker thread running that run. The synchronous `/execute` endpoint, and any other workflow running at the same time, can't publish into the stream.
- **Ordering.** Publishing is serialised per workflow, and each subscriber's queue is fed in FIFO order. Subscribing takes the backlog snapshot and registers the queue under the same lock, so a subscriber never sees a gap or a duplicate.
- **Disconnects.** Starlette cancels the stream generator and the subscriber is removed. **The execution keeps running.** Its result stays at `result_url`, and the stream can be reopened.
- **Reconnection.** The browser `EventSource` reconnects automatically (`retry: 3000`) and sends `Last-Event-ID`. The server replays every event after that id. Once the stream has ended and the client has everything, the server returns 204, which stops further reconnects. Clients should also call `close()` on the terminal event.
- **Heartbeats.** Idle streams receive a `: keep-alive` comment every `SSE_HEARTBEAT_SECONDS`.

### Security model (mock only)

- `stream_token` is an unguessable 256-bit, per-workflow capability. It is returned only to the caller of `POST /executions` and compared in constant time. A token for workflow A doesn't open workflow B.
- It is **not user authentication**. Anyone holding the URL can read that one stream. Tokens in query strings can end up in proxy or browser logs. The backend redacts `access_token=` values from the uvicorn access log and the httpx request log (`app/core/log_redaction.py`); a reverse proxy in front of it needs the same treatment.
- Streaming endpoints return **503 when `APP_ENV=production`**. A production deployment needs real authentication (OIDC/session) with per-user authorization on workflows, and the token should move to a header or cookie.
- The Phase 3 `GET /executions/{workflow_id}` result endpoint keeps its existing behaviour: no token, lookup by unguessable UUID.
- Phase 5.4 real-mode UI runs use this same access model, so they are suitable for a **local, single-operator** sandbox test only. The stream and stored result show the created page's id and Notion link (never the parent id, MCP URL or tokens).

### Limitations

- In-memory and single-process: streams and history are lost on restart, and won't work across multiple uvicorn workers or instances. Use one worker, or replace the broker with Redis Streams or similar.
- History is bounded: 2,000 events per workflow; the terminal event is always kept. Finished streams are evicted after `SSE_RETENTION_SECONDS`.
- When all `EXECUTION_WORKER_THREADS` are busy, new runs queue and their streams stay quiet until a thread frees up.
- Failure injection is test-only, so retries and rollbacks can't be triggered through the API. See the live-server tests in `tests/test_sse.py`.

### Verify (PowerShell)

Terminal 1:

```powershell
cd C:\Projects\workflowweaver\backend
$env:PLANNER_MODE = "mock"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Terminal 2 (`curl.exe` ships with Windows 11; `-N` disables buffering):

```powershell
$base = "http://127.0.0.1:8000"
$plan = Invoke-RestMethod -Method Post -Uri "$base/api/v1/workflows/plan" -ContentType "application/json" -Body (@{ instruction = "Read meeting notes, create a Notion project page, save a summary to Google Drive, and notify the team in Microsoft Teams." } | ConvertTo-Json)
$exec = @{ workflow_id = $plan.workflow_id; plan = @{ summary = $plan.summary; steps = $plan.planned_steps }; approval = @{ approved = $true; mode = "mock"; approved_by = "synthetic.reviewer" } } | ConvertTo-Json -Depth 8
$start = Invoke-RestMethod -Method Post -Uri "$base/api/v1/workflows/executions" -ContentType "application/json" -Body $exec
curl.exe -N "$base$($start.stream_url)"
(Invoke-RestMethod "$base$($start.result_url)").status                                                     # COMPLETED
curl.exe -s -o NUL -w "%{http_code}`n" -H "Last-Event-ID: 99" "$base$($start.stream_url)"                  # 204
curl.exe -s -o NUL -w "%{http_code}`n" "$base/api/v1/workflows/executions/$($plan.workflow_id)/events?access_token=wrong"  # 404
```

End-to-end reliability check: a real uvicorn server on a free local port, over HTTP and SSE, with mock tools and synthetic fault injection configured in code. It covers all three templates, transient retry with proof of live delivery, an idempotent retry after a timeout, retry exhaustion, permanent failure with reverse rollback, partial rollback, compensation failure, duplicate replay, and the `DOCUMENT_NOT_FOUND` negative case. It prints PASS/FAIL per scenario and exits non-zero on failure. The same scenarios run in pytest as `tests/test_e2e_live.py`.

```powershell
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\python.exe -m scripts.e2e_check
```

Retries, rollback, live delivery during a retry backoff, and disconnects (real uvicorn server on a local port):

```powershell
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\python.exe -m pytest tests/test_sse.py -v
```

## Observability (Langfuse)

Tracing uses the Langfuse Python SDK v5 (`langfuse==5.1.0`, OpenTelemetry based) behind a small facade in `backend/app/observability/`. It is **off by default**.

Enable it (current PowerShell session only):

```powershell
cd C:\Projects\workflowweaver\backend
$env:LANGFUSE_ENABLED = "true"
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-..."
$env:LANGFUSE_SECRET_KEY = Read-Host "Langfuse secret key" -AsSecureString | ForEach-Object { [System.Net.NetworkCredential]::new("", $_).Password }
$env:LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
.\.venv\Scripts\python.exe -m scripts.langfuse_check
```

`scripts.langfuse_check` runs `auth_check()`, then one mock planning request and one mock execution (one injected retry plus a rollback), flushes, and prints a session id. In Langfuse, open **Sessions** and search for that id.

What is traced (every observation carries `workflow_id`, and `session_id = workflow_id` so planning and execution of one workflow are grouped):

| Observation | Type | Created in |
|---|---|---|
| `planning.request` | agent (trace root) | `PlanningService.run` |
| `planning.planner` / `.validator` / `.reporter` / `.error` | chain | each planning LangGraph node (`traced_node`) |
| `claude.messages.parse` | generation: model, `max_tokens`, token usage, stop reason | `ClaudePlanner.plan` |
| `execution.request` | agent (trace root): final status, attempts, error codes, compensation statuses | `ExecutionService.execute` |
| `execution.prepare` / `.execute_step` / `.retry_wait` / `.compensate` / `.finalize` | chain, with `step_number` | each execution LangGraph node |
| `tool.<integration>.<action>` | tool: attempt, effect, resource id, error code | one per mock tool attempt |
| `compensation.<integration>.<action>` | tool: attempt, outcome | one per compensation attempt |
| `execution.retry_scheduled`, `execution.compensation_retry_scheduled` | event: failed attempt, delay, error code | `retry_wait` node / compensation loop |
| `execution.effect_reconciled`, `execution.outcome` | event | reconcile / end of request |

Latency is recorded on every observation (span timing plus `latency_ms` metadata). Levels: `WARNING` for retryable errors, rollbacks and rejections; `ERROR` for permanent tool errors and `COMPENSATION_FAILED`.

**Never sent**: instruction text, system prompt, tool argument values, document contents, error message text, `approved_by`, or keys. Only lengths, argument *names*, ids, codes, counts and timings are sent. A sanitiser (`observability/sanitize.py`) scrubs every payload anyway (secret-named keys, content keys, `sk-ant-`/`sk-lf-`/bearer/JWT/webhook patterns, size limits) and is also installed as the SDK's `mask` hook.

**Failure isolation**: every SDK call is wrapped. If Langfuse is disabled, misconfigured, unreachable, or the SDK raises, the workflow result is identical, and the failure is logged once by exception type. Export happens on the SDK's background thread. Tests pin telemetry to a no-op, so a developer `.env` with `LANGFUSE_ENABLED=true` never makes tests send data.

> Note: `langsmith` is installed as a transitive dependency of LangGraph. Do not set `LANGSMITH_TRACING=true`: LangSmith tracing would capture full graph state, including instruction text.

## Monitoring and observability (Phase 8)

WorkflowWeaver emits **structured logs**, **Prometheus metrics** and **OpenTelemetry traces**, and the existing **Langfuse** tracing keeps working unchanged. A local Docker Compose stack (Prometheus, Grafana, Loki, Tempo and an OpenTelemetry Collector) collects and visualises everything. The UI's **Monitoring & Observability** section shows the same data.

```text
API (127.0.0.1:8000) -- GET /metrics -----------------------------> Prometheus :9090 --+
API -- OTLP/HTTP traces --> OTel Collector :4318 --> Tempo :3200 ----------------------+--> Grafana :3000
API -- LOG_FILE (JSON lines in ./logs) --> Collector (filelog) --> Loki :3100 ---------+
API -- Langfuse SDK (unchanged) -----------------------------------> Langfuse
```

How it is wired:
- **One instrumentation point.** Planning and execution code already reports through `get_telemetry()`. That facade is now a composite: it passes every call to Langfuse (or the no-op) unchanged and fans it out to metrics, structured logs, a recent-runs index and, when Langfuse is off, OpenTelemetry spans. Workflow code, approval gates and sandbox rules are untouched. A failing sink never affects a workflow.
- **One tracer provider.** It is registered before the Langfuse client, so Langfuse attaches to it. Workflow spans are one set of spans:
  - Langfuse receives only its own spans (its default filter).
  - Tempo receives the whole tree: request, workflow, outbound HTTP.
  - No duplicates.

### Start everything (Windows PowerShell)

```powershell
# 1. One-time: install the new Python packages
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. One-time: Grafana admin password (monitoring\.env is git-ignored; Compose refuses to start without it)
cd C:\Projects\workflowweaver\monitoring
Copy-Item .env.example .env
notepad .env          # set GRAFANA_ADMIN_PASSWORD to a strong value

# 3. Start the monitoring stack (Docker Desktop must be running)
docker compose up -d
docker compose ps     # every service "running"

# 4. Start the API with JSON logs, a log file for Loki, tracing and the UI links
cd C:\Projects\workflowweaver\backend
$env:LOG_FORMAT = "json"
$env:LOG_FILE = "..\logs\workflowweaver.jsonl"
$env:OTEL_ENABLED = "true"
$env:GRAFANA_URL = "http://127.0.0.1:3000"
$env:PROMETHEUS_URL = "http://127.0.0.1:9090"
$env:LOKI_URL = "http://127.0.0.1:3100"
$env:TEMPO_URL = "http://127.0.0.1:3200"
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 5. Frontend (second terminal), as before
cd C:\Projects\workflowweaver\frontend
npm run dev
```

You can put the same variables in the repository `.env` instead (see the Phase 8 block in `.env.example`). Without the stack, nothing changes: `OTEL_ENABLED` defaults to `false`, `/metrics` still works, and logs stay human-readable in development (`LOG_FORMAT=auto`).

**Compose commands** (run in `monitoring\`):

| Command | Effect |
|---|---|
| `docker compose up -d` | Start the stack |
| `docker compose ps` | Status |
| `docker compose logs -f otel-collector` | Follow one service's logs |
| `docker compose restart grafana` | Restart one service |
| `docker compose down` | Stop (data volumes kept) |
| `docker compose down -v` | Stop and delete all stored metrics, logs and traces |

### URLs

| What | URL | Notes |
|---|---|---|
| WorkflowWeaver UI, Monitoring & Observability | http://127.0.0.1:5173/#/monitoring | Overview, Logs, Workflow Traces, AI Monitoring |
| Grafana | http://127.0.0.1:3000 | Sign in with `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` |
| Dashboard: API performance | http://127.0.0.1:3000/d/ww-api | Requests, 5xx ratio, latency percentiles, error logs |
| Dashboard: Workflow execution | http://127.0.0.1:3000/d/ww-workflows | Plans, executions, rejections, retries, compensation, LLM calls, fallbacks, tokens |
| Dashboard: Integration health | http://127.0.0.1:3000/d/ww-integrations | Step success, failures by error code, Drive/Teams call latency |
| Prometheus targets / alerts | http://127.0.0.1:9090/targets and http://127.0.0.1:9090/alerts | |
| API metrics | http://127.0.0.1:8000/metrics | Prometheus text format |
| Monitoring API | http://127.0.0.1:8000/api/v1/monitoring/summary | Also `/logs` and `/traces` |

Every port is published on **127.0.0.1 only**. Grafana has anonymous access and sign-up disabled. The datasources are provisioned read-only, with links from Loki logs to Tempo traces and back via `trace_id`.

### Configuration

| Setting | Default | Purpose |
|---|---|---|
| `LOG_FORMAT` | `auto` | `json`, `console`, or `auto` (console in development) |
| `LOG_FILE` | empty | JSON log file (rotated at 10 MB x 5) that the collector ships to Loki |
| `SERVICE_NAME` | `workflowweaver-api` | `service` field in logs, `service.name` in traces |
| `METRICS_ENABLED` / `METRICS_AUTH_TOKEN` | `true` / empty | `/metrics`, with an optional bearer token |
| `OTEL_ENABLED` / `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SAMPLE_RATIO` | `false` / `http://127.0.0.1:4318` / `1.0` | Tracing |
| `GRAFANA_URL`, `LANGFUSE_UI_URL` | empty | Links in the UI |
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL` | empty | Server-side reachability checks shown in the UI |

### Structured logs

- **JSON fields:** every record has `timestamp`, `severity`, `service`, `environment`, `logger`, `message`, `request_id`, `workflow_id`, `trace_id` and `span_id`. Where they apply, it also has `event`, `integration`, `action`, `step_number`, `duration_ms`, `error_code`, `status`, `mode`, `provider`, `model` and `http_status`, plus a sanitised `attributes` object.
- **Request IDs:** each request gets an `X-Request-ID`. A valid incoming value is kept; otherwise one is generated. It is returned in the response header.
- **Lifecycle events logged:** `planning.started` / `planning.finished`, `planning.provider_attempt`, `planning.fallback`, `llm.request`, `execution.started` / `execution.finished` / `execution.outcome`, `approval.rejected`, `step.succeeded` / `step.failed`, `execution.retry_scheduled`, `compensation.succeeded` / `compensation.failed` and `integration.call`. There is also one `http.request` access record per request, with the route template, status and duration.
- **Never logged:** API keys, OAuth tokens, webhook URLs, authorization headers, request bodies, prompts or document content. Redaction is layered:
  1. The existing filter removes registered integration secrets and URL tokens (`access_token=`, `sig=`, Google OAuth shapes).
  2. The formatter scrubs every rendered line again (Groq, Anthropic, Google, Langfuse and Notion keys, bearer tokens, JWTs, webhook URLs).
  3. Credential-named fields are replaced and content fields are reduced to their length.
  4. Exceptions are logged by type only.

### Metrics (`GET /metrics`)

| Area | Metrics |
|---|---|
| HTTP | `ww_http_requests_total{method,route,status}`, `ww_http_request_errors_total`, `ww_http_request_duration_seconds`, `ww_http_requests_in_progress` |
| Workflows | `ww_workflow_plans_total{target,status}`, `ww_workflow_planning_duration_seconds`, `ww_workflow_executions_started_total{mode,target}`, `ww_workflow_executions_finished_total{mode,target,status}`, `ww_workflow_executions_in_progress`, `ww_workflow_execution_duration_seconds`, `ww_workflow_rejections_total{reason}` |
| Steps and integrations | `ww_step_duration_seconds{integration,action,status}`, `ww_step_retries_total`, `ww_integration_failures_total{integration,action,error_code}`, `ww_integration_requests_total` and `ww_integration_request_duration_seconds` (Drive REST, Teams), `ww_compensations_total{outcome}` |
| LLM | `ww_llm_requests_total{provider,model,outcome}`, `ww_llm_request_duration_seconds`, `ww_llm_errors_total{provider,error_code}`, `ww_llm_fallbacks_total{from_provider,to_provider,reason}`, `ww_llm_tokens_total{provider,model,type}` (only when the provider reports usage) |

Labels are always bounded: route templates (never raw URLs), enums, configured model ids and normalised error codes. Workflow, user and file ids are never labels.

### Traces

With `OTEL_ENABLED=true`:
- **What is traced:**
  - FastAPI server spans (`/metrics` and `/health` excluded);
  - outbound httpx calls (Groq and Gemini SDKs, Drive REST, Notion MCP, Teams), with W3C `traceparent` propagation;
  - planning and execution spans: planning request, provider calls, graph nodes, each tool step, compensation.
- **Sanitising:** every exported span is scrubbed first, both attributes and span events. This covers the SSE `access_token` query parameter, the Teams `sig`, credential-named attributes and secret-shaped values.
- **Finding a run:** the Workflow Traces tab links each run to its trace in Grafana (Tempo). With Langfuse on, search Langfuse for the workflow id (it is the session id).

### Alerts

`monitoring/prometheus/alerts.yml` defines these sample rules. They appear in Prometheus (Alerts) and in Grafana; no Alertmanager or notification channel is configured.

| Alert | Fires when |
|---|---|
| `WorkflowWeaverApiDown` | `/metrics` is not scraped for 2 minutes |
| `HighApiErrorRate` | 5xx responses are above 5% for 5 minutes |
| `SlowWorkflowExecutions` | Execution p95 is above 2 minutes |
| `SlowPlanning` | Planning p95 is above 60 seconds |
| `IntegrationFailures` | More than 3 failed step attempts per integration in 10 minutes |
| `CompensationFailures` | Any failed rollback |
| `LlmFallbackSpike` | More than 5 fallbacks in 15 minutes |

### Troubleshooting

| Symptom | Check |
|---|---|
| `docker compose up` says `Set GRAFANA_ADMIN_PASSWORD` | Create `monitoring\.env` from `.env.example` and set the password, or set `$env:GRAFANA_ADMIN_PASSWORD` in the same shell |
| Prometheus target `workflowweaver-api` is down | Is the API running on port 8000? Open http://127.0.0.1:8000/metrics in a browser. Docker Desktop must reach the host through `host.docker.internal`. If a firewall or VPN blocks it, start uvicorn with `--host 0.0.0.0` only together with `METRICS_AUTH_TOKEN` and the commented `authorization` block in `prometheus.yml`. |
| `/metrics` returns 401 | `METRICS_AUTH_TOKEN` is set: put the same value in `monitoring\secrets\metrics_token` (git-ignored), mount it and enable `authorization` in `prometheus.yml` |
| No logs in Grafana (Loki) | `LOG_FILE` must be set and point into the repository `logs\` folder. The collector reads `*.jsonl` there and starts at the end of the file, so generate new activity. Run `docker compose logs otel-collector`. |
| No traces in Tempo, or "Not exported" in the UI | `OTEL_ENABLED=true` and the collector running on 127.0.0.1:4318; restart the API after changing it. `OTEL_SAMPLE_RATIO` below 1 drops some traces. |
| Monitoring tab says "Not configured" | Set `GRAFANA_URL`, `PROMETHEUS_URL`, `LOKI_URL` and `TEMPO_URL` for the API and restart it |
| Monitoring tab says "Unreachable" | The URL is set but the service is down: `docker compose ps` |
| UI counters reset | They are counters of the running API process. Prometheus keeps the history; use the Grafana dashboards. |
| Port already in use (3000, 9090, 3100, 3200, 4317, 4318) | Stop the other program, or change the left side of the `ports:` mapping in `docker-compose.yml` |

## Real integrations (Phase 5.1 foundation, Phase 5.2 verification)

Stage 5.1 added the infrastructure for real Notion, Google Drive and Microsoft Teams calls. Stage 5.2 verified the tool contracts against the selected servers (see [Contract verification](#contract-verification-phase-52)). Mock mode stays the default. Phase 5.4 lets the UI select a mode at or below the server ceiling and run the Notion Sandbox Test (see [Notion Sandbox Test from the UI](#notion-sandbox-test-from-the-ui-phase-54)).

### Integration modes (server-controlled ceiling)

| `INTEGRATION_MODE` | Adapters | Network | Use |
|---|---|---|---|
| `mock` (default) | In-memory mocks, synthetic documents | none | Development, demos, all Phase 1–4 behaviour |
| `dry_run` | Real adapters, no sender | **none** (no client is even created) | Validate destinations, authorization and request shapes before going live |
| `real` | Real adapters + MCP / webhook clients | configured servers only | Stage 5.2+, after verification against real servers |

`INTEGRATION_MODE` is the **ceiling** (`mock` < `dry_run` < `real`). Each execution request selects its mode with `approval.mode`, which may be the ceiling or any lower mode; a higher mode is rejected with HTTP 409 `MODE_MISMATCH` before anything runs (the streaming endpoint checks before creating a stream). A client can therefore never escalate. Downgrading is safe because mock and dry-run make no network call. Each mode has its own lazily built executor, and a `workflow_id` is bound to the first mode it was submitted in (another mode for the same id: 409), so generate a new plan to switch modes. `POST /plan` accepts an optional `mode`; only mock plans are checked against the synthetic document catalog.

The same LangGraph execution graph, retry policy, request idempotency, Saga compensation, SSE events and Langfuse spans are used in every mode. Only the adapters behind `ToolRegistry` change.

### Authorization for real writes

A plan containing any write step (`create_page`, `create_task`, `create_document`, `send_message`) runs in real mode only if **all** of these hold. Otherwise it is rejected in `prepare`, before any call:

1. `approval.approved: true` (as before; otherwise 403 `APPROVAL_REQUIRED`).
2. `REAL_WRITES_ENABLED=true` (otherwise 403 `REAL_WRITES_DISABLED`).
3. The request carries `X-Integration-Authorization: <REAL_WRITE_AUTHORIZATION_TOKEN>`, compared in constant time (otherwise 403 `REAL_WRITE_AUTHORIZATION_REQUIRED`). The adapters re-check this per call (`REAL_WRITE_NOT_AUTHORIZED`).
4. Every destination is **approved**. Plans name destinations by alias only (`parent`, `folder`, `channel`, or the configured default). An unknown alias, or a raw id or URL, is rejected with 422 `DESTINATION_NOT_APPROVED`. Notion tasks always go to `NOTION_TASK_DATABASE`.

Read-only plans (`read_document`) need no write authorization, but reads are confined to the approved Drive folders: the search is scoped to them, and each match is re-checked.

### Components

| Module | Responsibility |
|---|---|
| `app/integrations/config.py` | Validates settings: https-only URLs (http on loopback for local MCP servers), no credentials in URLs, alias maps, Notion ids are UUIDs, allowlists ⊆ verified contracts, retired Teams connector hosts refused. Errors name the setting, never the value |
| `app/integrations/contracts.py` | The verified tool contracts (tool name, arguments sent, provenance) and `check_tool_schema` drift detection |
| `app/integrations/mcp/client.py` | Reusable MCP client: lazy connect, `initialize` handshake + protocol-version check, paginated `tools/list` discovery filtered to the allowlist, `tools/call` with typed `McpToolResult`, session-expiry reconnect, thread-safe, `close()` |
| `app/integrations/mcp/transport.py` | `McpTransport` protocol + Streamable HTTP transport on httpx (JSON or SSE responses, `Mcp-Session-Id`, bearer auth, no redirects, response size cap) |
| `app/integrations/http/webhook.py` | Webhook HTTP client for Teams (timeouts, no redirects, payload size cap, status → error classification) |
| `app/integrations/destinations.py` | Approved-destination policy (alias → server-side target) |
| `app/integrations/secrets.py` | Registry of configured secret values, scrubbed from every log record and every telemetry payload |
| `app/agents/execution/tools/integrations.py` | `NotionMcpAdapter`, `GoogleDriveMcpAdapter`, `TeamsWebhookAdapter` (same `ToolAdapter` contract as the mocks) and `build_integration_registry` |
| `app/api/integrations.py` | `GET /api/v1/integrations/status` |
| `scripts/verify_integrations.py` | Opt-in live verification: plan only by default; `--live` = MCP discovery + contract check only (never `tools/call`); Teams test post needs two explicit flags |

### Reliability semantics

- **Errors** are classified as `TRANSIENT` (retried), `PERMANENT` or `PERMISSION`, and record whether the request may have reached the server. Error messages are fixed strings and never include remote response text, which can echo arguments.
- **No duplicate writes:** a transient failure *after* a write request was sent (read timeout, 5xx) becomes the permanent `OUTCOME_UNKNOWN` and is **not retried**, so a Teams message or Notion page cannot be created twice. Failures before sending (connection refused, 429) are retried as usual.
- **Idempotency:** each adapter records results per `workflow_id:step-N` and replays them on retry or duplicate delivery. Request-level replay (same `workflow_id`) works as before.
- **Saga compensation:** Notion pages are moved to trash (`API-patch-page` with `in_trash: true`); compensation is idempotent and retried on transient errors. **Google Drive documents cannot be rolled back**: the official Drive MCP server has no delete/trash tool, so `create_document` is `IRREVERSIBLE` in dry-run/real mode (a later failure yields `PARTIALLY_ROLLED_BACK`). Teams messages remain `NOT_COMPENSATABLE`.
- **Contract drift:** on connect, every contracted tool's live input schema is compared with what we send. A drifted tool is disabled and fails with `MCP_CONTRACT_MISMATCH` before anything is sent.
- **Notion API errors** arrive as normal tool results (`{"status": <http>, "object": "error", "code": ...}`), verified from the 2.6.1 source; they map to `NOTION_API_ERROR` (400/404 permanent, 401/403 permission, 409/429 retried, 5xx on a write → `OUTCOME_UNKNOWN`). Notion's message text is never copied.

### What never leaves the server

MCP URLs, bearer tokens, the operator token, webhook URLs and destination ids never appear in API responses, SSE events, Langfuse payloads or logs. Read document contents are reduced to `content_length`. This is enforced by construction and backed by the secret registry, which also scrubs httpx's own `HTTP Request: POST <url>` log line. Tests assert it for responses, SSE events, telemetry and captured logs.

### Try dry-run mode (no network)

```powershell
cd C:\Projects\workflowweaver\backend
$env:INTEGRATION_MODE = "dry_run"
$env:NOTION_DESTINATIONS = '{"engineering": "00000000-0000-4000-8000-000000000001"}'
$env:NOTION_DEFAULT_PAGE_PARENT = "engineering"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# Then: GET /api/v1/integrations/status, and POST /api/v1/workflows/execute with
# "approval": {"approved": true, "mode": "dry_run", "approved_by": "synthetic.reviewer"}
```

The UI always sends `mode: "mock"`, so with the server in `dry_run` or `real` the UI's executions are rejected with 409. Use the API directly for those modes in 5.1.

### Contract verification (Phase 5.2)

| Integration | Selected server | How it was verified | Status |
|---|---|---|---|
| Notion | Self-hosted `@notionhq/notion-mcp-server` **2.6.1** (npm integrity recorded in the fixture), `--transport http` | **Live** `initialize` + `tools/list` over Streamable HTTP on 127.0.0.1 with **our own** transport/client, no Notion token, no `tools/call`. Error format read from the same version's source (`proxy.ts`) | **Verified**: 24 tools; `API-post-page` (required `parent`, `properties`; `children` of block objects) and `API-patch-page` (required `page_id`; `in_trash`). Responses: Notion JSON as one text item. Not verified: behaviour against a real workspace |
| Google Drive | Official Google-hosted server `https://drivemcp.googleapis.com/mcp/v1` | Official reference documentation only (developers.google.com) | **Unverified live**: needs OAuth 2.0 and Developer Preview membership. Documented tools used: `create_file`, `search_files`, `read_file_content`; no delete/trash tool exists |
| Microsoft Teams | Workflows (Power Automate) incoming webhook | Official Microsoft Learn documentation only | **Unverified live**: payload shape matches the official example; success status (200/202) and whether 2xx means "posted" are **not documented**, so results report `accepted`, not `delivered` |

Sanitized fixtures live in `backend/tests/fixtures/` (`mcp/notion_mcp_2.6.1_tools_list.json` captured live; `mcp/google_drive_mcp_documented_tools.json` and `teams_workflows_webhook.json` transcribed from documentation, each with a `_provenance` block). `tests/test_integration_contracts.py` validates the arguments the adapters build against these schemas with `jsonschema`, checks drift detection, error mapping, allowlist narrowing and the verification script.

#### Changes made by verification

- Notion: block objects match the strict `paragraphBlockRequest` schema (5.1 sent an extra `object` key); database parents use `{"type": "database_id", ...}`; rollback uses `in_trash` (`archived` is deprecated); API errors are detected and classified (5.1 would have reported them as malformed results); destination ids must be UUIDs.
- Google Drive: 5.1 placeholder tools replaced by the documented `create_file` (`title`, `textContent`, `contentMimeType: text/plain`, `parentId`), `search_files` (`title = '…' and (parentId = '…')`, escaped literals, `excludeContentSnippets: true`) and `read_file_content` (`fileId` → `fileContent`). Created documents are irreversible.
- Teams: card adds `contentUrl: null` and uses Adaptive Card 1.2 as in the official example; posts to one webhook are spaced to ≤ 4/s (documented throttle); retired connector URLs are refused; `sig=` signatures are redacted from logs and traces.
- Tool names are no longer configurable (`NOTION_TOOL_*`, `GOOGLE_DRIVE_TOOL_*` removed); allowlists may only narrow the verified set.

#### Smoke tests (Phase 5.3)

`scripts/smoke_integrations.py` runs an offline **preflight checklist** by default. `--read` runs **read-only** connectivity checks. `--write <integration> --i-approve-real-writes` writes **one synthetic item to a sandbox destination** (`SMOKE_TEST_*` aliases), **reads it back** to verify the remote result, then rolls back where possible. Drive and Teams rollback is reported as `NOT_SUPPORTED`, and Teams delivery as `NOT_VERIFIABLE`. The checklist, exact setup steps and verification matrix are in [docs/INTEGRATION_SMOKE_TESTS.md](docs/INTEGRATION_SMOKE_TESTS.md). **As of 2026-11-03 no integration is configured on this machine, so no smoke test has run against a real service.**

#### Live verification (opt-in)

```powershell
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\python.exe -m scripts.verify_integrations          # plan only; contacts nothing
.\.venv\Scripts\python.exe -m scripts.verify_integrations --live   # MCP initialize + tools/list + contract check; never tools/call
# Posts ONE synthetic message (real, irreversible) - only with both flags:
.\.venv\Scripts\python.exe -m scripts.verify_integrations --live --teams-post-test <alias> --i-approve-a-real-teams-post
```

The script prints statuses, tool names and error codes only, never URLs, tokens, ids or content. Notion/Drive writes are not offered by the script; they run only through the authorized execution API.

### Remaining blockers and limitations

- **Google Drive auth (blocker):** the official server accepts OAuth 2.0 access tokens only. `GOOGLE_DRIVE_MCP_AUTH_TOKEN` holds a short-lived access token; there is no refresh-token flow, and Developer Preview membership is required. Drive cannot be used unattended until a token provider is added and the contract is verified live.
- **Google Drive rollback:** no delete/trash tool, so created documents remain after a failed workflow.
- **Notion server support:** Notion marks the self-hosted server "no longer actively maintained" and may sunset it; the hosted server (`mcp.notion.com`) uses OAuth and different tools (`notion-create-pages`, …) and is not supported. Pin 2.6.1 or re-run `--live` verification after upgrading.
- **Notion behaviour against a real workspace** (permissions, `create_task` title property named `Name`, page sharing) has not been exercised; `create_task` writes the title only and puts assignee / due date in the page body.
- **Teams:** delivery is not confirmable from the response; only trigger authentication "Anyone" is supported; no message id or deduplication exists (a stable synthetic id is derived).
- The idempotency ledger and execution results are in memory and per process; a restart loses them, so a real write cannot be reconciled after a crash. Durable storage is required before production use.
- The operator token is a single shared secret, not per-user authentication. SSE streaming remains disabled when `APP_ENV=production`.

## Notion Sandbox Test from the UI (Phase 5.4)

The UI offers **Mock / Dry Run / Real**. A mode is selectable only if the backend allows it (`available_modes` from `/api/v1/integrations/status`). **Real** additionally needs `notion_sandbox.real_ready` (Notion MCP configured, `SMOKE_TEST_NOTION_PARENT` an approved alias, the three Notion tools allowlisted, `REAL_WRITES_ENABLED=true`, an operator token configured) **and** a passing read-only connectivity check, which the UI runs once on load and on demand. Real mode runs only the Notion Sandbox Test. The three templates and free-form instructions stay available in Mock and Dry Run.

**Notion Sandbox Test**: `POST /api/v1/workflows/notion-sandbox/plan` builds one `notion.create_page` step with synthetic title/content under the `SMOKE_TEST_NOTION_PARENT` alias. It is built by the server (no LLM) and passes the normal validator. It needs no Google Drive or Teams. The UI shows the exact plan, operation, destination alias, rollback behaviour and the exact request body before approval. In real mode the approver must enter a name, tick the confirmation, type `CREATE NOTION PAGE`, and enter the operator token (sent once as `X-Integration-Authorization`, never stored). Execution uses the normal `/executions` endpoint, SSE stream and LangGraph graph, so every backend gate still applies. The report shows the page id, title, destination alias and the Notion link. A **successful run keeps the page**; delete it in Notion afterwards.

## Google Drive integration (Phase 6.1)

`GOOGLE_DRIVE_TRANSPORT=rest` switches Google Drive to the Drive REST API v3 with OAuth 2.0. The scope is **`drive.file` only**: the app can see and change only files and folders it created. Setup, scopes and token handling are in [docs/GOOGLE_DRIVE_SETUP.md](docs/GOOGLE_DRIVE_SETUP.md). The default stays `mcp` (Phase 5 behaviour).

| Plan action | Effect | What happens in real mode |
|---|---|---|
| `google_drive.create_folder` (`name`, `folder` \| `parent_step`) | reversible | `files.create` folder inside the sandbox folder (or a folder of this run) |
| `google_drive.create_document` (`title`, `content`, `folder` \| `parent_step`) | reversible | multipart upload (`text/markdown`), then `files.get` **verification** of parent, size and md5. On a mismatch, the upload is trashed and the step fails |
| `google_drive.list_files` (`folder` \| `parent_step`) | read-only | `files.list` of that folder |
| `google_drive.read_document` (`document_name`) | read-only | exact-name match in the sandbox folder, size reported (never the content) |

- **No delete action exists.** A plan, AI-generated or not, cannot remove anything. Trash is used only by Saga rollback, after the adapter re-reads the file and confirms it carries this run's private `workflowweaver_key` app property, and by the confirmed cleanup script. Nothing is ever permanently deleted.
- **Sandbox confinement** (`GOOGLE_DRIVE_REAL_WRITES_SANDBOX_ONLY=true`): every step works in `SMOKE_TEST_DRIVE_FOLDER` or a folder created earlier in the same run. Before the first real call, the sandbox folder must be a live folder with the `workflowweaver_sandbox` app property. Only this OAuth client can set that property, via the setup script. Plans name folders by alias only, so raw ids are refused.
- **Gates unchanged:** approval, `REAL_WRITES_ENABLED`, the operator token, mode ceiling and preflight run before any Drive call. A Gemini or Groq plan goes through the same catalog allowlist, validator and preflight.
- **Idempotency and retries:** each created resource carries its step's idempotency key as a private app property. A retry after a timeout or 5xx first looks the key up and reuses the existing file, so there are no duplicates. 429, `rateLimitExceeded`, 5xx and timeouts are retried with backoff. 401, 403 and 404 are not.
- **Mock and dry run:** mock runs the same three actions in memory. Dry run builds every request, including the report's md5 and size, with no network and no token.
- **Observability:** SSE step and compensation events are unchanged. Langfuse gets one `google_drive.<operation>` tool span per API call, carrying operation and HTTP status only. Access tokens (`ya29.`), refresh tokens (`1//`), client secrets (`GOCSPX-`) and OAuth `code=` values are scrubbed from logs and traces.

UI: **Google Drive Sandbox Test** shows the scope and authorization state and runs the read-only connectivity check. Its plan is built by the server, never by an LLM, so it works even when Groq and Gemini are unavailable. It has exactly three steps: a unique run folder in the sandbox, the Markdown report uploaded into it (verified after upload), and the report read back and checked against the uploaded content (`read_document` with `content_sha256`; a mismatch fails the run and rolls back this run's folder and report). Planning never writes to Drive. When an AI project plan is under review, that plan becomes the report. Real approval needs the phrase `UPLOAD TO DRIVE`, the approver and the operator token.

## Integration Target (integration-specific planning)

The UI's **Integration Target** selector restricts which integrations a plan may use. The default is **Google Drive Only**, so the sandbox test never plans or creates a Notion page.

| Target | UI shows | Server allows |
|---|---|---|
| Google Drive Only (default) | Google Drive Sandbox Test (server-built, no LLM: create_folder, create_document with a synthetic Markdown report, read_document with content verification), custom instructions | `google_drive` tools only |
| Notion Only | AI Project Plan, Notion Sandbox Test (unchanged), custom instructions | `notion` tools only |
| Microsoft Teams Only | custom instructions; disabled until Teams Workflows webhooks are configured | `teams` tools only |
| Multi-Integration (future) | everything, including the mixed templates | any supported tool (previous behaviour) |

Enforcement is server-side:
- **Planning.** `POST /api/v1/workflows/plan` accepts `target`. The planner prompt (Gemini, Groq or Claude, including after a fallback) lists only the target's tools. The LangGraph **validator** rejects any step outside the target with `INTEGRATION_NOT_ALLOWED`, for every planner including mock. Each planning endpoint returns `integration_target` and records it server-side by `workflow_id`.
- **Execution.** `plan.integration_target`, `approval.integration_target` and the recorded target must agree; otherwise the run is rejected with 403 `INTEGRATION_TARGET_MISMATCH` before anything runs. The tool allowlist is re-validated in `prepare` (422 `INVALID_PLAN` / `INTEGRATION_NOT_ALLOWED`) and again before every tool call. Approval, the operator token, the kill switch, sandbox destinations, idempotency, retries, rollback, SSE and Langfuse are unchanged.
- **Approval UI.** It lists the exact actions being approved, names the target, and words mock and dry-run copy per target, so a Drive-only plan never mentions Notion. It refuses to offer approval for a plan whose steps leave its target. Requests without a target keep the previous, unrestricted behaviour.

### Real-mode AI Google Drive workflows

With **Real** mode and **Google Drive Only** selected, the custom-instruction form plans real Drive workflows. It's enabled only when the Drive sandbox is ready, the read-only Drive check passes and an AI planner is configured.
- **Planning:** `POST /api/v1/workflows/plan` with `{"mode": "real", "target": "google_drive"}` uses Groq with Gemini fallback; `PLANNER_MODE=mock` gets 503. Other targets get 422 in Real mode. The prompt lists only `create_folder`, `create_document`, `list_files` and `read_document`, plus the sandbox folder **alias**.
- **Validator, dry run and real:** every Drive step must use `folder=<SMOKE_TEST_DRIVE_FOLDER>` or `parent_step` of a `create_folder` step earlier in the same plan.
  - `DESTINATION_NOT_APPROVED`: a raw folder id, URL or any other alias.
  - `DESTINATION_REQUIRED`: no destination given.
  - Delete, trash and share actions don't exist (`UNSUPPORTED_ACTION`).
  - Notion and Teams steps are rejected with `INTEGRATION_NOT_ALLOWED`.
- **Execution:** the approved destinations are recorded with the plan and re-checked in `prepare`, independent of the adapter's sandbox setting, on top of the target check, approval, the `UPLOAD TO DRIVE` phrase, the operator token and `REAL_WRITES_ENABLED`. Idempotency, retries, rollback (trash), SSE and Langfuse are unchanged.
- **Review:** the plan screen lists every step's Drive destination and the number of write and read actions. Approval lists the exact actions.
- **Legacy API:** requests without a `target` keep their previous behaviour.

## Gemini planner

`PLANNER_MODE=gemini` swaps the planner behind the same LangGraph planning graph. The Claude planner is unchanged.
- **Call:** `app/agents/planners/gemini.py` uses the official Google Gen AI SDK (`google-genai==2.34.0`): `client.models.generate_content` with `response_mime_type="application/json"` and `response_schema` set to the existing Pydantic plan schema (`DraftPlan`, or `ProjectPlanDraft` for the Notion project workflow).
- **Unchanged downstream:** the reply is re-validated against the same model and then passes through the same validator, retries, approval flow, Notion sandbox restrictions, execution, SSE and Langfuse.

**Errors** map to the codes the Claude planner already uses:

| Gemini outcome | Code | Retried by the graph |
|---|---|---|
| HTTP 429 (rate limit / quota) | `PLANNER_RATE_LIMITED` | yes |
| HTTP 500/503/504, 408 | `PLANNER_API_ERROR` | yes |
| Timeout / connection failure | `PLANNER_TIMEOUT` / `PLANNER_CONNECTION_ERROR` | yes |
| HTTP 401/403, or 400 `API_KEY_INVALID` | `PLANNER_AUTH_ERROR` | no |
| HTTP 400/404 | `PLANNER_REQUEST_REJECTED` (check `GEMINI_MODEL`) | no |
| Invalid / empty JSON, schema mismatch, `MAX_TOKENS` truncation | `INVALID_LLM_OUTPUT` | yes |
| Safety block / prompt blocked | `PLANNER_REFUSED` | no |

- **Retries:**
  - The SDK's own retries are disabled (`HttpRetryOptions(attempts=1)`), so `MAX_PLANNING_ATTEMPTS` bounds the number of API calls per plan.
  - Between attempts, the LangGraph planner node waits with **bounded exponential backoff and equal jitter**: about 0.5–1 s, then 1–2 s, then 2–4 s, capped at `PLANNING_BACKOFF_MAX_SECONDS` (8 s). The wait applies to any planner, Claude or Gemini.
  - The waits appear in the plan response (`retry_delays_seconds`), the UI and the log (`backoff_seconds=`).
  - Non-retryable errors (auth, bad request, refusal) never wait.
- **Status names:** error reports include the HTTP code and Google's canonical status name, e.g. `Gemini API error (HTTP 503 UNAVAILABLE).`
  - Only an uppercase enum token is accepted; the provider's message and response body are never copied.
  - The same label goes to the backend log and to the Langfuse generation metadata (`http_status`, `status_name`).
- **HTTP 503 UNAVAILABLE** means Google is temporarily out of capacity for the model, not a configuration problem: the model ID, key and project access were all verified on 2026-11-04. With backoff and 3 attempts most short spikes recover. If 503s persist, set `GEMINI_MODEL=gemini-3.6-flash`.
- **Provider text:** error messages never copy the provider's response body.
- **Langfuse:** a `gemini.models.generate_content` generation records:
  - the model,
  - the latency (the observation duration),
  - token usage: `input`, `output` including thinking tokens (Gemini bills them as output), `output_thinking_tokens`, `input_cached_tokens` and `total`,
  - the finish reason, response id and model version.

  Gemini returns no cost; Langfuse derives cost from its model price table when it recognises the model. Prompt and instruction text are never sent.
- **Notion project workflow:** Gemini may generate real-mode project plans. They are labelled **"AI-generated by Gemini · <model>"** in the review.

**Model selection (2026-11-04).** One `models.list` metadata request (no generation, no tokens) against the configured project returned 60 models. **`gemini-3.8-flash`** is selected:
- It is the newest stable Flash text model offered to this project (`generateContent`, 1M input / 65K output tokens, thinking supported).
- Google's pricing page lists it with a **free tier**. Paid tier: $0.75 input / $3.75 output per 1M tokens until 2026-12-31, then $1.50 / $7.50.
- That listing does **not** prove *your project* is on the free tier, which depends on its billing setup (see Google's Billing FAQ). The pricing page also notes that free-tier content may be used to improve Google's products.
- Cheaper free-tier-listed alternatives: `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`.
- Avoid `gemini-2.5-flash`: it is deprecated and reroutes to 3.5 Flash-Lite from 2026-12-01.

### First Gemini planning test (manual, one generation request)

1. In Google AI Studio, open the API key's project and confirm it is on the **free tier**, or that you accept paid usage. Note its rate limits.
2. In `.env`, set `PLANNER_MODE=gemini` and `GEMINI_MODEL=gemini-3.8-flash`, and keep `GEMINI_API_KEY`. Leave `REAL_WRITES_ENABLED=false` for this test.
3. Start the backend and frontend (Quick start). The project card should show **AI planner: Gemini · gemini-3.8-flash**.
4. Select **Mock** mode, keep the Customer Rewards example, and click **Generate AI Project Plan (Mock)** **once**. This sends one `generate_content` request; Mock mode means nothing is written anywhere.
5. Check the review:
   - the badge reads **AI-generated by Gemini · gemini-3.8-flash**,
   - there are four action items with descriptions, priorities and teams (no people, no dates),
   - the plan is not approved yet.
6. Do **not** approve a Real run yet. A Mock approval is optional and stays in memory.
7. If Langfuse is enabled, check the generation's model, tokens and latency.

## Business navigation and Document Management (Phase 8.2)

The UI is organised by business function. A sidebar (a tab strip on small screens) links to hash routes such as `#/documents`:

| Section | What it does | Backend it reuses |
|---|---|---|
| Dashboard | Readiness of each capability, the planner order, and recent runs | `/integrations/status` |
| Document Management | AI-drafted documents in the Drive sandbox, a sandbox document library, and the Drive Sandbox Test | **new** `/workflows/documents/*`, `/integrations/google-drive/connectivity` |
| Project Management | AI Project Plan to Notion and the Notion Sandbox Test | `/workflows/notion-project/plan`, `/workflows/notion-sandbox/plan` |
| Team Communication | Teams-only notifications to the approved sandbox channel | `/workflows/plan` (`target=teams`) |
| Business Workflows | Google Drive → Notion → Teams (document only, instructions only, or both), with a Plan → Approve → Execute → Report tracker | `/workflows/orchestration/*` |
| AI Workflow Builder | Free-form instructions for one integration target, a list of supported and unsupported actions, and live warnings for unsupported tools | `/workflows/plan` |
| Execution History | Runs from **this browser tab only**: the list is lost on reload or server restart (there is no history backend) | none |
| Integrations & Settings | Read-only planner, safeguard, integration and Drive sandbox status | `/integrations/status`, read-only checks |

Every workflow section uses the same review → approval → execution → report pipeline. Mock, Dry Run and Real modes and all real-write gates are unchanged.

**Document Management** (`backend/app/agents/documents.py`, `backend/app/api/documents.py`):

- `POST /api/v1/workflows/documents/plan` takes `{document_type, title, project_name?, instruction, mode}`. Document types: requirements, technical_design, implementation_plan, api_specification, readme, meeting_notes, custom.
  - The planner chain is the usual one (auto: Groq, then Gemini on outages). The AI returns a structured `DocumentDraft` (title, summary, sections). The server validates it: size limits; no code or shell content, HTML or code blocks, URLs or email addresses; no duplicate sections; the rendered document must be at most 4,800 characters.
  - The server renders the draft to Markdown and compiles **two steps**: `google_drive.create_document` into `SMOKE_TEST_DRIVE_FOLDER` (verified after upload), then `google_drive.read_document` to read it back.
  - The response includes the exact Markdown for preview. **Nothing is written.** The request cannot choose a folder (`extra="forbid"`), and the destination comes only from server configuration.
  - The plan is recorded with its `plan_digest`, so the approval is bound to the exact reviewed content. Execution uses the normal `/executions` endpoint, with the destination allowlist, sandbox-only Drive adapter, mode ceiling, `REAL_WRITES_ENABLED`, operator token and idempotency.
- `GET /api/v1/workflows/documents/library?mode=` lists Markdown/plain-text files **directly inside the sandbox folder** that the app can access (`drive.file`). It does not browse the rest of Google Drive. `GET .../library/{file_id}?mode=` gives a read-only preview, and the file must be inside the sandbox. Mock and dry run serve synthetic fixtures and never contact Drive. Listing and preview need Drive authorization, but not the write gates.
- `GET /api/v1/workflows/documents/types` returns the supported types and their default outlines.

## Planner fallback (Groq → Gemini)

`PLANNER_MODE=auto` uses Groq (`GROQ_API_KEY`, `GROQ_MODEL`) as the primary planner and falls back to Gemini (`GEMINI_API_KEY`, `GEMINI_MODEL`) only when Groq is **unavailable**. The same order applies to workflow planning (`/workflows/plan`), the AI Project Plan workflow and Phase 7.2 document extraction (`/workflows/orchestration/plan`). The order comes from `PRIMARY_PLANNER=groq` / `FALLBACK_PLANNER=gemini` (the defaults); set them explicitly to change it.

| Groq outcome | Behaviour |
|---|---|
| Success | Groq's plan is used; Gemini is never called |
| HTTP 429 (rate limit), 500 / 502 / 503 (provider failure), 408 / 504, timeout, connection error | Retried with the normal bounded backoff (`MAX_PLANNING_ATTEMPTS`); if the **last** attempt is still such a failure, Gemini is called with its own bounded attempts |
| HTTP 401 / 403 / invalid key, 400 / 404 / 413 / 422 | Fails immediately: no retry, **no fallback** |
| Refusal / unsafe content | Fails: **no fallback** |
| Output not matching the schema | Retried on Groq, **no fallback** |
| Plan rejected by the validator (unknown tool, unsafe argument, ...) | `INVALID`: **no fallback** |

- Groq uses strict `json_schema` structured outputs derived from the same Pydantic models (`DraftPlan`, `ProjectPlanDraft`), the same system prompts and tool allow-list, and its reply goes through the same validators. Each plan comes whole from one provider: outputs are never combined.
- If both providers fail, the API returns `502 FAILED` with `ALL_PLANNERS_FAILED` and no steps. Nothing is executed. Planning never executes anything anyway: approval, the real-write operator token, the kill switch, sandbox parent confinement, idempotency and rollback apply unchanged to a fallback plan.
- Responses report `planner` (the provider actually used), `planner_model`, `primary_planner`, `fallback_used`, `fallback_reason` and `provider_attempts` (provider, model, attempt, error code, HTTP status). The UI shows the provider and model, a **Fallback** badge, and the attempt log.
- Langfuse: each provider call is its own generation (`gemini.models.generate_content`, `groq.chat.completions.create`), plus a `planning.provider_attempt` event per call, a `planning.fallback` event and node, and the attempt summary on `planning.request`. Only codes, HTTP statuses, models and token counts are recorded. Prompts, provider error text and keys are never recorded. Groq (`gsk_…`) and Google (`AIza…`) key patterns are also scrubbed.
- If `GEMINI_API_KEY` or `GEMINI_MODEL` is missing, auto mode still plans with Groq, and `GET /api/v1/integrations/status` reports `fallback_ready: false` with the missing setting. If Groq (the primary) is not configured, auto mode reports the missing Groq setting.

## AI-to-Notion business workflow (Phase 5.3 business)

One complete business workflow, end to end, Notion only (no Google Drive or Teams):

1. **Request** (React UI, "AI Project Plan to Notion"): plain-English requirements, prefilled with the Customer Rewards Platform example.
2. **Plan** (`POST /api/v1/workflows/notion-project/plan`): Claude (structured outputs, `ProjectPlanDraft`) proposes a project title, an overview and **one action item per requested feature** with a description, a suggested priority (High/Medium/Low) and a suggested owning **team**. It runs through the existing LangGraph planning graph (retries, Langfuse generation span with lengths and counts only).
3. **Business validation** (`app/agents/project_workflow.py`). The server rejects the plan (422 `INVALID`, shown in the UI, not approvable) if:
   - an owner is not a team (`OWNER_NOT_A_TEAM`); named people are never accepted,
   - it contains a date or deadline (`DEADLINE_NOT_ALLOWED`),
   - it has more action items than `MAX_WORKFLOW_STEPS - 1`, duplicate items, empty or oversized fields, or code or shell content.
4. **Compile**: step 1 is `notion.create_page`, the project page under the `SMOKE_TEST_NOTION_PARENT` alias, with the overview and a bulleted list of the action items. Steps 2..N are one `notion.create_page` per action item **inside the project page** (`parent_step: "1"`), each holding the description and bullets for suggested priority, suggested owner and project. The normal plan validator then checks it.
5. **Review**: the UI shows the AI-generated badge with the model, the project, a table of action items, the exact destination alias, the operation, the rollback behaviour and the exact execution request.
6. **Approve**: Real mode requires a name, the typed phrase `CREATE NOTION PAGE`, the operator token, and the checkbox "approve creating N real Notion pages".
7. **Execute**: the existing `/executions` endpoint, LangGraph execution graph, SSE stream and Langfuse spans. Every gate applies: server mode ceiling, approval, `REAL_WRITES_ENABLED`, operator token, approved destination, and the new sandbox-only guard.
   - Each step card shows its page title, status, attempts, retries, last error and **page ID** as it completes.
   - Transient Notion errors (429/409/5xx before sending) are retried.
   - If a step fails, the pages already created are moved to trash newest first (`API-patch-page in_trash=true`).
8. **Report**: status, errors, retries, compensation, and every created page with its ID and **clickable Notion link**.

**Verified contract only.** Pages use only `API-post-page`, with `parent: {page_id}`, `properties.title` and the two block types in the live-captured 2.6.1 schema: `paragraph` and `bulleted_list_item`. Rollback uses `API-patch-page {page_id, in_trash}`. Tests validate every generated block against the strict schema definitions from that fixture.

**Child pages** (`parent_step`): a page may be created inside a page created by an **earlier step of the same run**. The validator requires the step to be a `notion.create_page` the child `depends_on`. The executor passes that step's page ID from server state (`ToolCall.upstream`); the client can never supply a raw page ID.

**Sandbox-only** (`NOTION_REAL_WRITES_SANDBOX_ONLY=true`, default): in dry-run and real mode:
- top-level Notion pages are allowed only under `SMOKE_TEST_NOTION_PARENT`,
- child pages only inside pages created by the same run,
- task-database writes are refused before any call (`DESTINATION_NOT_APPROVED`).

**Planner honesty**:
- Real-mode project plans **require Claude**. A missing `PLANNER_MODE=claude`, `ANTHROPIC_MODEL` or `ANTHROPIC_API_KEY` returns 503 naming the setting, and the UI disables the button with that reason.
- With `PLANNER_MODE=mock`, mock and dry-run plans come from a keyword stub labelled **"Mock planner (not AI)"** everywhere.
- `ANTHROPIC_API_KEY` is now read from `.env` as well as the shell. Previously a key only in `.env` never reached the SDK. It is registered for log redaction and is never returned by `/integrations/status`, which reports only `planner.claude_ready` and the setting name of any problem.

### Local setup

`C:\Projects\workflowweaver\.env` (in addition to the Notion settings that passed the smoke test):

```
APP_ENV=development
PLANNER_MODE=claude
ANTHROPIC_API_KEY=<key from the Claude Console>
ANTHROPIC_MODEL=<model id, e.g. claude-opus-5-5>
INTEGRATION_MODE=real
REAL_WRITES_ENABLED=true
REAL_WRITE_AUTHORIZATION_TOKEN=<long random string>
NOTION_MCP_URL=http://127.0.0.1:3000/mcp
NOTION_MCP_AUTH_TOKEN=<the MCP server's AUTH_TOKEN>
NOTION_MCP_TOOL_ALLOWLIST=API-post-page,API-patch-page,API-retrieve-a-page
NOTION_DESTINATIONS={"sandbox": "<sandbox page id>"}
SMOKE_TEST_NOTION_PARENT=sandbox
NOTION_REAL_WRITES_SANDBOX_ONLY=true
MAX_WORKFLOW_STEPS=10
```

Do not set `INTEGRATION_MODE`, `PLANNER_MODE` or `ANTHROPIC_*` in the shell unless you mean to: shell variables override `.env`.

```powershell
# Terminal 1 - Notion MCP server (holds the Notion integration token; the backend never sees it)
$env:NOTION_TOKEN = "<ntn_... integration token>"
$env:AUTH_TOKEN   = "<same value as NOTION_MCP_AUTH_TOKEN>"
npx @notionhq/notion-mcp-server@2.6.1 --transport http --port 3000

# Terminal 2 - backend
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\python.exe -m scripts.smoke_integrations --read    # optional: read-only check
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 3 - frontend
cd C:\Projects\workflowweaver\frontend
npm run dev
```

### Manual test (first real run)

1. Open http://127.0.0.1:5173. The sidebar should show **Notion reachable**, real writes **Enabled**, and the project card **AI planner: Claude · <model>**.
2. Optional rehearsal: select **Dry Run**, then **Generate AI Project Plan (Dry Run)**, approve, and check that every step reports `API-post-page (not sent)`. This calls Claude but not Notion.
3. Select **Real**. Keep the Customer Rewards example, then click **Generate AI Project Plan (Real)**. This calls Claude only; nothing is written.
4. Review the plan:
   - the badge reads **AI-generated by Claude**,
   - the four action items (customer registration, reward balance lookup, points redemption, transaction history) each have a description, priority and owning team,
   - the destination is your sandbox alias,
   - "Real write" says yes, and the exact request uses `"parent": "sandbox"` once and `"parent_step": "1"` four times.
5. Approve: enter your name, type `CREATE NOTION PAGE`, paste `REAL_WRITE_AUTHORIZATION_TOKEN`, tick the box, then click **Create 5 Real Notion Pages**.
6. Watch the step cards fill with page IDs, then open the links in the report.
7. In Notion, under the sandbox page, find the project page. It should contain the overview, a bulleted list of the four action items, and four child "Action item: ..." pages, each with its description and bullets for priority, owner and project.
8. Clean up: delete the project page in Notion (its child pages go with it). Set `REAL_WRITES_ENABLED=false` again if you want.

## Safety guarantees

- `/plan` never executes anything; every planning response has `executed: false`. Execution is a separate, explicitly approved call to `/execute` or `/executions`, and the plan is re-validated before anything runs.
- By default (`INTEGRATION_MODE=mock`) execution uses **in-memory mock adapters only**: no network, MCP server or webhook is contacted. `dry_run` opens no connection either. `real` needs explicit server configuration, `REAL_WRITES_ENABLED=true`, an operator token per request and approved destinations before any write.
- SSE streams require a per-workflow capability token (mock-only access model, disabled when `APP_ENV=production`). The token is redacted from access and request logs.
- No secrets reach the browser bundle, SSE events or Langfuse.
- LLM output is untrusted: it is parsed into a Pydantic schema, then checked against an allow-list of integrations, actions and arguments.
- Steps containing code or shell patterns (`rm -rf`, `$(...)`, `powershell`, `<script>`, `os.system(`, ...) are rejected.
- Step count and planning retries are capped; dependencies may only point to earlier steps (no cycles).
- Logs contain IDs, counts and error codes only, never instruction text, argument values or secrets.
- All test data is synthetic.

## Known limitations

Real integrations are contract-verified but have not written to a real service (see [Remaining blockers and limitations](#remaining-blockers-and-limitations)), in-memory single-process state (no persistence, no multi-worker SSE), capability-token access model rather than user authentication, sequential step execution, and no failure-demo mode in the UI. The complete list and Phase 5 recommendations are in [docs/ARCHITECTURE.md §17-18](docs/ARCHITECTURE.md#17-known-limitations).
