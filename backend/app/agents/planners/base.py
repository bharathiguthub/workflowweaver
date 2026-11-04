"""Planner interface shared by the mock and Claude planners."""

from typing import Protocol

from app.schemas.plan import DraftPlan


class PlannerError(Exception):
    """A planning attempt failed. ``retryable`` tells the graph whether to try again."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class PlannerConfigError(Exception):
    """The planner cannot be constructed (e.g. Claude mode without a model configured)."""


class Planner(Protocol):
    name: str

    def plan(self, instruction: str, *, max_steps: int) -> DraftPlan:
        """Return a draft plan or raise PlannerError. Must never execute anything."""
        ...
