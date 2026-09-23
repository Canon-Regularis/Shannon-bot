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
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.identities import ProvedAccount
from shannon.db.stores.user_links import LinkedAccount, UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.errors import RepositoryMismatchError
from shannon.domain.models import Actor
from shannon.github.errors import GitHubUnavailableError
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
    # The account behind the linked login, so the fake agrees with itself about who 999 is.
    # Without it every test here takes the branch for an account GitHub no longer has, which
    # happens to reach the same answer by the wrong road and would hide a real break.
    return FakeGitHubClient(
        pull_requests={(REPO_FULL, 7): pr_event("opened")},
        issues={(REPO_FULL, 12): issue_event("opened")},
        logins={999: "newbie"},
    )


class FakeProof:
    """What GitHub has vouched for, out of a mapping of Discord account to GitHub account.

    Nothing is looked up by login, deliberately: the real check holds the proved account id
    against the one on the link, because a name moves and an account does not.
    """

    def __init__(self, *, configured: bool = True, proved: dict[int, int] | None = None) -> None:
        self.configured = configured
        self.proved = {} if proved is None else proved

    async def ever_proved(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None:
        found = self.proved.get(discord_user_id)
        if found is None:
            return None
        return ProvedAccount(
            login="newbie", github_user_id=found, verified_at=datetime(2026, 9, 22, tzinfo=UTC)
        )


@pytest.fixture
def proof() -> FakeProof:
    """Proved by default, so the tests in this file are about what they say they are about.

    The unproved case is a class of its own below rather than a condition every assertion here
    has to step around.
    """
    return FakeProof(proved={ALICE: 999})


def people_service(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    github: FakeGitHubClient,
    proof: FakeProof,
    *,
    require_proved: bool = False,
) -> ItemPeople:
    return ItemPeople(
        db_sessionmaker,
        github,
        {
            ObjectType.PR: lambda owner, name, number: github.get_pull_request(owner, name, number),
            ObjectType.ISSUE: lambda owner, name, number: github.get_issue(owner, name, number),
        },
        proof,
        require_proved=require_proved,
    )


@pytest.fixture
def service(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    github: FakeGitHubClient,
    proof: FakeProof,
) -> ItemPeople:
    return people_service(db_sessionmaker, github, proof)


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
        github.permissions = {"newbie": "none"}

        with pytest.raises(WorkflowRefusedError, match="as a collaborator"):
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


class TestALoginThatMoved:
    """A GitHub account renamed since somebody ran /link. Issue #133.

    The stored login is a label, and GitHub hands a freed one straight back out. Asked about a
    name its owner no longer answers to, GitHub says it has never heard of them, and that was
    reported as a member with no access to the repository. The id was in the row the whole time.
    """

    async def test_a_renamed_account_is_followed_and_the_command_still_works(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """The whole point: it succeeds, rather than explaining itself and stopping."""
        github.logins = {999: "wanderer"}

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.login == "wanderer"
        assert github.people_calls == [("add_assignees", (REPO_FULL, 12), ("wanderer",))]

    async def test_the_row_is_corrected_so_the_next_run_costs_nothing(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        github.logins = {999: "wanderer"}

        await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        # The service wrote through its own sessionmaker, so this one is holding the row it read
        # before that happened.
        db_session.expunge_all()
        found = await UserLinkStore(db_session).account_for(guild_id=1, discord_user_id=ALICE)
        assert found == LinkedAccount(login="wanderer", github_user_id=999)

    @pytest.mark.parametrize(
        ("command", "channel", "already_on"),
        [
            ("assign", 98, False),
            ("unassign", 98, True),
            ("request_review", 99, False),
            ("unrequest_review", 99, True),
        ],
    )
    async def test_every_one_of_the_four_follows_it(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        issue_event,
        pr_event,
        command: str,
        channel: int,
        already_on: bool,
    ) -> None:
        """What justifies following the rename where the login is read rather than where the
        refusal is written. Three of these never reach GitHub with a stale name at all: the pure
        checks compare it against who is on the item, so a removal under the new name is refused
        as somebody who was never there, and no endpoint is touched to find out otherwise.
        """
        github.logins = {999: "wanderer"}
        moved = Actor(login="wanderer", github_user_id=999)
        if already_on:
            github.issues[(REPO_FULL, 12)] = replace(issue_event("opened"), assignees=(moved,))
            github.pull_requests[(REPO_FULL, 7)] = replace(pr_event("opened"), reviewers=(moved,))

        commands = {
            "assign": service.assign,
            "unassign": service.unassign,
            "request_review": service.request_review,
            "unrequest_review": service.unrequest_review,
        }
        await commands[command](thread_id=thread_for(threads, channel), discord_user_id=ALICE)

        assert [logins for _, _, logins in github.people_calls] == [("wanderer",)]

    async def test_a_row_from_before_the_id_column_uses_the_claim_as_made(
        self,
        tracked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """Nothing can invent an id for a link made before the column, so there is nothing to ask
        about. Worth pinning that it does not ask anyway: a tree full of these would spend a
        GitHub call per command to be told nothing."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="newbie", github_user_id=None, discord_user_id=ALICE
        )
        await db_session.commit()

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.login == "newbie"
        assert github.login_calls == [], "it asked about an account it had no id for"

    async def test_an_account_github_no_longer_has_falls_back_to_the_claim(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """A deleted account leaves the name as the only thing there is, which is what every one
        of these commands used before this existed."""
        github.logins = {}

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.login == "newbie"

    async def test_github_being_unreachable_does_not_stop_the_command(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """Best effort, and this is the test that says so. The call is anonymous and sits on the
        smaller of GitHub's two hourly budgets; spending it must not be able to refuse an
        assignment that would have worked yesterday."""
        github.login_error = GitHubUnavailableError("Could not reach GitHub")

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.login == "newbie"
        assert github.people_calls == [("add_assignees", (REPO_FULL, 12), ("newbie",))]

    async def test_a_login_another_member_already_holds_is_not_stolen(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The command still does what was asked, because GitHub is clear about who account 999
        is now. What it must not do is take the name off the row holding it: /link deletes what
        is in the way because somebody stated a claim, and this is not somebody stating one."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="wanderer", github_user_id=222, discord_user_id=77
        )
        await db_session.commit()
        github.logins = {999: "wanderer"}

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.login == "wanderer"
        db_session.expunge_all()
        store = UserLinkStore(db_session)
        assert await store.account_for(guild_id=1, discord_user_id=ALICE) == LinkedAccount(
            login="newbie", github_user_id=999
        )
        assert await store.account_for(guild_id=1, discord_user_id=77) == LinkedAccount(
            login="wanderer", github_user_id=222
        )

    async def test_nothing_is_written_when_the_login_still_matches(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The ordinary case, which is almost every case. One question, no transaction."""
        await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.login_calls == [999]
        db_session.expunge_all()
        found = await UserLinkStore(db_session).account_for(guild_id=1, discord_user_id=ALICE)
        assert found == LinkedAccount(login="newbie", github_user_id=999)


class TestWhyGitHubWouldNotTakeThem:
    """The sentence a refusal carries. Issue #133.

    One sentence used to cover every refusal, saying the person had no access to the repository.
    The assignee endpoint answers the same 404 for somebody who is not there, somebody who is
    there without write access, and a login GitHub has never heard of.
    """

    async def test_somebody_with_no_access_is_told_exactly_that(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        github.permissions = {"newbie": "none"}

        with pytest.raises(WorkflowRefusedError, match="has to invite them first"):
            await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_somebody_who_can_only_read_is_told_that_instead(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """The case the old sentence was least true of. They are in the repository; what they are
        missing is write access, and saying "no access" sends somebody looking in the wrong
        place."""
        github.permissions = {"newbie": "read"}

        with pytest.raises(WorkflowRefusedError, match="write access or better"):
            await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_write_access_that_github_still_refuses_says_so_honestly(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """Two GitHub answers disagreeing. Nothing here can explain it, and inventing a reason is
        what got this reported in the first place."""
        github.unassignable = {"newbie"}

        with pytest.raises(WorkflowRefusedError, match="Those two answers should agree"):
            await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_a_permission_that_cannot_be_read_still_refuses_clearly(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """A diagnosis must not be able to make the answer worse. The refusal stands, and the one
        thing added is an admission that the follow-up question could not be put."""
        github.unassignable = {"newbie"}
        github.permission_error = GitHubUnavailableError("Could not reach GitHub")

        with pytest.raises(WorkflowRefusedError, match="could not be asked why"):
            await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_the_permission_is_only_asked_once_the_answer_was_no(
        self,
        tracked,
        linked,
        service: ItemPeople,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """It explains a refusal and is worth nothing otherwise, so an assignment that lands must
        not spend a call on it."""
        await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.permission_calls == []


class TestALinkNobodyProved:
    """`/link` records a login an admin typed and GitHub was never asked whose it is.

    So a wrong one acts on a real repository as somebody who has nothing to do with the member
    named in the command. Warned about by default and refused once the server has had the chance
    to run `/link`, because refusing on the day this ships would stop every assignment at once.
    """

    async def test_an_unproved_link_still_works_and_says_so(
        self,
        tracked,
        linked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        service = people_service(db_sessionmaker, github, FakeProof())

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.proved is False
        assert github.people_calls == [("add_assignees", (REPO_FULL, 12), ("newbie",))]

    async def test_a_proved_link_says_nothing_about_it(
        self, tracked, linked, service: ItemPeople, threads: FakeThreadGateway
    ) -> None:
        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.proved is True

    async def test_with_the_setting_on_an_unproved_link_is_refused(
        self,
        tracked,
        linked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        service = people_service(db_sessionmaker, github, FakeProof(), require_proved=True)

        with pytest.raises(WorkflowRefusedError, match="run /link"):
            await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_with_the_setting_on_a_proved_link_goes_through(
        self,
        tracked,
        linked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        proof: FakeProof,
        threads: FakeThreadGateway,
    ) -> None:
        service = people_service(db_sessionmaker, github, proof, require_proved=True)

        await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == [("add_assignees", (REPO_FULL, 12), ("newbie",))]

    async def test_taking_somebody_off_is_refused_on_the_same_terms(
        self,
        tracked,
        linked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """All four, rather than the two that add somebody. Taking a reviewer off under a name
        nobody proved is the same claim as putting one on."""
        github.issues[(REPO_FULL, 12)] = replace(issue_event("opened"), assignees=(NEWBIE,))
        service = people_service(db_sessionmaker, github, FakeProof(), require_proved=True)

        with pytest.raises(WorkflowRefusedError, match="run /link"):
            await service.unassign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert github.people_calls == []

    async def test_a_deployment_that_cannot_verify_anybody_warns_rather_than_refusing(
        self,
        tracked,
        linked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """The escape hatch, and it is load-bearing. Without a public URL the round trip cannot
        run, so `/link` refuses too; enforcing there would leave every member holding a link
        they have no way to prove and a command that will not act on it."""
        service = people_service(
            db_sessionmaker, github, FakeProof(configured=False), require_proved=True
        )

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.proved is False
        assert github.people_calls == [("add_assignees", (REPO_FULL, 12), ("newbie",))]

    async def test_a_proof_survives_a_rename(
        self,
        tracked,
        linked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        proof: FakeProof,
        threads: FakeThreadGateway,
    ) -> None:
        """Held on the account id, which is the half that lasts. A proof invalidated by somebody
        changing their GitHub display name would send them back to the browser for nothing."""
        github.logins = {999: "wanderer"}
        service = people_service(db_sessionmaker, github, proof, require_proved=True)

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert (outcome.login, outcome.proved) == ("wanderer", True)

    async def test_a_link_pointed_somewhere_else_loses_its_proof(
        self,
        tracked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        github: FakeGitHubClient,
        proof: FakeProof,
        threads: FakeThreadGateway,
    ) -> None:
        """The other side of holding it on the id. An admin re-pointing somebody's link at a
        different account has not been vouched for by anybody, and must not inherit the proof."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="newbie", github_user_id=1234, discord_user_id=ALICE
        )
        await db_session.commit()
        github.logins = {1234: "newbie"}
        service = people_service(db_sessionmaker, github, proof, require_proved=True)

        with pytest.raises(WorkflowRefusedError, match="run /link"):
            await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

    async def test_a_row_from_before_the_id_column_cannot_be_proved(
        self,
        tracked,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        github: FakeGitHubClient,
        proof: FakeProof,
        threads: FakeThreadGateway,
    ) -> None:
        """There is nothing to compare, and reading no evidence as a yes is what this path is
        here to stop."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="newbie", github_user_id=None, discord_user_id=ALICE
        )
        await db_session.commit()
        service = people_service(db_sessionmaker, github, proof)

        outcome = await service.assign(thread_id=thread_for(threads, 98), discord_user_id=ALICE)

        assert outcome.proved is False
