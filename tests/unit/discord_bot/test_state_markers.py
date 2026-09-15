"""What a thread says when an item closes, merges or reopens.

A heading rather than a line, because an item closing is the one event in a thread worth finding
by scrolling, and the tag lines beside it are deliberately quieter than this.

The exact strings are asserted rather than a substring of them, because these are the whole of
what a reader sees and a mark quietly changing is the failure this file is for.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.formatting import format_state_change
from shannon.domain.enums import StateChange

pytestmark = pytest.mark.unit


def test_a_closed_issue_says_the_thread_is_shut_and_how_to_undo_it() -> None:
    assert format_state_change(StateChange.CLOSED, locked=True) == (
        "### 🔒 Closed\n-# This thread is locked. Reopen the item on GitHub to reopen it here."
    )


def test_a_closed_pull_request_claims_no_lock() -> None:
    """Pull requests close without their thread being shut. Saying otherwise would tell people
    they cannot reply where they can.
    """
    assert format_state_change(StateChange.CLOSED, locked=False) == "### 🔒 Closed"


def test_a_merged_pull_request_is_not_told_to_reopen_on_github() -> None:
    """`/set_done` is what locks a pull request and the requirements have it run before the
    merge, so a merged item arriving in a shut thread is the ordinary order rather than a corner.
    A merged pull request cannot be reopened, so it is not offered as the way out.
    """
    assert format_state_change(StateChange.MERGED, locked=True) == (
        "### 🟣 Merged\n-# This thread is locked."
    )


def test_a_merged_pull_request_whose_thread_was_never_shut_says_only_that() -> None:
    assert format_state_change(StateChange.MERGED, locked=False) == "### 🟣 Merged"


def test_a_reopened_item_says_the_thread_is_back() -> None:
    assert format_state_change(StateChange.REOPENED, locked=False) == (
        "### 🔓 Reopened\n-# This thread is open again."
    )


def test_a_reopen_whose_unlock_was_refused_does_not_promise_the_thread_is_open() -> None:
    """A refused unlock is logged and stepped over rather than failing the delivery, on purpose,
    so a reopened item really does reach here in a thread that is still shut. This is the one
    sentence in the file that would be a plain lie.
    """
    assert format_state_change(StateChange.REOPENED, locked=True) == "### 🔓 Reopened"
