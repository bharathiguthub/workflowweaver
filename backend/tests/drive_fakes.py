"""In-memory fake of the Google OAuth token endpoint and the Drive REST API v3 subset that
WorkflowWeaver uses, served through ``httpx.MockTransport`` - no network, no real Google.

It enforces what the real API would: a valid bearer token, the drive.file visibility rule
(only files created by this app are visible), multipart upload parsing, appProperties
queries, md5/size computation and trash semantics. Failure injection lets tests simulate
503/429 responses, timeouts and responses that arrive after the write was applied.
"""

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools.integrations import build_integration_registry
from app.integrations.google.drive import FOLDER_MIME, SANDBOX_PROPERTY
from app.integrations.google.oauth import DRIVE_FILE_SCOPE, save_token
from tests.integration_fakes import integration_settings

# Synthetic, non-functional OAuth test values (never real credentials). The Google-shaped prefixes are
# joined at runtime so the source never contains a credential-shaped literal; the redaction tests still
# see the real shapes (GOCSPX-..., 1//...).
CLIENT_ID = "synthetic-test-client-not-real.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX" + "-SYNTHETIC-test-secret-not-real-0001"
REFRESH_TOKEN = "1/" + "/SYNTHETIC-test-refresh-token-not-real-0001"
ACCESS_TOKEN = "ya29.SYNTHETIC-access-token-not-real-0001"
SANDBOX_ID = "1SyntheticSandboxFolderId000001"
OTHER_FOLDER_ID = "1SyntheticOtherFolderId0000002"
GOOGLE_SECRETS = (CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN)


