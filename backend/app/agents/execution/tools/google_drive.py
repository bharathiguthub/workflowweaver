"""Google Drive REST adapter (Phase 6.1): Drive API v3 with OAuth 2.0, ``drive.file`` scope.

Plan actions (validated against the catalog first, like every plan):
    create_folder    REVERSIBLE  folder inside the sandbox (or a folder this run created)
    create_document  REVERSIBLE  upload a text/markdown report, then VERIFY it remotely
                                 (parent, size, md5, not trashed); a failed verification
                                 trashes the upload and fails the step
    list_files       READ_ONLY   children of the sandbox (or a folder this run created)
    read_document    READ_ONLY   exact-name match among the sandbox folder's children
There is deliberately NO delete/trash plan action: an AI plan can never remove anything.
Trash happens only as Saga compensation of resources this run created, and only after the
adapter re-reads the file and confirms it carries this app's private idempotency marker.

Sandbox confinement (GOOGLE_DRIVE_REAL_WRITES_SANDBOX_ONLY=true, the default): every step
works in SMOKE_TEST_DRIVE_FOLDER or in a folder created by an earlier step of the same run
(parent_step). Before the first real call the sandbox folder itself is checked remotely:
it must be a live folder carrying the ``workflowweaver_sandbox`` app property that only the
setup script (with this OAuth client) can set - a mis-pasted id of a real folder is refused.
Combined with the drive.file scope, the app cannot see or modify any file it did not create.

Idempotency: every created resource carries appProperties ``workflowweaver_key`` (the step's
idempotency key). Before writing, the adapter looks that key up remotely and reuses an
existing resource, so retries after a timeout/5xx never create duplicates (remote_dedup).
"""

import hashlib
from typing import Any

from app.agents.execution.tools.base import CompensationCall, Effect, ErrorKind, ToolCall, ToolError, ToolResult
from app.agents.execution.tools.integrations import IdempotencyLedger, PreparedRequest, _IntegrationAdapter, _safe_url
from app.core.config import IntegrationMode
from app.integrations.config import DestinationConfig
from app.integrations.destinations import DestinationPolicy, ResolvedDestination
from app.integrations.errors import IntegrationError
from app.integrations.google.drive import (
    FOLDER_MIME,
    KEY_PROPERTY,
    MAX_LIST,
    RUN_PROPERTY,
    SANDBOX_PROPERTY,
    DriveClient,
)
from app.schemas.plan import Action
from app.schemas.workflow import Integration

REPORT_MIME = "text/markdown"
MAX_NAME = 200
SANDBOX_ONLY_MESSAGE = ("Google Drive access is restricted to the sandbox folder (SMOKE_TEST_DRIVE_FOLDER) and "
                        "folders created by this run while GOOGLE_DRIVE_REAL_WRITES_SANDBOX_ONLY=true.")

OPERATIONS = {
    Action.CREATE_FOLDER: "drive.files.create (folder)",
    Action.CREATE_DOCUMENT: "drive.files.create (multipart upload) + files.get (verify)",
    Action.LIST_FILES: "drive.files.list",
    Action.READ_DOCUMENT: "drive.files.list + files.get (alt=media)",
}


