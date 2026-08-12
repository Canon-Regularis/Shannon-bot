from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.services.policies import IssuePolicy, PullRequestPolicy

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


class TestPriorityComesFromTheLabels:
    """One rule for both kinds of item. GitHub's labels are where priority lives."""

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

        assert PullRequestPolicy().priority_for(labelled, Priority.UNSET) is expected

    def test_an_unlabelled_pull_request_is_unset(self) -> None:
        assert PullRequestPolicy().priority_for(PULL_REQUEST, Priority.UNSET) is Priority.UNSET

    def test_the_two_kinds_of_item_answer_alike(self) -> None:
        """A pull request and an issue with the same label used to disagree."""
        labels = (Label("high priority"),)

        assert PullRequestPolicy().priority_for(
            replace(PULL_REQUEST, labels=labels), Priority.UNSET
        ) is IssuePolicy().priority_for(replace(ISSUE, labels=labels), Priority.UNSET)

    def test_removing_the_label_clears_the_priority(self) -> None:
        """GitHub is the source of truth, so taking the label off takes the priority with it."""
        cleared = replace(PULL_REQUEST, labels=())

        assert PullRequestPolicy().priority_for(cleared, Priority.HIGH) is Priority.UNSET


class TestWhatEachPolicyLocks:
    def test_a_pull_request_thread_is_left_alone(self) -> None:
        assert PullRequestPolicy().locked(PULL_REQUEST) is None

    def test_an_open_issue_is_unlocked(self) -> None:
        assert IssuePolicy().locked(ISSUE) is False

    def test_a_closed_issue_is_locked(self) -> None:
        assert IssuePolicy().locked(replace(ISSUE, state="closed")) is True


class TestWhatEachPolicyStores:
    def test_a_pull_request_records_author_assignees_and_reviewers(self) -> None:
        roles = PullRequestPolicy().assignments(
            replace(PULL_REQUEST, assignees=(Actor("hubot"),), reviewers=(Actor("monalisa"),))
        )

        assert [actor.login for actor in roles[ActorRole.REVIEWER]] == ["monalisa"]
        assert [actor.login for actor in roles[ActorRole.ASSIGNEE]] == ["hubot"]

    def test_an_issue_has_no_reviewers_at_all(self) -> None:
        roles = IssuePolicy().assignments(replace(ISSUE, assignees=(Actor("hubot"),)))

        assert ActorRole.REVIEWER not in roles

    def test_closing_an_issue_marks_it_done(self) -> None:
        closed = replace(ISSUE, state="closed")

        assert IssuePolicy().status_for(closed, Status.NOT_REVIEWED) is Status.DONE

    def test_closing_a_pull_request_leaves_its_status_alone(self) -> None:
        closed = replace(PULL_REQUEST, state="closed")

        assert PullRequestPolicy().status_for(closed, Status.IN_REVIEW) is Status.IN_REVIEW

    def test_the_object_type_each_one_owns(self) -> None:
        assert PullRequestPolicy().object_type is ObjectType.PR
        assert IssuePolicy().object_type is ObjectType.ISSUE
