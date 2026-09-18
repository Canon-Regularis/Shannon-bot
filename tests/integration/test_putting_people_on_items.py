"""Putting somebody on an item from Discord, and what deliberately does not happen in the thread.

Issues #106 and #105. Two things this file exists to pin.

The first is an absence: the service writes to GitHub and stops. GitHub sends the change back as a
delivery, and the ordinary mirror rewrites the block and posts the line. Anything done here as well
would be the second copy of it.

The second is that a pull request has two lists rather than one. It can hold an assignee and a
reviewer at the same time, and they need not be the same person. The service used to infer which
list from the kind of item, which read well and left no way to assign a pull request at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.errors import RepositoryMismatchError
from shannon.domain.models import Actor
from shannon.services.people import ItemPeople
from shannon.services.workflow import NotAnItemThreadError, WorkflowRefusedError
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()
ALICE = 4242
# Deliberately nobody the fixtures already use. The default pull request is authored by
# octocat and already asks monalisa, and the default issue is assigned to hubot, so any of
# those three would be refused by the pure checks before reaching the part under test.
NEWBIE = Actor(login="newbie", github_user_id=999)


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[DeliveryClient]:
    """A pull request and an issue, both already mirrored into threads."""
    async with registered_stack(db_engine, db_session, threads) as http_client:
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        await deliver(http_client, "issues", payloads.issue_event("opened"), delivery="i0")
        yield http_client


@pytest_asyncio.fixture
async def linked(db_session: AsyncSession) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="newbie", github_user_id=999, discord_user_id=ALICE
    )
    await db_session.commit()


@pytest.fixture
def github(pr_event, issue_event) -> FakeGitHubClient:
    return FakeGitHubClient(
        pull_requests={(REPO_FULL, 7): pr_event("opened")},
        issues={(REPO_FULL, 12): issue_event("opened")},
    )


@pytest.fixture
def service(
    db_sessionmaker: async_sessionmaker[AsyncSession], github: FakeGitHubClient
) -> ItemPeople:
    return ItemPeople(
        db_sessionmaker,
        github,
        {
            ObjectType.PR: lambda owner, name, number: github.get_pull_request(owner, name, number),
            ObjectType.ISSUE: lambda owner, name, number: github.get_issue(owner, name, number),
        },
    )


def thread_for(threads: FakeThreadGateway, channel_id: int) -> int:
    return next(t.thread_id for t in threads.created if t.channel_id == channel_id)


class TestAPullRequest:
    """It has both lists, and which one is used is told to the service rather than guessed."""

    async def test_assigning_puts_them_on_the_assignees(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """Issue #105. This used to be impossible: a pull request always meant a reviewer."""
        outcome = await service.assign(thread_id=thread_for(threads, 99), discord_user_id=ALICE)

        assert github.people_calls == [("add_assignees", (REPO_FULL, 7), ("newbie",))]
        assert (outcome.role, outcome.added, outcome.login) == (ActorRole.ASSIGNEE, True, "newbie")

    async def test_asking_for_a_review_uses_the_reviewers(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        outcome = await service.request_review(
            thread_id=thread_for(threads, 99), discord_user_id=ALICE
        )

        assert github.people_calls == [("request_reviewers", (REPO_FULL, 7), ("newbie",))]
        assert outcome.role is ActorRole.REVIEWER

    async def test_one_person_can_hold_both_roles_at_once(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """GitHub keeps two lists and nothing stops somebody being on both. Inferring the list
        from the item made that unsayable, which is the whole of issue #105."""
        thread = thread_for(threads, 99)

        await service.assign(thread_id=thread, discord_user_id=ALICE)
        await service.request_review(thread_id=thread, discord_user_id=ALICE)

        assert [call[0] for call in github.people_calls] == ["add_assignees", "request_reviewers"]

    async def test_it_posts_nothing_into_the_thread(
        self, tracked, linked, service: ItemPeople, threads: FakeThreadGateway
    ) -> None:
        """The whole design in one assertion. GitHub's own delivery says it in the thread, once,
        through the mirror that already existed. A line from here would be the second copy."""
        before = len(threads.posts)

        await service.assign(thread_id=thread_for(threads, 99), discord_user_id=ALICE)

        assert len(threads.posts) == before

    async def test_withdrawing_a_review(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        github.pull_requests[(REPO_FULL, 7)] = replace(pr_event("opened"), reviewers=(NEWBIE,))

        outcome = await service.unrequest_review(
            thread_id=thread_for(threads, 99), discord_user_id=ALICE
        )

        assert github.people_calls == [("remove_reviewers", (REPO_FULL, 7), ("newbie",))]
        assert outcome.added is False

    async def test_the_author_can_be_assigned_though_not_asked_to_review(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """An asymmetry of GitHub's, and a second reason the two lists cannot share one command."""
        github.pull_requests[(REPO_FULL, 7)] = replace(pr_event("opened"), author=NEWBIE)
        thread = thread_for(threads, 99)

        await service.assign(thread_id=thread, discord_user_id=ALICE)

        with pytest.raises(WorkflowRefusedError, match="opened this pull request"):
            await service.request_review(thread_id=thread, discord_user_id=ALICE)

        assert github.people_calls == [("add_assignees", (REPO_FULL, 7), ("newbie",))]

    async def test_somebody_already_asked_is_refused_without_asking_github(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        github.pull_requests[(REPO_FULL, 7)] = replace(pr_event("opened"), reviewers=(NEWBIE,))

        with pytest.raises(WorkflowRefusedError, match="already been asked"):
            await service.request_review(thread_id=thread_for(threads, 99), discord_user_id=ALICE)

        assert github.people_calls == []


class TestAnIssue:
    async def test_assigning_works_the_same_way(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == [("add_assignees", (REPO_FULL, 12), ("newbie",))]
        assert outcome.role is ActorRole.ASSIGNEE

    async def test_asking_for_a_review_on_one_is_refused(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """An issue has no reviewers at all. Refused here rather than left to GitHub, whose 404
        for the endpoint tells nobody anything they can act on."""
        with pytest.raises(WorkflowRefusedError, match="an issue has no reviewers"):
            await service.request_review(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_it_asks_whether_github_would_take_them_first(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.assignable_calls == [(REPO_FULL, "newbie")]

    async def test_somebody_github_will_not_take_is_refused_before_the_write(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """GitHub does not refuse this one. It drops the person and answers as though it had done
        what was asked, so without the question first the command reports success for nothing."""
        github.unassignable = {"newbie"}

        with pytest.raises(WorkflowRefusedError, match="no access to the repository"):
            await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_taking_somebody_off_asks_nothing_first(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """Whether GitHub would ADD them says nothing about removing one who is already there."""
        github.issues[(REPO_FULL, 12)] = replace(issue_event("opened"), assignees=(NEWBIE,))

        await service.unassign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.assignable_calls == []
        assert github.people_calls == [("remove_assignees", (REPO_FULL, 12), ("newbie",))]


class TestWhatItRefuses:
    async def test_a_thread_this_bot_does_not_track(
        self, tracked, linked, service: ItemPeople
    ) -> None:
        with pytest.raises(NotAnItemThreadError):
            await service.assign(thread_id=999999, discord_user_id=ALICE)

    async def test_a_project_board_card(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        db_session: AsyncSession,
    ) -> None:
        """A card has no page on GitHub, so there is nothing to put anybody on. The container
        gives this service a reader for pull requests and issues and nothing else, and that
        absence is what this refusal is made of."""
        repository = await db_session.scalar(select(Repository))
        assert repository is not None
        db_session.add(
            TrackedItem(
                repository_id=repository.id,
                github_object_id=555,
                github_object_type=ObjectType.TICKET,
                github_object_number=1,
                github_url="",
                title="A card",
                discord_thread_id=7777,
            )
        )
        await db_session.commit()

        with pytest.raises(WorkflowRefusedError, match="board card"):
            await service.assign(thread_id=7777, discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_a_member_who_has_not_linked(
        self,
        tracked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        with pytest.raises(WorkflowRefusedError, match="/link"):
            await service.assign(thread_id=thread_for(threads, 99), discord_user_id=7777)

        assert github.people_calls == []

    async def test_a_repository_renamed_away_from_under_it(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """A stored name is not an identity. Unchecked, the write lands on whichever repository
        holds that name now, which is somebody else's pull request."""
        snapshot = pr_event("opened")
        github.pull_requests[(REPO_FULL, 7)] = replace(
            snapshot, repository=replace(snapshot.repository, github_repo_id=999999)
        )

        with pytest.raises(RepositoryMismatchError):
            await service.assign(thread_id=thread_for(threads, 99), discord_user_id=ALICE)

        assert github.people_calls == []
