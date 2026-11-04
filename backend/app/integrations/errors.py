"""Errors raised by the integration clients (MCP, webhooks).

``IntegrationError`` IS a ``ToolError``, so the execution graph's retry policy and error
reporting treat it exactly like any other tool failure. It adds ``request_sent``: whether
the request may have reached the remote system. Adapters use it to avoid retrying a WRITE
whose outcome is unknown (a retry could create a duplicate side effect).

Messages are fixed strings chosen by our code. They never contain remote response bodies,
URLs, headers, argument values or exception text, because they end up in API responses,
SSE events, logs and traces.
"""

from app.agents.execution.tools.base import ErrorKind, ToolError


class IntegrationError(ToolError):
    def __init__(self, kind: ErrorKind, code: str, message: str, *, request_sent: bool = False) -> None:
        super().__init__(kind, code, message)
        self.request_sent = request_sent


class IntegrationConfigError(ValueError):
    """Server-side integration configuration is invalid. Message names the setting only."""


def unknown_outcome(error: ToolError) -> ToolError:
    """A transient failure after a WRITE request was sent: the effect may or may not exist.
    Retrying could duplicate it, so it becomes a permanent, non-retried failure."""
    return ToolError(
        ErrorKind.PERMANENT,
        "OUTCOME_UNKNOWN",
        f"The request may have been applied ({error.code}); not retried to avoid a duplicate. Verify manually.",
    )