def _parent_step(arguments: dict[str, str]) -> int | None:
    raw = arguments.get("parent_step", "").strip()
    return int(raw) if raw.isdigit() else None


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class GoogleDriveRestAdapter(_IntegrationAdapter):
    integration = Integration.GOOGLE_DRIVE
    effects = {Action.CREATE_FOLDER: Effect.REVERSIBLE, Action.CREATE_DOCUMENT: Effect.REVERSIBLE,
               Action.LIST_FILES: Effect.READ_ONLY, Action.READ_DOCUMENT: Effect.READ_ONLY}
    remote_dedup = True

    def __init__(self, mode: IntegrationMode, ledger: IdempotencyLedger, *, folders: DestinationConfig,
                 client: DriveClient | None, sandbox_folder: str | None = None, sandbox_only: bool = True) -> None:
        super().__init__(mode, ledger)
        if mode is IntegrationMode.REAL and client is None:
            raise ValueError("Real Google Drive adapter needs a Drive client.")
        self.effects = dict(type(self).effects)
        self.client = client
        self.sandbox_folder = sandbox_folder
        self.sandbox_only = sandbox_only
        self._folders = DestinationPolicy(folders, argument="folder", label="Google Drive folder")
        self._sandbox_checked = False

    # ----- policy (pure, network-free) -----

    def _policy(self, action: Action) -> DestinationPolicy | None:
        return self._folders

    def _sandbox_problem(self, arguments: dict[str, str]) -> str | None:
        if _parent_step(arguments) is not None:
            return None  # inside a folder this run creates (validator: earlier create_folder step)
        if not self.sandbox_only:
            return None
        if not self.sandbox_folder:
            return SANDBOX_ONLY_MESSAGE
        return None if self._folders.requested_alias(arguments) == self.sandbox_folder else SANDBOX_ONLY_MESSAGE

    def preflight(self, action: Action, arguments: dict[str, str]) -> list[tuple[str, str]]:
        if problem := self._sandbox_problem(arguments):
            return [("DESTINATION_NOT_APPROVED", problem)]
        if _parent_step(arguments) is not None:
            return []
        for key in ("name", "title"):
            if len(arguments.get(key, "")) > MAX_NAME:
                return [("ARGUMENT_TOO_LONG", f"Google Drive {key} is limited to {MAX_NAME} characters.")]
        return super().preflight(action, arguments)

    def _resource_type(self, action: Action) -> str:
        return {Action.CREATE_FOLDER: "drive_folder", Action.LIST_FILES: "drive_listing"}.get(action, "drive_document")

    def _prepare(self, call: ToolCall) -> PreparedRequest:
        args = call.arguments
        if problem := self._sandbox_problem(args):  # defence in depth: preflight already rejected it
            raise ToolError(ErrorKind.PERMISSION, "DESTINATION_NOT_APPROVED", problem)
        parent_step = _parent_step(args)
        if parent_step is not None:
            target = call.upstream.get(parent_step)
            if not target:
                raise ToolError(ErrorKind.PERMANENT, "PARENT_NOT_AVAILABLE",
                                f"Step {parent_step} did not create a folder in this run.")
            dest = ResolvedDestination(alias=f"folder from step {parent_step}", target=target)
        else:
            dest = self._folders.resolve(args)
        request: dict[str, Any] = {"parent_id": dest.target}
        if call.action is Action.CREATE_FOLDER:
            request["name"] = args["name"]
        elif call.action is Action.CREATE_DOCUMENT:
            request.update(name=args["title"], content=args["content"].encode("utf-8"), mime_type=REPORT_MIME)
        elif call.action is Action.READ_DOCUMENT:
            request["name"] = args["document_name"]
            if args.get("content_sha256"):
                request["content_sha256"] = args["content_sha256"]
            if args.get("file_id"):
                request.update(file_id=args["file_id"], expected_sha256=args.get("expected_sha256"))
        return PreparedRequest(OPERATIONS[call.action], dest, request)

    def _dry_run(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        result = super()._dry_run(call, request)
        if call.action is Action.CREATE_DOCUMENT:
            content = request.arguments["content"]
            result.output.update(size_bytes=len(content), md5=md5_hex(content), mime_type=REPORT_MIME,
                                 verified=False)
        elif call.action is Action.CREATE_FOLDER:
            result.output["name"] = call.arguments["name"]
        elif call.action is Action.READ_DOCUMENT and call.arguments.get("content_sha256"):
            # Dry run opens no connection: the uploaded report is NOT read back or compared here.
            result.output.update(document_name=call.arguments["document_name"], content_check="not performed (dry run)")
        elif call.action is Action.READ_DOCUMENT and call.arguments.get("file_id"):
            # Dry run opens no connection: the source file is NOT checked or read here.
            result.output.update(document_name=call.arguments["document_name"], file_id=call.arguments["file_id"],
                                 source_check="not performed (dry run)")
        return result

    # ----- real calls -----

    def _check_sandbox(self) -> None:
        """Once per adapter: the configured sandbox folder must be a live folder that the
        setup script (this OAuth client) marked as the WorkflowWeaver sandbox."""
        if self._sandbox_checked or not self.sandbox_folder:
            return
        assert self.client is not None
        target = self._folders.resolve({"folder": self.sandbox_folder}).target
        try:
            meta = self.client.get(target)
        except IntegrationError as exc:
            if exc.code in {"GOOGLE_DRIVE_NOT_FOUND", "GOOGLE_DRIVE_PERMISSION_DENIED"}:
                raise ToolError(ErrorKind.PERMISSION, "SANDBOX_FOLDER_INVALID",
                                "The Drive sandbox folder is not accessible to this app; create it with "
                                "scripts/google_drive_sandbox.py create-folder.") from exc
            raise
        marker = (meta.get("appProperties") or {}).get(SANDBOX_PROPERTY)
        if meta.get("mimeType") != FOLDER_MIME or meta.get("trashed") or marker != "true":
            raise ToolError(ErrorKind.PERMISSION, "SANDBOX_FOLDER_INVALID",
                            "The configured Drive sandbox folder is not a live WorkflowWeaver sandbox folder.")
        self._sandbox_checked = True

    def _send(self, call: ToolCall, request: PreparedRequest) -> ToolResult:
        assert self.client is not None and request.destination is not None
        self._check_sandbox()
        args = request.arguments
        alias = request.destination.alias
        if call.action is Action.LIST_FILES:
            files = self.client.list_children(args["parent_id"], limit=MAX_LIST)
            listing = [{"id": f.get("id"), "name": f.get("name"), "mime_type": f.get("mimeType"),
                        "size_bytes": int(f["size"]) if str(f.get("size", "")).isdigit() else None}
                       for f in files]
            return self._result(call, None, "drive_listing",
                                {"destination": alias, "file_count": len(listing), "files": listing})
        if call.action is Action.READ_DOCUMENT:
            return self._read(call, args, alias)

        props = {KEY_PROPERTY: call.idempotency_key, RUN_PROPERTY: call.workflow_id}
        existing = self.client.find_by_key(call.idempotency_key)  # retry after an ambiguous failure
        if call.action is Action.CREATE_FOLDER:
            meta = existing or self.client.create_folder(args["name"], args["parent_id"], props)
            output = {"destination": alias, "name": args["name"], "reused": existing is not None}
            if url := _safe_url(meta.get("webViewLink")):
                output["url"] = url
            return self._result(call, self._id(meta), "drive_folder", output)

        meta = existing or self.client.upload_text(args["name"], args["content"], args["mime_type"],
                                                   args["parent_id"], props)
        file_id = self._id(meta)
        verified = self._verify(file_id, args)
        output = {"destination": alias, "title": call.arguments["title"], "verified": True,
                  "size_bytes": len(args["content"]), "md5": verified["md5Checksum"], "mime_type": args["mime_type"],
                  "reused": existing is not None}
        if url := _safe_url(verified.get("webViewLink")):
            output["url"] = url
        return self._result(call, file_id, "drive_document", output)

    @staticmethod
    def _id(meta: dict[str, Any]) -> str:
        file_id = meta.get("id")
        if not isinstance(file_id, str) or not 0 < len(file_id) <= 200:
            raise ToolError(ErrorKind.PERMANENT, "MALFORMED_TOOL_RESULT", "Google Drive returned no file id.")
        return file_id

    def _verify(self, file_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Re-read the uploaded file's metadata and compare it with what we sent. A mismatch
        trashes the upload (it carries our marker) and fails the step permanently."""
        assert self.client is not None
        meta = self.client.get(file_id)
        problems = []
        if args["parent_id"] not in (meta.get("parents") or []):
            problems.append("parent")
        if meta.get("trashed"):
            problems.append("trashed")
        if str(meta.get("size")) != str(len(args["content"])):
            problems.append("size")
        if meta.get("md5Checksum") != md5_hex(args["content"]):
            problems.append("md5")
        if problems:
            try:
                self.client.trash(file_id)
            except IntegrationError:
                pass  # reported below; the file stays in the sandbox folder for manual cleanup
            raise ToolError(ErrorKind.PERMANENT, "UPLOAD_VERIFICATION_FAILED",
                            f"The uploaded file did not match what was sent ({', '.join(problems)}); it was moved to trash.")
        return meta

    def _read(self, call: ToolCall, args: dict[str, Any], alias: str) -> ToolResult:
        assert self.client is not None
        if args.get("file_id"):
            return self._read_by_id(call, args, alias)
        name = args["name"]
        matches = [f for f in self.client.list_children(args["parent_id"])
                   if f.get("name") == name and f.get("mimeType") != FOLDER_MIME and isinstance(f.get("id"), str)]
        if not matches:
            raise ToolError(ErrorKind.PERMANENT, "DOCUMENT_NOT_FOUND", "No document with that name in the folder.")
        if len(matches) > 1:
            raise ToolError(ErrorKind.PERMANENT, "DOCUMENT_AMBIGUOUS", "Several documents have that name.")
        text = self.client.download_text(matches[0]["id"])
        output: dict[str, Any] = {"destination": alias, "document_name": name, "content_length": len(text),
                                  "md5": md5_hex(text.encode("utf-8"))}
        if expected := args.get("content_sha256"):
            # Read-back verification: the document must be exactly the content this plan uploaded.
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest != expected:
                raise ToolError(ErrorKind.PERMANENT, "READ_BACK_MISMATCH",
                                "The document read back does not match the uploaded content.")
            output.update(sha256=digest, content_verified=True)
        # Contents are business data: report size and hashes only, never the text itself.
        return self._result(call, matches[0]["id"], "drive_document", output)

    def _read_by_id(self, call: ToolCall, args: dict[str, Any], alias: str) -> ToolResult:
        """Phase 7.2: read EXACTLY the approved source file. It must still be a live,
        non-folder file directly inside the approved folder with the approved name, and its
        content must hash to expected_sha256 - otherwise nothing downstream runs. Read-only:
        the source is never modified."""
        assert self.client is not None
        file_id = args["file_id"]
        try:
            meta = self.client.get(file_id)
        except IntegrationError as exc:
            if exc.code in {"GOOGLE_DRIVE_NOT_FOUND", "GOOGLE_DRIVE_PERMISSION_DENIED"}:
                raise ToolError(ErrorKind.PERMISSION, "SOURCE_NOT_ACCESSIBLE",
                                "The source document is not accessible to this app.") from exc
            raise
        if args["parent_id"] not in (meta.get("parents") or []) or meta.get("trashed") \
                or meta.get("mimeType") == FOLDER_MIME:
            raise ToolError(ErrorKind.PERMISSION, "SOURCE_NOT_IN_SANDBOX",
                            "The source document is no longer a live file in the approved sandbox folder.")
        if meta.get("name") != args["name"]:
            raise ToolError(ErrorKind.PERMANENT, "SOURCE_CHANGED", "The source document was renamed after approval.")
        text = self.client.download_text(file_id)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if args.get("expected_sha256") and digest != args["expected_sha256"]:
            raise ToolError(ErrorKind.PERMANENT, "SOURCE_CHANGED",
                            "The source document changed after it was reviewed; plan and approve it again.")
        return self._result(call, file_id, "drive_document",
                            {"destination": alias, "document_name": args["name"], "file_id": file_id,
                             "content_length": len(text), "sha256": digest, "source_verified": True})

    def _undo(self, call: CompensationCall) -> None:
        """Trash a resource this run created - only after proving it carries our marker for
        this exact step. Already-gone resources count as compensated."""
        assert self.client is not None
        try:
            meta = self.client.get(call.resource_id)
        except IntegrationError as exc:
            if exc.code == "GOOGLE_DRIVE_NOT_FOUND":
                return
            raise
        step_key = f"{call.workflow_id}:step-{call.step_number}"  # == ToolCall.idempotency_key of that step
        if (meta.get("appProperties") or {}).get(KEY_PROPERTY) != step_key:
            raise ToolError(ErrorKind.PERMISSION, "COMPENSATION_REFUSED",
                            "Refusing to trash a Drive file this run did not create.")
        if not meta.get("trashed"):
            self.client.trash(call.resource_id)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
