from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.domain.enums import ActorRole, ObjectType, Status
from shannon.domain.errors import PermanentError, WrongPolicyError
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.services.sync.items import build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy

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


class TestAPolicyHandedTheWrongKindOfSnapshot:
    """Nothing pairs these but the wiring, and MVP 4 adds a third kind to get wrong."""

    async def test_a_pull_request_policy_refuses_an_issue(self) -> None:
        service = build_item_sync(None, None, PullRequestPolicy())  # type: ignore[arg-type]

        with pytest.raises(WrongPolicyError, match="PullRequestPolicy was handed a ISSUE"):
            await service.sync(ISSUE)

    async def test_an_issue_policy_refuses_a_pull_request(self) -> None:
        service = build_item_sync(None, None, IssuePolicy())  # type: ignore[arg-type]

        with pytest.raises(WrongPolicyError, match="IssuePolicy was handed a PR"):
            await service.sync(PULL_REQUEST)

    async def test_it_names_the_item_so_the_wiring_can_be_found(self) -> None:
        service = build_item_sync(None, None, IssuePolicy())  # type: ignore[arg-type]

        with pytest.raises(WrongPolicyError, match="Canon-Regularis/Shannon-bot#7"):
            await service.sync(PULL_REQUEST)

    async def test_it_is_permanent_so_the_worker_does_not_retry_a_wiring_bug(self) -> None:
        assert issubclass(WrongPolicyError, PermanentError)
