"""A moment in time as Discord's `<t:...>` markup, which each reader sees in their own timezone.

Out of `formatting` since issue #269, because these are the one part of it with no renderer and no
panel in them: `commands/` and `services.projects` imported the whole renderer module for three
one-line helpers. `formatting` reads them from here like everybody else.
"""

from __future__ import annotations

from datetime import datetime

from shannon.domain.time import as_utc

# The word for a time nobody said, here because a missing timestamp is the first place it is
# needed; `formatting` uses it for a missing author and an unnamed title too.
UNKNOWN = "Unknown"


def as_timestamp(value: datetime | None) -> str:
    """The day and the minute, as each reader's own clock writes it, or `Unknown` for no time.

    The form every block's `Last Updated` row and every comment's heading use. None is answered
    rather than refused, because GitHub leaves some times out and the row is still worth showing.
    """
    if value is None:
        return UNKNOWN
    return f"<t:{_epoch(value)}:f>"


def as_relative_time(value: datetime) -> str:
    """When, counted from whoever is reading: "in 3 hours", "2 days ago". Issue #229.

    Discord keeps it current while somebody looks, so a reminder read a week after it went off
    still says how long ago it was asked for, where a time of day would leave the reader to work
    it out.
    """
    return f"<t:{_epoch(value)}:R>"


def as_date(value: datetime) -> str:
    """The day alone, as each reader's own calendar writes it. Issue #232.

    "4 March 2026" or "March 4, 2026", for a fact that lasts months, such as the day somebody
    linked their account: the minute `as_timestamp` adds is noise there, and a relative "3 months
    ago" reads wrong after "since".
    """
    return f"<t:{_epoch(value)}:D>"


def _epoch(value: datetime) -> int:
    """Whole seconds since the epoch, which is what every `<t:...>` form takes.

    `as_utc` first because `timestamp()` reads a naive datetime as local time, which would shift
    every rendered time by the host's offset.
    """
    return int(as_utc(value).timestamp())
