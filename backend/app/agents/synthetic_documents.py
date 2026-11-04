"""Synthetic Google Drive documents available in mock mode.

Single source of truth shared by the mock planner (to resolve the document an instruction
names) and the mock Google Drive adapter (to serve ``read_document``). All content is
synthetic.
"""

SYNTHETIC_DRIVE_DOCUMENTS: dict[str, str] = {
    "Synthetic standup notes": "Alice drafts the roadmap by Friday. Bob reviews the API spec.",
    "Synthetic sprint backlog": "WW-1 Login page; WW-2 Export CSV; WW-3 Fix flaky test.",
    "Synthetic incident report": "SEV-3: synthetic latency spike on staging, mitigated.",
}


def canonical_document_name(name: str) -> str | None:
    """Exact catalog name for ``name`` (case/whitespace-insensitive), or None.

    Deliberately exact: no fuzzy matching, so a request for one document can never be
    silently resolved to a different one.
    """
    key = " ".join(name.split()).casefold()
    return next((doc for doc in SYNTHETIC_DRIVE_DOCUMENTS if doc.casefold() == key), None)


def find_document_mentioned_in(text: str) -> str | None:
    """Catalog document whose full name appears in ``text`` (case-insensitive), or None.
    The longest name wins if several appear."""
    folded = " ".join(text.split()).casefold()
    hits = [doc for doc in SYNTHETIC_DRIVE_DOCUMENTS if doc.casefold() in folded]
    return max(hits, key=len) if hits else None
