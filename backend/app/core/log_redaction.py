"""Redacts SSE capability tokens from log records.

EventSource cannot send headers, so the stream token travels in the query string
(``?access_token=...``). uvicorn's access log and httpx's request log both print full URLs,
which would write live tokens to disk. This filter rewrites any ``access_token=<value>`` in a
record's message and arguments to ``access_token=[REDACTED]``.
"""

import logging
import re

from app.integrations.secrets import scrub_secrets

_TOKEN = re.compile(r"((?:access_token|refresh_token|client_secret|sig|[?&]code)=)[^&\s\"']+", re.IGNORECASE)
REDACTED = r"\1[REDACTED]"
# Google OAuth values by shape (defence in depth behind register_secret): access tokens
# (ya29.), refresh tokens (1//) and OAuth client secrets (GOCSPX-).
_GOOGLE = re.compile(r"\b(?:ya29\.[A-Za-z0-9_\-.]{10,}|1//[A-Za-z0-9_\-]{10,}|GOCSPX-[A-Za-z0-9_\-]{10,})")


def _scrub(text: str) -> str:
    # Registered secrets FIRST: a whole configured value (e.g. a Teams webhook URL) is replaced
    # before the pattern pass rewrites part of it (sig=...), which would stop it matching.
    return _GOOGLE.sub("[REDACTED]", _TOKEN.sub(REDACTED, scrub_secrets(text)))

# Loggers that print request URLs.
URL_LOGGERS = ("uvicorn.access", "uvicorn.error", "httpx")


def redact(value: object) -> object:
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # e.g. httpx logs request.url as an httpx.URL object, not a str.
    text = str(value)
    scrubbed = _scrub(text)
    return scrubbed if scrubbed != text else value


class TokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(redact(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: redact(v) for k, v in record.args.items()}
        except Exception:  # never drop a log line because redaction failed
            pass
        return True


def install_secret_redaction() -> None:
    """Attach the filter to every root handler too, so configured integration secrets are
    scrubbed from records of any logger (idempotent). Handler filters see every record
    that reaches the handler, including those propagated from child loggers."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, TokenRedactingFilter) for f in handler.filters):
            handler.addFilter(TokenRedactingFilter())


def install_token_redaction() -> None:
    """Attach the filter to the URL-printing loggers (idempotent)."""
    for name in URL_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(TokenRedactingFilter())
