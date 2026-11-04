"""Redacts SSE capability tokens from log records.

EventSource cannot send headers, so the stream token travels in the query string
(``?access_token=...``). uvicorn's access log and httpx's request log both print full URLs,
which would write live tokens to disk. This filter rewrites any ``access_token=<value>`` in a
record's message and arguments to ``access_token=[REDACTED]``.
"""

import logging
import re

_TOKEN = re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE)
REDACTED = r"\1[REDACTED]"

# Loggers that print request URLs.
URL_LOGGERS = ("uvicorn.access", "uvicorn.error", "httpx")


def redact(value: object) -> object:
    if isinstance(value, str):
        return _TOKEN.sub(REDACTED, value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # e.g. httpx logs request.url as an httpx.URL object, not a str.
    text = str(value)
    return _TOKEN.sub(REDACTED, text) if _TOKEN.search(text) else value


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


def install_token_redaction() -> None:
    """Attach the filter to the URL-printing loggers (idempotent)."""
    for name in URL_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(TokenRedactingFilter())
