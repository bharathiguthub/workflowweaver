"""Minimal Google Drive REST API v3 client for the Phase 6.1 integration.

Only what WorkflowWeaver needs, all under the ``drive.file`` scope (the app sees only files
it created): get metadata, list a folder's children, find a file by our idempotency marker,
create a folder, upload a text file (multipart), download a small text file, and move a file
to trash. Nothing here can delete permanently, change sharing or touch files the app did not
create.

Errors become ``IntegrationError`` with a fixed message: HTTP status plus Google's fixed
error ``reason`` (e.g. ``insufficientPermissions``) - never the response body, file content,
URLs with ids, or tokens. Every request is one Langfuse span ``google_drive.<operation>``
recording the operation and HTTP status only.
"""

import json
import uuid
from typing import Any

import httpx

from app.agents.execution.tools.base import ErrorKind
from app.integrations.errors import IntegrationError
from app.integrations.google.oauth import TokenProvider
from app.observability import get_telemetry

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
# appProperties are private to this OAuth client: other apps (and the Drive UI) cannot set them.
KEY_PROPERTY = "workflowweaver_key"  # idempotency key: "<workflow_id>:step-<n>"
RUN_PROPERTY = "workflowweaver_run"  # workflow id that created the resource
SANDBOX_PROPERTY = "workflowweaver_sandbox"  # "true" on the sandbox root folder (setup script)
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,parents,trashed,webViewLink,createdTime,appProperties"
MAX_LIST = 50
MAX_DOWNLOAD_BYTES = 1_000_000
_REASON_MAX = 40
_TRANSIENT_403 = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