@dataclass
class FakeGoogle:
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (method, path)
    token_requests: int = 0
    # (method, path-regex) -> list of outcomes consumed in order: int status, "timeout",
    # ("after", status) = apply the write THEN answer with status (lost response)
    failures: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    token_status: int = 200
    token_error: str = "invalid_grant"
    token_scope: str = DRIVE_FILE_SCOPE
    corrupt_upload: bool = False  # report a different md5 than the bytes received
    next_id: int = 1

    def __post_init__(self) -> None:
        self.add_folder(SANDBOX_ID, "WorkflowWeaver Sandbox", parent="root", props={SANDBOX_PROPERTY: "true"})

    # ----- setup helpers -----

    def add_folder(self, file_id: str, name: str, *, parent: str, props: dict[str, str] | None = None,
                   app_created: bool = True) -> None:
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": FOLDER_MIME, "parents": [parent],
                               "trashed": False, "appProperties": dict(props or {}), "app_created": app_created}

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in {"POST", "PATCH", "DELETE"} and "oauth2" not in c[1]]

    def children(self, parent: str) -> list[dict[str, Any]]:
        return [f for f in self.files.values() if parent in f["parents"] and not f["trashed"]]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    # ----- transport -----

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            self.calls.append((request.method, f"oauth2{path}"))
            return self._token(request)
        self.calls.append((request.method, path))
        if request.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            return self._error(401, "authError")
        outcome = self._failure(request.method, path)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if isinstance(outcome, int):
            reason = {403: "insufficientPermissions", 429: "rateLimitExceeded"}.get(outcome, "backendError")
            return self._error(outcome, reason)
        response = self._route(request)
        if isinstance(outcome, tuple):  # the write happened, but the client sees an error
            return self._error(outcome[1], "backendError")
        return response

    def _failure(self, method: str, path: str):
        for (m, pattern), outcomes in self.failures.items():
            if m == method and re.search(pattern, path) and outcomes:
                return outcomes.pop(0)
        return None

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        form = parse_qs(request.content.decode())
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": self.token_error,
                                                           "error_description": "SYNTHETIC-PROVIDER-DETAIL"})
        if form.get("client_secret") != [CLIENT_SECRET] or form.get("refresh_token") != [REFRESH_TOKEN]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599,
                                         "scope": self.token_scope, "token_type": "Bearer"})

    @staticmethod
    def _error(status: int, reason: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": status, "message": "SYNTHETIC-PROVIDER-DETAIL",
                                                      "errors": [{"reason": reason}]}})

    def _visible(self, file_id: str) -> dict[str, Any] | None:
        f = self.files.get(file_id)
        return f if f is not None and f["app_created"] else None  # drive.file: app-created only

    def _route(self, request: httpx.Request) -> httpx.Response:
        method, path, params = request.method, request.url.path, request.url.params
        if method == "GET" and path == "/drive/v3/files":
            return self._list(params.get("q", ""))
        if m := re.fullmatch(r"/drive/v3/files/([^/]+)", path):
            f = self._visible(m.group(1))
            if f is None:
                return self._error(404, "notFound")
            if method == "GET" and params.get("alt") == "media":
                return httpx.Response(200, content=f.get("content", b""))
            if method == "GET":
                return httpx.Response(200, json=self._public(f))
            if method == "PATCH":
                f["trashed"] = bool(json.loads(request.content).get("trashed"))
                return httpx.Response(200, json={"id": f["id"], "trashed": f["trashed"]})
        if method == "POST" and path == "/drive/v3/files":
            body = json.loads(request.content)
            return httpx.Response(200, json=self._public(self._create(body, None)))
        if method == "POST" and path == "/upload/drive/v3/files":
            metadata, content = self._multipart(request)
            return httpx.Response(200, json=self._public(self._create(metadata, content)))
        return self._error(400, "badRequest")

    def _create(self, body: dict[str, Any], content: bytes | None) -> dict[str, Any]:
        parent = body["parents"][0]
        if parent != "root" and self._visible(parent) is None:
            raise AssertionError("parent not visible")  # the adapter must never write elsewhere
        file_id = f"1SyntheticCreated{self.next_id:06d}"
        self.next_id += 1
        f = {"id": file_id, "name": body["name"], "mimeType": body["mimeType"], "parents": [parent],
             "trashed": False, "appProperties": dict(body.get("appProperties") or {}), "app_created": True}
        if content is not None:
            f["content"] = content
        self.files[file_id] = f
        return f

    def _public(self, f: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in f.items() if k not in {"content", "app_created"}}
        out["webViewLink"] = f"https://drive.google.com/file/d/{f['id']}/view"
        if "content" in f:
            out["size"] = str(len(f["content"]))
            digest = hashlib.md5(f["content"]).hexdigest()
            out["md5Checksum"] = "0" * 32 if self.corrupt_upload else digest
        return out

    def _list(self, q: str) -> httpx.Response:
        if m := re.search(r"key='([^']+)' and value='([^']+)'", q):
            key, value = m.groups()
            found = [f for f in self.files.values()
                     if f["app_created"] and not f["trashed"] and f["appProperties"].get(key) == value]
        elif m := re.match(r"'([^']+)' in parents", q):
            found = [f for f in self.children(m.group(1)) if f["app_created"]]
        else:
            found = []
        return httpx.Response(200, json={"files": [self._public(f) for f in found]})

    @staticmethod
    def _multipart(request: httpx.Request) -> tuple[dict[str, Any], bytes]:
        boundary = request.headers["Content-Type"].split("boundary=")[1].encode()
        parts = request.content.split(b"--" + boundary)
        meta_part, media_part = parts[1], parts[2]
        metadata = json.loads(meta_part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
        media = media_part.split(b"\r\n\r\n", 1)[1]
        return metadata, media[:-2] if media.endswith(b"\r\n") else media


def drive_settings(mode: str = "real", token_dir: Path | None = None, **overrides: Any):
    values: dict[str, Any] = dict(
        google_drive_transport="rest",
        google_oauth_client_id=CLIENT_ID,
        google_oauth_client_secret=CLIENT_SECRET,
        google_drive_destinations=json.dumps({"sandbox": SANDBOX_ID, "reports": OTHER_FOLDER_ID}),
        google_drive_default_folder=None,
        smoke_test_drive_folder="sandbox",
        google_drive_real_writes_sandbox_only=True,
    )
    if token_dir is not None:
        values["google_drive_token_file"] = str(token_dir / "google-drive-token.json")
    values.update(overrides)
    return integration_settings(mode, **values)


def write_token(token_dir: Path, *, scope: str = DRIVE_FILE_SCOPE, client_id: str = CLIENT_ID) -> Path:
    path = token_dir / "google-drive-token.json"
    if scope == DRIVE_FILE_SCOPE:
        save_token(path, refresh_token=REFRESH_TOKEN, scope=scope, client_id=client_id)
    else:  # bypass save_token's own scope check to simulate a broader grant on disk
        path.write_text(json.dumps({"refresh_token": REFRESH_TOKEN, "scope": scope, "client_id": client_id}))
    return path


class DriveWorld:
    """Registry + execution service with the REST Drive adapter wired to a FakeGoogle."""

    def __init__(self, token_dir: Path, mode: str = "real", *, authorize: bool = True, **overrides: Any) -> None:
        self.google = FakeGoogle()
        if authorize:
            write_token(token_dir)
        self.settings = drive_settings(mode, token_dir, **overrides)
        self.registry = build_integration_registry(self.settings, drive_http=self.google.client())
        self.sleeps: list[float] = []
        self.service = ExecutionService(
            self.registry, RetryPolicy(max_attempts=3, base_delay_seconds=0.2, max_delay_seconds=2.0, jitter=False),
            max_steps=10, sleeper=self.sleeps.append, rng=random.Random(7))

    @property
    def adapter(self):
        from app.schemas.workflow import Integration

        return self.registry.adapters[Integration.GOOGLE_DRIVE]
