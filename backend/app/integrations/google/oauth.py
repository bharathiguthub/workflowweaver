"""Google OAuth 2.0 for the Drive REST integration (Phase 6.1).

Flow: OAuth 2.0 for installed applications ("Desktop app" client) with a loopback redirect
and PKCE (S256), run ONCE by the operator with ``scripts/google_drive_auth.py``. The only
scope ever requested is ``drive.file``: the app can see and change ONLY files and folders it
created itself (Google's narrowest Drive write scope, classified non-sensitive). Tokens
granted with any broader scope are refused.

Token handling
  refresh token  stored in a local JSON file OUTSIDE the repository (GOOGLE_DRIVE_TOKEN_FILE,
                 default %LOCALAPPDATA%\\WorkflowWeaver\\google-drive-token.json), written
                 atomically with owner-only permissions where the OS supports it
  access token   memory only; refreshed shortly before it expires; never written to disk
  client secret  from .env (GOOGLE_OAUTH_CLIENT_SECRET); never written to the token file
Every token and the client secret is registered with ``app.integrations.secrets`` so it is
scrubbed from logs and traces; no message produced here contains a token value or the
provider's error description (only Google's fixed error code, e.g. ``invalid_grant``).
"""

import base64
import hashlib
import json
import os
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.agents.execution.tools.base import ErrorKind
from app.integrations.errors import IntegrationConfigError, IntegrationError
from app.integrations.secrets import register_secret

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
TOKEN_FILE_VERSION = 1
_EXPIRY_MARGIN_SECONDS = 60
_ERROR_CODE_MAX = 40

# Repository root (C:\Projects\workflowweaver): token files may not live under it.
REPO_ROOT = Path(__file__).resolve().parents[4]


def default_token_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "WorkflowWeaver" / "google-drive-token.json"
    return Path.home() / ".config" / "workflowweaver" / "google-drive-token.json"


def resolve_token_path(raw: str | None, *, repo_root: Path = REPO_ROOT) -> Path:
    """GOOGLE_DRIVE_TOKEN_FILE (or the default), refused if it is inside the repository, so
    a refresh token can never be committed by accident. Errors name the setting only."""
    path = Path(os.path.expandvars(os.path.expanduser(raw.strip()))) if raw and raw.strip() else default_token_path()
    if not path.is_absolute():
        raise IntegrationConfigError("GOOGLE_DRIVE_TOKEN_FILE must be an absolute path outside the repository.")
    resolved, root = path.resolve(), repo_root.resolve()
    if resolved == root or root in resolved.parents:
        raise IntegrationConfigError("GOOGLE_DRIVE_TOKEN_FILE must be outside the repository (never commit tokens).")
    return resolved


def granted_scopes(scope: str | None) -> set[str]:
    return {s for s in (scope or "").split() if s}


def scope_problem(scope: str | None) -> str | None:
    """Exactly drive.file is required; anything broader (or extra) is refused."""
    scopes = granted_scopes(scope)
    if DRIVE_FILE_SCOPE not in scopes:
        return "The Google token was not granted the drive.file scope; re-run scripts/google_drive_auth.py."
    extra = scopes - {DRIVE_FILE_SCOPE}
    if extra:
        return ("The Google token carries scopes beyond drive.file; revoke it and re-run "
                "scripts/google_drive_auth.py (only drive.file is allowed).")
    return None


# ---------------- token file ----------------


@dataclass(frozen=True)
class StoredToken:
    refresh_token: str = field(repr=False)
    scope: str
    client_id: str  # which OAuth client issued it (not a secret)
    created_at: str = ""


def load_token(path: Path) -> StoredToken:
    """Read the refresh token. Raises IntegrationError (PERMISSION) when missing or invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_NOT_AUTHORIZED",
                               "Google Drive is not authorized yet: run scripts/google_drive_auth.py.") from exc
    except (OSError, ValueError) as exc:
        raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_TOKEN_INVALID",
                               "The Google Drive token file is unreadable; re-run scripts/google_drive_auth.py.") from exc
    token = data.get("refresh_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_TOKEN_INVALID",
                               "The Google Drive token file has no refresh token; re-run scripts/google_drive_auth.py.")
    register_secret(token)
    stored = StoredToken(refresh_token=token, scope=str(data.get("scope", "")),
                         client_id=str(data.get("client_id", "")), created_at=str(data.get("created_at", "")))
    if problem := scope_problem(stored.scope):
        raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_SCOPE_REJECTED", problem)
    return stored


def save_token(path: Path, *, refresh_token: str, scope: str, client_id: str) -> None:
    """Atomic write (temp file + replace) with owner-only permissions where supported.
    Stores the refresh token, granted scope and client id - never the client secret or an
    access token."""
    if problem := scope_problem(scope):
        raise IntegrationConfigError(problem)
    register_secret(refresh_token)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": TOKEN_FILE_VERSION, "refresh_token": refresh_token, "scope": scope,
               "client_id": client_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        os.chmod(path, 0o600)  # POSIX; on Windows the file is in the user's own profile
    except OSError:  # pragma: no cover
        pass


# ---------------- authorization (scripts/google_drive_auth.py) ----------------


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) per RFC 7636, S256."""
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(*, client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    """Consent URL requesting ONLY drive.file, offline access (a refresh token) and PKCE."""
    return AUTH_URI + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DRIVE_FILE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",  # never merge in scopes granted earlier
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })


