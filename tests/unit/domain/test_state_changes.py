"""Which deliveries are a state move, and which of the three they are.

Read off the snapshot rather than the payload, so this is the whole of the question: an action
and, for a closed pull request, whether it was merged. The merge flag itself is reconciled in
`github/mapping.py`, which reads both `merged` and `merged_at` because GitHub sends it either way
round, and asking the snapshot is what keeps that rule in one place.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.domain.enums import StateChange
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.domain.state_changes import STATE_ACTIONS, state_change_of

pytestmark = pytest.mark.unit

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
COMMON = {
    "repository": REPO,
    "github_object_id": 100,
    "number": 7,
    "title": "Add the webhook endpoint",
    "html_url": "https://github.com/Canon-Regularis/Shannon-bot/pull/7",
    "state": "open",
    "author": Actor("octocat"),
    "updated_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
}
PULL_REQUEST = PullRequestSnapshot(**COMMON)
ISSUE = IssueSnapshot(**COMMON)


def test_a_closed_issue_is_a_close() -> None:
    closed = replace(ISSUE, state="closed")

    assert state_change_of("closed", closed) is StateChange.CLOSED


def test_a_closed_pull_request_is_a_close() -> None:
    closed = replace(PULL_REQUEST, state="closed")

    assert state_change_of("closed", closed) is StateChange.CLOSED


def test_a_merged_pull_request_is_a_merge() -> None:
    """GitHub has no `merged` action. A merge arrives as a close with a flag beside it, and work
    finished reads differently from work abandoned."""
    merged = replace(PULL_REQUEST, state="closed", merged=True)

    assert state_change_of("closed", merged) is StateChange.MERGED


def test_a_reopened_item_is_a_reopen() -> None:
    reopened = replace(ISSUE, state="open")

    assert state_change_of("reopened", reopened) is StateChange.REOPENED


def test_the_two_actions_that_move_a_state_are_the_only_ones_read() -> None:
    """Every other action carries the item's state too. An item that reads closed on an `edited`
    delivery did not just close: it was closed already, and something else about it changed."""
    assert set(STATE_ACTIONS) == {"closed", "reopened"}


@pytest.mark.parametrize(
    "action", ["opened", "edited", "labeled", "unlabeled", "assigned", "review_requested"]
)
def test_an_action_that_moves_no_state_says_nothing(action: str) -> None:
    already_closed = replace(ISSUE, state="closed")

    assert state_change_of(action, already_closed) is None
