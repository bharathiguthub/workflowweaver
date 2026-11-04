"""Runs executions in a bounded worker thread pool so the FastAPI event loop never blocks
on the synchronous LangGraph run or on retry sleeps."""

import logging
from concurrent.futures import Future, ThreadPoolExecutor

from app.agents.execution.service import ExecutionService
from app.schemas.events import ExecutionEventType
from app.schemas.execution import ExecutionRequest, ExecutionResponse, ExecutionStatus
from app.streaming.broker import EventBroker, EventChannel
from app.streaming.emitter import reset_publisher, set_publisher

logger = logging.getLogger(__name__)


def terminal_metadata(response: ExecutionResponse) -> dict:
    return {
        "status": response.status.value,
        "replayed": response.replayed,
        "failed_step": next((r.step_number for r in response.step_results if r.status.value == "FAILED"), None),
        "attempts": response.attempts,
        "step_statuses": {str(r.step_number): r.status.value for r in response.step_results},
        "compensation_statuses": {str(c.step_number): c.status.value for c in response.compensation_results},
        "error_codes": [e.code for e in response.errors],
        "validation_issue_codes": [i.code for i in response.validation.issues],
    }


class ExecutionRunner:
    def __init__(self, service: ExecutionService, broker: EventBroker, *, max_workers: int = 4) -> None:
        self.service = service
        self.broker = broker
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ww-exec")

    def start(self, request: ExecutionRequest) -> tuple[EventChannel, Future]:
        """Raises IdempotencyConflict (different plan), ChannelBusy (stream active) or BrokerFull."""
        self.service.check_conflict(request)
        channel = self.broker.create(str(request.workflow_id))
        return channel, self._executor.submit(self._run, request, channel)

    def _run(self, request: ExecutionRequest, channel: EventChannel) -> ExecutionResponse | None:
        token = set_publisher(channel)
        try:
            response = self.service.execute(request)
        except Exception as exc:  # incl. IdempotencyConflict raced in after start()
            logger.warning(
                "async execution workflow_id=%s failed error=%s", channel.workflow_id, type(exc).__name__
            )
            channel.publish(
                ExecutionEventType.WORKFLOW_FAILED,
                metadata={"status": "ERROR", "error_code": getattr(exc, "code", None) or "INTERNAL_ERROR"},
            )
            return None
        finally:
            reset_publisher(token)
        if not channel.terminal:
            # The graph did not run (duplicate request replayed from the store), so no node
            # emitted a terminal event. Report the stored outcome instead.
            event_type = (
                ExecutionEventType.WORKFLOW_COMPLETED
                if response.status is ExecutionStatus.COMPLETED
                else ExecutionEventType.WORKFLOW_FAILED
            )
            channel.publish(event_type, metadata=terminal_metadata(response))
        return response

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
