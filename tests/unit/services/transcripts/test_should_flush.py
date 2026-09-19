"""When a captured conversation is worth a comment.

Issue #103. Lifted out of the flusher as a pure function precisely so this file exists: every
reason to publish can be proved without a database, a clock or a GitHub call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shannon.services.transcripts.flush import (
    BODY_BUDGET,
    LONGEST_A_LINE_WAITS,
    MOST_LINES,
    should_flush,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)
QUIET = timedelta(seconds=60)


def ready(**changes: Any) -> bool:
    asked: dict[str, Any] = {
        "count": 3,
        "characters": 100,
        # Said a moment ago, so nothing below trips unless a test says so.
        "oldest": NOW - timedelta(seconds=10),
        "newest": NOW - timedelta(seconds=10),
        "stopped": False,
        "now": NOW,
        "quiet_gap": QUIET,
    }
    asked.update(changes)
    return should_flush(**asked)


def test_nothing_waiting_is_never_worth_a_comment() -> None:
    assert ready(count=0) is False


def test_a_conversation_still_being_talked_in_waits() -> None:
    assert ready() is False


def test_a_thread_that_has_gone_quiet_publishes() -> None:
    assert ready(newest=NOW - QUIET) is True


def test_a_stopped_conversation_publishes_at_once() -> None:
    """Waiting out a quiet gap after somebody has said they are done is pure latency."""
    assert ready(stopped=True, newest=NOW) is True


def test_a_stopped_conversation_with_nothing_in_it_still_does_not() -> None:
    """The count is asked first, so stopping an empty conversation posts no empty comment."""
    assert ready(count=0, stopped=True) is False


def test_enough_lines_publishes_without_waiting_for_quiet() -> None:
    assert ready(count=MOST_LINES) is True


def test_one_line_short_of_that_does_not() -> None:
    assert ready(count=MOST_LINES - 1) is False


def test_enough_characters_publishes_even_where_the_lines_are_few() -> None:
    """Three people pasting stack traces is a few lines and most of a comment."""
    assert ready(count=3, characters=BODY_BUDGET) is True


def test_a_slow_trickle_publishes_once_the_oldest_line_has_waited_long_enough() -> None:
    """The reason this arm exists. A thread with one message just inside the quiet gap never goes
    quiet, so without it the conversation waits for the line count, which is most of an hour."""
    trickling = NOW - timedelta(seconds=30)

    assert ready(oldest=NOW - LONGEST_A_LINE_WAITS, newest=trickling) is True


def test_and_not_before_then() -> None:
    just_inside = NOW - LONGEST_A_LINE_WAITS + timedelta(seconds=1)

    assert ready(oldest=just_inside, newest=NOW - timedelta(seconds=30)) is False
