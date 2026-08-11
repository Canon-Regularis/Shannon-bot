from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Read a timestamp as UTC when it does not say what it is.

    A naive datetime is dangerous in two different ways here. `datetime.timestamp()` reads one
    as local time, so a Discord timestamp would render shifted by whatever offset the host
    happens to run in. And comparing a naive one to an aware one raises outright.

    GitHub sends offsets, so this is a guard rather than a routine conversion, but a timestamp
    that silently means something else depending on the machine is worth ruling out.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
