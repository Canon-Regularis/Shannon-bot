from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Read a timestamp as UTC when it does not say what it is.

    A naive datetime renders shifted by the host's offset, since `datetime.timestamp()` reads
    one as local time, and raises outright when compared against an aware one. GitHub does send
    offsets; this is a guard.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
