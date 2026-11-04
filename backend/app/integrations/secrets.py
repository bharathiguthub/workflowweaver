"""Process-wide registry of configured secret values (tokens, webhook URLs, MCP URLs).

Primary rule: integration code never logs, returns, emits or traces secrets. This registry
is defence in depth behind that rule: every registered value is replaced by [REDACTED] by
``scrub_secrets``, which the log filter (app.core.log_redaction) and the telemetry
sanitizer (app.observability.sanitize) both apply. It catches e.g. httpx's own
"HTTP Request: POST <url>" log line, which would otherwise print a Teams webhook URL.
"""

import threading

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 8  # shorter values would cause false-positive redaction of normal text

_lock = threading.Lock()
_secrets: tuple[str, ...] = ()


def register_secret(value: str | None) -> None:
    global _secrets
    if not value or len(value) < MIN_SECRET_LENGTH:
        return
    with _lock:
        if value not in _secrets:
            # Longest first, so a secret that contains another is replaced whole.
            _secrets = tuple(sorted((*_secrets, value), key=len, reverse=True))


def scrub_secrets(text: str) -> str:
    for secret in _secrets:  # tuple snapshot: safe without the lock
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def clear_registered_secrets() -> None:
    """Tests only."""
    global _secrets
    with _lock:
        _secrets = ()
