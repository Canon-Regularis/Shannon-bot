"""What a thread says when an item closes, merges or reopens.

A heading rather than a line, because an item closing is the one event in a thread worth finding
by scrolling, and the tag lines beside it are deliberately quieter than this.

Since issue #116 it is also the only card in a thread that carries GitHub's own state colours,
which is what makes a merge findable by scrolling without reading anything.

The exact strings are asserted rather than a substring of them, because these are the whole of
what a reader sees and a mark quietly changing is the failure this file is for.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.formatting import format_state_change
from shannon.discord_bot.panels import Accent, BlockKind
from shannon.domain.enums import StateChange

pytestmark = pytest.mark.unit


def test_a_closed_issue_says_the_thread_is_shut_and_how_to_undo_it() -> None:
    assert format_state_change(StateChange.CLOSED, shut=True).text == (
        "### 🔒 Closed\n-# This thread is locked and archived. "
        "Reopen the item on GitHub to reopen it here."
    )


def test_a_pull_request_nobody_finished_claims_nothing_about_the_thread() -> None:
    """Not every unshut thread is a failure. A pull request closed while this bot was being told
    not to shut anything is simply open, and there is nothing to say about that.
    """
    assert format_state_change(StateChange.CLOSED, shut=False).text == "### 🔒 Closed"


def test_a_thread_discord_would_not_shut_says_which_permission_is_missing() -> None:
    """The other way of not being shut, and the one worth a line. Without this somebody reads a
    closed item in a live thread and has nothing to go on; the log line naming the permission is
    on a server they cannot see. It is also why the delivery is no longer dropped on the refusal:
    dropping it took this line down with it.
    """
    assert format_state_change(StateChange.CLOSED, shut=False, refused=True).text == (
        "### 🔒 Closed\n-# This thread could not be closed: the bot needs Manage Threads."
    )


def test_a_merged_pull_request_is_not_told_to_reopen_on_github() -> None:
    """A merged pull request cannot be reopened, so it is not offered as the way out."""
    assert format_state_change(StateChange.MERGED, shut=True).text == (
        "### 🟣 Merged\n-# This thread is locked and archived."
    )


def test_a_merged_pull_request_whose_thread_was_never_shut_says_only_that() -> None:
    assert format_state_change(StateChange.MERGED, shut=False).text == "### 🟣 Merged"


def test_a_reopened_item_says_the_thread_is_back() -> None:
    assert format_state_change(StateChange.REOPENED, shut=False).text == (
        "### 🔓 Reopened\n-# This thread is open again."
    )


def test_a_reopen_whose_unlock_was_refused_does_not_promise_the_thread_is_open() -> None:
    """A refused unlock is logged and stepped over rather than failing the delivery, on purpose,
    so a reopened item really does reach here in a thread that is still shut. This is the one
    sentence in the file that would be a plain lie.
    """
    assert format_state_change(StateChange.REOPENED, shut=True).text == "### 🔓 Reopened"


def test_a_reopen_says_nothing_about_a_refusal_it_cannot_have_had() -> None:
    """`refused` is only ever set by the step that shuts a thread, and a reopen does not run it.
    Pinned because the flag defaults, so a caller passing it here would go unnoticed.
    """
    assert format_state_change(StateChange.REOPENED, shut=False, refused=True).text == (
        "### 🔓 Reopened\n-# This thread is open again."
    )


@pytest.mark.parametrize(
    ("change", "accent"),
    [
        (StateChange.CLOSED, Accent.CLOSED),
        (StateChange.MERGED, Accent.MERGED),
        (StateChange.REOPENED, Accent.OPEN),
    ],
)
def test_each_change_carries_the_colour_github_gives_it(
    change: StateChange, accent: Accent
) -> None:
    """Red, purple and green, which is what somebody scrolling a thread recognises before they
    have read a word of it. Merged is its own colour rather than a shade of closed, because a
    merge and an abandonment are the two outcomes worth telling apart at a glance.
    """
    assert format_state_change(change, shut=True).accent == accent


@pytest.mark.parametrize("shut", [True, False])
def test_the_small_line_is_a_subheading_under_the_heading_and_never_beside_it(shut: bool) -> None:
    """Order rather than presence. A panel over budget drops from the end, so the heading is
    what survives, and a subheading that had drifted above it would be what survived instead.
    """
    kinds = [block.kind for block in format_state_change(StateChange.CLOSED, shut=shut).blocks]

    assert kinds[0] is BlockKind.HEADING
    assert BlockKind.HEADING not in kinds[1:]
