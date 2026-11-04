"""Approved-destination policy.

Plans name destinations by ALIAS only (e.g. ``"parent": "engineering"``). The alias is
resolved against server configuration; an alias that is not configured is refused with a
PERMISSION error before any request is built. Raw ids or URLs supplied in a plan are never
used as destinations: they are simply unknown aliases.
"""

from dataclasses import dataclass, field

from app.agents.execution.tools.base import ErrorKind, ToolError
from app.integrations.config import DestinationConfig


@dataclass(frozen=True)
class ResolvedDestination:
    alias: str
    target: str = field(repr=False)  # id or webhook URL; never returned, logged or traced


class DestinationPolicy:
    def __init__(self, config: DestinationConfig, *, argument: str | None, label: str) -> None:
        self._config = config
        self.argument = argument  # plan argument naming the destination; None = default only
        self.label = label

    def requested_alias(self, arguments: dict[str, str]) -> str | None:
        value = arguments.get(self.argument, "").strip().lower() if self.argument else ""
        return value or self._config.default_alias

    def problem(self, arguments: dict[str, str]) -> str | None:
        """Why this step's destination is not approved, or None. Pure; no network."""
        alias = self.requested_alias(arguments)
        if alias is None:
            return f"No {self.label} given and no default {self.label} is configured."
        if alias not in self._config.targets:
            return f"{self.label.capitalize()} '{alias[:40]}' is not an approved destination."
        return None

    def resolve(self, arguments: dict[str, str]) -> ResolvedDestination:
        problem = self.problem(arguments)
        if problem is not None:
            raise ToolError(ErrorKind.PERMISSION, "DESTINATION_NOT_APPROVED", problem)
        alias = self.requested_alias(arguments)
        assert alias is not None
        return ResolvedDestination(alias=alias, target=self._config.targets[alias])

    def all_targets(self) -> list[str]:
        return [self._config.targets[a] for a in self._config.aliases]
