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
    # What a reviewer says when they approve, beside the word for the same thing. Nothing
    # else here is added on a guess: `ready to ship` is one word from `shipped`, which is
    # DONE, and `ready for review` means awaiting one rather than past it.
    "lgtm": Status.READY_FOR_MERGE,
    "done": Status.DONE,
    "closed": Status.DONE,
    "complete": Status.DONE,
    "completed": Status.DONE,
    "shipped": Status.DONE,
}


def columns_for(status: Status) -> tuple[str, ...]:
    """Every column name this bot reads as one status, in the table's own order.

    Derived rather than written out beside it: a word added to the table and not to the
    sentence would be one this bot accepts and never offers, which is the whole failure
    mode of a second hand-kept list.

    Normalised, because that is how the table is keyed - a board spelling one `In Progress`
    is told about `in progress` and is not misled by the difference, since the lookup
    normalises too.

    Complete for BOTH passes of the picker that writes a column, and only because every
    status's own spoken form is itself a key here mapping to that status. A test holds
    that, because it is otherwise an accident: break it and this would offer a list
    missing the one name the exact-match pass would have taken.
    """
    return tuple(column for column, reads_as in _COLUMNS.items() if reads_as is status)


def normalise(column: str) -> str:
    return _SEPARATORS.sub(" ", column.strip().lower()).strip()


def status_from_column(column: str | None) -> Status | None:
    """The status a board column stands for, or None for one nobody has taught us.

    None rather than a default: guessing NOT_REVIEWED would move real work backwards on every poll.
    """
    if not column:
        return None
    return _COLUMNS.get(normalise(column))
