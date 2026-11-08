# Integration smoke tests (Phase 5.3)

Controlled, opt-in checks against **real** Notion, Google Drive and Microsoft Teams services, run by an operator with `backend/scripts/smoke_integrations.py`. Mock mode is unaffected: nothing here runs unless you invoke the script, and writes need explicit flags.

| Phase | Command (from `backend/`) | Network | Writes |
|---|---|---|---|
| 1. Preflight checklist | `.\.venv\Scripts\python.exe -m scripts.smoke_integrations` | none | none |
| 2. Read-only connectivity | `... -m scripts.smoke_integrations --read` | MCP servers only | none |
| 3. One synthetic write | `... -m scripts.smoke_integrations --write notion --i-approve-real-writes` | yes | **one item, sandbox only** |

The script prints statuses, aliases, tool names and error codes. It never prints tokens, MCP or webhook URLs, destination ids or document contents. It prints a created item's own link only when you must delete that item by hand.

## Current status (2026-11-03)

The preflight was run against this machine's configuration: **no integration is configured**. There is no `.env`, and none of the integration variables are set in the process, user or machine environment. No read-only or write smoke test has been run against any real service, so **no real-integration result exists yet**.

## 1. Preflight checklist

`OK` and `MISSING` are checked automatically from configuration. `MANUAL` items can't be seen from this backend; you confirm them, and `--read` verifies the ones marked below.

### General
- [ ] `INTEGRATION_MODE=real` (needed for `--write` only; `--read` works in any mode)
- [ ] `REAL_WRITES_ENABLED=true` (server kill switch; needed for `--write` only)
- [ ] Configuration valid (URLs https or loopback, aliases, UUIDs, allowlists)

### Notion (self-hosted `@notionhq/notion-mcp-server@2.6.1`, contract verified live in 5.2)
- [ ] `NOTION_MCP_URL`, e.g. `http://127.0.0.1:3000/mcp`
- [ ] `NOTION_MCP_AUTH_TOKEN` = the MCP server's `AUTH_TOKEN`
- [ ] Allowlist contains `API-post-page`, `API-patch-page`, `API-retrieve-a-page`
- [ ] `SMOKE_TEST_NOTION_PARENT` = an alias in `NOTION_DESTINATIONS`
- [ ] MANUAL: `NOTION_TOKEN` (internal integration token) is set in the **MCP server's** environment
- [ ] MANUAL: the sandbox page is shared with the integration (`--read` verifies this)
- [ ] MANUAL: the sandbox page is **not** a production page

### Google Drive (official `drivemcp.googleapis.com`, contract documented only)
- [ ] `GOOGLE_DRIVE_MCP_URL=https://drivemcp.googleapis.com/mcp/v1`
- [ ] `GOOGLE_DRIVE_MCP_AUTH_TOKEN` = an OAuth 2.0 **access token** with scopes `drive.file` and `drive.readonly`
- [ ] Allowlist contains `create_file`, `search_files`, `read_file_content`, `get_file_metadata`
- [ ] `SMOKE_TEST_DRIVE_FOLDER` = an alias in `GOOGLE_DRIVE_DESTINATIONS`
- [ ] MANUAL: Google Workspace **Developer Preview Program** membership
- [ ] MANUAL: Drive API and Google Drive MCP API enabled; OAuth client with "Use this client for an AI-powered agent"
- [ ] MANUAL: the access token is unexpired (about 1 hour; `--read` verifies)
- [ ] MANUAL: the sandbox folder is **not** a production folder

### Microsoft Teams (Workflows webhook, contract documented only)
- [ ] `TEAMS_WEBHOOK_URLS` contains a Workflows webhook URL (retired `*.webhook.office.com` URLs are refused)
- [ ] `SMOKE_TEST_TEAMS_CHANNEL` = an alias in `TEAMS_WEBHOOK_URLS`
- [ ] MANUAL: workflow trigger authentication is **Anyone**
- [ ] MANUAL: the sandbox channel is **not** a production channel

## 2. Exact setup instructions

All values go in `C:\Projects\workflowweaver\.env` (copy `.env.example`; never commit `.env`). Use dedicated sandbox destinations only.

