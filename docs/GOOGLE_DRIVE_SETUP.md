# Google Drive integration setup (Phase 6.1)

WorkflowWeaver writes to Google Drive through the **Drive REST API v3** with **OAuth 2.0 for installed applications** (a "Desktop app" client, loopback redirect, PKCE). It requests exactly one scope:

| Scope | Classification | What the app can do |
|---|---|---|
| `https://www.googleapis.com/auth/drive.file` | Non-sensitive | See, create, change and trash **only files and folders this app created** (or that you explicitly open with it). Nothing else in your Drive is visible to it. |

The scope is fixed in code. A token granted with any broader or additional scope (`drive`, `drive.readonly`, `drive.metadata`, ...) is refused: it isn't saved by the auth script and it's rejected at refresh time.

## Why not the alternatives

| Option | Why it is not used |
|---|---|
| Official Drive MCP server (`drivemcp.googleapis.com`, Phase 5.2 adapter) | Developer Preview, never live-verified here, and it has no folder-creation, trash or delete tool, so uploads can't be rolled back. It remains available as `GOOGLE_DRIVE_TRANSPORT=mcp`. |
| Service account | Service accounts have no storage quota for My Drive uploads. They need a Shared Drive or Workspace domain-wide delegation, which is far broader than a personal sandbox folder. |
| `drive` (full) scope | Unrestricted access to every file. Restricted scope; not needed. |

## 1. Google Cloud Console (one time, by you)

1. Open <https://console.cloud.google.com/>, then create a project or select one, e.g. `workflowweaver-dev`.
2. **APIs & Services → Library → Google Drive API → Enable.**
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   - User type **External** (or **Internal** for a Workspace org), publishing status **Testing**.
   - App name e.g. `WorkflowWeaver (dev)`, your support email.
   - **Data access / Scopes:** add only `.../auth/drive.file`.
   - **Audience / Test users:** add the Google account whose Drive you will use.
   - In Testing status, refresh tokens expire after **7 days**. The server then reports `GOOGLE_OAUTH_REAUTH_REQUIRED`: run the authorize step again.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID → Application type: Desktop app.** Copy the client ID and client secret. No redirect URI is needed: Desktop clients accept `http://127.0.0.1:<any port>`.

## 2. `.env` (repository root)

```dotenv
GOOGLE_DRIVE_TRANSPORT=rest
GOOGLE_OAUTH_CLIENT_ID=<id>.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=<secret>
# optional; must be OUTSIDE the repository. Default: %LOCALAPPDATA%\WorkflowWeaver\google-drive-token.json
GOOGLE_DRIVE_TOKEN_FILE=
GOOGLE_DRIVE_REAL_WRITES_SANDBOX_ONLY=true
```

Leave `INTEGRATION_MODE`, `REAL_WRITES_ENABLED` and the Notion settings as they are.

## 3. Authorize (opens your browser; one consent)

```powershell
cd backend
.\.venv\Scripts\python.exe -m scripts.google_drive_auth status      # offline check
.\.venv\Scripts\python.exe -m scripts.google_drive_auth authorize   # consent screen, drive.file only
```

The script opens a loopback listener on `127.0.0.1` on a random port, plus a random `state` and a PKCE verifier. It exchanges the code and saves **only the refresh token, granted scope and client id** to the token file. The client secret and access tokens are never written to disk. Nothing the script prints contains a token, the authorization code or the secret.

## 4. Create the sandbox folder (one real write)

```powershell
.\.venv\Scripts\python.exe -m scripts.google_drive_sandbox create-folder --confirm
```

This creates **one** folder, `WorkflowWeaver Sandbox`, in My Drive with the private app property `workflowweaver_sandbox=true`. Add what it prints to `.env`:

```dotenv
GOOGLE_DRIVE_DESTINATIONS={"sandbox": "<printed folder id>"}
SMOKE_TEST_DRIVE_FOLDER=sandbox
```

A folder you create by hand in the Drive UI won't work, by design. Under `drive.file` it's invisible to the app, and it lacks the marker.

## 5. Verify (read-only)

```powershell
.\.venv\Scripts\python.exe -m scripts.google_drive_sandbox check
```

Or use **Check Drive connectivity** in the UI, or `POST /api/v1/integrations/google-drive/connectivity`. All three refresh an access token, read the sandbox folder's metadata and marker, and list its children. They never write.

## 6. Real run, then cleanup

1. Set `INTEGRATION_MODE=real`; `REAL_WRITES_ENABLED` and `REAL_WRITE_AUTHORIZATION_TOKEN` are already in use for Notion.
2. In the UI: **Real → Google Drive Sandbox Test → Generate Drive Sandbox Plan**. Review the plan, then approve with your name, the phrase `UPLOAD TO DRIVE` and the operator token.
3. Remove test runs: `python -m scripts.google_drive_sandbox cleanup` shows a count; add `--confirm` to move WorkflowWeaver run folders to Drive trash (recoverable for 30 days).

## Token handling summary

| Secret | Where it lives | Protection |
|---|---|---|
| Client secret | `.env` only | `SecretStr`; registered for redaction; never in the token file, API, SSE, traces or logs |
| Refresh token | Token file outside the repo | Path inside the repo is refused; atomic write; `0600` where supported; `.gitignore` pattern; redacted |
| Access token | Process memory only | Refreshed 60 s before expiry and once after an HTTP 401; redacted |
| Authorization code | Never stored | Loopback handler doesn't log requests; `code=` is scrubbed from URL logs |

Revoke access at any time with `python -m scripts.google_drive_auth revoke`, which revokes at Google and deletes the file, or at <https://myaccount.google.com/permissions>.