def _oauth_error(response: httpx.Response, action: str) -> IntegrationError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("error") if isinstance(body, dict) else None
    code = code if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= _ERROR_CODE_MAX \
        else "unknown_error"
    if code == "invalid_grant":  # revoked, expired (7 days in Testing status) or wrong client
        return IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_REAUTH_REQUIRED",
                                "The Google refresh token was revoked or expired; re-run scripts/google_drive_auth.py.")
    if code in {"invalid_client", "unauthorized_client"}:
        return IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_CLIENT_REJECTED",
                                "Google rejected the OAuth client; check GOOGLE_OAUTH_CLIENT_ID / _SECRET.")
    kind = ErrorKind.TRANSIENT if response.status_code >= 500 or response.status_code == 429 else ErrorKind.PERMISSION
    return IntegrationError(kind, "GOOGLE_OAUTH_ERROR",
                            f"Google OAuth {action} failed (HTTP {response.status_code}, {code}).")


def exchange_code(http: httpx.Client, *, client_id: str, client_secret: str, code: str, code_verifier: str,
                  redirect_uri: str) -> dict[str, Any]:
    """Authorization code -> tokens. Returns the token response (refresh_token, scope, ...)."""
    register_secret(code)
    response = http.post(TOKEN_URI, data={
        "client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": code_verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    })
    if response.status_code != 200:
        raise _oauth_error(response, "code exchange")
    data = response.json()
    for key in ("access_token", "refresh_token"):
        register_secret(data.get(key))
    if not data.get("refresh_token"):
        raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_NO_REFRESH_TOKEN",
                               "Google returned no refresh token; revoke the app's access and authorize again.")
    if problem := scope_problem(data.get("scope")):
        raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_SCOPE_REJECTED", problem)
    return data


def revoke(http: httpx.Client, token: str) -> bool:
    """Revoke a refresh token at Google. True on success."""
    register_secret(token)
    response = http.post(REVOKE_URI, data={"token": token})
    return response.status_code == 200


# ---------------- runtime: access tokens ----------------


class TokenProvider:
    """Short-lived access tokens from the stored refresh token. Thread-safe. The token file
    is read lazily on first use, so an unauthorized server still starts (and reports it)."""

    def __init__(self, *, client_id: str, client_secret: str, token_path: Path, http: httpx.Client,
                 clock=time.monotonic) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        register_secret(client_secret)
        self._token_path = token_path
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._stored: StoredToken | None = None
        self._access: str | None = None
        self._expires_at = 0.0

    def access_token(self) -> str:
        with self._lock:
            if self._access and self._clock() < self._expires_at - _EXPIRY_MARGIN_SECONDS:
                return self._access
            return self._refresh()

    def invalidate(self) -> None:
        """Forget the cached access token (e.g. after an HTTP 401)."""
        with self._lock:
            self._access = None

    def _refresh(self) -> str:
        if self._stored is None:
            self._stored = load_token(self._token_path)
            if self._stored.client_id and self._stored.client_id != self._client_id:
                raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_CLIENT_MISMATCH",
                                       "The token file was issued to a different OAuth client; "
                                       "re-run scripts/google_drive_auth.py.")
        try:
            response = self._http.post(TOKEN_URI, data={
                "client_id": self._client_id, "client_secret": self._client_secret,
                "refresh_token": self._stored.refresh_token, "grant_type": "refresh_token",
            })
        except httpx.TimeoutException as exc:
            raise IntegrationError(ErrorKind.TRANSIENT, "GOOGLE_OAUTH_TIMEOUT", "Google token refresh timed out.") from exc
        except httpx.TransportError as exc:
            raise IntegrationError(ErrorKind.TRANSIENT, "GOOGLE_OAUTH_UNREACHABLE",
                                   "Could not reach the Google token endpoint.") from exc
        if response.status_code != 200:
            raise _oauth_error(response, "token refresh")
        data = response.json()
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise IntegrationError(ErrorKind.TRANSIENT, "GOOGLE_OAUTH_ERROR", "Google returned no access token.")
        register_secret(token)
        if "scope" in data and (problem := scope_problem(data.get("scope"))):
            raise IntegrationError(ErrorKind.PERMISSION, "GOOGLE_OAUTH_SCOPE_REJECTED", problem)
        lifetime = data.get("expires_in")
        self._access = token
        self._expires_at = self._clock() + (float(lifetime) if isinstance(lifetime, (int, float)) else 3600.0)
        return token
