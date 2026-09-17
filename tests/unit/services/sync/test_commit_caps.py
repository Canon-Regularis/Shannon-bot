"""How many commits one push is allowed to say, pinned against the number rather than the code.

Its own file for the reason `test_safe_text` gives at length: everything else that exercises the
cap builds exactly as many commits as the cap allows and asserts it announced them, comparing the
output against the very constant that decided it. Move the constant and those assertions move
with it, which is the shape of gate that catches nothing.
"""

from __future__ import annotations

import pytest

from shannon.services.sync.commit_lines import AHEAD, COMMITS_PER_PUSH, PUSHED, REWRITTEN

pytestmark = pytest.mark.unit


def test_the_cap_is_ten() -> None:
    """Ten because the worker gives a delivery sixty seconds, and this is ten GitHub reads plus
    ten Discord posts plus the sync that ran before them. It is also the loudest thing this bot
    does, and a rebase of forty commits landing as forty messages would bury the thread."""
    assert COMMITS_PER_PUSH == 10


def test_the_action_is_the_one_github_sends_for_a_push() -> None:
    """GitHub calls it `synchronize` on a `pull_request` event rather than `push`, which is a
    different event about a branch and reaches nothing here."""
    assert PUSHED == "synchronize"


def test_both_kinds_of_rewrite_count_as_a_force_push() -> None:
    """`behind` is the one that is easy to leave out and the one that matters most. A
    `reset --hard HEAD~3 && push --force` leaves nothing ahead and `total_commits` at zero, so
    without it the thread says nothing at all about three commits being thrown away."""
    assert set(REWRITTEN) == {"behind", "diverged"}


def test_only_a_branch_that_moved_forward_has_commits_to_read() -> None:
    assert AHEAD == "ahead"
    assert AHEAD not in REWRITTEN
