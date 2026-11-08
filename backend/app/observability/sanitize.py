"""Scrubbing for anything that could leave the process as telemetry.

Primary rule: callers send only identifiers, codes, counts and timings - never prompts,
tool argument values, document contents, error message text or user details. This module
is the defence-in-depth layer behind that rule. It is applied to every input/output/
metadata value before it is handed to Langfuse, and it is also installed as the Langfuse
SDK's ``mask`` hook so data captured by the SDK itself is scrubbed too.
"""

import re
from collections.abc import Mapping
from typing import Any

from app.integrations.secrets import scrub_secrets

REDACTED = "[REDACTED]"
MAX_STRING = 200
MAX_ITEMS = 50
MAX_DEPTH = 6

# Keys whose values are credentials: always fully redacted.
_SECRET_KEY = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|authorization|cookie|credential|private[_-]?key|signature)",
    re.IGNORECASE,
)

# Keys whose values are user/business content: replaced by their length only.
CONTENT_KEYS = frozenset(
    {
        "instruction", "user_input", "prompt", "system", "messages", "content", "message", "text",
        "title", "description", "summary", "document_name", "assignee", "due_date", "parent",
        "folder", "channel", "arguments", "approved_by", "name", "workflow_name", "output_text",
        "error_message", "status_message_text",
    }
)

# Secret-looking substrings inside free text.
_SECRET_VALUE = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}"  # Anthropic keys
    r"|gsk_[A-Za-z0-9]{16,}"  # Groq keys
    r"|AIza[A-Za-z0-9_\-]{20,}"  # Google API keys (Gemini)
    r"|ya29\.[A-Za-z0-9_\-.]{10,}|1//[A-Za-z0-9_\-]{10,}|GOCSPX-[A-Za-z0-9_\-]{10,}"  # Google OAuth tokens/secrets
    r"|sk-lf-[A-Za-z0-9_\-]{8,}|pk-lf-[A-Za-z0-9_\-]{8,}"  # Langfuse keys
    r"|(?i:bearer)\s+[A-Za-z0-9._\-~+/]{8,}=*"  # bearer tokens
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"  # JWTs
    r"|https?://[^\s\"']*webhook[^\s\"']*"  # webhook URLs (Teams incoming webhooks)
    r"|[?&]sig=[^&\s\"']+)"  # SAS signatures (Teams Workflows / Logic Apps trigger URLs)
)


def scrub_text(value: str) -> str:
    value = _SECRET_VALUE.sub(REDACTED, scrub_secrets(value))
    if len(value) > MAX_STRING:
        value = value[:MAX_STRING] + f"...[truncated {len(value) - MAX_STRING} chars]"
    return value


def _redacted_content(value: Any) -> dict[str, Any]:
    length = len(value) if isinstance(value, (str, list, dict, tuple)) else None
    return {"redacted": True, "length": length}


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe, scrubbed copy of ``value``."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if _depth >= MAX_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out["_truncated_items"] = len(value) - MAX_ITEMS
                break
            k = str(key)
            if _SECRET_KEY.search(k):
                out[k] = REDACTED
            elif k.lower() in CONTENT_KEYS:
                out[k] = _redacted_content(item)
            else:
                out[k] = sanitize(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out_list = [sanitize(v, _depth=_depth + 1) for v in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out_list.append(f"[{len(items) - MAX_ITEMS} more items]")
        return out_list
    if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int)):  # Enum
        return sanitize(value.value, _depth=_depth + 1)
    return scrub_text(f"<{type(value).__name__}>")


def langfuse_mask(*, data: Any, **_: Any) -> Any:
    """Langfuse SDK ``mask`` hook (MaskFunction protocol: keyword ``data``)."""
    try:
        return sanitize(data)
    except Exception:  # a masking bug must never leak the raw value
        return REDACTED
