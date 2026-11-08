"""Phase 7.2 source documents: list and read candidate files in the approved Drive sandbox.

Real mode uses the existing Drive REST client (OAuth, ``drive.file`` scope: the app can only
see files it created or was granted), so "app-accessible" is guaranteed by Google. On top:
  - the sandbox folder must be the live folder carrying the private WorkflowWeaver sandbox
    marker (same check as the Drive adapter);
  - a source must be a live, non-folder Markdown/plain-text file whose parents include that
    folder, within the size limit;
  - selection is ALWAYS by file id: names may repeat (duplicates are flagged in the listing).
Read-only: nothing here creates, modifies or trashes anything.

Mock and dry run never contact Google Drive: they serve the deterministic synthetic fixtures
(``source_documents``) and every response says the source was simulated.
"""

from dataclasses import dataclass
from typing import Any

from app.agents.orchestration import MAX_SOURCE_BYTES
from app.agents.source_documents import SOURCE_FIXTURES, sha256_text
from app.agents.validation import DRIVE_FILE_ID
from app.core.config import IntegrationMode
from app.integrations.errors import IntegrationError
from app.integrations.google.drive import FOLDER_MIME, SANDBOX_PROPERTY

SOURCE_MIMES = frozenset({"text/markdown", "text/x-markdown", "text/plain"})


@dataclass(frozen=True)
class SourceInfo:
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None


@dataclass(frozen=True)
class SourceRead:
    info: SourceInfo
    text: str
    sha256: str
    simulated: bool
    checks: tuple[str, ...]  # checks actually performed, for the reviewer


class SourceError(Exception):
    """A source problem the API reports as-is (codes and setting names only, never ids/URLs)."""

    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def simulated(mode: IntegrationMode) -> bool:
    return mode is not IntegrationMode.REAL


# ---------- mock / dry run: synthetic fixtures ----------


def _fixture_info(doc) -> SourceInfo:
    return SourceInfo(doc.file_id, doc.name, doc.mime_type, doc.size_bytes, doc.modified_time)


def list_fixture_sources() -> list[SourceInfo]:
    return sorted((_fixture_info(d) for d in SOURCE_FIXTURES.values()), key=lambda s: s.modified_time or "",
                  reverse=True)


def read_fixture_source(file_id: str, mode: IntegrationMode) -> SourceRead:
    _check_id(file_id)
    doc = SOURCE_FIXTURES.get(file_id)
    if doc is None:
        raise SourceError("SOURCE_NOT_FOUND", "No source document with that id in the sandbox.", 404)
    return SourceRead(_fixture_info(doc), doc.content, doc.sha256, simulated=True,
                      checks=(f"Synthetic source fixture ({mode.value} mode never contacts Google Drive)",
                              "File id selected explicitly (names may repeat)", "Content hashed (SHA-256)"))


# ---------- real: Drive REST, drive.file ----------


def _check_id(file_id: str) -> None:
    if not DRIVE_FILE_ID.fullmatch(file_id or ""):
        raise SourceError("INVALID_IDENTIFIER", "source_file_id is not a Google Drive file id.")


def sandbox_folder_id(client: Any, folder_target: str) -> str:
    """The configured sandbox folder must be a live folder carrying this app's sandbox marker."""
    try:
        meta = client.get(folder_target)
    except IntegrationError as exc:
        raise SourceError("SANDBOX_FOLDER_INVALID", "The Drive sandbox folder is not accessible to this app.",
                          503) from exc
    marker = (meta.get("appProperties") or {}).get(SANDBOX_PROPERTY)
    if meta.get("mimeType") != FOLDER_MIME or meta.get("trashed") or marker != "true":
        raise SourceError("SANDBOX_FOLDER_INVALID",
                          "The configured Drive sandbox folder is not a live WorkflowWeaver sandbox folder.", 503)
    return folder_target


def _is_source(meta: dict[str, Any]) -> bool:
    return (meta.get("mimeType") in SOURCE_MIMES or str(meta.get("name", "")).lower().endswith(".md")) \
        and meta.get("mimeType") != FOLDER_MIME and not meta.get("trashed")


def _info(meta: dict[str, Any]) -> SourceInfo:
    size = str(meta.get("size", ""))
    return SourceInfo(str(meta.get("id")), str(meta.get("name", ""))[:200], str(meta.get("mimeType", "")),
                      int(size) if size.isdigit() else None, meta.get("createdTime"))


def list_drive_sources(client: Any, folder_id: str) -> list[SourceInfo]:
    return [_info(f) for f in client.list_children(folder_id)
            if _is_source(f) and isinstance(f.get("id"), str) and DRIVE_FILE_ID.fullmatch(f["id"])]


def read_drive_source(client: Any, folder_id: str, file_id: str) -> SourceRead:
    _check_id(file_id)
    try:
        meta = client.get(file_id)
    except IntegrationError as exc:
        if exc.code in {"GOOGLE_DRIVE_NOT_FOUND", "GOOGLE_DRIVE_PERMISSION_DENIED"}:
            raise SourceError("SOURCE_NOT_ACCESSIBLE", "The source document is not accessible to this app "
                                                       "(drive.file: only files this app created or was granted).",
                              404) from exc
        raise SourceError("SOURCE_READ_FAILED", f"Google Drive error ({exc.code}).", 502) from exc
    if folder_id not in (meta.get("parents") or []):
        raise SourceError("SOURCE_NOT_IN_SANDBOX", "The document is not inside the approved Drive sandbox folder.")
    if not _is_source(meta):
        raise SourceError("SOURCE_NOT_SUPPORTED", "The source must be a live Markdown or plain-text file.")
    size = str(meta.get("size", ""))
    if size.isdigit() and int(size) > MAX_SOURCE_BYTES:
        raise SourceError("SOURCE_TOO_LARGE", f"The source document exceeds {MAX_SOURCE_BYTES} bytes.")
    try:
        text = client.download_text(file_id, max_bytes=MAX_SOURCE_BYTES)
    except IntegrationError as exc:
        raise SourceError("SOURCE_READ_FAILED", f"Google Drive error ({exc.code}).", 502) from exc
    return SourceRead(_info(meta), text, sha256_text(text), simulated=False,
                      checks=("Sandbox folder verified (live, WorkflowWeaver marker)",
                              "File is inside the sandbox folder and app-accessible (drive.file)",
                              "Markdown/plain-text file within the size limit", "Read-only download, SHA-256 recorded"))


def duplicate_names(sources: list[SourceInfo]) -> set[str]:
    seen: dict[str, int] = {}
    for s in sources:
        seen[s.name] = seen.get(s.name, 0) + 1
    return {name for name, count in seen.items() if count > 1}