def q_literal(value: str) -> str:
    """Single-quoted Drive query string literal (backslash and quote escaped)."""
    if any(ord(ch) < 32 for ch in value):
        raise IntegrationError(ErrorKind.PERMANENT, "INVALID_ARGUMENTS", "Names cannot contain control characters.")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _reason(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
        errors = error.get("errors") or [{}]
        reason = errors[0].get("reason") or error.get("status")
    except (ValueError, AttributeError, IndexError, TypeError):
        reason = None
    return reason if isinstance(reason, str) and reason.isalnum() and len(reason) <= _REASON_MAX else "error"


def drive_error(response: httpx.Response, *, write: bool) -> IntegrationError:
    status, reason = response.status_code, _reason(response)
    message = f"Google Drive rejected the request (HTTP {status}, {reason})."
    if status == 429 or (status == 403 and reason in _TRANSIENT_403):
        return IntegrationError(ErrorKind.TRANSIENT, "GOOGLE_DRIVE_RATE_LIMITED", message)
    if status >= 500:
        # The write may have been applied before the server failed.
        return IntegrationError(ErrorKind.TRANSIENT, "GOOGLE_DRIVE_API_ERROR", message, request_sent=write)
    if status in (401, 403):
        return IntegrationError(ErrorKind.PERMISSION, "GOOGLE_DRIVE_PERMISSION_DENIED", message)
    if status == 404:
        return IntegrationError(ErrorKind.PERMANENT, "GOOGLE_DRIVE_NOT_FOUND", message)
    return IntegrationError(ErrorKind.PERMANENT, "GOOGLE_DRIVE_API_ERROR", message)


class DriveClient:
    def __init__(self, tokens: TokenProvider, http: httpx.Client) -> None:
        self._tokens = tokens
        self._http = http

    # ----- transport -----

    def _request(self, operation: str, method: str, url: str, *, write: bool = False, **kwargs: Any) -> httpx.Response:
        with get_telemetry().observe(f"google_drive.{operation}", as_type="tool",
                                     metadata={"operation": operation, "method": method}) as span:
            response = self._send(method, url, write=write, **kwargs)
            if response.status_code == 401:  # access token expired early or revoked: refresh once
                self._tokens.invalidate()
                response = self._send(method, url, write=write, **kwargs)
            span.update(metadata={"http_status": response.status_code})
            if response.status_code >= 400:
                raise drive_error(response, write=write)
            return response

    def _send(self, method: str, url: str, *, write: bool, **kwargs: Any) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._tokens.access_token()}"}
        try:
            return self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise IntegrationError(ErrorKind.TRANSIENT, "GOOGLE_DRIVE_TIMEOUT", "Google Drive request timed out.",
                                   request_sent=write) from exc
        except httpx.TransportError as exc:
            raise IntegrationError(ErrorKind.TRANSIENT, "GOOGLE_DRIVE_UNREACHABLE",
                                   "Could not connect to Google Drive.") from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise IntegrationError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Unexpected Google Drive response.") from exc
        if not isinstance(data, dict):
            raise IntegrationError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Unexpected Google Drive response.")
        return data

    # ----- read -----

    def get(self, file_id: str) -> dict[str, Any]:
        return self._json(self._request("files.get", "GET", f"{API}/files/{file_id}",
                                        params={"fields": FILE_FIELDS, "supportsAllDrives": "true"}))

    def list_children(self, folder_id: str, *, limit: int = MAX_LIST) -> list[dict[str, Any]]:
        data = self._json(self._request("files.list", "GET", f"{API}/files", params={
            "q": f"{q_literal(folder_id)} in parents and trashed = false",
            "fields": f"files({FILE_FIELDS})", "pageSize": min(limit, MAX_LIST), "orderBy": "createdTime",
        }))
        files = data.get("files", [])
        return [f for f in files if isinstance(f, dict)][:limit] if isinstance(files, list) else []

    def find_by_key(self, key: str) -> dict[str, Any] | None:
        """The non-trashed file this app created for idempotency key ``key``, if any."""
        data = self._json(self._request("files.list", "GET", f"{API}/files", params={
            "q": f"appProperties has {{ key={q_literal(KEY_PROPERTY)} and value={q_literal(key)} }} "
                 "and trashed = false",
            "fields": f"files({FILE_FIELDS})", "pageSize": 2,
        }))
        files = [f for f in data.get("files", []) if isinstance(f, dict)] if isinstance(data.get("files"), list) else []
        return files[0] if files else None

    def download_text(self, file_id: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> str:
        response = self._request("files.get_media", "GET", f"{API}/files/{file_id}", params={"alt": "media"})
        if len(response.content) > max_bytes:
            raise IntegrationError(ErrorKind.PERMANENT, "DOCUMENT_TOO_LARGE", "The document is too large to read.")
        return response.content.decode("utf-8", errors="replace")

    # ----- write (only files the app creates; trash, never permanent delete) -----

    def create_folder(self, name: str, parent_id: str, app_properties: dict[str, str]) -> dict[str, Any]:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id], "appProperties": app_properties}
        return self._json(self._request("files.create_folder", "POST", f"{API}/files", write=True,
                                        params={"fields": FILE_FIELDS}, json=body))

    def upload_text(self, name: str, content: bytes, mime_type: str, parent_id: str,
                    app_properties: dict[str, str]) -> dict[str, Any]:
        """multipart/related upload (metadata + media) in one request (files.create uploadType=multipart)."""
        boundary = f"workflowweaver-{uuid.uuid4().hex}"
        metadata = {"name": name, "mimeType": mime_type, "parents": [parent_id], "appProperties": app_properties}
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        return self._json(self._request(
            "files.upload", "POST", f"{UPLOAD_API}/files", write=True,
            params={"uploadType": "multipart", "fields": FILE_FIELDS},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body))

    def trash(self, file_id: str) -> None:
        """Move to trash (recoverable for 30 days). Trashing an already-trashed file succeeds."""
        self._request("files.trash", "PATCH", f"{API}/files/{file_id}", write=True,
                      params={"fields": "id,trashed"}, json={"trashed": True})

    def close(self) -> None:
        self._http.close()
