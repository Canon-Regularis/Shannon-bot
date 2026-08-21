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


def is_newer(incoming: datetime | None, stored: datetime | None) -> bool:
    """Whether this snapshot carries a change the stored item has not been brought up to yet.

    Strictly later, which is the difference from `is_superseded` and the whole reason to ask.
    A delivery replayed after a failure carries the timestamp the item was already raised to,
    and telling that apart from a change made since is what stops the second copy of an event
    being acted on as though it were a second event.

    Both sides are GitHub's clock. Comparing one of them against ours would make the answer
    depend on how far apart two machines' idea of now had drifted.
    """
    if incoming is None or stored is None:
        return False
    return as_utc(incoming) > as_utc(stored)
