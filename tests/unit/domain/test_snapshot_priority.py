from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.domain.enums import Priority
from shannon.domain.models import (
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)

REPO = RepositorySnapshot(github_repo_id=1, owner="o", name="r", html_url="https://github.com/o/r")
WHEN = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

PULL_REQUEST = PullRequestSnapshot(
    repository=REPO,
    github_object_id=1,
    number=7,
    title="t",
    html_url="u",
    state="open",
    updated_at=WHEN,
)
ISSUE = IssueSnapshot(
    repository=REPO,
    github_object_id=2,
    number=12,
    title="t",
    html_url="u",
    state="open",
    updated_at=WHEN,
)


class TestPriorityComesFromTheLabels:
    """One rule for both kinds of item, which is why it sits on the snapshot.

    It used to be a policy method that both policies implemented identically, so the interface
    advertised a difference that was never there.
    """

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("high priority", Priority.HIGH),
            ("medium priority", Priority.MEDIUM),
            ("low priority", Priority.LOW),
        ],
    )
    def test_a_labelled_pull_request_takes_its_priority_from_the_label(
        self, label: str, expected: Priority
    ) -> None:
        labelled = replace(PULL_REQUEST, labels=(Label("backend"), Label(label)))

        assert labelled.priority is expected

    def test_an_unlabelled_pull_request_is_unset(self) -> None:
        assert PULL_REQUEST.priority is Priority.UNSET

    def test_the_two_kinds_of_item_answer_alike(self) -> None:
        """A pull request and an issue with the same label used to disagree."""
        labels = (Label("high priority"),)

        pull_request = replace(PULL_REQUEST, labels=labels)
        issue = replace(ISSUE, labels=labels)

        assert pull_request.priority is issue.priority

    def test_removing_the_label_clears_the_priority(self) -> None:
        """GitHub is the source of truth, so taking the label off takes the priority with it."""
        cleared = replace(PULL_REQUEST, labels=())

        assert cleared.priority is Priority.UNSET
