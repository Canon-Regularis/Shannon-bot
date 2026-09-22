from __future__ import annotations

from datetime import datetime

from shannon.domain.time import as_utc


def is_superseded(
    incoming: datetime | None,
    stored: datetime | None,
    *,
    arrived: int | None = None,
    applied: int | None = None,
) -> bool:
    """Whether this snapshot describes an item as it was before what is already stored.

    Purely a question about the two timestamps: an item with no thread still has to be given
    one however old the delivery is. Missing timestamps are no evidence either way, and equal
    ones fall through to `arrived` and `applied` rather than counting as superseded.
    """
    if incoming is None or stored is None:
        return False
    if as_utc(incoming) < as_utc(stored):
        return True
    if as_utc(incoming) > as_utc(stored):
        return False
    return _arrived_earlier(arrived, applied)


def _arrived_earlier(arrived: int | None, applied: int | None) -> bool:
    """Whether this delivery reached us before the one already applied, when the clocks tie.

    GitHub stamps `updated_at` to the second and sends several events for one item inside one, so
    equal timestamps are the ordinary case and nothing in the payload orders them.
    `webhook_events.id` is assigned as each delivery is written down, which is the order they
    reached this bot. That order matters because a delivery that backs off is skipped until its
    next attempt and the one behind it goes first, leaving the older payload the last to be
    believed.

    Unknown on either side answers False: an item written before the number was kept has none, and
    a sync from a command or the board has no number to offer.
    """
    if arrived is None or applied is None:
        return False
    return arrived < applied
