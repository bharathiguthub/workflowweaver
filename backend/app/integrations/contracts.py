"""Verified MCP tool contracts (Phase 5.2).

Each contract records the exact MCP tool WorkflowWeaver calls, the argument names it sends,
and where that contract came from. Tool names are NOT configurable: the arguments we build
are specific to one server's schema, so pointing a name at another tool would send it
arguments it does not understand. Operators can only NARROW the set via the allowlist.

At connect time the MCP client checks every contracted tool against the server's live
``tools/list`` schema (``check_tool_schema``). A drifted tool - a required argument we do
not send, or an argument we send that the schema forbids - is disabled and every call to it
fails with MCP_CONTRACT_MISMATCH before anything is sent.

Verification status
  notion        LIVE: initialize + tools/list against @notionhq/notion-mcp-server 2.6.1
                (Streamable HTTP, loopback, no Notion token, no tools/call), 2026-11-03.
                Error format verified from that version's source (proxy.ts).
  google_drive  DOCUMENTED ONLY: developers.google.com reference for the official
                drivemcp.googleapis.com server. Not live-verified (OAuth 2.0 + Developer
                Preview membership required).
"""

from dataclasses import dataclass
from typing import Any

from app.schemas.workflow import Integration


@dataclass(frozen=True)
class ToolContract:
    name: str
    sends: frozenset[str]  # every argument name WorkflowWeaver may send
    verification: str


NOTION_SERVER = "@notionhq/notion-mcp-server 2.6.1"
NOTION_VERIFICATION = f"live tools/list, {NOTION_SERVER}"
DRIVE_VERIFICATION = "documented (developers.google.com Drive MCP reference); not live-verified"

NOTION_CREATE_PAGE = ToolContract("API-post-page", frozenset({"parent", "properties", "children"}),
                                  NOTION_VERIFICATION)
NOTION_PATCH_PAGE = ToolContract("API-patch-page", frozenset({"page_id", "in_trash"}), NOTION_VERIFICATION)
# Read-only; used by connectivity checks and to verify remote results of smoke tests.
NOTION_RETRIEVE_PAGE = ToolContract("API-retrieve-a-page", frozenset({"page_id"}), NOTION_VERIFICATION)

DRIVE_CREATE_FILE = ToolContract(
    "create_file", frozenset({"title", "textContent", "contentMimeType", "parentId"}), DRIVE_VERIFICATION)
DRIVE_SEARCH_FILES = ToolContract(
    "search_files", frozenset({"query", "pageSize", "excludeContentSnippets"}), DRIVE_VERIFICATION)
DRIVE_READ_FILE_CONTENT = ToolContract("read_file_content", frozenset({"fileId"}), DRIVE_VERIFICATION)
# Read-only; used to verify remote results of smoke tests.
DRIVE_GET_FILE_METADATA = ToolContract("get_file_metadata", frozenset({"fileId", "excludeContentSnippets"}),
                                       DRIVE_VERIFICATION)

CONTRACTS: dict[Integration, dict[str, ToolContract]] = {
    Integration.NOTION: {c.name: c for c in (NOTION_CREATE_PAGE, NOTION_PATCH_PAGE, NOTION_RETRIEVE_PAGE)},
    Integration.GOOGLE_DRIVE: {c.name: c for c in (DRIVE_CREATE_FILE, DRIVE_SEARCH_FILES, DRIVE_READ_FILE_CONTENT,
                                                   DRIVE_GET_FILE_METADATA)},
}


def check_tool_schema(contract: ToolContract, input_schema: dict[str, Any]) -> list[str]:
    """Problems between what we send and the server's advertised input schema (names only;
    never values). Empty list = compatible."""
    problems: list[str] = []
    if not isinstance(input_schema, dict):
        return ["input schema missing"]
    properties = input_schema.get("properties")
    required = input_schema.get("required") or []
    missing = sorted(set(required) - contract.sends)
    if missing:
        problems.append(f"server requires arguments we never send: {missing}")
    if isinstance(properties, dict) and input_schema.get("additionalProperties") is False:
        unknown = sorted(contract.sends - set(properties))
        if unknown:
            problems.append(f"server schema does not accept: {unknown}")
    elif isinstance(properties, dict):
        # Open schema: still require that the arguments that matter are declared.
        undeclared = sorted(contract.sends - set(properties))
        if undeclared:
            problems.append(f"server schema does not declare: {undeclared}")
    return problems
