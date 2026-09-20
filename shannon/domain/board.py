"""Reading a project board's column back as one of our statuses.

GitHub's default board template spells them `Todo`, `In Progress`, `Done`, so those are accepted
beside our own five. More forgiving than `labels.status_of`, where a repository may have a `done`
meaning something else; a board's Status column is a small closed set chosen for this workflow.
"""

from __future__ import annotations

import re

from shannon.domain.enums import Status

_SEPARATORS = re.compile(r"[\s_\-:/]+")

# Normalised keys: lowercased, with runs of punctuation and space collapsed to one space.
_COLUMNS: dict[str, Status] = {
    "backlog": Status.BACKLOG,
    "icebox": Status.BACKLOG,
    "on hold": Status.BACKLOG,
    "blocked": Status.BACKLOG,
    "not reviewed": Status.NOT_REVIEWED,
    "todo": Status.NOT_REVIEWED,
    "to do": Status.NOT_REVIEWED,
    "new": Status.NOT_REVIEWED,
    "open": Status.NOT_REVIEWED,
    "ready": Status.NOT_REVIEWED,
    "in review": Status.IN_REVIEW,
    "in progress": Status.IN_REVIEW,
    "doing": Status.IN_REVIEW,
    "started": Status.IN_REVIEW,
    "under review": Status.IN_REVIEW,
    "ready for merge": Status.READY_FOR_MERGE,
    "ready to merge": Status.READY_FOR_MERGE,
    "approved": Status.READY_FOR_MERGE,
    "done": Status.DONE,
    "closed": Status.DONE,
    "complete": Status.DONE,
    "completed": Status.DONE,
    "shipped": Status.DONE,
}


def normalise(column: str) -> str:
    return _SEPARATORS.sub(" ", column.strip().lower()).strip()


def status_from_column(column: str | None) -> Status | None:
    """The status a board column stands for, or None for one nobody has taught us.

    None rather than a default: guessing NOT_REVIEWED would move real work backwards on every poll.
    """
    if not column:
        return None
    return _COLUMNS.get(normalise(column))
