"""Verify Langfuse tracing against your own Langfuse project (manual check, not a test).

Runs one mock planning request and one mock execution (with an injected transient failure
and a rollback, so retries and compensation show up), flushes, and prints the session id to
search for in the Langfuse UI. Uses the mock planner and mock tools only.

    .\\.venv\\Scripts\\python.exe -m scripts.langfuse_check

Requires LANGFUSE_ENABLED=true, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY (and optionally
LANGFUSE_BASE_URL) in the shell or in the repo-root .env.
"""

import random
import sys

from app.agents.execution.retry import RetryPolicy
from app.agents.execution.service import ExecutionService
from app.agents.execution.tools import FailureInjector, Fault, MockToolRegistry
from app.agents.planners.mock import MockPlanner
from app.agents.service import PlanningService
from app.core.config import get_settings
from app.observability import LangfuseTelemetry, get_telemetry, shutdown_telemetry
from app.schemas.execution import ExecutionRequest

INSTRUCTION = (
    "Read meeting notes, create a Notion project page, save a summary to Google Drive, "
    "and notify the team in Microsoft Teams."
)


def main() -> int:
    settings = get_settings()
    telemetry = get_telemetry()
    if not isinstance(telemetry, LangfuseTelemetry):
        print("Langfuse is not enabled/configured (see LANGFUSE_* settings). Nothing sent.")
        return 1

    if not telemetry._client.auth_check():  # real SDK method; blocking network call
        print("Langfuse auth_check failed: check keys and LANGFUSE_BASE_URL.")
        return 1

    plan = PlanningService(MockPlanner(), max_steps=settings.max_workflow_steps, max_planning_attempts=2).plan(INSTRUCTION)
    workflow_id = str(plan.workflow_id)
    steps = [s.model_dump(mode="json") for s in plan.planned_steps]
    print(f"planning: {plan.status.value} ({len(steps)} steps)")

    # Step 2 times out once (retry), the last step fails permanently (Saga rollback).
    faults = FailureInjector({2: [Fault.TRANSIENT], len(steps): [Fault.PERMANENT]})
    service = ExecutionService(
        MockToolRegistry(faults=faults), RetryPolicy(jitter=False), max_steps=settings.max_workflow_steps,
        sleeper=lambda _: None, rng=random.Random(0),
    )
    response = service.execute(
        ExecutionRequest.model_validate(
            {
                "workflow_id": workflow_id,
                "plan": {"summary": plan.summary or "", "steps": steps},
                "approval": {"approved": True, "mode": "mock", "approved_by": "synthetic.reviewer"},
            }
        )
    )
    print(f"execution: {response.status.value} attempts={response.attempts}")

    shutdown_telemetry()  # flushes
    print(f"\nIn Langfuse, open Sessions and search for session id: {workflow_id}")
    print("Expect traces 'planning.request' and 'execution.request'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