### Notion
1. In Notion, create a **new sandbox page** (for example "WorkflowWeaver Sandbox") that contains nothing you need.
2. At https://www.notion.so/profile/integrations create an **internal integration** and copy its token (`ntn_...`). Give it the Read content, Update content and Insert content capabilities.
3. On the sandbox page, open **••• → Connections** and add the integration. Copy the page id, the 32-hex value at the end of the page URL.
4. In a separate terminal, start the MCP server. Its environment holds the Notion token; this backend never sees that token.
   ```powershell
   $env:NOTION_TOKEN = "<ntn_... integration token>"
   $env:AUTH_TOKEN   = "<long random string>"   # python -c "import secrets; print(secrets.token_urlsafe(32))"
   npx @notionhq/notion-mcp-server@2.6.1 --transport http --port 3000
   ```
5. In `.env`:
   ```
   NOTION_MCP_URL=http://127.0.0.1:3000/mcp
   NOTION_MCP_AUTH_TOKEN=<the same AUTH_TOKEN>
   NOTION_DESTINATIONS={"sandbox": "<sandbox page id>"}
   SMOKE_TEST_NOTION_PARENT=sandbox
   ```

### Google Drive
1. Join the **Google Workspace Developer Preview Program** with the Google account you will use. Without it the Drive MCP server rejects requests, and this cannot be worked around.
2. In a Google Cloud project, enable the **Google Drive API** and the **Google Drive MCP API**.
3. Configure the OAuth consent screen with scopes `https://www.googleapis.com/auth/drive.file` and `https://www.googleapis.com/auth/drive.readonly`. Create an OAuth client and select **"Use this client for an AI-powered agent"**.
4. In Google Drive, create a **new empty sandbox folder** and copy its id from the folder URL.
5. Obtain an OAuth 2.0 **access token** for those scopes through that OAuth client, for example with the OAuth 2.0 Playground configured to "Use your own OAuth credentials". Other token sources are untested and may be rejected if they don't use the "AI-powered agent" client. The token expires in about 1 hour; there is no refresh flow yet.
6. In `.env`:
   ```
   GOOGLE_DRIVE_MCP_URL=https://drivemcp.googleapis.com/mcp/v1
   GOOGLE_DRIVE_MCP_AUTH_TOKEN=<access token>
   GOOGLE_DRIVE_DESTINATIONS={"sandbox": "<sandbox folder id>"}
   SMOKE_TEST_DRIVE_FOLDER=sandbox
   ```

### Microsoft Teams
1. Create a **sandbox team or channel** used only for testing.
2. In that channel, open **••• → Workflows** and choose the template **"Send webhook alerts to a channel"**. Set trigger authentication to **Anyone**, save, and copy the generated URL. The URL is a credential.
3. In `.env`:
   ```
   TEAMS_WEBHOOK_URLS={"sandbox": "<workflow URL>"}
   SMOKE_TEST_TEAMS_CHANNEL=sandbox
   ```

### Enable writes (only when you are ready for phase 3)
```
INTEGRATION_MODE=real
REAL_WRITES_ENABLED=true
```

## 3. What each phase verifies

| Integration | `--read` (read-only) | `--write` (one synthetic item) | Remote verification | Rollback |
|---|---|---|---|---|
| Notion | `initialize` + `tools/list` + contract check; `API-retrieve-a-page` on the sandbox page | Page titled `WorkflowWeaver smoke test <run id>` under the sandbox page, through the production adapter | **Read back** with `API-retrieve-a-page`: live page, parent is the sandbox page, title matches | Moved to trash with `API-patch-page`, then **read back** to confirm `in_trash: true` |
| Google Drive | `initialize` + `tools/list` + contract check; `get_file_metadata` on the sandbox folder; `search_files` scoped to it | Google Doc with synthetic content in the sandbox folder | **Read back** with `get_file_metadata` (title, parent folder) and `read_file_content` (contains the run id; content is not printed) | **NOT SUPPORTED.** The official server has no delete/trash tool; the script prints the title and link for manual deletion |
| Teams | **NOT VERIFIABLE**: a webhook has no read-only operation | One synthetic message to the sandbox channel | **NOT VERIFIABLE** through a webhook: a 2xx means the workflow was triggered. Confirm the run id in the channel and the workflow's run history | **NOT SUPPORTED.** Webhook messages cannot be deleted |

A write runs only if all of these hold:
- `--i-approve-real-writes` is given.
- `INTEGRATION_MODE=real` and `REAL_WRITES_ENABLED=true`.
- That integration's preflight and read-only checks passed in the same run.
- A `SMOKE_TEST_*` sandbox alias is configured.

Writes always target the sandbox alias, never the default destination. Exit codes: `0` no failures, `1` a check failed, `2` a requested write was blocked.
