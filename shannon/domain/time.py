from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Read a timestamp as UTC when it does not say what it is.

    `datetime.timestamp()` reads a naive value as local time, so it renders shifted, and comparing
    one against an aware datetime raises. GitHub does send offsets; this is a guard.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
