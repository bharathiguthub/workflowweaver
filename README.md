# WorkflowWeaver

FDE Capstone: an AI workflow orchestrator that turns unstructured input (meeting notes, backlogs, incident reports) into executed multi-tool workflows.

## Target architecture

| Layer | Technology |
|---|---|
| API | FastAPI (+ SSE for live progress) |
| Orchestration | LangGraph Python SDK + Claude |
| Integrations | Notion MCP, Google Drive MCP, Microsoft Teams webhook |
| Observability | Langfuse |
| Frontend | React 19 + TypeScript + Vite 8 + Tailwind CSS 4 (`frontend/`) |

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | FastAPI skeleton, templates, `PLANNED` workflow records | Done |
| 2 | LangGraph planning agent: natural language → validated plan | Done |
| 3 | LangGraph execution graph over **mock** tools: approval gate, bounded retries, idempotency, Saga compensation | **Done (mock only)** |
| 4 | Langfuse observability, SSE progress, React + Tailwind UI, integration and reliability testing (Stages 1-4 done); docs next | In progress |
| 5 | Real Notion / Drive MCP and Teams webhook adapters | Planned |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, graph flow, state model, and a worked example.

## Endpoints

| Method | Path | Phase | Description |
|---|---|---|---|
| GET | `/health` | 1 | Liveness check |
| GET | `/api/v1/workflows/templates` | 1 | Meeting-to-Tasks, Sprint Planning, Incident Response |
| POST | `/api/v1/workflows` | 1 | Validates input, returns a UUID and status `PLANNED` |
| POST | `/api/v1/workflows/plan` | 2 | Runs the LangGraph planner; returns a validated plan |
| POST | `/api/v1/workflows/execute` | 3 | Executes an approved plan against **mock** tools |
| GET | `/api/v1/workflows/executions/{workflow_id}` | 3 | Stored result of a finished execution (404 if unknown) |
| POST | `/api/v1/workflows/executions` | 4 | Start a **mock** execution in the background; returns 202 + stream URL/token |
| GET | `/api/v1/workflows/executions/{workflow_id}/events` | 4 | Live progress as Server-Sent Events (token required) |

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
| `PLANNER_MODE` | `mock` | `mock` (no key, no network) or `claude` |
| `ANTHROPIC_API_KEY` | — | Required for `claude`. Read by the Anthropic SDK only; never logged |
| `ANTHROPIC_MODEL` | — | Required for `claude` (e.g. `claude-opus-5`). No default in code |
| `ANTHROPIC_MAX_TOKENS` | `8000` | Output cap per planning call |
| `PLANNER_TIMEOUT_SECONDS` | `60` | Per-request timeout for Claude |
| `MAX_WORKFLOW_STEPS` | `10` | Plans with more steps are rejected |
| `MAX_PLANNING_ATTEMPTS` | `2` | Planner attempts for retryable failures |
| `MOCK_EXECUTION_ENABLED` | `true` | `false` makes `/execute` return 503 |
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

Claude Code's login does **not** give your application an API key. Create one in the Claude Console and set it only in your shell or in `.env`.

## Run tests

```powershell
cd C:\Projects\workflowweaver\backend
.\.venv\Scripts\python.exe -m pytest -v
```

Tests never call the Claude API or any real Notion / Google Drive / Teams service: the Claude planner is tested with a fake client, and `ANTHROPIC_API_KEY` is removed from the environment for every test.

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

## Safety guarantees

- `/plan` never executes anything; every planning response has `executed: false`. Execution is a separate, explicitly approved call to `/execute`.
- Phase 3 execution uses **in-memory mock adapters only**: no network, MCP server or webhook is contacted, and Notion/Drive/Teams credentials are not read.
- LLM output is untrusted: it is parsed into a Pydantic schema, then checked against an allow-list of integrations, actions and arguments.
- Steps containing code or shell patterns (`rm -rf`, `$(...)`, `powershell`, `<script>`, `os.system(`, ...) are rejected.
- Step count and planning retries are capped; dependencies may only point to earlier steps (no cycles).
- Logs contain IDs, counts and error codes only, never instruction text, argument values or secrets.
- All test data is synthetic.
