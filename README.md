# WorkflowWeaver

FDE Capstone: an AI workflow orchestrator that turns unstructured input (meeting notes, backlogs, incident reports) into executed multi-tool workflows.

## Target architecture

| Layer | Technology |
|---|---|
| API | FastAPI (+ SSE for live progress) |
| Orchestration | LangGraph Python SDK + Claude |
| Integrations | Notion MCP, Google Drive MCP, Microsoft Teams webhook |
| Observability | Langfuse |
| Frontend | React + Tailwind CSS |

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | FastAPI skeleton, templates, `PLANNED` workflow records | Done |
| 2 | LangGraph planning agent: natural language → validated plan | Done |
| 3 | LangGraph execution graph over **mock** tools: approval gate, bounded retries, idempotency, Saga compensation | **Done (mock only)** |
| 4 | Real Notion / Drive MCP and Teams webhook adapters, SSE progress, Langfuse | Next |

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

`POST /api/v1/workflows/plan` status codes:

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `PLANNED` | Plan generated and validated. **Not executed.** |
| 422 | `INVALID` | Plan generated but rejected by the validator; see `validation.issues` |
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

Synthetic Drive documents available to `read_document`: `Synthetic standup notes`, `Synthetic sprint backlog`, `Synthetic incident report` (or any document created earlier by `create_document`).

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

## Safety guarantees

- `/plan` never executes anything; every planning response has `executed: false`. Execution is a separate, explicitly approved call to `/execute`.
- Phase 3 execution uses **in-memory mock adapters only**: no network, MCP server or webhook is contacted, and Notion/Drive/Teams credentials are not read.
- LLM output is untrusted: it is parsed into a Pydantic schema, then checked against an allow-list of integrations, actions and arguments.
- Steps containing code or shell patterns (`rm -rf`, `$(...)`, `powershell`, `<script>`, `os.system(`, ...) are rejected.
- Step count and planning retries are capped; dependencies may only point to earlier steps (no cycles).
- Logs contain IDs, counts and error codes only, never instruction text, argument values or secrets.
- All test data is synthetic.
