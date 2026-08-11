from __future__ import annotations

from datetime import UTC, datetime


def is_stale(*, has_thread: bool, incoming: datetime | None, stored: datetime | None) -> bool:
    """Whether an incoming snapshot describes an item as it was before what is already stored.

    GitHub does not guarantee webhook ordering, and a retry can land long after the event that
    superseded it. Applying one of those would undo a rename, reopen a closed issue, or put
    back an assignee who had been removed.

    Three cases deliberately are not stale:

    - An item with no thread yet, because skipping there would mean it never gets one.
    - Either timestamp missing, because there is then no evidence of staleness.
    - Equal timestamps, because several changes inside one second share a timestamp and all of
      them are real.

    This assumes GitHub advances `updated_at` for every change worth mirroring. It does for
    titles, state, labels and assignees, which is everything the metadata block shows.
    """
    if not has_thread or incoming is None or stored is None:
        return False
    return _as_utc(incoming) < _as_utc(stored)


def _as_utc(value: datetime) -> datetime:
    """The database hands back aware values, and comparing an aware one to a naive one raises."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
