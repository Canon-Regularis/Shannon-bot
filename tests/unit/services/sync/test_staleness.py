from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from shannon.services.sync.staleness import is_superseded

EARLY = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
LATE = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)


def test_an_older_snapshot_is_stale() -> None:
    assert is_superseded(EARLY, LATE) is True


def test_a_newer_snapshot_is_not() -> None:
    assert is_superseded(LATE, EARLY) is False


def test_equal_timestamps_are_not_stale() -> None:
    """Several changes inside one second share a timestamp, and all of them are real."""
    assert is_superseded(LATE, LATE) is False


def test_a_naive_timestamp_is_read_as_utc_rather_than_raising() -> None:
    """Comparing an aware datetime to a naive one raises, and a payload could carry either."""
    naive_early = EARLY.replace(tzinfo=None)

    assert is_superseded(naive_early, LATE) is True
    assert is_superseded(LATE, naive_early) is False


def test_offsets_are_compared_as_instants_not_wall_clocks() -> None:
    """09:00 in a +05:00 zone is earlier than 09:00 UTC, not the same moment."""
    ahead = datetime(2026, 8, 11, 9, 0, tzinfo=timezone(timedelta(hours=5)))

    assert is_superseded(ahead, EARLY) is True
    assert is_superseded(EARLY, ahead) is False


def test_a_single_second_of_difference_counts() -> None:
    assert is_superseded(LATE - timedelta(seconds=1), LATE) is True
