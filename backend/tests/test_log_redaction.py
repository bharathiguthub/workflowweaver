"""SSE capability tokens never reach access/request logs."""

import logging

import httpx
import pytest

import app.main  # noqa: F401  (installs the filters)
from app.core.log_redaction import URL_LOGGERS, TokenRedactingFilter
from scripts.e2e_check import live_server

TOKEN = "SyntheticToken_abcdefghijklmnopqrstuvwxyz0123456789"


def test_filters_installed_on_url_loggers():
    for name in URL_LOGGERS:
        assert any(isinstance(f, TokenRedactingFilter) for f in logging.getLogger(name).filters), name


@pytest.mark.parametrize(
    "args",
    [
        ("127.0.0.1:5000", "GET", f"/api/v1/workflows/executions/x/events?access_token={TOKEN}", "1.1", 200),
        ("127.0.0.1:5000", "GET", f"/events?last_event_id=3&access_token={TOKEN}&x=1", "1.1", 200),
    ],
)
def test_uvicorn_access_record_is_redacted(args):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    TokenRedactingFilter().filter(record)
    message = record.getMessage()
    assert TOKEN not in message and "access_token=[REDACTED]" in message
    assert "last_event_id=3" in message or "last_event_id" not in args[2]


def test_live_server_logs_never_contain_the_token(caplog):
    """Real uvicorn access log + httpx request log during a streamed run."""
    with caplog.at_level(logging.INFO), live_server() as live:
        for name in ("uvicorn.access", "httpx"):
            logging.getLogger(name).setLevel(logging.INFO)
        plan = live.plan(live.templates()["sprint-planning"]["example_instruction"]).json()
        start = live.start(plan)
        live.stream(start)
    assert start["stream_token"] not in caplog.text
    assert "access_token=[REDACTED]" in caplog.text
