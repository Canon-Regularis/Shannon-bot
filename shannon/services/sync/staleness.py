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

    Purely a question about the two timestamps. Whether there is anything to be done about it
    is a separate question, and the two were worth separating: an item with no thread still has
    to be given one however old the delivery is, but that is a reason to build the thread, not
    a reason to believe the delivery about the title, the state or who is on it.

    Missing timestamps are no evidence either way, and equal ones are not superseded on the
    timestamps alone, because several changes inside one second share a timestamp and all of them
    are real. `arrived` and `applied` are what separate those, where they are known.
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
    equal timestamps are the ordinary case rather than a corner of one. Nothing in a payload says
    which of two came first, and nothing on the item could say either until the deliveries
    themselves were numbered: `webhook_events.id` is assigned as each is written down, which is
    the order they reached this bot.

    That order only matters because things reorder. The worker leases by that same number, so
    deliveries are handled in arrival order until one of them fails: a delivery that backs off is
    skipped until its next attempt and the one behind it goes first. Then the older payload is
    the last to be believed, and with equal timestamps nothing turned it away.

    Unknown on either side answers False, which is the timestamps deciding alone. An item written
    before the number was kept has none and no way to work one out, and a sync that came from a
    command or the board rather than from a delivery has no number to offer.
    """
    if arrived is None or applied is None:
        return False
    return arrived < applied
