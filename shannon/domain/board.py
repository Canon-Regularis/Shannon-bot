"""Reading a project board's column back as one of our statuses.

The five statuses the requirements name read like board columns because that is what they are, so
a board whose columns are spelled the same way needs no configuration at all. Boards that came
from somewhere else spell them differently, and GitHub's own default template is `Todo`,
`In Progress`, `Done`, so those are accepted too.

Deliberately more forgiving than `labels.status_of`, and for the opposite reason. A label is a
free-for-all namespace where a repository may have a `done` that means something else, so labels
match exactly. A board's Status column is a small closed set that somebody chose to describe this
workflow, so guessing at it is safe and refusing to would make the feature useless.
"""

from __future__ import annotations

import re

from shannon.domain.enums import Status

_SEPARATORS = re.compile(r"[\s_\-:/]+")

# Written as normalised keys: lowercased, with runs of punctuation and space collapsed to one
# space. The five own names are here as well as the synonyms, so a board spelled our way is
# matched by the same lookup rather than by a separate branch.
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

    None rather than a default, because an unrecognised column is a question for whoever named
    it. Guessing NOT_REVIEWED would quietly move real work backwards on every poll.
    """
    if not column:
        return None
    return _COLUMNS.get(normalise(column))
