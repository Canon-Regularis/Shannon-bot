from __future__ import annotations

from datetime import datetime

from shannon.domain.time import as_utc


def is_superseded(incoming: datetime | None, stored: datetime | None) -> bool:
    """Whether this snapshot describes an item as it was before what is already stored.

    Purely a question about the two timestamps. Whether there is anything to be done about it
    is a separate question, and the two were worth separating: an item with no thread still has
    to be given one however old the delivery is, but that is a reason to build the thread, not
    a reason to believe the delivery about the title, the state or who is on it.

    Missing timestamps are no evidence either way, and equal ones are not superseded, because
    several changes inside one second share a timestamp and all of them are real.
    """
    if incoming is None or stored is None:
        return False
    return as_utc(incoming) < as_utc(stored)


def is_stale(*, has_thread: bool, incoming: datetime | None, stored: datetime | None) -> bool:
    """Whether an incoming snapshot describes an item as it was before what is already stored.

    GitHub does not guarantee webhook ordering, and a retry can land long after the event that
    superseded it. Applying one of those would undo a rename, reopen a closed issue, or put
    back an assignee who had been removed.

    An item with no thread is never stale, however old the delivery, or it would never get one.
    `is_superseded` covers what the timestamps alone do and do not prove.

    This assumes GitHub advances `updated_at` for every change worth mirroring. It does for
    titles, state, labels and assignees, which is everything the metadata block shows.
    """
    return has_thread and is_superseded(incoming, stored)
