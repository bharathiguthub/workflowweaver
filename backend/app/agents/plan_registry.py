"""Server-side record of the integration target each plan was generated for.

Planning endpoints record ``workflow_id -> integration target`` when they return a plan. The
executor looks it up so a client cannot relabel a plan: execution is rejected when the
approved target or the target in the submitted plan differs from the recorded one, and every
step is re-checked against the recorded target right before it runs.

In-memory and bounded (oldest entries are evicted), like the other Phase 4/5 run state. A
plan that is not in the registry (server restart, scripts, direct API use) is checked
against the target in the request itself.
"""

import threading
from collections import OrderedDict

MAX_ENTRIES = 10_000


class PlanTargetRegistry:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: OrderedDict[str, str] = OrderedDict()
        # Approved Google Drive folder aliases recorded for dry-run/real custom Drive plans.
        self._drive_folders: dict[str, tuple[str, ...]] = {}
        # Approved Teams channel aliases recorded for dry-run/real Teams Only plans.
        self._teams_channels: dict[str, tuple[str, ...]] = {}
        # Phase 7.2: SHA-256 of the exact plan returned for review (approval binding).
        self._digests: dict[str, str] = {}
        # Phase 7.2: SHA-256 of the plan's inputs (source + instructions), part of the plan digest.
        self._input_digests: dict[str, str] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def record(self, workflow_id: object, target: str | None, drive_folders=None, teams_channels=None,
               digest: str | None = None, input_digest: str | None = None) -> None:
        if target is None:
            return
        key = str(workflow_id)
        with self._lock:
            self._entries[key] = str(target)
            self._entries.move_to_end(key)
            if drive_folders is not None:
                self._drive_folders[key] = tuple(drive_folders)
            if teams_channels is not None:
                self._teams_channels[key] = tuple(teams_channels)
            if digest is not None:
                self._digests[key] = digest
            if input_digest is not None:
                self._input_digests[key] = input_digest
            while len(self._entries) > self._max:
                old, _ = self._entries.popitem(last=False)
                self._drive_folders.pop(old, None)
                self._teams_channels.pop(old, None)
                self._digests.pop(old, None)
                self._input_digests.pop(old, None)

    def get(self, workflow_id: object) -> str | None:
        with self._lock:
            return self._entries.get(str(workflow_id))

    def drive_folders(self, workflow_id: object) -> list[str] | None:
        with self._lock:
            folders = self._drive_folders.get(str(workflow_id))
        return list(folders) if folders is not None else None

    def teams_channels(self, workflow_id: object) -> list[str] | None:
        with self._lock:
            channels = self._teams_channels.get(str(workflow_id))
        return list(channels) if channels is not None else None

    def digest(self, workflow_id: object) -> str | None:
        with self._lock:
            return self._digests.get(str(workflow_id))

    def input_digest(self, workflow_id: object) -> str | None:
        with self._lock:
            return self._input_digests.get(str(workflow_id))

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._entries.clear()
            self._drive_folders.clear()
            self._teams_channels.clear()
            self._digests.clear()
            self._input_digests.clear()


_registry = PlanTargetRegistry()


def get_plan_registry() -> PlanTargetRegistry:
    return _registry
