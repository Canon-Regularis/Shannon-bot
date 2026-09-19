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
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy, TicketPolicy

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


class TestWhatEachPolicyShuts:
    """Three answers, and the pull request needs all three.

    True and False are the thread being shut and given back. None is the one that is easy to
    lose: it means leave the thread exactly as it is, and it is the only thing standing between
    `/set_done` and the next `synchronize` webhook undoing it.
    """

    def test_a_closed_pull_request_is_shut(self) -> None:
        closed = replace(PULL_REQUEST, state="closed")

        assert PullRequestPolicy().shut(closed, status=Status.NOT_REVIEWED) is True

    def test_a_merged_pull_request_is_shut(self) -> None:
        """Merged and abandoned are one case here, which `display_state` already folds."""
        merged = replace(PULL_REQUEST, state="closed", merged=True)

        assert PullRequestPolicy().shut(merged, status=Status.NOT_REVIEWED) is True

    def test_set_done_survives_the_next_webhook(self) -> None:
        """The trap. `/set_done` shuts an OPEN pull request's thread and the payload has no idea,
        so answering False here would give the thread back on the next `synchronize` and undo a
        command somebody ran on purpose. Only the row's status can tell it apart.
        """
        assert PullRequestPolicy().shut(PULL_REQUEST, status=Status.DONE) is None

    def test_a_reopened_pull_request_gets_its_thread_back(self) -> None:
        """The other half, and the reason None is not the answer for every open one. Nothing else
        on any path ever unshuts a pull request's thread."""
        assert PullRequestPolicy().shut(PULL_REQUEST, status=Status.NOT_REVIEWED) is False

    def test_an_open_issue_is_given_back(self) -> None:
        assert IssuePolicy().shut(ISSUE, status=Status.NOT_REVIEWED) is False

    def test_a_closed_issue_is_shut(self) -> None:
        closed = replace(ISSUE, state="closed")

        assert IssuePolicy().shut(closed, status=Status.NOT_REVIEWED) is True

    def test_an_issue_does_not_read_the_status(self) -> None:
        """It has no `/set_done` of its own: the command sends you to close it on GitHub, so the
        payload is the whole story and DONE on an open issue means nothing here."""
        assert IssuePolicy().shut(ISSUE, status=Status.DONE) is False


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


class TestWhatTheRowAloneSaysAboutTheLock:
    """Asked where there is no payload to ask instead: a delivery turned away as superseded is
    refused before anything reads its snapshot, so the row is the only thing that knows whether
    the item's thread is finished with.

    Each kind answers from a different column, and the differences are the point.
    """

    def test_a_pull_request_is_finished_when_somebody_says_so_or_github_does(self) -> None:
        """`/set_done` writes the status, and closing or merging writes the state. Either shuts
        it, which is why both arms are asserted: an `or` short-circuits."""
        policy = PullRequestPolicy()

        assert policy.shut_for_state(status=Status.DONE, github_state="open") is True
        assert policy.shut_for_state(status=Status.IN_REVIEW, github_state="closed") is True
        assert policy.shut_for_state(status=Status.IN_REVIEW, github_state="merged") is True
        assert policy.shut_for_state(status=Status.IN_REVIEW, github_state="open") is False

    def test_an_issue_is_finished_when_github_closes_it(self) -> None:
        """Its state, not its status. `/set_done` can put an open issue at DONE, and an open
        issue's thread is one people are still meant to be talking in."""
        policy = IssuePolicy()

        assert policy.shut_for_state(status=Status.NOT_REVIEWED, github_state="closed") is True
        assert policy.shut_for_state(status=Status.DONE, github_state="open") is False

    def test_a_ticket_is_never_finished_with(self) -> None:
        """A card in Done is a card somebody can drag back out, and nothing in this bot would
        ever unlock its thread again."""
        policy = TicketPolicy()

        assert policy.shut_for_state(status=Status.DONE, github_state="open") is False
