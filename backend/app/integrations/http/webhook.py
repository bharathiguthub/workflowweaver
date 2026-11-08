"""HTTP client abstraction for outgoing webhooks (Microsoft Teams Workflows webhooks).

Microsoft documents a limit of four requests per second per webhook ("the client
connection is throttled"), so posts to the same URL are spaced at least
``MIN_INTERVAL_SECONDS`` apart client-side. Any 2xx is accepted: Microsoft does not
document whether success is 200 or 202, nor whether 2xx means "posted" or only "workflow
triggered".

The webhook URL is a credential: it is passed per call from server configuration, never
logged (httpx's own request log line is scrubbed by the secret registry), never included
in errors, and redirects are not followed. The underlying ``httpx`` transport is
injectable so tests use ``httpx.MockTransport`` and never open a connection.

Error classification (``request_sent`` = the message may have been delivered):
  connect error / connect timeout     TRANSIENT   request_sent=False
  read/write timeout, broken response TRANSIENT   request_sent=True
  HTTP 429                            TRANSIENT   request_sent=False
  HTTP 5xx                            TRANSIENT   request_sent=True
  HTTP 401 / 403                      PERMISSION
  HTTP 404 / 410                      PERMANENT   (webhook removed)
  other 4xx / 3xx                     PERMANENT
"""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.agents.execution.tools.base import ErrorKind
from app.integrations.errors import IntegrationError

MAX_PAYLOAD_BYTES = 28 * 1024  # documented Teams message size limit
MIN_INTERVAL_SECONDS = 0.25  # documented: more than four requests per second are throttled


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int


class WebhookSender(Protocol):
    def post_json(self, url: str, payload: dict[str, Any]) -> WebhookResponse: ...

    def close(self) -> None: ...


def _err(kind: ErrorKind, code: str, message: str, *, sent: bool) -> IntegrationError:
    return IntegrationError(kind, code, message, request_sent=sent)


class WebhookClient:
    def __init__(self, *, connect_timeout: float, timeout: float,
                 http_transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep,
                 min_interval: float = MIN_INTERVAL_SECONDS) -> None:
        self._clock, self._sleep, self._min_interval = clock, sleeper, min_interval
        self._last_post: dict[str, float] = {}
        self._throttle_lock = threading.Lock()
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            follow_redirects=False,
            headers={"Content-Type": "application/json"},
            transport=http_transport,
        )

    def __repr__(self) -> str:
        return "WebhookClient(<redacted>)"

    def post_json(self, url: str, payload: dict[str, Any]) -> WebhookResponse:
        body = json.dumps(payload).encode("utf-8")
        if len(body) > MAX_PAYLOAD_BYTES:
            raise _err(ErrorKind.PERMANENT, "WEBHOOK_PAYLOAD_TOO_LARGE", "Message is too large for the webhook.",
                       sent=False)
        self._throttle(url)
        try:
            response = self._client.post(url, content=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise _err(ErrorKind.TRANSIENT, "WEBHOOK_UNREACHABLE", "Webhook endpoint unreachable.", sent=False) from exc
        except httpx.TimeoutException as exc:
            raise _err(ErrorKind.TRANSIENT, "WEBHOOK_TIMEOUT", "Webhook request timed out.", sent=True) from exc
        except httpx.HTTPError as exc:
            raise _err(ErrorKind.TRANSIENT, "WEBHOOK_TRANSPORT_ERROR", "Webhook transport error.", sent=True) from exc

        code = response.status_code
        if 200 <= code < 300:
            return WebhookResponse(status_code=code)
        if code == 429:
            raise _err(ErrorKind.TRANSIENT, "WEBHOOK_RATE_LIMITED", "Webhook rate limited (HTTP 429).", sent=False)
        if code >= 500:
            raise _err(ErrorKind.TRANSIENT, "WEBHOOK_SERVER_ERROR", f"Webhook server error (HTTP {code}).", sent=True)
        if code in (401, 403):
            raise _err(ErrorKind.PERMISSION, "WEBHOOK_UNAUTHORIZED", f"Webhook refused the request (HTTP {code}).",
                       sent=True)
        if code in (404, 410):
            raise _err(ErrorKind.PERMANENT, "WEBHOOK_NOT_FOUND", "Webhook no longer exists.", sent=True)
        raise _err(ErrorKind.PERMANENT, "WEBHOOK_REJECTED", f"Webhook rejected the request (HTTP {code}).", sent=True)

    def _throttle(self, url: str) -> None:
        with self._throttle_lock:
            last = self._last_post.get(url)
            now = self._clock()
            if last is not None and now - last < self._min_interval:
                self._sleep(self._min_interval - (now - last))
                now = self._clock()
            self._last_post[url] = now

    def close(self) -> None:
        self._client.close()
